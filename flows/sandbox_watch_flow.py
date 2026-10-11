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

执行宿主纪律（2026-10-11 事故）：
- 本 flow 由 plaita-flow-worker **在 VM 上**执行（PLAITA_CODE_BACKEND=subprocess），
  故 CODE 节点里**不得出现 Mac 专属绝对路径**：解释器走 sys.executable（= flow-worker
  自己的 venv python）优先解析，辅助脚本按 flow 文件相对定位，见 facts/act 的 _pick_py。
- 清单获取是三态的：ok / empty（合法无实例）/ failed；failed 一律 critical + escalate，
  **绝不**允许渲染成「实例 0 · 浪费 0.00 实例小时」（成本闸不允许瞎成"报绿"）。
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
    import datetime, json, os, shutil, subprocess, sys, time, urllib.request

    # ── 解释器/脚本路径解析（2026-10-11 便携化）─────────────────────────
    # 为什么不再硬编码：本 flow 由 plaita-flow-worker **在 VM tcloud_gz 上**执行
    # （PLAITA_CODE_BACKEND=subprocess），Mac 专属的 /Users/kong/... 在 VM 上不存在
    # ⇒ lister 起不来（exit=127）⇒ 清单恒失败。解析顺序（含理由）：
    #   ① 显式 input.venv_py（调度侧可覆盖，永远最大优先级）；
    #   ② sys.executable —— CODE 节点的 subprocess 后端就是 [sys.executable, "-c",
    #      runner]（plaita/node/code.py），故它 == flow-worker 自己的 venv python
    #      （VM: ~/.venvs/issuekeeper/bin/python），**该 venv 里装了 e2b**；
    #   ③ <repo>/.venv/bin/python（bash 手工跑 flow 时的常见布局）；
    #   ④ 同目录 .venv（本仓就在 flows/ 里）；
    #   ⑤ env AGS_LISTER_PY / ISSUEKEEPER_PY（部署侧显式钉死）；
    #   ⑥ shutil.which("python3")（最后兜底）。
    def _root(path, marker="flows", limit=4):
        # 从 CODE 节点落地的文件路径回溯仓根：<repo>/flows/xxx.py → <repo>
        d = os.path.dirname(os.path.abspath(path))
        for _ in range(limit):
            if os.path.isdir(os.path.join(d, marker)):
                return d
            nd = os.path.dirname(d)
            if nd == d:
                break
            d = nd
        return ""

    def _pick_py(explicit):
        # ⚠️ 本函数在 3 处逐字复制：sandbox_watch_flow.py 的 facts + act、
        # inflight_watch_flow.py 的 facts。CODE 节点的 code 是**自包含源码字符串**
        # （编译进 .flow.json，各节点独立 subprocess 执行），无法 import 复用——
        # 故只能靠"逐字一致 + 本标注"防漂移。**改任何一处必须同步三处**，
        # 否则解释器解析会按节点静默分叉（2026-10-11 验收：act 那份曾漏 AGS_LISTER_PY）。
        for c in [explicit, sys.executable,
                  os.path.join(ROOT, ".venv", "bin", "python") if ROOT else "",
                  os.path.join(FLOW_DIR, ".venv", "bin", "python"),
                  os.environ.get("AGS_LISTER_PY"), os.environ.get("ISSUEKEEPER_PY"),
                  shutil.which("python3")]:
            c = str(c or "").strip()
            if c and os.path.exists(c) and os.access(c, os.X_OK):
                return c
        # 全都不存在：返回"最该存在"的那个（错误文本里要能看见它，便于下次定位）
        return str(explicit or sys.executable or "python3")

    def _pick_script(explicit, name):
        cands = [str(explicit or "").strip()]
        cands.append(os.path.join(FLOW_DIR, name))                       # 与 flow 同目录
        if ROOT:
            cands.append(os.path.join(ROOT, "flows", name))              # 仓根/flows
        cands += [os.environ.get("ISSUEKEEPER_FLOWS_DIR", "") and
                  os.path.join(os.environ["ISSUEKEEPER_FLOWS_DIR"], name),
                  os.path.join(os.getcwd(), "flows", name)]
        for c in cands:
            if c and os.path.exists(c):
                return c
        return cands[1] if len(cands) > 1 else name

    # 这条字符串会随 flow 源码"编译"进 code 节点，故必须保持对**执行宿主**成立：
    # 用 <repo>/flows 作为基准（两处部署点都有它），再逐级回退。
    _frame = __file__ if "__file__" in dir() else ""
    FLOW_DIR = os.path.dirname(os.path.abspath(
        _frame or os.path.join(os.getcwd(), "flows", "sandbox_watch_flow.py")))
    ROOT = _root(FLOW_DIR)
    VENV = _pick_py(input.get("venv_py"))
    LISTER = _pick_script(input.get("lister"), "ags-list.py")
    KEY = input.get("console_key") or "***REMOVED***"
    # 环境由父进程白名单传递（subprocess 后端的 env 白名单），这里按需补齐，
    # 不再依赖 shell 前缀赋值（cmd 走 argv 列表，不经 shell）。
    ENVV = dict(os.environ)
    ENVV.setdefault("E2B_DOMAIN", "ap-guangzhou.tencentags.com")
    ENVV.setdefault("E2B_API_KEY", "***REMOVED***")

    def sh(argv, timeout=60):
        # 返回 (returncode, stdout, stderr, why)：**绝不吞 stderr**——旧实现把非零
        # 退出/超时/命令不存在一律折叠成 ""，于是"底层不可用"与"输出为空"不可区分，
        # 真故障被渲染成"实例 0 · 浪费 0.00"（假阴性，成本闸瞎眼）。
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=ENVV)
            return (r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip(), "")
        except FileNotFoundError as e:
            return (127, "", "", "解释器不存在: %s" % str(e)[:120])
        except subprocess.TimeoutExpired:
            return (124, "", "", "超时 %ss 未返回" % timeout)
        except Exception as e:
            return (1, "", "", "%s: %s" % (type(e).__name__, str(e)[:120]))

    # 三态判定的**唯一依据 = 子进程 returncode**（不再用 stdout 空串的真值性）
    rc, out, err, why = sh([VENV, LISTER])
    state, detail, items = "ok", "", []
    if rc != 0:
        state = "failed"
        detail = why or "lister 退出码 %d" % rc
        if err:
            detail += " | stderr: " + err[-400:]
    elif not out:
        state = "failed"
        detail = "lister 退出码 0 但 stdout 为空（无 JSON 可解析）"
    else:
        try:
            items = json.loads(out)
            if not isinstance(items, list):
                raise ValueError("顶层不是数组: %s" % type(items).__name__)
        except Exception as e:
            # ⚠️ 必须把 items 重置为 []：json.loads 已把**非数组**赋给了 items，
            # 只改 state/detail 会让下游 `for it in items` 崩
            # （AttributeError: 'str' object has no attribute 'get'）。
            # 与 inflight_watch_flow.py 的同款位置保持逐字一致。
            items = []
            state = "failed"
            detail = "JSON 解析失败（%s）| stdout[:200]=%r" % (str(e)[:120], out[:200])

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
    if state == "ok" and not rows:
        state = "empty"          # 合法的"当前无运行中实例"，**不是**故障
    return {"instances": rows, "count": len(rows), "now": now,
            "state": state, "detail": detail, "stderr": err[-400:],
            "lister": LISTER, "interpreter": VENV,
            "error": detail if state == "failed" else None}
