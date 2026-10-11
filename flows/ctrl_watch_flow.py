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
        python3 flows/build_flows.py ctrl-watch
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
    import json, os, subprocess, tempfile, time

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

    # ── 在途「空转」探测（2026-10-11 无限交接风暴后补）──────────────────────
    # 为什么补：本日 11:21–13:40 两台 worker 因「宿主归属自锁」全线空转
    # （近 300 行日志 292 行是「不亲和」、6 条在途零产出、队列恒定 6），
    # 而**所有心跳都是健康的**（A/B/shadow 全绿）⇒ ctrl-watch 连报 3 轮
    # 「全线正常」。**心跳只证「进程活着」，不证「工作在进行」**——
    # 这正是 meta-watchdog 的盲区：看门狗自己没死，但整个体系产能为零。
    #
    # 判据取「队列长度 + 在途条数」这个**不依赖心跳**的产能信号：
    # 若队列有积压（>0）且长时间毫无变化，说明消费侧在空转（领了就退/踢皮球）。
    # 刻意用**两次采样比对**（相隔 queue_stall_secs）而非单次阈值：
    # 单看「队列非空」会误报（正常排队也有），必须**不动**才算停摆。
    def _queue_snapshot():
        # VM 侧 v2 队列长度（在途积压的产能信号）。取不到返回 None（不报警）。
        # 注意：本 flow 是 CODE 源码字符串，codeflow 编译时会吃掉反斜杠转义
        # （引号转义与换行转义都会被吞 ⇒ 产物语法错误）。故本函数内**不出现
        # 任何反斜杠**：单引号/换行一律用 chr() 构造。
        # 另：远端脚本用 **base64** 传输，彻底绕开「ssh 单引号 + heredoc +
        # python -c」三层引号互相吃掉的坑（实测 heredoc 形式恒返回空）。
        import base64 as _b64
        _SQ = chr(39)
        _REDIS_PW = "***REMOVED***"
        _script = (
            "import redis" + chr(10)
            + "r = redis.Redis(host=" + _SQ + "127.0.0.1" + _SQ
            + ", port=6379, db=1, password=" + _SQ + _REDIS_PW + _SQ
            + ", socket_timeout=8)" + chr(10)
            + "print(r.xlen(" + _SQ + "plaita:flow:queue:v2" + _SQ + "))" + chr(10)
        )
        _enc = _b64.b64encode(_script.encode()).decode()
        qcmd = ("ssh -o ConnectTimeout=8 tcloud_gz "
                + _SQ + "echo " + _enc + " | base64 -d | ~/.venvs/issuekeeper/bin/python -"
                + _SQ + " 2>/dev/null")
        out = sh(qcmd, timeout=40)
        lines = [x.strip() for x in (out or "").splitlines() if x.strip()]
        s = lines[-1] if lines else ""
        return int(s) if s.isdigit() else None

    qlen_now = _queue_snapshot()
    # 上次采样（存 duty 目录，供两轮比对；缺失则只记基线，不报警）
    qprev_path = os.path.join(duty, "ctrl-queue-watch.json")
    qprev, qprev_at = None, None
    try:
        _p = json.load(open(qprev_path))
        qprev, qprev_at = _p.get("len"), _p.get("at")
    except Exception:
        pass
    try:
        _fd, _tmp = tempfile.mkstemp(dir=duty, prefix=".tmp-qw-")
        with os.fdopen(_fd, "w") as _fh:
            _fh.write(json.dumps({"len": qlen_now, "at": now}))
        os.replace(_tmp, qprev_path)
    except Exception:
        pass

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
            "shadow_age": shadow_age,
            "qlen_now": qlen_now, "qlen_prev": qprev, "qlen_prev_at": qprev_at}
