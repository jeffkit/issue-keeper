#!/usr/bin/env python3
"""ags-list.py —— AGS 沙箱实例清单（JSON 输出，供 flow/dashboard 复用）。

输出：[{"id","age_h","exec","state"}]（state 取 metadata 的 x-ags-sandbox-state）
用法：E2B_DOMAIN=... E2B_API_KEY=... <plaita venv python> ags-list.py
"""
from __future__ import annotations

import datetime
import json
import sys


def main() -> int:
    from e2b import Sandbox
    from e2b.sandbox.sandbox_api import SandboxQuery

    pag = Sandbox.list(SandboxQuery(metadata={}))
    items = pag.next_items() if hasattr(pag, "next_items") else list(pag)
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = []
    for it in items or []:
        meta = getattr(it, "metadata", {}) or {}
        started = getattr(it, "started_at", None)
        age = None
        try:
            st = started if isinstance(started, datetime.datetime) else datetime.datetime.fromisoformat(
                str(started).replace("Z", "+00:00"))
            if st.tzinfo is None:
                st = st.replace(tzinfo=datetime.timezone.utc)
            age = round((now - st).total_seconds() / 3600, 2)
        except Exception:
            pass
        rows.append({
            "id": str(getattr(it, "sandbox_id", "")),
            "short": str(getattr(it, "sandbox_id", ""))[:14],
            "age_h": age,
            "exec": str(meta.get("plaita_execution") or ""),
            "state": str(meta.get("x-ags-sandbox-state") or getattr(it, "state", "?")),
        })
    print(json.dumps(rows, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
