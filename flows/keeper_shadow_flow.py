#!/usr/bin/env python3
"""keeper 派发决策影子（**只读**）—— keeper→flow Phase 2 的第一件产物。

目标：把「keeper 本轮会派发哪些 issue、其余为什么跳过」用 flow 形态**独立算一遍**，
与现役 keeper 的实际决策影子对账；对账若干 case 后再切派发权（Phase 3）。

红线（本 flow 只读）：
- 不派发、不回评、不改 state.json / internal.db / GitHub；唯一输出 = 本机
  `~/.issue-keeper/shadow/decisions-<ts>.json` + `latest.json`（落在执行机上）。
- screener 是 LLM 判定，影子**不复制**（成本/非确定性）——对 keeper 尚未建状态的
  issue 标记 `would_screen_then_dispatch`（预期发散类，对账时单列）。

图结构（引擎逻辑在图里；CODE 只做 IO 叶子）：
  facts（读 keeper config/state/runs.jsonl + gh 扫各仓 open issues → join 好的事实清单）
    → MAP gate（图内 if/elif 链 = 闸门次序，对齐 keeper 实际判定）
    → report（落盘 + 摘要）

运行前提：**在保持有 keeper 状态的那台机上跑**（现为 tcloud_gz）——调度 params 里
带 `repo=/home/ubuntu/projects/infra4agent/issue-keeper`，worker 亲和机制会把它
交接给远端机（Mac worker 判路径不存在 → TaskNotForThisWorker 交接）。

编译：PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_keeper_shadow.py
（code= 不能引用模块常量——codeflow 实锤坑；下列 code 一律写完整字面量。）
"""
from __future__ import annotations

from plaita.dsl.codeflow import CODE, MAP, flow
from plaita.node import register_code_node

register_code_node(default_backend="subprocess")


