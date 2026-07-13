"""配置加载与校验。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .screener import ScreenerConfig


@dataclass
class RepoBinding:
    repo: str
    profile: str
    labels: list[str] = field(default_factory=list)
    monitor_prs: bool = False
    pr_labels: list[str] = field(default_factory=list)
    source: str = "github_cli"
    # agent 可见身份标签。出现在 agent 发出的每条评论正文前缀里：
    #   [issue-keeper:<agent_label>]
    # 也用于跨项目时让别的仓库识别"是哪个 agent 来的"。默认 fallback 到 agent_from_user。
    agent_label: str = ""
    # github_token source 用的 PAT。支持 ${ENV_VAR} 展开。空则回退到环境变量 GITHUB_TOKEN/GH_TOKEN。
    github_token: str = ""
    # internal source 用的 SQLite 路径。支持 ${ENV_VAR} 展开。空则用全局默认 ~/.issue-keeper/internal.db。
    internal_db: str = ""
    # agent 工作目录（agentproc --cwd）。agent 会以这个目录为上下文跑。
    # 对 GitHub source 应为仓库代码本地路径；对 internal source 是项目代码路径。
    cwd: str = ""
    # 传给 agent 子进程的额外 env（API key、模型等）。支持 ${VAR} 插值。
    env: dict[str, str] = field(default_factory=dict)
    # 该仓库专用的 review agent（覆盖全局 default_review_agent）。
    # 人提的 issue 处理完后由此 agent review。
    review_agent: str = ""
    poll_interval_secs: int | None = None
    session_prefix: str = "issue-keeper"
    timeout_secs: int | None = None

    def effective_poll_interval(self, default: int) -> int:
        return self.poll_interval_secs if self.poll_interval_secs is not None else default

    def effective_timeout(self, default: int) -> int:
        return self.timeout_secs if self.timeout_secs is not None else default

    def effective_review_agent(self, global_default: str) -> str:
        """该仓库的 review agent。优先 binding.review_agent，否则用全局默认。"""
        return self.review_agent or global_default

    @property
    def repo_slug(self) -> str:
        """repo 标识里非法字符替换为 -，用于 session id / 状态 key。"""
        return self.repo.replace("/", "-").replace(":", "-")


@dataclass
class Config:
    poll_interval_secs: int = 300
    state_file: Path = Path("~/.issue-keeper/state.json")
    bot_marker: str = "<!-- issue-keeper-bot -->"
    default_timeout_secs: int = 600
    agent_from_user: str = "issue-keeper"
    # 默认 review agent：人提的 issue 处理完后，由这个 agent 先 review。
    # 为空则人提的 issue 处理完停在 review 状态等人接手。
    default_review_agent: str = ""
    screener: ScreenerConfig = field(default_factory=lambda: ScreenerConfig(
        enabled=False, provider="openai", api_key=None, base_url=None, model=None,
        on_unsafe="skip", max_chars=8000,
    ))
    repos: list[RepoBinding] = field(default_factory=list)

    @property
    def state_path(self) -> Path:
        return self.state_file.expanduser()


def _expand_path(v: Any) -> Path:
    return Path(v).expanduser()


def _expand_env(value: Any) -> str:
    """展开字符串里的 ${VAR}。其他类型先转 str 再展开。"""
    s = str(value)
    for k, mv in os.environ.items():
        s = s.replace(f"${{{k}}}", mv)
    return s


def _load_screener(raw: dict[str, Any]) -> ScreenerConfig:
    """解析 screener 段。enabled 默认 None（未声明即报错），强制用户显式选择。"""
    enabled_raw = raw.get("enabled")
    if enabled_raw is None:
        raise ValueError(
            "必须显式配置 screener.enabled（true 启用安全过滤；false 明确放行）。"
            "不配置 screener 段同样会报错——这是 fail-safe 设计。"
        )
    enabled = bool(enabled_raw)

    on_unsafe = (raw.get("on_unsafe") or "skip").strip()
    if on_unsafe not in ("skip", "comment"):
        raise ValueError("screener.on_unsafe 只能是 'skip' 或 'comment'")

    provider = (raw.get("provider") or "openai").strip().lower()
    if provider not in ("openai", "anthropic"):
        raise ValueError("screener.provider 只能是 'openai' 或 'anthropic'")

    api_key = _expand_env(raw.get("api_key") or "").strip() or None
    base_url = _expand_env(raw.get("base_url") or "").strip() or None
    model = _expand_env(raw.get("model") or "").strip() or None

    creds_profile = (raw.get("credentials_from_profile") or "").strip()
    if creds_profile:
        from .screener import _load_credentials_from_profile
        creds = _load_credentials_from_profile(creds_profile)
        # profile 推断出的 provider / api_key / base_url / model 作为兜底，显式配置优先
        provider = provider if raw.get("provider") else creds["provider"]
        api_key = api_key or creds["api_key"]
        base_url = base_url or creds["base_url"]
        model = model or creds["model"]

    max_chars = int(raw.get("max_chars", 8000))

    cfg = ScreenerConfig(
        enabled=enabled,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        on_unsafe=on_unsafe,
        max_chars=max_chars,
    )

    if cfg.enabled:
        missing = [k for k in ("api_key", "base_url", "model") if not getattr(cfg, k)]
        if missing:
            raise ValueError(
                f"screener.enabled=true 但缺少: {', '.join(missing)}。"
                f"请配置 screener.api_key/base_url/model，或 screener.credentials_from_profile。"
            )
    return cfg


def load_config(path: str | os.PathLike) -> Config:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    repos: list[RepoBinding] = []
    for item in raw.get("repos", []) or []:
        if not isinstance(item, dict):
            raise ValueError(f"repos 条目必须是映射: {item!r}")
        repo = (item.get("repo") or "").strip()
        profile = (item.get("profile") or "").strip()
        if not repo or not profile:
            raise ValueError("每个 repos 条目必须提供非空的 repo 和 profile")
        repos.append(
            RepoBinding(
                repo=repo,
                profile=profile,
                labels=list(item.get("labels") or []),
                monitor_prs=bool(item.get("monitor_prs", False)),
                pr_labels=list(item.get("pr_labels") or []),
                source=(item.get("source") or "github_cli").strip() or "github_cli",
                agent_label=(item.get("agent_label") or "").strip(),
                github_token=_expand_env(item.get("github_token") or ""),
                internal_db=_expand_env(item.get("internal_db") or ""),
                cwd=os.path.expanduser(_expand_env(item.get("cwd") or "")),
                env={str(k): _expand_env(v) for k, v in (item.get("env") or {}).items()},
                review_agent=(item.get("review_agent") or "").strip(),
                poll_interval_secs=item.get("poll_interval_secs"),
                session_prefix=(item.get("session_prefix") or "issue-keeper").strip() or "issue-keeper",
                timeout_secs=item.get("timeout_secs"),
            )
        )

    state_file = raw.get("state_file") or "~/.issue-keeper/state.json"

    screener_raw = raw.get("screener")
    if screener_raw is None:
        raise ValueError(
            "配置文件缺少 screener 段。issue-keeper 必须显式声明安全过滤策略：\n"
            "  screener:\n"
            "    enabled: true             # 启用过滤（推荐）\n"
            "    credentials_from_profile: issue-keeper-glm\n"
            "  或明确放行（不推荐，仅用于本地调试）：\n"
            "  screener:\n"
            "    enabled: false\n"
        )
    screener = _load_screener(screener_raw)

    cfg = Config(
        poll_interval_secs=int(raw.get("poll_interval_secs", 300)),
        state_file=_expand_path(state_file),
        bot_marker=raw.get("bot_marker", Config.bot_marker),
        default_timeout_secs=int(raw.get("default_timeout_secs", 600)),
        agent_from_user=(raw.get("agent_from_user") or "issue-keeper").strip() or "issue-keeper",
        default_review_agent=(raw.get("default_review_agent") or "").strip(),
        screener=screener,
        repos=repos,
    )

    if cfg.poll_interval_secs <= 0:
        raise ValueError("poll_interval_secs 必须为正整数")
    return cfg
