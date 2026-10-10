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

规则（第一阶梯保守：只报、只 resume 一次；第二阶梯=确定性判据命中的机械白名单）：
- `terminal_lag`：console 已终态但 keeper 仍算在途 → critical（keeper 该收尸没收）
- `stalled`：exec running 且进度龄 > stall_min（默认 30）→ warn；> hard_min（默认 45）→ critical
- `long_run`：在途 > long_min（默认 180）→ warn（长跑未必异常，但值得人看一眼）
- 自动动作①：对 `stalled` **每个执行只 resume 一次**（救回 #27 那类；对 #28 这类能把
  终态滞后暴露出来），其余交人/交 keeper。

第二阶梯（#22，2026-10-09）：单日 5 次 cancel 全是同一 signature——sbx 通道条件假分支
悬死（引擎缺口 jeffkit/plaita#53，其落地前会持续复发），而值守轮次间隔 30min+、夜间
无人时卡死 run 空跑 2h+。故在「resume 无效」之后补一档**确定性机械处置**：

- 触发：`running` 且**双 120 交集**（末节点结束 >120min **且** 进度龄 >120min）
  且该执行的 resume 额度已用（账本 `duty/inflight-resume.json`）。
  双交集天然排除活跃 impl：健康 impl 周期性写步界/刷进度龄，且节点未结束时不填
  「末节点结束龄」；「无进度信号」也不进有损档（证明不了状态两小时没刷新）。
- 动作（全 authorized）：worktree `git diff HEAD` + `git log --oneline -5` 落
  `<run_dir>/salvage-snapshot.patch`（**快照代替抢救**，run 目录保留不删——复杂修复
  仍留值守/人工，机械层只保证产物不丢）→ `POST /api/executions/<eid>/cancel`
  → VM 上 `issue_keeper reopen`（自动摘 needs-human）→ 杀该执行的 AGS 沙箱实例
  （完整 sandbox_id）。cancel 失败即止（不 reopen，防重复 run）；reopen 失败单列
  critical 交值守——**轮次终态就是 critical 且必递工单**，finish 的「闭环降级」
  只吃 triage 那条，不吃 auto-dispose 的失败结论。
- 熔断：账本记 `cancelled`；同 issue 24h 内自动 cancel ≥2 次 → 停止机械处置，
  改递值守工单（防 reopen 后复发死循环，如 recursive#145 三连）。
- 留痕：每动作写 rounds.log + duty state 的 `actions[]`
  （capability `cancel_execution` / `reopen_issue`，level `authorized`）。
- 边界：**不碰引擎缺陷本身**（plaita#53 单独立单）；判断类动作仍归值守——
  本档只吃确定性判据（双 120）命中的机械动作。

