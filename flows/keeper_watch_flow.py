#!/usr/bin/env python3
"""keeper-watch —— B 值守 flow（影子期 v0.1；ctrl 队列专用）。

jeffkit 2026-10-08 拍板「值守 flow 化 + 事件分类分队列」的第一件：
B 班巡检以 flow 形态跑在 `plaita:flow:queue:ctrl`（Mac 专用 worker-ctrl-1），
与管线 v2 队列完全隔离。影子期 = 只读观察（不碰 GitHub/state/磁盘/服务），
产出落 controller 台账（rounds.log + 滚动 handoff），与现役 zcode B 并行对账。

图结构（引擎逻辑在图里；CODE 只做 IO 叶子）：
  facts（CODE：远端 keeper/队列/磁盘 + shadow 快照，全 ssh 只读）
    → triage（CODE：纯规则判定 → findings + report JSON 字符串）
    → watch（AGENTRUN：glm53-flash，repo=大仓根；读最新滚动 handoff + 内嵌 facts，
             产出本轮简报；影子期禁止一切变更动作）
    → finish（CODE：简报落 rounds.log 一行 + 新滚动 handoff 文件）

运行前提：Mac worker（~/.plaita/agents.json 有 glm53-flash；env.sh 有 GLM key；
ssh tcloud_gz 免密）。发布三步配方见 controller/duty-as-flow-proposal-20261008.md。
编译：PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_keeper_watch.py
"""
from __future__ import annotations

from plaita.dsl.codeflow import CODE, F, NODE, flow
from plaita.node import register_code_node

register_code_node(default_backend="subprocess")


@flow("keeper-watch", desc="【值守·B 班影子】ctrl 队列只读巡检：facts→triage→agent 简报→rounds+滚动 handoff")
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
    m = re.search(r"(\\d+)%?\\s+/$", df)
    m2 = re.search(r"(\\d+(?:\\.\\d+)?)G\\s+\\d+%?\\s*$", df) or re.search(r"\\s(\\d+(?:\\.\\d+)?)G\\s", df)
    if df:
        try:
            avail = float(m2.group(1)) if m2 else None
            if avail is not None and avail < 20:
                findings.append(f"远端磁盘可用 {avail}G < 20G 守线")
        except Exception:
            pass

    errs = str(f.get("keeper_err_tail") or "")
    n_err = len([l for l in errs.splitlines() if "ERROR" in l])
    if n_err:
        findings.append(f"keeper 日志近尾有 {n_err} 条 ERROR（见 facts）")

    sd = f.get("shadow_dispatch") or {}
    if sd.get("would_dispatch") is None:
        findings.append("shadow latest.json 不可读或缺失（派发环可能停摆）")

    report = json.dumps({"facts": f, "findings": findings}, ensure_ascii=False)
    return {"findings": findings, "report": report}
""")

    # ── ③ watch（AGENTRUN：glm53-flash 宿主直跑；影子期只读纪律写进提示词）────
    watch = AGENTRUN(agent="glm53-flash",
                     repo="/Users/kong/projects/infra4agent",
                     timeout_secs=1200,
                     prompt=F.concat(
        "你是 infra4agent 大仓「B 值守」的影子轮（keeper-watch flow；只读观察）。\n"
        "任务：产出本轮巡检简报。步骤：\n"
        "1) 读磁盘上最新一份滚动交接：`ls -t ~/.issue-keeper/pipeline/controller/handoffs/B-handoff-*.md | head -1` 然后读它"
        "（这是你的上下文：在途/退避/观察项/口径）。\n"
        "2) 结合下面这份实时 facts JSON（远端 keeper/队列/磁盘 + 规则 findings）：\n<<<FACTS>>>\n"
        "3) 产出 markdown 简报，≤40 行，三段：**盘面**（一两行）/ **异常与观察**（对照 handoff 的观察项，"
        "无异常就写「无新增」）/ **建议**（给主控/值守的动作建议；影子期只建议不执行）。\n"
        "铁律：禁止一切变更动作——不 reopen、不删文件、不重启服务、不发 GitHub 评论、不改 state；"
        "你的唯一产出就是这份简报文本。\n", NODE.triage.report, "\n<<<END FACTS>>>\n"))

    # ── ④ finish（IO 叶子：简报落 rounds.log 一行 + 新滚动 handoff）──────────
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
    line = (f"{ts} B-flow 轮次=keeper-watch（影子） "
            f"落地=简报落 handoffs/B-handoff-{stamp}.md "
            f"findings={len(findings)} 摘要={one}\\n")
    with open(base / "rounds.log", "a", encoding="utf-8") as fh:
        fh.write(line)

    hd = (f"# B 班滚动交接（keeper-watch 影子轮 {ts} 自动生成）\\n\\n"
          f"**findings**：{json.dumps(findings, ensure_ascii=False)}\\n\\n"
          f"**简报**：\\n\\n{text}\\n\\n"
          f"---\\n（本文件由 keeper-watch flow 生成；下一轮开机读最新 B-handoff-*.md）\\n")
    out = base / "handoffs" / f"B-handoff-{stamp}.md"
    out.write_text(hd, encoding="utf-8")
    return {"handoff": str(out), "rounds_line": line.strip()}
""")

    return {"handoff": NODE.finish.handoff, "brief_head": NODE.watch.text}


if __name__ == "__main__":
    print("keeper_watch flow 源码（编译见 build_keeper_watch.py）")