@flow("keeper-shadow", desc="【影子/金丝雀】keeper 派发决策 + 可选 canary 派发——dispatch_repos 空=纯影子零写入；白名单内仓真派发（复用 keeper 库写回，reaper 可收尾）")
def keeper_shadow(INPUT):
    # ── ① 事实装载（IO 叶子：config/state/台账 + gh 扫单，join 成 items）─────
    facts = CODE(id="facts", lang="python", input={"ik_home": INPUT.ik_home}, code="""
def run(input):
    import json, os, subprocess, time
    IK = os.path.expanduser(input.get("ik_home") or "~/.issue-keeper")
    # ---- keeper 配置（pipeline_repos / 闸门参数）
    try:
        import yaml
        cfg = yaml.safe_load(open(os.path.join(IK, "config.yaml"), encoding="utf-8")) or {}
    except Exception as e:
        return {"error": "config 读取失败: %s" % e, "items": [], "errors": [],
                "generated_at": int(time.time()), "g_inflight": 0, "g_max": 0, "repos_scanned": 0}
    repos = {k: v for k, v in (cfg.get("pipeline_repos") or {}).items()
             if (v or {}).get("mode") != "readonly"}
    g_max = int(cfg.get("pipeline_max_in_flight") or 0)
    r_limits = {str(k): int(v) for k, v in (cfg.get("pipeline_repo_limits") or {}).items()}
    issue_daily = int(cfg.get("pipeline_issue_daily_limit") or 0)
    author_daily = int(cfg.get("author_daily_limit") or 0)
    exempt = {str(a).lower() for a in (cfg.get("author_daily_limit_exempt") or [])}
    allow = {str(a).lower() for a in (cfg.get("author_allowlist") or [])}
    # 2026-10-08 外部验收闭环（jeffkit 拍板）：external_authors = 不在 allowlist 的
    # 外部作者，其 issue 同样接单开发（screener 照走；验收由 issue-accept flow 承接）。
    external = {str(a).lower() for a in (cfg.get("external_authors") or [])}
    optout = [str(x) for x in (cfg.get("opt_out_labels") or ["keeper-ignore"])]
    # ---- keeper 状态（processed/blocked/在途/退避/screener 连击）
    try:
        st = json.load(open(os.path.join(IK, "state.json"), encoding="utf-8"))
    except Exception:
        st = {"repos": {}}
    srepos = st.get("repos") or {}
    # ---- 在途计数（镜像 keeper _count_in_flight：全网 in_flight_since 逐仓累计）
    g_inflight = 0
    r_inflight = {}
    for slug, rv in srepos.items():
        for _num, it in (((rv or {}).get("items")) or {}).items():
            if it.get("in_flight_since"):
                g_inflight += 1
                r_inflight[slug] = r_inflight.get(slug, 0) + 1
    # ---- 台账 runs.jsonl：今日有效 run 计数（与 keeper 同口径：非终态类才计额度）
    NON_QUOTA = ("retry-later", "failed", "guarded", "partial")
    today = time.strftime("%Y-%m-%d")
    issue_today, author_today = {}, {}
    try:
        with open(os.path.join(IK, "pipeline", "runs.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not str(rec.get("ts", "")).startswith(today):
                    continue
                if rec.get("status") in NON_QUOTA:
                    continue
                key = "%s#%s" % (rec.get("repo"), rec.get("issue"))
                issue_today[key] = issue_today.get(key, 0) + 1
                au = (rec.get("author") or "").lower()
                if au:
                    author_today[au] = author_today.get(au, 0) + 1
    except Exception:
        pass
    # ---- 扫单（gh，按仓并行——sandbox 墙钟默认 10s，串行 14 仓必超）
    now_ts = time.time()
    items, errors = [], []
    def _scan(repo_full):
        try:
            p = subprocess.run(["gh", "issue", "list", "--repo", repo_full, "--state", "open",
                                "--limit", "100",
                                "--json", "number,title,author,labels,createdAt,updatedAt"],
                               capture_output=True, text=True, timeout=8)
            if p.returncode != 0:
                return repo_full, None, "gh exit=%s %s" % (p.returncode, (p.stderr or "").strip()[:160])
            return repo_full, json.loads(p.stdout or "[]"), None
        except Exception as e:
            return repo_full, None, str(e)[:160]
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=6) as ex:
        scanned = list(ex.map(_scan, sorted(repos)))
    for repo_full, issues, err in scanned:
        if err is not None:
            errors.append("%s: %s" % (repo_full, err))
            continue
        slug = repo_full.replace("/", "-")
        for iss in issues:
            num = iss.get("number")
            it = (((srepos.get(slug) or {}).get("items") or {}).get(str(num))) or {}
            author = ((iss.get("author") or {}).get("login") or "")
            labels = [l.get("name") for l in (iss.get("labels") or []) if isinstance(l, dict)]
            items.append({
                "repo": repo_full, "number": num,
                "title": (iss.get("title") or "")[:120],
                "author": author, "labels": labels,
                "created_at": iss.get("createdAt") or "",
                "updated_at": iss.get("updatedAt") or "",
                "opt_out": any(l in optout for l in labels),
                "allow_ok": (not allow) or (author.lower() in allow),
                "external_ok": author.lower() in external,
                "has_state": bool(it),
                "st_processed": bool(it.get("processed")),
                "st_blocked": bool(it.get("blocked")),
                "st_inflight": bool(it.get("in_flight_since")),
                "st_retry_after": float(it.get("retry_after") or 0),
                "st_screener_streak": int(it.get("screener_retry_streak") or 0),
                "wakeup_deps": list(it.get("wakeup_deps") or []),
                "issue_today": issue_today.get("%s#%s" % (repo_full, num), 0),
                "author_today": author_today.get(author.lower(), 0),
                "author_exempt": author.lower() in exempt,
                "g_inflight": g_inflight, "g_max": g_max,
                "r_inflight": r_inflight.get(slug, 0),
                "r_limit": r_limits.get(repo_full, -1),
                "issue_daily": issue_daily, "author_daily": author_daily,
                "now_ts": now_ts,
            })
    return {"items": items, "errors": errors, "generated_at": now_ts,
            "g_inflight": g_inflight, "g_max": g_max, "repos_scanned": len(repos)}
""")

    # ── ② 闸门判定（图内 if/elif 链；**次序对齐 keeper 实际判定顺序**：
    #       cycle 级 processed 过滤 → opt-out → 在途 → 退避 → allowlist →
    #       作者/单日限 → blocked → screener 退避 → 全局背压 → 按仓配额）──────
    for x in MAP(NODE.facts.items, id="gate"):
        if x.st_processed == True:
            return {"repo": x.repo, "num": x.number, "title": x.title, "author": x.author,
                    "decision": "skip", "reason": "已处理（processed）"}
        if x.opt_out == True:
            return {"repo": x.repo, "num": x.number, "title": x.title, "author": x.author,
                    "decision": "skip", "reason": "opt-out 标签"}
        if x.st_inflight == True:
            return {"repo": x.repo, "num": x.number, "title": x.title, "author": x.author,
                    "decision": "skip", "reason": "已在途（同 issue run 在跑）"}
        if x.st_retry_after > x.now_ts:
            return {"repo": x.repo, "num": x.number, "title": x.title, "author": x.author,
                    "decision": "skip", "reason": "retry-later 退避中"}
        if x.allow_ok != True and x.external_ok != True:
            return {"repo": x.repo, "num": x.number, "title": x.title, "author": x.author,
                    "decision": "skip", "reason": "作者不在 allowlist/external_authors"}
        if x.author_exempt != True and x.author_daily > 0 and x.author_today >= x.author_daily:
            return {"repo": x.repo, "num": x.number, "title": x.title, "author": x.author,
                    "decision": "skip", "reason": "作者当日 run 达上限", "author_today": x.author_today}
        if x.issue_daily > 0 and x.issue_today >= x.issue_daily:
            return {"repo": x.repo, "num": x.number, "title": x.title, "author": x.author,
                    "decision": "skip", "reason": "本 issue 当日 run 达上限", "issue_today": x.issue_today}
        if x.st_blocked == True:
            return {"repo": x.repo, "num": x.number, "title": x.title, "author": x.author,
                    "decision": "skip", "reason": "blocked（依赖/screener 判定）", "deps": x.wakeup_deps}
        if x.st_screener_streak > 0:
            return {"repo": x.repo, "num": x.number, "title": x.title, "author": x.author,
                    "decision": "skip", "reason": "screener 未判定退避中", "streak": x.st_screener_streak}
        if x.g_max > 0 and x.g_inflight >= x.g_max:
            return {"repo": x.repo, "num": x.number, "title": x.title, "author": x.author,
                    "decision": "skip", "reason": "全局背压满（在途到闸）",
                    "g_inflight": x.g_inflight, "g_max": x.g_max}
        if x.r_limit == 0:
            return {"repo": x.repo, "num": x.number, "title": x.title, "author": x.author,
                    "decision": "skip", "reason": "本仓停派（配额=0）"}
        if x.r_limit > 0 and x.r_inflight >= x.r_limit:
            return {"repo": x.repo, "num": x.number, "title": x.title, "author": x.author,
                    "decision": "skip", "reason": "本仓配额满",
                    "r_inflight": x.r_inflight, "r_limit": x.r_limit}
        if x.has_state == True:
            return {"repo": x.repo, "num": x.number, "title": x.title, "author": x.author,
                    "decision": "would_dispatch", "reason": "可派发", "has_state": True}
        return {"repo": x.repo, "num": x.number, "title": x.title, "author": x.author,
                "decision": "would_dispatch", "reason": "可派发（需先过 screener：keeper 无该 issue 状态）",
                "has_state": False}

    # ── ③ 派发（canary 白名单；空=纯影子零写入）──────────────────────────
    # 红线：只有 `dispatch_repos` 白名单内的仓、且 has_state=True（已被 keeper 过筛）
    # 的单才会真派发；派发与状态写回**复用 keeper 库函数**（_dispatch_pipeline /
    # save_state_item），保证 in_flight 锚 / 产物 / 去重语义与 keeper 一字不差，
    # 使现役 keeper 的 reaper 能正常收尾这些 run；keeper 轮末的 save_state_merged
    # （「没变的一律保留盘上值」）保护本节点写入不被覆盖。
    disp = CODE(id="dispatch", lang="python", input={"rows": NODE.gate,
                                                     "dispatch_repos": INPUT.dispatch_repos,
                                                     "ik_home": INPUT.ik_home,
                                                     "repo_root": INPUT.repo_root}, code="""
def run(input):
    import json, os, re, sys, time
    allow_raw = [str(x) for x in (input.get("dispatch_repos") or [])]
    if not allow_raw:
        return {"mode": "shadow", "dispatched": [], "note": "dispatch_repos 空：纯影子，零写入"}
    allow_all = "*" in allow_raw
    allow = set(x for x in allow_raw if x != "*")
    ik = os.path.expanduser(input.get("ik_home") or "~/.issue-keeper")
    root = os.path.expanduser(input.get("repo_root") or "")
    if root and root not in sys.path:
        sys.path.insert(0, root)
    # worker 进程 env 不含 keeper 的 env.sh（DEEPSEEK_API_KEY 等）——手工注入，
    # 否则 screener 的 ${DEEPSEEK_API_KEY} 展开为空、LLM 调用必失败。
    try:
        envf = os.path.join(ik, "env.sh")
        for line in open(envf, encoding="utf-8"):
            m = re.match(r"[ \t]*(?:export[ \t]+)?([A-Z_][A-Z0-9_]*)=(.+)", line.strip())
            if m and m.group(1) not in os.environ:
                v = m.group(2).split(" #", 1)[0].strip()   # 剥行内注释（env.sh 实有）
                os.environ[m.group(1)] = v.strip('"').strip("'")
    except OSError:
        pass
    from issue_keeper.config import load_config
    from issue_keeper.sources import Resource
    from issue_keeper.state import load_state, save_state_item
    from issue_keeper import keeper as K
    cfg = load_config(os.path.join(ik, "config.yaml"))
    st = load_state(cfg.state_path)
    inflight = sum(1 for rs in st.repos.values()
                   for it in rs.items.values() if it.in_flight_since)
    budget = max(0, int(cfg.pipeline_max_in_flight or 0) - inflight)
    bindings = {b.repo: b for b in cfg.repos}
    src_cache = {}
    out = {"mode": "canary", "allow": "*" if allow_all else sorted(allow),
           "budget_left": budget, "screened": [], "dispatched": [], "skipped": []}

    def _persist_fields(slug, num, it):
        def _mut(x):
            x.blocked = it.blocked
            x.screener_retry_streak = it.screener_retry_streak
            x.retry_after = it.retry_after
            x.in_flight_since = it.in_flight_since
        save_state_item(cfg.state_path, slug, str(num), _mut)

    # 派发次序对齐 keeper：priority_repos 优先，其次按仓/单号（槽位竞争的公平性）
    prio = set(cfg.pipeline_priority_repos or [])
    rows = sorted(input.get("rows") or [],
                  key=lambda r: (0 if r.get("repo") in prio else 1,
                                 r.get("repo") or "", int(r.get("num") or 0)))
    for r in rows:
        repo, num = r.get("repo"), r.get("num")
        if r.get("decision") != "would_dispatch":
            continue
        if not (allow_all or repo in allow):
            continue
        if budget <= 0:
            out["skipped"].append({"repo": repo, "num": num, "why": "预算用尽（在途到闸）"})
            continue
        b = bindings.get(repo)
        if b is None:
            out["skipped"].append({"repo": repo, "num": num, "why": "无绑定"})
            continue
        # state 的 repo 键约定 = repo 全名 replace("/","-")（keeper 同款；
        # 用裸仓名会创建幽灵键，reaper 看不到 → run 永不收尾）
        slug = repo.replace("/", "-")
        it = st.repo(slug).item(str(num))
        if it.in_flight_since:
            out["skipped"].append({"repo": repo, "num": num, "why": "状态已在途（并发窗口）"})
            continue
        pc = cfg.pipeline_repo_cfg(repo)
        import subprocess
        try:
            p = subprocess.run(["gh", "issue", "view", str(num), "--repo", repo,
                                "--json", "body,title,author,labels,createdAt,updatedAt"],
                               capture_output=True, text=True, timeout=8)
            d = json.loads(p.stdout or "{}") if p.returncode == 0 else {}
        except Exception:
            d = {}
        res = Resource(kind="issue", number=int(num),
                       title=d.get("title") or r.get("title") or "",
                       body=d.get("body") or "",
                       author=(d.get("author") or {}).get("login") or r.get("author") or "",
                       labels=[l.get("name") for l in (d.get("labels") or []) if isinstance(l, dict)],
                       state="open", created_at=d.get("createdAt") or r.get("created_at") or "",
                       updated_at=d.get("updatedAt") or r.get("updated_at") or "",
                       status="", actor_type="agent")
        label = "%s issue#%s" % (repo, num)
        # ── intake 承接：未过筛的单由 flow 走 screener（keeper 库同款语义）──
        if not r.get("has_state"):
            try:
                sc = cfg.screener
                src = K._ensure_source(b, src_cache)
                vp = K._visible_prefix(b, cfg)
                if sc.enabled and K._author_trusted_by_screener(cfg, res.author):
                    out["screened"].append({"repo": repo, "num": num, "verdict": "trusted-pass"})
                elif sc.enabled:
                    msg = K._compose_new_message(b, res, src, K._agent_label(b, cfg), cfg)
                    verdict = K._screen_or_block(msg, sc, label + " body")
                    vd = K._screener_disposition(verdict, sc)
                    if vd == "block":
                        it.blocked = True
                        it.screener_retry_streak = 0
                        if sc.on_unsafe == "comment":
                            K._post_unsafe_notice(src, b, res, cfg.bot_marker, vp,
                                                  reason=verdict.reason)
                        _persist_fields(slug, num, it)
                        out["screened"].append({"repo": repo, "num": num, "verdict": "block"})
                        continue
                    if vd != "pass":
                        K._hold_for_screener(src, b, cfg, res, sc, it, verdict,
                                             visible_prefix=vp, label=label)
                        _persist_fields(slug, num, it)
                        out["screened"].append({"repo": repo, "num": num, "verdict": vd})
                        continue
                    out["screened"].append({"repo": repo, "num": num, "verdict": "pass"})
            except Exception as e:
                out["skipped"].append({"repo": repo, "num": num,
                                       "why": "screener 异常: %s" % str(e)[:140]})
                continue
        # ── 派发（keeper 库同款：payload/产物/锚 一字不差）──
        try:
            pres = K._dispatch_pipeline(cfg, b, res, it, label, pc=pc)
        except Exception as e:
            out["skipped"].append({"repo": repo, "num": num,
                                   "why": "派发异常: %s" % str(e)[:140]})
            continue
        if it.in_flight_since:
            anchor = it.in_flight_since
            save_state_item(cfg.state_path, slug, str(num),
                            lambda x, a=anchor: setattr(x, "in_flight_since", a))
            out["dispatched"].append({"repo": repo, "num": num,
                                      "status": pres.get("status"), "label": label,
                                      "anchor": anchor})
            budget -= 1
            out["budget_left"] = budget
        else:
            out["skipped"].append({"repo": repo, "num": num,
                                   "why": "dispatch 未落锚: %s" % pres.get("status")})
    return out
""")

    # ── ④ 报告落盘（IO 叶子）────────────────────────────────────────────
    rep = CODE(id="report", lang="python", input={"rows": NODE.gate, "ik_home": INPUT.ik_home,
                                                  "facts_errors": NODE.facts.errors,
                                                  "generated_at": NODE.facts.generated_at,
                                                  "dispatch_result": NODE.disp,
                                                  "dispatch_repos": INPUT.dispatch_repos}, code="""
def run(input):
    import json, os, time
    rows = input.get("rows") or []
    IK = os.path.expanduser(input.get("ik_home") or "~/.issue-keeper")
    outdir = os.path.join(IK, "shadow")
    os.makedirs(outdir, exist_ok=True)
    would = [r for r in rows if r.get("decision") == "would_dispatch"]
    skips = {}
    for r in rows:
        if r.get("decision") != "would_dispatch":
            skips[r.get("reason")] = skips.get(r.get("reason"), 0) + 1
    doc = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "keeper-shadow flow（决策影子 + 可选 canary 派发；对照物=现役 keeper 的 keeper.log 决策）",
        "repos_scanned": len({r.get("repo") for r in rows}),
        "total_open_matched": len(rows),
        "would_dispatch": len(would),
        "skip_by_reason": skips,
        "facts_errors": input.get("facts_errors") or [],
        "dispatch": input.get("dispatch_result") or {},
        "rows": rows,
    }
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(outdir, "decisions-%s.json" % stamp)
    for p in (path, os.path.join(outdir, "latest.json")):
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
    dres = doc["dispatch"] or {}
    summary = "keeper-shadow: open=%d would_dispatch=%d dispatched=%d skips=%s mode=%s" % (
        len(rows), len(would), len(dres.get("dispatched") or []),
        json.dumps(skips, ensure_ascii=False), dres.get("mode") or "-")
    return {"summary": summary, "path": path, "would_dispatch": len(would),
            "dispatched": len(dres.get("dispatched") or []),
            "facts_errors": doc["facts_errors"], "doc": doc}
""")
    return rep
