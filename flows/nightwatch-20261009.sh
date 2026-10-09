#!/bin/bash
# nightwatch-20261009 —— 全 flow 值守体系夜间看护（jeffkit 授权：有问题及时干预）
# 覆盖窗口：~23:10 → 02:50（含 GLM 窗口 22:10→03:10 的烧穿与重置）
# 看护面（全部来自今晚的实证教训）：
#   1. A 班心跳（issue-accept，*/10）：>30min 无新轮 = 停摆 → CRITICAL
#   2. 调度器心跳（ctrl-watch 轮，*/30）：>45min 无新轮 = schedule service 再次挂死
#      → 自动干预：pkill 重拉（今晚 22:34 同款，已验证安全）
#   3. B 班心跳（keeper-watch，40 */2）：00:45 后仍无 B-flow 行 = 复活失败 → CRITICAL（不自动修，需判断）
#   4. GLM 烧穿：worker-mac.log 1308 计数增长 → 提取重置时刻记录；重置+3min 后核验自愈
#   5. 僵尸锚：in_flight_since > 125min 计数（keeper 僵尸线 2h 应收）
# 日志：stdout + ~/.issue-keeper/duty/nightwatch-20261009.log
LOG=~/.issue-keeper/duty/nightwatch-20261009.log
KEY="b4b5042ee7d1b937633c08f3f50d4c8efbca88d33ece8a03"
say() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

say "=== 夜间看护启动（窗口至 02:50）==="
PREV_1308=$(grep -c "1308" ~/.plaita-console/worker-mac.log 2>/dev/null || echo 0)
RESET_AT=""
B_REVIVED=0

for i in $(seq 1 105); do
  sleep 120
  NOW=$(date "+%H:%M")
  # ---- A 班心跳 ----
  A_AGE=$(python3 -c "
import json, time, pathlib
try:
    st = json.load(open(pathlib.Path.home()/'.issue-keeper/duty/state-issue-accept.json'))
    from datetime import datetime
    ts = st['rounds'][-1]['finished_at']
    print(int(time.time() - datetime.fromisoformat(ts).timestamp()))
except Exception:
    print(99999)" 2>/dev/null)
  # ---- ctrl-watch 心跳 ----
  C_AGE=$(python3 -c "
import json, time, pathlib
try:
    st = json.load(open(pathlib.Path.home()/'.issue-keeper/duty/state-controller.json'))
    from datetime import datetime
    ts = st['rounds'][-1]['finished_at']
    print(int(time.time() - datetime.fromisoformat(ts).timestamp()))
except Exception:
    print(99999)" 2>/dev/null)
  # ---- B 班心跳 ----
  B_LAST=$(grep " B-flow" ~/.issue-keeper/pipeline/controller/rounds.log 2>/dev/null | tail -1 | cut -c1-16)
  B_MIN=$(python3 -c "
import time, sys
try:
    t = time.strptime('$B_LAST', '%Y-%m-%d %H:%M')
    import calendar
    print(int((time.time() - (calendar.timegm(t) - 8*3600)) / 60))
except Exception:
    print(99999)" 2>/dev/null)
  # ---- GLM 1308 ----
  CUR_1308=$(grep -c "1308" ~/.plaita-console/worker-mac.log 2>/dev/null || echo 0)

  STATUS="A:${A_AGE}s C:${C_AGE}s B:${B_MIN}min 1308:${CUR_1308}"

  # ---- 判定与干预 ----
  if [ "$CUR_1308" -gt "$PREV_1308" ] 2>/dev/null; then
    RESET=$(grep -o "限额将在 [0-9-]* [0-9:]*" ~/.plaita-console/worker-mac.log 2>/dev/null | tail -1)
    say "⚠ GLM 烧穿确认（1308: $PREV_1308→$CUR_1308）。$RESET。按兵不动，重置后核验自愈。"
    PREV_1308=$CUR_1308
    RESET_AT=$(echo "$RESET" | grep -o "[0-9][0-9]:[0-9][0-9]:[0-9][0-9]$")
  fi
  if [ -n "$RESET_AT" ] && [ "$NOW" \> "$RESET_AT" ] 2>/dev/null && [ "${RESET_CHECKED:-0}" = "0" ]; then
    sleep 180  # 重置后 3 分钟再核
    NEWRUN=$(ssh -o ConnectTimeout=8 tcloud_gz "grep -c '已入队' /home/ubuntu/.plaita-console/schedule-service.log 2>/dev/null" 2>/dev/null)
    say "✅ 重置核验：调度入队累计=$NEWRUN（在增长=管线自愈中）"
    RESET_CHECKED=1
  fi
  if [ "${A_AGE:-0}" -gt 1800 ] 2>/dev/null; then
    say "🚨 CRITICAL: A 班心跳 ${A_AGE}s 无新轮——issue-accept 停摆，需人工查（不自动修）"
  fi
  if [ "${C_AGE:-0}" -gt 2700 ] 2>/dev/null; then
    say "🚨 调度器再挂（ctrl-watch 心跳 ${C_AGE}s）→ 自动干预：pkill 重拉 schedule service"
    ssh -o ConnectTimeout=8 tcloud_gz 'pkill -f "services.__main__.*schedule_service"' 2>/dev/null
    sleep 8
    ALIVE=$(ssh -o ConnectTimeout=8 tcloud_gz 'systemctl is-active plaita-schedule-service' 2>/dev/null)
    say "   干预后：schedule service = $ALIVE"
    sleep 100
  fi
  if [ "$NOW" \> "00:45" 2>/dev/null ] && [ "$B_REVIVED" = "0" ]; then
    if [ "${B_MIN:-99999}" -lt 60 ] 2>/dev/null; then
      say "✅ B 班复活确认（最后 B-flow 轮 $B_LAST，${B_MIN}min 前）"
      B_REVIVED=1
    else
      say "🚨 CRITICAL: 00:45 已过，B 班仍无成功轮（最后 $B_LAST）——需人工查 keeper-watch DLQ"
      B_REVIVED=2
    fi
  fi
  # 每 5 tick 一次常规行
  if [ $((i % 3)) -eq 0 ]; then say "tick $i $STATUS"; fi
done
say "=== 看护窗口结束 ==="
