#!/bin/bash
# external-watchdog —— 外置看门狗（launchd 每 5 分钟；独立于 schedule service 进程）
# 职责（闭环 2026-10-08 夜的教训「调度器挂死无人自动知道」）：
#   1. 读 duty 内核心跳（本机文件，不依赖任何远端）：
#      - ctrl-watch 轮 >45min 无新 = 调度器挂 → 自动 pkill 重拉（22:34 实证安全）
#      - A 班轮 >40min 无新 = 同上处置（A 是 */10，40min 必异常）
#   2. 修复后 15min 仍停摆 → 追加 ALERT 行（人工看）；同日首次 ALERTPERSIST 才发 #2（防刷屏）
#   3. GLM 1308 新增 → 记录重置时刻
# 日志：~/.issue-keeper/duty/external-watchdog.log
LOG=~/.issue-keeper/duty/external-watchdog.log
MARK=/tmp/extwd-alerted-$(date +%Y%m%d)
say() { echo "[$(date '+%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

age_of() {  # $1 = state 文件名 → 心跳年龄秒
  python3 - "$1" <<'PY'
import json, time, pathlib, sys
from datetime import datetime
try:
    st = json.loads((pathlib.Path.home() / ".issue-keeper/duty" / f"state-{sys.argv[1]}.json").read_text())
    ts = st["rounds"][-1]["finished_at"]
    print(int(time.time() - datetime.fromisoformat(ts).timestamp()))
except Exception:
    print(999999)
PY
}

C_AGE=$(age_of controller)
A_AGE=$(age_of issue-accept)

need_fix=0
[ "${C_AGE:-999999}" -gt 2700 ] 2>/dev/null && need_fix=1
[ "${A_AGE:-999999}" -gt 2400 ] 2>/dev/null && need_fix=1

if [ "$need_fix" = "1" ]; then
  say "心跳停摆（ctrl=${C_AGE}s a=${A_AGE}s）→ 自动干预：重拉 VM schedule service"
  ssh -o ConnectTimeout=8 tcloud_gz 'pkill -f "services.__main__.*schedule_service"' 2>/dev/null
  sleep 10
  ST=$(ssh -o ConnectTimeout=8 tcloud_gz 'systemctl is-active plaita-schedule-service' 2>/dev/null)
  say "  干预后 schedule service = $ST"
  sleep 90  # 给新实例时间跑出下一轮
  C_AGE2=$(age_of controller)
  if [ "${C_AGE2:-999999}" -gt 1800 ] 2>/dev/null; then
    say "🚨 ALERTPERSIST: 干预后 ctrl-watch 心跳仍 ${C_AGE2}s——值守停摆持续，需人工介入"
    if [ ! -f "$MARK" ]; then
      gh issue comment 2 -R jeffkit/infra4agent --body "**[external-watchdog] 值守调度持续停摆**：schedule service 重拉后 ctrl-watch 心跳仍超时（${C_AGE2}s）。外置看门狗已自动干预一次未果，请人工查看。今日仅此一条（防刷屏）。" >/dev/null 2>&1 \
        && say "  已发 #2 报请" && touch "$MARK"
    fi
  else
    say "  修复确认：ctrl-watch 心跳恢复（${C_AGE2}s）"
  fi
else
  # 健康：只记 1308 变化，其余静默（日志不膨胀）；基线持久化防误报
  N1308=$(grep -c "1308" ~/.plaita-console/worker-mac.log 2>/dev/null || echo 0)
  BASEF=/tmp/extwd-1308.baseline
  PREV=$(cat "$BASEF" 2>/dev/null || echo 0)
  if [ "$N1308" -gt "$PREV" ] 2>/dev/null; then
    R=$(grep -o "限额将在 [0-9-]* [0-9:]*" ~/.plaita-console/worker-mac.log | tail -1)
    say "GLM 烧穿（1308=$PREV→$N1308）。${R:-重置时刻未取到，看 AGS 控制台}"
    echo "$N1308" > "$BASEF"
  elif [ "$N1308" -lt "$PREV" ] 2>/dev/null; then
    echo "$N1308" > "$BASEF"  # 日志轮转/清理，基线跟随回落
  fi
fi