编译：PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_flows.py inflight-watch
"""
from __future__ import annotations

from plaita.dsl.codeflow import CODE, NODE, flow
from plaita.node import register_code_node

register_code_node(default_backend="subprocess")


@flow("inflight-watch", desc="【值守·在途】run 级进度巡检：状态滞后/零进展/超长跑；每执行最多自动 resume 一次；双 120 死档自动 cancel+reopen+杀沙箱（熔断+快照）；ctrl 队列 */15")
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
        flow_id = ""
        try:
            ce = json.load(open(os.path.join(d, "console-exec.json")))
            eid = ce.get("execution_id", "")
            flow_id = ce.get("flow_id", "")
        except Exception:
            pass
        # 处置用的两样事实（#22）：reopen 要 repo 全名（state 的 slug 只给
        # jeffkit-xxx）；salvage 快照要 worktree 路径（keeper 派发时写进
        # dispatch.json）。两者都可能读不到（老 run / 文件被清）——缺失时如实
        # 标注：worktree 空 → 快照报「无产物可救」，repo_full 空 → 用 slug 反推兜底。
        repo_full, worktree = "", ""
        try:
            dj = json.load(open(os.path.join(d, "dispatch.json")))
            repo_full = str(dj.get("repo_full") or "")
            worktree = str(dj.get("worktree_dir") or "")
        except Exception:
            pass
        if not repo_full:
            # slug 的约定 = repo 全名 replace("/", "-")（keeper 同款）；兜底只
            # 换回首段那一个分隔符（仓名自身可能含连字符）
            repo_full = slug.replace("-", "/", 1)
        # retry_pending：keeper 是否仍在**有效的**退避窗口中等待重派。
        # ⚠️ 不能只看「retry_after 有值」——实测存在**过期很久的残留值**
        # （2026-10-10 04:0x：plaita#37/#46/#47 携带 1791420123 ≈ 10-08 15:42），
        # 若仅判有值会把真·收尸滞后误跳过。必须与当前时间比较。
        ra = it.get("retry_after")
        try:
            retry_pending = bool(ra) and float(ra) > time.time()
        except Exception:
            retry_pending = False
        rows.append({"repo": short, "num": str(num), "in_flight_min": round((time.time() - ifs) / 60),
                     "exec_id": eid, "flow_id": flow_id, "retry_after": ra,
                     "retry_pending": retry_pending, "repo_full": repo_full,
                     "run_dir": d, "worktree_dir": worktree})
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

    # ── ② triage（三类规则 + 找可救的 stalled + 双 120 死档）────────────
    triage = CODE(id="triage", lang="python", input={
        "rows": NODE.facts.rows, "instances": NODE.facts.instances,
        "error": NODE.facts.error,
        "stall_min": INPUT.stall_min, "hard_min": INPUT.hard_min, "long_min": INPUT.long_min,
        "terminal_lag_min": INPUT.terminal_lag_min, "dead_min": INPUT.dead_min,
    }, code="""
