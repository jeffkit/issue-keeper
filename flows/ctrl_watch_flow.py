#!/usr/bin/env python3
"""ctrl-watch —— 主控 flow（看门狗 + 升级路由 · v0.1，ctrl 队列专用）。

2026-10-08 夜 jeffkit 令 zcode 主控退勤（19:36）后，本 flow 是**唯一值班主控**。
定位（docs/duty/DUTY-PROTOCOL.md §2.1）：**事件驱动的仲裁器，不是定时巡检器**——

  - 看门狗：盯 A（issue-accept，*/10）与 B（keeper-watch，2h）的心跳、
    shadow（派发链）新鲜度；超期才动作，**无事则一轮一行、零 LLM 调用**；
  - 升级路由：critical（A 死 / shadow 死）才发 GitHub #2 报请 jeffkit，
    且按 key 去重（60min 内不重复打扰）；
  - **不替值守做业务判断**（验收/派发决策归 A/B；权力边界 = roster.capabilities）。

为什么纯 CODE 无 AGENTRUN：本版的全部判定都是**规则**（时间戳差值、字段检查）。
LLM 仲裁留给真实冲突场景（如 A/B 报告互斥）——届时按 protocol 加 arb 节点，
不预支成本。

图结构（线性；CODE 只做 IO 叶子，同 keeper-watch 纪律）：
  facts（CODE：A/B 心跳 + shadow mtime + A 班未消化 escalation，全只读）
    → triage（CODE：纯规则 → findings + need_escalate）
    → act（CODE：critical 且未去重 → gh #2 报请）
    → finish（CODE：duty state-controller.json 滚动窗口 + rounds.log 一行）

心跳口径（读谁、怎么算死）：
  A = duty/state-issue-accept.json 的 rounds[-1].finished_at；周期 */10，超 25min = stale
  B = controller/rounds.log 最后一行 " B-flow"；周期 2h，超 3h = stale
  shadow = ssh tcloud_gz stat ~/.issue-keeper/shadow/latest.json mtime；周期 5min，超 20min = stale

编译：PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_ctrl_watch.py
"""
from __future__ import annotations

from plaita.dsl.codeflow import CODE, NODE, flow
from plaita.node import register_code_node

register_code_node(default_backend="subprocess")


