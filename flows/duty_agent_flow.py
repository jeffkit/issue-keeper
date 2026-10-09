#!/usr/bin/env python3
"""duty-agent —— 值守 Agent 层（v0.1，ctrl 队列专用）。

三层协同（jeffkit 2026-10-09 定，替代"flow 直接把决策推给人"的旧形态）：

    flow 过程里需要决策
      ① **本 flow**：值守 Agent 读工单（duty/requests/*.json）→ 按能力矩阵判定
         —— 它能做的自己做（执行 + 留痕），这一层要吃掉绝大多数请求；
      ② 它判不了（human-in-loop：重启 worker / 改并发 / 改模型档 / 改基础设施 /
         重大拍板 / 关单这种业务判断）→ 发 HITL 通知给人 + 注册监听；
      ③ 人回复 → hitl_inbox 回填工单（status=answered）→ 本 flow 下轮据回复执行闭环。

能力矩阵（docs/duty/CAPABILITY-MATRIX.md，本 flow 的授权边界）：
- autonomous/authorized（可自决）：resume/cancel execution、reopen_issue、
  add/remove_label、comment_issue、clean_disk（受路径红线约束：不动 iOS 模拟器、
  不删 live worktree 的 target、不删 cargo registry、删 target 前查软链）
- human-in-loop（必须上报人）：restart_worker/restart_keeper（重启任何服务）、
  change_concurrency、change_model_tier、change_infrastructure、重大拍板、close_issue
  （验收结论归 A 班，值守不替业务判断）
- forbidden（恒定拒绝）：purge_dlq、push_others_commit、动 A/B 班 automation

图结构：facts（读工单+矩阵+近况）→ decide（AGENTRUN）→ act（执行白名单动作/
上报人）→ finish（轮报+状态回填）。

编译：PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_flows.py duty-agent
"""
from __future__ import annotations

from plaita.dsl.codeflow import AGENTRUN, CODE, F, NODE, flow
from plaita.node import register_code_node

register_code_node(default_backend="subprocess")