def run(input):
    TERMINAL = {"completed", "failed", "cancelled", "error"}
    rows = input.get("rows") or []
    inst = input.get("instances") or []
    stall = float(input.get("stall_min") or 30)
    hard = float(input.get("hard_min") or 45)
    long_min = float(input.get("long_min") or 180)
    # ⚠️ 必须用 max(...) 强制下限，不能只写 `or 40`：INPUT 由 console 的 flow 输入
    # schema 提供（当前 schema 里是 10），`input.get()` 拿到 10 时 `or` 兜底永不触发
    # ——2026-10-10 04:46 实测：改成 `or 40` 后阈值仍是 10，工单照旧刷。
    lag_min = max(float(input.get("terminal_lag_min") or 40), 40)
    # 死档线（#22 机械白名单）：playbook §1 判据「末节点结束 >120min **且** 进度龄
    # >120min」，与 keeper 僵尸线同一条 2h。同样用 max() 钉死下限——这条线一旦被
    # 调小，有损档（cancel+reopen）就会开始吃掉健康长节点。
    dead = max(float(input.get("dead_min") or 120), 120)

    findings, stalled, terminal_lag, dead_rows = [], [], [], []
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
            #
            # ⚠️ 第二层（2026-10-10 03:3x 实测）：keeper 对失败执行是**有意持有**——
            # 先落账（posted=True），再记 retry_after（退避 300s/3600s）等窗口重派，
            # 期间该 issue 仍留在在途集合里。若把这种「退避等待」当收尸滞后，会每
            # 10 分钟刷一张 critical 单（今晚 #148/#27/#22/#21 四单连续全是这类：
            # 执行 error、keeper 已收尾过、正等 retry_after 到点重派）。
            # 判据：**仍处于有效退避窗口**（retry_pending，由 payload 按时间判定）→ 不算滞后。
            # 注意不能用「retry_after 有值」代替——存在过期残留值（见 payload 注释）。
            if it.get("retry_pending"):
                continue
            lag = it.get("progress_age_min")
            if lag is None:
                lag = it.get("last_node_ended_age_min")
            if lag is not None and lag >= lag_min:
                terminal_lag.append(it)
                findings.append({"severity": "critical",
                                 "summary": "%s 执行已终态（%s）已 %.0f 分钟未收账（在途共 %.0f 分钟，且无退避排程）——收尸滞后"
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
        # 分支门取 min(stall, dead)：死档线是死的 120，不能因为 stall_min 被调大
        # （SKILL.md 记过 30→130 那次掩盖）就把双 120 的死档挡在门外。
        if st == "running" and it.get("has_node_ts") and not it.get("in_progress_nodes") \
                and lne is not None and lne > min(stall, dead):
            hard_stall = lne > hard and (pa2 is None or pa2 > hard)
            # 机械白名单档（#22）：**双 120 交集**才进有损档（cancel+reopen）。
            # 与 hard_stall 有意的三处差别：
            # ① 进度龄必须**有信号**——一次状态刷新都没有证明不了「两小时没动」，
            #    这种模糊形态留给值守判，机械层不赌；
            # ② 两条线都是死的 120（不是可调的 hard_min）——有损动作不跟旋钮走；
            # ③ 有开节点（in_progress_nodes>0）根本没有 lne（payload 不填），
            #    健康长 impl 天然进不来。
            dead_hit = lne > dead and pa2 is not None and pa2 > dead
            sev = "critical" if hard_stall else "warn"
            if sev == "critical":
                stalled.append(it)
            if dead_hit:
                dead_rows.append(it)
            findings.append({"severity": sev, "label": label,
                             "summary": "%s 末节点结束已 %.0f 分钟仍无下一个节点（在途 %.0f 分钟，进度龄 %s，%s 节点，flow=%s）%s"
                                        % (label, lne, age, pa2, it.get("nodes"), it.get("flow_id"),
                                           "——双 120 命中（机械处置判据；处置结果见 act）" if dead_hit else
                                           ("——超过 keeper 僵尸线仍未收，判定真卡死" if hard_stall
                                            else "（长节点进行中或需留意）")),
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
    report = "在途 %d（终态滞后 %d / 零进展 %d / 双120死档 %d）" % (
        len(rows), len(terminal_lag), len(stalled), len(dead_rows))
    return {"findings": findings, "stalled": stalled, "terminal_lag": terminal_lag,
            "hard_dead": dead_rows, "report": report, "rows": rows}
""")

    # ── ③ act（两阶梯，全为 authorized 机械动作）────────────────────────
    # 第一阶梯（原有）：每个执行最多自动 resume 一次（账本 duty/inflight-resume.json）。
    # 第二阶梯（#22）：**双 120 死档**（triage 已算，确定性判据）且 resume 额度已用 →
    # 快照 → cancel → reopen → 杀该执行的沙箱实例。判断仍归值守；熔断（同 issue
    # 24h 内已自动 cancel 2 次）触发时不再动手，只递工单（防 reopen 后复发死循环）。
    act = CODE(id="act", lang="python", input={
        "stalled": NODE.triage.stalled, "hard_dead": NODE.triage.hard_dead,
        "instances": NODE.facts.instances,
        "console_key": INPUT.console_key, "ssh_host": INPUT.ssh_host,
        "duty_dir": INPUT.duty_dir, "dryrun": INPUT.dryrun,
    }, code="""
def run(input):
    import json, os, subprocess, time, urllib.request, fcntl

    KEY = input.get("console_key") or "b4b5042ee7d1b937633c08f3f50d4c8efbca88d33ece8a03"
    HOST = input.get("ssh_host") or "tcloud_gz"
    # keeper 真值机（VM）的配方，与 playbook/toolbox 的手工处置逐字对齐
    VM = {"py": "/home/ubuntu/.venvs/issuekeeper/bin/python",
          "cfg": "/home/ubuntu/.issue-keeper/config.yaml",
          "repo_dir": "/home/ubuntu/projects/infra4agent/issue-keeper"}
    # 快照脚本：cancel 前把 worktree 的 diff + 近 5 条提交写进 run 目录（run 目录保留
    # 不删）。worktree 未知/已消失 = 无产物可救，如实报 why，不阻塞后续步骤。
    SALVAGE = r'''
import json, os, subprocess
spec = %(spec)r
run_dir = spec["run_dir"]
wt = spec["worktree"]
if not run_dir or not wt or not os.path.isdir(wt):
    print(json.dumps({"ok": False, "why": "worktree 不存在或未知: " + str(wt)}))
else:
    def git(*args):
        r = subprocess.run(["git", "-C", wt] + list(args), capture_output=True, text=True, timeout=60)
        return (r.stdout or "") + ("" if r.returncode == 0 else "[stderr] " + (r.stderr or ""))
    body = ["# salvage snapshot（inflight-watch #22 机械处置前自动落盘；run 目录保留不删）",
            "# worktree: " + wt,
            "## git diff HEAD", git("diff", "HEAD"),
            "## git log --oneline -5", git("log", "--oneline", "-5")]
    os.makedirs(run_dir, exist_ok=True)
    p = os.path.join(run_dir, "salvage-snapshot.patch")
    f = open(p, "w")
    f.write("\\n".join(body) + "\\n")
    f.close()
    print(json.dumps({"ok": True, "path": p, "bytes": os.path.getsize(p)}))
'''
    # reopen 脚本：VM 上的 keeper CLI（清终态 + 自动摘 needs-human），argv 列表不经 shell
    REOPEN = r'''
import json, subprocess
spec = %(spec)r
r = subprocess.run([spec["py"], "-m", "issue_keeper", "reopen", "-c", spec["config"],
                    spec["repo"], spec["num"]],
                   cwd=spec["repo_dir"], capture_output=True, text=True, timeout=180)
print(json.dumps({"rc": r.returncode, "out": (r.stdout or "")[-400:].strip(),
                  "err": (r.stderr or "")[-200:].strip()}))
'''
    duty = os.path.expanduser(input.get("duty_dir") or "~/.issue-keeper/duty")
    os.makedirs(duty, exist_ok=True)
    statef = os.path.join(duty, "inflight-resume.json")
    dry = bool(input.get("dryrun"))
    doc = {}
    try:
        doc = json.load(open(statef))
    except Exception:
        pass
    done = doc.get("resumed") or {}
    cancels = doc.get("cancelled") or {}
    # 轮初的 resume 账：本轮的 resume 不算「额度已用」——否则双 120 会在**同一轮**
    # 里先 resume 再 cancel（resume 是救命稻草，*/15 至少给它一跳的机会）。
    had_resume = set(done.keys())
    resumed, skipped, cancelled, breaker = [], [], [], []
    reopen_failed = []
    findings, notes = [], []

    def _ssh_script(src, timeout=120):
        # 远端脚本走 stdin（引号不被 shell 吃掉——2026-10-09 实测踩过）
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", HOST, "python3", "-"],
                           input=src, capture_output=True, text=True, timeout=timeout)
        txt = (r.stdout or "").strip().splitlines()
        return json.loads(txt[-1]) if txt else {}

    if not dry:
        # ── 第一阶梯：resume（每执行一次）───────────────────────────────
        # 候选 = stalled ∪ hard_dead：死档本应是 stalled 的子集（hard_min 缺省=120），
        # 但 hard_min 是可调旋钮——万一它被调大，死档里可能有没吃过救命稻草的执行；
        # 这里按 exec_id 去重并入，保证「先 resume 一次、再谈 cancel」对两者都成立。
        resume_cand = list(input.get("stalled") or [])
        seen = {str(x.get("exec_id") or "") for x in resume_cand}
        for x in input.get("hard_dead") or []:
            key = str(x.get("exec_id") or "")
            if key not in seen:
                resume_cand.append(x)
                seen.add(key)
        dead_eids = {str(x.get("exec_id") or "") for x in (input.get("hard_dead") or [])}
        for it in resume_cand:
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
                if eid in dead_eids:
                    # 死档的救命稻草**没递出去也算用过**：只写 skipped 会让死档永远
                    # 停在「额度未用」——每轮 resume 都抛错 → 既不进机械档也不留痕，
                    # 「无人收尸」原地复发（2026-10-10 复评）。普通 stalled 不记，
                    # 下轮重试；console 真坏时 cancel 也会失败，那才是 critical 递单。
                    done[eid] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "label": label,
                                 "error": str(e)[:60]}

        # ── 第二阶梯：双 120 死档 + resume 额度已用 → 机械处置 ────────────
        by_exec = {}
        for i in input.get("instances") or []:
            e = str(i.get("exec") or "")
            if e:
                by_exec.setdefault(e, i)
        now = time.time()
        for it in input.get("hard_dead") or []:
            eid = str(it.get("exec_id") or "")
            label = "%s#%s" % (it.get("repo"), it.get("num"))
            if not eid or eid not in had_resume:
                # resume 额度未用：本轮第一阶梯已 resume，等下一轮（*/15）看它活不活
                skipped.append(label)
                continue
            recent = [c for c in (cancels.get(label) or [])
                      if now - float(c.get("at") or 0) < 86400]
            if len(recent) >= 2:
                # 熔断：reopen 之后又死（plaita#33 / recursive#145 三连就是这形态）。
                # 机械层不再动手——重复 cancel+reopen 只烧沙箱，根因判断归值守。
                breaker.append(label)
                notes.append("%s 24h 内已自动 cancel %d 次，熔断（不再自动处置）" % (label, len(recent)))
                findings.append({"severity": "critical", "source": "auto-dispose", "label": label,
                                 "summary": "%s 双 120 死档但 24h 内已自动 cancel %d 次——已熔断停止机械处置，请值守判根因"
                                            % (label, len(recent)), "escalate": True})
                continue
            steps = {"salvage": None, "cancel": None, "reopen": None, "sandbox": None}
            try:
                steps["salvage"] = _ssh_script(SALVAGE % {"spec": {
                    "run_dir": str(it.get("run_dir") or ""),
                    "worktree": str(it.get("worktree_dir") or "")}})
            except Exception as e:
                steps["salvage"] = {"ok": False, "error": str(e)[:120]}
            # cancel 失败即止：run 可能还在跑，reopen 会造出重复 run
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:8323/api/executions/%s/cancel" % eid, data=b"",
                    headers={"X-Admin-API-Key": KEY, "Content-Type": "application/json"},
                    method="POST")
                resp = json.load(urllib.request.urlopen(req, timeout=20))
                steps["cancel"] = str(resp.get("status") or resp)[:60]
            except Exception as e:
                notes.append("%s cancel 失败（%s）——不 reopen，交值守" % (label, str(e)[:80]))
                findings.append({"severity": "critical", "source": "auto-dispose", "label": label,
                                 "summary": "%s 双 120 死档，自动 cancel 失败：%s——机械处置中止，请值守手工处置"
                                            % (label, str(e)[:80]), "escalate": True})
                continue
            try:
                steps["reopen"] = _ssh_script(REOPEN % {"spec": {
                    "py": VM["py"], "config": VM["cfg"], "repo_dir": VM["repo_dir"],
                    "repo": str(it.get("repo_full") or ""), "num": str(it.get("num") or "")}},
                    timeout=180)   # reopen 会为此单发状态评论 + 摘标，给足 gh 时间
            except Exception as e:
                steps["reopen"] = {"rc": None, "err": str(e)[:120]}
            # 杀该执行的沙箱实例（cancel 后它已是孤儿；必须完整 sandbox_id——截断会 404）
            i2 = by_exec.get(eid) or {}
            if i2.get("id"):
                try:
                    os.environ.setdefault("E2B_DOMAIN", "ap-guangzhou.tencentags.com")
                    os.environ.setdefault(
                        "E2B_API_KEY", "e2b_725235357335be8d27367c596c9e3199cf3c5eeb")
                    from e2b import Sandbox
                    steps["sandbox"] = bool(Sandbox.kill(str(i2["id"])))
                except Exception as e:
                    steps["sandbox"] = "error: %s" % str(e)[:100]
            else:
                steps["sandbox"] = "无匹配实例"
            hist = cancels.setdefault(label, [])
            hist.append({"at": now, "at_ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                         "exec": eid, "salvage": steps["salvage"],
                         "reopen": steps["reopen"], "sandbox": steps["sandbox"]})
            cancels[label] = hist[-20:]
            cancelled.append(label)
            notes.append("%s 机械处置：cancel=%s reopen=%s 沙箱=%s 快照=%s"
                         % (label, steps["cancel"], str(steps["reopen"])[:60], steps["sandbox"],
                            str(steps["salvage"])[:60]))
            if not isinstance(steps["reopen"], dict) or steps["reopen"].get("rc") != 0:
                reopen_failed.append(label)
                findings.append({"severity": "critical", "source": "auto-dispose", "label": label,
                                 "summary": "%s 已 cancel，但 reopen 未成功（%s）——单子仍在终态，请值守手工 reopen"
                                            % (label, str(steps["reopen"])[:100]), "escalate": True})
            elif steps["salvage"] and not steps["salvage"].get("ok"):
                findings.append({"severity": "warn", "source": "auto-dispose", "label": label,
                                 "summary": "%s 已 cancel+reopen，但 salvage 快照未落盘（%s）——worktree 若仍有改动请人工看一眼"
                                            % (label, str(steps["salvage"].get("why")
                                                          or steps["salvage"].get("error"))[:80]),
                                 "escalate": False})
            else:
                findings.append({"severity": "warn", "source": "auto-dispose", "label": label,
                                 "summary": "%s 双 120 死档 → 自动 cancel+reopen（快照 %s，沙箱 %s）"
                                            % (label, (steps["salvage"] or {}).get("path") or "无",
                                               steps["sandbox"]), "escalate": False})

    # ── 账本落盘（resumed + cancelled 同源；双双截尾）────────────────────
    if not dry:
        try:
            with open(statef + ".lock", "w") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                json.dump({"resumed": dict(list(done.items())[-200:]),
                           "cancelled": dict(list(cancels.items())[-200:])},
                          open(statef, "w"), ensure_ascii=False, indent=1)
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
    return {"resumed": resumed, "skipped": skipped, "cancelled": cancelled,
            "breaker": breaker, "reopen_failed": reopen_failed,
            "findings": findings, "notes": notes,
            "why": "dryrun" if dry else ""}
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
        "cancelled": NODE.act.cancelled, "act_findings": NODE.act.findings,
        "reopen_failed": NODE.act.reopen_failed,
        "hitl_wait_secs": INPUT.hitl_wait_secs, "duty_dir": INPUT.duty_dir,
    }, code="""
def run(input):
    import fcntl, json, os, subprocess, tempfile, time
    duty = os.path.expanduser(input.get("duty_dir") or "~/.issue-keeper/duty")
    os.makedirs(duty, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    findings = input.get("findings") or []
    cancelled = [str(x) for x in (input.get("cancelled") or [])]
    oc = input.get("outcome") or {}
    # act 的处置结论并进轮报（含熔断/cancel 失败/reopen 失败/reopen 未动的单）
    findings = findings + [f for f in (input.get("act_findings") or [])]
    # 本轮已机械处置（cancel+reopen）的执行：其 critical 降为 warn——已闭环，报
    # critical 会让值守以为还得接手（status=critical 直接进值守的必查清单）。
    # ⚠️ 只降「闭环」那一条：`cancelled` 是按 label 记的，act 自己的 auto-dispose
    # critical（reopen 未成功——label 照样在 cancelled 里）**就是失败结论**，一并降级
    # 会把「已 cancel 但单子仍卡终态」洗成 warn → 轮次不进 critical、不递单，
    # 正是 #22 要消灭的无人收尸（2026-10-10 复评实测：reopen-failure 轮
    # status=attention / ticket=None / actions 还写着「随 cancel 自动 reopen」）。
    if cancelled:
        findings = [({**f, "severity": "warn", "escalate": False,
                      "summary": str(f.get("summary") or "") + "（本轮已机械处置）"}
                     if str(f.get("label") or "") in cancelled and f.get("severity") == "critical"
                        and f.get("source") != "auto-dispose"
                     else f) for f in findings]
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
    # 这里不再为它重复递通用 inflight-stall 单（2026-10-09 实证双单）。同理，本轮
    # 已被机械处置（cancel+reopen）**且闭环**的执行不再刷单——它已闭环，刷单只会让
    # 值守白跑；auto-dispose 的失败结论（reopen 未成功/熔断/cancel 失败）一律递单。
    crit_ticket = [f for f in crit if f.get("source") != "outcome"
                   and not (str(f.get("label") or "") in cancelled
                            and f.get("source") != "auto-dispose")]
    spath = os.path.join(duty, "state-inflight.json")
    rd = 1
    try:
        doc = json.load(open(spath))
        rd = max([r.get("round", 0) for r in (doc.get("rounds") or [])] or [0]) + 1
    except Exception:
        pass
    status = "critical" if crit else ("attention" if findings else "ok")
    # `at` 是 state.schema.json 的 Action.required —— 缺了就不是合规留痕（审计看不出
    # 这动作什么时候发生）。之前三条都漏（既有漂移），这里一并补上。
    reopen_failed = [str(x) for x in (input.get("reopen_failed") or [])]
    acts = []
    if input.get("resumed"):
        acts.append({"capability": "restart_worker", "level": "authorized", "at": ts,
                     "summary": "自动 resume 卡死执行：%s" % ", ".join(input.get("resumed") or [])})
    if cancelled:
        # 机械白名单留痕（#22）：capability 与能力矩阵同名词，level=authorized
        acts.append({"capability": "cancel_execution", "level": "authorized", "at": ts,
                     "summary": "双 120 死档自动 cancel：%s" % ", ".join(cancelled)})
        # reopen 的结果要写进留痕本身：照抄「随 cancel 自动 reopen」会让 audit 以为
        # 这单已回队列，实际它仍卡在终态（复评：留痕把失败写成失败才有意义）。
        fixed = [l for l in cancelled if l not in reopen_failed]
        acts.append({"capability": "reopen_issue", "level": "authorized", "at": ts,
                     "summary": "随 cancel 自动 reopen：%s" % (", ".join(fixed) or "无")
                                + ("；未成功（单子仍在终态，需值守手工 reopen）：%s"
                                   % ", ".join(l for l in cancelled if l in reopen_failed)
                                   if reopen_failed else "")})
    round_doc = {"schema_version": "duty/state@0", "role": "inflight", "generation": 2,
                 "round": rd, "started_at": ts, "finished_at": ts, "status": status,
                 "findings": findings, "actions": acts,
                 "narrative": (str(input.get("report") or "")[:160]
                               + (" ｜ 机械处置 %s" % ", ".join(cancelled) if cancelled else "")
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
        fh.write("%s inflight-watch 轮次=%s 状态=%s findings=%s resume=%s 机械处置=%s 摘要=%s\\n"
                 % (ts, rd, status, len(findings), len(input.get("resumed") or []),
                    ",".join(cancelled) or "无", str(input.get("report"))[:90]))
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
