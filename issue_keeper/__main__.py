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

    run_daemon(args.config)
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
            labels=args.label or [],
        )
        print(f"已创建 {kind} #{res.number}：{res.title}")
        print(f"  作者: {res.author}（{actor_type}）")
        print(f"  状态: {res.status}")
        if res.labels:
            print(f"  标签: {', '.join(res.labels)}")
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
        resources = src.list_open(project, [kind], labels=args.label or None)
        if not resources:
            print(f"项目 {project} 没有开放的 {kind}")
            return 0
        print(f"项目 {project} 的开放 {kind}（共 {len(resources)} 条）：")
        for r in resources:
            label_tag = f"  [{','.join(r.labels)}]" if r.labels else ""
            print(f"  #{r.number}  [{r.status}]  [{r.author}/{r.actor_type}]  {r.title}{label_tag}")
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


def _run_team(args) -> int:
    """项目绑定 + 团队介绍管理 CLI。数据存 internal.db 的 projects 表。"""
    from .sources.internal import DEFAULT_DB
    from .team import (add_project_to_db, import_from_config, load_team,
                       remove_project_from_db, set_intro)

    db_path = args.db or str(DEFAULT_DB)

    if args.team_cmd == "import":
        members = import_from_config(args.config, db_path)
        print(f"已从 {args.config} 迁移 {len(members)} 个项目绑定到 db（{db_path}）：")
        for m in members:
            intro_tag = "（有介绍）" if m.intro else "（无介绍）"
            print(f"  {m.agent_label:20} 项目={m.project}  {intro_tag}")
        return 0

    if args.team_cmd == "add":
        env: dict[str, str] = {}
        for kv in args.env or []:
            if "=" in kv:
                k, v = kv.split("=", 1)
                env[k.strip()] = v
        add_project_to_db(
            db_path, repo=args.repo, agent_label=args.agent_label, cwd=args.cwd,
            profile=args.profile, source=args.source,
            github_token=args.github_token,
            monitor_prs=args.monitor_prs, env=env or None,
        )
        print(f"已写入项目绑定: repo={args.repo}, agent_label={args.agent_label}, cwd={args.cwd}")
        return 0

    if args.team_cmd == "remove":
        ok = remove_project_from_db(db_path, repo=args.repo)
        if ok:
            print(f"已从 db 删除项目绑定: {args.repo}（其 issue 保留）")
            return 0
        print(f"未找到项目: {args.repo}", file=sys.stderr)
        return 1

    if args.team_cmd == "set-intro":
        name = set_intro(args.agent_label, args.intro, db_path)
        if name is None:
            print(f"找不到 agent_label={args.agent_label}，请先 `team add` 添加项目",
                  file=sys.stderr)
            return 1
        print(f"已设置 {args.agent_label}（项目 {name}）的介绍（{len(args.intro)} 字）")
        return 0

    if args.team_cmd == "list":
        members = load_team(db_path)
        if not members:
            print("db 里还没有项目绑定。用 `team add` 添加，或 `team import -c 旧config.yaml` 迁移。")
            return 0
        print(f"项目绑定 + 团队（共 {len(members)} 个，db {db_path}）：")
        for m in members:
            prs = " [监控PR]" if m.monitor_prs else ""
            print(f"\n  {m.agent_label}")
            print(f"    项目: {m.project}  profile={m.profile}  source={m.source}{prs}")
            if m.cwd:
                print(f"    工作目录: {m.cwd}")
            print(f"    介绍: {m.intro or '（待生成）'}")
        return 0

    return 2


