"""配置加载与校验。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class RepoBinding:
    repo: str
    profile: str
    labels: list[str] = field(default_factory=list)
    poll_interval_secs: int | None = None
    session_prefix: str = "issue-keeper"
    timeout_secs: int | None = None

    def effective_poll_interval(self, default: int) -> int:
        return self.poll_interval_secs if self.poll_interval_secs is not None else default

    def effective_timeout(self, default: int) -> int:
        return self.timeout_secs if self.timeout_secs is not None else default

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
    repos: list[RepoBinding] = field(default_factory=list)

    @property
    def state_path(self) -> Path:
        return self.state_file.expanduser()


def _expand_path(v: Any) -> Path:
    return Path(v).expanduser()


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
                poll_interval_secs=item.get("poll_interval_secs"),
                session_prefix=(item.get("session_prefix") or "issue-keeper").strip() or "issue-keeper",
                timeout_secs=item.get("timeout_secs"),
            )
        )

    state_file = raw.get("state_file") or "~/.issue-keeper/state.json"

    cfg = Config(
        poll_interval_secs=int(raw.get("poll_interval_secs", 300)),
        state_file=_expand_path(state_file),
        bot_marker=raw.get("bot_marker", Config.bot_marker),
        default_timeout_secs=int(raw.get("default_timeout_secs", 600)),
        agent_from_user=(raw.get("agent_from_user") or "issue-keeper").strip() or "issue-keeper",
        repos=repos,
    )

    if cfg.poll_interval_secs <= 0:
        raise ValueError("poll_interval_secs 必须为正整数")
    return cfg