""")

    # ── ② triage（纯规则 + 活性探针）────────────────────────────────────
    triage = CODE(id="triage", lang="python", input={
        "instances": NODE.facts.instances, "count": NODE.facts.count,
        "error": NODE.facts.error, "state": NODE.facts.state, "detail": NODE.facts.detail,
        "lister": NODE.facts.lister, "interpreter": NODE.facts.interpreter,
        "orphan_min_age_h": INPUT.orphan_min_age_h, "stall_min": INPUT.stall_min,
        "quota_warn": INPUT.quota_warn, "default_impl_budget": INPUT.default_impl_budget_secs,
    }, code="""
def run(input):
    import subprocess

    TERMINAL = {"completed", "failed", "cancelled", "error"}
    inst = input.get("instances") or []
    # 三态（2026-10-11 判据硬化）：ok = 跑通且列到实例；empty = 跑通但当前无实例
    # （合法，健康）；failed = 非零退出/超时/解释器缺失/JSON 不可解析。旧实现用
    # `error = out[:200]` 的空串真值性兼作"有无错误"，stdout 恰为空时真故障被判成
    # 健康（渲染成"实例 0 · 浪费 0.00"）——成本闸瞎眼且瞎成"报绿"。
    state = str(input.get("state") or ("failed" if input.get("error") else "ok"))
    if state not in ("ok", "empty", "failed"):
        state = "failed"
    failed = state == "failed"
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
    if failed:
        # escalate: true —— 连续多轮"清单失败"必须出声（2026-10-11 实测：11:45→12:46
        # 连续 5 轮失败，findings=1 一直跳却没有任何工单/HITL，无人被告知）。
        findings.append({"severity": "critical",
                         "summary": "AGS 实例清单获取失败（无法判定成本状态）：%s"
                                    "｜解释器=%s ｜lister=%s"
                                    % (str(input.get("detail") or input.get("error"))[:200],
                                       input.get("interpreter") or "?", input.get("lister") or "?"),
                         "escalate": True,
                         "evidence": {"state": state, "interpreter": input.get("interpreter"),
                                      "lister": input.get("lister"),
                                      "detail": str(input.get("detail") or "")[:400]}})

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

    # 报告字符串必须三态都truthful：failed **绝不**能渲染成"实例 0 · 浪费 0.00"
    # （那就是把真故障洗成健康）；empty 与 ok 都照实报数。
    if failed:
        report = "清单失败（成本状态未知）：%s" % str(
            input.get("detail") or input.get("error") or "未知原因")[:100]
    else:
        report = ("实例 %d（孤儿 %d / 卡死 %d）· 浪费 %.2f 实例小时%s"
                  % (len(inst), len(orphans), len(stalled), waste_h,
                     "｜清单为空（无运行中实例）" if state == "empty" else ""))
    return {"orphans": orphans, "stalled": stalled, "findings": findings, "report": report,
            "waste_h": waste_h, "paused_idle": len(paused_idle), "state": state}
