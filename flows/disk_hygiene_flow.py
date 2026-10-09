#!/usr/bin/env python3
"""disk-hygiene —— 磁盘卫生 + 守卫解封（v0.1，ctrl 队列专用）。

起因（2026-10-09 实测，阻塞级）：VM 可用磁盘 19.7GiB < `RECURSIVE_MIN_FREE_DISK_GIB`
（recursive self-improve preflight 的守卫，默认 **20**）→ 每条 plaita run 开跑前被挡回
`retry-later（disk 19.7GiB < min）`，每次退避 **21600s（6h）**：

    plaita#28 streak=9 / #31 streak=8 / #32 streak=7 / #34 streak=8  ← 累计被挡 30+ 次
    等于 plaita 整条线瘫痪，且**看门狗全都不报**（执行没启动，不是卡死、不是孤儿）。

磁盘大头（实测）：`<repo>/.flowcast/runs/*` 是 self-improve 的 run worktree（含整仓副本），
旧件累计 1.7G；陈旧构建目录 ~1.3G；包缓存 ~0.3G。

本 flow 的职责（保守优先，只做可逆/低风险动作）：
1. **观测**：VM 可用磁盘 + 守卫阈值 + 被守卫挡住的在册条目（retry_after 在未来且 streak≥2）；
2. **清理**：仅当可用 < 阈值+余量时，删 `<repo>/.flowcast/runs/` 下 **mtime > keep_hours**
   （默认 24h）的 run worktree——在跑/近期的一律不碰；WIP 快照在 git 分支上，不受影响；
3. **解封**：把被守卫挡住条目的 `retry_after` 清零（**先备份 state.json**；keeper 每轮重载
   state 且保存走三方合并，外部清零安全），让流水线不等 6h 立刻恢复；
4. 释放量/解封清单写入 duty 轮报；critical 推 HITL。

编译：PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_flows.py disk-hygiene
"""
from __future__ import annotations

from plaita.dsl.codeflow import CODE, NODE, flow
from plaita.node import register_code_node

register_code_node(default_backend="subprocess")


