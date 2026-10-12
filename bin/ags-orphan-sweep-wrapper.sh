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

# ⚠ 代理环境变量：本机 shell/worker.env 可能带 `http_proxy=http://127.0.0.1:7897`，
# 而 `NO_PROXY` **不含** tencentags.com。带上它时该主机连不通（实测 `000`），
# 清掉后可通。故此处显式 unset，不依赖调用方环境。
#
# ⚠⚠ 但请注意（2026-10-13 分层实测的结论）：**清掉代理并不能修好本脚本**。
# 同一台机器上，对 `api.ap-guangzhou.tencentags.com`：
#
#   curl                              → 401（HTTP 往返正常）
#   python 裸 ssl + 手写 HTTP/1.1     → 401（正常）
#   python 裸 ssl 握手（含各种 ALPN）  → 成功，证书链有效（DigiCert/腾讯）
#   python **httpx / pyqwest**        → ConnectError: SSL: UNEXPECTED_EOF_WHILE_READING
#
# ⇒ 网络、DNS、TCP、TLS、服务端**都正常**；失败的只有 **httpx/pyqwest 这条传输栈**，
# 且失败发生在**握手之后**（裸 socket 同 ALPN 可正常收发）。
# ⇒ 这是 e2b 客户端与其依赖（pyqwest/httpx 0.28.x）在本机的兼容性问题，
# **不是** AGS 故障，也不是凭据问题——**不要**把它误报成"AGS 挂了"。
# 排查/修复方向：升级或降级 `pyqwest`/`httpx`，或让 e2b 走 requests 传输。
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy 2>/dev/null || true

exec "$PY" "$SCRIPT" "$@"
