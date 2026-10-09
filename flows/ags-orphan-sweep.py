#!/usr/bin/env python3
"""AGS 沙箱孤儿清查——成本闸（2026-10-09，jeffkit 报「沙箱费用好几十块」后立）。

只杀一种实例：**其 plaita_execution 已终态（completed/failed/cancelled）或查无**
且存活超过 --min-age 小时的 AGS 实例。对活跑零风险。

用法：ags-orphan-sweep.py [--min-age 1.0] [--dry-run]
建议 launchd 每 30min 跑一次（治本=plaita 侧 run 结束时杀实例，见 P1）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import urllib.request

E2B_DOMAIN = "ap-guangzhou.tencentags.com"
E2B_API_KEY = "e2b_725235357335be8d27367c596c9e3199cf3c5eeb"
CONSOLE = "http://127.0.0.1:8323/api"
CONSOLE_KEY = "b4b5042ee7d1b937633c08f3f50d4c8efbca88d33ece8a03"
TERMINAL = {"completed", "failed", "cancelled", "error"}


def console_exec(eid: str):
    if not eid or eid == "?":
        return None
    req = urllib.request.Request(
        f"{CONSOLE}/executions/{eid}", headers={"X-Admin-API-Key": CONSOLE_KEY})
    try:
        return json.load(urllib.request.urlopen(req, timeout=8))
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-age", type=float, default=1.0, help="实例最小存活小时数")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import os
    os.environ.setdefault("E2B_DOMAIN", E2B_DOMAIN)
    os.environ.setdefault("E2B_API_KEY", E2B_API_KEY)
    from e2b import Sandbox
    from e2b.sandbox.sandbox_api import SandboxQuery

    pag = Sandbox.list(SandboxQuery(metadata={}))
    items = pag.next_items() if hasattr(pag, "next_items") else list(pag)
    now = datetime.datetime.now(datetime.timezone.utc)
    killed, kept = [], []
    for it in (items or []):
        sid = getattr(it, "sandbox_id", "")
        meta = getattr(it, "metadata", {}) or {}
        eid = str(meta.get("plaita_execution", "") or "?")
        started = getattr(it, "started_at", None)
        age_h = 0.0
        try:
            st = started if isinstance(started, datetime.datetime) else datetime.datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            if st.tzinfo is None:
                st = st.replace(tzinfo=datetime.timezone.utc)
            age_h = (now - st).total_seconds() / 3600
        except Exception:
            age_h = 999.0
        e = console_exec(eid)
        est = str((e or {}).get("status") or "unknown")
        orphan = (est in TERMINAL or est == "unknown") and age_h >= args.min_age
        tag = "ORPHAN" if orphan else "keep"
        line = f"[{tag}] {sid[:20]} {age_h:.1f}h exec={eid[:10]} 执行态={est}"
        print(line, flush=True)
        if orphan and not args.dry_run:
            try:
                r = Sandbox.kill(sid)
                (killed if r else kept).append(sid)
                print(f"    → kill={r}")
            except Exception as exc:
                print(f"    → kill 异常: {str(exc)[:120]}")
        elif orphan:
            kept.append(sid)
    print(f"汇总：实例 {len(items or [])}，孤儿击杀 {len(killed)}，保留 {len(kept)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