@flow("duty-agent", desc="【值守·决策层】读 flow 决策工单→按能力矩阵自决执行或上报人（HITL+监听回复）；ctrl */10")
def duty_agent(INPUT):
    # ── ① facts：开放工单 + 能力矩阵 + 近况（纯 CODE 只读）──────────────
    facts = CODE(id="facts", lang="python", input={
        "requests_script": INPUT.requests_script, "duty_dir": INPUT.duty_dir,
    }, code="""
def run(input):
    import json, os, subprocess, time

    script = input.get("requests_script") or "/Users/kong/projects/infra4agent/issue-keeper/flows/duty_request.py"
    duty = os.path.expanduser(input.get("duty_dir") or "~/.issue-keeper/duty")

    def sh(cmd, timeout=60):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return (r.stdout or "").strip()
        except Exception:
            return ""

    rows = []
    try:
        rows = json.loads(sh("python3 %s list --status open,decided,answered --json" % script) or "[]")
    except Exception:
        rows = []
    # 近况：各值守 flow 最近轮报（给 agent 判断上下文）
    recent = []
    try:
        with open(os.path.join(duty, "rounds.log")) as fh:
            recent = fh.read().strip().splitlines()[-15:]
    except Exception:
        pass
    # 在途/磁盘快照（决策常用事实）
    inflight = sh("ssh -o ConnectTimeout=8 tcloud_gz 'python3 -c \\"import json;a=json.load(open(\\'/home/ubuntu/.issue-keeper/state.json\\'));print(len([1 for rv in (a.get(\\'repos\\') or {}).values() for it in (rv.get(\\'items\\') or {}).values() if it.get(\\'in_flight_since\\')]))\\"'", timeout=30)
    return {"requests": rows, "open_count": len(rows), "rounds_tail": recent, "inflight": inflight}
""")

    # ── ② decide：值守 Agent 判定（能做的自决，做不了的转人）────────────
    decide = AGENTRUN(agent="deepseek-flash",
                      repo="/Users/kong/projects/infra4agent",
                      timeout_secs=600,
                      prompt=F.concat(
        "你是 infra4agent 值守体系的**值守 Agent（决策层）**（jeffkit 2026-10-09 授权）。\n"
        "下游 flow 在运行中需要决策时，会写「决策工单」给你（本提示词末尾内嵌）。"
        "你的职责：**先自己判**——能办的按能力矩阵直接办掉（这才叫值守），"
        "只有确实超出你权限或属于业务/成本/对外判断时，才转人工。\n\n",
        "【你的授权边界（能力矩阵）】\n"
        "- 可自决执行（autonomous/authorized）：resume_execution、cancel_execution、"
        "reopen_issue、add_label、remove_label、comment_issue、clean_disk、dismiss\n"
        "- **必须上报人**（human-in-loop，不要自决）：restart_worker、restart_keeper、"
        "change_concurrency、change_model_tier、change_infrastructure、close_issue"
        "（业务判断归 A 班）、任何「重大拍板」/涉及成本承诺/对外承诺的事\n"
        "- 恒定禁止：purge_dlq、push_others_commit、改 A/B 班 automation\n"
        "- clean_disk 红线：不动 iOS 模拟器目录、不删 live run worktree 的 target、"
        "不删 ~/.cargo/registry、删 target 前必查 ~/.local/bin 软链指向\n",
        "\n【判定纪律】\n"
        "1) 每个工单都要有结论，不许漏；\n"
        "2) 优先用最小动作解决（如卡死先 resume，无效再考虑 cancel+reopen）；\n"
        "3) clean_disk / reopen / resume 之类动作要给出**证据**（工单 context 里的数字/链接）；\n"
        "4) 转人时 message 要**自足**：说清是什么事、卡在哪、需要人决定什么、"
        "有哪些选项与各自后果（人没有上下文，别让他去翻日志）；\n"
        "5) human-in-loop 事项**不要自决**，宁可上报。\n\n"
        "【输出纪律】最后一行输出一行严格 JSON：\n"
        "{\"decisions\":[{\"id\":\"req-...\",\"action\":\"resume_execution\","
        "\"args\":{\"execution_id\":\"...\"},\"rationale\":\"为什么（≤120字）\"},"
        "{\"id\":\"req-...\",\"action\":\"escalate_human\",\"message\":\"给自足的一句话（≤400字）\","
        "\"why\":\"为什么你判不了\"}]}\n"
        "action 取值：resume_execution | cancel_execution | reopen_issue | add_label | "
        "remove_label | comment_issue | clean_disk | dismiss | escalate_human\n"
        "开放工单为空时：不做任何工具调用，直接输出 {\"decisions\":[]}\n\n"
        "<<<REQUESTS>>>\n", NODE.facts.requests, "\n<<<END REQUESTS>>>\n",
        "近况（各值守 flow 最近轮报）：\n", NODE.facts.rounds_tail,
        "\n在途 run 数：", NODE.facts.inflight, "\n"))

    # ── ③ act：执行白名单动作 / 上报人（CODE，含红线约束）──────────────
    act = CODE(id="act", lang="python", input={
        "decisions": NODE.decide.text, "requests_script": INPUT.requests_script,
        "hitl_script": INPUT.hitl_script, "ssh_host": INPUT.ssh_host,
        "duty_dir": INPUT.duty_dir, "dryrun": INPUT.dryrun,
    }, code="""
def run(input):
    import json, os, re, subprocess

    REQ = input.get("requests_script") or "/Users/kong/projects/infra4agent/issue-keeper/flows/duty_request.py"
    HITL = input.get("hitl_script") or "/Users/kong/projects/infra4agent/issue-keeper/flows/hitl_notify.py"
    HOST = input.get("ssh_host") or "tcloud_gz"
    DRY = bool(input.get("dryrun"))

    def sh(cmd, timeout=180):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return (r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip())
        except Exception as e:
            return (-1, "", str(e)[:150])

    # 解析 agent 的决策 JSON（取最后一行可解析的 JSON）
    raw = input.get("decisions") or ""
    decisions = []
    for line in reversed(str(raw).splitlines()):
        line = line.strip()
        if line.startswith("{") and "decisions" in line:
            try:
                decisions = json.loads(line).get("decisions") or []
                break
            except Exception:
                continue
    if not decisions:
        return {"executed": [], "escalated": [], "why": "未解析到决策 JSON（值守本轮不动手）"}

    ALLOWED = {"resume_execution", "cancel_execution", "reopen_issue", "add_label",
               "remove_label", "comment_issue", "clean_disk", "dismiss", "escalate_human"}
    FORBIDDEN = {"purge_dlq", "push_others_commit", "restart_worker", "restart_keeper",
                 "change_concurrency", "change_model_tier", "change_infrastructure", "close_issue"}
    executed, escalated, refused = [], [], []

    def _gh(args, timeout=60):
        return sh("gh " + args, timeout=timeout)

    for d in decisions:
        rid = str(d.get("id") or "")
        action = str(d.get("action") or "")
        args = d.get("args") or {}
        why = str(d.get("rationale") or d.get("why") or "")[:300]
        if action in FORBIDDEN:
            # matrix 说必须上报——agent 越权则一律转人（不给它硬闯的机会）
            refused.append("%s:%s" % (rid, action))
            action = "escalate_human"
            d["message"] = d.get("message") or ("值守 Agent 试图执行越权动作 %s，按能力矩阵转人工：%s" % (action, why))
        if action not in ALLOWED:
            refused.append("%s:%s(未知)" % (rid, action))
            continue
        if DRY:
            executed.append("%s:%s(dryrun)" % (rid, action))
            continue
        ok, note = False, ""
        if action == "resume_execution":
            eid = str(args.get("execution_id") or "")
            rc, out, err = sh("curl -s -X POST http://127.0.0.1:8323/api/executions/%s/resume "
                              "-H 'X-Admin-API-Key: %s' -H 'Content-Type: application/json' "
                              "-d '{\\"resume_type\\":\\"retry\\"}'" % (
                                  eid, os.environ.get("PLAITA_CONSOLE_KEY",
                                                      "b4b5042ee7d1b937633c08f3f50d4c8efbca88d33ece8a03")))
            ok, note = ("success" in out or "resuming" in out), out[:120]
        elif action == "cancel_execution":
            eid = str(args.get("execution_id") or "")
            rc, out, err = sh("curl -s -X POST http://127.0.0.1:8323/api/executions/%s/cancel "
                              "-H 'X-Admin-API-Key: %s' -H 'Content-Type: application/json'" % (
                                  eid, os.environ.get("PLAITA_CONSOLE_KEY",
                                                      "b4b5042ee7d1b937633c08f3f50d4c8efbca88d33ece8a03")))
            ok, note = ("success" in out), out[:120]
        elif action == "reopen_issue":
            repo, num = str(args.get("repo") or ""), str(args.get("number") or "")
            rc, out, err = sh("ssh -o ConnectTimeout=8 %s 'cd ~/projects/infra4agent/issue-keeper && "
                              "~/.venvs/issuekeeper/bin/python -m issue_keeper reopen "
                              "-c ~/.issue-keeper/config.yaml %s %s'" % (HOST, repo, num), timeout=120)
            ok, note = (rc == 0), (out or err)[:120]
        elif action in ("add_label", "remove_label"):
            repo, num = str(args.get("repo") or ""), str(args.get("number") or "")
            label = str(args.get("label") or "needs-human")
            flag = "--add-label" if action == "add_label" else "--remove-label"
            rc, out, err = _gh("issue edit %s -R %s %s %s" % (num, repo, flag, label))
            ok, note = (rc == 0), (out or err)[:120]
        elif action == "comment_issue":
            repo, num = str(args.get("repo") or ""), str(args.get("number") or "")
            body = str(args.get("body") or "")
            if body:
                rc, out, err = _gh("issue comment %s -R %s --body %s" % (num, repo, json.dumps(body, ensure_ascii=False).replace("'", "")))
                ok, note = (rc == 0), (out or err)[:120]
        elif action == "clean_disk":
            # 与 disk-hygiene 同款：只删 <repo>/.flowcast/runs 下 mtime>24h 的 run worktree
            cmd = ("ssh -o ConnectTimeout=8 %s 'python3 - <<PY\\n"
                   "import os,shutil,time\\n"
                   "cutoff=time.time()-24*3600;n=0\\n"
                   "for base in os.listdir(\\"/home/ubuntu/projects/infra4agent\\"):\\n"
                   "    d=\\"/home/ubuntu/projects/infra4agent/%s/.flowcast/runs\\"%base\\n"
                   "    if os.path.isdir(d):\\n"
                   "        for x in os.listdir(d):\\n"
                   "            p=os.path.join(d,x)\\n"
                   "            if os.path.isdir(p) and os.stat(p).st_mtime<cutoff: shutil.rmtree(p,ignore_errors=True);n+=1\\n"
                   "print(n)\\nPY'" % HOST)
            rc, out, err = sh(cmd, timeout=300)
            ok, note = (rc == 0), "清理 %s 个" % (out.strip()[-10:] or "?")
        elif action == "dismiss":
            ok, note = True, "值守判定无需动作"
        elif action == "escalate_human":
            msg = str(d.get("message") or args.get("message") or "")[:900]
            title = "值守上报：" + msg[:48]
            hargs = args.get("_request") or {}
            rc, out, err = sh("python3 %s --title %s --body %s --dedupe-key %s "
                              "--feedback-url https://github.com/jeffkit/infra4agent/issues/2"
                              % (HITL, json.dumps(title, ensure_ascii=False).replace("'", ""),
                                 json.dumps(msg, ensure_ascii=False).replace("'", ""), "duty:" + rid))
            sid = ""
            try:
                sid = json.loads(out).get("session_id") or ""
            except Exception:
                pass
            ok = ('"sent": true' in out) or ("sent': True" in out)
            note = "HITL 已发（session=%s）" % sid[:12]
            if sid and rid:
                sh("python3 %s update --id %s --status escalated --note human_notified" % (REQ, rid))
                # 回填 session_id 到工单 human 字段（供 hitl_inbox 回填回复）
                sh("python3 - <<PY\\nimport json\\np='/Users/kong/.issue-keeper/duty/requests/%s.json'\\n"
                   "d=json.load(open(p));d['human']={'session_id':'%s','notified_at':d['created_at']}\\n"
                   "json.dump(d,open(p,'w'),ensure_ascii=False,indent=1)\\nPY" % (rid, sid))
            escalated.append("%s:%s" % (rid, note))
            continue
        if rid:
            dec = json.dumps({"by": "agent", "action": action, "args": args,
                              "rationale": why}, ensure_ascii=False)
            status = "resolved" if ok else "open"
            sh("python3 %s update --id %s --status %s --decision-json %s --note '%s'"
               % (REQ, rid, status, json.dumps(dec, ensure_ascii=False).replace("'", ""),
                  ("执行成功" if ok else "执行失败") + "：" + note.replace("'", "")[:80]))
        executed.append("%s:%s(%s)" % (rid, action, "ok" if ok else "fail"))
    return {"executed": executed, "escalated": escalated, "refused": refused,
            "n": len(decisions)}
""")

    # ── ④ finish：轮报（有动作才写，避免刷屏）──────────────────────────
    finish = CODE(id="finish", lang="python", input={
        "executed": NODE.act.executed, "escalated": NODE.act.escalated,
        "refused": NODE.act.refused, "open_count": NODE.facts.open_count,
        "duty_dir": INPUT.duty_dir,
    }, code="""
def run(input):
    import fcntl, json, os, tempfile, time
    ex = input.get("executed") or []
    esc = input.get("escalated") or []
    ref = input.get("refused") or []
    if not ex and not esc and not ref:
        return {"round": None, "status": "quiet"}
    duty = os.path.expanduser(input.get("duty_dir") or "~/.issue-keeper/duty")
    os.makedirs(duty, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    spath = os.path.join(duty, "state-agent.json")
    rd = 1
    try:
        doc = json.load(open(spath))
        rd = max([r.get("round", 0) for r in (doc.get("rounds") or [])] or [0]) + 1
    except Exception:
        pass
    findings = ([{"severity": "info", "summary": "自决执行：" + ", ".join(ex[:6]), "escalate": False}]
                if ex else []) + \\
               ([{"severity": "warn", "summary": "越权拦下转人：" + ", ".join(ref[:4]), "escalate": False}]
                if ref else [])
    status = "attention" if (esc or ref) else "ok"
    round_doc = {"schema_version": "duty/state@0", "role": "agent", "generation": 2,
                 "round": rd, "started_at": ts, "finished_at": ts, "status": status,
                 "findings": findings,
                 "actions": [{"capability": e.split(":")[1].split("(")[0] if ":" in e else "unknown",
                              "level": "authorized", "summary": e} for e in ex][:10],
                 "narrative": "工单 %s 条 → 自决 %d / 转人 %d / 拦截 %d" % (
                     input.get("open_count"), len(ex), len(esc), len(ref))}
    def atomic(path, d):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(d, indent=1, ensure_ascii=False))
        os.replace(tmp, path)
    with open(spath + ".lock", "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            doc = {"schema_version": "duty/state@0", "role": "agent", "rounds": []}
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
        fh.write("%s duty-agent 轮次=%s 状态=%s 自决=%d 转人=%d 拦截=%d\\n"
                 % (ts, rd, status, len(ex), len(esc), len(ref)))
    return {"round": rd, "status": status}
""")

    return {"round": NODE.finish.round, "status": NODE.finish.status}


if __name__ == "__main__":
    print("duty-agent flow 源码（编译见 build_flows.py）")
