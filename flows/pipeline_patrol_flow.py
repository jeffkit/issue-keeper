#!/usr/bin/env python3
"""pipeline-patrol —— 管线巡检 flow（L3 经验闭环 · v0.1，ctrl 队列专用）。

取代 launchd `cc.agentstudio.issue-pipeline-patrol`（每 4h）。替代理由（2026-10-09 实测）：
keeper 已于 10-07 单实例迁远端 tcloud_gz，而该 LaunchAgent 读的是**本机**
`~/.issue-keeper/{pipeline/runs.jsonl,keeper.log,metrics/}`——本机数据自 10-07 19:3x
起停更（陈旧 1.5 天），巡检结论与提案都基于旧数据。flow 版把跑点放到 VM（数据所在），
并只把**结论**带回 duty 内核与轮报。

职责（沿用 supervisor_patrol.py 的 L3 语义，不重写分析逻辑）：
  台账 + metrics + keeper 日志 → 观测摘要报告（report-latest.md）
                              → 契约变更提案（proposals/*.json：门超时/段预算/人工）
本 flow 只做**采集 + 分级 + 留痕**；提案的 apply/reject 仍由人（或后续人机边界流程）裁决。

图结构（纯 CODE，零 LLM）：
  facts（CODE：ssh 跑远端 patrol + 提案清点 + 台账新鲜度，全只读）
    → triage（CODE：错误/待审提案/管线闲置 三类规则）
    → finish（CODE：duty state-patrol + rounds.log 一行）

编译：PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_pipeline_patrol.py
"""
from __future__ import annotations

from plaita.dsl.codeflow import CODE, NODE, flow
from plaita.node import register_code_node

register_code_node(default_backend="subprocess")


