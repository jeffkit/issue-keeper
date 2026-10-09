#!/usr/bin/env python3
"""inflight-watch —— 在途 run 进度巡检（v0.1，ctrl 队列专用）。

为什么需要它（2026-10-09 plaita#28 实证，jeffkit：「太久了，不正常。日常是否也应该有这种巡检？」）：
现有五层看门狗都**看不到"某个 run 卡住了"**：
| 层 | 谁在盯 | 漏什么 |
|---|---|---|
| flow 心跳 | ctrl-watch */30 | 只看 A/B/主控 flow 有没有跑，不看单条 run |
| AGS 实例 | sandbox-watch */30 | 只看沙箱实例（孤儿/实例级卡死） |
| keeper 僵尸线 | keeper reaper | 判据=「status=running 且 `last_update_time` 年龄 > `console_zombie_secs`（默认 **2h**）」——
  | 阈值取 2h 是为容忍长节点（impl 60-120min），所以**失败后要等满 2h** 才收尸（#28 等了 229 分钟） |
| 管线统计 | pipeline-patrol 0 */4 | 4h 一次，且只看已终态 run 的聚合 |
| **run 进度** | **无人** | **console 说 running、87 分钟零节点进展 → 无人发现** |

#28 的完整病灶（本 flow 要抓的形态）：
1) 07:35 派发到 self-improve-v2-sbx；2) 节点 _n17 遇 `AgsError: 分块上传失败（块 0）：
   printf: write error` 重试 4 次后失败（≈09:50）；3) **console 状态没落终态**，一直 running；
4) keeper 等终态 → 一等 87 分钟；5) 我手工 resume 才把它翻成 error（终端状态滞后）。

规则（保守优先：只报、只 resume 一次，不自动 cancel/杀进程）：
- `terminal_lag`：console 已终态但 keeper 仍算在途 → critical（keeper 该收尸没收）
- `stalled`：exec running 且进度龄 > stall_min（默认 30）→ warn；> hard_min（默认 45）→ critical
- `long_run`：在途 > long_min（默认 180）→ warn（长跑未必异常，但值得人看一眼）
- 自动动作：对 `stalled` **每个执行只 resume 一次**（救回 #27 那类；对 #28 这类能把
  终态滞后暴露出来），其余交人/交 keeper。

编译：PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_flows.py inflight-watch
"""
from __future__ import annotations

from plaita.dsl.codeflow import CODE, NODE, flow
from plaita.node import register_code_node

register_code_node(default_backend="subprocess")


