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

import copy
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .config import Config, PipelineRepoConfig, RepoBinding, load_config
from .profile import (
    AgentReply, ProfileEntry, _build_command, _parse_reply, invoke_agent, load_profile,
)
from .reply import polish
from .screener import ScreenerConfig, Verdict, screen as screen_text
from .sources import IssueSource, Resource, make_source
from .state import ItemState, State, drop_orphan_comment_tasks, load_state, save_state_item, save_state_merged

log = logging.getLogger("issue-keeper")

_UNSAFE_COMMENT_BODY = (
    "⚠️ 这条内容触发了 issue-keeper 的前置安全过滤，本轮跳过自动处理。"
    "判定详情：{reason}维护者可人工查看；确认误拦可清状态重新入队。"
)

# 「screener 没判出来」的两种公开文案（#5）——措辞刻意不含指控：低置信是猜测、
# 服务故障与内容无关，都不能说成「疑似注入」。
_SCREENER_ERROR_COMMENT_BODY = (
    "⚠️ 安全过滤服务故障，待重试：issue-keeper 的前置安全过滤本轮未能完成判定"
    "（与内容无关）。本轮跳过自动处理，稍后自动重试，连续失败会升级人工。"
    "故障详情：{reason}"
)

_SCREENER_UNCERTAIN_COMMENT_BODY = (
    "⏳ 本条内容的安全判定置信度不足（{confidence}），已转人工确认，本轮跳过自动处理。"
    "维护者确认无误后可清状态重新入队。"
)


def _scrub_reason(reason: str) -> str:
    """判定原因会发到公开 issue 上：压成一行、截断，避免内部长报错刷屏。"""
    text = " ".join(str(reason or "").split())
    return text[:200] + ("…" if len(text) > 200 else "")


def _public_reason(reason: str) -> str:
    """unsafe 通告用的原因：空值兜底为「疑似注入」提示（故障文案不走这里）。"""
    return _scrub_reason(reason) or "疑似指令注入或越权诱导（screener 未给出原因）"


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


def _post_notice(
    source: IssueSource, binding: RepoBinding, res: Resource,
    bot_marker: str, visible_prefix: str, body: str,
) -> None:
    """发一条系统通告：统一拼防循环头（marker + 可见前缀）。

    三种通告（unsafe / 服务故障 / 低置信）共用——缺任一层都会被自己再筛一遍，
    形成「自己回自己的评论」循环（AGENTS.md 禁止项③）。"""
    source.post_comment(binding.repo, res, f"{bot_marker}\n{visible_prefix}\n{body}")


def _post_unsafe_notice(
    source: IssueSource, binding: RepoBinding, res: Resource,
    bot_marker: str, visible_prefix: str, reason: str = "",
) -> None:
    body = _UNSAFE_COMMENT_BODY.format(reason=_public_reason(reason))
    _post_notice(source, binding, res, bot_marker, visible_prefix, body)
    log.info("[%s %s#%d] 已发表安全过滤提示评论（reason=%s）",
             binding.repo, res.kind, res.number, _public_reason(reason))


def _post_screener_error_notice(
    source: IssueSource, binding: RepoBinding, res: Resource,
    bot_marker: str, visible_prefix: str, reason: str = "",
) -> None:
    """服务故障通告：明示「安全过滤服务故障，待重试」，不回显置信度。"""
    body = _SCREENER_ERROR_COMMENT_BODY.format(reason=_scrub_reason(reason) or "未记录")
    _post_notice(source, binding, res, bot_marker, visible_prefix, body)
    log.info("[%s %s#%d] 已发表 screener 服务故障通告（reason=%s）",
             binding.repo, res.kind, res.number, _scrub_reason(reason) or "未记录")


def _post_screener_uncertain_notice(
    source: IssueSource, binding: RepoBinding, res: Resource,
    bot_marker: str, visible_prefix: str, confidence: float = 0.0,
) -> None:
    """低置信通告：只带实测置信度，不带 verdict.reason（可能含「注入风险」字样）。"""
    body = _SCREENER_UNCERTAIN_COMMENT_BODY.format(confidence=f"{confidence:.2f}")
    _post_notice(source, binding, res, bot_marker, visible_prefix, body)
    log.info("[%s %s#%d] 已发表 screener 低置信通告（confidence=%.2f）",
             binding.repo, res.kind, res.number, confidence)


def _ensure_profile(binding: RepoBinding, cache: dict[str, ProfileEntry]) -> ProfileEntry:
    key = (binding.profile, binding.cwd or "")
    if key not in cache:
        cache[key] = load_profile(binding)
    return cache[key]


def _screen_or_block(
    message: str, cfg: ScreenerConfig, source_label: str
) -> Verdict:
    """返回 Verdict；.safe 为 True 表示通过安全过滤，可以投递给 agent。

    注意：`.safe` 不区分「模型判 unsafe」与「低置信/服务故障」——分流看
    `_screener_disposition`。"""
    verdict = screen_text(message, cfg, source_label=source_label)
    if verdict.safe and verdict.low_confidence is None:
        log.debug("[%s] screener 通过: %s", source_label, verdict.reason)
        return verdict
    log.warning(
        "[%s] screener 未通过（error=%s low_confidence=%s）: reason=%s raw=%r",
        source_label, verdict.error, verdict.low_confidence, verdict.reason,
        verdict.raw[:200],
    )
    return verdict


_SCREENER_RETRY_LIMIT = 3  # 未判定的重试上限：到顶升级人工（不写 blocked）


def _screener_disposition(verdict: Verdict, screener: ScreenerConfig) -> str:
    """screener 结果的四分流：error / uncertain / pass / block。

    铁律：低置信只看 `verdict.low_confidence`，不能只看 `safe`——线上定义
    `on_low_confidence=default + default_choice=safe` 会把低置信的 unsafe 换成
    safe 后返回，`safe` 已无法表达「不确定」。"""
    if verdict.error:
        return "error"
    if verdict.low_confidence is not None:
        return "uncertain"
    if verdict.safe:
        return "pass"
    return "block"


def _hold_for_screener(
    src: IssueSource, binding: RepoBinding, config: Config, res: Resource,
    screener: ScreenerConfig, it: ItemState, verdict: Verdict, *,
    visible_prefix: str, label: str,
) -> None:
    """「没判出来」的公共处置（低置信 / 服务故障）：不拉黑、不消费，退避重试。

    与 blocked 的区别（#5）：这两类不是「内容有问题」，只是「没判出来」——
    写 blocked 会把猜测与故障永久拉黑（10-03/10-04 实证 44 条拦截里 22 条是
    自身故障），因此只推 retry_after 让下轮重试；连击到上限再升级人工。"""
    it.screener_retry_streak = int(getattr(it, "screener_retry_streak", 0) or 0) + 1
    streak = it.screener_retry_streak
    it.retry_after = time.time() + _retry_later_backoff(config, streak)
    disp = _screener_disposition(verdict, screener)
    log.warning("[%s] screener 未判定（%s，第 %d 次，%.0fs 后重试）: %s",
                label, disp, streak, it.retry_after - time.time(), verdict.reason)
    if streak == 1 and screener.on_unsafe == "comment":
        if disp == "error":
            _post_screener_error_notice(src, binding, res, config.bot_marker,
                                        visible_prefix, reason=verdict.reason)
        else:
            _post_screener_uncertain_notice(src, binding, res, config.bot_marker,
                                            visible_prefix,
                                            confidence=verdict.low_confidence or 0.0)
        return
    if streak >= _SCREENER_RETRY_LIMIT:
        why = "服务故障" if disp == "error" else "置信度不足"
        body = (f"⚠️ 安全过滤连续 {streak} 次未能判定本条内容（{why}），"
                f"已升级人工处理（标签 {config.pipeline_needs_human_label}）。"
                f"详情：{_scrub_reason(verdict.reason) or '未记录'}")
        _post_notice(src, binding, res, config.bot_marker, visible_prefix, body)
        _gh_add_label(res.kind, binding.repo, res.number,
                      config.pipeline_needs_human_label)
        log.warning("[%s] screener 连续 %d 次未判定（%s），升级人工", label, streak, disp)