""")

    # ── ② triage（纯规则；fail-safe：读不到心跳 = 按最坏报 critical）────────
    triage = CODE(id="triage", lang="python", input={
        "now": NODE.facts.now, "a": NODE.facts.a, "a_ts": NODE.facts.a_ts,
        "b": NODE.facts.b, "b_ts": NODE.facts.b_ts, "shadow_age": NODE.facts.shadow_age,
        "qlen_now": NODE.facts.qlen_now, "qlen_prev": NODE.facts.qlen_prev,
        "qlen_prev_at": NODE.facts.qlen_prev_at,
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

    # ── 在途空转（产能）判据（2026-10-11 无限交接风暴后补）──────────────────
    # 心跳全绿 ≠ 工作在推进。本日两台 worker 空转 2h+（队列恒定 6、6 条在途零
    # 产出），而 A/B/shadow 心跳全健康 ⇒ 本看门狗连报 3 轮「全线正常」。
    # 判据：**两次采样队列长度都 >0 且完全相同**（相隔 ≥ 一个调度周期）。
    # 单次非空不算（正常排队也存在）；只有「积压且毫无变化」才是消费侧停摆。
    # 取不到队列长度（ssh/redis 失败）时**不报**——那是另一类故障，由下方心跳/
    # shadow 判据覆盖，避免此处误报。
    qn, qp = input.get("qlen_now"), input.get("qlen_prev")
    qp_at = input.get("qlen_prev_at")
    if (isinstance(qn, int) and isinstance(qp, int) and not isinstance(qn, bool)
            and qn > 0 and qn == qp and isinstance(qp_at, (int, float))):
        stalled_min = (now - float(qp_at)) / 60.0
        # 只在与上次采样相隔 ≥20min 时才下结论（避免把「刚记基线」当成停摆）
        if stalled_min >= 20:
            f.append({"severity": "critical",
                      "summary": "队列积压 %d 条且 %.0fmin 毫无变化——消费侧疑似空转"
                                 "（心跳正常但零推进；查 worker 日志是否有重复"
                                 "「不亲和」/踢皮球）" % (qn, stalled_min),
                      "escalate": True})

    need = any(x.get("escalate") and x.get("severity") == "critical" for x in f)
    report = "; ".join(x["summary"] for x in f) if f else "全线正常"
    return {"findings": f, "need_escalate": need, "report": report}
""")

    # ── ③ act（critical 且距上次同 key 升级 >60min 才扰 jeffkit）──────────
    act = CODE(id="act", lang="python", input={
        "need": NODE.triage.need_escalate, "report": NODE.triage.report,
        "duty_dir": INPUT.duty_dir, "dryrun": INPUT.dryrun,
        "requests_script": INPUT.requests_script,
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

    # 2026-10-09 jeffkit 定：**不再直接发 #2**（#2 这种单子后续也不用）。
    # 主控只探测 + 递工单给值守 Agent；是否惊动/留痕由值守判断。
    ok = True
    # ── HITL 推送（jeffkit 2026-10-09：需要人工处理时用 HITL 通知人）──
    # 默认只推（wait_secs=0，零占用）；看情况给 hitl_wait_secs>0 才建会话限时等回复。
    # 反馈入口双通道：微信直接回 + 本条 #2 链接（在 issue 上回评/打标同样有效）。
    # 三层协同（jeffkit 2026-10-09）：critical 升级**先递工单给值守 Agent**，
    # 由它按能力矩阵自决；它判不了才 HITL 通知人并监听回复。
    req = {"id": None, "why": "skip"}
    try:
        rs = input.get("requests_script") or "/Users/kong/projects/infra4agent/issue-keeper/flows/duty_request.py"
        ctx = json.dumps({"flow": "ctrl-watch", "report": input.get("report"),
                          "github_escalation": "https://github.com/jeffkit/infra4agent/issues/2"},
                         ensure_ascii=False)
        cmd = ("python3 %s create --from-flow ctrl-watch --kind flow-escalation "
               "--severity critical --title %s --context-json %s --options resume,reopen_issue,escalate_human"
               % (rs, json.dumps("[主控] " + str(input.get("report"))[:60], ensure_ascii=False).replace("'", ""),
                  json.dumps(ctx, ensure_ascii=False).replace("'", "")))
        rr = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        req = json.loads((rr.stdout or "{}").strip().splitlines()[-1]) if rr.stdout.strip() else {"id": None}
    except Exception as e:
        req = {"id": None, "why": str(e)[:120]}
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
    return {"posted": ok, "why": (out.stdout or out.stderr)[:160], "request": req}
""")

    # ── ④ finish（duty 滚动窗口 + rounds.log 一行；无事也只一行）──────────
    finish = CODE(id="finish", lang="python", input={
        "findings": NODE.triage.findings, "report": NODE.triage.report,
        "posted": NODE.act.posted, "duty_dir": INPUT.duty_dir,
        "request": NODE.act.request,
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
    print("ctrl-watch flow 源码（编译见 build_flows.py ctrl-watch）")