""")

    # ── ③ act（只杀孤儿：复用已验证的 ags-orphan-sweep.py）──────────────
    act = CODE(id="act", lang="python", input={
        "orphans": NODE.triage.orphans, "venv_py": INPUT.venv_py,
        "sweeper": INPUT.sweeper, "dryrun": INPUT.dryrun,
    }, code="""
def run(input):
    import os, shutil, subprocess, sys

    # 与 facts 同款便携解析（同一个执行宿主，绝不能硬编码 /Users/kong/...）：
    # 显式参数 → sys.executable（CODE 节点 subprocess 后端即 flow-worker 的 venv
    # python，VM 上为 ~/.venvs/issuekeeper/bin/python，装 e2b）→ <repo>/.venv →
    # env → which。sweep 只需 python3 + stdlib，但 venv 能拿到 e2b/requests。
    def _root(path, marker="flows", limit=4):
        d = os.path.dirname(os.path.abspath(path))
        for _ in range(limit):
            if os.path.isdir(os.path.join(d, marker)):
                return d
            nd = os.path.dirname(d)
            if nd == d:
                break
            d = nd
        return ""

    _frame = __file__ if "__file__" in dir() else ""
    FLOW_DIR = os.path.dirname(os.path.abspath(
        _frame or os.path.join(os.getcwd(), "flows", "sandbox_watch_flow.py")))
    ROOT = _root(FLOW_DIR)

    def _pick_py(explicit):
        # ⚠️ 本函数在 3 处逐字复制：sandbox_watch_flow.py 的 facts + act、
        # inflight_watch_flow.py 的 facts。CODE 节点的 code 是**自包含源码字符串**
        # （编译进 .flow.json，各节点独立 subprocess 执行），无法 import 复用——
        # 故只能靠"逐字一致 + 本标注"防漂移。**改任何一处必须同步三处**，
        # 否则解释器解析会按节点静默分叉（2026-10-11 验收：act 那份曾漏 AGS_LISTER_PY）。
        for c in [explicit, sys.executable,
                  os.path.join(ROOT, ".venv", "bin", "python") if ROOT else "",
                  os.path.join(FLOW_DIR, ".venv", "bin", "python"),
                  os.environ.get("AGS_LISTER_PY"), os.environ.get("ISSUEKEEPER_PY"),
                  shutil.which("python3")]:
            c = str(c or "").strip()
            if c and os.path.exists(c) and os.access(c, os.X_OK):
                return c
        return str(explicit or sys.executable or "python3")

    VENV = _pick_py(input.get("venv_py"))
    SWEEP = ""
    for c in [str(input.get("sweeper") or "").strip(),
              os.path.join(FLOW_DIR, "ags-orphan-sweep.py"),
              os.path.join(ROOT, "flows", "ags-orphan-sweep.py") if ROOT else ""]:
        if c and os.path.exists(c):
            SWEEP = c
            break
    if not SWEEP:
        SWEEP = os.path.join(FLOW_DIR, "ags-orphan-sweep.py")
    n_orphans = len(input.get("orphans") or [])
    if not n_orphans:
        return {"killed": 0, "out": "无孤儿"}
    if input.get("dryrun"):
        return {"killed": 0, "out": "dryrun：将清 %d 个孤儿" % n_orphans}
    try:
        # argv 列表（不经 shell），与 facts 的调用姿势一致
        r = subprocess.run([VENV, SWEEP, "--min-age", "0.5"],
                           capture_output=True, text=True, timeout=120)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        killed = out.count("→ kill=True")
        return {"killed": killed, "out": out[-600:], "rc": r.returncode}
    except Exception as e:
        return {"killed": 0, "out": "sweep 异常（%s %s）: %s"
                % (VENV, SWEEP, str(e)[:200]), "rc": None}
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
