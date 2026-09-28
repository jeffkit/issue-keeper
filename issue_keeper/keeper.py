"""核心监控逻辑：扫描仓库 -> 安全过滤 -> 调 agent -> 发评论。

主循环对 issue 和 PR 一视同仁（都是 Resource）。每条投递给 agent 的消息
（新资源本体 / 新评论）都先过 screener，判定不安全则按 on_unsafe 策略处理。

防循环（三层保险，任一命中即跳过）：
  1. 隐藏 marker   <!-- issue-keeper-bot -->      机器识别，必带
  2. 可见前缀      [issue-keeper:<agent_label>]   人眼识别 + 备份识别
  3. self_identity  当前 source 的 GitHub 账号     账号级兜底

资源层（issue/PR 本体）也识别 marker：AI 自己提的 issue（body 含 marker）
不触发首次 agent 回复，但评论层照常——避免 AI 自己给自己写日记。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path

from .config import Config, RepoBinding, load_config
from .profile import AgentReply, ProfileEntry, invoke_agent, load_profile
from .reply import polish
from .screener import ScreenerConfig, screen as screen_text
from .sources import IssueSource, Resource, make_source
from .state import State, load_state, save_state

log = logging.getLogger("issue-keeper")

_UNSAFE_COMMENT_BODY = (
    "⚠️ 这条内容触发了 issue-keeper 的安全过滤（疑似指令注入或越权诱导），"
    "已跳过自动处理。维护者可人工查看。"
)

# 给 agent 的系统提示，告诉它角色和能力。不提 stdout / 协议细节。
_AGENT_PREAMBLE = (
    "你是 issue-keeper 调用的 agent，负责处理下面的 issue/PR。"
    "你的回复将作为评论发布到该 issue/PR 上（发布前会做隐私消毒），所以请直接给出"
    "可发布的最终内容——不要写「回复草稿」「以下是回复」这类元描述，不要叙述你的"
    "工作过程（不要「我先…然后…现在我将…」），不要出现本机绝对路径或任何密钥，"
    "直接给结论、事实与下一步。\n"
    "你有 bash 等工具能力（在当前工作目录下运行）。如果需要跨项目沟通，"
    "可以用 bash 调用 issue-keeper 的 CLI 给别的项目提 issue：\n"
    "  python -m issue_keeper internal create <项目名> --title \"标题\" --body \"正文\" --author {agent_label}\n"
    "--author 必须用你自己的身份标签（{agent_label}），这样对方才知道是你去的。\n"
    "跨项目提 issue 是可选的——只在确实需要别的项目协同时才用。"
    "可跨项目提 issue 的目标 <项目名> 见下方「可用项目」列表（标「（你自己）」的是你当前所在项目）。"
)

# keeper 角色专属提示词：管理向，不聚焦改代码，而是帮人类管 issue、代理人类跨项目提问、
# 回弹给人类时优先解答、必要时用 HitL（hitl MCP）联系人类拿反馈。
# keeper 由 daemon 驱动，会响应 issue 变更事件——这点区别于「只在对话时工作」的交互式 agent。
_KEEPER_PREAMBLE = (
    "你是 issue-keeper 的「keeper」——本协同系统的管理向 agent（不是改本仓代码的代码 agent）。"
    "你的回复将作为评论发布到对应 issue 上（发布前会做隐私消毒），请直接给出可发布的"
    "最终内容，不要写「回复草稿」「以下是回复」这类元描述，不要叙述工作过程，"
    "不要出现本机绝对路径或任何密钥。\n"
    "你有 bash 工具能力（工作目录是 issue-keeper 仓）。你的核心职责：\n"
    "1. 帮人类管理这些 issue：分类、规划、驱动状态流转（用 `python -m issue_keeper internal move ...`），"
    "在 issue-keeper 项目里执行管理类诉求（如「把某项目加入协同」「更新某 agent 介绍」→ 调 `onboard`/`team` CLI）。\n"
    "2. 代理人类跨项目提问：当人类想就某事问某个项目团队，由你判断目标项目并以你自己的身份"
    "（--author {agent_label}）用 `python -m issue_keeper internal create <项目名> --title ... --body ...` 代为提 issue。"
    "可用项目见下方「可用项目」列表。\n"
    "3. 当 issue 回弹给人类（如停在 review 等人接手、或问题本身需要人决策）时，你优先尝试自行解答/分诊，"
    "把能定的先定掉，把真正需要人拍板的精简成清晰问题再交回。\n"
    "4. 必要时通过 HitL 联系人类拿反馈：你环境里有 `hitl` MCP 工具——"
    "`send_and_wait_reply`（发带 [#id] 的确认问题并阻塞等回复，最多等约 1 小时）和 "
    "`send_message_only`（单向通知，不等回复）。遇到关键决策、危险操作、或必须人确认的岔路时，"
    "用 `send_and_wait_reply` 发简短问题给人类；只需告知无需等回复时用 `send_message_only`。"
    "不要为琐碎事打扰人类。\n"
    "你能改 issue-keeper 的代码，但那不是你的主职——只有当某 issue 明确是 issue-keeper 自身的代码缺陷时才动手修。"
)


def _roster(config: Config, current_repo: str) -> str:
    """列出所有项目及其 agent 身份，供当前 agent 跨项目提 issue 时参考。"""
    lines = []
    for b in config.repos:
        label = b.agent_label or config.agent_from_user or "issue-keeper"
        tag = "（你自己）" if b.repo == current_repo else ""
        lines.append(f"  - {b.repo}（agent：{label}）{tag}")
    return "可用项目：\n" + "\n".join(lines)


def _preamble(binding: RepoBinding, config: Config, agent_label: str) -> str:
    """组装给 agent 的系统提示（含跨项目花名册）。

    role=keeper 用管理向提示词（帮人类管 issue / 代理提问 / 优先解答 / HitL），
    其余用普通代码 agent 提示词。
    """
    base = _KEEPER_PREAMBLE if binding.role == "keeper" else _AGENT_PREAMBLE
    return base.format(agent_label=agent_label) + "\n" + _roster(config, binding.repo)


def _agent_label(binding: RepoBinding, config: Config) -> str:
    """决定本仓库 agent 的可见身份标签。"""
    return binding.agent_label or config.agent_from_user or "issue-keeper"


def _visible_prefix(binding: RepoBinding, config: Config) -> str:
    """可见前缀，出现在 agent 发出的每条评论正文开头。"""
    return f"[issue-keeper:{_agent_label(binding, config)}]"


def _compose_new_message(
    binding: RepoBinding, res: Resource, src: IssueSource, agent_label: str, config: Config
) -> str:
    """新 issue/PR 时发给 agent 的消息正文。"""
    labels = ", ".join(res.labels) if res.labels else "（无）"
    url = src.web_url(binding.repo, res)
    url_line = f"链接：{url}\n" if url else ""
    return (
        f"{_preamble(binding, config, agent_label)}\n\n"
        f"---\n\n"
        f"项目 {binding.repo} 收到新 {res.noun} #{res.number}：\n"
        f"标题：{res.title}\n"
        f"标签：{labels}\n"
        f"提交人：{res.author or '未知'}\n"
        f"创建时间：{res.created_at}\n"
        f"{url_line}\n"
        f"{res.noun} 正文：\n{res.body or '（空）'}\n\n"
        f"请分析并处理这个 {res.noun}。"
    )


def _compose_comment_message(
    binding: RepoBinding, res: Resource, comment, src: IssueSource, agent_label: str,
    config: Config,
) -> str:
    """issue/PR 有新评论时发给 agent 的消息正文。"""
    url = comment.url or src.web_url(binding.repo, res)
    url_line = f"链接：{url}\n" if url else ""
    return (
        f"{_preamble(binding, config, agent_label)}\n\n"
        f"---\n\n"
        f"项目 {binding.repo} 的 {res.noun} #{res.number} 有新评论：\n"
        f"标题：{res.title}\n"
        f"评论人：{comment.author or '未知'}\n"
        f"评论时间：{comment.created_at}\n"
        f"{url_line}\n"
        f"评论内容：\n{comment.body or '（空）'}\n\n"
        f"请基于之前的上下文继续处理。"
    )


def _is_bot_output(body: str, bot_marker: str, visible_prefix: str) -> bool:
    """判断一段文本是否为 issue-keeper 自己产出。

    三层保险任一命中即认为是 bot 自己发的：
      1. 隐藏 marker
      2. 可见前缀
    （第三层 self_identity 在调用方按 author 比对）
    """
    if bot_marker and bot_marker in body:
        return True
    if visible_prefix and visible_prefix in body:
        return True
    return False


def _publish_reply(
    source: IssueSource, binding: RepoBinding, res: Resource,
    raw_text: str, config: Config, visible_prefix: str, *,
    source_label: str = "",
) -> None:
    """发布一条 agent 回复：礼仪化（消毒 + LLM 改写）后套防循环头发评论。

    agent 的原始输出可能带工作过程叙述 / 本机路径 / 重复段落，不适合直接公开
    （见 reply.py）——发布前统一过 reply.polish。改写是尽力而为：任何失败都
    降级为消毒后的原文，除空回复外总会发出。
    """
    label = source_label or f"{binding.repo} {res.kind}#{res.number}"
    if not raw_text or not raw_text.strip():
        log.warning("[%s] agent 返回空回复，跳过发评论", label)
        return
    text = polish(raw_text, config.reply_polish, source_label=label)
    body = f"{config.bot_marker}\n{visible_prefix}\n{text}"
    source.post_comment(binding.repo, res, body)
    log.info("[%s] 已发表 agent 评论（礼仪化 %d → %d 字符）", label, len(raw_text), len(text))


def _post_unsafe_notice(
    source: IssueSource, binding: RepoBinding, res: Resource,
    bot_marker: str, visible_prefix: str,
) -> None:
    body = f"{bot_marker}\n{visible_prefix}\n{_UNSAFE_COMMENT_BODY}"
    source.post_comment(binding.repo, res, body)
    log.info("[%s %s#%d] 已发表安全过滤提示评论", binding.repo, res.kind, res.number)


def _ensure_profile(binding: RepoBinding, cache: dict[str, ProfileEntry]) -> ProfileEntry:
    key = (binding.profile, binding.cwd or "")
    if key not in cache:
        cache[key] = load_profile(binding)
    return cache[key]


def _screen_or_block(
    message: str, cfg: ScreenerConfig, source_label: str
) -> bool:
    """返回 True 表示通过安全过滤，可以投递给 agent。"""
    verdict = screen_text(message, cfg, source_label=source_label)
    if verdict.safe:
        log.debug("[%s] screener 通过: %s", source_label, verdict.reason)
        return True
    log.warning(
        "[%s] screener 拦截: reason=%s raw=%r",
        source_label, verdict.reason, verdict.raw[:200],
    )
    return False


def _extract_issue_refs(text: str) -> list[int]:
    """正文里的 #N 引用（同仓裸数字）。去重保序，供依赖排序/唤醒监视用。"""
    import re
    seen: list[int] = []
    for m in re.finditer(r"#(\d+)", text or ""):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def _dependency_first_order(resources: list, rs) -> list:
    """未处理 issue 按依赖拓扑排序——被依赖者先行（2026-09-28 批次编排地板）。

    图只建在本批未处理 issue 之间：正文 #N 引用命中批内其他未处理编号 → 被引用
    者先跑，串行管线下依赖者的 triage/deps 预检就能看到已合并的依赖成果。
    已处理的原序靠前（评论快扫不被长管线 run 压后）。环按原序放行，依赖正确性
    交给 deps 预检的 blocked 兜底。
    """
    processed_first, pending = [], []
    for r in resources:
        (processed_first if rs.item(r.resource_key).processed else pending).append(r)
    if len(pending) <= 1:
        return processed_first + pending

    index = {r.number: i for i, r in enumerate(pending)}
    deps_of: dict[int, list[int]] = {}
    for r in pending:
        deps_of[r.number] = [d for d in _extract_issue_refs(r.body) if d in index and d != r.number]

    ordered: list = []
    done: set[int] = set()
    remaining = [r.number for r in pending]
    while remaining:
        ready = [n for n in remaining if all(d in done for d in deps_of.get(n, []))]
        if not ready:  # 环：按原序放行
            ordered.extend(r for r in pending if r.number in set(remaining))
            break
        for n in ready:
            ordered.append(pending[index[n]])
            done.add(n)
        remaining = [n for n in remaining if n not in done]
    return processed_first + ordered


