"""issue-keeper 入口。

用法:
  # keeper 行为
  python -m issue_keeper keep --config config.yaml --once      # 一次性扫描
  python -m issue_keeper keep --config config.yaml             # daemon 轮询

  # internal source 管理（自建 issue 系统）
  python -m issue_keeper internal create  PROJECT --title "..." --body "..." [--author alice] [--actor-type human|agent] [--kind issue] [--db PATH]
  python -m issue_keeper internal comment PROJECT NUMBER --body "..." [--author bob] [--actor-type human|agent] [--kind issue] [--db PATH]
  python -m issue_keeper internal move    PROJECT NUMBER --status todo|doing|review|done|closed [--author ...] [--actor-type ...] [--comment "..."] [--kind issue] [--db PATH]
  python -m issue_keeper internal list    PROJECT [--kind issue] [--db PATH]
  python -m issue_keeper internal board   PROJECT [--kind issue] [--db PATH]
  python -m issue_keeper internal show    PROJECT NUMBER [--kind issue] [--db PATH]
  python -m issue_keeper internal close   PROJECT NUMBER [--kind issue] [--db PATH]
  python -m issue_keeper internal projects [--db PATH]

  internal 子命令的 --db 可覆盖默认 SQLite 路径（~/.issue-keeper/internal.db），
  也可以不传——它会用全局默认路径，与 keeper 的 internal source 共享同一个库。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import load_config
from .keeper import run_daemon, run_once


def _run_keeper(args) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"加载配置失败: {e}", file=sys.stderr)
        return 2

    if not config.repos:
        print("配置里没有绑定任何仓库 (repos 为空)", file=sys.stderr)
        return 2

    if args.once:
        handled = run_once(config)
        print(f"完成，本轮处理 {handled} 条。")
        return 0

    run_daemon(config)
    return 0


def _find_issue(src, project: str, kind: str, number: int):
    """从 DB 直接查任意状态的 issue（不限于 open）。"""
    return src.get_issue(project, kind, number)


def _run_internal(args) -> int:
    """internal source 管理 CLI。不依赖 config.yaml，直接用 --db。"""
    from .sources.internal import InternalSource, DEFAULT_DB, STATUSES
    from .config import RepoBinding

    db_path = args.db or str(DEFAULT_DB)
    binding = RepoBinding(
        repo=args.project if hasattr(args, "project") else "",
        profile="",
        source="internal",
        agent_label=args.author or "cli-user",
        internal_db=db_path,
    )
    src = InternalSource(binding=binding)
    kind = args.kind

    if args.internal_cmd == "create":
        project = args.project
        actor_type = args.actor_type
        res = src.create_issue(
            repo=project, kind=kind,
            title=args.title, body=args.body or "",
            author=args.author or ("anonymous" if actor_type == "human" else "anonymous-agent"),
            actor_type=actor_type,
        )
        print(f"已创建 {kind} #{res.number}：{res.title}")
        print(f"  作者: {res.author}（{actor_type}）")
        print(f"  状态: {res.status}")
        print(f"  项目: {project}")
        print(f"  数据库: {db_path}")
        if args.body:
            print(f"  正文:\n{args.body}")
        return 0

    if args.internal_cmd == "comment":
        project = args.project
        match = _find_issue(src, project, kind, args.number)
        if match is None:
            print(f"找不到 {kind} #{args.number}（项目 {project}）", file=sys.stderr)
            return 1
        c = src.add_comment(
            repo=project, resource=match,
            body=args.body or "", author=args.author or "anonymous",
            actor_type=args.actor_type,
        )
        print(f"已在 {kind} #{args.number} 添加评论（id={c.id}，作者={c.author}，{args.actor_type}）")
        return 0

    if args.internal_cmd == "move":
        project = args.project
        match = _find_issue(src, project, kind, args.number)
        if match is None:
            print(f"找不到 {kind} #{args.number}（项目 {project}）", file=sys.stderr)
            return 1
        ok, from_status = src.move_status(
            repo=project, resource=match, to_status=args.status,
            actor=args.author or "cli-user", actor_type=args.actor_type,
            comment=args.comment or "",
        )
        if ok:
            print(f"{kind} #{args.number}: {from_status} → {args.status}（by {args.author or 'cli-user'}）")
            return 0
        print(f"{kind} #{args.number} 已经是 {args.status} 状态", file=sys.stderr)
        return 1

    if args.internal_cmd == "list":
        project = args.project
        resources = src.list_open(project, [kind])
        if not resources:
            print(f"项目 {project} 没有开放的 {kind}")
            return 0
        print(f"项目 {project} 的开放 {kind}（共 {len(resources)} 条）：")
        for r in resources:
            print(f"  #{r.number}  [{r.status}]  [{r.author}/{r.actor_type}]  {r.title}")
            body_preview = (r.body or "").replace("\n", " ")[:60]
            if body_preview:
                suffix = "…" if len(r.body or "") > 60 else ""
                print(f"         {body_preview}{suffix}")
        return 0

    if args.internal_cmd == "board":
        project = args.project
        all_issues = src.list_all(project, [kind])
        # 按状态分组
        by_status: dict[str, list] = {s: [] for s in STATUSES}
        for r in all_issues:
            by_status.setdefault(r.status, []).append(r)
        print(f"项目 {project} 的看板（共 {len(all_issues)} 条 {kind}）：")
        for status in STATUSES:
            issues = by_status.get(status, [])
            if not issues:
                continue
            print(f"\n  [{status}]（{len(issues)}）")
            for r in issues:
                author_tag = f"{r.author}/{r.actor_type}"
                assignee_tag = f" → {r.assignee}" if r.assignee else ""
                print(f"    #{r.number}  [{author_tag}]{assignee_tag}  {r.title}")
        return 0

    if args.internal_cmd == "show":
        project = args.project
        match = _find_issue(src, project, kind, args.number)
        if match is None:
            print(f"找不到 {kind} #{args.number}（项目 {project}）", file=sys.stderr)
            return 1
        print(f"{match.kind} #{match.number}：{match.title}")
        print(f"  作者: {match.author}（{match.actor_type}）")
        print(f"  状态: {match.status}")
        if match.assignee:
            print(f"  负责人: {match.assignee}")
        print(f"  创建: {match.created_at}")
        print(f"  更新: {match.updated_at}")
        if match.body:
            print(f"\n  正文:\n{match.body}")

        # 状态历史
        history = src.list_status_history(project, match)
        if history:
            print(f"\n  状态历史（{len(history)} 条）：")
            for h in history:
                fr = h["from_status"] or "—"
                to = h["to_status"]
                actor = h["actor"]
                atype = h["actor_type"]
                cmt = f"  备注: {h['comment']}" if h["comment"] else ""
                print(f"    {fr} → {to}  by {actor}（{atype}）  {h['created_at']}{cmt}")

        cmts = src.list_comments(project, match)
        if cmts:
            print(f"\n  评论（{len(cmts)} 条）：")
            for c in cmts:
                print(f"    [{c.author}] ({c.created_at}):")
                for line in (c.body or "").splitlines() or [""]:
                    print(f"      {line}")
        else:
            print("\n  （暂无评论）")
        return 0

    if args.internal_cmd == "close":
        project = args.project
        match = _find_issue(src, project, kind, args.number)
        if match is None:
            print(f"找不到 {kind} #{args.number}（项目 {project}）", file=sys.stderr)
            return 1
        ok = src.close_issue(
            project, match,
            actor=args.author or "cli-user", actor_type=args.actor_type,
        )
        if ok:
            print(f"已关闭 {kind} #{args.number}")
            return 0
        print(f"{kind} #{args.number} 已经是关闭状态", file=sys.stderr)
        return 1

    if args.internal_cmd == "projects":
        # 列出所有项目（从 issues 表 distinct project）
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT project, COUNT(*) as n, "
                "SUM(CASE WHEN status IN ('inbox','todo','doing','review') THEN 1 ELSE 0 END) as open_n "
                "FROM issues GROUP BY project ORDER BY project"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            print("没有项目")
            return 0
        print(f"所有项目（共 {len(rows)} 个）：")
        for r in rows:
            print(f"  {r[0]}  共 {r[1]} 条 issue（{r[2]} 条开放）")
        return 0

    return 2


def _add_internal_common(p: argparse.ArgumentParser) -> None:
    """给 internal 子命令加共用参数。"""
    p.add_argument("project", help="项目名（对应 binding.repo）")
    p.add_argument("--kind", default="issue", choices=["issue", "pr"])
    p.add_argument("--author", default="", help="提交/评论作者身份")
    p.add_argument("--actor-type", default="human", choices=["human", "agent"],
                   help="作者角色：human（默认）/ agent")
    p.add_argument("--db", help=f"SQLite 路径（默认 {Path.home()}/.issue-keeper/internal.db）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="issue-keeper",
        description="监控 issue 并调用 agent 处理；含 internal source 管理 CLI",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    # keeper 行为
    keeper_parser = subparsers.add_parser("keep", help="运行 keeper")
    keeper_parser.add_argument("--config", "-c", required=True)
    keeper_parser.add_argument("--once", action="store_true")
    keeper_parser.add_argument("--log-level", default="INFO",
                               choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    # dashboard（Web 看板，读 internal.db）
    dash_parser = subparsers.add_parser("dashboard", help="启动 Web 看板（读 internal source）")
    dash_parser.add_argument("--port", type=int, default=7433)
    dash_parser.add_argument("--host", default="127.0.0.1")
    dash_parser.add_argument("--db", help=f"SQLite 路径（默认 {Path.home()}/.issue-keeper/internal.db）")
    dash_parser.add_argument("--agent-label", default="dashboard",
                             help="dashboard 在 db 里发评论/改状态时用的身份（默认 dashboard）")

    # internal source 管理
    internal_parser = subparsers.add_parser("internal", help="管理 internal source 的 issue")
    internal_sub = internal_parser.add_subparsers(dest="internal_cmd", required=True)

    p_create = internal_sub.add_parser("create", help="提一个新 issue/PR")
    _add_internal_common(p_create)
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--body", default="")

    p_comment = internal_sub.add_parser("comment", help="在某 issue 下评论")
    _add_internal_common(p_comment)
    p_comment.add_argument("number", type=int, help="issue 编号")
    p_comment.add_argument("--body", required=True)

    p_move = internal_sub.add_parser("move", help="改 issue 状态")
    _add_internal_common(p_move)
    p_move.add_argument("number", type=int, help="issue 编号")
    p_move.add_argument("--status", required=True,
                        choices=["inbox", "todo", "doing", "review", "done", "closed"])
    p_move.add_argument("--comment", default="", help="状态变更备注（可选）")

    p_list = internal_sub.add_parser("list", help="列出某项目的 open issue")
    _add_internal_common(p_list)

    p_board = internal_sub.add_parser("board", help="看板视图（按状态分列）")
    _add_internal_common(p_board)

    p_show = internal_sub.add_parser("show", help="查看某 issue 详情、状态历史和评论")
    _add_internal_common(p_show)
    p_show.add_argument("number", type=int, help="issue 编号")

    p_close = internal_sub.add_parser("close", help="关闭某 issue")
    _add_internal_common(p_close)
    p_close.add_argument("number", type=int, help="issue 编号")

    # projects 不需要 project 参数
    p_projects = internal_sub.add_parser("projects", help="列出所有项目")
    p_projects.add_argument("--db", help=f"SQLite 路径（默认 {Path.home()}/.issue-keeper/internal.db）")
    p_projects.set_defaults(actor_type="human", kind="issue", author="", project="")

    args = parser.parse_args(argv)

    if args.cmd == "keep":
        return _run_keeper(args)
    if args.cmd == "internal":
        return _run_internal(args)
    if args.cmd == "dashboard":
        from .dashboard import run_dashboard
        from .sources.internal import DEFAULT_DB
        db_path = args.db or str(DEFAULT_DB)
        run_dashboard(db_path, host=args.host, port=args.port,
                      agent_label=args.agent_label)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
