"""团队成员 + 项目绑定数据。

项目绑定（repo/agent_label/cwd/profile/source/monitor_prs/env）和 agent 自我介绍
都存在 internal.db 的 projects 表——这是 issue-keeper 的**单一配置源**。keeper 每轮
`load_config` 从这里读绑定；dashboard 从这里读项目列表和团队介绍。

config.yaml 只保留引导/全局旋钮/screener/全局 agent_env 模板（含密钥 ${VAR} 引用）。
`team import` 用于一次性把旧版 config.yaml 的 `repos:` 段迁进 db。

历史：介绍曾用 ~/.issue-keeper/team.json 单独存。import 时若发现旧 team.json，会一次性
把 intro 迁进 db，然后把 team.json 改名为 team.json.migrated 保留备份。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

# 仅用于读取旧 team.json 做一次性迁移
_LEGACY_TEAM_PATH = Path.home() / ".issue-keeper" / "team.json"


@dataclass
class TeamMember:
    project: str
    agent_label: str
    cwd: str = ""
    intro: str = ""
    profile: str = ""
    source: str = "internal"
    monitor_prs: bool = False
    role: str = "agent"   # 'agent' | 'keeper'

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _source(db_path: str | None = None):
    """构造一个指向指定 db 的 InternalSource。"""
    from .config import RepoBinding
    from .sources.internal import DEFAULT_DB, InternalSource

    binding = RepoBinding(
        repo="", profile="", source="internal",
        agent_label="team-cli", internal_db=db_path or str(DEFAULT_DB),
    )
    return InternalSource(binding=binding)


def load_team(db_path: str | None = None) -> list[TeamMember]:
    """从 db 的 projects 表读全部团队成员。"""
    src = _source(db_path)
    return [
        TeamMember(
            project=m["name"],
            agent_label=m["agent_label"],
            cwd=m["cwd"],
            intro=m["intro"],
            profile=m["profile"],
            source=m["source"],
            monitor_prs=bool(m.get("monitor_prs", False)),
            role=m.get("role") or "agent",
        )
        for m in src.list_projects_meta()
    ]


def add_project_to_db(
    db_path: str | None = None, *,
    repo: str, agent_label: str, cwd: str,
    profile: str = "claude-code", source: str = "internal",
    github_token: str = "",
    monitor_prs: bool = False, env: dict[str, str] | None = None,
    role: str = "agent",
) -> None:
    """新增/更新一个项目绑定到 db（on conflict 覆盖绑定字段、保留 intro/role）。"""
    src = _source(db_path)
    src.upsert_project(
        name=repo, agent_label=agent_label, cwd=cwd,
        profile=profile, source=source, github_token=github_token,
        monitor_prs=monitor_prs, env=env, role=role,
    )


def set_role(agent_label: str, role: str, db_path: str | None = None) -> str | None:
    """给某个 agent 设置 role（按 agent_label 找项目）。返回被更新的 project name，未命中返回 None。"""
    src = _source(db_path)
    return src.set_project_role_by_label(agent_label, role)


def remove_project_from_db(db_path: str | None = None, *, repo: str) -> bool:
    """从 db 删除一个项目绑定（不删它的 issue）。"""
    src = _source(db_path)
    return src.delete_project(repo)


def import_from_config(old_config_path: str | Path, db_path: str | None = None) -> list[TeamMember]:
    """一次性迁移：把旧版 config.yaml 的 `repos:` 段灌进 db projects 表。

    旧 repos 条目里的 env（通常是同一份 anchor 复制）会被原样存进各项目的 env 列
    （${VAR} 占位保留，不展开入库——展开在 load_config 时做）。
    若发现旧 team.json，把 intro 一并迁进 db。
    """
    p = Path(old_config_path).expanduser()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    src = _source(db_path)
    for item in raw.get("repos", []) or []:
        if not isinstance(item, dict):
            continue
        repo = (item.get("repo") or "").strip()
        if not repo:
            continue
        env_dict = {str(k): str(v) for k, v in (item.get("env") or {}).items()}
        src.upsert_project(
            name=repo,
            profile=(item.get("profile") or "claude-code").strip(),
            source=(item.get("source") or "internal").strip() or "internal",
            agent_label=(item.get("agent_label") or "").strip(),
            github_token=str(item.get("github_token") or ""),
            cwd=os.path.expanduser(str(item.get("cwd") or "")),
            monitor_prs=bool(item.get("monitor_prs", False)),
            env=env_dict or None,
        )
    _migrate_legacy_teamjson(src)
    return load_team(db_path)


def _migrate_legacy_teamjson(src) -> None:
    """一次性迁移：旧 team.json 的 intro 灌进 db（仅当 db 对应项目 intro 为空），然后改名备份。"""
    if not _LEGACY_TEAM_PATH.exists():
        return
    try:
        raw = json.loads(_LEGACY_TEAM_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    existing = {m["name"]: m for m in src.list_projects_meta()}
    for m in raw.get("members", []) or []:
        if not isinstance(m, dict):
            continue
        name = m.get("project") or ""
        intro = m.get("intro") or ""
        if name and intro and existing.get(name, {}).get("intro", "") == "":
            src.set_project_intro(name, intro)
    backup = _LEGACY_TEAM_PATH.with_suffix(".json.migrated")
    try:
        _LEGACY_TEAM_PATH.rename(backup)
    except OSError:
        pass


def set_intro(agent_label: str, intro: str, db_path: str | None = None) -> str | None:
    """给某个 agent 设置 intro（按 agent_label 找项目）。返回被更新的 project name，未命中返回 None。"""
    src = _source(db_path)
    return src.set_project_intro_by_label(agent_label, intro)
