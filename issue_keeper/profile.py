"""通过 agentproc CLI 调用 agent。

issue-keeper 不再直接读 bridge profile YAML，而是调 `agentproc` CLI，
由 agentproc 负责加载 profile（hub 名或本地路径）、驱动 agent 子进程、
解析 AgentProc 协议输出。

profile 字段语义（binding.profile）：
- 简短名字（如 "deepseek"、"claude-code"）→ 当作 hub profile 名，
  agentproc 自动从 hub 拉取并缓存。等价于 `agentproc hub run <name>`。
- 文件路径（如 "./profiles/glm.yaml" 或 "/abs/path/x.yaml"）→ 当作本地
  profile 路径，等价于 `agentproc --profile <path>`。

凭据：通过 binding.env 注入子进程环境，支持 ${VAR} 插值。profile 自身的
env_allowlist 决定哪些 env 真正传到 agent 子进程（由 agentproc/profile 控制）。
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("issue-keeper.profile")

AGENTPROC_BIN = os.environ.get("AGENTPROC_BIN", "agentproc")


@dataclass
class ProfileEntry:
    """一个 binding 对应的 agent 调用配置。

    issue-keeper 不再解析 profile YAML 内容——那交给 agentproc。
    这里只保留 issue-keeper 调 agentproc 时需要的参数。
    """

    name: str  # 原始 profile 字段值（hub 名或路径），用于日志/缓存 key
    is_hub: bool  # True=hub 名，False=本地路径
    cwd: str | None  # agent 工作目录（binding.cwd）
    env: dict[str, str] = field(default_factory=dict)  # 额外 env（支持 ${VAR} 已展开）
    timeout_secs: int | None = None


@dataclass
class AgentReply:
    session_id: str | None  # agent 返回的 AGENT_SESSION:<uuid>（若有）
    text: str               # 回复正文


def _expand_env(value: str) -> str:
    """展开 ${VAR}。"""
    s = str(value)
    for k, mv in os.environ.items():
        s = s.replace(f"${{{k}}}", mv)
    return s


def load_profile(binding) -> ProfileEntry:
    """根据 binding 解析出 ProfileEntry。

    binding.profile 可以是：
      - hub 名（deepseek / claude-code / kimi-code / ...）
      - 本地 .yaml 路径

    binding.cwd 决定 agent 工作目录。
    binding.env 是额外环境变量（${VAR} 已在 config 层展开过，这里直接用）。
    """
    raw = binding.profile.strip()
    is_hub = True
    # 启发式判断：包含路径分隔符或以 . 开头或后缀是 .yaml/.yml，当作本地路径
    if "/" in raw or "\\" in raw or raw.endswith(".yaml") or raw.endswith(".yml"):
        is_hub = False
        p = Path(raw).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"profile 本地路径不存在: {p}")

    return ProfileEntry(
        name=raw,
        is_hub=is_hub,
        cwd=binding.cwd or None,
        env=dict(getattr(binding, "env", None) or {}),
        timeout_secs=getattr(binding, "timeout_secs", None),
    )


def _build_command(entry: ProfileEntry, message: str, session_id: str,
                   *, from_user: str = "issue-keeper",
                   default_timeout: int = 600) -> list[str]:
    """构造 agentproc 调用命令。

    hub 名：agentproc hub run <name> --prompt <msg> [--cwd <path>] [--session <id>] [--from <user>]
    本地：  agentproc --profile <path>   --prompt <msg> [--cwd <path>] [--session <id>] [--from <user>]

    prompt 用 --prompt 直接传。注：agentproc 0.4.0 的 --stdin 实现有 bug
    （报 AGENT_MESSAGE 缺失），故改用 --prompt。

    env 通过 --env KEY=VALUE 显式传。这是 agentproc 0.7.0（wire 0.3）的硬要求：
    新 runner 不再继承父进程全量 env，只传 infra 集 + profile env 块（allowlist
    过滤）+ CLI --env 的 extraEnv；binding.env 里非 allowlist 的变量（如
    ANTHROPIC_BASE_URL）只有走 --env 才能到 agent。--env 在 0.4.0 也支持，冗余但无害。
    """
    if entry.is_hub:
        cmd = [AGENTPROC_BIN, "hub", "run", entry.name]
    else:
        cmd = [AGENTPROC_BIN, "--profile", entry.name]

    cmd += ["--quiet", "--prompt", message]
    if entry.cwd:
        cmd += ["--cwd", entry.cwd]
    if session_id:
        cmd += ["--session", session_id]
    if from_user:
        cmd += ["--from", from_user]
    # binding.env 经 --env 透传给 agent（绕过 0.7.0 的 env_allowlist 过滤）
    for k, v in entry.env.items():
        cmd += ["--env", f"{k}={v}"]
    if entry.timeout_secs:
        cmd += ["--timeout", str(entry.timeout_secs)]
    elif default_timeout:
        cmd += ["--timeout", str(default_timeout)]
    return cmd


def _parse_reply(stdout: str, stderr: str) -> AgentReply:
    """解析 agentproc 输出。

    agentproc 的输出契约（来自 --help）：
      stderr → 协议行（AGENT_PARTIAL:, AGENT_SESSION:, AGENT_ERROR:）
      stdout → 最终回复正文（非协议行）
      最终 session id 印在 stderr 的 `agentproc:session:<id>` 行

    我们用 --quiet 抑制协议行，但 `agentproc:session:<id>` 仍会在 stderr。
    stdout 整体作为回复正文。
    """
    text = stdout.strip()
    session_id = None
    for line in stderr.splitlines():
        # 形如 "agentproc:session:abc-123"
        if line.startswith("agentproc:session:"):
            session_id = line[len("agentproc:session:"):].strip() or None
            break
    return AgentReply(session_id=session_id, text=text)


def invoke_agent(
    entry: ProfileEntry,
    message: str,
    session_id: str,
    *,
    from_user: str = "issue-keeper",
    default_timeout: int = 600,
) -> AgentReply:
    """调 agent，返回 AgentReply。

    子进程的 env = 当前 env + binding.env（凭据等）。
    agentproc/profile 的 env_allowlist 决定哪些真正传到 agent 子进程。
    """
    cmd = _build_command(entry, message, session_id or "",
                        from_user=from_user, default_timeout=default_timeout)
    log.debug("调 agentproc: %s", " ".join(shlex.quote(c) for c in cmd[:6]) + " ...")

    env = os.environ.copy()
    env.update(entry.env)

    try:
        proc = subprocess.run(
            cmd,
            input=None,
            capture_output=True,
            text=True,
            cwd=entry.cwd or None,
            env=env,
            timeout=default_timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"agent 超时 ({default_timeout}s): cmd={' '.join(cmd[:4])}..."
        ) from e

    if proc.returncode != 0:
        raise RuntimeError(
            f"agent 执行失败 (profile={entry.name}): exit={proc.returncode}\n"
            f"cmd: {' '.join(cmd[:6])}...\n"
            f"stderr: {proc.stderr.strip()[:2000]}"
        )

    # 即使 exit=0，agentproc 也可能在 stderr 报错。兼容两种 wire：
    #   0.2 旧协议：`AGENT_ERROR:<msg>`
    #   0.3 新协议：`agentproc:error:<msg>`（NDJSON 事件经 CLI 汇总后的行）
    error_prefix = None
    for line in proc.stderr.splitlines():
        if line.startswith("AGENT_ERROR:") or line.startswith("agentproc:error:"):
            error_prefix = "AGENT_ERROR:" if line.startswith("AGENT_ERROR:") else "agentproc:error:"
            break
    if error_prefix:
        for line in proc.stderr.splitlines():
            if line.startswith(error_prefix):
                log.warning("agent 报错: %s", line)
                # 不 raise——有些 agent 错误仍带部分输出；让 keeper 决定要不要发
                break

    return _parse_reply(proc.stdout, proc.stderr)