def _author_trusted_by_screener(config, author: str | None) -> bool:
    """内容作者在 screener 可信名单（screener.trusted_authors）时完全跳过安全过滤。

    jeffkit 2026-10-04 拍板：okguitar 完全可信——其 issue 正文/评论不再过判定
    模型（省调用、免「边缘 0.95 误拦」与排队单每轮重复筛的累积拦截）。名单外
    作者行为不变。"""
    if not author:
        return False
    trusted = getattr(config.screener, "trusted_authors", ()) or ()
    wanted = {str(a).strip().lower() for a in trusted if str(a).strip()}
    return author.strip().lower() in wanted


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
                src, binding, config, screener, entry, rs, res, me, timeout, visible_prefix,
                pipeline_in_flight=_count_in_flight(state), state=state,
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
    pipeline_in_flight: int = 0,
    state=None,
) -> int:
    """处理单个 issue/PR，返回本轮处理条目数。"""
    handled = 0
    kind = res.kind
    label = f"{binding.repo} {kind}#{res.number}"

    # ── 0) 显式豁免（2026-09-30）：带 opt-out 标签的资源完全不进处理循环 ──
    # 人工协调线程/公告类 issue 用；先于 allowlist/self_identity/screener 生效，
    # agent 连读都不读。摘掉标签后下一轮按正常流程处理。
    opt_out = {str(x).strip().lower()
               for x in (getattr(config, "opt_out_labels", None) or [])}
    if opt_out and any(str(lb).strip().lower() in opt_out for lb in (res.labels or [])):
        log.info("[%s] %s 带 opt-out 标签，keeper 完全跳过（首响/评论均不处理）", label, kind)
        return handled

    it = rs.item(res.resource_key)

    # ── 管线在途：整体跳过（回评/状态/看板由 reaper 收尾）────────────
    # 2026-09-29 派发解耦：dispatch 只负责把 run 拉起来（后台），完成后的
    # 兜底回评、processed、kanban 全部由每轮开头的 _reap_pipelines 处理。
    if getattr(it, "in_flight_since", None):
        return handled

    # 自动重试的退避闸（#8 failed / #6 retry-later）：reaper 判重试时写了
    # retry_after，到点前不派发。
    ra = getattr(it, "retry_after", None)
    if ra and time.time() < ra:
        log.info("[%s] %s 自动重试退避中（%.0fs 后重派）", label, kind, ra - time.time())
        return handled

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
    # 派发权移交（2026-10-07 keeper→flow）：owner=flow 且本仓有管线契约 → intake
    # （screener + 首次派发）由 keeper-shadow flow 承担；keeper 跳过本段，只保留
    # 下方评论处理与轮首 reaper 等收尾职责。防双筛（两处 screener 会给同单发两份
    # 通告）与双派。非管线仓（无契约/readonly 走 legacy 的）不受影响。
    _handover = False
    if config.pipeline_mode and str(getattr(config, "pipeline_dispatch_owner", "keeper")) == "flow":
        _handover = _pipeline_repo_cfg(config, binding)[0] is not None
    if _handover and not it.processed:
        log.info("[%s] 派发权已移交 flow（pipeline_dispatch_owner=flow）——"
                 "keeper 跳过首次处理（screener/派发由 keeper-shadow 承担）", label)
    if (not it.processed and not it.blocked) and not _handover:
        # 三层防循环之资源层：AI 自己提的 issue（body 含 marker / 可见前缀）
        # 不触发首次 agent 回复，但评论层照常处理
        if _is_bot_output(res.body or "", config.bot_marker, visible_prefix):
            log.info("[%s] %s 由 issue-keeper 自己创建，跳过首次回复", label, kind)
            it.processed = True  # 标记已处理，后续只看评论
        elif not _author_allowed(config, res.author):
            # 作者 allowlist：非名单内作者不触发 agent（防注入骚扰/资源滥用，S7）
            log.info("[%s] %s 作者 %s 不在 allowlist，跳过首次回复", label, kind, res.author)
            it.processed = True
        elif _author_over_limit(config, res.author):
            # 故意不置 processed：日限会随日期重置、豁免名单也可能事后追加，
            # 一旦置了 processed 就只剩「新评论」能唤醒——#19/#23/#41-#44 就是
            # 这样被静默丢掉的（2026-09-28）。这里只推迟，不消费首次响应。
            log.warning("[%s] %s 作者 %s 今日触发次数已达上限，本轮跳过（未标记已处理，次日重试）",
                        label, kind, res.author)
        elif config.pipeline_mode and _issue_over_pipeline_limit(config, binding.repo, res.number):
            # 同 issue 管线日上限（#40 空转事故）：终态 issue 被拉回队列时不再
            # 无限重派整轮 run（半小时起步）。同样只推迟、不消费首次响应。
            log.warning("[%s] 本 issue 今日管线 run 已达上限（%d），本轮跳过（未标记已处理）",
                        label, config.pipeline_issue_daily_limit)
        else:
            message = _compose_new_message(binding, res, src, _agent_label(binding, config), config)
            source = f"{label} body"

            if screener.enabled and _author_trusted_by_screener(config, res.author):
                log.debug("[%s] 作者 %s 在 screener 可信名单，跳过安全过滤", label, res.author)
            elif screener.enabled:
                verdict = _screen_or_block(message, screener, source)
                disp = _screener_disposition(verdict, screener)
                if disp == "pass":
                    it.screener_retry_streak = 0
                elif disp == "block":
                    # 只有「模型判 unsafe」才是内容问题：拉黑 + 指控通告
                    it.blocked = True
                    it.screener_retry_streak = 0
                    if screener.on_unsafe == "comment":
                        _post_unsafe_notice(src, binding, res, config.bot_marker,
                                            visible_prefix, reason=verdict.reason)
                    return 0
                else:
                    # 低置信 / 服务故障：不拉黑、不消费首响，退避重试 + 连击升级
                    _hold_for_screener(src, binding, config, res, screener, it, verdict,
                                       visible_prefix=visible_prefix, label=label)
                    return 0

            # 调 agent 前推到 doing
            _safe_move(src, binding, res, "doing", actor=_agent_label(binding, config),
                       actor_type="agent", comment="开始处理")

            # ── plaita 管线模式：整段 agent 工作交给 issue-pipeline flow ──
            # v0.3 per-repo 门控：无契约/无真门的仓不进管线（防空门假绿 +
            # 硬编码基线在 develop/master 仓上出错），回退 legacy 单 agent 路径。
            _pc, _pc_why = _pipeline_repo_cfg(config, binding) if config.pipeline_mode else (None, "")
            if config.pipeline_mode and _pc is None:
                log.info("[%s] 管线不适用（%s），走 legacy 单 agent 路径", label, _pc_why)
            if config.pipeline_mode and _pc is not None:
                in_flight = max(pipeline_in_flight, 0)
                if in_flight >= max(1, config.pipeline_max_in_flight):
                    # 全局并发上限：不标记 processed，下轮腾出槽位再派。
                    log.info("[%s] 在途管线 run %d/%d，本轮不派发", label,
                             in_flight, config.pipeline_max_in_flight)
                    return 0
                _over, _cur, _cap = _repo_quota_exceeded(config, state, binding)
                if _over:
                    # 按仓配额（S5）：该仓已在途到顶——同样不标记 processed，下轮再试。
                    log.info("[%s] 本仓在途 run %d/%d（按仓配额），本轮不派发", label,
                             _cur, _cap)
                    return 0
                pres = _dispatch_pipeline(config, binding, res, it, label, pc=_pc)
                if pres.get("status") == ALREADY_RUNNING:
                    # 同 issue 已有 run 在跑（run.lock 的 pid 活着）。不置 processed、
                    # 不回评——锁释放后下一轮自然重派。
                    log.info("[%s] 同 issue 已有 pipeline run 在跑，本轮跳过派发", label)
                    return 0
                # 已后台派发：回评由 flow 的 post 节点发；兜底/状态/看板由
                # _reap_pipelines 在 run 结束的下一轮收尾。
                handled += 1
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

    # ── 1.5) 评论异步任务收尸（评论层后台化 2026-10-01）──────────────
    # 在 blocked 检查之前：拉黑单的在途任务也要收尾发布，防 agent 白跑。
    handled += _collect_comment_tasks(src, binding, config, entry, res, it, label,
                                      timeout, visible_prefix)
    # 同 issue 串行：仍有在途评论任务则本轮不派发新评论（下轮收尸后自然接续）
    if it.comment_tasks:
        return handled

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

        if screener.enabled and _author_trusted_by_screener(config, c.author):
            log.debug("[%s] 评论作者 %s 在 screener 可信名单，跳过安全过滤", label, c.author)
        elif screener.enabled:
            verdict = _screen_or_block(message, screener, source)
            disp = _screener_disposition(verdict, screener)
            if disp == "pass":
                it.screener_retry_streak = 0
            elif disp == "block":
                it.processed_comment_ids.add(c.id)
                it.screener_retry_streak = 0
                if screener.on_unsafe == "comment":
                    _post_unsafe_notice(src, binding, res, config.bot_marker,
                                        visible_prefix, reason=verdict.reason)
                continue
            else:
                # 低置信 / 服务故障：不消费评论 id（消费掉就再也不会重判了）
                _hold_for_screener(src, binding, config, res, screener, it, verdict,
                                   visible_prefix=visible_prefix, label=label)
                continue

        # 如果 issue 在 done/closed 状态收到新评论，推回 doing 重新处理
        if res.status in ("done", "closed") and _supports_status(src):
            _safe_move(src, binding, res, "doing", actor=c.author,
                       actor_type="human", comment=f"收到新评论，重新打开")

        # 全局并发上限（跨仓评论 agent；保护 GLM 配额——2026-10-01 配额爆量教训）。
        # #19：账本口径≠容量口径——陈尸记录（进程已死）不计入，闸门不会被
        # 永久占满；记录本身的清除仍由收尸/轮末孤儿清扫负责（不在这儿删）。
        if _count_comment_tasks(state) >= max(1, config.comment_max_in_flight):
            log.info("[%s] 评论 agent 并发已达上限 (%d)，本轮不派发新评论",
                     label, config.comment_max_in_flight)
            return handled

        log.info(
            "[%s] 新评论 id=%s (by %s)，异步派发 agent (session=%s)",
            label, c.id, c.author, it.session_id,
        )
        base = f"{binding.repo_slug}-{res.number}"
        pid = _spawn_comment_agent(entry, message, it, base, str(c.id),
                                   config.agent_from_user, timeout)
        if pid is None:
            log.error("[%s] 评论 agent 派发失败 (comment=%s)，下轮重试", label, c.id)
            return handled
        it.comment_tasks[str(c.id)] = {"pid": pid, "started_at": time.time(),
                                       "attempts": 1}
        handled += 1
        # 收尸时发布回评并推 review（异步化：此处仅派发）

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
    base = copy.deepcopy(state)  # 轮首快照：轮尾 diff 用（判断「本轮真改过」）
    total = _reap_pipelines(config, state, config.repos)
    profile_cache: dict[str, ProfileEntry] = {}
    source_cache: dict[str, IssueSource] = {}
    open_keys: set[str] = set()  # #19：本轮扫到的 open 资源（repo_slug:resource_key）
    list_failed_slugs: set[str] = set()  # #19：列表失败的仓——本轮孤儿清扫整仓跳过
    for binding in config.repos:
        kinds = ["issue"] + (["pr"] if binding.monitor_prs else [])
        log.info(
            "扫描仓库 %s (profile=%s, source=%s, agent=%s, kinds=%s)",
            binding.repo, binding.profile, binding.source,
            _agent_label(binding, config), kinds,
        )
        total += process_repo(binding, config, state, profile_cache, source_cache)
        for kind in kinds:
            try:
                src = _ensure_source(binding, source_cache)
                open_keys.update(f"{binding.repo_slug}:{r.resource_key}"
                                 for r in src.list_open(binding.repo, [kind],
                                                        binding.labels if kind == "issue" else None))
            except Exception as e:
                list_failed_slugs.add(binding.repo_slug)
                log.warning("[%s] 列 open 资源失败（#19 孤儿收尸本轮跳过）: %s",
                            binding.repo, e)
    # #19：收掉 closed 资源上的孤儿评论任务记录（_process_resource 只见 open，
    # closed 资源的 comment_tasks 永远没人收 → 占满全局并发闸）。
    # 列表失败的仓必须排除：「不在 open_keys」≠「已关闭」——这轮压根没列出来，
    # 在途任务（pid 活着、回评还没发）的记录被删就永远没人收尸发布了。
    n_orphan = drop_orphan_comment_tasks(state, open_keys,
                                         skip_slugs=list_failed_slugs)
    if n_orphan:
        log.info("#19 孤儿评论任务：清除 %d 条记录（对应 issue 已关闭）", n_orphan)
    if list_failed_slugs:
        log.info("#19 孤儿收尸本轮跳过仓库: %s", ", ".join(sorted(list_failed_slugs)))
    # keeper 巡检：代人类 review / 主动分诊（按 interval_cycles 节流）
    total += keeper_patrol(config, state, profile_cache, source_cache)
    save_state_merged(config.state_path, state, base)
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
        # 12（2026-09-29 由 6 上调）：#40 的 must-fix 清单一度只比窗口早 1 条——keeper
        # 自己的兜底回评会把关键评论挤出窗口，agent 就看不到上一轮审查结论了。
        for c in comments[-12:]:
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
        if (config.screener.enabled
                and not _author_trusted_by_screener(config, res.author)
                and not _screen_or_block(message, config.screener, source).safe):
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


def _clear_terminal_state(it: ItemState) -> None:
    it.processed = False
    it.blocked = False
    it.wakeup_deps = []
    it.retry_after = None      # #8：人工 reopen 立即重派，不被上轮退避挡住
    it.retry_later_streak = 0  # #6：人工干预即重算连击，退避从 7min 起
    it.screener_retry_streak = 0  # #5：同理，screener 连击从 0 重算
    # #7：记 reopen 时刻——reaper 收尾见它晚于本次派发即跳过 processed 重派。
    # 刻意**不**清 in_flight_since：run 还在跑时 reaper 仍要认这个在途锚。
    it.manual_reopen_at = time.time()


def reopen_issues(config: Config, repo: str, numbers: list[int]) -> list[int]:
    """把已消费的 issue 重新放回队列（清 processed/blocked），返回实际改动的编号。

    存在的理由：keeper 此前没有任何手段让一条 processed 的 issue 再进队——
    日限误跳过、引擎异常终态、孤儿 run 之后，只能靠绕过 keeper 的临时脚本重派
    （2026-09-28 的 /tmp/run_batch_41_44.py 就是这么来的，代价是丢掉 keeper 的
    兜底回评与看板联动）。只清 processed/blocked/wakeup_deps：评论级进度
    （processed_comment_ids）保留，免得把已经答过的旧评论再答一遍。

    在途条目也算「改动」并记 `manual_reopen_at`（#7）：run 结束到轮首收尾之间
    有中位 533s 的窗口，此时条目 processed=False 但 in_flight_since 仍在——旧
    逻辑判「本就没被消费」直接返回，人工的重跑意图随后被 reaper 的 processed=True
    静默吃掉。
    """
    binding = next((b for b in config.repos if b.repo == repo), None)
    if binding is None:
        raise ValueError(f"配置里没有 repo 绑定: {repo}")
    state = load_state(config.state_path)
    rs = state.repo(binding.repo_slug)
    changed: list[int] = []
    for n in numbers:
        it = rs.item(str(n))
        if it.processed or it.blocked or getattr(it, "in_flight_since", None):
            it.processed = False
            it.blocked = False
            it.wakeup_deps = []
            changed.append(n)
    if changed:
        # 逐条合并写：每条都在锁内重读盘上最新值再清终态，不用 CLI 自己的旧快照
        # 整份反盖 daemon 轮内的进度（processed_comment_ids / in_flight_since 等）。
        for n in changed:
            save_state_item(config.state_path, binding.repo_slug, str(n),
                            _clear_terminal_state)
        # 2026-10-09 定约：重新入队 = 不再等人 → 自动摘 needs-human 标签。
        # 标签只表示「正在等人处理」；只加不摘会让看板把已恢复处理的单一直列着
        # （plaita#35 实测挂 16h、hitl-mcp#4 处理完仍需手工摘）。
        for n in changed:
            _gh_remove_label("issue", repo, n, config.pipeline_needs_human_label)
        # 状态透明化（okguitar 2026-09-30 建议）：拦截/终态评论只有「已拦」形态
        # 没有「已解除」，外部无法从 issue 页面区分排队中/被拦——reopen 时补一条
        # 带 bot marker 的状态评论（marker + self_identity 双保险，不会被评论层
        # 当新输入处理）。发布失败不阻塞 state 语义（重新入队已生效）。
        src_cache: dict[str, IssueSource] = {}
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
        for n in changed:
            try:
                src = _ensure_source(binding, src_cache)
                src.post_comment(binding.repo, Resource(
                    kind="issue", number=n, title="", body="", state="open",
                    labels=[], author="", created_at="", updated_at="",
                    status="", actor_type="human", source_ref=""),
                    f"{config.bot_marker}\n[issue-keeper] 本条已于 {stamp} "
                    "解除处理终态并重新入队（值守处置），将按依赖顺序重新派发。\n")
            except Exception as e:
                log.warning("[reopen] %s#%s 状态评论发布失败: %s", binding.repo, n, e)
    return changed


def _author_allowed(config, author: str | None) -> bool:
    """作者 allowlist：空名单=全放行；大小写不敏感。"""
    if not config.author_allowlist:
        return True
    if not author:
        return False
    allowed = {a.lower() for a in config.author_allowlist}
    return author.lower() in allowed


# 不计入日限的终态（#8）：环境闸的「没跑、等重试」与内容性失败——它们各自
# 有自己的预算（failed_auto_retry + 连击升级即终态），配额语义上不是一次有效 run。
_NON_QUOTA_STATUSES = ("retry-later", "failed", "guarded", "partial")


