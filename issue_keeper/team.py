"""团队成员数据：哪些 agent 参与协作 + 各自自我介绍。

数据存在 ~/.issue-keeper/team.json，供 dashboard 的「团队成员」页面展示。
不依赖 internal.db——这是一个独立的、关于 agent 阵容的元数据文件。

team.json 结构：
{
  "members": [
    {"project": "issue-keeper", "agent_label": "issue-keeper-agent",
     "cwd": "/Users/.../issue-keeper", "intro": "我是 ..."},
    ...
  ]
}
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_TEAM_PATH = Path.home() / ".issue-keeper" / "team.json"


@dataclass
class TeamMember:
    project: str
    agent_label: str
    cwd: str = ""
    intro: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_team(path: Path | str | None = None) -> list[TeamMember]:
    """读取 team.json，不存在则返回空列表。"""
    p = Path(path) if path else DEFAULT_TEAM_PATH
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    members: list[TeamMember] = []
    for m in raw.get("members", []) or []:
        if not isinstance(m, dict):
            continue
        members.append(
            TeamMember(
                project=str(m.get("project", "")),
                agent_label=str(m.get("agent_label", "")),
                cwd=str(m.get("cwd", "")),
                intro=str(m.get("intro", "")),
            )
        )
    return members


def save_team(members: list[TeamMember], path: Path | str | None = None) -> Path:
    """写入 team.json（含父目录创建）。"""
    p = Path(path) if path else DEFAULT_TEAM_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"members": [m.to_dict() for m in members]}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def sync_from_config(config, path: Path | str | None = None) -> list[TeamMember]:
    """用 config 的 repos 重建花名册；保留已有 intro（按 agent_label 匹配）。

    config 里没有的旧 member 会被丢弃；config 新增的 member intro 为空。
    """
    existing = {m.agent_label: m for m in load_team(path) if m.agent_label}
    members: list[TeamMember] = []
    for b in config.repos:
        label = b.agent_label or config.agent_from_user or "issue-keeper"
        prev = existing.get(label)
        members.append(
            TeamMember(
                project=b.repo,
                agent_label=label,
                cwd=b.cwd or "",
                intro=prev.intro if prev else "",
            )
        )
    save_team(members, path)
    return members


def set_intro(agent_label: str, intro: str, path: Path | str | None = None) -> TeamMember | None:
    """给某个 agent 设置 intro。找不到该 agent 返回 None。"""
    members = load_team(path)
    for m in members:
        if m.agent_label == agent_label:
            m.intro = intro
            save_team(members, path)
            return m
    return None


def to_json(members: list[TeamMember]) -> list[dict[str, Any]]:
    return [m.to_dict() for m in members]


def add_binding_to_config(
    config_path: str | Path,
    *,
    repo: str,
    agent_label: str,
    cwd: str,
    profile: str = "claude-code",
) -> None:
    """往 config.yaml 的 repos 列表追加一个 binding（文本追加，保留注释/anchor）。

    假设 repos 是配置文件最后一个顶层段（issue-keeper 的约定写法）。若文件里
    已定义 `&agent_env` anchor，新 binding 复用它；否则写一份显式 DeepSeek env。
    repo 已存在则抛 ValueError。
    """
    import re
    from pathlib import Path

    p = Path(config_path)
    text = p.read_text(encoding="utf-8")
    if re.search(rf'^\s*-\s*repo:\s*["\']?{re.escape(repo)}["\']?\s*$', text, re.M):
        raise ValueError(f"repo '{repo}' 已存在于 {p}")

    use_anchor = "&agent_env" in text
    if use_anchor:
        env_block = "    env: *agent_env"
    else:
        env_block = (
            "    env:\n"
            "      ANTHROPIC_API_KEY: ${DEEPSEEK_API_KEY}\n"
            '      ANTHROPIC_BASE_URL: "https://api.deepseek.com/anthropic"\n'
            '      CLAUDE_MODEL: "deepseek-chat"'
        )
    block = (
        f'\n  - repo: "{repo}"\n'
        f'    profile: "{profile}"\n'
        f'    source: internal\n'
        f'    agent_label: "{agent_label}"\n'
        f'    cwd: {cwd}\n'
        f'{env_block}\n'
        f'    monitor_prs: false\n'
    )
    if not text.endswith("\n"):
        text += "\n"
    p.write_text(text + block, encoding="utf-8")

