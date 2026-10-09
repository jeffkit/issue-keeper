#!/usr/bin/env python3
"""duty_escalation.py —— 值守兜底升级：critical 工单久悬时**直接叫醒人**。

为什么需要（2026-10-09 jeffkit 拍板 #3）：
值守 Agent 是「有上下文的脑子」，但它会宕、会被限流、会话可能不在。此前唯一发现
路径是「工单躺在收件箱等我下一轮」——若我不在场，critical 工单可以无限期无人知。
本脚本独立于 DSH 会话与 console flow 运行（launchd），是**最后防线**。

判据（两级阶梯）：
- 值守疑似不在场：心跳（probe-heartbeat.json，探针每轮第 0 步写）距今 > absent_min
  **且** 存在 critical 工单 age > stale_min  → 升级
- 值守在场但单久悬：critical 工单 age > stuck_min → 升级（提示值守卡住了）
动作：hitl_notify.py 发三行白话（出了什么事 / 建议 / 回个字即可）。
节流：同一工单 cooldown_hours 内只推一次（本地 ledger，稳定键 `duty:stale:<id>`）。

用法：python3 duty_escalation.py [--dry-run] [--stale-min 15] [--absent-min 20] [--stuck-min 60]
输出：JSON {"checked","escalated":[ids],"skipped","heartbeat_age_min"}
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

DUTY = pathlib.Path("~/.issue-keeper/duty").expanduser()
REQ = DUTY / "requests"
HB = DUTY / "probe-heartbeat.json"
LEDGER = DUTY / "escalation-ledger.json"
HITL = pathlib.Path("~/projects/infra4agent/issue-keeper/flows/hitl_notify.py").expanduser()


def _age_min(ts: float) -> float:
    return round((time.time() - ts) / 60, 1)


def _parse_iso(s: str) -> float | None:
    try:
        return time.mktime(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-min", type=float, default=15.0, help="值守不在场时，critical 工单可悬的分钟数")
    ap.add_argument("--absent-min", type=float, default=20.0, help="心跳超过此分钟数视为值守不在场")
    ap.add_argument("--stuck-min", type=float, default=60.0, help="值守在场也允许的 critical 最长悬挂")
    ap.add_argument("--cooldown-hours", type=float, default=6.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    out = {"checked": 0, "escalated": [], "skipped": [], "heartbeat_age_min": None, "error": None}

    # 心跳（值守活跃度代理）
    hb_age = None
    if HB.exists():
        hb_age = _age_min(HB.stat().st_mtime)
    out["heartbeat_age_min"] = hb_age
    agent_absent = hb_age is None or hb_age > args.absent_min

    try:
        ledger = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else {}
    except Exception:
        ledger = {}

    now = time.time()
    for p in sorted(REQ.glob("req-*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("severity") != "critical":
            continue
        if d.get("status") not in ("open", "escalated"):
            continue
        out["checked"] += 1
        created = _parse_iso(d.get("created_at")) or p.stat().st_mtime
        age = (now - created) / 60
        reason = None
        if agent_absent and age > args.stale_min:
            reason = "值守疑似不在场（心跳 %s 分钟前）且 critical 工单已悬 %.0f 分钟" % (hb_age, age)
        elif age > args.stuck_min:
            reason = "critical 工单已悬 %.0f 分钟（值守在场但久未处置）" % age
        if not reason:
            continue
        rid = str(d.get("id"))
        last = float(ledger.get(rid) or 0)
        if now - last < args.cooldown_hours * 3600:
            out["skipped"].append({"id": rid, "why": "cooldown"})
            continue
        title = "值守兜底：%s" % str(d.get("title") or rid)[:60]
        body = ("【出了什么事】%s\n"
                "【卡在哪】%s\n"
                "【我建议】回一个字：继续等值守，或回「我来」接手；细节见 %s\n"
                "（本消息由 duty_escalation 兜底发出——值守侧疑似未及时处置）"
                % (str(d.get("title") or "")[:120], reason,
                   "~/.issue-keeper/duty/requests/%s.json" % rid))
        if args.dry_run:
            out["escalated"].append({"id": rid, "dry": True, "reason": reason})
            continue
        try:
            r = subprocess.run(["python3", str(HITL), "--title", title, "--body", body,
                                "--dedupe-key", "duty:stale:%s" % rid],
                               capture_output=True, text=True, timeout=60)
            ok = r.returncode == 0
            out["escalated"].append({"id": rid, "sent": ok, "reason": reason,
                                     "err": (r.stderr or "")[:80] if not ok else ""})
            if ok:
                ledger[rid] = now
        except Exception as e:
            out["escalated"].append({"id": rid, "sent": False, "err": str(e)[:80]})

    if not args.dry_run and out["escalated"]:
        try:
            fd = os.open(str(LEDGER), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(ledger, f)
        except Exception:
            pass
        try:
            with open(DUTY / "escalation.log", "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                     json.dumps(out, ensure_ascii=False)))
        except Exception:
            pass

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