def _wake_resolved_dependencies(src, binding, rs) -> None:
    """依赖唤醒（2026-09-28）：blocked issue 的依赖全部闭合 → 清 processed 重跑。

    闭合判定（每仓每轮一次）：依赖编号已不在 open 列表，或修复 commit 已进
    origin/main（git log --grep #N——管线 deliver 直推 main 不会关 issue，
    commit message 按 #17 惯例引用编号）。任何检查失败都不唤醒（保守）。
    """
    watched = [(int(k), it) for k, it in rs.items.items()
               if it.wakeup_deps and it.processed and k.isdigit()]
    if not watched:
        return

    open_numbers: set[int] | None = None
    try:
        kinds = ["issue"] + (["pr"] if binding.monitor_prs else [])
        open_numbers = {r.number for r in src.list_open(binding.repo, kinds)}
    except Exception as e:
        log.warning("[wake] [%s] 列 open 资源失败，本轮只按 commit 判定: %s", binding.repo, e)

    merged: set[int] = set()
    import subprocess
    from pathlib import Path
    cwd = Path(binding.cwd).expanduser()
    if cwd.is_dir() and (cwd / ".git").exists():
        try:
            all_deps = sorted({d for _, it in watched for d in it.wakeup_deps})
            subprocess.run(["git", "-C", str(cwd), "fetch", "origin", "main", "--quiet"],
                           timeout=120, check=False)
            for n in all_deps:
                r = subprocess.run(
                    ["git", "-C", str(cwd), "log", "origin/main", "--grep", f"#{n}",
                     "--oneline", "-1"],
                    capture_output=True, text=True, timeout=30)
                if r.returncode == 0 and r.stdout.strip():
                    merged.add(n)
        except Exception as e:
            log.warning("[wake] [%s] git 检查失败: %s", binding.repo, e)

    for num, it in watched:
        resolved = all(
            (d in merged) or (open_numbers is not None and d not in open_numbers)
            for d in it.wakeup_deps
        )
        if resolved:
            log.info("[wake] [%s issue#%d] 依赖 %s 已闭合，唤醒重跑", binding.repo, num, it.wakeup_deps)
            it.wakeup_deps = []
            it.processed = False
            it.session_id = None


