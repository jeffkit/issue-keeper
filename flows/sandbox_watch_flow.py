#!/usr/bin/env python3
"""sandbox-watch —— 沙箱值守 flow（v0.1，ctrl 队列专用）。

为什么有它（2026-10-09 夜实证）：
- **孤儿实例烧钱**：run 06:27 就完成，实例却 running 到 08:00（1.6h 纯浪费）；
  24h 统计里 cancelled 轮吃掉 19.1 实例小时（占总用量 65%）——成本可见性必须常驻。
- **静默卡死**：某 sbx run 82 分钟零节点进展、租约照续、不报错（#83 看门狗想解但
  未覆盖 console/沙箱路径）——值守侧需要一条「卡死即报」的规则。

与 launchd `ags-orphan-sweep` 的分工（有意并存）：
- 本 flow = **值守语义**：业务级留痕（duty round + 拓扑可见）、卡死判定、配额压力预警；
- launchd sweep = **独立兜底**：不依赖调度器进程，纯成本安全网。
两者都只杀「run 已终态」的实例，对在跑零风险。

图结构（纯 CODE，零 LLM；同 ctrl-watch 纪律）：
  facts（CODE：AGS 实例清单 + 每个实例的执行态/最后更新，全部只读）
    → triage（CODE：孤儿 / 卡死 / 配额压力 三类规则）
    → act（CODE：杀孤儿（复用 ags-orphan-sweep.py）；卡死只报不动）
    → finish（CODE：duty state-sandbox 滚动窗口 + rounds.log 一行）

编译：PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_sandbox_watch.py
"""
from __future__ import annotations

from plaita.dsl.codeflow import CODE, NODE, flow
from plaita.node import register_code_node

register_code_node(default_backend="subprocess")


