"""回评礼仪化：agent 原始输出 → 可直接发布的 issue 评论。

agent 的最终回复常带工作过程叙述（「我先…然后…现在我将…」）、内部状态
（本地未推送、会话细节）、本机绝对路径，甚至重复段落——原样贴到 issue 上
既难读又可能泄露隐私。发布前做两层处理：

1. sanitize —— 确定性消毒：本机绝对路径 / 密钥赋值模式替换为 [REDACTED-*]。
   无论礼仪化开关与否都执行，是最后的兜底。
2. polish —— 一次廉价 LLM 调用把原文改写成 issue 礼仪的评论（结论先行、
   只留结果与事实、去重）。任何失败（网络/解析/凭据缺失）都降级为
   sanitize 后原样发布——礼仪化是尽力而为，绝不阻塞或吞掉回评。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("issue-keeper.reply")


@dataclass
class ReplyPolishConfig:
    enabled: bool = True
    provider: str = "openai"  # "openai" | "anthropic"
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    # 附加请求体字段（openai 协议；如 {"reasoning_effort": "low"}——glm-5.3-flash
    # 这类始终思考模型不压档位会烧掉 max_tokens 还交不出 content）
    extra_body: dict | None = None
    # 喂给改写调用的最大字符数（超长原文截断——改写是发布把关，不是全文翻译）
    max_chars: int = 16000
    timeout_secs: int = 60
    # 短于此字符数的原文跳过 LLM 改写（只消毒）——短回复没有过程叙述的空间
    min_chars: int = 120


# 确定性消毒规则（与 issue-pipeline flow 的出害口消毒同款）
_REDACTIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"/Users/\S+"), "[REDACTED-PATH]"),
    (re.compile(r"/home/\S+"), "[REDACTED-PATH]"),
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+"), "[REDACTED-SECRET]"),
    # LLM 偶尔把「想执行的命令」原样写进回评（#19 实证：`$(git rev-parse …)` 字面上屏，
    # GitHub 不做命令替换，读者只看到一句假哈希）。反引号包裹的 $() 是回评里的
    # 伪执行签名——行内代码不会是给人跑的脚本，改写成如实说明。
    (re.compile(r"`\$\(([^`]+)\)`"), r"（命令 `\1` 未在发布时执行，以仓库实际状态为准）"),
]


def sanitize(text: str) -> str:
    """确定性消毒：本机绝对路径 / 密钥赋值替换为占位符。"""
    for pat, rep in _REDACTIONS:
        text = pat.sub(rep, text)
    return text


_SYSTEM_PROMPT = (
    "你是 issue-keeper 的回评编辑。输入是一个 AI agent 处理完 GitHub issue/PR 后的"
    "原始输出，请把它改写成一条可以直接发布到该 issue/PR 的公开评论。\n\n"
    "改写规则：\n"
    "1. 结论先行：第一句话说清做了什么/结论是什么（保留 commit hash、分支名、"
    "PR 编号等可核验引用）。\n"
    "2. 只保留结果与事实：根因、方案要点、改动文件、验证/测试结果、后续事项。\n"
    "3. 删除工作过程叙述与元描述（「我先…然后…现在我将…」「让我看看…」"
    "「以下是回复」这类）。\n"
    "4. 原文若有重复段落（同一内容出现两遍），只保留一份。\n"
    "5. 删除不宜公开的内部信息：本机绝对路径、内部状态（如「本地未推送」）、"
    "与结论无关的探索细节。\n"
    "6. 保留原文的技术事实、代码引用（文件:行号）与语言（中文原文→中文输出）。\n"
    "7. markdown、简洁：通常 3~10 句；若原文本身已是合格的 issue 评论，"
    "则只按第 5 条最小修改，不要为改而改。\n\n"
    "只输出评论正文本身，不要任何解释、前后缀或代码围栏。"
)


def _openai_chat_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def _anthropic_messages_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def _call_llm(cfg: ReplyPolishConfig, prompt: str) -> str:
    """一次 LLM 调用（openai 兼容 / anthropic 协议），返回补全文本。失败抛异常。"""
    if cfg.provider == "anthropic":
        url = _anthropic_messages_url(cfg.base_url or "")
        payload = {
            "model": cfg.model,
            "max_tokens": 2048,
            "temperature": 0,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "content-type": "application/json",
            "x-api-key": cfg.api_key or "",
            "anthropic-version": "2023-06-01",
        }
    else:
        url = _openai_chat_url(cfg.base_url or "")
        payload: dict = {
            "model": cfg.model,
            "max_tokens": 2048,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        if cfg.extra_body:
            payload.update(cfg.extra_body)
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {cfg.api_key or ''}",
        }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers,
    )
    with urllib.request.urlopen(req, timeout=cfg.timeout_secs) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if cfg.provider == "anthropic":
        blocks = data.get("content") or []
        return "".join(
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
    choices = data.get("choices") or []
    if not choices:
        return ""
    return str((choices[0].get("message") or {}).get("content") or "")


_FENCE_RE = re.compile(r"^```[\w-]*\n(.*)\n```\s*$", re.S)


def _strip_code_fence(text: str) -> str:
    """模型偶尔会把整条评论包进 ``` 围栏——剥掉。"""
    m = _FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text.strip()


def polish(text: str, cfg: ReplyPolishConfig, *, source_label: str = "") -> str:
    """发布前处理入口：消毒 +（可选）LLM 礼仪化改写。永不抛异常。"""
    safe_text = sanitize(text)
    if not safe_text.strip():
        return safe_text
    if not cfg.enabled:
        return safe_text
    if len(safe_text) < cfg.min_chars:
        log.info("[%s] reply_polish 原文 %d 字符 < %d，跳过改写（发布消毒原文）",
                 source_label, len(safe_text), cfg.min_chars)
        return safe_text
    if not (cfg.api_key and cfg.base_url and cfg.model):
        log.warning("[%s] reply_polish 凭据不完整，跳过改写（已消毒原文）", source_label)
        return safe_text

    prompt = safe_text
    if len(prompt) > cfg.max_chars:
        prompt = prompt[: cfg.max_chars] + "\n…[原文过长已截断]"
    try:
        out = _call_llm(cfg, prompt)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("[%s] reply_polish LLM 调用失败，发布消毒原文: %s", source_label, e)
        return safe_text
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        log.warning("[%s] reply_polish 响应解析失败，发布消毒原文: %s", source_label, e)
        return safe_text

    out = _strip_code_fence(out or "")
    if not out:
        log.warning("[%s] reply_polish 返回空改写，发布消毒原文", source_label)
        return safe_text
    # 改写结果也可能复述原文里的路径/密钥——再过一遍消毒
    log.info("[%s] reply_polish 走 LLM 改写（%d → %d 字符）", source_label, len(safe_text), len(out))
    return sanitize(out)