def _counts_against_daily_limit(rec: dict) -> bool:
    """该台账行是否消耗 issue/作者日限额度。"""
    return rec.get("status") not in _NON_QUOTA_STATUSES


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
                if not _counts_against_daily_limit(rec):
                    continue                                     # #8 失败类不占额度
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


def _retry_later_backoff(config, streak: int) -> float:
    """retry-later 空转的指数退避：7min(=poll) → 1h → 6h 封顶（#6）。"""
    base = max(1, int(getattr(config, "poll_interval_secs", 300) or 300))
    if streak <= 1:
        return float(base)
    if streak == 2:
        return float(max(base, 3600))
    return float(max(base, 21600))   # max() 保单调：poll 被调到 >1h 时档位不倒退


def _arm_auto_redispatch(it, config, now: float, *, lock: Path,
                         backoff: bool = False) -> float:
    """自动重派武装（retry-later / failed 类 / engine_error 三支共用同一语义）：

    清锁 + 清在途锚 + `retry_later_streak += 1`（=「连续自动重派次数」，既是
    retry-later 指数档位的基数，也是「本轮为自动重派」的判据 → 重派轮不补发
    认领评论）+ 写 `retry_after` 冷却（backoff=True 按 #6 指数档位，否则固定
    一轮 poll_interval_secs）。不写截止就是同轮立刻再派（run_once 先收尸再扫仓）。
    终态收尾与人工 reopen 清零 streak。
    """
    lock.unlink(missing_ok=True)
    it.in_flight_since = None
    it.retry_later_streak = int(getattr(it, "retry_later_streak", 0) or 0) + 1
    delay = (_retry_later_backoff(config, it.retry_later_streak) if backoff
             else float(max(1, int(config.poll_interval_secs))))
    it.retry_after = now + delay
    return delay


def _issue_over_pipeline_limit(config, repo_full: str, number: int) -> bool:
    """同 issue 每日管线 run 次数上限（读 runs.jsonl 台账；台账缺失视为未超限）。

    与作者日限互补：作者日限防「一人刷多 issue」，这里防「同一 issue 的终态被
    反复重派」——guarded/engine_error 完成后任何把条目拉回队的路径都会再花
    半小时起步跑一整轮，#40 一夜连烧 5 轮全是超时/护栏拦截（2026-09-29）。

    retry-later 不计入：它是 preflight 环境闸（如磁盘守卫）的「没跑、等重试」
    快速失败（秒级、不消费），配额语义上不是一次 run。计入会出 10-04 事件：
    守卫风暴把每 issue 的 10 次额度烧在 1 秒的 preflight 上 → 磁盘恢复后
    当日整批 issue 被日限锁死（同内容重跑才有意义的额度被空转耗尽）。
    """
    import json
    import time
    if config.pipeline_issue_daily_limit <= 0:
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
            if rec.get("repo") == repo_full and str(rec.get("issue")) == str(number) \
                    and str(rec.get("ts", "")).startswith(today):
                if not _counts_against_daily_limit(rec):
                    continue   # 环境闸快速失败/内容性失败不占额度（#8 解耦 reopen 配额）
                n += 1
    except Exception:
        return False
    return n >= config.pipeline_issue_daily_limit


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
    """同一 issue 是否已有 run 在跑。

    bridge = run.lock 里的 pid；v2-console = console-exec.json 在途记录
    （无本地 pid，返回哨兵 -1——仅作真值判定，不参与 killpg）。
    """
    if (artifact_dir / CONSOLE_EXEC_RECORD).exists():
        return -1
    return _lock_holder(artifact_dir / PIPELINE_LOCK_NAME)


def _global_pipeline_in_flight(artifact_dir: Path) -> int | None:
    """是否有任何 run 在跑（跨进程全局并发闸）。"""
    return _lock_holder(artifact_dir.parent / GLOBAL_LOCK_NAME)


def _release_pipeline_lock(lock: Path, pid: int) -> None:
    """只删自己写的那把锁，避免误删后来者的。"""
    if _read_pipeline_lock(lock) == pid:
        lock.unlink(missing_ok=True)


def _count_in_flight(state) -> int:
    """当前在途管线 run 数（跨仓；worker 池大小的依据）。"""
    return sum(
        1
        for rs in state.repos.values()
        for it in rs.items.values()
        if getattr(it, "in_flight_since", None)
    )


def _count_repo_in_flight(state, repo_slug: str) -> int:
    """本仓当前在途管线 run 数（按仓配额 S5 用）。"""
    if state is None:
        return 0
    rs = state.repos.get(repo_slug)
    if rs is None:
        return 0
    return sum(1 for it in rs.items.values() if getattr(it, "in_flight_since", None))


def _repo_quota_exceeded(config, state, binding) -> tuple[bool, int, int]:
    """按仓配额（S5，2026-10-07）：返回 (是否超限, 本仓在途, 配额)。

    未配置该仓的配额时恒返回 (False, 0, 0)——空默认=行为不变。0 配额=该仓暂停派发。"""
    cap = (getattr(config, "pipeline_repo_limits", None) or {}).get(binding.repo)
    if cap is None:
        return False, 0, 0
    cur = _count_repo_in_flight(state, binding.repo_slug)
    return cur >= cap, cur, cap


def _gh_post_comment(kind: str, repo: str, number: int, body: str) -> None:
    """reaper 的兜底回评：不依赖 IssueSource（收尸阶段还没建 source）。"""
    import subprocess as _sp
    cmd = ["gh", kind, "comment", str(number), "--repo", repo, "--body", body]
    r = _sp.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"gh comment 失败: {(r.stderr or r.stdout or '')[-200:]}")


def _escalate_engine_error(config, kind: str, repo: str, number: int,
                           n_err: int, err: str, label: str) -> None:
    """engine_error 连击升级人工：升级评论（语义区别于终态说明）+ needs-human 标签。

    2026-10-09：原先只有 failed/筛选两条路径会「评论 + 打标」，engine_error 路径
    只落 keeper 日志 → 人工队列在 GitHub 上不可见（recursive#86 / argusai#13 实证）。
    抽成函数后，`not posted` 与 `posted=True` 两个入口都走同一套动作。
    """
    try:
        _gh_post_comment(kind, repo, number,
                         f"{config.bot_marker}\n[issue-pipeline] 本 issue 近 12 小时内连续 "
                         f"{n_err} 次引擎级失败（status=engine_error，自动重派已耗尽），"
                         f"已升级人工处理（标签 {config.pipeline_needs_human_label}）。"
                         f"最近一次原因：{_sanitize_public_comment(err or '未记录')[:280]}")
    except Exception as e:
        log.error("[%s] engine_error 升级评论发送失败: %s", label, e)
    _gh_add_label(kind, repo, number, config.pipeline_needs_human_label)


