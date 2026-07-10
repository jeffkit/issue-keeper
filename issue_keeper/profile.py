"""解析 bridge profile YAML 并按 P0 协议调用 agent。

P0 协议：bridge 通过 env var 传入输入，handler 向 stdout 写出输出。
输入 env: AGENT_MESSAGE / AGENT_SESSION_ID / AGENT_SESSION_NAME /
         AGENT_FROM_USER / AGENT_CONTEXT_TOKEN
stdout:  可选首行 `AGENT_SESSION:<uuid>`，其余为回复正文。
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROFILES_DIR = Path.home() / ".ilink-hub-bridge" / "profiles"

_RUNTIME_BY_EXT = {
    ".py": ["python3"],
    ".js": ["node"],
    ".mjs": ["node"],
    ".ts": ["npx", "tsx"],
    ".sh": ["bash"],
    ".bash": ["bash"],
    ".rb": ["ruby"],
}


@dataclass
class ProfileEntry:
    """解析后的一条 profile（default profile 对应的执行单元）。"""

    name: str
    kind: str  # "claude-code" | "script" | "command"
    cwd: Path | None
    env: dict[str, str]
    timeout_secs: int | None
    # command 模式
    command: str | None = None
    args: list[str] | None = None
    stdin_mode: str = "none"
    # script 模式
    script: str | None = None


@dataclass
class AgentReply:
    session_id: str | None  # agent 返回的 AGENT_SESSION:<uuid>（若有）
    text: str               # 回复正文


def _resolve_profile_path(profile_name: str) -> Path:
    p = PROFILES_DIR / f"{profile_name}.yaml"
    if not p.exists():
        # 兼容 .yml
        yml = PROFILES_DIR / f"{profile_name}.yml"
        if yml.exists():
            return yml
        raise FileNotFoundError(
            f"找不到 profile YAML: {p}（也在 {PROFILES_DIR} 下未发现 {profile_name}.yml）"
        )
    return p


def load_profile(profile_name: str) -> ProfileEntry:
    """读取 profile YAML，解析出 default profile 对应的执行单元。"""
    path = _resolve_profile_path(profile_name)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profiles: dict[str, Any] = raw.get("profiles") or {}
    if not profiles:
        raise ValueError(f"profile {profile_name} 缺少 profiles 段")

    routing = raw.get("routing") or {}
    default_name = routing.get("default_profile")
    if not default_name:
        # 取第一个 profile 作为默认
        default_name = next(iter(profiles))
    entry_raw = profiles.get(default_name)
    if not entry_raw:
        raise ValueError(f"profile {profile_name} 找不到 default_profile '{default_name}'")

    cwd_raw = entry_raw.get("cwd")
    cwd = Path(cwd_raw).expanduser() if cwd_raw else None

    env = {str(k): str(v) for k, v in (entry_raw.get("env") or {}).items()}

    if entry_raw.get("command"):
        kind = "command"
    elif entry_raw.get("script"):
        kind = "script"
    elif entry_raw.get("type"):
        kind = entry_raw["type"]
    else:
        raise ValueError(f"profile {profile_name}/{default_name} 未提供 type/script/command")

    return ProfileEntry(
        name=default_name,
        kind=kind,
        cwd=cwd,
        env=env,
        timeout_secs=int(entry_raw["timeout_secs"]) if entry_raw.get("timeout_secs") else None,
        command=entry_raw.get("command"),
        args=list(entry_raw.get("args") or []),
        stdin_mode=entry_raw.get("stdin") or "none",
        script=entry_raw.get("script"),
    )


def _build_command(entry: ProfileEntry, message: str, session_id: str) -> list[str]:
    """根据 entry 类型构造要执行的命令。"""
    if entry.kind == "claude-code":
        # 内置类型：交给 ilink-hub-bridge profile claude-code 运行
        bridge_bin = os.environ.get("ILINK_HUB_BRIDGE_BIN", "ilink-hub-bridge")
        return [bridge_bin, "profile", "claude-code"]

    if entry.kind == "script" and entry.script:
        script_path = Path(entry.script).expanduser()
        runtime = _RUNTIME_BY_EXT.get(script_path.suffix.lower())
        if runtime:
            return [*runtime, str(script_path)]
        # 未知扩展名：直接执行（需 chmod +x）
        return [str(script_path)]

    if entry.kind == "command" and entry.command:
        # command + args，替换占位符
        cmd = [entry.command, *entry.args]
        cmd = [
            c.replace("{{MESSAGE}}", message)
              .replace("{{SESSION_ID}}", session_id)
              .replace("{{SESSION_NAME}}", "default")
            for c in cmd
        ]
        # command 可能含空格（如 "claude -p"），用 shlex 拆分首段
        first = shlex.split(cmd[0])
        return [*first, *cmd[1:]]

    raise ValueError(f"无法为 profile kind='{entry.kind}' 构造命令")


def _parse_reply(stdout: str) -> AgentReply:
    """解析 stdout：可选首行 AGENT_SESSION:<uuid>，其余为正文。"""
    if not stdout:
        return AgentReply(session_id=None, text="")
    lines = stdout.splitlines()
    session_id = None
    start = 0
    if lines and lines[0].startswith("AGENT_SESSION:"):
        session_id = lines[0][len("AGENT_SESSION:"):].strip() or None
        start = 1
    text = "\n".join(lines[start:]).strip()
    return AgentReply(session_id=session_id, text=text)


def invoke_agent(
    entry: ProfileEntry,
    message: str,
    session_id: str,
    *,
    from_user: str = "issue-keeper",
    default_timeout: int = 600,
    context_token: str = "issue-keeper",
) -> AgentReply:
    """按 P0 协议调用 agent，返回 AgentReply。"""
    timeout = entry.timeout_secs if entry.timeout_secs else default_timeout

    env = os.environ.copy()
    # P0 输入 env
    env["AGENT_MESSAGE"] = message
    env["AGENT_SESSION_ID"] = session_id or ""
    env["AGENT_SESSION_NAME"] = "default"
    env["AGENT_FROM_USER"] = from_user
    env["AGENT_CONTEXT_TOKEN"] = context_token
    # profile 自定义 env
    env.update(entry.env)

    cmd = _build_command(entry, message, session_id)

    stdin_data = None
    if entry.stdin_mode == "message":
        stdin_data = message

    proc = subprocess.run(
        cmd,
        input=stdin_data,
        capture_output=True,
        text=True,
        cwd=str(entry.cwd) if entry.cwd else None,
        env=env,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"agent 执行失败 (profile kind={entry.kind}): exit={proc.returncode}\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr: {proc.stderr.strip()[:2000]}"
        )
    return _parse_reply(proc.stdout)