def process_repo(
    binding: RepoBinding,
    config: Config,
    state: State,
    profile_cache: dict[str, ProfileEntry],
    source_cache: dict[str, IssueSource],
) -> int:
    """处理单个仓库，返回本轮处理的条目数（issue/PR + 评论）。"""
    handled = 0
    rs = state.repo(binding.repo_slug)
    screener = config.screener
    visible_prefix = _visible_prefix(binding, config)

    try:
        entry = _ensure_profile(binding, profile_cache)
    except Exception as e:
        log.error("[%s] 加载 profile '%s' 失败，跳过该仓库: %s", binding.repo, binding.profile, e)
        return 0

    try:
        src = _ensure_source(binding, source_cache)
    except Exception as e:
        log.error("[%s] 实例化 source '%s' 失败，跳过该仓库: %s", binding.repo, binding.source, e)
        return 0

    me = src.self_identity()
    # keeper 可能用 HitL 等人类回复（最长 1 小时），用更大的调用超时；普通 agent 用默认。
    if binding.role == "keeper":
        timeout = max(binding.effective_timeout(config.default_timeout_secs), config.keeper_timeout_secs)
    else:
        timeout = binding.effective_timeout(config.default_timeout_secs)

    # 要扫描的资源类型列表：[(kind, labels)]
    kinds: list[tuple[str, list[str] | None]] = [("issue", binding.labels or None)]
    if binding.monitor_prs:
        kinds.append(("pr", binding.pr_labels or binding.labels or None))

    # 依赖唤醒：上轮 blocked 的 issue 若依赖已闭合，先清 processed——本轮下方
    # 循环会把它当新 issue 重新处理（重新过 screener/管线，triage 再判一次）。
    _wake_resolved_dependencies(src, binding, rs)

    for kind, labels in kinds:
        try:
            resources = src.list_open(binding.repo, [kind], labels)
        except Exception as e:
            log.error("[%s] 列出 %s 失败: %s", binding.repo, kind, e)
            continue

        if kind == "issue":
            resources = _dependency_first_order(resources, rs)

        # 候选清单落 DEBUG：排查「这一轮为什么没处理某条」时，第一件事就是确认
        # 它有没有进候选（2026-09-28 有过一轮逐条静默跳过、事后无法复盘的情况）。
        log.debug("[%s] %s 候选 %d 条，未处理: %s", binding.repo, kind, len(resources),
                  [r.number for r in resources if not rs.item(r.resource_key).processed])

        for res in resources:
            handled += _process_resource(
                src, binding, config, screener, entry, rs, res, me, timeout, visible_prefix
            )

    return handled


