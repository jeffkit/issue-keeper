#!/usr/bin/env python3
"""issue-accept —— A 班验收 flow（外部验收闭环 · v0.1，ctrl 队列专用）。

2026-10-08 夜 jeffkit 拍板的 E2E 闭环的验收侧：
  外部用户提 issue → keeper-shadow 派发 → self-improve-v2 开发自测 → reaper 回评 done
  → **本 flow 初验 + 回评「请验收」** → 外部用户评论 /accept → **本 flow 合并 PR + 关单致谢**
  （B 班 keeper-watch 流程不参与外部单，仅供内部批量提 issue 场景。）

图结构（线性链，引擎逻辑在图里；CODE 只做 IO 叶子——同 keeper-watch 纪律）：
  facts（CODE：扫 open issue 的评论信号，全 gh 只读）
    → triage（CODE：纯规则 → to_verify / to_close / to_reject + report）
    → verify（AGENTRUN deepseek-flash：读 issue+PR diff 初验；空单 no-op）
    → act（CODE：发 awaiting-accept 回评 / 合并 PR + 关单致谢）
    → finish（CODE：duty state 滚动窗口 + handoff + rounds 一行）

状态放两处（都非本机私有）：
  - **issue 评论里的 marker `<!-- duty:awaiting-accept -->`** = "已初验待外部验收"，
    天然幂等（重跑不会重复发）；
  - duty 内核（~/.issue-keeper/duty/，文件为准）= 轮次滚动窗口 + 交接（协议 §4.1）。

防循环：本 flow 的回评以 `<!-- issue-pipeline -->` 开头（keeper/triage 认作自己人，
不触发评论 agent），并带 jeffkit 身份（self_identity 层兜底）。

验收信号（外部用户，作者 ≠ BOT）：
  /accept 或含「验收通过」→ 合并 PR + 关单致谢
  /reject 或含「验收不通过」→ 只记 finding（本轮不自动动作）

编译：PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_issue_accept.py
"""
from __future__ import annotations

from plaita.dsl.codeflow import CODE, F, NODE, flow
from plaita.node import register_code_node

register_code_node(default_backend="subprocess")


