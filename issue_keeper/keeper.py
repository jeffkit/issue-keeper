"""核心监控逻辑：扫描仓库 -> 发现新 issue/评论 -> 调 agent -> 发评论。"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from . import github as gh
from .config import Config, RepoBinding
from .profile import AgentReply, ProfileEntry, invoke_agent, load_profile
from .state import State, load_state, save_state

log = logging.getLogger("issue-keeper")


def _compose_issue_message(binding: RepoBinding, issue: gh.Issue) -> str:
    """新 issue 时发给 agent 的消息正文。"""
    labels = ", ".join(issue.labels) if issue.labels else "（无）"
    return (
        f"GitHub 仓库 {binding.repo} 收到新 issue #{issue.number}：\n"
        f"标题：{issue.title}\n"
        f"标签：{labels}\n"
        f"提交人：{issue.author or '未知'}\n"
        f"创建时间：{issue.created_at}\n"
        f"链接：https://github.com/{binding.repo}/issues/{issue.number}\n\n"
        f"issue 正文：\n{issue.body or '（空）'}\n\n"
        f"请分析并处理该 issue，给出你的回复（将以评论形式发到 issue 上）。"
    )


def _compose_comment_message(
    binding: RepoBinding, issue: gh.Issue, comment: gh.Comment
) -> str:
    """issue 有新评论时发给 agent 的消息正文。"""
    return (
        f"GitHub 仓库 {binding.repo} 的 issue #{issue.number} 有新评论：\n"
        f"标题：{issue.title}\n"
        f"评论人：{comment.author or '未知'}\n"
        f"评论时间：{comment.created_at}\n"
        f"链接：{comment.url or f'https://github.com/{binding.repo}/issues/{issue.number}'}\n\n"
        f"评论内容：\n{comment.body or '（空）'}\n\n"
        f"请基于之前的上下文继续处理，给出你的回复（将以评论形式发到 issue 上）。"
    )


def _is_bot_comment(body: str, bot_marker: str) -> bool:
    return bot_marker in body


def _post_agent_reply(
    binding: RepoBinding, issue_number: int, reply: AgentReply, bot_marker: str
) -> None:
    if not reply.text:
        log.warning("[%s #%d] agent 返回空回复，跳过发评论", binding.repo, issue_number)
        return
    body = f"{bot_marker}\n{reply.text}"
    gh.post_comment(binding.repo, issue_number, body)
    log.info("[%s #%d] 已发表 agent 评论（%d 字符）", binding.repo, issue_number, len(reply.text))


def _ensure_profile(name: str, cache: dict[str, ProfileEntry]) -> ProfileEntry:
    if name not in cache:
        cache[name] = load_profile(name)
    return cache[name]


def process_repo(
    binding: RepoBinding, config: Config, state: State, profile_cache: dict[str, ProfileEntry]
) -> int:
    """处理单个仓库，返回本轮处理的条目数（issue + 评论）。"""
    handled = 0
    rs = state.repo(binding.repo_slug)

    try:
        entry = _ensure_profile(binding.profile, profile_cache)
    except Exception as e:
        log.error("[%s] 加载 profile '%s' 失败，跳过该仓库: %s", binding.repo, binding.profile, e)
        return 0

    try:
        issues = gh.list_open_issues(binding.repo, binding.labels or None)
    except Exception as e:
        log.error("[%s] 列出 issue 失败: %s", binding.repo, e)
        return 0

    me = gh.whoami()
    timeout = binding.effective_timeout(config.default_timeout_secs)

    for issue in issues:
        is_ = rs.issue(issue.number)

        # 1) 新 issue：首次处理 issue 本体
        if not is_.processed:
            message = _compose_issue_message(binding, issue)
            log.info("[%s #%d] 新 issue，调用 agent (profile=%s)", binding.repo, issue.number, binding.profile)
            try:
                reply = invoke_agent(
                    entry, message, is_.session_id or "",
                    from_user=config.agent_from_user,
                    default_timeout=timeout,
                )
            except Exception as e:
                log.error("[%s #%d] agent 调用失败: %s", binding.repo, issue.number, e)
                continue
            if reply.session_id:
                is_.session_id = reply.session_id
            _post_agent_reply(binding, issue.number, reply, config.bot_marker)
            is_.processed = True
            handled += 1

        # 2) 处理新评论
        try:
            comments = gh.list_comments(binding.repo, issue.number)
        except Exception as e:
            log.error("[%s #%d] 读取评论失败: %s", binding.repo, issue.number, e)
            continue

        for c in comments:
            if c.id in is_.processed_comment_ids:
                continue
            # 忽略 bot 自己发的评论（避免死循环）
            if _is_bot_comment(c.body, config.bot_marker):
                is_.processed_comment_ids.add(c.id)
                continue
            # 忽略当前 gh 登录账号发的评论（双重保险）
            if me and c.author and c.author.lower() == me.lower():
                is_.processed_comment_ids.add(c.id)
                continue

            message = _compose_comment_message(binding, issue, c)
            log.info(
                "[%s #%d] 新评论 id=%d (by %s)，调用 agent (session=%s)",
                binding.repo, issue.number, c.id, c.author, is_.session_id,
            )
            try:
                reply = invoke_agent(
                    entry, message, is_.session_id or "",
                    from_user=config.agent_from_user,
                    default_timeout=timeout,
                )
            except Exception as e:
                log.error("[%s #%d] agent 处理评论 %d 失败: %s", binding.repo, issue.number, c.id, e)
                # 不标记为已处理，下轮重试
                break
            if reply.session_id:
                is_.session_id = reply.session_id
            _post_agent_reply(binding, issue.number, reply, config.bot_marker)
            is_.processed_comment_ids.add(c.id)
            handled += 1

    return handled


def run_once(config: Config) -> int:
    """执行一轮全量扫描。返回处理条目总数。"""
    state = load_state(config.state_path)
    profile_cache: dict[str, ProfileEntry] = {}
    total = 0
    for binding in config.repos:
        log.info("扫描仓库 %s (profile=%s)", binding.repo, binding.profile)
        total += process_repo(binding, config, state, profile_cache)
    save_state(config.state_path, state)
    return total


def run_daemon(config: Config) -> None:
    """常驻轮询。"""
    log.info(
        "issue-keeper daemon 启动，监控 %d 个仓库，轮询间隔 %ds",
        len(config.repos), config.poll_interval_secs,
    )
    while True:
        start = datetime.now()
        try:
            handled = run_once(config)
            log.info("本轮完成，处理 %d 条，耗时 %.1fs", handled, (datetime.now() - start).total_seconds())
        except Exception as e:
            log.exception("本轮扫描异常: %s", e)
        time.sleep(config.poll_interval_secs)
