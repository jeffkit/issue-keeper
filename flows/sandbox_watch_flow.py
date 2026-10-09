#!/usr/bin/env python3
"""sandbox-watch —— 沙箱值守 flow（v0.1，ctrl 队列专用）。

为什么有它（2026-10-09 夜实证）：
- **孤儿实例烧钱**：run 06:27 就完成，实例却 running 到 08:00（1.6h 纯浪费）；
  24h 统计里 cancelled 轮吃掉 19.1 实例小时（占总用量 65%）——成本可见性必须常驻。
- **静默卡死**：某 sbx run 82 分钟零节点进展、租约照续、不报错（#83 看门狗想解但
  未覆盖 console/沙箱路径）——值守侧需要一条「卡死即报」的规则。
- **误报修正（#23，2026-10-09 19:00 轮）**：旧判据 `upd > stall_min(30)` 与 impl 节点
  自身预算（7200s）冲突，任何长 impl 必然被误报「卡死」（当日两例：沙箱内 agent
  实跑 57 分钟 / pytest 自验在跑）。现改为「先证活，再判死」：预算门（开节点 +
  upd < 预算+300s 放行）+ 活性门（超预算才进沙箱探活），stall_min 退回筛选用途。

与 launchd `ags-orphan-sweep` 的分工（有意并存）：
- 本 flow = **值守语义**：业务级留痕（duty round + 拓扑可见）、卡死判定、配额压力预警；
- launchd sweep = **独立兜底**：不依赖调度器进程，纯成本安全网。
两者都只杀「run 已终态」的实例，对在跑零风险。

图结构（纯 CODE，零 LLM；同 ctrl-watch 纪律）：
  facts（CODE：AGS 实例清单 + 每个实例的执行详情（含 context.$INPUT 预算与
         node_timings），全部只读）
    → triage（CODE：孤儿 / 卡死 / 配额压力；卡死=先证活再判死——候选进沙箱
         查存活 agent 进程 + 预算感知双门，见 #23）
    → act（CODE：杀孤儿（复用 ags-orphan-sweep.py）；卡死只报不动）
    → finish（CODE：duty state-sandbox 滚动窗口 + rounds.log 一行）

编译：PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_flows.py sandbox-watch
"""
from __future__ import annotations

from plaita.dsl.codeflow import CODE, NODE, flow
from plaita.node import register_code_node

register_code_node(default_backend="subprocess")