@flow("issue-accept", desc="【值守·A 班】外部验收闭环（tunely 试点）：初验 done 单→回评请验收→/accept 合并关单；ctrl 队列 10min")
def issue_accept(INPUT):
    # ── ① facts（IO 叶子：gh 只读扫描；每命令独立超时，失败置空不炸节点）─────
    facts = CODE(id="facts", lang="python", input={
        "repo": INPUT.repo, "bot": INPUT.bot, "max_scan": INPUT.max_scan,
    }, code="""
def run(input):
    import json, subprocess

    REPO = input.get("repo") or "jeffkit/tunely"
    BOT = (input.get("bot") or "jeffkit").strip().lower()
    MAX_SCAN = int(input.get("max_scan") or 20)

    def sh(cmd, timeout=40):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return (r.stdout or "").strip()
        except Exception as e:
            return ""

    raw = sh("gh issue list -R %s --state open --json number,title,author,updatedAt,labels" % REPO)
    try:
        issues = json.loads(raw)
    except Exception:
        issues = []
    issues.sort(key=lambda x: str(x.get("updatedAt") or ""), reverse=True)
    issues = issues[:MAX_SCAN]

    ACCEPT = ("/accept", "验收通过")
    REJECT = ("/reject", "验收不通过")

    items = []
    for it in issues:
        n = it.get("number")
        cj = sh('gh api "repos/%s/issues/%s/comments?per_page=100"' % (REPO, n))
        try:
            comments = json.loads(cj)
        except Exception:
            comments = []
        bot_done_at, awaiting_at = None, None
        accept_sig, reject_sig = None, None
        last_bot_done_head = ""
        for c in comments:
            au = ((c.get("user") or {}).get("login") or "").strip().lower()
            body = c.get("body") or ""
            cat = c.get("created_at") or ""
            if au == BOT and "[issue-pipeline]" in body and "status=done" in body:
                bot_done_at = cat
                last_bot_done_head = body[:600]
            if "duty:awaiting-accept" in body:
                awaiting_at = cat
            if au and au != BOT:
                low = body.strip().lower()
                if low.startswith("/accept") or "验收通过" in body:
                    if awaiting_at is None or cat >= awaiting_at:
                        accept_sig = {"at": cat, "by": au, "head": body[:200]}
                if low.startswith("/reject") or "验收不通过" in body:
                    if awaiting_at is None or cat >= awaiting_at:
                        reject_sig = {"at": cat, "by": au, "head": body[:200]}
        if bot_done_at or awaiting_at or accept_sig or reject_sig:
            items.append({
                "number": n, "title": it.get("title") or "",
                "author": ((it.get("author") or {}).get("login") or ""),
                "bot_done_at": bot_done_at, "awaiting_at": awaiting_at,
                "accept": accept_sig, "reject": reject_sig,
                "last_bot_done_head": last_bot_done_head,
            })
    return {"items": items, "repo": REPO, "bot": BOT}
""")

    # ── ② triage（纯规则）────────────────────────────────────────────────
    triage = CODE(id="triage", lang="python", input={
        "items": NODE.facts.items, "max_verify": INPUT.max_verify, "max_close": INPUT.max_close,
    }, code="""
def run(input):
    import json
    items = input.get("items") or []
    max_verify = int(input.get("max_verify") or 2)
    max_close = int(input.get("max_close") or 3)

    to_verify, to_close, to_reject = [], [], []
    for it in sorted(items, key=lambda x: x.get("number") or 0):
        if it.get("accept"):
            if it.get("awaiting_at"):
                to_close.append(it)
        elif it.get("reject"):
            if it.get("awaiting_at"):
                to_reject.append(it)
        elif it.get("bot_done_at") and not it.get("awaiting_at"):
            to_verify.append(it)

    findings = []
    for it in to_reject:
        findings.append({"severity": "warn",
                         "summary": "issue #%s 外部验收方报告问题（/reject）：%s"
                                    % (it.get("number"), (it.get("reject") or {}).get("head", "")[:150]),
                         "escalate": True})
    report = json.dumps({
        "repo": "", "to_verify": to_verify[:max_verify],
        "to_close": to_close[:max_close], "to_reject": to_reject,
        "findings": findings,
    }, ensure_ascii=False)
    return {"to_verify": to_verify[:max_verify], "to_close": to_close[:max_close],
            "to_reject": to_reject, "findings": findings, "report": report}
""")

    # ── ③ verify（AGENTRUN：初验；空单 no-op）────────────────────────────
    verify = AGENTRUN(agent="deepseek-flash",
                      repo="/Users/kong/projects/infra4agent",
                      timeout_secs=900,
                      prompt=F.concat(
        "你是 infra4agent 大仓的「A 班验收」（issue-accept flow 初验轮；jeffkit 委托授权）。\n"
        "本轮待初验清单（可能为空）内嵌在 <<<REPORT>>> 里（JSON 的 to_verify 字段，"
        "repo 字段在 facts 段）。\n\n"
        "职责：对每个待初验 issue 做**独立读码初验**（不是开发方的自测复述）：\n"
        "1) `gh issue view <N> --repo <repo>` 读需求与全部评论；\n"
        "2) 找关联 PR：`gh pr list --repo <repo> --state open --json number,title,headRefName` "
        "（head 分支通常含 issue 号）；无 open PR → 在该仓 `git log --oneline -8 --all --grep \"#<N>\"` "
        "找落地提交（push_mode=main 的仓直接进 main）；\n"
        "3) 读改动：有 PR 用 `gh pr diff <PR#> --repo <repo>`（大 diff 只读 stat + 关键文件）；\n"
        "4) 判定：改动是否**针对该 issue 的需求**、无越范围改动、无明显坏味道。"
        "本轮不重跑测试门（门由管线跑过，你是读码初验）；无法确认就 ok=false 并说明。\n\n"
        "输出纪律：**最后一行输出一行严格 JSON**，形如：\n"
        "{\"results\":[{\"issue\":24,\"ok\":true,\"pr\":17,\"summary\":\"一句话判定理由（中文，≤80字）\"}]}\n"
        "待初验清单为空时：不做任何工具调用，直接输出 {\"results\":[]}。\n\n"
        "<<<REPORT>>>\n", NODE.triage.report, "\n<<<END REPORT>>>\n"))

    # ── ④ act（IO 叶子：发 awaiting 回评 / 合并 PR + 关单）────────────────
    act = CODE(id="act", lang="python", input={
        "repo": NODE.facts.repo, "bot": NODE.facts.bot,
        "to_close": NODE.triage.to_close, "report": NODE.triage.report,
        "verify_text": NODE.verify.text, "dryrun": INPUT.dryrun,
    }, code="""
def run(input):
    import json, subprocess, tempfile, os

    REPO = input.get("repo") or "jeffkit/tunely"
    DRY = bool(input.get("dryrun"))
    to_close = input.get("to_close") or []
    actions, posted, merged, close_errors = [], [], [], []

    def sh(cmd, timeout=60):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return (r.stdout or "").strip() or (r.stderr or "").strip()
        except Exception as e:
            return ""

    def gh_comment(n, body):
        fd, p = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w") as f:
            f.write(body)
        out = sh("gh issue comment %s --repo %s --body-file %s" % (n, REPO, p))
        os.unlink(p)
        return out

    # ── 4a. 解析初验结果 → 发「请验收」──
    results = []
    text = str(input.get("verify_text") or "")
    for line in reversed([x.strip() for x in text.splitlines() if x.strip()]):
        if line.startswith("{") and '"results"' in line:
            try:
                results = json.loads(line).get("results") or []
                break
            except Exception:
                pass
    else:
        i, j = text.rfind("{"), text.rfind("}")
        if 0 <= i < j:
            try:
                results = json.loads(text[i:j + 1]).get("results") or []
            except Exception:
                results = []

    for r in results:
        n = r.get("issue")
        if not n or not r.get("ok"):
            continue
        body = ("<!-- issue-pipeline -->\\n<!-- duty:awaiting-accept -->\\n"
                "## 开发完成，请外部验收\\n\\n**初验结论**：%s\\n\\n" % (r.get("summary") or ""))
        if r.get("pr"):
            body += "关联 PR：#%s\\n\\n" % r.get("pr")
        body += ("验收通过 → 回复 `/accept`\\n发现问题 → 回复 `/reject` + 描述\\n\\n"
                 "（本回评由 issue-accept flow 自动发出）")
        actions.append({"capability": "post_brief", "level": "autonomous",
                        "summary": "初验通过，发待验收回评", "target": "#%s" % n})
        if DRY:
            posted.append({"issue": n, "dryrun": True, "summary": r.get("summary")})
        else:
            out = gh_comment(n, body)
            posted.append({"issue": n, "summary": r.get("summary"), "resp": out[:120]})

    # ── 4b. /accept → 合并 PR + 关单致谢 ──
    for it in to_close:
        n = it.get("number")
        prs = sh("gh pr list --repo %s --state open --json number,headRefName,title" % REPO)
        try:
            prlist = json.loads(prs)
        except Exception:
            prlist = []
        # 2026-10-08 教训（#20 误合 dependabot#15）：标题子串匹配会撞版本号
        # （"bump @types/node from 20.x" 含 "20"）——只认分支名精确后缀
        # issue-<n> / pipeline/issue-<n> / <n>-*，永不匹配标题。
        def _branch_matches(head):
            h = (head or "").strip().lower()
            return (h == "issue-%s" % n or h.endswith("/issue-%s" % n)
                    or h.endswith("-%s" % n) or h.startswith("issue-%s-" % n))
        pr = next((p for p in prlist if _branch_matches(p.get("headRefName"))), None)
        note = ""
        if DRY:
            actions.append({"capability": "close_issue", "level": "authorized",
                            "summary": "[dryrun] 将合并 PR 并关单", "target": "#%s" % n})
            merged.append({"issue": n, "dryrun": True})
            continue
        if pr:
            mo = sh("gh pr merge %s --repo %s --merge" % (pr.get("number"), REPO), timeout=90)
            note = "已合并 PR #%s" % pr.get("number")
            if "not mergeable" in mo.lower() or "unable" in mo.lower():
                close_errors.append({"issue": n, "why": mo[:200]})
                actions.append({"capability": "close_issue", "level": "authorized",
                                "summary": "PR 合并失败，未关单", "target": "#%s" % n})
                continue
        else:
            note = "未发现 open PR（可能已直推 main）"
        gh_comment(n, ("<!-- issue-pipeline -->\\n✅ 外部验收通过（%s）。%s。感谢验收，本单关闭。\\n"
                       "（issue-accept flow 自动收尾）" % ((it.get("accept") or {}).get("by", "?"), note)))
        co = sh("gh issue close %s --repo %s" % (n, REPO))
        actions.append({"capability": "close_issue", "level": "authorized",
                        "summary": "外部 /accept → %s → 关单" % note, "target": "#%s" % n})
        merged.append({"issue": n, "pr": (pr or {}).get("number"), "closed": True})

    return {"actions": actions, "posted": posted, "merged": merged,
            "close_errors": close_errors}
""")

    # ── ⑤ finish（IO 叶子：duty state + handoff + rounds 一行）────────────
    finish = CODE(id="finish", lang="python", input={
        "role": "issue-accept", "dryrun": INPUT.dryrun,
        "actions": NODE.act.actions, "posted": NODE.act.posted,
        "merged": NODE.act.merged,
        "close_errors": NODE.act.close_errors, "findings": NODE.triage.findings,
    }, code="""
def run(input):
    import json, os, time, pathlib, fcntl, tempfile

    base = pathlib.Path("~/.issue-keeper/duty").expanduser()
    base.mkdir(parents=True, exist_ok=True)
    role = input.get("role") or "issue-accept"
    now = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    findings = input.get("findings") or []
    posted = input.get("posted") or []
    merged = input.get("merged") or []

    # 上一轮号（读 state 滚动窗口）
    spath = base / ("state-%s.json" % role)
    prev_round = 0
    generation = 1
    try:
        prev = json.loads(spath.read_text())
        prev_round = max([r.get("round", 0) for r in (prev.get("rounds") or [])] or [0])
    except Exception:
        pass
    try:
        generation = (json.loads((base / "roster.json").read_text()).get("generation")) or 1
    except Exception:
        pass

    status = "ok"
    if input.get("close_errors") or any(f.get("severity") == "critical" for f in findings):
        status = "attention"
    elif findings:
        status = "attention"

    round_doc = {
        "schema_version": "duty/state@0", "role": role, "generation": generation,
        "round": prev_round + 1, "started_at": now, "finished_at": now,
        "status": status,
        "actions": input.get("actions") or [],
        "findings": findings,
        "narrative": "posted=%s merged=%s dryrun=%s"
                     % (len(posted), len(merged), bool(input.get("dryrun"))),
    }

    def atomic(path, doc):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(doc, indent=2, ensure_ascii=False))
        os.replace(tmp, path)

    lock = spath.with_suffix(".lock")
    with open(lock, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            doc = {"schema_version": "duty/state@0", "role": role, "rounds": []}
            try:
                doc = json.loads(spath.read_text())
            except Exception:
                pass
            rounds = doc.setdefault("rounds", [])
            rounds.append(round_doc)
            doc["rounds"] = rounds[-20:]
            doc["updated_at"] = now
            atomic(spath, doc)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    hpath = base / "handoffs" / ("%s-handoff-%s.json" % (role, time.strftime("%Y%m%d-%H%M%S")))
    atomic(hpath, {
        "schema_version": "duty/handoff@0", "role": role, "generation": generation,
        "round": round_doc["round"], "written_at": now,
        "in_flight": [], "todo": [], "watched": [],
        "criteria": [], "environment": {},
        "narrative": round_doc["narrative"],
    })
    try:
        olds = sorted(base.glob("handoffs/%s-handoff-*.json" % role))
        for o in olds[:-20]:
            o.unlink()
    except Exception:
        pass

    with open(base / "rounds.log", "a") as fh:
        fh.write("%s %s 轮次=%s 落地=posted(%s)/merged(%s) findings=%s status=%s\\n"
                 % (now, role, round_doc["round"], len(posted), len(merged),
                    len(findings), status))

    return {"round": round_doc["round"], "status": status, "state": str(spath)}
""")

    return {"round": NODE.finish.round, "status": NODE.finish.status,
            "posted": NODE.act.posted, "merged": NODE.act.merged}


if __name__ == "__main__":
    print("issue-accept flow 源码（编译见 build_issue_accept.py）")