@flow("disk-hygiene", desc="【值守·环境】磁盘卫生 + 守卫解封：清陈旧 run worktree、清磁盘守卫退避，防流水线因磁盘被挡 6h；ctrl */30")
def disk_hygiene(INPUT):
    # ── ① facts（ssh 观测磁盘/阈值/挡单清单，全只读）────────────────────
    facts = CODE(id="facts", lang="python", input={
        "ssh_host": INPUT.ssh_host, "min_free_gib": INPUT.min_free_gib,
    }, code="""
def run(input):
    import json, subprocess

    HOST = input.get("ssh_host") or "tcloud_gz"
    remote = r'''
import json, os, time
st = json.load(open("/home/ubuntu/.issue-keeper/state.json"))
now = time.time()
blocked = []
for slug, rv in (st.get("repos") or {}).items():
    for num, it in (rv.get("items") or {}).items():
        ra = it.get("retry_after")
        if ra and ra > now:
            blocked.append({"repo": slug.replace("jeffkit-", ""), "num": str(num),
                            "until": ra, "streak": it.get("retry_later_streak") or 0})
stt = os.statvfs("/home/ubuntu")
free_gib = (stt.f_bavail * stt.f_frsize) / 2**30
# run worktree 体积与年龄
runs = []
for base in os.listdir("/home/ubuntu/projects/infra4agent"):
    d = "/home/ubuntu/projects/infra4agent/%s/.flowcast/runs" % base
    if os.path.isdir(d):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isdir(p):
                runs.append({"path": p, "age_h": round((now - os.stat(p).st_mtime) / 3600, 1)})
print(json.dumps({"free_gib": round(free_gib, 1), "blocked": blocked,
                  "runs": runs, "n_runs": len(runs)}, ensure_ascii=False))
'''
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", HOST, "python3 -"],
                           input=remote, capture_output=True, text=True, timeout=60)
        d = json.loads((r.stdout or "{}").strip() or "{}")
    except Exception as e:
        return {"error": "ssh/state 读取失败: %s" % str(e)[:120], "free_gib": None,
                "blocked": [], "runs": [], "n_runs": 0}
    d["error"] = None
    d["min_free_gib"] = float(input.get("min_free_gib") or 20)
    return d
""")

    # ── ② triage（阈值/挡单分级）──────────────────────────────────────
    triage = CODE(id="triage", lang="python", input={
        "free_gib": NODE.facts.free_gib, "min_free_gib": NODE.facts.min_free_gib,
        "blocked": NODE.facts.blocked, "n_runs": NODE.facts.n_runs, "error": NODE.facts.error,
        "margin_gib": INPUT.margin_gib,
    }, code="""
def run(input):
    findings = []
    free = input.get("free_gib")
    mn = float(input.get("min_free_gib") or 20)
    margin = float(input.get("margin_gib") or 5)
    blocked = input.get("blocked") or []
    disk_blocked = [b for b in blocked if (b.get("streak") or 0) >= 2]
    if input.get("error"):
        findings.append({"severity": "critical", "summary": str(input.get("error"))[:150],
                         "escalate": True})
    if free is not None:
        if free < mn:
            findings.append({"severity": "critical",
                             "summary": "VM 可用磁盘 %.1fGiB < 守卫阈值 %.0fGiB——self-improve 全线会被挡回 6h"
                                        % (free, mn), "escalate": True})
        elif free < mn + margin:
            findings.append({"severity": "warn",
                             "summary": "VM 可用磁盘 %.1fGiB 逼近守卫阈值 %.0fGiB（余量 <%.0f）"
                                        % (free, mn, margin), "escalate": False})
    if disk_blocked:
        findings.append({"severity": "critical",
                         "summary": "被守卫挡住的条目 %d 条（退避最长 6h）：%s"
                                    % (len(disk_blocked),
                                       ", ".join("%s#%s(streak=%s)" % (b["repo"], b["num"], b["streak"])
                                                 for b in disk_blocked[:6])),
                         "escalate": True})
    need_clean = free is not None and free < mn + margin
    report = "可用 %.1fGiB（阈值 %.0f）· 挡单 %d · run worktree %d 个" % (
        free if free is not None else -1, mn, len(disk_blocked), input.get("n_runs") or 0)
    return {"findings": findings, "need_clean": need_clean, "disk_blocked": disk_blocked,
            "report": report}
""")

    # ── ③ act（条件清理 + 解封；先备份 state）────────────────────────
    act = CODE(id="act", lang="python", input={
        "need_clean": NODE.triage.need_clean, "disk_blocked": NODE.triage.disk_blocked,
        "ssh_host": INPUT.ssh_host, "keep_hours": INPUT.keep_hours, "dryrun": INPUT.dryrun,
    }, code="""
def run(input):
    import json, subprocess

    HOST = input.get("ssh_host") or "tcloud_gz"
    keep = float(input.get("keep_hours") or 24)
    if input.get("dryrun"):
        return {"freed_gib": 0, "removed": 0, "cleared": [], "why": "dryrun"}
    need_clean = bool(input.get("need_clean"))
    blocked = input.get("disk_blocked") or []

    remote = r'''
import json, os, shutil, subprocess, tempfile, time
need_clean = %s
keep_h = %s
cleared = []
removed = 0
before = os.statvfs("/home/ubuntu")
if need_clean:
    cutoff = time.time() - keep_h * 3600
    for base in os.listdir("/home/ubuntu/projects/infra4agent"):
        d = "/home/ubuntu/projects/infra4agent/%%s/.flowcast/runs" %% base
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isdir(p) and os.stat(p).st_mtime < cutoff:
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
# 解封：清被守卫挡住的 retry_after（先备份）
p = "/home/ubuntu/.issue-keeper/state.json"
shutil.copy(p, p + ".bak-diskhygiene")
doc = json.load(open(p))
now = time.time()
for slug, rv in (doc.get("repos") or {}).items():
    for num, it in (rv.get("items") or {}).items():
        ra = it.get("retry_after")
        if ra and ra > now and (it.get("retry_later_streak") or 0) >= 2:
            cleared.append("%%s#%%s" %% (slug.replace("jeffkit-", ""), num))
            it["retry_after"] = None
            it["retry_later_streak"] = 0
if cleared:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".tmp-state-")
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, ensure_ascii=False)
    os.replace(tmp, p)
after = os.statvfs("/home/ubuntu")
freed = ((after.f_bavail - before.f_bavail) * after.f_frsize) / 2**30
print(json.dumps({"freed_gib": round(freed, 2), "removed": removed, "cleared": cleared}))
''' % (repr(need_clean), repr(keep))
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", HOST, "python3 -"],
                           input=remote, capture_output=True, text=True, timeout=300)
        return json.loads((r.stdout or "{}").strip().splitlines()[-1] or "{}")
    except Exception as e:
        return {"freed_gib": 0, "removed": 0, "cleared": [], "error": str(e)[:120]}
""")

    # ── ④ finish（duty 轮报 + critical 推 HITL）──────────────────────
    finish = CODE(id="finish", lang="python", input={
        "report": NODE.triage.report, "findings": NODE.triage.findings,
        "freed_gib": NODE.act.freed_gib, "removed": NODE.act.removed,
        "cleared": NODE.act.cleared, "requests_script": INPUT.requests_script,
        "hitl_wait_secs": INPUT.hitl_wait_secs, "duty_dir": INPUT.duty_dir,
    }, code="""
def run(input):
    import fcntl, json, os, subprocess, tempfile, time
    duty = os.path.expanduser(input.get("duty_dir") or "~/.issue-keeper/duty")
    os.makedirs(duty, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    findings = input.get("findings") or []
    crit = [f for f in findings if f.get("severity") == "critical"]
    spath = os.path.join(duty, "state-disk.json")
    rd = 1
    try:
        doc = json.load(open(spath))
        rd = max([r.get("round", 0) for r in (doc.get("rounds") or [])] or [0]) + 1
    except Exception:
        pass
    status = "critical" if crit else ("attention" if findings else "ok")
    narrative = "%s ｜ 清理 %s 个（释放 %.2fGiB）｜ 解封 %s" % (
        input.get("report"), input.get("removed"), input.get("freed_gib") or 0,
        ",".join(input.get("cleared") or []) or "无")
    round_doc = {"schema_version": "duty/state@0", "role": "disk", "generation": 2,
                 "round": rd, "started_at": ts, "finished_at": ts, "status": status,
                 "findings": findings,
                 "actions": ([{"capability": "delete_cache", "level": "authorized",
                               "summary": narrative[:200], "target": "vm-disk"}]),
                 "narrative": narrative[:300]}
    def atomic(path, d):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(d, indent=1, ensure_ascii=False))
        os.replace(tmp, path)
    with open(spath + ".lock", "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            doc = {"schema_version": "duty/state@0", "role": "disk", "rounds": []}
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
        fh.write("%s disk-hygiene 轮次=%s 状态=%s findings=%s 释放=%.2fGiB 清理=%s 解封=%s\\n"
                 % (ts, rd, status, len(findings), input.get("freed_gib") or 0,
                    input.get("removed") or 0, len(input.get("cleared") or [])))
    # 三层协同：critical 先递工单给值守 Agent（不再直接推人）。
    req = {"id": None, "why": "无 critical"}
    if crit:
        try:
            rs = input.get("requests_script") or "/Users/kong/projects/infra4agent/issue-keeper/flows/duty_request.py"
            ctx = json.dumps({"flow": "disk-hygiene", "report": input.get("report"),
                              "findings": [f.get("summary") for f in crit[:6]],
                              "freed_gib": input.get("freed_gib"), "cleared": input.get("cleared")},
                             ensure_ascii=False)
            cmd = ("python3 %s create --from-flow disk-hygiene --kind disk-guard "
                   "--severity critical --title %s --context-json %s --options clean_disk,escalate_human"
                   % (rs, json.dumps("[磁盘] " + str(input.get("report"))[:60], ensure_ascii=False).replace("'", ""),
                      json.dumps(ctx, ensure_ascii=False).replace("'", "")))
            rr = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
            req = json.loads((rr.stdout or "{}").strip().splitlines()[-1]) if rr.stdout.strip() else {"id": None}
        except Exception as e:
            req = {"id": None, "why": str(e)[:120]}
    return {"round": rd, "status": status, "request": req}
""")

    return {"round": NODE.finish.round, "status": NODE.finish.status}


if __name__ == "__main__":
    print("disk-hygiene flow 源码（编译见 build_flows.py）")
