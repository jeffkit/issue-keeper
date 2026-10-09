#!/usr/bin/env python3
"""inflight-watch —— 在途 run 进度巡检（v0.1，ctrl 队列专用）。

为什么需要它（2026-10-09 plaita#28 实证，jeffkit：「太久了，不正常。日常是否也应该有这种巡检？」）：
现有五层看门狗都**看不到"某个 run 卡住了"**：
| 层 | 谁在盯 | 漏什么 |
|---|---|---|
| flow 心跳 | ctrl-watch */30 | 只看 A/B/主控 flow 有没有跑，不看单条 run |
| AGS 实例 | sandbox-watch */30 | 只看沙箱实例（孤儿/实例级卡死） |
| keeper 僵尸线 | keeper reaper | 判据=「running 且**活性确证死亡**：末节点 ended_at 停滞超 `console_node_stale_secs`（默认 **30min**），或无任何活性判据时 `last_update_time` 年龄 > `console_zombie_secs`（默认 **2h**），或在途越 `console_inflight_budget_secs` 总预算」——
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
        "terminal_lag_min": INPUT.terminal_lag_min,
    }, code="""
def run(input):
    TERMINAL = {"completed", "failed", "cancelled", "error"}
    rows = input.get("rows") or []
    inst = input.get("instances") or []
    stall = float(input.get("stall_min") or 30)
    hard = float(input.get("hard_min") or 45)
    long_min = float(input.get("long_min") or 180)
    lag_min = float(input.get("terminal_lag_min") or 10)

    findings, stalled, terminal_lag = [], [], []
    for it in rows:
        label = "%s#%s" % (it.get("repo"), it.get("num"))
        st = it.get("exec_status") or "unknown"
        pa = it.get("progress_age_min")
        age = it.get("in_flight_min") or 0
        if st in TERMINAL:
            # keeper 收尸是轮询制（轮间隔数分钟）。⚠️ 判据必须用**终态以来的时长**，
            # 而不是总在途时长——后者会让任何跑过 lag_min 的 run 一完成就误报
            # （2026-10-09 18:59 实测：recursive#148 刚落地推送 main，即被报成
            #  「已终态但 keeper 仍算在途 137 分钟」，而 keeper 只是还没轮到收尸）。
            lag = it.get("progress_age_min")
            if lag is None:
                lag = it.get("last_node_ended_age_min")
            if lag is not None and lag >= lag_min:
                terminal_lag.append(it)
                findings.append({"severity": "critical",
                                 "summary": "%s 执行已终态（%s）已 %.0f 分钟未收账（在途共 %.0f 分钟）——收尸滞后"
                                            % (label, st, lag, age), "escalate": True})
            continue
        # ① 末节点结束已久：**先分清长节点与卡死**——impl 类节点单跑 60-120 分钟且
        # 中途不写状态，所以「末节点结束 60 分钟」本身正常（2026-10-09 实测：据此
        # 30/45 分钟阈值把三条健康长节点的 run 连推 4 次 HITL，全是误报）。
        # 判据改为：越过长节点预算（stall=60 只 warn）后，再看进度龄是否也已越过
        # keeper 僵尸线（hard=120）——两者的交集才是「keeper 都收不走」的真卡死，
        # 此时才 critical + 自动 resume（避免 resume 打断健康长节点造成重复劳动）。
        lne = it.get("last_node_ended_age_min")
        pa2 = it.get("progress_age_min")
        if st == "running" and it.get("has_node_ts") and not it.get("in_progress_nodes") \
                and lne is not None and lne > stall:
            hard_stall = lne > hard and (pa2 is None or pa2 > hard)
            sev = "critical" if hard_stall else "warn"
            if sev == "critical":
                stalled.append(it)
            findings.append({"severity": sev,
                             "summary": "%s 末节点结束已 %.0f 分钟仍无下一个节点（在途 %.0f 分钟，进度龄 %s，%s 节点，flow=%s）%s"
                                        % (label, lne, age, pa2, it.get("nodes"), it.get("flow_id"),
                                           "——超过 keeper 僵尸线仍未收，判定真卡死" if hard_stall else "（长节点进行中或需留意）"),
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
    # ── ④ outcome（产出探针：活着 ≠ 出活）────────────────────────────
    # 2026-10-09 教训（jeffkit 点名）：8 个 flow 轮报全绿，而磁盘守卫把 8 次派发
    # 全挡回、落地为 0——**没有任何 flow 在看「产出」**。此节点每 15min 跑
    # duty_probe，凡 attempts>=5 且落地 0 即递工单（同类未决不重复递），
    # 使「产出停摆」不再依赖值守会话在场。
    outcome = CODE(id="outcome", lang="python", input={
        "requests_script": INPUT.requests_script, "duty_dir": INPUT.duty_dir,
        "dryrun": INPUT.dryrun,
    }, code="""
def run(input):
    import glob, json, os, subprocess
    probe = os.environ.get("DUTY_PROBE_SCRIPT") or \\
        "/Users/kong/projects/infra4agent/issue-keeper/flows/duty_probe.py"
    try:
        r = subprocess.run(["python3", probe, "--json", "--window-min", "90"],
                           capture_output=True, text=True, timeout=300)
        v = json.loads((r.stdout or "{}").strip() or "{}")
    except Exception as e:
        return {"level": "unknown", "error": str(e)[:120], "raised": None, "reasons": []}
    tp = v.get("throughput") or {}
    level = v.get("level") or "unknown"
    reasons = v.get("reasons") or []
    raised = None
    attempts = int(tp.get("attempts") or 0)
    landed = int(tp.get("landed") or 0)
    # 只在**真停摆**（BLOCKED：0 落地且无 run 在跑）递单；DEGRADED（如恢复中）不递，
    # 避免历史挡回窗口造成的误报疲劳。
    stuck = level == "BLOCKED" and attempts >= 5 and landed == 0
    if stuck and not input.get("dryrun"):
        duty = os.path.expanduser(input.get("duty_dir") or "~/.issue-keeper/duty")
        # 去重窗口 2h（**不论状态**）：产出停摆的判据窗口是 90 分钟，若只看「未决」
        # 工单，值守一处置完，下一轮（15min）就再递一张 → 同一停摆刷屏
        # （2026-10-09 17:00 实证：dismiss 后立刻又递 req-...-c32294）。
        # 同一停摆 2h 内只递一次；持续超过 2h 才值得再报。
        import datetime as _dt
        now_ts = time.time()
        dup = False
        for p in glob.glob(os.path.join(duty, "requests", "req-*.json")):
            try:
                d = json.load(open(p))
            except Exception:
                continue
            if d.get("kind") != "outcome-block":
                continue
            if d.get("status") in ("open", "escalated", "answered"):
                dup = True
                break
            try:
                t = _dt.datetime.strptime(str(d.get("created_at"))[:19],
                                          "%Y-%m-%dT%H:%M:%S").timestamp()
                if now_ts - t < 7200:
                    dup = True
                    break
            except Exception:
                pass
        if not dup:
            rs = os.path.expanduser(input.get("requests_script") or
                "~/projects/infra4agent/issue-keeper/flows/duty_request.py")
            ctx = json.dumps({"flow": "inflight-watch/outcome", "level": level,
                              "reasons": reasons, "throughput": tp}, ensure_ascii=False)
            title = "[产出] 近90m %d 次派发 0 落地：%s" % (
                attempts, (reasons[0] if reasons else "原因待查")[:60])
            try:
                rr = subprocess.run(["python3", rs, "create", "--from-flow", "inflight-watch",
                                     "--kind", "outcome-block", "--severity", "critical",
                                     "--title", title, "--context-json", ctx,
                                     "--options", "clean_disk,resume,cancel_reopen,escalate_human"],
                                    capture_output=True, text=True, timeout=60)
                lines = (rr.stdout or "").strip().splitlines()
                raised = json.loads(lines[-1]).get("id") if lines else None
            except Exception as e:
                raised = "create-failed: %s" % str(e)[:60]
    return {"level": level, "reasons": reasons, "attempts": attempts, "landed": landed,
            "raised": raised}
""")

    finish = CODE(id="finish", lang="python", input={
        "report": NODE.triage.report, "findings": NODE.triage.findings,
        "resumed": NODE.act.resumed, "requests_script": INPUT.requests_script,
        "rows": NODE.triage.rows, "outcome": NODE.outcome,
        "hitl_wait_secs": INPUT.hitl_wait_secs, "duty_dir": INPUT.duty_dir,
    }, code="""
def run(input):
    import fcntl, json, os, subprocess, tempfile, time
    duty = os.path.expanduser(input.get("duty_dir") or "~/.issue-keeper/duty")
    os.makedirs(duty, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    findings = input.get("findings") or []
    oc = input.get("outcome") or {}
    if oc.get("level") in ("BLOCKED", "DEGRADED"):
        findings = findings + [{
            "severity": "critical" if oc.get("level") == "BLOCKED" else "warn",
            "source": "outcome",
            "summary": "产出探针 %s：近90m %s 次派发落地 %s——%s" % (
                oc.get("level"), oc.get("attempts"), oc.get("landed"),
                "；".join(oc.get("reasons") or [])[:120]),
            "escalate": False}]
    crit = [f for f in findings if f.get("severity") == "critical"]
    # 递单去重：产出类 finding 已由 outcome 节点递了精确工单（kind=outcome-block），
    # 这里不再为它重复递通用 inflight-stall 单（2026-10-09 实证双单）。
    crit_ticket = [f for f in crit if f.get("source") != "outcome"]
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
                 "narrative": (str(input.get("report") or "")[:160]
                               + (" ｜ 产出 %s（派发 %s/落地 %s%s）" % (
                                   oc.get("level", "?"), oc.get("attempts"), oc.get("landed"),
                                   "，已递工单 " + str(oc.get("raised")) if oc.get("raised") else "")
                                  if oc else ""))[:300]}
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
    # 三层协同（jeffkit 2026-10-09）：critical **不再直接推人**——先写「决策工单」
    # 给值守 Agent（duty-agent */10 按能力矩阵自决，判不了才 HITL 人 + 监听回复）。
    req = {"id": None, "why": "无 critical"}
    if crit_ticket:
        try:
            rs = input.get("requests_script") or "/Users/kong/projects/infra4agent/issue-keeper/flows/duty_request.py"
            ctx = json.dumps({"flow": "inflight-watch", "report": input.get("report"),
                              "findings": [f.get("summary") for f in crit_ticket[:6]],
                              "rows": input.get("rows") or []}, ensure_ascii=False)
            cmd = ("python3 %s create --from-flow inflight-watch --kind inflight-stall "
                   "--severity critical --title %s --context-json %s --options resume,cancel_reopen,wait"
                   % (rs, json.dumps("[在途] " + str(input.get("report"))[:60], ensure_ascii=False).replace("'", ""),
                      json.dumps(ctx, ensure_ascii=False).replace("'", "")))
            rr = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
            req = json.loads((rr.stdout or "{}").strip().splitlines()[-1]) if rr.stdout.strip() else {"id": None}
        except Exception as e:
            req = {"id": None, "why": str(e)[:120]}
    return {"round": rd, "status": status, "request": req}
""")

    return {"round": NODE.finish.round, "status": NODE.finish.status}


if __name__ == "__main__":
    print("inflight-watch flow 源码（编译见 build_flows.py）")