@flow("inflight-watch", desc="【值守·在途】run 级进度巡检：状态滞后/零进展/超长跑；每执行最多自动 resume 一次；ctrl 队列 */15")
def inflight_watch(INPUT):
    # ── ① facts（一次 ssh 取 keeper 在途 + 各单执行 id；再逐个查 console/AGS）────
    facts = CODE(id="facts", lang="python", input={
        "ssh_host": INPUT.ssh_host, "console_key": INPUT.console_key,
        "venv_py": INPUT.venv_py, "lister": INPUT.lister,
    }, code="""
def run(input):
    import json, subprocess, urllib.request

    HOST = input.get("ssh_host") or "tcloud_gz"
    KEY = input.get("console_key") or "b4b5042ee7d1b937633c08f3f50d4c8efbca88d33ece8a03"
    VENV = input.get("venv_py") or "/Users/kong/projects/infra4agent/plaita/.venv/bin/python"
    LISTER = input.get("lister") or "/Users/kong/projects/infra4agent/issue-keeper/flows/ags-list.py"

    remote = r'''
import json, os, time
st = json.load(open("/home/ubuntu/.issue-keeper/state.json"))
rows = []
for slug, rv in (st.get("repos") or {}).items():
    for num, it in (rv.get("items") or {}).items():
        ifs = it.get("in_flight_since")
        if not ifs:
            continue
        short = slug.replace("jeffkit-", "")
        d = os.path.expanduser("~/.issue-keeper/pipeline/%s-%s" % (short, num))
        eid = ""
        try:
            eid = json.load(open(os.path.join(d, "console-exec.json"))).get("execution_id", "")
        except Exception:
            pass
        flow_id = ""
        try:
            flow_id = json.load(open(os.path.join(d, "console-exec.json"))).get("flow_id", "")
        except Exception:
            pass
        rows.append({"repo": short, "num": str(num), "in_flight_min": round((time.time() - ifs) / 60),
                     "exec_id": eid, "flow_id": flow_id, "retry_after": it.get("retry_after")})
print(json.dumps(rows, ensure_ascii=False))
'''
    try:
        # 脚本经 stdin 传给远端 python（避免 shell 转义吃掉引号——2026-10-09 实测踩过）
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", HOST, "python3 -"],
                           input=remote, capture_output=True, text=True, timeout=60)
        rows = json.loads((r.stdout or "[]").strip() or "[]")
    except Exception as e:
        rows = []
        return {"rows": [], "error": "ssh/state 读取失败: %s" % str(e)[:120], "instances": []}

    def api(path):
        try:
            req = urllib.request.Request("http://127.0.0.1:8323/api" + path,
                                         headers={"X-Admin-API-Key": KEY})
            return json.load(urllib.request.urlopen(req, timeout=10))
        except Exception:
            return {}

    for it in rows:
        e = api("/executions/" + it["exec_id"]) if it.get("exec_id") else {}
        it["exec_status"] = str(e.get("status") or "unknown")
        nt = e.get("node_timings") or {}
        it["nodes"] = len(nt)
        # 进度信号优先级：last_update_time → 任一节点 ended_at/started_at 的最新值
        # （实测：新起 run 的 node_timings 为空且无 last_update_time，只看它会把
        #  进度龄算成 None 造成盲区；回退到节点时间戳/起点时间兜底）
        import datetime
        cands = [str(e.get("last_update_time") or ""), str(e.get("updated_at") or "")]
        for v in nt.values():
            if isinstance(v, dict):
                cands += [str(v.get("ended_at") or ""), str(v.get("started_at") or "")]
        cands += [str(e.get("start_time") or "")]
        # 时区纪律：console 返回的时间戳多为**本地无时区**字符串（实测把它当 UTC 会
        # 得到负的进度龄，差 8h）——统一折算成本地 naive 再比，aware 的先转本地。
        best = None
        for c in cands:
            c = c.strip()
            if len(c) < 19:
                continue
            try:
                t = datetime.datetime.fromisoformat(c.replace("Z", "+00:00"))
            except Exception:
                continue
            if t.tzinfo is not None:
                t = t.astimezone().replace(tzinfo=None)
            if best is None or t > best:
                best = t
        it["progress_age_min"] = None
        if best is not None:
            it["progress_age_min"] = round((datetime.datetime.now() - best).total_seconds() / 60)
        it["progress_signal"] = "none" if best is None else "ts"
        # 关键区分（2026-10-09 实测）：impl 类节点单次跑 60-120 分钟且中途不写状态，
        # 所以「进度龄大」本身正常。真正异常的是**节点已结束却迟迟不开下一个**
        # （#28：11 个节点停在 09:50，之后 100 分钟无任何动作）。
        in_prog = [v for v in nt.values() if isinstance(v, dict) and v.get("started_at") and not v.get("ended_at")]
        it["has_node_ts"] = len(nt) > 0
        it["in_progress_nodes"] = len(in_prog)
        it["last_node_ended_age_min"] = None
        if nt and not in_prog:
            ends = [str(v.get("ended_at")) for v in nt.values() if isinstance(v, dict) and v.get("ended_at")]
            best_end = None
            for c in ends:
                try:
                    t = datetime.datetime.fromisoformat(c.replace("Z", "+00:00"))
                except Exception:
                    continue
                if t.tzinfo is not None:
                    t = t.astimezone().replace(tzinfo=None)
                if best_end is None or t > best_end:
                    best_end = t
            if best_end is not None:
                it["last_node_ended_age_min"] = round((datetime.datetime.now() - best_end).total_seconds() / 60)
        err = e.get("error")
        if isinstance(err, dict):
            it["last_error"] = str(err.get("message") or "")[:200]
        elif err:
            it["last_error"] = str(err)[:200]

    sbx = []
    try:
        r2 = subprocess.run(
            "E2B_DOMAIN=ap-guangzhou.tencentags.com "
            "E2B_API_KEY=e2b_725235357335be8d27367c596c9e3199cf3c5eeb %s %s" % (VENV, LISTER),
            shell=True, capture_output=True, text=True, timeout=90)
        sbx = json.loads((r2.stdout or "[]").strip() or "[]")
    except Exception:
        pass
    return {"rows": rows, "instances": sbx, "error": None}
""")

    # ── ② triage（三类规则 + 找可救的 stalled）──────────────────────────
    triage = CODE(id="triage", lang="python", input={
        "rows": NODE.facts.rows, "instances": NODE.facts.instances,
        "error": NODE.facts.error,
        "stall_min": INPUT.stall_min, "hard_min": INPUT.hard_min, "long_min": INPUT.long_min,
    }, code="""
def run(input):
    TERMINAL = {"completed", "failed", "cancelled", "error"}
    rows = input.get("rows") or []
    inst = input.get("instances") or []
    stall = float(input.get("stall_min") or 30)
    hard = float(input.get("hard_min") or 45)
    long_min = float(input.get("long_min") or 180)

    findings, stalled, terminal_lag = [], [], []
    for it in rows:
        label = "%s#%s" % (it.get("repo"), it.get("num"))
        st = it.get("exec_status") or "unknown"
        pa = it.get("progress_age_min")
        age = it.get("in_flight_min") or 0
        if st in TERMINAL:
            terminal_lag.append(it)
            findings.append({"severity": "critical",
                             "summary": "%s 执行已终态（%s）但 keeper 仍算在途 %.0f 分钟——收尸滞后"
                                        % (label, st, age), "escalate": True})
            continue
        # ① 最强信号：有节点时间戳、无在跑节点、最后一个节点早已结束 → 流程该动没动
        lne = it.get("last_node_ended_age_min")
        if st == "running" and it.get("has_node_ts") and not it.get("in_progress_nodes") \
                and lne is not None and lne > stall:
            sev = "critical" if lne > hard else "warn"
            if sev == "critical":
                stalled.append(it)
            findings.append({"severity": sev,
                             "summary": "%s 末节点结束已 %.0f 分钟仍无下一个节点（在途 %.0f 分钟，%s 节点，flow=%s）"
                                        % (label, lne, age, it.get("nodes"), it.get("flow_id")),
                             "escalate": sev == "critical"})
        # ② 次强：无节点时间戳（长节点进行中，impl 常 60-120 分钟）→ 只在超长时报
        elif st == "running" and pa is not None and pa > long_min:
            findings.append({"severity": "warn",
                             "summary": "%s 无新节点 %.0f 分钟（在途 %.0f 分钟，flow=%s）——长节点进行中或卡死，需瞄一眼"
                                        % (label, pa, age, it.get("flow_id")), "escalate": False})
        elif age > long_min:
            findings.append({"severity": "warn",
                             "summary": "%s 在途 %.0f 分钟（长跑；进度龄 %s）"
                                        % (label, age, pa), "escalate": False})
    if input.get("error"):
        findings.append({"severity": "critical", "summary": str(input.get("error"))[:160],
                         "escalate": True})

    # 沙箱成本线索：终态执行却仍有活实例（孤儿），提示 sweep
    alive = {str(i.get("exec") or ""): i for i in inst if i.get("exec")}
    for it in terminal_lag:
        i2 = alive.get(it.get("exec_id") or "")
        if i2:
            findings.append({"severity": "warn",
                             "summary": "%s 终态但沙箱实例 %s 仍存活 %sh（孤儿，sweep 会清）"
                                        % ("%s#%s" % (it.get("repo"), it.get("num")),
                                           i2.get("short"), i2.get("age_h")),
                             "escalate": False})
    report = "在途 %d（终态滞后 %d / 零进展 %d）" % (len(rows), len(terminal_lag), len(stalled))
    return {"findings": findings, "stalled": stalled, "terminal_lag": terminal_lag,
            "report": report, "rows": rows}
""")

    # ── ③ act（每个执行最多自动 resume 一次；不 cancel、不杀进程）────────
    act = CODE(id="act", lang="python", input={
        "stalled": NODE.triage.stalled, "console_key": INPUT.console_key,
        "duty_dir": INPUT.duty_dir, "dryrun": INPUT.dryrun,
    }, code="""
def run(input):
    import json, os, time, urllib.request, fcntl

    KEY = input.get("console_key") or "b4b5042ee7d1b937633c08f3f50d4c8efbca88d33ece8a03"
    duty = os.path.expanduser(input.get("duty_dir") or "~/.issue-keeper/duty")
    os.makedirs(duty, exist_ok=True)
    statef = os.path.join(duty, "inflight-resume.json")
    if input.get("dryrun"):
        return {"resumed": [], "why": "dryrun"}
    done = {}
    try:
        done = json.load(open(statef)).get("resumed") or {}
    except Exception:
        pass
    resumed, skipped = [], []
    for it in input.get("stalled") or []:
        eid = it.get("exec_id") or ""
        label = "%s#%s" % (it.get("repo"), it.get("num"))
        if not eid or eid in done:
            skipped.append(label)
            continue
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8323/api/executions/%s/resume" % eid,
                data=json.dumps({"resume_type": "retry"}).encode(),
                headers={"X-Admin-API-Key": KEY, "Content-Type": "application/json"})
            resp = json.load(urllib.request.urlopen(req, timeout=20))
            done[eid] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "label": label,
                         "resp": str(resp.get("status") or resp)[:40]}
            resumed.append(label)
        except Exception as e:
            skipped.append("%s(%s)" % (label, str(e)[:40]))
    try:
        with open(statef + ".lock", "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            json.dump({"resumed": dict(list(done.items())[-200:])}, open(statef, "w"),
                      ensure_ascii=False, indent=1)
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    return {"resumed": resumed, "skipped": skipped}
""")

    # ── ④ finish（duty state-inflight + rounds.log + critical 时推 HITL）──
    finish = CODE(id="finish", lang="python", input={
        "report": NODE.triage.report, "findings": NODE.triage.findings,
        "resumed": NODE.act.resumed, "hitl_script": INPUT.hitl_script,
        "hitl_wait_secs": INPUT.hitl_wait_secs, "duty_dir": INPUT.duty_dir,
    }, code="""
def run(input):
    import fcntl, json, os, subprocess, tempfile, time
    duty = os.path.expanduser(input.get("duty_dir") or "~/.issue-keeper/duty")
    os.makedirs(duty, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    findings = input.get("findings") or []
    crit = [f for f in findings if f.get("severity") == "critical"]
    spath = os.path.join(duty, "state-inflight.json")
    rd = 1
    try:
        doc = json.load(open(spath))
        rd = max([r.get("round", 0) for r in (doc.get("rounds") or [])] or [0]) + 1
    except Exception:
        pass
    status = "critical" if crit else ("attention" if findings else "ok")
    round_doc = {"schema_version": "duty/state@0", "role": "inflight", "generation": 2,
                 "round": rd, "started_at": ts, "finished_at": ts, "status": status,
                 "findings": findings,
                 "actions": ([{"capability": "restart_worker", "level": "authorized",
                               "summary": "自动 resume 卡死执行：%s" % ", ".join(input.get("resumed") or [])}]
                             if input.get("resumed") else []),
                 "narrative": str(input.get("report") or "")[:200]}
    def atomic(path, d):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(d, indent=1, ensure_ascii=False))
        os.replace(tmp, path)
    with open(spath + ".lock", "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            doc = {"schema_version": "duty/state@0", "role": "inflight", "rounds": []}
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
        fh.write("%s inflight-watch 轮次=%s 状态=%s findings=%s resume=%s 摘要=%s\\n"
                 % (ts, rd, status, len(findings), len(input.get("resumed") or []),
                    str(input.get("report"))[:90]))
    hitl = {"sent": False, "why": "无 critical"}
    if crit:
        try:
            script = input.get("hitl_script") or "/Users/kong/projects/infra4agent/issue-keeper/flows/hitl_notify.py"
            body = "\\n".join("- " + f.get("summary", "") for f in crit[:5])
            import hashlib
            dkey = "inflight:" + hashlib.sha1(
                "|".join(sorted(f.get("summary", "") for f in crit)).encode()).hexdigest()[:12]
            cmd = ("python3 %s --title %s --body %s --dedupe-key %s --wait-secs %d "
                   "--feedback-url https://github.com/jeffkit/infra4agent/issues/2"
                   % (script, json.dumps("在途异常 " + str(input.get("report") or "")[:50], ensure_ascii=False).replace("'", ""),
                      json.dumps(body[:900], ensure_ascii=False).replace("'", ""),
                      dkey, int(input.get("hitl_wait_secs") or 0)))
            hr = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                timeout=max(60, int(input.get("hitl_wait_secs") or 0) + 60))
            hitl = json.loads((hr.stdout or "{}").strip().splitlines()[-1]) if hr.stdout.strip() else {"sent": False}
        except Exception as e:
            hitl = {"sent": False, "why": str(e)[:100]}
    return {"round": rd, "status": status, "hitl": hitl}
""")

    return {"round": NODE.finish.round, "status": NODE.finish.status}


if __name__ == "__main__":
    print("inflight-watch flow 源码（编译见 build_flows.py）")