def _process_resource(
    src: IssueSource,
    binding: RepoBinding,
    config: Config,
    screener: ScreenerConfig,
    entry: ProfileEntry,
    rs,
    res: Resource,
    me: str,
    timeout: int,
    visible_prefix: str,
) -> int:
    """处理单个 issue/PR，返回本轮处理条目数。"""
    handled = 0
    it = rs.item(res.resource_key)
    kind = res.kind
    label = f"{binding.repo} {kind}#{res.number}"

    # ── review 状态自动 review ─────────────────────────────────────
    # issue 在 review 状态时，判断当前 keeper 是否应自动 review 通过
    if res.status == "review":
        should, actor, atype = _should_auto_review(src, binding, config, res)
        if should:
            log.info("[%s] %s 处于 review，由 %s 自动 review 通过", label, kind, actor)
            _safe_move(src, binding, res, "done", actor=actor, actor_type=atype,
                       comment="自动 review 通过")
            handled += 1
        # review 状态下不调 agent 处理新评论——等 review 结果
        return handled

    # ── 1) 新资源本体：首次处理 ────────────────────────────────────
    if not it.processed and not it.blocked:
        # 三层防循环之资源层：AI 自己提的 issue（body 含 marker / 可见前缀）
        # 不触发首次 agent 回复，但评论层照常处理
        if _is_bot_output(res.body or "", config.bot_marker, visible_prefix):
            log.info("[%s] %s 由 issue-keeper 自己创建，跳过首次回复", label, kind)
            it.processed = True  # 标记已处理，后续只看评论
        elif me and res.author and res.author.lower() == me.lower():
            # 自己（当前账号）提的 issue 也不自己回自己
            log.info("[%s] %s 由当前账号 %s 创建，跳过首次回复", label, kind, me)
            it.processed = True
        elif not _author_allowed(config, res.author):
            # 作者 allowlist：非名单内作者不触发 agent（防注入骚扰/资源滥用，S7）
            log.info("[%s] %s 作者 %s 不在 allowlist，跳过首次回复", label, kind, res.author)
            it.processed = True
        elif _author_over_limit(config, res.author):
            # 故意不置 processed：日限会随日期重置、豁免名单也可能事后追加，
            # 一旦置了 processed 就只剩「新评论」能唤醒——#19/#23/#41-#44 就是
            # 这样被静默丢掉的（2026-09-28）。这里只推迟，不消费首次响应。
            log.info("[%s] %s 作者 %s 今日触发次数已达上限，本轮跳过（未标记已处理，次日重试）",
                     label, kind, res.author)
        else:
            message = _compose_new_message(binding, res, src, _agent_label(binding, config), config)
            source = f"{label} body"

            if screener.enabled and not _screen_or_block(message, screener, source):
                it.blocked = True
                if screener.on_unsafe == "comment":
                    _post_unsafe_notice(src, binding, res, config.bot_marker, visible_prefix)
                return 0

            # 调 agent 前推到 doing
            _safe_move(src, binding, res, "doing", actor=_agent_label(binding, config),
                       actor_type="agent", comment="开始处理")

            # ── plaita 管线模式：整段 agent 工作交给 issue-pipeline flow ──
            if config.pipeline_mode:
                pres = _invoke_pipeline(config, binding, res, label)
                if pres is not None and pres.get("status") == ALREADY_RUNNING:
                    # 同 issue 已有 run 在跑（典型：launchd KeepAlive 重启 keeper，
                    # 旧 bridge 变成孤儿仍在改同一个 worktree）。不置 processed、
                    # 不回评、不动看板——锁释放后下一轮自然重派，避免两个 run
                    # 抢同一分支/同一次 main 推送。
                    log.info("[%s] 同 issue 已有 pipeline run 在跑，本轮跳过派发", label)
                    return 0
                if pres is None:
                    _safe_move(src, binding, res, "todo", actor=_agent_label(binding, config),
                               actor_type="agent", comment="管线异常，回退")
                    return 0
                if not pres.get("comment_posted", pres.get("posted")):
                    # D1/D2 兜底：管线没发出任何回评 → keeper 补一条（带 bot marker 防循环）
                    # key 双读兼容旧 bridge；文案区分引擎异常与「终态但回评未发出」
                    if pres.get("status") == "engine_error":
                        reason = "管线引擎异常终止（未发出回评）"
                    else:
                        reason = f"管线终态（status={pres.get('status')}），但未确认发出回评"
                    try:
                        src.post_comment(
                            binding.repo, res,
                            f"{config.bot_marker}\n[issue-pipeline] {reason}，请人工查看。")
                    except Exception as e:
                        log.error("[%s] 兜底回评失败: %s", label, e)
                it.processed = True
                handled += 1
                status = pres.get("status", "")
                # 依赖唤醒监视：blocked = 依赖未就绪。记录正文引用的依赖编号，
                # 每轮检查就绪后唤醒重跑（此前 blocked 即永久沉默——#17 教训）。
                if status == "blocked" and res.kind == "issue":
                    deps = [n for n in _extract_issue_refs(res.body) if n != res.number]
                    if deps:
                        rs.item(res.resource_key).wakeup_deps = deps
                        log.info("[%s] blocked，监视依赖 %s 就绪后唤醒", label, deps)
                if status in ("partial", "guarded", "onhold", "abort", "engine_error"):
                    _safe_move(src, binding, res, "todo", actor=_agent_label(binding, config),
                               actor_type="agent", comment=f"pipeline {status}，待人工")
                else:
                    _safe_move(src, binding, res, "review", actor=_agent_label(binding, config),
                               actor_type="agent", comment=f"pipeline {status}，待 review")
                return handled

            log.info("[%s] 新 %s，调用 agent (profile=%s)", label, kind, binding.profile)
            try:
                reply = invoke_agent(
                    entry, message, it.session_id or "",
                    from_user=config.agent_from_user,
                    default_timeout=timeout,
                )
            except Exception as e:
                log.error("[%s] agent 调用失败: %s", label, e)
                # 失败回退到 todo
                _safe_move(src, binding, res, "todo", actor=_agent_label(binding, config),
                           actor_type="agent", comment="agent 调用失败，回退")
                return 0
            if reply.session_id:
                it.session_id = reply.session_id
            _publish_reply(src, binding, res, reply.text or "", config, visible_prefix)
            it.processed = True
            handled += 1

            # 回复完推到 review
            _safe_move(src, binding, res, "review", actor=_agent_label(binding, config),
                       actor_type="agent", comment="处理完成，待 review")

    # 已被安全过滤拉黑：不再处理它的评论
    if it.blocked:
        return handled

    # ── 2) 处理新评论 ──────────────────────────────────────────────
    try:
        comments = src.list_comments(binding.repo, res)
    except Exception as e:
        log.error("[%s] 读取评论失败: %s", label, e)
        return handled

    for c in comments:
        if c.id in it.processed_comment_ids:
            continue
        # 三层防循环之评论层：marker / 可见前缀 / self_identity 任一命中即跳过
        if _is_bot_output(c.body or "", config.bot_marker, visible_prefix):
            it.processed_comment_ids.add(c.id)
            continue
        if me and c.author and c.author.lower() == me.lower():
            it.processed_comment_ids.add(c.id)
            continue

        message = _compose_comment_message(binding, res, c, src, _agent_label(binding, config), config)
        source = f"{label} comment {c.id}"

        if screener.enabled and not _screen_or_block(message, screener, source):
            it.processed_comment_ids.add(c.id)
            if screener.on_unsafe == "comment":
                _post_unsafe_notice(src, binding, res, config.bot_marker, visible_prefix)
            continue

        # 如果 issue 在 done/closed 状态收到新评论，推回 doing 重新处理
        if res.status in ("done", "closed") and _supports_status(src):
            _safe_move(src, binding, res, "doing", actor=c.author,
                       actor_type="human", comment=f"收到新评论，重新打开")

        log.info(
            "[%s] 新评论 id=%s (by %s)，调用 agent (session=%s)",
            label, c.id, c.author, it.session_id,
        )
        try:
            reply = invoke_agent(
                entry, message, it.session_id or "",
                from_user=config.agent_from_user,
                default_timeout=timeout,
            )
        except Exception as e:
            log.error("[%s] agent 处理评论 %s 失败: %s", label, c.id, e)
            break
        if reply.session_id:
            it.session_id = reply.session_id
        _publish_reply(src, binding, res, reply.text or "", config, visible_prefix)
        it.processed_comment_ids.add(c.id)
        handled += 1

        # 评论回复完也推到 review（重新 review）
        if _supports_status(src) and res.status not in ("review",):
            _safe_move(src, binding, res, "review", actor=_agent_label(binding, config),
                       actor_type="agent", comment="评论后重新 review")

    return handled