def _gh_add_label(kind: str, repo: str, number: int, label: str) -> None:
    """升级留痕：给 issue/PR 打标签。fail-open——标签不存在或无权限不阻塞收尾
    （本工具不自动建标签；internal 看板源无 label 接口，调用方照常降级为仅评论）。"""
    import subprocess as _sp
    sub = "pr" if kind == "pr" else "issue"
    try:
        r = _sp.run(["gh", sub, "edit", str(number), "--repo", repo,
                     "--add-label", label], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            log.warning("[gh] 打标签失败（%s#%s label=%s）: %s", repo, number, label,
                        (r.stderr or r.stdout or "")[-200:])
    except Exception as e:
        log.warning("[gh] 打标签异常（%s#%s label=%s）: %s", repo, number, label, e)


def _gh_remove_label(kind: str, repo: str, number: int, label: str) -> None:
    """摘升级标签（fail-open）。语义：标签只表示「正在等人处理」——

    2026-10-09 定约：keeper 在**升级时**打 `needs-human`，在**该 issue 重新入队
    回到流水线时**（reopen / 重新派发）自动摘掉。否则标签只加不摘，看板「等人工」
    会把早已恢复处理的单一直列着（plaita#35 实测：已重新入队 16h 仍挂着标签）。
    """
    import subprocess as _sp
    sub = "pr" if kind == "pr" else "issue"
    try:
        r = _sp.run(["gh", sub, "edit", str(number), "--repo", repo,
                     "--remove-label", label], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            # 标签本就不在时 gh 也会非零退出——不是错误，降为 debug
            log.debug("[gh] 摘标签未生效（%s#%s label=%s）: %s", repo, number, label,
                      (r.stderr or r.stdout or "")[-120:])
    except Exception as e:
        log.warning("[gh] 摘标签异常（%s#%s label=%s）: %s", repo, number, label, e)


@lru_cache(maxsize=1)
def _gh_login() -> str:
    """当前 gh 认证账号（跨渠道读回比对作者用）；失败返回空串。"""
    import subprocess as _sp
    try:
        r = _sp.run(["gh", "api", "user", "--jq", ".login"],
                    capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _gh_created_epoch(created_at: str) -> float:
    """GitHub ISO8601（Z 结尾 UTC）→ epoch 秒；解析失败返回 0（永不命中）。"""
    from datetime import datetime
    try:
        return datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _channel_reply_posted(repo: str, number: int, since_ts: float,
                          bot_marker: str, kind: str = "issue") -> bool:
    """跨渠道读回校验：目标渠道上派发之后是否真的出现过管线回评。

    台账的 comment_posted 由 bridge 进程收尾时写——bridge 在「评论已发出」与
    「台账落盘」之间崩溃，或旧版 bridge 根本没归一化该键（recursive #31/#32 的
    comment_posted=null），reaper 就会把「已回评」误报成「未发出回评」。
    写到哪、就从哪读回验证（recursive#2 建议）：

    - 硬证据：派发后新出现的评论正文含管线标记 ``<!-- issue-pipeline -->``
      （flow 全部出害口从 v1.0.15 起机械携带）；
    - 兜底（覆盖无标记的历史回评）：派发后新出现、作者为 keeper 自己的账号、
      且不是 keeper 自身机器评论（认领/兜底都带 bot_marker 或 [issue-pipeline]）。
    """
    import json as _json
    import subprocess as _sp
    try:
        r = _sp.run(["gh", kind, "view", str(number), "-R", repo,
                     "--json", "comments"],
                    capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False
        comments = (_json.loads(r.stdout or "{}").get("comments") or [])
    except Exception:
        return False
    me = _gh_login()
    machinery = ("[issue-pipeline]", bot_marker)
    for c in comments:
        body = str(c.get("body") or "")
        created = _gh_created_epoch(str(c.get("createdAt") or ""))
        if created < since_ts - 5:
            continue
        if "<!-- issue-pipeline -->" in body:
            return True
        author = str((c.get("author") or {}).get("login") or "")
        if me and author == me and not any(m in body for m in machinery):
            return True
    return False


def _preflight_orphans(worktree_dir: Path) -> None:
    """WIP 快照前清场（G3 kill-before-start）：杀掉 worker 硬死遗存的孤儿 agent。

    worker 被 SIGKILL/OOM 后 agent CLI（start_new_session 脱离进程组）可能仍在
    写 worktree——直接快照会拍进半写内容。经 plaita-nodes 的 preflight（agentproc
    遗言锁定位、双因子核实后 killpg）。fail-open：任何导入/执行失败只告警，
    快照照常——快照本身是尽力而为的保全，并发写硬门在 agent_run 开工 preflight。
    """
    try:
        try:
            from plaita_nodes.agent_run import preflight_workspace as _pf
        except ImportError:
            import sys as _sys
            _sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita-nodes/src")
            from plaita_nodes.agent_run import preflight_workspace as _pf
        info = _pf(str(worktree_dir))
        if info.get("action") == "killed":
            log.warning("worktree %s 孤儿 agent 已清场（pid=%s）",
                        worktree_dir, info.get("pid"))
    except Exception as e:  # noqa: BLE001 —— 见 docstring，fail-open
        log.warning("worktree %s 孤儿清场跳过：%s", worktree_dir, str(e)[:160])


def _snapshot_worktree_wip(worktree_dir: Path, label: str) -> str:
    """引擎异常终止后，把 worktree 里的未提交改动快照成本地 wip 提交。

    recursive #33/#51 实证：引擎半路崩溃时 implement 的改动以脏工作区形式留在
    `.worktrees/issue-N`——无 journal、无提交，下次 run 复用就是踩半成品。
    这里在收尸时把脏改动 commit 到管线分支自身（分支本就是管线私有产物），
    让半成品变成可 diff、可恢复、可继续的原子快照；不推送、不跑测试。
    返回给人看的快照结论（空串 = 没有脏改动/目录不存在，无需说明）。
    """
    import subprocess as _sp
    if not worktree_dir.is_dir():
        return ""
    _preflight_orphans(worktree_dir)
    def _git(args: list[str], t: int = 60) -> subprocess.CompletedProcess:
        return _sp.run(["git", "-C", str(worktree_dir), *args],
                       capture_output=True, text=True, timeout=t)
    try:
        dirty = _git(["status", "--porcelain"]).stdout or ""
        if not dirty.strip():
            return ""
        n_files = len([ln for ln in dirty.splitlines() if ln.strip()])
        _git(["add", "-A"])
        msg = (f"wip({label}): 管线异常终止自动快照（未验证、未推送；keeper 收尸兜底）")
        # --no-verify：这是 keeper 的保全性快照不是正式变更，不被仓库 commit 钩子拦/改
        r = _git(["commit", "--no-verify", "-m", msg])
        if r.returncode != 0:
            return (f"worktree 仍有 {n_files} 个文件的未提交改动，自动快照失败："
                    f"{(r.stderr or '').strip()[-160:]}")
        branch = (_git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout or "").strip()
        sha = (_git(["rev-parse", "--short", "HEAD"]).stdout or "").strip()
        return f"worktree 未提交改动（{n_files} 个文件）已快照为本地提交 {branch}@{sha}（未推送）"
    except Exception as e:
        return f"worktree 快照检查失败：{str(e)[:160]}"


def _pipeline_commit_note(worktree_dir: Path | None, base_branch: str) -> str:
    """引擎异常终止后，报告管线分支上已有的提交位置（#4 评论区新增诉求）。

    wip 快照只兜「未提交的脏改动」；recursive#67 实证另一半情况——implement 已把
    工作提交到 pipeline/issue-N（甚至已推送 origin），随后 GATE 崩溃，兜底回评却只有
    engine_error，人得去 `git branch -r` 里翻。这里就地读 worktree 的 HEAD
    （分支@短SHA、领先基线多少、是否已推送），把「干了活但没报到」变成一眼可查。
    容错：目录不存在 / git 不可用 / HEAD 无超出基线的独立提交 → 空串，不影响兜底回评。
    """
    import subprocess as _sp
    if worktree_dir is None or not worktree_dir.is_dir():
        return ""
    def _git(args: list[str], t: int = 60) -> subprocess.CompletedProcess:
        return _sp.run(["git", "-C", str(worktree_dir), *args],
                       capture_output=True, text=True, timeout=t)
    try:
        if _git(["rev-parse", "--verify", "HEAD"]).returncode != 0:
            return ""
        branch = (_git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout or "").strip()
        sha = (_git(["rev-parse", "--short", "HEAD"]).stdout or "").strip()
        remote_contains = _git(["branch", "-r", "--contains", "HEAD"])
        pushed = (remote_contains.returncode == 0 and any(
            ln.strip().startswith("origin/") for ln in remote_contains.stdout.splitlines()))
        details: list[str] = []
        base = (base_branch or "").strip()
        if base:
            ahead = _git(["rev-list", "--count", f"origin/{base}..HEAD"])
            if ahead.returncode == 0:
                n = (ahead.stdout or "0").strip()
                if n == "0":
                    return ""  # HEAD 没有超出基线的独立提交，报了也是噪音
                details.append(f"领先 origin/{base} {n} 个提交")
        details.append("已推送 origin" if pushed else "仅本地未推送")
        return f"工作已提交到 {branch}@{sha}（{'，'.join(details)}）"
    except Exception:
        return ""






_ANSI_ESC_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _sanitize_public_comment(text: str) -> str:
    """对外评论的卫生化（2026-10-01）：兜底回评会把引擎 err/gate_ctx 原样带上，
    其中含 ANSI 转义码（recursive CLI 的彩色日志）与本机绝对路径（HOME/仓库结构）
    ——对公开仓的外部读者是噪音加泄露。剥 ANSI、HOME 打码为 ~、压空白。"""
    if not text:
        return ""
    t = _ANSI_ESC_RE.sub("", text)
    home = str(Path.home())
    if home and home != "/":
        t = t.replace(home, "~")
    # 不依赖运行时 HOME：macOS 用户目录一律打码（/Users/<name>/… → ~/…）
    t = re.sub(r"/Users/[^/\s]+/", "~/", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


_COMMENT_PROC_DIR = Path("~/.issue-keeper/comments")
# 活 daemon 持有的 Popen 句柄（key=(repo_slug, number)）：优先 poll() 判活，
# daemon 重启后句柄丢失则退化为 os.kill(pid, 0)（分离进程被 launchd 收养，死即 ESRCH）。
_COMMENT_PROCS: dict = {}


def _comment_proc_alive(pid, popen) -> bool:
    if popen is not None:
        return popen.poll() is None
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def _count_comment_tasks(state) -> int:
    """全局在途评论任务数（跨仓）。只计**存活**任务：pid 已死的记录可能只是
    「agent 跑完、还没轮到属主收尸发布」，不能算容量，更不能在这儿删——
    删了属主就读不到输出、回评丢了（#19：3 条死记录曾恒占满闸门）。"""
    n = 0
    for rs in state.repos.values():
        for it in rs.items.values():
            for task in (getattr(it, "comment_tasks", None) or {}).values():
                if _comment_proc_alive(int(task.get("pid") or 0), None):
                    n += 1
    return n


def _spawn_comment_agent(entry, message: str, it, base: str, cid: str,
                         from_user: str, timeout: int) -> int | None:
    """分离进程派发评论回复 agent（评论层后台化 2026-10-01）。

    与 invoke_agent 同一命令构造（profile.py::_build_command），差异仅在：
    Popen + start_new_session（不阻塞扫描循环）、stdout/stderr 落文件供收尸、
    message 先落盘（重启接管与重试复用）。
    """
    import signal as _signal
    import subprocess
    d = _COMMENT_PROC_DIR.expanduser()
    d.mkdir(parents=True, exist_ok=True)
    out_f, err_f, msg_f = d / f"{base}.out", d / f"{base}.err", d / f"{base}.msg"
    cmd = _build_command(entry, it.session_id or "",
                         from_user=from_user, default_timeout=timeout)
    msg_f.write_text(message, encoding="utf-8")
    env = os.environ.copy()
    env.update(entry.env)
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=open(out_f, "wb"),
            stderr=open(err_f, "wb"), cwd=entry.cwd or None, env=env,
            text=True, start_new_session=True)
    except Exception as e:
        log.error("[%s] 评论 agent 派发失败 (comment=%s): %s", base, cid, e)
        return None
    try:
        proc.stdin.write(message)
        proc.stdin.close()
    except Exception as e:
        log.error("[%s] 评论 agent stdin 写入失败 (comment=%s): %s", base, cid, e)
        try:
            os.killpg(proc.pid, _signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        return None
    _COMMENT_PROCS[(base, str(cid))] = proc
    return proc.pid


def _collect_comment_tasks(src, binding, config, entry, res, it, label,
                           timeout: int, visible_prefix: str) -> int:
    """评论异步任务收尸（周期开头）：完成→消毒发布；超时→killpg；失败→重试≤1 次。

    返回本轮收尸处理的条数。同 issue 串行由调用方保证（有在途任务则不派发新评论）。
    """
    import signal as _signal
    base = f"{binding.repo_slug}-{res.number}"
    if not it.comment_tasks:
        return 0
    handled = 0
    for cid in list(it.comment_tasks.keys()):
        task = it.comment_tasks.get(cid) or {}
        pid, started = task.get("pid"), float(task.get("started_at") or 0)
        attempts = int(task.get("attempts") or 1)
        popen = _COMMENT_PROCS.pop((base, str(cid)), None)
        alive = _comment_proc_alive(pid, popen)
        timed_out = (time.time() - started) > timeout
        if alive and not timed_out:
            continue  # 仍在跑
        if alive and timed_out:
            try:
                os.killpg(int(pid), _signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            log.warning("[%s] 评论 agent 超时（%ss），进程组已清 (comment=%s)",
                        label, timeout, cid)
            alive = False
            popen = None
        if popen is not None:
            try:
                popen.wait(timeout=10)
            except Exception:
                pass
        d = _COMMENT_PROC_DIR.expanduser()
        out = ""
        err = ""
        try:
            out = (d / f"{base}.out").read_text(encoding="utf-8", errors="replace")
            err = (d / f"{base}.err").read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        reply = _parse_reply(out, err)
        if reply.text and reply.text.strip():
            if reply.session_id:
                it.session_id = reply.session_id
            _publish_reply(src, binding, res, reply.text, config, visible_prefix)
            it.processed_comment_ids.add(str(cid))
            del it.comment_tasks[cid]
            handled += 1
            # 评论回复完也推到 review（与原同步路径同语义）
            if _supports_status(src) and res.status not in ("review",):
                _safe_move(src, binding, res, "review", actor=_agent_label(binding, config),
                           actor_type="agent", comment="评论后重新 review")
            log.info("[%s] 评论 agent 完成 (comment=%s)，回评已发布", label, cid)
            continue
        # 失败：重试 ≤1 次（attempts 计数跨重试累加），到顶放弃并标记已处理
        if attempts < 2:
            pid2 = _spawn_comment_agent(entry, (d / f"{base}.msg").read_text(encoding="utf-8")
                                        if (d / f"{base}.msg").exists() else "",
                                        it, base, cid, config.agent_from_user, timeout)
            if pid2 is not None:
                it.comment_tasks[cid] = {"pid": pid2, "started_at": time.time(),
                                         "attempts": attempts + 1}
                log.warning("[%s] 评论 agent 无输出，已自动重试 (comment=%s, 第 %d 次)",
                            label, cid, attempts + 1)
                continue
        it.processed_comment_ids.add(str(cid))
        del it.comment_tasks[cid]
        log.warning("[%s] 评论 agent 失败且重试耗尽 (comment=%s, attempts=%d)，"
                    "标记已处理；如需重新处理请让作者再评论一次", label, cid, attempts)
    return handled


def _append_pipeline_record(repo_full: str, number: int, record: dict) -> None:
    """reaper 代记台账行（bridge 未写台账的崩溃路径，使连续 engine_error 计数可用）。"""
    import json
    ledger = Path("~/.issue-keeper/pipeline/runs.jsonl").expanduser()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"repo": repo_full, "issue": number,
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S+0800"),
                            **record}) + "\n")

def _consecutive_engine_errors(repo_full: str, number: int) -> int:
    """台账里该 issue 末尾连续 engine_error 的条数（自动重试封顶用）。

    仅统计 **近 12h** 内的记录（2026-10-02：连击原本跨天不衰减——disk 时代
    的陈旧失败会让今晚的首败直接判「二连升级」吃掉自动重试，#68/#56 实证
    连烧手工 reopen；失败间隔超过半天的，语义上已是新的一次尝试）。

    `node_retry_exhausted` 行**打断**连击（2026-10-02，DESIGN §5 D6）：该标记
    是宿主烧满节点重试后的终局判定，属「完整一轮战役的终态」而非瞬态崩溃；
    本计数封顶的是**崩溃重试**，把终局行混入会让一次耗尽升级吃掉其后新派发
    首败的重试额度。耗尽路径本身永远不走本计数（reaper 见标记直接升级），
    故打断零成本。"""
    import json
    import time as _time
    ledger = Path("~/.issue-keeper/pipeline/runs.jsonl").expanduser()
    if not ledger.exists():
        return 0
    cutoff = _time.time() - 12 * 3600
    rows: list[tuple[str, bool]] = []
    try:
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("repo") == repo_full and str(rec.get("issue")) == str(number):
                # ts 形如 2026-10-01T16:42:44+0800；解析失败按窗口外处理。
                # 时间序遍历：窗口外记录只在头部，跳过（勿 break——会丢其后新记录）
                try:
                    from datetime import datetime
                    ts = datetime.fromisoformat(str(rec.get("ts", "")).replace("Z", "+00:00"))
                    if ts.timestamp() < cutoff:
                        continue
                except Exception:
                    continue
                rows.append((str(rec.get("status") or ""),
                             bool(rec.get("node_retry_exhausted"))))
    except OSError:
        return 0
    n = 0
    for s, exhausted in reversed(rows):
        if s in _FAILURE_STATUSES:
            continue                     # #8：内容性失败不清零崩溃连击
        if s != "engine_error" or exhausted:
            break
        n += 1
    return n


# 内容性失败（#8）：共享「近 12h 连击 ≥2 → 升级人工」的语义
_FAILURE_STATUSES = ("failed", "guarded", "partial")
# 只有 failed 吃 failed_auto_retry 的自动重派额度（guarded/partial 单次即终态）
_RETRY_STATUSES = ("failed",)


def _consecutive_failures(repo_full: str, number: int) -> int:
    """台账里该 issue 末尾连续失败类（failed/guarded/partial）的条数，12h 窗内。

    与 `_consecutive_engine_errors` 同款 off-by-one 契约：台账终态行在计数
    **之前**已落盘，尾部连续数已含本次，决策处不得再 +1（首败 1 → 自动重试，
    二连 2 → 升级）。"""
    import json
    import time as _time
    ledger = Path("~/.issue-keeper/pipeline/runs.jsonl").expanduser()
    if not ledger.exists():
        return 0
    cutoff = _time.time() - 12 * 3600
    rows: list[str] = []
    try:
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("repo") == repo_full and str(rec.get("issue")) == str(number):
                try:
                    from datetime import datetime
                    ts = datetime.fromisoformat(str(rec.get("ts", "")).replace("Z", "+00:00"))
                    if ts.timestamp() < cutoff:
                        continue     # 窗口外记录只在头部，跳过（勿 break）
                except Exception:
                    continue
                rows.append(str(rec.get("status") or ""))
    except OSError:
        return 0
    n = 0
    for s in reversed(rows):
        if s not in _FAILURE_STATUSES:
            break
        n += 1
    return n


def _latest_pipeline_record(repo_full: str, number: int, since_ts: float) -> dict | None:
    """读台账里该 issue 最晚的一条记录（dispatch 之后写的才算）。"""
    import json
    ledger = Path("~/.issue-keeper/pipeline/runs.jsonl").expanduser()
    if not ledger.exists():
        return None
    best: dict | None = None
    try:
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("repo") != repo_full or str(rec.get("issue")) != str(number):
                continue
            ts = rec.get("ts", "")
            try:
                t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                continue
            if t >= since_ts - 1 and (best is None or ts >= (best.get("ts") or "")):
                best = rec
    except OSError:
        return None
    return best


# ── per-repo 管线契约解析（v0.3）────────────────────────────────────────────
# 通用 flow 曾只参数化 test_command：基线硬编码 origin/main、push_mode 全局直推、
# 17/18 仓空门假绿。契约化后：无真门的仓不进管线（回退 legacy 单 agent 路径），
# readonly 仓只调查不开工，基线/安装/交付策略/仓规注入全部 per-repo。

# flow 内置段预算（与 issue_pipeline_flow.py 的历史全局值一致；per-repo
# timeout_overrides 在此之上覆盖——TS 仓可大幅调小，recursive 的长门可放大）
_PIPELINE_SEGMENT_TIMEOUTS = {
    "investigate": 2100, "plan": 1200, "implement": 4200,
    "review": 2700, "fix_review": 2700, "fix_test": 900, "document": 300,
}


def _pipeline_repo_cfg(config: Config, binding: RepoBinding) -> tuple[PipelineRepoConfig | None, str]:
    """解析该仓的管线契约。返回 (None, 原因) = 不进管线（回退 legacy 路径）。"""
    pc = config.pipeline_repo_cfg(binding.repo)
    if not pc.enabled:
        return None, f"pipeline_repos[{binding.repo}].enabled=false"
    if pc.mode == "readonly":
        return pc, ""
    if not pc.has_gate():
        return None, (f"pipeline_repos 未登记 {binding.repo} 且无质量门——"
                      "无真门不进管线（空门跑 true 恒过的假绿已废止），走 legacy")
    return pc, ""


def _gate_runner_path(config: Config) -> Path:
    """gate_runner.py 的位置（本地路径与 console 路径共用的**单一事实源**）。"""
    return Path(config.pipeline_bridge).expanduser().parent / "gates" / "gate_runner.py"


def _gate_spec(pc: PipelineRepoConfig) -> dict:
    """本仓的 gate spec（结构与 gate_runner 的 --spec 输入一致）。

    console 路径（engine=v2-console）与本地路径共用同一份 spec 生成——保证
    「N 道门 / 独立预算 / paths 条件 / autofix 重检」两条路径逐字段等价
    （2026-10-07，jeffkit 指示「flow 严格按原来的实现，不许短斤缺两」）。"""
    if pc.gates:
        gates = [{"name": g.name, "command": g.command,
                  "timeout_secs": g.timeout_secs, "paths": list(g.paths or []),
                  "autofix": g.autofix}
                 for g in pc.gates]
    elif (pc.test_command or "").strip():
        # 仅配 test_command 的旧式仓：包成单道门走同一 runner（避免 console
        # 路径掉进 flow 的 cargo 回退——非 Rust 仓必假红）。
        gates = [{"name": "test", "command": pc.test_command.strip(),
                  "timeout_secs": 0, "paths": [], "autofix": ""}]
    else:
        return {}
    return {"base": pc.base_branch, "gates": gates}


def _gate_runner_invocation(config: Config, pc: PipelineRepoConfig, artifact_dir: Path) -> str:
    """多门仓：写 spec 到产物目录，test_command = gate_runner 调用（argv 可执行）。"""
    import json

    spec_file = artifact_dir / "gates.json"
    spec_file.write_text(json.dumps(_gate_spec(pc), ensure_ascii=False, indent=1),
                         encoding="utf-8")
    return f"python3 {_gate_runner_path(config)} --spec {spec_file} --cwd ."



_DEP_RE = re.compile(r"depends-on[:：]\s*#(\d+)", re.IGNORECASE)


def _parse_depends_on(body: str) -> list[int]:
    """解析正文依赖声明 `depends-on: #N`（大小写不敏感；可声明多个，去重升序）。"""
    return sorted({int(m) for m in _DEP_RE.findall(body or "")})


_CI_FIX_RE = re.compile(r"^\s*ci-fix\s*:\s*(true|yes|on)\s*$", re.IGNORECASE | re.MULTILINE)


def _declares_ci_fix(body: str) -> bool:
    """正文头部是否显式声明 `ci-fix: true`（大小写不敏感）。

    #11：CI/工作流类 issue 的修复对象恰是 `.github/workflows/*`（guard 禁改
    名单默认拦截）。这是 issue 侧的显式 opt-in 通道——**还必须配合仓库契约
    allow_github_paths 才放行**（声明权在契约、触发权在 issue，两者都有才开）。
    只看头部 500 字符，正文深处出现同形字样不认（不可信正文不配当开关）。
    """
    return bool(_CI_FIX_RE.search((body or "")[:500]))


def _dep_settled(config, repo_full: str, num: int) -> bool:
    """依赖 #N 是否已终态：state 里该 item 存在且 processed=True（管线已收尾）。

    依赖项尚未进过管线（未 screen/未派发）= 未就绪。依赖 run 以失败收尾也算
    settled（v1 语义：失败由人看，不级联阻塞）——严格失败阻断留待有真实需求再加。
    """
    try:
        st = json.loads(Path(config.state_file).expanduser().read_text())
    except Exception:
        return False
    it = (st.get("repos", {}).get(repo_full.replace("/", "-"), {})
            .get("items", {}).get(str(num)))
    return bool(it and it.get("processed"))


# ── engine=v2-console（G5/G6，2026-10-02）────────────────────────────
# 在途锚与哨兵：console 模式没有本地 bridge 进程，run.lock 里没有 pid 可写。
# console-exec.json = 在途记录（daemon 重启不丢，reaper 据此轮询）；锁文件写
# 哨兵串（非数字，_lock_holder 视为无 pid 但不删——在途判定走记录文件）。
CONSOLE_EXEC_RECORD = "console-exec.json"
# 影子副本在途锚（与主锚分离——reaper 只认主锚，影子绝不进收尾分流）
SHADOW_EXEC_RECORD = "shadow-exec.json"
# 影子副本结果（对账用；只读产物，不进台账）
SHADOW_RESULT_RECORD = "shadow-result.json"
CONSOLE_LOCK_SENTINEL = "console"


def _console_exec_record(artifact_dir: Path) -> dict | None:
    """读 console 在途记录；缺失/损坏 → None（= 非 console 在途）。"""
    try:
        rec = json.loads((artifact_dir / CONSOLE_EXEC_RECORD).read_text(encoding="utf-8"))
        return rec if isinstance(rec, dict) and rec.get("execution_id") else None
    except (OSError, ValueError):
        return None


def _dispatch_console_execution(config, binding, res, it, label: str,
                                pc: PipelineRepoConfig, artifact_dir: Path,
                                body_file: Path) -> dict:
    """engine=v2-console：派发到 plaita-console（POST /api/executions），立即返回。

    台账由 reaper 收尾时落（D5 台账写入迁移）；本函数留三样：v2-goal.md（goal
    正文，与 v2_bridge 同构）、console-exec.json（在途锚）、dispatch.json（收尾
    上下文：worktree 路径等，供共享收尾复用）。
    """
    import time as _time

    from . import console_exec as _ce

    try:
        # 真执行目标 = **新系统 console**（2026-10-06 修正）：此前用
        # pipeline.console（旧链路/本机 8123），金丝雀首次试切实测把单派到了
        # 本地单机档 console 而非新系统 —— 见 engine_client_from_config。
        client = _ce.engine_client_from_config(config)
    except _ce.ConsoleExecError as e:
        log.error("[%s] console 派发不可用：%s", label, e)
        return {"status": "engine_error", "comment_posted": False}

    flow_id = pc.console_flow_id or "self-improve-v2"
    goal = f"#{res.number} {res.title or ''}\n\n{(res.body or '')[:16000]}".strip()
    (artifact_dir / "v2-goal.md").write_text(goal, encoding="utf-8")
    run_id = f"pipeline-{res.number}-{_time.strftime('%m%d%H%M%S')}"
    params = {
        "goal": goal,
        "repo": binding.cwd,
        "run_id": run_id,
        # v2 flow 的 INPUT 契约吃 run_dir（checkpoint 目录；self_improve_bridge_v2
        # 同式 repo/.flowcast/runs/<run_id>），run_id 仅供日志/台账关联。
        "run_dir": f"{binding.cwd.rstrip('/')}/.flowcast/runs/{run_id}",
        "agent": pc.agent or "glm53-flash",
        "reviewer": pc.reviewer or "glm53-flash",
    }
    # per-repo 门禁注入（2026-10-06，多仓支持）：v2 flow 原为 recursive(Rust)
    # 硬编码 cargo 门——跑别的仓必然假红（plaita#41 实证：cargo fmt 在 Python
    # 仓报 "could not find Cargo.toml"）。把本仓已配置的 pc.gates 传下去，
    # flow 侧按序取用（fmt→lint→test 语义位）；不传 = flow 回退 cargo 三段
    # （recursive 自身零回归）。仅传 name/command，timeout 由 flow 默认（其
    # 预算按 Rust 冷构建调优，非 Rust 仓通常更快，够用）。
    #
    # ⚠️ 执行语义（2026-10-07 修复，plaita#28「gates/tests 失败」根因）：flow 的
    # GATE 节点对单字符串命令做 shlex.split 后**按 argv 执行、不经 shell**
    # （plaita_nodes/gate.py）——门命令里的 `&&`/`cd`/环境前缀会被拆成 argv：
    # 轻则参数错乱报 usage error，重则 `cd X && …` 静默假绿（cd 吞掉剩余参数）。
    # 本地 gate_runner 对同一份命令是 ["bash","-c",cmd]（shell 语义），两条路径
    # 必须对齐——这里显式用 bash -c 包装（shlex.quote 保引号）。
    if pc.gates:
        import shlex as _shlex
        params["gates"] = [{"name": g.name, "cmd": f"bash -c {_shlex.quote(g.command)}",
                            "timeout_secs": int(g.timeout_secs or 0)}
                           for g in pc.gates]
    # 完整 gate spec 透传（2026-10-07，jeffkit 指示「flow 严格按原来的实现」）：
    # flow v1.0.5 起优先消费 gates_spec —— 在 run_dir 落盘后调**同一个**
    # gate_runner.py，N 道门/独立预算/paths 条件/autofix 与本地路径逐字段等价；
    # gates 三段（旧形态）保留作回滚兼容（旧 flow 版本仍可跑，只是降级）。
    _spec = _gate_spec(pc)
    if _spec:
        params["gates_spec"] = json.dumps(_spec, ensure_ascii=False)
        params["gate_runner"] = str(_gate_runner_path(config))
        params["gate_timeout_secs"] = pc.effective_gate_timeout()
    # setup 透传（2026-10-07）：flow 的 preflight 会在 worktree 建立后执行
    # `bash -c <setup_command>`（900s 预算；失败=preflight 失败）。非 Rust 仓
    # （TS/Python）在 fresh worktree 里必须先装依赖，否则门必挂。
    if (pc.setup_command or "").strip():
        params["setup_command"] = pc.setup_command
    try:
        execution_id = client.start_execution(flow_id, params)
    except _ce.ConsoleExecError as e:
        log.error("[%s] console 派发失败（flow=%s）：%s", label, flow_id, e)
        return {"status": "engine_error", "comment_posted": False}

    record = {
        "execution_id": execution_id, "flow_id": flow_id, "run_id": run_id,
        "engine": "v2-console", "retry_count": 0,
        "dispatched_at": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (artifact_dir / CONSOLE_EXEC_RECORD).write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8")
    (artifact_dir / "dispatch.json").write_text(json.dumps({
        "repo_full": binding.repo, "issue_number": res.number,
        "engine": "v2-console", "execution_id": execution_id, "flow_id": flow_id,
        "main_clone": binding.cwd,
        "worktree_dir": f"{binding.cwd}/.worktrees/issue-{res.number}",
        "base_branch": pc.base_branch,
    }, ensure_ascii=False), encoding="utf-8")
    for lk in (artifact_dir / PIPELINE_LOCK_NAME, artifact_dir.parent / GLOBAL_LOCK_NAME):
        try:
            lk.write_text(CONSOLE_LOCK_SENTINEL, encoding="utf-8")
        except OSError as e:
            log.warning("[%s] 写 console 锁哨兵失败: %s", label, e)
    it.in_flight_since = _time.time()
    log.info("[%s] 已提交 console execution %s（flow=%s，run=%s）",
             label, execution_id, flow_id, run_id)
    return {"status": "dispatched", "comment_posted": True}


def _dispatch_shadow_execution(config, binding, res, label: str,
                                pc: PipelineRepoConfig, artifact_dir: Path) -> None:
    """影子模式（放量迁移首阶）：本地主执行已在跑，这里再旁路派一份到 console。

    影子副本的**唯一目的**是产出「同一 issue 在新系统下的结论」供对账，因此：
    - **绝不**写 console-exec.json 在途锚（reaper 会把本地主的台账误判为 console
      在途）；影子锚单独落 shadow-exec.json，仅供对账读取；
    - 失败**只记日志不抛**——影子绝不能影响本地主执行的成败；
    - 落 shadow-result.json（收尾时由 reaper 的 shadow 回收器补写，见
      _collect_shadow_results）。
    """
    import time as _time

    from . import console_exec as _ce

    # 影子目标 = 新系统 console。优先专用 `shadow_console`（不干扰既有
    # pipeline.console——后者仍服务 screener/bridge 的旧链路），缺省回退它。
    try:
        client = _ce.engine_client_from_config(config)
    except _ce.ConsoleExecError as e:
        log.warning("[%s] 影子派发跳过（console 不可用）：%s", label, e)
        return

    flow_id = pc.shadow_flow_id or pc.console_flow_id or "self-improve-v2"
    goal = f"#{res.number} {res.title or ''}\n\n{(res.body or '')[:16000]}".strip()
    run_id = f"shadow-{res.number}-{_time.strftime('%m%d%H%M%S')}"
    params = {
        "goal": goal,
        "repo": binding.cwd,
        "run_id": run_id,
        "run_dir": f"{binding.cwd.rstrip('/')}/.flowcast/runs/{run_id}",
        "agent": pc.agent or "glm53-flash",
        "reviewer": pc.reviewer or "glm53-flash",
        "shadow": True,          # 供 flow/worker 侧识别并强制不落地（阶段 1a 后启用）
    }
    try:
        execution_id = client.start_execution(flow_id, params)
    except _ce.ConsoleExecError as e:
        log.warning("[%s] 影子派发失败（flow=%s，不影响主执行）：%s", label, flow_id, e)
        return

    rec = {
        "execution_id": execution_id, "flow_id": flow_id, "run_id": run_id,
        "engine": "shadow", "shadow_of": "local",
        "dispatched_at": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    try:
        (artifact_dir / SHADOW_EXEC_RECORD).write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("[%s] 写影子锚失败: %s", label, e)
    log.info("[%s] 影子副本已提交 console execution %s（flow=%s，run=%s）——只算不发，不影响主执行",
             label, execution_id, flow_id, run_id)


def _reap_console_execution(config, binding, it, key, label: str,
                            artifact_dir: Path, crec: dict, now: float,
                            void: bool = False) -> dict | None:
    """v2-console 在途轮询（G5/G6）。返回 None=仍在途；否则返回已落账的台账行，
    交回 _reap_pipelines 走共享收尾（读回校验/engine_error 重派/升级/兜底回评）。

    决策表（设计稿 §G5）：error → resume-retry ×1（断点步进，免整跑重做）；
    running + last_update_time 年龄超阈 → zombie（cancel + engine_error 行）；
    GET 404 且在 `console_queue_grace_secs` 宽限内 → 记录未落（排队中），返回
    None 不动作不重派（plaita#18）；其余终态 → verdict 映射落账。
    非 engine_error 的收尾回评由本函数出
    （console flow 无回评节点契约），避免共享兜底「未确认发出回评」文案。

    void=True（#7 窗口内人工 reopen，run 结论作废）：照常轮询判终态并清锚，但不发
    终态回评、不落台账行——收尾分流在此处发生，晚于它的 void 判定会留下「结论作废」
    却已回评的痕迹。
    """
    import time as _time

    from . import console_exec as _ce

    try:
        # 收尾目标须与派发目标一致（同为 engine_client_from_config）：派到新
        # 系统却从旧 console 收尾 = 永远查不到，执行悬挂。
        client = _ce.engine_client_from_config(config)
        detail = client.get_execution(str(crec["execution_id"]))
    except _ce.ConsoleExecUnavailable as e:
        log.warning("[%s] console 不可达，本轮跳过：%s", label, e)
        return None
    except _ce.ConsoleExecNotFound:
        # plaita#18：POST 只入队 Redis，执行记录由 worker 消费时才首次落盘——
        # 派发到被消费之间 GET 恒 404。宽限期内这是**排队中**（背压队列里排队
        # 是设计内行为），判 engine_error 会 re-dispatch 同一 issue（重复执行，
        # 两条 run 同时 land 还会撞 git 合并）。超期仍 404 才走既有自愈路径。
        if _ce.record_queued(crec, config.console_queue_grace_secs,
                             inflight_since=getattr(it, "in_flight_since", None),
                             now=now):
            log.info("[%s] execution %s 尚无记录（派发于 %s，宽限 %ss）——判排队中，"
                     "本轮不动作不重派", label, crec.get("execution_id"),
                     crec.get("dispatched_at"), config.console_queue_grace_secs)
            return None
        detail = {"status": "error",
                  "error": {"message": "execution 404（console 侧被清理/TTL 过期?）"}}
    except _ce.ConsoleExecError as e:
        log.warning("[%s] console 查询失败，本轮跳过：%s", label, e)
        return None

    status = str(detail.get("status") or "")
    number = int(key.split(":")[-1])
    kind = "pr" if key.startswith("pr:") else "issue"

    if status == "running":
        if not _ce.zombie(detail, config.console_zombie_secs, now=now):
            return None
        try:
            client.cancel(str(crec["execution_id"]))
        except _ce.ConsoleExecError as e:
            log.warning("[%s] zombie cancel 失败（仍按 zombie 收尾）：%s", label, e)
        log.error("[%s] console execution 疑似 zombie（>%ss 无 checkpoint 刷新），cancel",
                  label, config.console_zombie_secs)
        row = _ce.map_verdict({"verdict": "engine_error",
                               "why": f"zombie：>{config.console_zombie_secs}s 无步界持久化"})
    elif status == "error":
        if int(crec.get("retry_count") or 0) < config.console_retry_max:
            try:
                client.resume(str(crec["execution_id"]), "retry")
            except _ce.ConsoleExecError as e:
                log.warning("[%s] resume-retry 失败（转 engine_error 行重派）：%s", label, e)
                row = _ce.map_verdict({"verdict": "engine_error",
                                       "why": f"resume-retry 失败：{e}"})
            else:
                crec["retry_count"] = int(crec.get("retry_count") or 0) + 1
                (artifact_dir / CONSOLE_EXEC_RECORD).write_text(
                    json.dumps(crec, ensure_ascii=False), encoding="utf-8")
                log.info("[%s] engine error → resume-retry（第 %d 次，断点步进）",
                         label, crec["retry_count"])
                return None
        else:
            row = _ce.map_verdict(_ce.verdict_from_execution(detail))
    else:
        row = _ce.map_verdict(_ce.verdict_from_execution(detail))

    # 回评守卫：engine_error（既有台账/升级语义）与 retry-later（环境性 deferral，
    # console_exec.py 语义表——issue 状态未变、不消费，同 404 宽限族）都不出收尾
    # 回评，只落台账行；其余结论（done/failed 等）keeper 照常出真实收尾回评。
    # void（#7 窗口内人工 reopen）：结论作废，两者都不留。
    if (not void and row["status"] not in ("engine_error", "retry-later")
            and not bool(row.get("comment_posted"))):
        parts = [f"管线收尾（console 执行）：status={row['status']}"]
        if row.get("note"):
            parts.append(str(row["note"]))
        if row.get("stage"):
            parts.append(f"stage={row['stage']}")
        if row.get("error"):
            parts.append(str(row["error"])[-280:])
        try:
            _gh_post_comment(kind, binding.repo, number,
                             f"{config.bot_marker}\n[issue-pipeline] "
                             f"{_sanitize_public_comment('；'.join(parts))}。")
            row["comment_posted"] = True
        except Exception as e:
            log.error("[%s] console 收尾回评失败: %s", label, e)

    if not void:
        _append_pipeline_record(binding.repo, number, {
            **row, "flow_source": "v2-console",
            "execution_id": crec.get("execution_id"),
        })
    # 终态已落账：清在途锚与锁（共享收尾按无锁/终态处理）
    (artifact_dir / CONSOLE_EXEC_RECORD).unlink(missing_ok=True)
    (artifact_dir / PIPELINE_LOCK_NAME).unlink(missing_ok=True)
    (artifact_dir.parent / GLOBAL_LOCK_NAME).unlink(missing_ok=True)
    log.info("[%s] console execution 终态%s：status=%s（execution=%s）",
             label, "作废（人工 reopen）" if void else "落账",
             row["status"], crec.get("execution_id"))
    return row
def _engine_env_with_run_deadline(config, pc, start_ts: float, label: str) -> dict:
    """构造 dispatch engine_env：engine=v2 注入 RECURSIVE_RUN_DEADLINE（2026-10-02 评审遗留 #3）。

    recursive v3 宿主读该 env（epoch 秒，`dl = float(deadline)`）做 run 级预算
    墙，到点优雅退出：verdict/台账落盘且带 node_retry_exhausted——reaper 见标记
    跳过自动重派。不注入则宿主走进程内默认 8h，与 keeper 侧护栏脱节：默认
    pipeline_timeout_secs=5400s 下 reaper 先 SIGKILL 整组，宿主没机会
    checkpoint/回评（v2_bridge 的 V2_TIMEOUT_SECS=28800 更是永不到点）。

    注入值 = start_ts + pipeline_timeout_secs - run_deadline_margin_secs；
    start_ts 与 reaper 基线（it.in_flight_since）取同一时钟读数——宿主到点
    恰在 killpg 时刻前 margin 秒。用户在 engine_env 显式配了该键则不覆盖
    （显式优先）。仅 engine=v2 注入（pipeline 引擎的 issue-pipeline flow 没有
    该 env 的读者）；margin ≥ 预算即无优雅窗口，视为配置矛盾，跳过注入并
    告警（护栏退回 killpg），绝不注入一个必然秒触发的假 deadline。
    """
    env = dict(pc.engine_env)
    if pc.engine != "v2":
        return env
    if env.get("RECURSIVE_RUN_DEADLINE"):
        return env  # 显式优先
    margin = max(0, int(getattr(config, "run_deadline_margin_secs", 300)))
    window = int(config.pipeline_timeout_secs) - margin
    if window <= 0:
        log.warning("[%s] run_deadline_margin_secs(%d) ≥ pipeline_timeout_secs(%d)："
                    "无优雅收尾窗口，跳过 RECURSIVE_RUN_DEADLINE 注入（护栏退回 killpg）",
                    label, margin, config.pipeline_timeout_secs)
        return env
    env["RECURSIVE_RUN_DEADLINE"] = str(int(start_ts + window))
    return env


def _dispatch_pipeline(config, binding, res, it, label: str,
                       pc: PipelineRepoConfig | None = None) -> dict:
    """后台派发一次管线 run（bridge），立即返回；完成由 _reap_pipelines 收尾。

    bridge 需要的 payload 走 dispatch.json 文件（后台进程不再有 stdin 可写），
    stdout/stderr 追加到 artifact_dir/bridge-<时刻>.log 供人工排查。
    pc = per-repo 契约（v0.3）；缺省时现场解析。
    """
    import json
    import subprocess
    import sys
    import time

    if pc is None:
        # 生产路径总是先过 _pipeline_repo_cfg 门控再传入；直接调用（测试/工具）
        # 时退到无门控解析——空门会在 flow 的 GATE 节点大声失败（不再假绿）。
        pc = config.pipeline_repo_cfg(binding.repo)
    # 仓库契约的稳定别名：`pc` 下方会被 config.pipeline.console 顶掉（既有无害
    # 遮蔽），影子等需要 per-repo 契约的逻辑一律读 repo_pc，避免踩遮蔽。
    repo_pc = pc

    bridge = Path(config.pipeline_bridge).expanduser()
    if pc.engine == "v2":
        # engine=v2：接单切 self-improve v2 引擎（同目录 v2_bridge 适配同一
        # 派发契约——dispatch.json / 台账 ledger / RESULT 行）。
        bridge = bridge.parent / "v2_bridge.py"
    if pc.engine == "v2-console":
        # engine=v2-console（G5/G6）：不经 bridge——派发到 plaita-console
        # （POST /api/executions），reaper 轮询收尾。单独入口，共享下方
        # deps 闸 / 在途去重 / body_file 产物。
        pass  # bridge 检查对 console 无意义，放行到 body_file 后的分叉
    elif not bridge.exists():
        log.error("[%s] pipeline bridge 不存在: %s", label, bridge)
        return {"status": "engine_error", "comment_posted": False}

    slug = binding.repo.split("/")[-1]
    artifact_dir = Path(f"~/.issue-keeper/pipeline/{slug}-{res.number}").expanduser()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # 并发闸（2026-09-30 重构）：二值全局锁此前把并发压成事实串行（12 连发
    # issue 只消化得动 1 条），已移除——并发上限由调用方的计数闸
    # （pipeline_max_in_flight，state 口径）统一管。同 issue 互斥保留二值锁
    # （同一 issue 的产物目录不能两 run 共用）。
    # 依赖声明：正文 `depends-on: #N`（可多行/逗号多个）——依赖项未走到终态
    # （processed）则本轮 hold，不消费首次响应，下一轮重查。
    deps = _parse_depends_on(res.body or "")
    if deps:
        waiting = [d for d in deps if not _dep_settled(config, binding.repo, d)]
        if waiting:
            log.info("[%s] 依赖未就绪 %s，本轮 hold（depends-on: %s）",
                     label, waiting, deps)
            return {"status": ALREADY_RUNNING, "comment_posted": True}
    holder = _pipeline_in_flight(artifact_dir)
    if holder is not None:
        log.warning("[%s] 同 issue 已有 pipeline run 在跑 (pid=%s)，跳过本轮派发", label, holder)
        return {"status": ALREADY_RUNNING, "comment_posted": True}

    body_file = artifact_dir / "00-issue.md"
    body_file.write_text((res.body or "")[:16000], encoding="utf-8")

    if pc.engine == "v2-console":
        return _dispatch_console_execution(config, binding, res, it, label, pc,
                                           artifact_dir, body_file)

    # per-repo 契约（v0.3）：基线/安装/门/交付/注入全部来自 pipeline_repos 配置，
    # 全局 pipeline_push_mode / pipeline_review_mode 仅作缺省。
    test_command = pc.test_command.strip()
    if pc.gates:
        test_command = _gate_runner_invocation(config, pc, artifact_dir)
    timeouts = dict(_PIPELINE_SEGMENT_TIMEOUTS)
    timeouts.update({k: int(v) for k, v in pc.timeout_overrides.items()})

    # 时钟统一（RECURSIVE_RUN_DEADLINE 注入）：下面这个读数既是 deadline 注入的
    # 基点，也是 reaper 的超时基线（it.in_flight_since）——保证宿主预算墙恰好
    # 落在 killpg 时刻前 run_deadline_margin_secs 秒（见 helper docstring）。
    now = time.time()

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
        "test_command": test_command,
        "gate_timeout_secs": pc.effective_gate_timeout(),
        "base_branch": pc.base_branch,
        "setup_command": pc.setup_command,
        "setup_timeout_secs": pc.setup_timeout_secs,
        "readonly": pc.mode == "readonly",
        "review_notes": pc.review_notes,
        "triage_notes": pc.triage_notes,
        "doc_notes": pc.doc_notes,
        # #11：.github 禁改名单的显式例外——仓库契约声明允许的子路径 +
        # issue 正文头部 `ci-fix: true` 双闸门，两者齐备才放行。
        "allow_github_paths": pc.allow_github_paths if _declares_ci_fix(res.body) else [],
        "review_mode": pc.resolved_review_mode(config.pipeline_review_mode),
        "push_mode": pc.resolved_push_mode(config.pipeline_push_mode),
        "agent": pc.agent,
        "reviewer": pc.reviewer,
        "engine_env": _engine_env_with_run_deadline(config, pc, now, label),
        "investigate_timeout": timeouts["investigate"],
        "plan_timeout": timeouts["plan"],
        "implement_timeout": timeouts["implement"],
        "review_timeout": timeouts["review"],
        "fix_review_timeout": timeouts["fix_review"],
        "fix_test_timeout": timeouts["fix_test"],
        "document_timeout": timeouts["document"],
    }
    pc = config.pipeline.console
    if pc.url and pc.api_key:
        payload["console"] = {
            "url": pc.url, "api_key": pc.api_key, "flow_id": pc.flow_id,
            "refresh_secs": pc.refresh_secs, "cache_path": pc.cache_path,
        }
    if config.pipeline.observability_redis:
        payload["observability_redis"] = config.pipeline.observability_redis

    payload_file = artifact_dir / "dispatch.json"
    payload_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    log_path = artifact_dir / f"bridge-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log_fh = open(log_path, "ab")
    log.info("[%s] 提交 issue-pipeline run（后台，push_mode=%s, review_mode=%s, 日志 %s）",
             label, payload["push_mode"], payload["review_mode"], log_path.name)
    try:
        proc = subprocess.Popen(
            [sys.executable, str(bridge), str(payload_file)],
            stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT,
            text=True, start_new_session=True, close_fds=True,
        )
    finally:
        log_fh.close()

    # 锁里写 bridge 的 pid：keeper 被 KeepAlive 重启后，新实例据此发现「已在跑」；
    # reaper 也据 pid 判断 run 是否结束。两把：本 issue 一把 + 全局槽位一把。
    lock = artifact_dir / PIPELINE_LOCK_NAME
    global_lock = artifact_dir.parent / GLOBAL_LOCK_NAME
    for lk in (lock, global_lock):
        try:
            lk.write_text(str(proc.pid), encoding="utf-8")
        except OSError as e:
            log.warning("[%s] 写 pipeline 锁失败（%s，并发保护失效）: %s", label, lk.name, e)

    it.in_flight_since = now
    it.retry_after = None      # #8：本次重派已消费退避，清掉免得残留
    # 注意：这里**不**清 retry_later_streak（#6）——它是「本轮是自动重派」的判据，
    # 清了每轮都从 7min 重来，认领评论抑制也失效。

    # 影子模式（放量迁移首阶）：本地主执行已起，旁路再派一份到 console 供对账。
    # 失败只记日志、不抛——影子绝不能影响主执行；且影子**不写** console-exec.json
    # 在途锚，故 reaper 的收尾分流（认主锚）不会把本地台账误判为 console 在途。
    # 注：`pc` 已在上方被 `config.pipeline.console` 顶掉（既有无害遮蔽），故影子
    # 判定必须用一开始就捕获的仓库契约 `repo_pc`。
    if repo_pc.shadow:
        try:
            _dispatch_shadow_execution(config, binding, res, label, repo_pc, artifact_dir)
        except Exception as e:  # noqa: BLE001 — 影子是旁路，任何异常都不该外溢
            log.warning("[%s] 影子派发异常（已忽略，不影响主执行）: %s", label, e)

    # 认领评论（可关）：多会话/多人并行时，这是「谁在做」的机器可读信号——
    # 2026-09-29 与另一会话在同一 issue 上撞车的教训。
    # #6/#12：本轮为自动重派（retry-later / failed / engine_error 三支共用
    # `retry_later_streak` 作「连续自动重派次数」）就不补发——同一 chain 续跑期间
    # 公屏认领评论恒 ≤1 条；真首派（streak == 0）仍恰发一条，bot_marker 不动。
    auto_redispatch = int(getattr(it, "retry_later_streak", 0) or 0) > 0
    if config.pipeline_claim_comment and not auto_redispatch:
        try:
            _gh_post_comment(
                res.kind, binding.repo, res.number,
                f"{config.bot_marker}\n[issue-pipeline] 已认领本 issue 开始处理"
                f"（run {time.strftime('%H:%M:%S')} 起）。"
                "如有并行会话在做同一件事，请在本条下留言，避免重复动工。")
        except Exception as e:
            log.warning("[%s] 认领评论失败（不影响派发）: %s", label, e)
    return {"status": "dispatched", "comment_posted": True}


def _collect_shadow_result(config, binding, artifact_dir: Path, label: str) -> None:
    """影子副本回收（放量迁移首阶）：轮询 shadow 执行的终态，落结果供对账。

    **只读语义**：仅记录 console 侧执行的成败/耗时到 shadow-result.json；绝不写
    台账、不发评论、不动本地主执行的任何状态。终态落定后删 shadow-exec.json 锚
    （避免每轮重复轮询）。
    """
    import time as _time

    anchor_file = artifact_dir / SHADOW_EXEC_RECORD
    if not anchor_file.exists():
        return
    try:
        rec = json.loads(anchor_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        anchor_file.unlink(missing_ok=True)
        return
    eid = rec.get("execution_id")
    if not eid:
        anchor_file.unlink(missing_ok=True)
        return
    try:
        from . import console_exec as _ce
        client = _ce.engine_client_from_config(config)
        info = client.get_execution(eid)
    except Exception as e:  # noqa: BLE001 — 影子回收失败绝不影响主流程
        log.debug("[%s] 影子回收跳过（%s）", label, e)
        return
    status = str((info or {}).get("status") or "")
    if status in ("queued", "running", "pending", ""):
        return  # 仍在途，下轮再查
    out = {
        "execution_id": eid, "flow_id": rec.get("flow_id"), "run_id": rec.get("run_id"),
        "shadow_status": status,
        "start_time": (info or {}).get("start_time"),
        "end_time": (info or {}).get("end_time"),
        "error": (info or {}).get("error"),
        "collected_at": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    try:
        (artifact_dir / SHADOW_RESULT_RECORD).write_text(
            json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("[%s] 写影子结果失败: %s", label, e)
    anchor_file.unlink(missing_ok=True)
    log.info("[%s] 影子副本终态=%s（已落 shadow-result.json 供对账）", label, status)


def _reap_pipelines(config, state, bindings) -> int:
    """收尸：对所有 in_flight 条目判「run 是否已结束」，结束则补回评/状态/看板。

    返回本轮收尾的条数。判定：
    - run 已结束且人工 reopen 晚于本次派发 → 结论作废：清在途锚但不置 processed（#7）；
    - run.lock 的 pid 活着且未超 pipeline_timeout_secs → 还在跑，跳过；
    - pid 活着但超时 → killpg（bridge 是 start_new_session，整组清）+ engine_error；
    - pid 死了 → bridge 已退出：读台账该 issue 最晚一条记录拿 status/comment_posted；
      台账缺失（bridge 极早崩溃）按 engine_error 处理。
    """
    import os as _os
    import signal as _signal
    import time as _time

    finalized = 0
    now = _time.time()
    for binding in bindings:
        rs = state.repo(binding.repo_slug)
        for key, it in list(rs.items.items()):
            since = getattr(it, "in_flight_since", None)
            if not since:
                continue
            label = f"{binding.repo} #{key}"
            slug = binding.repo.split("/")[-1]
            artifact_dir = Path(f"~/.issue-keeper/pipeline/{slug}-{key}").expanduser()
            lock = artifact_dir / PIPELINE_LOCK_NAME

            # 影子副本回收（放量迁移首阶·只读）：有 shadow 锚就查一次终态落对账结果。
            # 纯旁路——不写台账、不改主执行状态，失败静默。
            try:
                _collect_shadow_result(config, binding, artifact_dir, label)
            except Exception as e:  # noqa: BLE001
                log.debug("[%s] 影子回收异常（忽略）: %s", label, e)

            # #7 窗口内人工 reopen：本次 run 结论作废（判定分支在下方分流之后——
            # 「还在跑」要先放行，等 run 结束再作废）。console 分流在轮询到终态时
            # 会发终态回评/落台账行，故先把结论算出来传进去抑制。
            void_run = bool(getattr(it, "manual_reopen_at", None)
                            and it.manual_reopen_at > since)

            console_rec = _console_exec_record(artifact_dir)
            if console_rec is not None:
                # engine=v2-console 在途：轮询 execution，终态则落账后走共享收尾
                row = _reap_console_execution(config, binding, it, key, label,
                                              artifact_dir, console_rec, now,
                                              void=void_run)
                if row is None:
                    continue  # 仍在途（本轮无终态/console 不可达）
                rec = row
                status = str(rec.get("status") or "engine_error")
                posted = bool(rec.get("comment_posted"))
                err = str(rec.get("error") or "")
                node_exhausted = False
                holder = None          # console 无本地 pid
                timed_out = False      # 超时语义由 zombie 判定承担
            else:
                holder = _lock_holder(lock)
                timed_out = (now - since) > config.pipeline_timeout_secs
                if holder is not None and not timed_out:
                    continue  # 还在跑
                if holder is not None and timed_out:
                    try:
                        _os.killpg(_os.getpgid(holder), _signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                    lock.unlink(missing_ok=True)
                    log.error("[%s] pipeline 超时（%ss），进程组已清",
                              label, config.pipeline_timeout_secs)

                rec = _latest_pipeline_record(binding.repo, int(key.split(":")[-1]), since) or {}
                status = rec.get("status") or "engine_error"
                posted = bool(rec.get("comment_posted"))
                err = str(rec.get("error") or "")
                # 宿主终局标记（v2 台账 extra 透传，DESIGN-local-distributed-host §5）：
                # 节点重试耗尽 / run 预算墙——宿主内已烧满重试，keeper 不再自动重派。
                node_exhausted = bool(rec.get("node_retry_exhausted"))
                if holder is None and not rec:
                    status, err = "engine_error", "bridge 进程已退出且未写台账"
                    # 代记台账行：连续 engine_error 计数跨尝试可用（否则永不封顶）
                    _append_pipeline_record(binding.repo, int(key.split(":")[-1]),
                                            {"status": "engine_error", "error": err,
                                             "comment_posted": False})
                if timed_out and not err:
                    err = f"超时（{config.pipeline_timeout_secs}s），进程组已清"
            number = int(key.split(":")[-1])
            kind = "pr" if key.startswith("pr:") else "issue"

            # ── 人工 reopen 晚于本次派发：run 结论作废，直接重派（#7）────────
            # 收尾只在轮首发生一次，run 结束 → 收尾之间有实测中位 533s 的窗口
            # （轮内同步 agent 调用可拉到 >1h）。人工在窗口内 reopen 表达的是
            # 「按最新内容重跑」，此时条目 processed=False 但 in_flight_since
            # 仍在——若照常 processed=True，这次 reopen 就被静默吃掉（#100/#97/
            # #107 被反复 re-screen 的痕迹）。清在途锚但不置 processed →
            # run_once 随后的扫仓当轮直接重派（不消费首响、不吃退避）。
            # 到此为止 run 已确证结束（在跑的上面已 continue），console 分流的
            # 终态回评/台账行也已由 `void=` 抑制——不留「结论作废」的痕迹。
            if void_run:
                # 本次派发的产物锚要清干净，否则重派会被 `_pipeline_in_flight` 挡住
                for anchor in (artifact_dir / CONSOLE_EXEC_RECORD, lock,
                               artifact_dir.parent / GLOBAL_LOCK_NAME):
                    anchor.unlink(missing_ok=True)
                it.in_flight_since = None
                it.manual_reopen_at = None
                it.retry_after = None
                it.retry_later_streak = 0
                finalized += 1
                log.info("[%s] 窗口内人工 reopen 晚于本次派发，run(%s) 结论作废，直接重派",
                         label, status)
                continue

            # ── 跨渠道读回校验（recursive#2）：台账说没回评，先去目标渠道核实——
            # bridge 在「评论已发出」与「台账落盘」之间崩溃、或旧版台账键漏记
            # （#31/#32 的 comment_posted=null），不能把已回评误报成未发出。
            if not posted and _channel_reply_posted(
                    binding.repo, number, since, config.bot_marker, kind):
                posted = True
                log.info("[%s] 台账漏记回评，渠道读回确认已发出（status=%s）", label, status)

            # ── retry-later / engine_error 自动重试（2026-10-01）──────────
            # 环境性失败（磁盘守卫）与引擎级崩溃不消费首响：清在途标记与锁，
            # 退避到期后由 `_process_resource` 的 retry_after 闸放行重派
            # （keeper 是「先收尸再扫仓」，不写截止就是同轮立刻再派）。
            # engine_error 连续 2 次才升级人工（防系统性崩溃
            # 刷跑）；daily-limit 在派发路径兜底总量。兜底评论仅在升级时发。
            if status == "retry-later":
                delay = _arm_auto_redispatch(it, config, now, lock=lock, backoff=True)
                finalized += 1
                log.info("[%s] pipeline retry-later（%s）——不消费，第 %d 次，%.0fs 后重派",
                         label, err[:80], it.retry_later_streak, delay)
                continue
            # ── 失败类（failed/guarded/partial）自动重试与连击升级（#8）──────
            # failed 是内容性失败的真实终态（worktree 已保全），此前直接落进
            # 无条件收尾 processed=True → 只能人工 reopen。现在：首败自动重派
            # 一轮（带一个 poll 周期的退避——run 本身 1-4h，按行龄退避等于没退避），
            # 额度耗尽后近 12h 连击 ≥2 即升级人工（评论 + 标签）。
            # guarded/partial 单次仍是终态（自身回评），只有连击才升级。
            if status in _FAILURE_STATUSES:
                n_fail = _consecutive_failures(binding.repo, number)
                budget = max(0, int(getattr(config, "failed_auto_retry", 1)))
                if status in _RETRY_STATUSES and n_fail <= budget:
                    _arm_auto_redispatch(it, config, now, lock=lock)
                    finalized += 1
                    log.warning("[%s] failed 自动重试（连续第 %d 次，退避 %ds）：%s",
                                label, n_fail, int(config.poll_interval_secs), err[:80])
                    continue
                if n_fail >= 2 and n_fail > budget:
                    # 升级评论无条件发（console 路径可能已有终态回评，语义不同：
                    # 一条终态说明、一条升级求助）；marker 必带（防循环第一层）。
                    body = (f"{config.bot_marker}\n[issue-pipeline] 本 issue 近 12 小时内连续 "
                            f"{n_fail} 次失败（status={status}），已超过自动重试额度"
                            f"（failed_auto_retry={budget}），升级人工处理"
                            f"（标签 {config.pipeline_needs_human_label}）。"
                            f"最近一次原因：{_sanitize_public_comment(err or '未记录')[:280]}")
                    try:
                        _gh_post_comment(kind, binding.repo, number, body)
                    except Exception as e:
                        log.error("[%s] 升级评论发送失败: %s", label, e)
                    _gh_add_label(kind, binding.repo, number,
                                  config.pipeline_needs_human_label)
                    posted = True     # 挡住下方兜底回评：升级评论恰一条
                    log.warning("[%s] failed/guarded/partial 连续 %d 次，升级人工", label, n_fail)
                # 单次 guarded/partial（含 budget=0 的单次 failed）→ 落回通用收尾
            if status == "engine_error" and not timed_out:
                if not posted:
                    # 宿主终局标记优先（DESIGN §5 D6）：node_retry_exhausted 表示宿主
                    # 已在节点级烧满重试/预算墙才判 engine_error——keeper 再自动重派
                    # 一轮（implement 1-2h）纯浪费，跳过连击计数直接走下方升级。
                    # 普通崩溃（无标记）才吃自动重试额度。
                    if node_exhausted:
                        log.warning("[%s] node_retry_exhausted（宿主已耗尽节点重试），"
                                    "跳过自动重派直接升级：%s", label, err[:80])
                    else:
                        # 台账终态行（bridge _finish / reaper 代记）在计数**之前**已落，
                        # 尾部连续数已含本次——不能再 +1，否则首败即 2 直接升级、
                        # 重试分支永不可达（2026-10-01 实证：日志 0 次自动重试/24 次
                        # 升级；语义：首败 trailing=1 → 重试，二连 trailing=2 → 升级）。
                        n_err = _consecutive_engine_errors(binding.repo, number)
                        if n_err < 2:
                            delay = _arm_auto_redispatch(it, config, now, lock=lock)
                            finalized += 1
                            log.info("[%s] engine_error 自动重试（连续第 %d 次，退避 %ds）：%s",
                                     label, n_err, int(delay), err[:80])
                            continue
                        log.warning("[%s] engine_error 连续 %d 次，升级人工", label, n_err)
                        _escalate_engine_error(config, kind, binding.repo, number, n_err, err, label)
                        posted = True
                else:
                    # 已有终态回评（bridge/reaper 已发）：**仍按连击判定升级人工**。
                    # failed 分支先例：「升级评论无条件发（语义不同：一条终态说明、
                    # 一条升级求助）」。此前 `not posted` 闸让这类 engine_error 连
                    # 升级分支都不进（既无评论也无标签）→ 人工队列在 GitHub 上不可见，
                    # 值守/看板都发现不了（recursive#86 / argusai#13 实证）。
                    n_err = _consecutive_engine_errors(binding.repo, number)
                    if n_err >= 2:
                        log.warning("[%s] engine_error 连续 %d 次（已有终态回评），仍升级人工",
                                    label, n_err)
                        _escalate_engine_error(config, kind, binding.repo, number, n_err, err, label)

            # ── 收尾（与旧同步路径同一套语义）─────────────────────────
            lock.unlink(missing_ok=True)  # 收尾即清锁（dead 路径 _lock_holder 已清，kill 路径在这补）
            wip_note = ""
            commit_note = ""
            gate_ctx = ""
            if status == "engine_error" and not key.startswith("pr:"):
                # 引擎异常终止：worktree 可能留着无 journal 的半成品（#33/#51 实证），
                # 就地快照成 wip 提交防丢，兜底回评里告知位置。
                worktree_dir = (Path(binding.cwd) / ".worktrees" / f"issue-{number}"
                                if binding.cwd else None)
                if worktree_dir is not None:
                    wip_note = _snapshot_worktree_wip(worktree_dir, f"issue-{number}")
                # #4：gate 名/命令/cwd 来自台账与 dispatch.json（容错缺失，不影响兜底回评）
                gate_failed = str(rec.get("gate_failed") or "").strip()
                if gate_failed:
                    gate_ctx += f"gate={gate_failed}；"
                try:
                    dispatch = json.loads(
                        (artifact_dir / "dispatch.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    dispatch = {}
                cmd = str(dispatch.get("test_command") or "").strip()
                wt_path = (dispatch.get("worktree_dir") or
                           (str(worktree_dir) if worktree_dir else ""))
                exists = "存在" if wt_path and Path(wt_path).is_dir() else "不存在"
                # #4 评论区：管线分支上已有的提交也报位置——recursive#67 实证
                # 「工作已提交甚至已推送、只有回评崩了」的情况，人工不该去 branch -r 里捞
                commit_note = _pipeline_commit_note(
                    worktree_dir, str(dispatch.get("base_branch") or ""))
                # #4：无论有无脏改动都留一条日志，目录不存在这条路径不再静默
                log.warning("[%s] engine_error 收尾：worktree 目录%s%s%s", label, exists,
                            f"；{wip_note}" if wip_note else "",
                            f"；{commit_note}" if commit_note else "")
                gate_ctx += f"cmd={cmd or '未知'}；worktree={wt_path or '未知'}（{exists}）"
            if not posted:
                if status == "engine_error":
                    if node_exhausted:
                        # 升级留痕：让人工一眼看出为何没自动重派（宿主已判终局）
                        reason = "宿主已耗尽节点内重试（node_retry_exhausted），keeper 不自动重派"
                    else:
                        reason = "管线引擎异常终止（未发出回评）"
                else:
                    reason = f"管线终态，但未确认发出回评"
                reason += f"（status={status}）"
                if gate_ctx:
                    reason += f"；{gate_ctx}"
                if err:
                    # #4：尾部截断——多层异常链的最内层（FileNotFoundError 路径）在链尾
                    err_tail = err[-280:]
                    if len(err) > len(err_tail):
                        err_tail = "..." + err_tail
                    reason += f"：{err_tail}"
                if wip_note:
                    reason += f"；{wip_note}"
                if commit_note:
                    reason += f"；{commit_note}"
                try:
                    _gh_post_comment(
                        kind, binding.repo, number,
                        f"{config.bot_marker}\n[issue-pipeline] {_sanitize_public_comment(reason)}，请人工查看。")
                except Exception as e:
                    log.error("[%s] 兜底回评失败: %s", label, e)
            it.processed = True
            it.in_flight_since = None
            it.manual_reopen_at = None  # #7：在途窗口已结束，人工 reopen 标记失效
            it.retry_after = None      # #8：收尾即销掉陈旧退避，不留幽灵字段
            it.retry_later_streak = 0  # #6：终态收尾即清零，下次故障重新 7min 起算
            finalized += 1
            if status == "blocked" and not key.startswith("pr:"):
                body = ""
                try:
                    body = (artifact_dir / "00-issue.md").read_text(encoding="utf-8")
                except OSError:
                    pass
                deps = [n for n in _extract_issue_refs(body) if n != int(key)]
                if deps:
                    it.wakeup_deps = deps
                    log.info("[%s] blocked，监视依赖 %s 就绪后唤醒", label, deps)
            _move_board_after_reap(binding, status, posted, kind, number, label)
            log.info("[%s] pipeline 完成: status=%s posted=%s（已收尾）", label, status, posted)
    return finalized


def _move_board_after_reap(binding: RepoBinding, status: str, posted: bool,
                           kind: str, number: int, label: str) -> None:
    """收尸后的看板收尾（recursive#2：原 status_for_board 算完没用、日志虚报「看板→todo」）。

    仅对支持状态机的 source（internal 看板）生效；GitHub 是 issue 唯一权威，
    其工作台阶段由 workbench 从台账/metrics 派生，不经这里。
    映射：回评已发出且非 blocked → review（issue 上有说明，等人确认）；blocked →
    todo（依赖闭合后自动唤醒重派）；未回评（引擎异常/护栏等）→ todo（兜底回评已
    指到人工）。
    """
    to_status = "review" if (posted and status != "blocked") else "todo"
    try:
        src = _ensure_source(binding, {})
        if not _supports_status(src):
            return
        res = Resource(kind=kind, number=number, title="", body="", state="open",
                       labels=[], author="", created_at="", updated_at="",
                       status="", actor_type="agent")
        _safe_move(src, binding, res, to_status, actor="issue-keeper-agent",
                   actor_type="agent", comment=f"pipeline 收尾 status={status}")
    except Exception as e:
        log.warning("[%s] 看板收尾失败: %s", label, e)



