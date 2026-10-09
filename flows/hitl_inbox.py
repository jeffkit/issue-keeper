#!/usr/bin/env python3
"""hitl_inbox.py —— 回收人对 HITL 通知的回复，并回落到业务面（GitHub issue）。

背景（2026-10-09 jeffkit 实测）：值守发出的 HITL 通知此前用 `wait_reply=false`
发送 → hitl-server **不建会话** → 人在微信/企微里的回复没有可归属的 session，
被服务端丢弃，AI 收不到（`/admin/api/hil/sessions` 为空即证据）。
`hitl_notify.py` 已改为一律 `wait_reply=true` 建会话；本脚本负责**收取回复**：

1. 读 `~/.issue-keeper/duty/hitl-notifications.jsonl` 里带 `session_id` 且未记录回复的记录；
2. `GET {base}/api/poll/{sid}` 查回复；
3. 收到回复 → ①回写 jsonl（status=replied + 回复正文）②`gh issue comment` 落到
   `feedback_url` 指向的 issue（业务面留痕，值守/管线随后都能看到）③打印摘要；
4. 兼收「服务端有回复但不在 jsonl」的会话（防御：通知脚本失败但会话建成了）。

用法：hitl_inbox.py [--base http://127.0.0.1:8081] [--dry-run]
输出：JSON {checked, replied: [{sid, title, text, routed_to}], errors}
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

LEDGER = pathlib.Path("~/.issue-keeper/duty/hitl-notifications.jsonl").expanduser()
BASE = "http://127.0.0.1:8081"


def _get(url: str, timeout: int = 15) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def _load() -> list[dict]:
    rows = []
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _append(rec: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _route_to_issue(feedback_url: str, text: str, title: str) -> str:
    """把回复落到 GitHub issue（业务面）；返回结果描述。"""
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)", feedback_url or "")
    if not m:
        return "无 GitHub 反馈入口（仅留痕）"
    repo, num = m.group(1), m.group(2)
    body = (f"<!-- issue-keeper-bot -->\n"
            f"**[hitl-inbox] 人工对值守通知的回复**（原通知：{title[:80]}）\n\n"
            f"> {text.strip()[:1500]}\n\n"
            f"—— 由 hitl_inbox 自动回落（微信/企微回复 → issue）")
    try:
        r = subprocess.run(["gh", "issue", "comment", num, "-R", repo, "--body", body],
                           capture_output=True, text=True, timeout=60)
        return f"已回落到 {repo}#{num}" if r.returncode == 0 else f"回落失败: {(r.stderr or '')[:80]}"
    except Exception as e:
        return f"回落异常: {str(e)[:80]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-age-h", type=float, default=48.0)
    args = ap.parse_args()

    rows = _load()
    # append-only 账本：同一 session 的「原通知行 + 回复行」都在账本里，原通知行
    # 永远没有 replied_at——直接用它构造 pending 会让已回复会话每轮重复处理、
    # 反复向 issue 贴同一条回复评论（2026-10-09 15:0x 实证：plaita#30 被贴 19 条）。
    # 按 session_id 取**最新一行**作为该会话当前状态（回复行由 dict(r) 复制了
    # 原通知行全部字段，取最新不丢 feedback_url/title）。
    latest: dict[str, dict] = {}
    for r in rows:
        sid = r.get("session_id")
        if sid:
            latest[str(sid)] = r
    rows = list(latest.values())
    cutoff = time.time() - args.max_age_h * 3600
    pending = [r for r in rows if r.get("session_id") and not r.get("replied_at")
               and float(r.get("ts") or 0) >= cutoff]
    # 防御：服务端有会话但 jsonl 未记录（例如通知脚本中途失败）
    try:
        srv = _get(f"{args.base}/admin/api/hil/sessions").get("sessions") or []
    except Exception:
        srv = []
    known = {r.get("session_id") for r in rows if r.get("session_id")}
    for s in srv:
        sid = str(s.get("session_id") or "")
        if sid and sid not in known and int(s.get("replies_count") or 0) > 0:
            pending.append({"session_id": sid, "title": "（服务端补录）" + str(s.get("message") or "")[:60],
                            "feedback_url": "", "ts": time.time()})

    replied, errors = [], []
    for r in pending:
        sid = r["session_id"]
        try:
            p = _get(f"{args.base}/api/poll/{sid}")
        except Exception as e:
            errors.append(f"{sid[:8]}: {str(e)[:60]}")
            continue
        if not p.get("has_reply"):
            continue
        texts = [str(x.get("content") or x.get("text") or "") for x in (p.get("replies") or [])]
        text = " / ".join(t for t in texts if t)[:2000]
        rec = dict(r)
        rec.update({"replied_at": time.time(), "status": "replied", "replies": texts[:3],
                    "reply_text": text[:500]})
        if not args.dry_run:
            rec["routed_to"] = _route_to_issue(r.get("feedback_url") or "", text, r.get("title") or "")
            _append(rec)
            # 三层协同闭环：把回复回填到对应决策工单（值守 Agent 下轮据此执行）
            try:
                rs = "/Users/kong/projects/infra4agent/issue-keeper/flows/duty_request.py"
                rr = subprocess.run(["python3", rs, "answer", "--session-id", sid, "--text", text],
                                    capture_output=True, text=True, timeout=30)
                hit = json.loads((rr.stdout or "[]").strip() or "[]")
                if hit:
                    rec["request_id"] = hit[0].get("id")
            except Exception:
                pass
        replied.append({"sid": sid[:12], "title": (r.get("title") or "")[:50], "text": text[:200],
                        "routed_to": rec.get("routed_to")})
    print(json.dumps({"checked": len(pending), "replied": replied, "errors": errors,
                      "server_sessions": len(srv)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