def _ensure_source(binding: RepoBinding, cache: dict[str, IssueSource]) -> IssueSource:
    """根据 binding.source 实例化/复用 IssueSource。

    cache key 用 (source, agent_label, github_token, internal_db) 组合，
    因为不同 source 的不同 binding 可能有不同凭据/标签，要分别实例化。
    """
    key = (binding.source, binding.agent_label, binding.github_token, binding.internal_db)
    if key not in cache:
        cache[key] = make_source(binding.source, binding=binding)
    return cache[key]


def _supports_status(src: IssueSource) -> bool:
    """source 是否支持看板状态机（有 move_status 方法）。"""
    return hasattr(src, "move_status")


def _safe_move(
    src: IssueSource, binding: RepoBinding, res: Resource, to_status: str,
    *, actor: str = "", actor_type: str = "human", comment: str = "",
) -> bool:
    """安全地改状态。不支持状态机的 source 静默跳过。"""
    if not _supports_status(src):
        return False
    try:
        ok, _from = src.move_status(
            binding.repo, res, to_status,
            actor=actor, actor_type=actor_type, comment=comment,
        )
        if ok:
            log.info("[%s %s#%d] 状态 → %s（by %s）", binding.repo, res.kind, res.number, to_status, actor or "system")
        return ok
    except Exception as e:
        log.warning("[%s %s#%d] move_status 失败: %s", binding.repo, res.kind, res.number, e)
        return False


def _should_auto_review(
    src: IssueSource, binding: RepoBinding, config: Config, res: Resource,
) -> tuple[bool, str, str]:
    """判断 issue 处于 review 状态时，是否应该由当前 keeper 自动 review 通过。

    返回 (should_review, actor, actor_type)：
    - author 是 agent 且 == 当前 agent_label（agent 提的，自己 review）→ True
    - author 是 human + 配了 review_agent + 当前 agent_label == review_agent → True
    - 否则 False（等人或别的 agent）
    """
    if not _supports_status(src):
        return False, "", "human"
    if res.status != "review":
        return False, "", "human"
    agent_label = _agent_label(binding, config)
    # agent 提的 issue：author == agent_label 时自己 review
    if res.actor_type == "agent" and res.author and res.author.lower() == agent_label.lower():
        return True, agent_label, "agent"
    # 人提的 issue：配了 review_agent 且当前 agent 就是 review_agent
    review_agent = binding.effective_review_agent(config.default_review_agent)
    if res.actor_type == "human" and review_agent and review_agent.lower() == agent_label.lower():
        return True, agent_label, "agent"
    return False, "", "human"


def run_once(config: Config) -> int:
    """执行一轮全量扫描。返回处理条目总数。"""
    state = load_state(config.state_path)
    profile_cache: dict[str, ProfileEntry] = {}
    source_cache: dict[str, IssueSource] = {}
    total = 0
    for binding in config.repos:
        kinds = ["issue"] + (["pr"] if binding.monitor_prs else [])
        log.info(
            "扫描仓库 %s (profile=%s, source=%s, agent=%s, kinds=%s)",
            binding.repo, binding.profile, binding.source,
            _agent_label(binding, config), kinds,
        )
        total += process_repo(binding, config, state, profile_cache, source_cache)
    # keeper 巡检：代人类 review / 主动分诊（按 interval_cycles 节流）
    total += keeper_patrol(config, state, profile_cache, source_cache)
    save_state(config.state_path, state)
    return total


def _find_keeper_binding(config: Config) -> RepoBinding | None:
    """找到 role=keeper 的绑定（ keeper 巡检用它跑 agent）。没有返回 None。"""
    for b in config.repos:
        if b.role == "keeper":
            return b
    return None


