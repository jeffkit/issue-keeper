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
import time
from datetime import datetime

from .config import Config, RepoBinding, load_config
from .profile import AgentReply, ProfileEntry, invoke_agent, load_profile
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
    "你的回复会被原样作为评论发到该 issue/PR 上，所以请直接说话——"
    "不要写「回复草稿」「以下是回复」这类元描述，不要解释你要怎么回复，直接给出回复内容。\n"
    "你有 bash 等工具能力（在当前工作目录下运行）。如果需要跨项目沟通，"
    "可以用 bash 调用 issue-keeper 的 CLI 给别的项目提 issue：\n"
    "  python -m issue_keeper internal create <项目名> --title \"标题\" --body \"正文\" --author {agent_label}\n"
    "--author 必须用你自己的身份标签（{agent_label}），这样对方才知道是你去的。\n"
    "跨项目提 issue 是可选的——只在确实需要别的项目协同时才用。"
    "可跨项目提 issue 的目标 <项目名> 见下方「可用项目」列表（标「（你自己）」的是你当前所在项目）。"
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
    """组装给 agent 的系统提示（含跨项目花名册）。"""
    return _AGENT_PREAMBLE.format(agent_label=agent_label) + "\n" + _roster(config, binding.repo)


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


def _post_agent_reply(
    source: IssueSource, binding: RepoBinding, res: Resource,
    reply: AgentReply, bot_marker: str, visible_prefix: str,
) -> None:
    if not reply.text:
        log.warning("[%s %s#%d] agent 返回空回复，跳过发评论", binding.repo, res.kind, res.number)
        return
    body = f"{bot_marker}\n{visible_prefix}\n{reply.text}"
    source.post_comment(binding.repo, res, body)
    log.info("[%s %s#%d] 已发表 agent 评论（%d 字符）", binding.repo, res.kind, res.number, len(reply.text))


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
    timeout = binding.effective_timeout(config.default_timeout_secs)

    # 要扫描的资源类型列表：[(kind, labels)]
    kinds: list[tuple[str, list[str] | None]] = [("issue", binding.labels or None)]
    if binding.monitor_prs:
        kinds.append(("pr", binding.pr_labels or binding.labels or None))

    for kind, labels in kinds:
        try:
            resources = src.list_open(binding.repo, [kind], labels)
        except Exception as e:
            log.error("[%s] 列出 %s 失败: %s", binding.repo, kind, e)
            continue

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
            _post_agent_reply(src, binding, res, reply, config.bot_marker, visible_prefix)
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
        _post_agent_reply(src, binding, res, reply, config.bot_marker, visible_prefix)
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
    save_state(config.state_path, state)
    return total


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
