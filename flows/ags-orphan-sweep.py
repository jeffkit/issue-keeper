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
E2B_API_KEY = __import__("os").environ.get("E2B_API_KEY", "")
CONSOLE = "http://127.0.0.1:8323/api"
CONSOLE_KEY = __import__("os").environ.get("PLAITA_CONSOLE_ADMIN_API_KEY", "")
TERMINAL = {"completed", "failed", "cancelled", "error"}


def _list_sandboxes():
    """列出 AGS 沙箱，返回 (items, via)。

    **为什么要有 urllib 这条兜底路径**（2026-10-13 实测）：
    本机上 `httpx` / `requests` 对 AGS 主机**确定性失败**
    （`SSL: UNEXPECTED_EOF_WHILE_READING`），而 **`urllib`（stdlib）正常**
    （`GET /v2/sandboxes` → 200 `[]`），`curl` 亦正常。e2b SDK 走的是 httpx，
    因此它会失败 —— 于是**先试 e2b，失败则退到 urllib**，
    保证"列出沙箱"这一步不会因为客户端库的问题而让整个清理失效。

    两条路径返回**统一形状**（list[dict]，键用 snake_case），
    故下游遍历逻辑无需分支。

    注意：**这里只改 list**。`kill` 仍走 e2b（当前 0 沙箱，未能独立验证
    `DELETE /v2/sandboxes/{id}` 的行为，**不拿"list 通了"推断"kill 也通"**）。
    """
    # 路径 1：e2b SDK（原行为）
    try:
        from e2b import Sandbox
        from e2b.sandbox.sandbox_api import SandboxQuery

        pag = Sandbox.list(SandboxQuery(metadata={}))
        raw = pag.next_items() if hasattr(pag, "next_items") else list(pag)
        out = []
        for it in (raw or []):
            out.append({
                "sandbox_id": getattr(it, "sandbox_id", ""),
                "metadata": getattr(it, "metadata", {}) or {},
                "started_at": getattr(it, "started_at", None),
                "_raw": it,
            })
        return out, "e2b"
    except Exception as exc:  # noqa: BLE001 —— 任何失败都退到 urllib
        print(f"[warn] e2b 列沙箱失败（{type(exc).__name__}: {str(exc)[:80]}），改用 urllib", flush=True)

    # 路径 2：stdlib urllib 直连 REST
    req = urllib.request.Request(
        f"https://api.{E2B_DOMAIN}/v2/sandboxes",
        headers={"X-API-KEY": E2B_API_KEY},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for it in (data or []):
        out.append({
            "sandbox_id": it.get("sandboxID") or it.get("sandbox_id") or it.get("id") or "",
            "metadata": it.get("metadata") or {},
            "started_at": it.get("startedAt") or it.get("started_at"),
            "_raw": it,
        })
    return out, "urllib"


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
    # 代理必须清掉：`NO_PROXY` 不含 AGS 域名，带上它该主机**必失败**（实测 curl
    # 清 proxy=401 / 带 proxy=000）。故在此兜底，不依赖调用方环境。
    for _v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        os.environ.pop(_v, None)

    items, via = _list_sandboxes()
    now = datetime.datetime.now(datetime.timezone.utc)
    killed, kept = [], []
    for it in (items or []):
        sid = it.get("sandbox_id") or it.get("sandboxID") or it.get("id") or ""
        meta = it.get("metadata") or {}
        eid = str(meta.get("plaita_execution", "") or "?")
        started = it.get("started_at") or it.get("startedAt")
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
                # kill 仍走 e2b（**未改**）：当前 0 沙箱，`DELETE /v2/sandboxes/{id}`
                # 的行为**未被独立验证**，故不做"list 通了 ⇒ kill 也通"的推断。
                # 若 e2b 因 httpx 问题不可用，这里会走 except 并**打印异常**（不静默）。
                from e2b import Sandbox as _Sbx
                r = _Sbx.kill(sid)
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
