#!/usr/bin/env python3
"""issue-pipeline supervisor 巡检：读台账 + keeper 日志 → 生成运行报告与改进建议。

用法：python3 flows/supervisor_patrol.py [--ledger PATH] [--log PATH]
输出：~/.issue-keeper/pipeline/report-latest.md（同时打印摘要到 stdout）
定时：LaunchAgent cc.agentstudio.issue-pipeline-patrol（每 4 小时）
"""
import json
import pathlib
import time
from collections import Counter

LEDGER = pathlib.Path("~/.issue-keeper/pipeline/runs.jsonl").expanduser()
KLOG = pathlib.Path("~/.issue-keeper/keeper.log").expanduser()
OUT = pathlib.Path("~/.issue-keeper/pipeline/report-latest.md").expanduser()


def load_runs(days: int = 7):
    if not LEDGER.exists():
        return []
    cutoff = time.time() - days * 86400
    runs = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        try:
            ts = time.mktime(time.strptime(r.get("ts", ""), "%Y-%m-%dT%H:%M:%S%z"))
        except Exception:
            continue
        if ts >= cutoff:
            runs.append(r)
    return runs


def keeper_alerts(hours: int = 24):
    """keeper 日志里近 N 小时的引擎级失败（超时/异常/兜底回评）。"""
    if not KLOG.exists():
        return []
    cutoff = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - hours * 3600))
    alerts = []
    for line in KLOG.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line[:16] >= cutoff and ("ERROR" in line) and ("issue" in line):
            alerts.append(line)
    return alerts


def suggestions(status_counts: Counter, alerts: list) -> list:
    out = []
    if status_counts.get("partial", 0) + status_counts.get("engine_error", 0) >= 2:
        out.append("partial/engine_error 偏多：把对应大题在 screener 或提示词层拆小，"
                   "或在 pipeline_test_commands 里放宽该仓的验证命令。")
    if status_counts.get("engine_error", 0):
        out.append("存在 engine_error（引擎层异常，keeper 兜底回评已覆盖）——"
                   "查 keeper.log 对应 issue 的堆栈，多为凭据/超时/沙箱问题。")
    no_comment = sum(1 for r in RUNS if r.get("comment_posted") is False)
    if no_comment:
        out.append(f"{no_comment} 次 comment_posted=false：keeper 兜底回评应已覆盖，"
                   "若 issue 上仍无评论则检查 gh 认证。")
    if status_counts.get("guarded", 0):
        out.append("出现 diff 护栏拦截：人工确认拦截是否正确；正确则考虑把对应约束写进 plan 提示词。")
    if status_counts.get("onhold", 0):
        out.append("有 HITL 暂缓待批：去聊天渠道或 hitl-server console 处理。")
    slow = [r for r in RUNS if (r.get("duration_secs") or 0) > 3600]
    if slow:
        out.append(f"{len(slow)} 个 run 超过 1 小时：考虑共享 CARGO_TARGET_DIR/缓存，或提高该段预算。")
    if not out:
        out.append("运行平稳，无规则触发。")
    return out


RUNS = load_runs()
status_counts = Counter(r.get("status") for r in RUNS)
alerts = keeper_alerts()
sugg = suggestions(status_counts, alerts)

lines = [
    f"# issue-pipeline 巡检报告 {time.strftime('%Y-%m-%d %H:%M')}",
    "",
    f"- 近 7 天 run 数：{len(RUNS)}",
    f"- 状态分布：{dict(status_counts)}",
    f"- comment_posted=false：{sum(1 for r in RUNS if r.get('comment_posted') is False)}",
    f"- keeper 近 24h ERROR：{len(alerts)} 条",
    "",
    "## 近 24h keeper 错误",
    *(["```", *alerts[-10:], "```"] if alerts else ["（无）"]),
    "",
    "## 建议",
    *(f"- {s}" for s in sugg),
    "",
]
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"报告已写 {OUT}")
print("\n".join(lines[-len(sugg) - 1:]))