def _patrol_candidates(
    src: IssueSource, config: Config, state: State,
) -> list[tuple[RepoBinding, Resource]]:
    """收集需要 keeper 巡检的候选 issue：人提的、停在 review 或 stale inbox。

    只扫 internal source 的项目（跨项目协同系统都在 internal db 里；github source
    的 review 暂不纳入巡检，后续可加）。排除「上次巡检后无新活动」的 issue（防刷屏）。
    """
    patrol = config.keeper_patrol
    now = datetime.now().timestamp()
    candidates: list[tuple[RepoBinding, Resource]] = []
    for binding in config.repos:
        if binding.source != "internal":
            continue
        try:
            resources = src.list_open(binding.repo, ["issue", "pr"])
        except Exception as e:
            log.warning("[patrol] 列出 %s 失败: %s", binding.repo, e)
            continue
        for res in resources:
            # 只代人类处理人提的 issue（agent 提的由各 agent 自 review）
            if res.actor_type != "human":
                continue
            is_review = res.status == "review"
            is_stale_inbox = (
                res.status == "inbox"
                and patrol.stale_inbox_secs > 0
                and _age_secs(res.updated_at) >= patrol.stale_inbox_secs
            )
            if not (is_review or is_stale_inbox):
                continue
            key = state.patrol_key(binding.repo_slug, res.kind, res.number)
            if state.patrol_snapshot(key) == res.updated_at:
                # 上次巡检后无新活动，跳过（防刷屏）
                continue
            candidates.append((binding, res))
    # review 优先于 stale inbox；按 updated_at 升序（最老的先处理）
    def _rank(item: tuple[RepoBinding, Resource]) -> tuple[int, str]:
        _, r = item
        order = 0 if r.status == "review" else 1
        return (order, r.updated_at)
    candidates.sort(key=_rank)
    return candidates[: patrol.max_per_cycle]


def _age_secs(updated_at: str) -> float:
    """updated_at（ISO）距今秒数；解析失败返回很大值（视为 stale）。"""
    try:
        from datetime import datetime as _dt
        # updated_at 形如 2026-07-14T11:08:00+00:00
        dt = _dt.fromisoformat(updated_at)
        return datetime.now().timestamp() - dt.timestamp()
    except Exception:
        return 1e12


def _compose_patrol_message(
    keeper_binding: RepoBinding, config: Config, target_binding: RepoBinding,
    res: Resource, comments: list, keeper_label: str,
) -> str:
    """组装巡检消息：keeper 提示词 + 候选 issue + 代人类 review 指令。"""
    base = _preamble(keeper_binding, config, keeper_label)
    cmt_block = ""
    if comments:
        lines = []
        for c in comments[-6:]:
            lines.append(f"— {c.author}（{c.created_at}）：\n{c.body}")
        cmt_block = "\n\n最近评论：\n" + "\n\n".join(lines)
    return (
        f"{base}\n\n"
        f"---\n\n"
        f"【巡检任务·代人类 review】\n"
        f"项目 {target_binding.repo} 的 {res.noun} #{res.number} 现在停在「{res.status}」状态，"
        f"是人类（{config.human_label}）没及时查看/回复的。你代人类前置处理一道。\n\n"
        f"标题：{res.title}\n"
        f"提交人：{res.author} / 状态：{res.status} / 更新：{res.updated_at}\n\n"
        f"{res.noun} 正文：\n{res.body or '（空）'}{cmt_block}\n\n"
        f"你可以用 bash 调 issue-keeper CLI 在该 issue 上动作（--author 用你自己的 {keeper_label}）：\n"
        f"  python -m issue_keeper internal move {target_binding.repo} {res.number} "
        f"--status done --kind {res.kind} --author {keeper_label} --comment \"代人类 review 通过\"\n"
        f"  python -m issue_keeper internal comment {target_binding.repo} {res.number} "
        f"--kind {res.kind} --author {keeper_label} --body \"...\"\n"
        f"处理原则：\n"
        f"1. 读 agent 的回复/讨论，若显然没问题→move 到 done 自动通过。\n"
        f"2. 若确实需要人拍板→用 hitl 的 send_and_wait_reply 给人类发**一个**聚焦问题"
        f"（最多等约 1 小时），拿到回复后再 move/comment。一条 issue 最多发一次 HitL，别刷屏。\n"
        f"3. 也可先 comment 补一条分诊/澄清再决定。\n"
        f"你的最终回复会作为评论发到该 issue（用你的 keeper 身份，发布前做隐私消毒），"
        f"直接给动作结论，不要写草稿、不要叙述过程。"
    )


def keeper_patrol(
    config: Config, state: State,
    profile_cache: dict[str, ProfileEntry],
    source_cache: dict[str, IssueSource],
) -> int:
    """keeper 巡检一轮：代人类 review / 主动分诊等人处理的 issue。返回本轮处理的条目数。

    - 只在有 role=keeper 绑定且 patrol.enabled 时跑
    - 按 patrol.interval_cycles 节流（state.patrol_cycle 计数）
    - 每条 issue 无新活动不重复巡检（防刷屏）
    """
    patrol = config.keeper_patrol
    state.patrol_cycle += 1
    if not patrol.enabled:
        return 0
    if state.patrol_cycle % patrol.interval_cycles != 0:
        return 0
    keeper_binding = _find_keeper_binding(config)
    if keeper_binding is None:
        return 0  # 没有 keeper，不巡检

    try:
        entry = _ensure_profile(keeper_binding, profile_cache)
    except Exception as e:
        log.error("[patrol] 加载 keeper profile '%s' 失败: %s", keeper_binding.profile, e)
        return 0
    # 用 keeper 自己的 source 读写各 internal 项目（keeper 是 internal source，同库可查任意项目）
    try:
        keeper_src = _ensure_source(keeper_binding, source_cache)
    except Exception as e:
        log.error("[patrol] 实例化 keeper source 失败: %s", e)
        return 0
    if not _supports_status(keeper_src):
        return 0

    keeper_label = _agent_label(keeper_binding, config)
    visible_prefix = _visible_prefix(keeper_binding, config)
    candidates = _patrol_candidates(keeper_src, config, state)
    if not candidates:
        return 0
    log.info("[patrol] 本轮巡检 %d 条候选 issue", len(candidates))

    handled = 0
    for target_binding, res in candidates:
        key = state.patrol_key(target_binding.repo_slug, res.kind, res.number)
        label = f"{target_binding.repo} {res.kind}#{res.number}"
        try:
            comments = keeper_src.list_comments(target_binding.repo, res)
        except Exception as e:
            log.warning("[patrol] [%s] 读评论失败: %s", label, e)
            comments = []

        message = _compose_patrol_message(
            keeper_binding, config, target_binding, res, comments, keeper_label,
        )
        source = f"[patrol] {label}"
        if config.screener.enabled and not _screen_or_block(message, config.screener, source):
            log.warning("[patrol] [%s] 被安全过滤跳过", label)
            # 仍推进快照，避免下轮反复筛
            state.mark_patrolled(key, res.updated_at, None)
            continue

        prev_session = (state.patrol.get(key) or {}).get("session_id") or ""
        log.info("[patrol] [%s] 代人类 review，调用 keeper (session=%s)", label, prev_session)
        keeper_timeout = max(
            keeper_binding.effective_timeout(config.default_timeout_secs),
            config.keeper_timeout_secs,
        )
        try:
            reply = invoke_agent(
                entry, message, prev_session,
                from_user=config.agent_from_user,
                default_timeout=keeper_timeout,
            )
        except Exception as e:
            log.error("[patrol] [%s] keeper 调用失败: %s", label, e)
            state.mark_patrolled(key, res.updated_at, prev_session or None)
            continue

        # keeper 的回复作为评论发到该 issue（带 marker，各项目自己的 agent 会跳过，不互相触发）
        try:
            _publish_reply(keeper_src, target_binding, res, reply.text or "", config,
                           visible_prefix, source_label=f"[patrol] {label}")
        except Exception as e:
            log.warning("[patrol] [%s] 发评论失败: %s", label, e)
        # 推进快照到「发评论后」的 updated_at，避免 keeper 自己的评论触发自己下轮重巡
        fresh = keeper_src.get_issue(target_binding.repo, res.kind, res.number)
        state.mark_patrolled(
            key, (fresh.updated_at if fresh else res.updated_at),
            reply.session_id or prev_session or None,
        )
        handled += 1
    return handled