@flow("sandbox-watch", desc="【值守·沙箱】AGS 实例巡检：孤儿清查（终态 run 的实例）+ 静默卡死判定（先证活再判死，#23）+ 配额压力；ctrl 队列 */30")
def sandbox_watch(INPUT):
    # ── ① facts（IO 叶子：只读；plaita venv 子进程查 AGS + console 执行详情）─────
    facts = CODE(id="facts", lang="python", input={
        "venv_py": INPUT.venv_py, "lister": INPUT.lister, "console_key": INPUT.console_key,
    }, code="""
def run(input):
    import datetime, json, subprocess, time, urllib.request

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

    def parse_ts(ts):
        # 时区纪律（inflight-watch 同款）：console 时间戳多为本地 naive；
        # aware 的先转本地再当 naive 比，避免 8h 偏移。
        try:
            t = datetime.datetime.fromisoformat(str(ts)[:19])
        except Exception:
            return None
        if t.tzinfo is not None:
            t = t.astimezone().replace(tzinfo=None)
        return t

    def api(eid):
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8323/api/executions/" + eid,
                headers={"X-Admin-API-Key": KEY})
            return json.load(urllib.request.urlopen(req, timeout=8))
        except Exception:
            return None

    rows = []
    for it in items:
        eid = it.get("exec") or ""
        d = api(eid) if eid else None
        st = str((d or {}).get("status") or ("query-failed" if eid else "unknown"))
        lu = (d or {}).get("last_update_time") or (d or {}).get("start_time")
        upd_age = None
        if lu:
            t = parse_ts(lu)
            if t is not None:
                upd_age = int(now - t.timestamp())
        # 节点边界证据（#23）：impl 类节点只在结束时落一条 started/ended，
        # 「末节点结束龄」才是「该节点真跑不动了」的信号；open 节点（有
        # started 无 ended）说明有节点正在进行——长节点中途本就无更新。
        node = {}
        open_node, last_end_age = "", None
        nt = (d or {}).get("node_timings") or {}
        best_end = None
        for k, v in nt.items():
            if not isinstance(v, dict):
                continue
            node[k] = {"started": bool(v.get("started_at")), "ended": bool(v.get("ended_at"))}
            if v.get("started_at") and not v.get("ended_at"):
                open_node = str(k)
            e = v.get("ended_at")
            if e:
                t = parse_ts(e)
                if t is not None and (best_end is None or t > best_end):
                    best_end = t
        if best_end is not None:
            last_end_age = int(now - best_end.timestamp())
        # 节点预算（#23）：self-improve 把 $INPUT.impl_timeout_secs 经
        # assignment 节点物化进 $NODE.impl_timeout——这比猜 INPUT 缺省更可靠；
        # 两者都拿不到（老执行/别的 flow）回退 default_impl_budget_secs。
        inp = ((d or {}).get("context") or {}).get("$INPUT") or {}
        nodes_ctx = ((d or {}).get("context") or {}).get("$NODE") or {}
        budget = nodes_ctx.get("impl_timeout")
        if not isinstance(budget, (int, float)) or budget <= 0:
            budget = inp.get("impl_timeout_secs")
        if not isinstance(budget, (int, float)) or budget <= 0:
            budget = None
        rows.append({**it, "exec_status": st, "exec_update_age_sec": upd_age,
                     "open_node": open_node, "last_node_end_age_sec": last_end_age,
                     "impl_budget_sec": budget, "flow_id": (d or {}).get("flow_id") or "",
                     "goal": str(inp.get("goal") or "")[:60], "node_summary": node})
    return {"instances": rows, "count": len(rows), "now": now, "error": None if items else out[:200]}
""")

    # ── ② triage（纯规则 + 活性探针）────────────────────────────────────
    triage = CODE(id="triage", lang="python", input={
        "instances": NODE.facts.instances, "count": NODE.facts.count,
        "error": NODE.facts.error,
        "orphan_min_age_h": INPUT.orphan_min_age_h, "stall_min": INPUT.stall_min,
        "quota_warn": INPUT.quota_warn, "default_impl_budget": INPUT.default_impl_budget_secs,
    }, code="""
def run(input):
    import subprocess

    TERMINAL = {"completed", "failed", "cancelled", "error"}
    inst = input.get("instances") or []
    orphan_h = float(input.get("orphan_min_age_h") or 0.5)
    stall_min = float(input.get("stall_min") or 30)
    quota_warn = int(input.get("quota_warn") or 6)
    # impl 节点预算缺省与 self-improve 的 $F.or($INPUT.impl_timeout_secs, 7200)
    # 对齐（#23）；调度参数可用 default_impl_budget_secs 覆盖。
    default_budget = float(input.get("default_impl_budget") or 7200)

    # 进沙箱证活（#23 首选方案）：有存活 agent 进程且 etime 在增长 → 活的。
    # 只读 ps，不杀不动；探针失败（连接异常等）当「无法证明」处理，
    # 回退纯时限判据，绝不因探针故障静默放过真卡死。
    def agent_alive(sid):
        pat = "ps -eo pid,etimes,args --no-headers"
        grep = "grep -E 'recursive --workspace|recursive .* run |python3? -m pytest' | grep -v grep | head -8"
        try:
            from e2b import Sandbox
            sbx = Sandbox.connect(sid)
            res = sbx.commands.run(pat + " | " + grep, timeout=45)
            best = None
            for line in (res.stdout or "").splitlines():
                parts = line.split(None, 2)
                if len(parts) < 3:
                    continue
                try:
                    et = int(parts[1])
                except ValueError:
                    continue
                if best is None or et > best:
                    best = et
            if best is None:
                return (False, 0, "no agent process")
            if best < 120:
                # 刚拉起 2 分钟内的进程不可信（agent 崩溃循环/探针撞上重启窗），
                # 视同未证活，走判死路径并带 etime 证据。
                return (False, best, "agent etime %ds too fresh" % best)
            return (True, best, "")
        except Exception as e:
            return (None, 0, str(e)[:120])

    orphans, stalled, findings = [], [], []
    for it in inst:
        age = it.get("age_h") or 0
        st = it.get("exec_status") or "unknown"
        upd = it.get("exec_update_age_sec")
        if (st in TERMINAL or st in ("unknown", "query-failed")) and age >= orphan_h:
            orphans.append(it)
        if st != "running" or upd is None or upd <= stall_min * 60 or age < 0.75:
            continue
        # 候选（超 stall_min 无节点边界更新）。#23 判据，先证活再判死：
        # ① 预算门——节点自身预算（impl 7200s）内且仍有开节点 → 长节点进行中，
        #   不报（stall_min 只用来筛候选，不再直接定罪）；
        # ② 活性门——超预算（或无开节点=边界之间空白）时进沙箱查存活 agent：
        #   有存活且 etime>120s → 不报；无 → 报「agent 已退出」带证据字段。
        budget = it.get("impl_budget_sec")
        budget = float(budget) if isinstance(budget, (int, float)) and budget > 0 else default_budget
        open_node = it.get("open_node") or ""
        upd_min = int(upd / 60)
        if open_node and upd < budget + 300:
            continue
        alive, etime, err = agent_alive(it.get("id"))
        if alive:
            continue
        if alive is None:
            findings.append({"severity": "warn",
                             "summary": "疑似静默卡死：实例 %s 存活 %sh，执行 %s running %s 分钟无节点更新，"
                                        "但沙箱探针失败无法证伪（%s）——建议人工进沙箱核实"
                                        % (it.get("short"), it.get("age_h"), (it.get("exec") or "?")[:8],
                                           upd_min, err),
                             "escalate": False})
            continue
        stalled.append(it)
        dead_why = "agent 已退出（无 recursive/pytest 存活进程）" if not etime \
            else "仅存 etime=%ss 的新进程（崩溃循环？）" % etime
        findings.append({"severity": "warn",
                         "summary": "静默卡死：实例 %s 存活 %sh，执行 %s running %s 分钟无节点更新，"
                                    "沙箱内 %s"
                                    % (it.get("short"), it.get("age_h"), (it.get("exec") or "?")[:8],
                                       upd_min, dead_why),
                         "escalate": False,
                         "evidence": {"agent_alive": False, "probe_error": err,
                                      "upd_min": upd_min, "budget_sec": int(budget),
                                      "open_node": open_node}})
    for it in orphans:
        findings.append({"severity": "warn",
                         "summary": "孤儿实例：实例 %s 存活 %sh，其执行 %s 已终态（%s）——sweep 会清"
                                    % (it.get("short"), it.get("age_h"), (it.get("exec") or "?")[:8],
                                       it.get("exec_status")),
                         "escalate": False})
    if len(inst) >= quota_warn:
        findings.append({"severity": "warn",
                         "summary": "AGS 实例数 %d ≥ %d，配额压力（成本/并发受限）" % (len(inst), quota_warn),
                         "escalate": False})
    if input.get("error"):
        findings.append({"severity": "critical",
                         "summary": "AGS 实例清单获取失败：%s" % str(input.get("error"))[:120],
                         "escalate": True})

    # 浪费口径（2026-10-09 增，jeffkit 点名「孤儿没察觉」）：
    # 孤儿由 act 击杀，但**浪费已经发生**——原先只报个数、不折算成本、也不递单，
    # 于是「3 个 paused 且执行已终态的实例白占 2-2.5h」没有任何工单路径，只有人问
    # 才看得见。现在折算实例小时（instance-hours），超阈值递单（成本视角，非每轮噪音）。
    waste_h = 0.0
    for it in orphans:
        waste_h += float(it.get("age_h") or 0)
    paused_idle = [it for it in inst if it.get("state") == "paused"
                   and (it.get("exec_status") or "") in TERMINAL | {"unknown", "query-failed"}]
    for it in paused_idle:
        if it not in orphans:
            waste_h += float(it.get("age_h") or 0)
    waste_h = round(waste_h, 2)
    if waste_h >= 2.0:
        findings.append({"severity": "critical",
                         "summary": "沙箱浪费累计 %.1f 实例小时（孤儿 %d / paused 空闲 %d）——查清扫链路"
                                    % (waste_h, len(orphans), len(paused_idle)),
                         "escalate": True})

    report = ("实例 %d（孤儿 %d / 卡死 %d）· 浪费 %.2f 实例小时"
              % (len(inst), len(orphans), len(stalled), waste_h)) \
        if inst or not input.get("error") else "清单失败"
    return {"orphans": orphans, "stalled": stalled, "findings": findings, "report": report,
            "waste_h": waste_h, "paused_idle": len(paused_idle)}
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
