#!/usr/bin/env python3
"""keeper-watch —— B 值守 flow（值守版 v0.2；ctrl 队列专用）。

jeffkit 2026-10-08 拍板「值守 flow 化 + 事件分类分队列」；影子对账当日 5/5 一致后
提前进 Phase 2（jeffkit：「一天下来已经好多次了，基本够了」）——本版接管 B 班
动作权：巡检 + **按权限行动**（磁盘回收/reopen/回评/指令板处置/简报发帖），
zcode B 降为每日抽查。跑在 `plaita:flow:queue:ctrl`（Mac worker-ctrl-1），
与管线 v2 队列完全隔离。

图结构（引擎逻辑在图里；CODE 只做 IO 叶子）：
  facts（CODE：远端 keeper/队列/磁盘 + shadow 快照，全 ssh 只读）
    → triage（CODE：纯规则判定 → findings + report JSON 字符串）
    → watch（AGENTRUN：glm53-flash，repo=大仓根；读最新滚动 handoff + 内嵌 facts，
             执行值守动作【红线内】+ 简报发 #3）
    → finish（CODE：总结落 rounds.log 一行 + 新滚动 handoff 文件）

红线（写死在 agent 提示词）：iOS 模拟器绝不动；删 */target 前必查 ~/.local/bin
软链；live run target 与 ~/.cargo/registry 不可删；不重启任何 worker/keeper
（重启=升级 #2 报请）；不代推他人提交；重大拍板 → #2 留言不擅动。

编译：PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_keeper_watch.py
"""
from __future__ import annotations

from plaita.dsl.codeflow import CODE, F, NODE, flow
from plaita.node import register_code_node

register_code_node(default_backend="subprocess")