def run_daemon(config_path: str) -> None:
    """常驻轮询。每轮重新 load_config，使 db 里项目绑定/全局旋钮的改动即时生效。"""
    try:
        config = load_config(config_path)
    except Exception as e:
        log.error("启动加载配置失败，退出: %s", e)
        return
    log.info(
        "issue-keeper daemon 启动，监控 %d 个仓库，轮询间隔 %ds，screener=%s",
        len(config.repos), config.poll_interval_secs,
        "enabled" if config.screener.enabled else "DISABLED (fail-open)",
    )
    while True:
        start = datetime.now()
        try:
            config = load_config(config_path)  # live-reload：db 为单一配置源
            handled = run_once(config)
            log.info("本轮完成，处理 %d 条，耗时 %.1fs", handled, (datetime.now() - start).total_seconds())
        except Exception as e:
            log.exception("本轮扫描异常: %s", e)
        time.sleep(config.poll_interval_secs)


def reopen_issues(config: Config, repo: str, numbers: list[int]) -> list[int]:
    """把已消费的 issue 重新放回队列（清 processed/blocked），返回实际改动的编号。

    存在的理由：keeper 此前没有任何手段让一条 processed 的 issue 再进队——
    日限误跳过、引擎异常终态、孤儿 run 之后，只能靠绕过 keeper 的临时脚本重派
    （2026-09-28 的 /tmp/run_batch_41_44.py 就是这么来的，代价是丢掉 keeper 的
    兜底回评与看板联动）。只清 processed/blocked/wakeup_deps：评论级进度
    （processed_comment_ids）保留，免得把已经答过的旧评论再答一遍。
    """
    binding = next((b for b in config.repos if b.repo == repo), None)
    if binding is None:
        raise ValueError(f"配置里没有 repo 绑定: {repo}")
    state = load_state(config.state_path)
    rs = state.repo(binding.repo_slug)
    changed: list[int] = []
    for n in numbers:
        it = rs.item(str(n))
        if it.processed or it.blocked:
            it.processed = False
            it.blocked = False
            it.wakeup_deps = []
            changed.append(n)
    if changed:
        save_state(config.state_path, state)
    return changed


def _author_allowed(config, author: str | None) -> bool:
    """作者 allowlist：空名单=全放行；大小写不敏感。"""
    if not config.author_allowlist:
        return True
    if not author:
        return False
    allowed = {a.lower() for a in config.author_allowlist}
    return author.lower() in allowed


def _author_over_limit(config, author: str | None) -> bool:
    """同作者每日触发次数限制（读 pipeline runs.jsonl 台账；台账缺失视为未超限）。
    豁免名单（author_daily_limit_exempt）内的作者不受限（大小写不敏感）。"""
    import json
    import time
    from pathlib import Path
    if not author or config.author_daily_limit <= 0:
        return False
    if author.lower() in {a.lower() for a in config.author_daily_limit_exempt}:
        return False
    ledger = Path("~/.issue-keeper/pipeline/runs.jsonl").expanduser()
    if not ledger.exists():
        return False
    today = time.strftime("%Y-%m-%d")
    n = 0
    try:
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if (rec.get("author") or "").lower() == author.lower() \
                    and str(rec.get("ts", "")).startswith(today):
                n += 1
    except Exception:
        return False
    return n >= config.author_daily_limit


# ── pipeline 派发互斥（2026-09-28）────────────────────────────────────
# bridge 用 start_new_session 起独立会话：keeper 被 launchd KeepAlive 重启时，
# 旧 bridge 不会跟着死，会继续改同一个 worktree/分支；新实例看不见它又派一份，
# 于是同一个 issue 两个 run 并行（#45 实证：02-plan.md 里留下两份「实施记录」，
# 且残留的 run 在 push_mode=main 下仍可能往 main 推）。
# 锁文件里放 bridge 的 pid：pid 活着就跳过本轮派发；pid 死了视为陈旧锁清掉。
#
# 两级锁：
#   run.lock                        每 issue 一把（同 issue 不并发）
#   .pipeline.lock（pipeline 根目录）全局单槽位（跨进程全局并发 1）
# 全局槽位是 README「部署强制项 #2：全局并发 1-2」的落地——keeper 自身是串行的，
# 但第二个实例/`--once` 补跑脚本会绕过它：2026-09-28 实证两个进程各派一个 run，
# 抢同一个 main clone 做 ff 合并与 push，还互相抢 CPU 把 agent 顶到超时。
PIPELINE_LOCK_NAME = "run.lock"
GLOBAL_LOCK_NAME = ".pipeline.lock"
ALREADY_RUNNING = "already_running"