def _run_onboard(args) -> int:
    """把一个新项目 onboard 进 issue-keeper 协同：写 db 绑定 + 可选生成介绍 + 可选重载 keeper。

    db 是单一配置源，写入后 keeper 下一轮 live-reload 即生效（--reload 仅用于立即重载或 keeper 异常时）。
    """
    import os
    from pathlib import Path
    from .sources.internal import DEFAULT_DB
    from .team import add_project_to_db, set_intro

    project_path = Path(args.project_path).expanduser().resolve()
    if not project_path.is_dir():
        print(f"项目目录不存在: {project_path}", file=sys.stderr)
        return 1
    repo = args.repo or project_path.name
    agent_label = args.agent_label or f"{repo}-agent"
    db_path = args.db or str(DEFAULT_DB)

    # 1) 写 db 绑定（单一配置源）
    add_project_to_db(
        db_path, repo=repo, agent_label=agent_label, cwd=str(project_path),
        profile=args.profile,
    )
    print(f"已写入 db 绑定: repo={repo}, agent_label={agent_label}, cwd={project_path}（db: {db_path}）")

    # 2) 可选：生成自我介绍
    if args.gen_intro:
        if not os.environ.get("DEEPSEEK_API_KEY"):
            print("未设置 DEEPSEEK_API_KEY，跳过介绍生成（可后续 `team set-intro` 手填）", file=sys.stderr)
        else:
            from .profile import ProfileEntry, invoke_agent
            env = {
                "ANTHROPIC_API_KEY": os.environ["DEEPSEEK_API_KEY"],
                "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
                "CLAUDE_MODEL": "deepseek-chat",
            }
            entry = ProfileEntry(name=args.profile, is_hub=True, cwd=str(project_path),
                                 env=env, timeout_secs=180)
            prompt = (
                "请用 100-180 字写一段自我介绍，用于 issue-keeper dashboard 团队页卡片展示。"
                "覆盖：本项目是做什么的（一句话）、你作为本项目 agent 负责什么（一句话）、技术栈（一两个关键词）。"
                "只输出一段纯文本，不要 markdown 标题、不要列表、不要元描述。"
            )
            try:
                reply = invoke_agent(entry, prompt, "", from_user="onboard", default_timeout=180)
                intro = (reply.text or "").strip()
                set_intro(agent_label, intro, db_path)
                print(f"已生成并写入介绍（{len(intro)} 字）")
            except Exception as e:
                print(f"介绍生成失败: {e}", file=sys.stderr)

    # 4) 可选：重载 keeper（仅 macOS launchd）
    if args.reload:
        import platform
        if platform.system() == "Darwin":
            import subprocess
            plist = Path.home() / "Library/LaunchAgents/com.issue-keeper.keeper.plist"
            if plist.exists():
                subprocess.run(["launchctl", "unload", str(plist)], check=False)
                subprocess.run(["launchctl", "load", str(plist)], check=False)
                print("已重载 keeper daemon")
            else:
                print(f"未找到 {plist}，跳过重载（请手工重启 keeper）", file=sys.stderr)
        else:
            print("非 macOS，跳过 launchd 重载（请手工重启 keeper）", file=sys.stderr)

    print(f"\nonboard 完成：{agent_label}（项目 {repo}）已加入协同。")
    return 0


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

    # 团队成员 + 项目绑定管理（数据存 internal.db 的 projects 表，单一配置源）
    team_parser = subparsers.add_parser("team", help="管理项目绑定 + 团队介绍（存 internal.db，keeper 单一配置源）")
    team_parser.add_argument("--db", help=f"SQLite 路径（默认 {Path.home()}/.issue-keeper/internal.db）")
    team_sub = team_parser.add_subparsers(dest="team_cmd", required=True)

    p_import = team_sub.add_parser("import", help="一次性把旧 config.yaml 的 repos 段迁进 db")
    p_import.add_argument("--config", "-c", required=True, help="旧版 config.yaml 路径")

    p_add = team_sub.add_parser("add", help="新增/更新一个项目绑定到 db")
    p_add.add_argument("repo", help="项目名（binding.repo）")
    p_add.add_argument("--agent-label", required=True, help="agent 身份标签")
    p_add.add_argument("--cwd", required=True, help="agent 工作目录")
    p_add.add_argument("--profile", default="claude-code", help="agentproc profile（默认 claude-code）")
    p_add.add_argument("--source", default="internal", help="issue 来源（默认 internal）")
    p_add.add_argument("--github-token", default="",
                       help="source=github_token 时的 PAT，支持 ${VAR} 占位")
    p_add.add_argument("--monitor-prs", action="store_true", help="是否监控 PR")
    p_add.add_argument("--env", action="append", default=[],
                       help="项目级 env 覆盖，格式 KEY=VALUE（可重复；密钥用 ${VAR} 占位）")

    p_remove = team_sub.add_parser("remove", help="从 db 删除一个项目绑定（不删 issue）")
    p_remove.add_argument("repo", help="项目名")

    p_intro = team_sub.add_parser("set-intro", help="设置某 agent 的自我介绍")
    p_intro.add_argument("agent_label", help="agent 身份标签")
    p_intro.add_argument("--intro", required=True, help="介绍正文")

    p_list = team_sub.add_parser("list", help="列出项目绑定 + 团队介绍")

    # onboard：把一个新项目快速注册进协同
    onb_parser = subparsers.add_parser(
        "onboard", help="把一个新项目 onboard 进 issue-keeper 协同（写 db 绑定 + 可选生成介绍 + 可选重载 keeper）")
    onb_parser.add_argument("project_path", help="新项目目录路径（git 仓根）")
    onb_parser.add_argument("--config", "-c", default="config.yaml",
                            help="(已弃用，保留兼容) issue-keeper config.yaml 路径；绑定现在写 db")
    onb_parser.add_argument("--repo", help="项目名（默认取目录名）")
    onb_parser.add_argument("--agent-label", help="agent 身份标签（默认 <repo>-agent）")
    onb_parser.add_argument("--profile", default="claude-code", help="agentproc profile（默认 claude-code）")
    onb_parser.add_argument("--db", help=f"SQLite 路径（默认 {Path.home()}/.issue-keeper/internal.db）")
    onb_parser.add_argument("--gen-intro", action="store_true", help="调 agent 自动生成自我介绍并写入 db")
    onb_parser.add_argument("--reload", action="store_true", help="立即重载 keeper daemon（macOS launchd）；不加重载则等下一轮 live-reload")

    # internal source 管理
    internal_parser = subparsers.add_parser("internal", help="管理 internal source 的 issue")
    internal_sub = internal_parser.add_subparsers(dest="internal_cmd", required=True)

    p_create = internal_sub.add_parser("create", help="提一个新 issue/PR")
    _add_internal_common(p_create)
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--body", default="")
    p_create.add_argument("--label", action="append", default=[], help="标签，可多次指定")

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
    p_list.add_argument("--label", action="append", default=[], help="按标签过滤，可多次指定（交集）")

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
    if args.cmd == "team":
        return _run_team(args)
    if args.cmd == "onboard":
        return _run_onboard(args)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
