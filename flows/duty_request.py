#!/usr/bin/env python3
"""duty_request.py —— 决策请求（工单）存储：flow → 值守 Agent → 人 的载体。

三层协同（jeffkit 2026-10-09 定的正确形态）：

    flow 过程里需要决策
      ① 先交给**值守 Agent**（本目录的 requests/*.json 就是它的收件箱）
         —— 它能判的按能力矩阵自己判 + 执行 + 留痕；
      ② 它判不了（human-in-loop 类：重启 worker/改并发/改模型档/改基础设施/
         重大拍板）→ 发 HITL 通知给人 + **监听回复**（hitl_inbox 回收）；
      ③ 人回复 → 回填到本工单（status=answered）→ 值守 Agent 据回复执行闭环。

字段（duty/request@0）：
    id, created_at, from_flow, kind, severity, title, context{}, options[],
    status(open|decided|escalated|answered|resolved|dismissed),
    decision{by, action, args{}, rationale, at}, human{session_id, notified_at, reply, replied_at},
    history[]

用法：
    duty_request.py create --from-flow F --kind K --severity critical --title T \
        [--context-json '{...}'] [--options a,b]
    duty_request.py list [--status open] [--json]
    duty_request.py update --id ID --status S [--decision-json '{}'] [--note N]
    duty_request.py answer --session-id SID --text "人回复的原文"
    duty_request.py get --id ID
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pathlib
import sys
import tempfile
import time
import uuid

BASE = pathlib.Path(os.path.expanduser(os.environ.get("DUTY_DIR", "~/.issue-keeper/duty")))
REQ_DIR = BASE / "requests"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _atomic(path: pathlib.Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _locked(path: pathlib.Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(str(path) + ".lock", "w")


def create(args) -> dict:
    rid = "req-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    ctx = {}
    if args.context_json:
        try:
            ctx = json.loads(args.context_json)
        except Exception:
            ctx = {"raw": args.context_json[:2000]}
    doc = {
        "schema_version": "duty/request@0",
        "id": rid, "created_at": _now(), "from_flow": args.from_flow,
        "kind": args.kind, "severity": args.severity, "title": args.title,
        "context": ctx,
        "options": [o.strip() for o in (args.options or "").split(",") if o.strip()],
        "status": "open", "decision": None, "human": None,
        "history": [{"at": _now(), "event": "created", "by": args.from_flow}],
    }
    _atomic(REQ_DIR / (rid + ".json"), doc)
    return doc


def _load_all() -> list[dict]:
    out = []
    if REQ_DIR.is_dir():
        for p in sorted(REQ_DIR.glob("req-*.json")):
            try:
                out.append(json.load(open(p)))
            except Exception:
                continue
    return out


def list_reqs(args) -> list[dict]:
    rows = _load_all()
    if args.status:
        want = set(args.status.split(","))
        rows = [r for r in rows if r.get("status") in want]
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


def update(args) -> dict | None:
    path = REQ_DIR / (args.id + ".json")
    if not path.exists():
        return None
    with _locked(path) as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            doc = json.load(open(path))
            if args.status:
                doc["status"] = args.status
            if args.decision_json:
                try:
                    doc["decision"] = json.loads(args.decision_json)
                except Exception as e:
                    doc["decision"] = {"by": "agent", "error": str(e)[:120]}
            if args.note:
                doc.setdefault("history", []).append({"at": _now(), "event": args.note, "by": "duty-agent"})
            _atomic(path, doc)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    return doc


def answer(args) -> list[dict]:
    """按 session_id 把人回复回填到工单（hitl_inbox 调用）。"""
    hit = []
    for p in REQ_DIR.glob("req-*.json") if REQ_DIR.is_dir() else []:
        with _locked(p) as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                doc = json.load(open(p))
                h = doc.get("human") or {}
                if h.get("session_id") and h["session_id"] == args.session_id:
                    h["reply"] = (args.text or "")[:2000]
                    h["replied_at"] = _now()
                    doc["human"] = h
                    doc["status"] = "answered"
                    doc.setdefault("history", []).append(
                        {"at": _now(), "event": "human_replied", "by": "hitl"})
                    _atomic(p, doc)
                    hit.append(doc)
            except Exception:
                continue
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    return hit


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--from-flow", required=True)
    c.add_argument("--kind", required=True)
    c.add_argument("--severity", default="warn")
    c.add_argument("--title", required=True)
    c.add_argument("--context-json", default="")
    c.add_argument("--options", default="")
    l = sub.add_parser("list")
    l.add_argument("--status", default="")
    l.add_argument("--json", action="store_true")
    u = sub.add_parser("update")
    u.add_argument("--id", required=True)
    u.add_argument("--status", default="")
    u.add_argument("--decision-json", default="")
    u.add_argument("--note", default="")
    a = sub.add_parser("answer")
    a.add_argument("--session-id", required=True)
    a.add_argument("--text", required=True)
    g = sub.add_parser("get")
    g.add_argument("--id", required=True)

    args = ap.parse_args()
    if args.cmd == "create":
        print(json.dumps(create(args), ensure_ascii=False))
    elif args.cmd == "list":
        rows = list_reqs(args)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            for r in rows:
                print("%s %-9s %-8s %-16s %s" % (r["id"], r["status"], r["severity"],
                                                 r["from_flow"], (r["title"] or "")[:60]))
    elif args.cmd == "update":
        print(json.dumps(update(args) or {"error": "not found"}, ensure_ascii=False))
    elif args.cmd == "answer":
        print(json.dumps(answer(args), ensure_ascii=False))
    elif args.cmd == "get":
        p = REQ_DIR / (args.id + ".json")
        print(json.dumps(json.load(open(p)), ensure_ascii=False) if p.exists() else "{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