def _read_pipeline_lock(lock: Path) -> int | None:
    try:
        return int(lock.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """pid 是否仍存在；无权限发信号视为存在（不可误清别人的锁）。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_holder(lock: Path) -> int | None:
    """锁文件里的 pid 还活着就返回它；陈旧/损坏的锁顺手清掉并返回 None。"""
    if not lock.exists():
        return None
    holder = _read_pipeline_lock(lock)
    if holder is not None and _pid_alive(holder):
        return holder
    lock.unlink(missing_ok=True)
    return None


def _pipeline_in_flight(artifact_dir: Path) -> int | None:
    """同一 issue 是否已有 run 在跑。"""
    return _lock_holder(artifact_dir / PIPELINE_LOCK_NAME)


def _global_pipeline_in_flight(artifact_dir: Path) -> int | None:
    """是否有任何 run 在跑（跨进程全局并发闸）。"""
    return _lock_holder(artifact_dir.parent / GLOBAL_LOCK_NAME)


def _release_pipeline_lock(lock: Path, pid: int) -> None:
    """只删自己写的那把锁，避免误删后来者的。"""
    if _read_pipeline_lock(lock) == pid:
        lock.unlink(missing_ok=True)


def _invoke_pipeline(config, binding, res, label: str) -> dict | None:
    """以子进程跑 issue-pipeline flow（bridge），返回 RESULT dict；异常返回 None。

    子进程 start_new_session + 超时 killpg：flow 内部还会再起 recursive/claude
    子树，超时必须连整棵树一起清（2026-09-27 孤儿事故）。
    """
    import json
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    bridge = Path(config.pipeline_bridge).expanduser()
    if not bridge.exists():
        log.error("[%s] pipeline bridge 不存在: %s", label, bridge)
        return None

    slug = binding.repo.split("/")[-1]
    artifact_dir = Path(f"~/.issue-keeper/pipeline/{slug}-{res.number}").expanduser()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # 互斥检查必须在写 00-issue.md 之前——在跑的 run 正用着这份产物。
    # 先看全局槽位：别的 issue 在跑也一律不派发（全局并发 1，跨进程）。
    global_holder = _global_pipeline_in_flight(artifact_dir)
    if global_holder is not None:
        log.warning("[%s] 已有另一个 pipeline run 在跑 (pid=%s)，本轮不派发（全局并发 1）",
                    label, global_holder)
        return {"status": ALREADY_RUNNING, "comment_posted": True}

    holder = _pipeline_in_flight(artifact_dir)
    if holder is not None:
        log.warning("[%s] 同 issue 已有 pipeline run 在跑 (pid=%s)，跳过本轮派发", label, holder)
        return {"status": ALREADY_RUNNING, "comment_posted": True}

    body_file = artifact_dir / "00-issue.md"
    body_file.write_text((res.body or "")[:16000], encoding="utf-8")

    payload = {
        "repo_full": binding.repo,
        "issue_number": res.number,
        "title": res.title or "",
        "author": res.author or "",
        "body_file": str(body_file),
        "screener_verdict": "safe",
        "main_clone": binding.cwd,
        "worktree_dir": f"{binding.cwd}/.worktrees/issue-{res.number}",
        "branch_name": f"pipeline/issue-{res.number}",
        "artifact_dir": str(artifact_dir),
        "test_command": config.pipeline_test_commands.get(binding.repo, ""),
        "review_mode": config.pipeline_review_mode,
        "push_mode": config.pipeline_push_mode,
    }
    # 混合形态（2026-09-28）：定义源 console + 执行观测上报。bridge 侧对两者都
    # fail-open——拉不到定义退 stale 缓存/本地文件，Redis 不可达静默跳过上报。
    pc = config.pipeline.console
    if pc.url and pc.api_key:
        payload["console"] = {
            "url": pc.url, "api_key": pc.api_key, "flow_id": pc.flow_id,
            "refresh_secs": pc.refresh_secs, "cache_path": pc.cache_path,
        }
    if config.pipeline.observability_redis:
        payload["observability_redis"] = config.pipeline.observability_redis

    log.info("[%s] 提交 issue-pipeline run (push_mode=%s, review_mode=%s)",
             label, payload["push_mode"], payload["review_mode"])
    t0 = time.time()
    proc = subprocess.Popen(
        [sys.executable, str(bridge)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    # 锁里写 bridge 的 pid 而不是 keeper 自己的：keeper 被重启后 bridge 还活着，
    # 新实例据此就能发现「已经有人在跑」。两把：本 issue 一把 + 全局槽位一把。
    lock = artifact_dir / PIPELINE_LOCK_NAME
    global_lock = artifact_dir.parent / GLOBAL_LOCK_NAME
    for lk in (lock, global_lock):
        try:
            lk.write_text(str(proc.pid), encoding="utf-8")
        except OSError as e:
            log.warning("[%s] 写 pipeline 锁失败（%s，并发保护失效）: %s", label, lk.name, e)
    try:
        out, err = proc.communicate(
            input=json.dumps(payload, ensure_ascii=False),
            timeout=config.pipeline_timeout_secs,
        )
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass
        log.error("[%s] pipeline 超时（%ss），进程组已清", label, config.pipeline_timeout_secs)
        return None
    finally:
        _release_pipeline_lock(lock, proc.pid)
        _release_pipeline_lock(global_lock, proc.pid)

    if proc.returncode != 0:
        log.error("[%s] pipeline bridge 退出码 %s: %s", label, proc.returncode,
                  (err or "")[-400:])
        return None

    status, posted, duration = None, None, round(time.time() - t0, 1)
    for line in (out or "").splitlines():
        if line.startswith("RESULT "):
            try:
                pres = json.loads(line[len("RESULT "):])
                status = pres.get("status")
                posted = pres.get("comment_posted")
            except Exception:
                pass
    if status is None:
        log.error("[%s] pipeline 无 RESULT 输出", label)
        return None
    log.info("[%s] pipeline 完成: status=%s comment_posted=%s 耗时=%ss",
             label, status, posted, duration)
    return {"status": status, "comment_posted": posted}