@flow("pipeline-patrol", desc="【值守·管线】L3 巡检（远端执行点）：台账/metrics/keeper 日志→观测报告+契约变更提案；ctrl 队列 0 */4")
def pipeline_patrol(INPUT):
    # ── ① facts（IO 叶子：ssh 远端跑 patrol + 提案/新鲜度清点，全只读）────
    facts = CODE(id="facts", lang="python", input={
        "ik_repo": INPUT.ik_repo, "ssh_host": INPUT.ssh_host,
    }, code="""
def run(input):
    import json, subprocess, time

    HOST = input.get("ssh_host") or "tcloud_gz"
    REPO = input.get("ik_repo") or "/home/ubuntu/projects/infra4agent/issue-keeper"

    def sh(cmd, timeout=180):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return (r.stdout or "").strip(), (r.stderr or "").strip(), r.returncode
        except Exception as e:
            return "", str(e), -1

    # 1) 远端跑 patrol（数据所在机器）
    out, err, rc = sh("ssh -o ConnectTimeout=8 %s 'cd %s && python3 flows/supervisor_patrol.py 2>&1 | tail -25'" % (HOST, REPO))
    # 2) 提案清点
    pj, _, _ = sh("ssh -o ConnectTimeout=8 %s 'cat ~/.issue-keeper/pipeline/proposals/*.json 2>/dev/null | head -200'" % HOST)
    proposals = []
    for line in (pj or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                proposals.append(json.loads(line))
            except Exception:
                pass
    # 3) 台账新鲜度（管线是否在动）
    ft, _, _ = sh("ssh -o ConnectTimeout=8 %s 'stat -c %%Y ~/.issue-keeper/pipeline/runs.jsonl 2>/dev/null; "
                  "tail -1 ~/.issue-keeper/pipeline/runs.jsonl 2>/dev/null'" % HOST)
    lines = [l for l in (ft or "").splitlines() if l.strip()]
    ledger_age_min = None
    last_run_ts = ""
    if lines and lines[0].strip().isdigit():
        ledger_age_min = int((time.time() - int(lines[0].strip())) / 60)
    if len(lines) > 1:
        try:
            last_run_ts = str(json.loads(lines[1]).get("ts", ""))[:16]
        except Exception:
            last_run_ts = lines[1][:40]

    return {"patrol_out": out[-1500:], "patrol_err": err[-400:], "patrol_rc": rc,
            "proposals": proposals, "ledger_age_min": ledger_age_min,
            "last_run_ts": last_run_ts}
""")

    # ── ② triage（纯规则）──────────────────────────────────────────────
    triage = CODE(id="triage", lang="python", input={
        "patrol_rc": NODE.facts.patrol_rc, "patrol_err": NODE.facts.patrol_err,
        "patrol_out": NODE.facts.patrol_out, "proposals": NODE.facts.proposals,
        "ledger_age_min": NODE.facts.ledger_age_min, "last_run_ts": NODE.facts.last_run_ts,
        "idle_hours": INPUT.idle_hours,
    }, code="""
def run(input):
    findings = []
    props = input.get("proposals") or []
    if input.get("patrol_rc") not in (0, "0"):
        findings.append({"severity": "critical",
                         "summary": "远端 patrol 执行失败（rc=%s）：%s" % (input.get("patrol_rc"), str(input.get("patrol_err"))[:160]),
                         "escalate": True})
    numeric, manual = [], []
    for p in props:
        kind = str(p.get("kind") or p.get("type") or "?")
        pid = str(p.get("id") or "?")[:12]
        (numeric if p.get("auto_appliable") or p.get("numeric") else manual).append("%s(%s)" % (pid, kind))
    if props:
        findings.append({"severity": "warn",
                         "summary": "待审契约提案 %d 条（可数值自动应用 %d / 需人工 %d）：%s"
                                    % (len(props), len(numeric), len(manual), ", ".join((numeric + manual)[:6])),
                         "escalate": False})
    idle_h = float(input.get("idle_hours") or 12)
    age_min = input.get("ledger_age_min")
    if age_min is not None and age_min > idle_h * 60:
        findings.append({"severity": "warn",
                         "summary": "管线台账 %d 分钟无新增（最后一条 %s）——可能闲置或派发停摆"
                                    % (age_min, input.get("last_run_ts") or "?"),
                         "escalate": age_min > 24 * 60})
    report = "提案 %d（数值 %d/人工 %d）· 台账龄 %s min · 末单 %s" % (
        len(props), len(numeric), len(manual),
        age_min if age_min is not None else "?", input.get("last_run_ts") or "?")
    return {"findings": findings, "numeric": numeric, "manual": manual,
            "report": report, "patrol_out": input.get("patrol_out")}
""")

    # ── ③ finish（duty state-patrol + rounds.log 一行）──────────────────
    finish = CODE(id="finish", lang="python", input={
        "report": NODE.triage.report, "findings": NODE.triage.findings,
        "patrol_out": NODE.triage.patrol_out, "role": "patrol",
        "duty_dir": INPUT.duty_dir,
    }, code="""
def run(input):
    import fcntl, json, os, tempfile, time
    duty = os.path.expanduser(input.get("duty_dir") or "~/.issue-keeper/duty")
    os.makedirs(duty, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    findings = input.get("findings") or []
    spath = os.path.join(duty, "state-patrol.json")
    rd = 1
    try:
        doc = json.load(open(spath))
        rd = max([r.get("round", 0) for r in (doc.get("rounds") or [])] or [0]) + 1
    except Exception:
        pass
    status = "attention" if findings else "ok"
    round_doc = {"schema_version": "duty/state@0", "role": "patrol", "generation": 2,
                 "round": rd, "started_at": ts, "finished_at": ts, "status": status,
                 "findings": findings,
                 "narrative": (str(input.get("report") or "") + " ｜ " + str(input.get("patrol_out") or "")[-300:])[:600]}
    def atomic(path, d):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(d, indent=1, ensure_ascii=False))
        os.replace(tmp, path)
    with open(spath + ".lock", "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            doc = {"schema_version": "duty/state@0", "role": "patrol", "rounds": []}
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
        fh.write("%s pipeline-patrol 轮次=%s 状态=%s findings=%s 摘要=%s\\n"
                 % (ts, rd, status, len(findings), str(input.get("report"))[:110]))
    return {"round": rd, "status": status}
""")

    return {"round": NODE.finish.round, "status": NODE.finish.status}


if __name__ == "__main__":
    print("pipeline-patrol flow 源码（编译见 build_pipeline_patrol.py）")