@flow("sandbox-watch", desc="【值守·沙箱】AGS 实例巡检：孤儿清查（终态 run 的实例）+ 静默卡死判定 + 配额压力；ctrl 队列 */30")
def sandbox_watch(INPUT):
    # ── ① facts（IO 叶子：只读；plaita venv 子进程查 AGS + console 执行态）─────
    facts = CODE(id="facts", lang="python", input={
        "venv_py": INPUT.venv_py, "lister": INPUT.lister, "console_key": INPUT.console_key,
    }, code="""
def run(input):
    import json, subprocess, time, urllib.request

    VENV = input.get("venv_py") or "/Users/kong/projects/infra4agent/plaita/.venv/bin/python"
    LISTER = input.get("lister") or "/Users/kong/projects/infra4agent/issue-keeper/flows/ags-list.py"
    KEY = input.get("console_key") or "b4b5042ee7d1b937633c08f3f50d4c8efbca88d33ece8a03"
    ENV = ("E2B_DOMAIN=ap-guangzhou.tencentags.com "
           "E2B_API_KEY=e2b_725235357335be8d27367c596c9e3199cf3c5eeb ")

    def sh(cmd, timeout=60):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return (r.stdout or "").strip()
        except Exception as e:
            return ""

    out = sh(ENV + VENV + " " + LISTER)
    try:
        items = json.loads(out)
    except Exception:
        items = []

    now = time.time()
    rows = []
    for it in items:
        eid = it.get("exec") or ""
        st, upd_age = "unknown", None
        if eid:
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:8323/api/executions/" + eid,
                    headers={"X-Admin-API-Key": KEY})
                d = json.load(urllib.request.urlopen(req, timeout=8))
                st = str(d.get("status") or "unknown")
                ts = d.get("last_update_time") or d.get("start_time")
                if ts:
                    import datetime
                    try:
                        upd = datetime.datetime.fromisoformat(str(ts)[:19])
                        upd_age = int(now - upd.timestamp())
                    except Exception:
                        upd_age = None
            except Exception as e:
                st = "query-failed"
        rows.append({**it, "exec_status": st, "exec_update_age_sec": upd_age})
    return {"instances": rows, "count": len(rows), "now": now, "error": None if items else out[:200]}
""")

    # ── ② triage（纯规则）──────────────────────────────────────────────
    triage = CODE(id="triage", lang="python", input={
        "instances": NODE.facts.instances, "count": NODE.facts.count,
        "error": NODE.facts.error,
        "orphan_min_age_h": INPUT.orphan_min_age_h, "stall_min": INPUT.stall_min,
        "quota_warn": INPUT.quota_warn,
    }, code="""
def run(input):
    TERMINAL = {"completed", "failed", "cancelled", "error"}
    inst = input.get("instances") or []
    orphan_h = float(input.get("orphan_min_age_h") or 0.5)
    stall_min = float(input.get("stall_min") or 30)
    quota_warn = int(input.get("quota_warn") or 6)

    orphans, stalled, findings = [], [], []
    for it in inst:
        age = it.get("age_h") or 0
        st = it.get("exec_status") or "unknown"
        upd = it.get("exec_update_age_sec")
        if (st in TERMINAL or st in ("unknown", "query-failed")) and age >= orphan_h:
            orphans.append(it)
        if st == "running" and upd is not None and upd > stall_min * 60 and age >= 0.75:
            stalled.append(it)
    for it in stalled:
        findings.append({"severity": "warn",
                         "summary": "疑似静默卡死：实例 %s 存活 %sh，其执行 %s running 但 %s 分钟无节点更新"
                                    % (it.get("short"), it.get("age_h"), (it.get("exec") or "?")[:8],
                                       int((it.get("exec_update_age_sec") or 0) / 60)),
                         "escalate": False})
    if len(inst) >= quota_warn:
        findings.append({"severity": "warn",
                         "summary": "AGS 实例数 %d ≥ %d，配额压力（成本/并发受限）" % (len(inst), quota_warn),
                         "escalate": False})
    if input.get("error"):
        findings.append({"severity": "critical",
                         "summary": "AGS 实例清单获取失败：%s" % str(input.get("error"))[:120],
                         "escalate": True})

    report = ("实例 %d（孤儿 %d / 卡死 %d）" % (len(inst), len(orphans), len(stalled))) \
        if inst or not input.get("error") else "清单失败"
    return {"orphans": orphans, "stalled": stalled, "findings": findings, "report": report}
""")

    # ── ③ act（只杀孤儿：复用已验证的 ags-orphan-sweep.py）──────────────
    act = CODE(id="act", lang="python", input={
        "orphans": NODE.triage.orphans, "venv_py": INPUT.venv_py,
        "sweeper": INPUT.sweeper, "dryrun": INPUT.dryrun,
    }, code="""
def run(input):
    import subprocess
    VENV = input.get("venv_py") or "/Users/kong/projects/infra4agent/plaita/.venv/bin/python"
    SWEEP = input.get("sweeper") or "/Users/kong/projects/infra4agent/issue-keeper/flows/ags-orphan-sweep.py"
    n_orphans = len(input.get("orphans") or [])
    if not n_orphans:
        return {"killed": 0, "out": "无孤儿"}
    if input.get("dryrun"):
        return {"killed": 0, "out": "dryrun：将清 %d 个孤儿" % n_orphans}
    cmd = "%s %s --min-age 0.5" % (VENV, SWEEP)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        killed = out.count("→ kill=True")
        return {"killed": killed, "out": out[-600:]}
    except Exception as e:
        return {"killed": 0, "out": "sweep 异常: %s" % str(e)[:200]}
""")

    # ── ④ finish（duty state-sandbox + rounds.log 一行）─────────────────
    finish = CODE(id="finish", lang="python", input={
        "report": NODE.triage.report, "findings": NODE.triage.findings,
        "killed": NODE.act.killed, "act_out": NODE.act.out,
        "role": "sandbox", "duty_dir": INPUT.duty_dir,
    }, code="""
def run(input):
    import fcntl, json, os, tempfile, time
    duty = os.path.expanduser(input.get("duty_dir") or "~/.issue-keeper/duty")
    os.makedirs(duty, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    findings = input.get("findings") or []
    killed = int(input.get("killed") or 0)
    spath = os.path.join(duty, "state-sandbox.json")
    rd = 1
    try:
        doc = json.load(open(spath))
        rd = max([r.get("round", 0) for r in (doc.get("rounds") or [])] or [0]) + 1
    except Exception:
        pass
    status = "attention" if findings else "ok"
    round_doc = {"schema_version": "duty/state@0", "role": "sandbox", "generation": 2,
                 "round": rd, "started_at": ts, "finished_at": ts, "status": status,
                 "findings": findings,
                 "actions": ([{"capability": "delete_cache", "level": "authorized",
                               "summary": "杀孤儿沙箱实例 %d 个" % killed, "target": "ags"}]
                             if killed else []),
                 "narrative": str(input.get("report") or "")[:200]}
    def atomic(path, d):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(d, indent=1, ensure_ascii=False))
        os.replace(tmp, path)
    with open(spath + ".lock", "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            doc = {"schema_version": "duty/state@0", "role": "sandbox", "rounds": []}
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
        fh.write("%s sandbox-watch 轮次=%s 状态=%s findings=%s 击杀=%s 摘要=%s\\n"
                 % (ts, rd, status, len(findings), killed, str(input.get("report"))[:100]))
    return {"round": rd, "status": status, "killed": killed}
""")

    return {"round": NODE.finish.round, "killed": NODE.finish.killed}


if __name__ == "__main__":
    print("sandbox-watch flow 源码（编译见 build_flows.py sandbox-watch）")
