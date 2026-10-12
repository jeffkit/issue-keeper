#!/usr/bin/env bash
# AGS 沙箱孤儿清查的 launchd 包装器 —— 先加载凭据环境，再跑清查脚本。
#
# 存在理由（2026-10-12 根因定位）：
# `cc.agentstudio.ags-orphan-sweep.plist` **没有** `EnvironmentVariables`，而 launchd
# 给进程的环境是**最小集**。`ags-orphan-sweep.py` 在导入期读
# `os.environ.get("E2B_API_KEY", "")` ⇒ 拿到**空串** ⇒ e2b 客户端
# `AuthenticationException: 401 Unauthorized … [AuthFailure]`。
#
# 后果：该任务**静默死了 14 小时**（日志里连续 28 次 401），期间孤儿沙箱无人清理。
# 更糟的是它**不会自我告警**——只在 launchd 的 stdout/stderr 里堆栈，而没人看。
#
# 实测对照（最小 launchd 环境）：
#   直接跑脚本                  → 401 Unauthorized
#   先 source worker.env 再跑   → 正常（"汇总：实例 0，孤儿击杀 0，保留 0"）
#
# ⚠ 脚本需要**两个**凭据：`E2B_API_KEY` 与 `PLAITA_CONSOLE_ADMIN_API_KEY`
# （后者用于查 plaita execution 是否已终态）。两个都在 `~/.plaita-console/worker.env`。
set -uo pipefail

ENV_FILE="${AGS_ENV_FILE:-$HOME/.plaita-console/worker.env}"
PY="${AGS_PYTHON:-/Users/kong/projects/infra4agent/plaita/.venv/bin/python}"
SCRIPT="${AGS_SCRIPT:-/Users/kong/projects/infra4agent/issue-keeper/flows/ags-orphan-sweep.py}"

if [[ ! -r "$ENV_FILE" ]]; then
    echo "[ags-orphan-sweep] 凭据文件不可读：${ENV_FILE}" >&2
    echo "[ags-orphan-sweep] 期望其中含 E2B_API_KEY 与 PLAITA_CONSOLE_ADMIN_API_KEY" >&2
    exit 78   # EX_CONFIG
fi

# `set -a` 让 source 进来的变量自动导出，供子进程（python）继承。
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# 缺任一凭据就**明确失败**——不要带着空 key 去撞 401，那会伪装成"API 抖动"。
for k in E2B_API_KEY PLAITA_CONSOLE_ADMIN_API_KEY; do
    if [[ -z "${!k:-}" ]]; then
        echo "[ags-orphan-sweep] 凭据缺失：${k}（来自 ${ENV_FILE}）" >&2
        exit 78
    fi
done

exec "$PY" "$SCRIPT" "$@"
