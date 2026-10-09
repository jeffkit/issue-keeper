#!/usr/bin/env python3
"""hitl_notify.py —— 值守→人 的 HITL 通知助手（微信/企微推送 + 反馈入口 + 可选阻塞等待）。

设计（jeffkit 2026-10-09：「需要人工处理时可以通过 HITL 通知人，但要提供反馈入口；
阻塞等待要看情况使用」）：
- **默认只推送**（`--wait-secs 0`）：不占执行位，但**仍建会话**（否则回复无处落，
  2026-10-09 实测踩过：无会话 → 人回复被服务端丢弃）；
- **按需限时等待**（`--wait-secs N` >0）：推完轮询回复最多 N 秒——仅用于「不马上定就
  会持续烧钱/扩大影响」的场景（如平台级 P0）；阻塞会占住 ctrl worker 并发位，慎用；
- **反馈入口双通道**：微信直接回（由 hitl_inbox.py 轮询会话收取并回落到 issue）+ 正文里必带 GitHub 链接
  （人在 issue 上回评/打标同样有效，且留痕在业务面上）；
- **去重**：同 dedupe-key 在 --dedupe-hours 内只推一次（防刷屏）；
- **留痕**：每次推送落 ~/.issue-keeper/duty/hitl-notifications.jsonl，供看板「等人工」面板展示。

用法：
  hitl_notify.py --title "标题" --body "正文" [--dedupe-key K] [--dedupe-hours 6]
                 [--wait-secs 0] [--feedback-url URL] [--dry-run]
输出：JSON {sent, session_id, skipped, waited_secs, replies}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8081"
LEDGER = pathlib.Path("~/.issue-keeper/duty/hitl-notifications.jsonl").expanduser()


def _post(url: str, payload: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _get(url: str, timeout: int = 15) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def _recent_keys(hours: float) -> set[str]:
    keys: set[str] = set()
    if not LEDGER.exists():
        return keys
    cutoff = time.time() - hours * 3600
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            if float(r.get("ts") or 0) >= cutoff and r.get("dedupe_key"):
                keys.add(str(r["dedupe_key"]))
        except Exception:
            continue
    return keys


def _append(rec: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", required=True)
    ap.add_argument("--dedupe-key", default="")
    ap.add_argument("--dedupe-hours", type=float, default=6.0)
    ap.add_argument("--wait-secs", type=int, default=0, help="0=只推送不等待（默认）；>0=限时轮询回复")
    ap.add_argument("--feedback-url", default="", help="反馈入口（GitHub issue 链接等）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    key = args.dedupe_key or hashlib.sha1((args.title + args.body).encode()).hexdigest()[:16]
    if key in _recent_keys(args.dedupe_hours):
        print(json.dumps({"sent": False, "skipped": "dedupe", "dedupe_key": key}, ensure_ascii=False))
        return 0

    lines = [f"🔔 【值守】{args.title}", "", args.body.strip()[:1200]]
    if args.feedback_url:
        lines += ["", f"反馈入口：{args.feedback_url}", "（微信直接回我，或在上面链接里回评/打 needs-human 标）"]
    if args.wait_secs > 0:
        lines += ["", f"⏳ 我在等你回复（最多 {args.wait_secs}s），不回我就按预案继续。"]
    message = "\n".join(lines)

    rec = {"ts": time.time(), "dedupe_key": key, "title": args.title,
           "feedback_url": args.feedback_url, "wait_secs": args.wait_secs, "body": args.body[:400]}
    if args.dry_run:
        print(json.dumps({"sent": False, "dryrun": True, "preview": message[:300]}, ensure_ascii=False))
        return 0

    try:
        # 2026-10-09 修正（jeffkit 实测：回复我发的 HITL 消息「AI 也没收到」）：
        # **必须建会话**，否则人在微信/企微里的回复没有可归属的 session，服务端只能丢弃。
        # 服务端语义（hil-mcp handlers/api.py）：只有 wait_reply=true 才 create_session；
        # false 时纯推送、无 session_id、`/admin/api/hil/sessions` 里查不到、回复无处落。
        # 因此这里**一律 wait_reply=true**（HTTP 调用本身不阻塞，阻塞的是 MCP 客户端的
        # 轮询循环）；timeout 给足（默认 24h）让回复有足够窗口被 ingest。
        # --wait-secs 只决定**本脚本是否当场轮询**（占用执行位），与建会话无关。
        resp = _post(f"{BASE}/api/send", {"message": message, "wait_reply": True,
                                          "timeout": int(os.environ.get("HITL_SESSION_TTL_SECS", "86400")),
                                          "upstream": "ilink"})
    except Exception as e:
        rec.update({"sent": False, "error": str(e)[:200]})
        _append(rec)
        print(json.dumps({"sent": False, "error": str(e)[:200]}, ensure_ascii=False))
        return 1

    sid = str(resp.get("session_id") or "")
    rec.update({"sent": True, "session_id": sid, "status": "sent"})

    replies: list = []
    waited = 0
    if args.wait_secs > 0 and sid:
        deadline = time.time() + args.wait_secs
        while time.time() < deadline:
            time.sleep(5)
            waited = int(args.wait_secs - max(0, deadline - time.time()))
            try:
                p = _get(f"{BASE}/api/poll/{sid}")
                if p.get("has_reply"):
                    replies = p.get("replies") or []
                    rec["status"] = "replied"
                    break
            except Exception:
                continue
        if not replies:
            rec["status"] = "timeout"

    rec["replies"] = [str(r.get("content") or r.get("text") or "")[:300] for r in replies][:3]
    _append(rec)
    print(json.dumps({"sent": True, "session_id": sid, "dedupe_key": key,
                      "waited_secs": waited, "status": rec["status"], "replies": rec["replies"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