@flow("ctrl-watch", desc="【值守·主控】看门狗+升级路由（A/B/shadow 心跳；critical 才扰 jeffkit，60min 去重）；ctrl 队列 */30")
def ctrl_watch(INPUT):
    # ── ① facts（IO 叶子：只读；每命令独立超时，失败置 None 不炸节点）─────
    facts = CODE(id="facts", lang="python", input={
        "duty_dir": INPUT.duty_dir, "rounds_log": INPUT.rounds_log,
    }, code="""
def run(input):
    import json, os, subprocess, time

    def sh(cmd, timeout=30):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return (r.stdout or "").strip()
        except Exception:
            return ""

    now = time.time()
    duty = os.path.expanduser(input.get("duty_dir") or "~/.issue-keeper/duty")
    rlog = os.path.expanduser(input.get("rounds_log")
                              or "~/.issue-keeper/pipeline/controller/rounds.log")

    # A 班心跳：duty state 滚动窗口最后一轮
    a = None
    try:
        st = json.load(open(os.path.join(duty, "state-issue-accept.json")))
        rs = st.get("rounds") or []
        if rs:
            last = rs[-1]
            a = {"round": last.get("round"), "at": last.get("finished_at"),
                 "status": last.get("status"),
                 "escalations": [f for f in (last.get("findings") or [])
                                 if f.get("escalate")]}
    except Exception:
        pass

    # B 班心跳：rounds.log 最后一行 B-flow（keeper-watch finish 写入）
    b = None
    try:
        for line in reversed(open(rlog, encoding="utf-8").readlines()[-400:]):
            if " B-flow" in line:
                b = {"line": line.strip()[:160], "at": line[:16]}
                break
    except Exception:
        pass

    # shadow 派发链心跳（远端 latest.json mtime；keeper 每 5min 派 shadow 一轮）
    shadow_age = None
    mt = sh("ssh -o ConnectTimeout=8 tcloud_gz "
            "'stat -c %Y ~/.issue-keeper/shadow/latest.json 2>/dev/null'")
    if mt.strip().isdigit():
        shadow_age = max(0, now - int(mt.strip()))

    def parse_hm(s):
        try:
            import calendar
            t = time.strptime(s, "%Y-%m-%d %H:%M")
            return calendar.timegm(t) - 8 * 3600  # 本机为 Asia/Shanghai（UTC+8）
        except Exception:
            return None

    def parse_iso(s):
        try:
            from datetime import datetime
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return None

    return {"now": now, "a": a, "a_ts": parse_iso((a or {}).get("at") or ""),
            "b": b, "b_ts": parse_hm((b or {}).get("at") or ""),
            "shadow_age": shadow_age}
""")

    # ── ② triage（纯规则；fail-safe：读不到心跳 = 按最坏报 critical）────────
    triage = CODE(id="triage", lang="python", input={
        "now": NODE.facts.now, "a": NODE.facts.a, "a_ts": NODE.facts.a_ts,
        "b": NODE.facts.b, "b_ts": NODE.facts.b_ts, "shadow_age": NODE.facts.shadow_age,
        "a_stale_min": INPUT.a_stale_min, "b_stale_min": INPUT.b_stale_min,
        "shadow_stale_min": INPUT.shadow_stale_min,
    }, code="""
def run(input):
    now = float(input.get("now") or 0)
    f = []

    a_stale = float(input.get("a_stale_min") or 25) * 60
    b_stale = float(input.get("b_stale_min") or 180) * 60
    s_stale = float(input.get("shadow_stale_min") or 20) * 60

    a, a_ts = input.get("a"), input.get("a_ts")
    if not a or not a_ts:
        f.append({"severity": "critical", "summary": "A 班（issue-accept）心跳不可读——duty state 缺失或损坏",
                  "escalate": True})
    elif now - a_ts > a_stale:
        f.append({"severity": "critical",
                  "summary": "A 班心跳超期 %.0fmin（阈值 %.0fmin；轮=%s）——外部验收闭环停摆"
                             % ((now - a_ts) / 60, a_stale / 60, a.get("round")),
                  "escalate": True})
    b, b_ts = input.get("b"), input.get("b_ts")
    if not b or not b_ts:
        f.append({"severity": "warn", "summary": "B 班（keeper-watch）心跳不可读（rounds.log 无 B-flow 行）"})
    elif now - b_ts > b_stale:
        f.append({"severity": "warn",
                  "summary": "B 班心跳超期 %.0fmin（阈值 %.0fmin）" % ((now - b_ts) / 60, b_stale / 60),
                  "escalate": True})
    sa = input.get("shadow_age")
    if sa is None:
        f.append({"severity": "critical", "summary": "shadow 心跳不可读（ssh 失败/latest.json 缺失）——派发链存疑",
                  "escalate": True})
    elif sa > s_stale:
        f.append({"severity": "critical",
                  "summary": "shadow latest.json %0.0fmin 未更新（阈值 %.0fmin）——派发链可能停摆"
                             % (sa / 60, s_stale / 60),
                  "escalate": True})

    # A 班上轮的待升级 finding（issue-accept 自己报请的）并入路由
    for e in (a or {}).get("escalations") or []:
        f.append({"severity": str(e.get("severity") or "warn"),
                  "summary": "A 班轮%s 报请：%s" % (a.get("round"), str(e.get("summary"))[:200]),
                  "escalate": True})

    need = any(x.get("escalate") and x.get("severity") == "critical" for x in f)
    report = "; ".join(x["summary"] for x in f) if f else "全线正常"
    return {"findings": f, "need_escalate": need, "report": report}
""")

    # ── ③ act（critical 且距上次同 key 升级 >60min 才扰 jeffkit）──────────
    act = CODE(id="act", lang="python", input={
        "need": NODE.triage.need_escalate, "report": NODE.triage.report,
        "duty_dir": INPUT.duty_dir, "dryrun": INPUT.dryrun,
    }, code="""
def run(input):
    import json, os, subprocess, time

    need = bool(input.get("need"))
    if not need:
        return {"posted": False, "why": "无需升级"}

    duty = os.path.expanduser(input.get("duty_dir") or "~/.issue-keeper/duty")
    spath = os.path.join(duty, "state-controller.json")
    now = time.time()
    doc = {}
    try:
        doc = json.load(open(spath))
    except Exception:
        pass
    meta = doc.get("meta") or {}
    last = float(meta.get("last_escalate_at") or 0)
    if now - last < 3600:
        return {"posted": False,
                "why": "60min 内已升级过（%.0fmin 前），去重" % ((now - last) / 60)}
    if input.get("dryrun"):
        return {"posted": False, "why": "dryrun"}

    body = ("<!-- issue-keeper-bot -->\\n**[ctrl-watch] 值守看门狗升级（P1）**\\n\\n"
            "%s\\n\\n—— 主控已 flow 化（ctrl-watch），本条为自动升级路由；"
            "处理完了回一句即可，我不会重复刷屏（60min 去重）。"
            % str(input.get("report") or "")[:1500])
    import tempfile
    fd, p = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w") as f:
        f.write(body)
    out = subprocess.run("gh issue comment 2 -R jeffkit/infra4agent --body-file " + p,
                         shell=True, capture_output=True, text=True, timeout=60)
    os.unlink(p)
    ok = out.returncode == 0
    meta["last_escalate_at"] = now
    doc["meta"] = meta
    try:
        import fcntl
        with open(spath + ".lock", "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            json.dump(doc, open(spath + ".tmp", "w"), ensure_ascii=False, indent=1)
            os.replace(spath + ".tmp", spath)
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    return {"posted": ok, "why": (out.stdout or out.stderr)[:160]}
""")

    # ── ④ finish（duty 滚动窗口 + rounds.log 一行；无事也只一行）──────────
    finish = CODE(id="finish", lang="python", input={
        "findings": NODE.triage.findings, "report": NODE.triage.report,
        "posted": NODE.act.posted, "duty_dir": INPUT.duty_dir,
    }, code="""
def run(input):
    import fcntl, json, os, tempfile, time

    duty = os.path.expanduser(input.get("duty_dir") or "~/.issue-keeper/duty")
    os.makedirs(duty, exist_ok=True)
    now = time.time()
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    findings = input.get("findings") or []
    status = "attention" if findings else "ok"
    rd = now
    try:
        doc = json.load(open(os.path.join(duty, "state-controller.json")))
        rd = max([r.get("round", 0) for r in (doc.get("rounds") or [])] or [0]) + 1
    except Exception:
        rd = 1
    round_doc = {
        "schema_version": "duty/state@0", "role": "controller", "generation": 2,
        "round": rd, "started_at": ts, "finished_at": ts, "status": status,
        "findings": findings,
        "narrative": str(input.get("report") or "")[:300]
                     + (" | 升级已发" if input.get("posted") else ""),
    }

    def atomic(path, d):
        fd, tmp = tempfile.mkstemp(dir=str(os.path.dirname(path)), prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(d, indent=1, ensure_ascii=False))
        os.replace(tmp, path)

    spath = os.path.join(duty, "state-controller.json")
    with open(spath + ".lock", "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            doc = {"schema_version": "duty/state@0", "role": "controller", "rounds": []}
            try:
                doc = json.load(open(spath))
            except Exception:
                pass
            meta = doc.get("meta") or {}
            rounds = doc.get("rounds") or []
            rounds.append(round_doc)
            doc.update({"rounds": rounds[-20:], "meta": meta, "updated_at": ts})
            atomic(spath, doc)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    with open(os.path.join(duty, "rounds.log"), "a") as fh:
        fh.write("%s ctrl-watch 轮次=%s 状态=%s findings=%s 升级=%s 摘要=%s\\n"
                 % (ts, rd, status, len(findings),
                    "已发" if input.get("posted") else "无",
                    str(input.get("report") or "")[:120]))

    return {"round": rd, "status": status, "posted": bool(input.get("posted"))}
""")

    return {"round": NODE.finish.round, "status": NODE.finish.status}


if __name__ == "__main__":
    print("ctrl-watch flow 源码（编译见 build_ctrl_watch.py）")