@flow("keeper-watch", desc="【值守·B 班】ctrl 队列巡检+动作（红线内）：facts→triage→agent 值守轮→rounds+滚动 handoff")
def keeper_watch(INPUT):
    # ── ① facts（IO 叶子：全只读；每条命令独立超时，失败置 None 不炸节点）─────
    facts = CODE(id="facts", lang="python", input={}, code="""
def run(input):
    import json, subprocess

    def sh(cmd, timeout=40):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return (r.stdout or "").strip()
        except Exception as e:
            return f"__err__ {e}"

    df_remote = sh("ssh -o ConnectTimeout=8 tcloud_gz 'df -h / | tail -1'")
    keeper_err = sh("ssh -o ConnectTimeout=8 tcloud_gz "
                    "'grep -iE \\"ERROR|WARN\\" ~/.issue-keeper/keeper.log | tail -5'")[:800]
    shadow = sh("ssh -o ConnectTimeout=8 tcloud_gz 'cat ~/.issue-keeper/shadow/latest.json'")

    disp = {}
    try:
        d = json.loads(shadow)
        disp = {"ts": d.get("generated_at"), "dispatch": d.get("dispatch"),
                "would_dispatch": d.get("would_dispatch"),
                "skip": d.get("skip_by_reason")}
    except Exception:
        disp = {"raw_head": shadow[:300]}

    q = sh("ssh -o ConnectTimeout=8 tcloud_gz "
           "'docker exec langfuse-v4-redis-1 redis-cli -a 6ace3bde72955c70b9f264e24f57b343 -n 1 "
           "--no-auth-warning XINFO GROUPS plaita:flow:queue:ctrl'")[:400]

    return {"df_remote": df_remote, "keeper_err_tail": keeper_err,
            "shadow_dispatch": disp, "ctrl_queue_groups": q}
""")

    # ── ② triage（纯规则；report = 供 agent 内嵌的 JSON 字符串）──────────────
    triage = CODE(id="triage", lang="python", input={"facts": NODE.facts}, code="""
def run(input):
    import json, re
    f = input.get("facts") or {}
    findings = []

    df = str(f.get("df_remote") or "")
    m2 = re.search(r"(\\d+(?:\\.\\d+)?)G\\s+\\d+%?\\s*$", df) or re.search(r"\\s(\\d+(?:\\.\\d+)?)G\\s", df)
    if df:
        try:
            avail = float(m2.group(1)) if m2 else None
            if avail is not None and avail < 20:
                findings.append(f"远端磁盘可用 {avail}G < 20G 守线 → 按 playbook 红线回收可再生层")
        except Exception:
            pass

    errs = str(f.get("keeper_err_tail") or "")
    n_err = len([l for l in errs.splitlines() if "ERROR" in l])
    if n_err:
        findings.append(f"keeper 日志近尾有 {n_err} 条 ERROR（见 facts，需判读）")

    sd = f.get("shadow_dispatch") or {}
    if sd.get("would_dispatch") is None:
        findings.append("shadow latest.json 不可读或缺失（派发环可能停摆）→ 查 sched/timer 与 keeper 日志")

    report = json.dumps({"facts": f, "findings": findings}, ensure_ascii=False)
    return {"findings": findings, "report": report}
""")

    # ── ③ watch（AGENTRUN：glm53-flash 宿主直跑；值守动作权+红线写死提示词）───
    watch = AGENTRUN(agent="glm53-flash",
                     repo="/Users/kong/projects/infra4agent",
                     timeout_secs=2700,
                     prompt=F.concat(
        "你是 infra4agent 大仓「B 值守」（keeper-watch flow 正班；jeffkit 委托授权，"
        "代表其做派发健康监督、磁盘守卫与卡单处置）。\n"
        "**时间预算 45 分钟**：先做第 3-4 步的动作与简报、最后写第 5 步总结；"
        "一切文件截读（handoff 只取最新一份、directives 只读 issued 段、facts 用给定的不重跑探测）。\n"
        "本轮步骤：\n"
        "1) 读磁盘上最新一份滚动交接：`ls -t ~/.issue-keeper/pipeline/controller/handoffs/B-handoff-*.md | head -1` 并读它"
        "（在途/退避/观察项/口径——你的上下文）。\n"
        "2) 读 `~/.issue-keeper/pipeline/controller/directives.md`：所有 status=issued 且 target 含 B 的条目 →"
        " 执行并在条目下追加回执行（ack:/done:/prog:，带时间与证据）。\n"
        "3) 结合实时 facts JSON（<<<FACTS>>>)：按 playbook（~/.issue-keeper/pipeline/controller/playbook.md）"
        "执行必要动作——磁盘回收（红线内可再生层）、卡单 reopen（ssh 配方见 handoff）、明显卡死的派发环处置。\n"
        "4) 简报发帖：把本轮简报（盘面/动作/异常与观察/建议，≤40 行 markdown）写到 /tmp/kw-brief.md 后"
        " `gh issue comment 3 -R jeffkit/infra4agent --body-file /tmp/kw-brief.md`；"
        "需 jeffkit 拍板的事项发 #2（`gh issue comment 2 -R jeffkit/infra4agent`）报请，不擅动。\n"
        "5) 你的最终输出=本轮总结（做了什么/发现什么/移交下一轮什么，≤30 行）。\n"
        "红线（违反=事故）：iOS 模拟器相关绝不动；删任何 */target 或缓存目录前必查 `~/.local/bin` 软链指向；"
        "live run 的 worktree target 与 `~/.cargo/registry` 不可删；不重启任何 worker/keeper/调度服务"
        "（需要重启 → #2 报请）；不代推他人提交；不动 A 班与主控的 automation；DLQ 只记不清。\n", NODE.triage.report, "\n<<<END FACTS>>>\n"))

    # ── ④ finish（IO 叶子：总结落 rounds.log 一行 + 新滚动 handoff）──────────
    finish = CODE(id="finish",
                  lang="python",
                  input={"text": NODE.watch.text, "report": NODE.triage.report},
                  code="""
def run(input):
    import json, time, pathlib
    base = pathlib.Path.home() / ".issue-keeper/pipeline/controller"
    ts = time.strftime("%Y-%m-%d %H:%M")
    stamp = time.strftime("%Y%m%d-%H%M")
    text = str(input.get("text") or "")
    findings = []
    try:
        findings = json.loads(input.get("report") or "{}").get("findings") or []
    except Exception:
        pass

    one = " ".join(text.strip().splitlines()[0:1])[:160]
    line = (f"{ts} B-flow 轮次=keeper-watch（值守） "
            f"落地=简报发 #3 + handoffs/B-handoff-{stamp}.md "
            f"findings={len(findings)} 摘要={one}\\n")
    with open(base / "rounds.log", "a", encoding="utf-8") as fh:
        fh.write(line)

    hd = (f"# B 班滚动交接（keeper-watch 值守轮 {ts} 自动生成）\\n\\n"
          f"**findings**：{json.dumps(findings, ensure_ascii=False)}\\n\\n"
          f"**本轮总结**：\\n\\n{text}\\n\\n"
          f"---\\n（本文件由 keeper-watch flow 生成；下一轮开机读最新 B-handoff-*.md）\\n")
    out = base / "handoffs" / f"B-handoff-{stamp}.md"
    out.write_text(hd, encoding="utf-8")
    return {"handoff": str(out), "rounds_line": line.strip()}
""")

    return {"handoff": NODE.finish.handoff, "brief_head": NODE.watch.text}


if __name__ == "__main__":
    print("keeper_watch flow 源码（编译见 build_flows.py keeper-watch）")
