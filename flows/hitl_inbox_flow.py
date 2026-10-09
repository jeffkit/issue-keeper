#!/usr/bin/env python3
"""hitl-inbox —— 回收人对 HITL 通知的回复（v0.1，ctrl 队列专用）。

背景（2026-10-09 jeffkit 实测「我回复了，AI 也应该没收到的」）：
值守发出的 HITL 通知此前用 `wait_reply=false` → 服务端**不建会话** → 人在微信/
企微的回复没有可归属 session，被丢弃（`/admin/api/hil/sessions` 为空即证据）。
`hitl_notify.py` 已改为一律建会话（`wait_reply=true` + 24h TTL）；本 flow 负责**收**：

  facts（CODE：跑 hitl_inbox.py——轮询会话 → 回复回写 jsonl + `gh issue comment`
         落到通知里带的 feedback_url，业务面留痕）
    → finish（CODE：**有回复才写 duty 轮报**，避免每 5 分钟刷屏）

为什么单独一个 flow 而不是塞进 ctrl-watch：回复要**分钟级**收（人回完就期待有反应），
ctrl-watch 是 */30；本 flow 轻量纯 IO，*/5 一轮成本可忽略。

编译：PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_flows.py hitl-inbox
"""
from __future__ import annotations

from plaita.dsl.codeflow import CODE, NODE, flow
from plaita.node import register_code_node

register_code_node(default_backend="subprocess")


@flow("hitl-inbox", desc="【值守·通道】回收人对 HITL 通知的回复（轮询会话 → 落到 issue + 留痕）；ctrl 队列 */5")
def hitl_inbox(INPUT):
    facts = CODE(id="facts", lang="python", input={
        "inbox_script": INPUT.inbox_script, "hitl_base": INPUT.hitl_base,
    }, code="""
def run(input):
    import json, subprocess

    script = input.get("inbox_script") or "/Users/kong/projects/infra4agent/issue-keeper/flows/hitl_inbox.py"
    base = input.get("hitl_base") or "http://127.0.0.1:8081"
    try:
        r = subprocess.run(["python3", script, "--base", base],
                           capture_output=True, text=True, timeout=180)
        out = (r.stdout or "").strip().splitlines()
        d = json.loads(out[-1]) if out else {}
    except Exception as e:
        return {"checked": 0, "replied": [], "errors": [str(e)[:120]], "server_sessions": 0}
    d.setdefault("replied", [])
    d.setdefault("errors", [])
    return d
""")

    finish = CODE(id="finish", lang="python", input={
        "replied": NODE.facts.replied, "checked": NODE.facts.checked,
        "errors": NODE.facts.errors, "duty_dir": INPUT.duty_dir,
    }, code="""
def run(input):
    import fcntl, json, os, tempfile, time
    replied = input.get("replied") or []
    if not replied and not (input.get("errors") or []):
        # 无回复且无异常：静默（不写轮报，避免每 5 分钟刷屏）
        return {"round": None, "status": "quiet", "n": 0}
    duty = os.path.expanduser(input.get("duty_dir") or "~/.issue-keeper/duty")
    os.makedirs(duty, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    spath = os.path.join(duty, "state-hitl.json")
    rd = 1
    try:
        doc = json.load(open(spath))
        rd = max([r.get("round", 0) for r in (doc.get("rounds") or [])] or [0]) + 1
    except Exception:
        pass
    findings = []
    for r in replied:
        findings.append({"severity": "info",
                         "summary": "收到人工回复（%s）：%s → %s" % (r.get("sid"), (r.get("text") or "")[:80],
                                                                   r.get("routed_to") or "仅留痕"),
                         "escalate": False})
    for e in (input.get("errors") or [])[:3]:
        findings.append({"severity": "warn", "summary": "会话轮询异常：%s" % str(e)[:100],
                         "escalate": False})
    status = "ok" if replied else "attention"
    round_doc = {"schema_version": "duty/state@0", "role": "hitl", "generation": 2,
                 "round": rd, "started_at": ts, "finished_at": ts, "status": status,
                 "findings": findings,
                 "narrative": "回收人工回复 %d 条（检查会话 %s）" % (len(replied), input.get("checked"))}
    def atomic(path, d):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(d, indent=1, ensure_ascii=False))
        os.replace(tmp, path)
    with open(spath + ".lock", "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            doc = {"schema_version": "duty/state@0", "role": "hitl", "rounds": []}
            try:
                doc = json.load(open(spath))
            except Exception:
                pass
            rounds = doc.get("rounds") or []
            rounds.append(round_doc)
            doc.update({"rounds": rounds[-20:], "updated_at": ts})
            atomic(spath, doc)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    with open(os.path.join(duty, "rounds.log"), "a") as fh:
        fh.write("%s hitl-inbox 轮次=%s 状态=%s 回复=%d 检查=%s\\n"
                 % (ts, rd, status, len(replied), input.get("checked")))
    return {"round": rd, "status": status, "n": len(replied)}
""")

    return {"round": NODE.finish.round, "status": NODE.finish.status}


if __name__ == "__main__":
    print("hitl-inbox flow 源码（编译见 build_flows.py）")
