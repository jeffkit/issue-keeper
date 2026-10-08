# issue-pipeline —— issue-keeper 的 plaita 化管线（@flow 源码 + 生成 JSON）

> v0.3，2026-09-30。per-repo 契约化（见下方专节）。**审查主体是
> `issue_pipeline_flow.py`（@flow 源码），JSON 是编译产物不要手改**：
>
> ```bash
> python3 flows/build_issue_pipeline.py     # 需 PYTHONPATH 含 plaita 与 plaita-nodes/src
> ```
>
> 已验证：codeflow 编译 ✓、plaita `validate_flow_ir` ✓（v0.3 重建后 69 节点）。
> 未做端到端真跑。

## v0.3（2026-09-30）：per-repo 契约化——通用 flow 剥离单仓形状

动机：通用 flow 实际是围绕 recursive 打磨的（预算按 cargo 冷构建调优、review
红线内嵌 recursive 专属内容、门脚本 cargo 专用、基线硬编码 origin/main），
review 结论「不能 cover 所有子仓」。改造后 flow 不再内嵌任何单仓形状：

- **配置面**：`config.yaml` 新增 `pipeline_repos`（repo_full → 契约）：
  `enabled / mode(full|readonly) / base_branch / setup_command / test_command /
  gates[] / gate_timeout_secs / push_mode(branch|pr|main|none) / review_mode /
  review_notes / triage_notes / doc_notes / timeout_overrides`。
  旧 `pipeline_test_commands` 兼容（未登记仓兜底，登记仓以新契约为准）。
- **资格门控（keeper 侧）**：`_pipeline_repo_cfg`——未登记且无门的仓**不进
  管线**，回退 legacy 单 agent 路径。根治「17/18 仓空门跑 `true` 恒过、回评
  却报质量门通过」的假绿。readonly 仓无门也进（只调查不开工）。
- **多门 gate_runner**：`flows/gates/gate_runner.py`——per-gate 预算 + diff
  路径条件（触及 `crates/recursive-tui/**` 才跑 mutants 这类条件门进门），
  先修再跑（首败即停）。门清单放 keeper 配置或仓内 `.issue-keeper/gates.json`
  （惯例归仓）。GATE 节点只调 runner，整门预算 = Σgate+300s 传入
  `INPUT.gate_timeout_secs`。
- **flow 参数化**：`INPUT.base_branch`（wt_add/sync_main/git_publish 全链）、
  `INPUT.setup_command`（fresh worktree 装 node_modules/.venv，失败如实回评）、
  段预算全部 `INPUT.*_timeout`（默认=旧全局值，per-repo 覆盖）。
- **readonly 早退**：investigate 后回评+路由建议（triage_notes 指路），
  不进 plan/implement——数据/分发/镜像仓（recursive-providers、
  argusai-marketplace）与「改行为请去别仓」的仓不再被九段管线误处理。
- **惯例注入**：`INPUT.review_notes/triage_notes/doc_notes` 注入对应段提示词；
  曾硬编码在 review 提示词的 recursive 红线（input_schema oneOf…）迁入
  recursive 的 per-repo 配置；document 段不再假设「CHANGELOG.md Unreleased」
  （Changesets 制仓的正确动作是加 .changeset 文件）。各实施段提示词显式要求
  **先读目标仓 AGENTS.md / CLAUDE.md**。
- **`.github` 禁改名单的显式通道（v0.3.2，#11）**：CI 修复单的修复对象就是
  `.github/workflows/*`，硬拦曾让「实现完成、测试全绿」的 run 以 guarded
  收尾（ilink-hub #39 实证），只能值守手工落地、绕过三门评审。现行双闸门：
  仓库契约 `allow_github_paths`（声明允许的 `.github` 子路径前缀，声明权在
  契约）× issue 正文头部 `ci-fix: true`（触发权在 issue），keeper 在派发时
  合成进 `INPUT.allow_github_paths`（空 = 无例外）。guard 只对清单前缀内的
  `.github/**` 放行；`.github/` 其余部分、`.worktrees/`、目录穿越、绝对路径
  一律照拦——注入者要么拿不到契约要么拿不到 issue 编辑权，改 CI 绕门禁的
  攻击面不变。plan/implement/review 提示词同步感知例外（例外清单为空时
  措辞退化为原禁令）。
- **交付策略**：git_publish（plaita-nodes 0.6.x）`merge_mode` 新增
  `pr`（推分支 + `gh pr create --base <base_branch>`，argusai 家族 PR 制）与
  `none`（只本地 commit 不 push）；`base_branch` 参数化 main 模式推送目标。
  gate/agent_run 节点的 `timeout_secs` 修为表达式求值（DSL 传参是表达式串——
  与 git_publish.merge_mode 同一批坑，pydantic 构造期拒收 str 进 int 字段，
  字段放宽为 Any + execute 内求值）。

**部署链（v0.3 起）**：①重建 flow JSON → ②console 发布新 semver（旧 console
定义不认识新 INPUT 字段，不发布则 bridge 用旧定义、新 payload 字段被忽略）；
③`pip install -e . --break-system-packages` 刷新 plaita-nodes（gate/agent_run/
git_publish 三节点行为变更）④keeper daemon 重启（加载新节点代码）；
⑤config.yaml 迁移到 pipeline_repos（否则除 recursive 外全部仓回退 legacy——
这是有意的安全缺省，不是故障）。

## 输入契约（keeper → run，v0.3）

## v1.0.13（2026-09-29）：post_*/解析器/git 操作沉淀为 plaita-nodes 库节点

plaita-nodes **0.6.0** 新增三节点（`dff35ab`+`ca1654d`，已推 origin），flow 源码 724 → 531 行、
**60 → 59 节点（code 17 → 4：deps/check/guard/kanban）**：

- **`github_comment`×9**：替代 post_* 九连拷——消毒（路径/密钥/`$()` 打码）、
  `dedup_marker` 去重、`footer` 尾注（成功路径核验行由 `F.concat` 拼 `pub.pushed`/`pub.note`）、
  artifact 留档；dry-run 写草稿不连网；
- **`parse_json`×2**（#17 起 ×3，见下）：替代 parsed/verdict——#43 健壮解析策略沉淀入库（逐行倒序严格
  JSON → rfind 切片），`choices` verdict 白名单 + `default` fail-safe（明细追加进
  `notes`/`parse_error`）；triage 的 acceptance 经 `join_fields` 自动拼 `acceptance_str`；
  **#17**：triage 侧 `default.verdict` 退出 choices（`"degraded"`）——「解析失败」
  （基础设施故障）与「真判 blocked」（业务判定）从此可区分，失败路径由
  `parsed.parse_ok != True` 路由进降级分支；分支里的第三个 `parse_json`（`salvage`，
  去掉 verdict 白名单）只做一次容错重解析、结果进注解，**不接业务出害口**（否则要给
  blocked/invalid 各复制一套回评提示词）；
- **`git_publish`×1**：替代 deliver+merge，并**修掉缺口 #6**——旧 deliver 见远端已有
  分支就跳过 commit（重投丢改动），新语义=有改动一律先 commit、远端头==本地头才
  跳过 push；main 模式 ff 合并 `origin/<branch>` 不变。

**部署链注意**：①注册走 pip dist-info entry-points——本机 editable 元数据曾停在
0.5.0 拒收新节点，须 `pip install -e . --break-system-packages` 刷新；build/bridge/run_e2e
已加 `plaita_nodes.register_all()` 显式兜底，不再依赖元数据新鲜度。②console 与 keeper
daemon 均须重启加载 0.6.0 才能解析/执行新节点类型。③`git_publish.merge_mode` 经 DSL
传入是表达式串，节点内已先求值（`ca1654d`）。回归：`tests/test_flow_verdict_parser.py`
改为对生产定义中 verdict 节点（parse_json）的动态构造测试，#43 用例全保留。

## v1.0.12（2026-09-29）：expr 胶水节点清理

plaita `feat/expr-in-assignment` 分支放开 codeflow DSL 表达式位置的比较/and/or/not/三元
（编译为 `$F.eq/$F.and/…/$F.ifelse`，注册表补同名比较函数与 ifelse），据此消掉三个胶水
code 节点，**62 → 60 节点（code 20 → 17）**：

- `reject`：文案改 `F.concat(...)` 内联进 `post_reject` 的 input 表达式；
- `risk_gate`：删除——`if INPUT.review_mode == "human" and parsed.risk == "high"`
  直写复合条件，编译为结构化 and-ConditionGroup（console 可视化更友好）；
- `prep`：改一行赋值 `cmd = INPUT.test_command or 'true'`，两个 GATE 引用 `$NODE.cmd`。

同时 plaita 侧修正 assignment 节点两处坑：`output_type` 类型校验曾匹配原始表达式串
（数值/布尔类型必然 miss 且静默返回 None）改为先求值再匹配、不匹配大声抛错；
假值字面量（`0`/`False`/`""`）不再被真值判断吞掉。
**发布前置条件：keeper daemon 重启**（daemon 常驻进程的 plaita 注册表是启动时 import 的，
不重启执行 `$F.or/eq/ifelse` 会 NameError）。离线验证：假 `gh` shim 走 rejected
路径全绿，`$F.concat` 在真实节点 input 求值正确。

## 管线形状（v0.2.1，编译后 61 节点）

```
screener闸(INPUT.screener_verdict != safe → 拒评短路)
└─ deps预检(正文 #N 引用 → gh 查 issue/PR 状态 → deps_json)
   └─ triage(agent, 查重+定级, 正文只经 body_file；依赖门硬判据)
      ├─ blocked(依赖未就绪/已有在途) → 回评 → END
      ├─ invalid(已在 main 修复/无需改动) → 回评 → END        ← #17 重派问题根治
      ├─ 解析失败（基础设施故障，非业务判定）→ 落盘 triage-raw.txt + 明示故障回评 → 降级继续（不落 blocked）
      └─ actionable / degraded
         └─ git fetch --prune（clone 新鲜度）
            └─ worktree prep（幂等：复用 / prune+复用残留分支重建 / 失败回评）
               └─ investigate(agent 10min, bug 先立失败测试) → 01-investigation.md
                  └─ plan(agent 10min) → 02-plan.md（含 COMMIT_MESSAGE）
                     └─ [review_mode=human 且 risk=high → HITL 1h；未批准 → 「暂缓」回评 END]
                        └─ implement(agent 30min, 禁 .github/**, 不 commit/push)
                           └─ 无改动 → 回评 → END
                              └─ review(agent=glm53-flash 独立审查, 解析失败=abort)
                                 ├─ abort → 回评 → END
                                 ├─ fix → fix_review(实施方按指令修)
                                 └─ approve ▼
                                    gate(INPUT.test_command, 20min)  ← per-repo 配置，非写死
                                    ├─ fail → fix_test(15min) → retest
                                    │           └─ 仍 fail → 回评「在本地 worktree 未推送」→ END
                                    └─ pass → diff护栏(.github/**、>800行 → 待人工)
                                       └─ document → deliver(幂等 commit/push)
                                          └─ push_mode=main → fetch + ff 合并 origin/<branch> → push main
                                             └─ reply(消毒+去重+管线核验尾行) → kanban → END
```

## 对三方审查的处置

**已落进 flow**：screener 结论消费（入口闸）、body 不进 prompt（body_file 引用）、
独立 review（glm53-flash 独立审查方，09-30 前 deepseek-flash 异构模型 + fail-safe abort）、质量门命令参数化、
HITL 条件化+未批准即停、partial/hold 如实「未推送」、deliver/回评幂等（标记去重+
ls-remote 查重）、diff 护栏（.github/** 与超大 diff）、评论出害前消毒
（本机路径/密钥模式 → [REDACTED]）、git fetch 同步（origin/main 基线）、
triage 区分 invalid/blocked-in-flight、
解析失败 fail-safe（triage→基础设施故障：落盘 + 明示故障回评 + 降级继续，#17 起；
review→abort）。

**部署强制项（README 职责，不落 flow）**：
1. 默认 `push_mode: branch`；`main` 模式必须 HITL 可用（HITL_BASE_URL/HITL_URL 已配），
   否则注册时拒绝。
2. 作者 allowlist + 同作者限频 + 全局并发 1-2（keeper 提交 run 前检查）。
3. agent 子进程降权：env 白名单（剔 GH_TOKEN/SSH_AUTH_SOCK）、评论出口用独立低权限
   token、~/.ssh 与 ~/.flowcast 对 agent 进程不可读。
4. runner 启动：`register_code_node(default_backend="subprocess")`；flow 里所有 code 节点
   **显式**声明 `sandbox_backend="subprocess"`（这些节点要跑 git/gh/文件 IO，需要网络+FS，
   docker 档会跑不了；显式声明也让运营者改 default_backend 不会悄悄改变本 flow）。
   单节点墙钟由 bridge 进程的 `PLAITA_SANDBOX_TIMEOUT` 提供（`pipeline_bridge.
   ensure_sandbox_timeout`，默认 900s）。多租户部署用
   `register_code_node(allowed_backends=(...))` 收窄，白名单外的档位在解析期硬失败。

**留给后续立项（flow 之外）**：
- keeper 接线：`_process_resource` 改为向 console 提 issue-pipeline run + 轮询终态；
  **终态 error 且 issue 无评论 → keeper 发 fallback 回评**（引擎层异常不进 flow 业务出害口）。
- agentproc/agent_run 超时 killpg（孤儿根治，今天实测三次 3600s 超时全部留孤儿）。
- per-repo 队列化（同仓并发 1-2）+ 单 run SLA 告警；共享 CARGO_TARGET_DIR/sccache
  降冷构建成本。
- 大题拆分：#25/#26/#29 这类 refactor milestone 在 screener 或提示词层拆小。
- umbrella tracking issue 注入 triage（依赖预检已覆盖 #N 引用；tracking 正文关联
  需先约定 umbrella 标记元数据）。

## issue #1 复盘处置（2026-09-28）

1. **「未发出回评」误报**：早退路径返回 `posted` 而成功路径返回 `comment_posted`，
   keeper 兜底判定只看后者 → 所有早退终态都被误报「管线异常终止」。修复：bridge 出口
   key 归一化 + keeper 双读；兜底文案区分引擎异常与终态未回评。
2. **基线污染**：`pull --ff-only` 在本地 main 领先 origin 时 no-op，worktree 从本地
   HEAD 切分支夹带未推送提交（`origin/e2e/issue-34` 即此机制）。修复：fetch + 显式以
   `origin/main` 为 worktree 基线。
3. **merge 合并错对象**：push_mode=main 时 `merge --ff-only origin/main` 只同步了
   main，分支内容从未进 main，却回报「已 ff 合并推送 main」。修复：ff 合并
   `origin/<branch>`。
4. **落地判定信 agent 自由文本**：成功回评尾部改由管线追加核验行（分支/推送/合并
   事实，取自 deliver/merge 节点返回值），agent 自由文本只讲技术内容。
5. **依赖门无输入可判**：新增 deps 预检节点（正文 #N 引用 → gh 查状态）注入 triage，
   「依赖未合入 main → blocked + 禁止就地实现依赖」写成硬判据（误引用可在 notes
   说明后忽略）。

## issue #43 复盘处置（2026-09-28）

`verdict` 节点的 review 判定解析用「第一个 `{` 到最后一个 `}`」切片。正文里一旦出现
花括号（#43 的正文恰好是 `"preset resolves to: type={}, model={}"`），切片就把正文和
JSON 粘成一段，`json.loads` 必然失败 → fail-safe abort：整轮 ~22 分钟白跑，回评只说
「独立审查未给出可解析结论」，从外部完全看不出是解析器的锅（review 其实老老实实输出了
`{"verdict":"fix",...}`，还在 notes 里指出了 `cargo fmt --check` 失败这个真问题）。

修复：先逐行从后往前找严格 JSON（提示词本来就要求「输出一行严格 JSON」），再退化到
「最后一个 `{` 到最后一个 `}`」；fail-safe 语义（解析不出 = abort）不变，且区分
「解析不出」与「解析出但 verdict 非法」两种文案。回归：
`tests/test_flow_verdict_parser.py`（含 #43 原文形态 + 反证旧写法失败）。
已发布 console **v1.0.3**。

第二轮（同一 issue 重派）又被自己挡住：triage 的查重会 `gh issue view --json comments`，
把**本管线自己上一轮的 abort 回评**当成「已有在途处理」→ 36 秒判 blocked，重派永远
开不了工。修复：triage 提示词明确「带 `<!-- issue-keeper-bot -->` / `<!-- issue-pipeline -->`
标记的评论是本管线自己的历史记录，不算 in-flight；判在途只认远端分支 / 未合并 PR /
人类认领」。同批把 investigate 预算 900 → **1800s**（#41 连续两轮卡在 900s：调研段要先立
失败测试再跑 cargo，而 worktree 的 `target/` 是空的，冷构建常十几分钟；#42 同节点 381s）。
两处一并发布 console **v1.0.4**。

## recursive#2 处置（2026-09-30）：回评/状态同步 + worktree WIP 语义

recursive 侧 agent 实测报来的两个可修点（+一条跨渠道校验建议），逐条落法：

1. **回评已写出、状态却记 blocked（未发出回评）**：#31/#32 跑在 09-27 夜里，早于
   #1 复盘的 `normalize_result`（09-28 12:48），台账 `comment_posted=null` → reaper
   按「未确认发出回评」补了兜底评论，与 issue 上已有的完整 blocked 回评自相矛盾。
   根治分三层：①台账/归一化（已修，见 #1 复盘第 1 条）；②**跨渠道读回校验**——
   reaper 在补兜底前先从目标渠道（GitHub comments）读回：派发之后出现过
   `<!-- issue-pipeline -->` 标记评论（硬证据，**flow 全部出害口现在机械携带该标记**，
   不再只靠最终回评的提示词自觉）或自己账号的非机器评论即认定已回评，跳过兜底；
   渠道读不到时维持兜底（fail-safe）。③工作台派生视角修正：blocked 终态按
   `comment_posted` 分叉——已回评 = blocked（依赖未就绪、唤醒监视中），未发出 =
   needs-human。
2. **引擎崩溃后 worktree 静默留脏**：#33（Goal 394）/ #51 两次实证。语义定为
   **快照（snapshot）**：reaper 收尾 engine_error 时，若 `.worktrees/issue-N` 有
   未提交改动，就地 `git add -A` + commit 到管线分支自身（`wip(issue-N): 管线异常
   终止自动快照`）——半成品变成可 diff/可恢复/可继续的原子提交，不推送、不跑测试，
   下轮 run 按既有提示词「侦察已有进展、就地修正」接着干；快照位置写进兜底回评与
   日志。其余终态（partial/guarded/abort）**有意**保留脏 worktree 等人工接手，不变。
3. **看板收尾补真**：reaper 原来只算 `status_for_board` 没用（死变量）、日志虚报
   「看板→todo/review」。现在对支持状态机的 source（internal 看板）真实移动：
   done/invalid/readonly/nochange 且已回评 → review；blocked/引擎异常/未回评 → todo
   （依赖闭合自动唤醒或人工接手）。GitHub 仓不经此路径——工作台是从台账/metrics
   派生的只读视图。

## issue #13 处置（2026-10-06）：重派路径的建树缺口

**现象**：重派（`reopen` / engine_error 自动重试）首跳即 **engine_error 终态、不可续跑**，
回评都没发出——`RESULT {"status": "engine_error", "error": "执行节点setup出错了:
RuntimeError: FileNotFoundError: ... .worktrees/issue-8 (源码第 208 行)"}`（agentproc#8/#17）。

**根因**：清树（worktree 目录被回收 / reaper 收尾）时 **`pipeline/issue-N` 分支留在仓里**
（分支由 `-b` 建，清树不动 refs）。旧建树段 `wt_add = CAPTURE(git worktree add <dir> -b
<branch> origin/<base>)` 必撞「branch already exists」（实测 rc 255），而 **CAPTURE 对非零退出
不中止流程、其返回码在流程里从未被引用** → 失败被静默吞掉；随后 `setup` code 节点拿
不存在的目录当 cwd，只 catch `TimeoutExpired` → `FileNotFoundError` 逃逸出节点 = 引擎层异常。
`setup_command` 为空的仓（recursive / issue-keeper / ilink-hub）更隐蔽：setup 早退不报错，
炸弹推给 `investigate`/`guard` 等下游节点，**同样是 engine_error，只是死得晚**。

**修法**（`flows/issue_pipeline_flow.py`，编译后 73 节点）：
- `wt_add` capture → **`wt_prep` code 节点**（幂等判定顺序写死）：
  ① 目录已是合法 worktree（`rev-parse --is-inside-work-tree` = true）→ 复用上一轮现场；
  ② 否则 `git worktree prune`（清掉「目录没了、注册还在」的失效项）后，
     **分支存在 → `worktree add <dir> <branch>` 复用残留分支重建**（保留 keeper 的
     `wip(issue-N)` 快照提交）；分支不存在 → `worktree add <dir> -b <branch> origin/<base>`
    首发语义不变；
  ③ 仍失败（目录非空 / 分支被别的 worktree 占用 / 其他 rc≠0）→
     `reply_prep_fail → post_prep_fail → END(partial)`，**绝不进 setup/investigate**。
- `setup` 加固：**空命令早退之前**先判 `worktree_dir` 是否存在（缺失 → `ok=False`，让既有
  `setup.ok == False → reply_setup_fail` 出口接管，补上此前缺失的 `ok` 键）；`except` 从
  `TimeoutExpired` 扩到 `(TimeoutExpired, OSError)`（覆盖 cwd 竞态消失等残余路径）。

**重现/回归命令**（离线，不真打 GitHub/LLM）：
`python3 -m pytest tests/test_issue13_worktree_redispatch.py -q`（清树后重派 / 残留注册 / 缺 cwd /
首发正向对照 / 编译产物结构断言）。人工复核：
`git -C <main_clone> worktree add <clone>/.worktrees/issue-N pipeline/issue-N` → `rm -rf` 该目录 →
重放 `wt_prep`：`prune → add <dir> pipeline/issue-N` rc=0，`git -C <dir> log` 仍见 `wip(issue-N)`。

**发布**：重建 `flows/issue-pipeline.flow.json`（JSON 是产物，不手改）→ console 发布
**v2.1.2**（节点类型未变，无需 `pip install -e .`）。发布前 bridge 仍按 console 定义执行，
**不发布 = 修复不生效**。

## v1.0.5（2026-09-28）：document 提示词 + 质量门转真

- **`document` 段会张冠李戴**：#43 落地时发现它写的 CHANGELOG 条目描述的是 **#45** 的改动
  （编号与内容都不对）。根因是提示词只说「按本仓惯例补一行」，没告诉它「这次改了什么」，
  它就自己猜。现在注入本 issue 号/标题 + `$NODE.parsed.commit_message`，并要求先 `git diff`
  + `git status -uall` 再动笔、条目必须如实描述本次 diff、拿不准就 SKIP。
- **recursive 的质量门由空转真**：`pipeline_test_commands["jeffkit/recursive"]` 原本是空串，
  gate 节点跑 `true` 恒过——回评里的「质量门已通过」其实只来自 agent 自述。现在设为
  `cargo fmt --all --check && cargo test --workspace --no-fail-fast`（**pre-push 守卫**（bridge 安装）：2026-09-29 一个 implement agent 自己 commit → rebase main → 把 main 推了上去，绕过 gate/guard，推上去的代码在 recursive-tui 的 clippy 上直接挂掉。提示词"不要 push"拦不住 agent，于是在 git 层拦——只对 linked worktree 生效、只拦 `refs/heads/main`，人工在主 checkout 的推送与 flow 的 deliver（推分支）/merge（在主 clone 推 main）都不受影响。同时 **gate 脚本补上 clippy**（对齐仓的 CI 契约 fmt → clippy -D warnings → test）：此前门只有 fmt+test，正是它漏掉了那 3 个 `MutexGuard held across await`。v1.0.11：implement 是唯一的墙——四轮实测都在 3000s 被掐（而 worktree 里其实已有实质进展，甚至已提交），于是 implement 提到 4200s，且提示词要求「先 `git status`/`git diff`/`git log` 侦察已有进展、就地修正、不要从零重写」；keeper 整跑上限 28800s。v1.0.10：**GATE 节点按 argv 执行、不经 shell** —— 所以 `cargo fmt --all --check && cargo test ...` 这种写法会得到 `error: unexpected argument '&&' found` 并瞬时 passed:false，流程一路走 partial、永远到不了 deliver/merge；改用 `bash flows/gates/repo-tests.sh`，并把 8 个回复类节点预算 180→600s（回复要过 LLM，180s 不够，之前的 run 就死在 reply_partial）。v1.0.9：bridge 补齐 PATH（launchd 的 keeper 没有 ~/.cargo/bin，gate 跑 cargo 直接 FileNotFoundError——#40 就这么死在最后一步）；review/fix_review 提示词明确「不要重复跑全量测试」（门会跑），预算 2700s；agent 预算 investigate 2100 / plan 1200 / implement 3000 / review 2400，keeper 整跑上限 21600s。**v1.0.8 的「共用主 clone CARGO_TARGET_DIR」已回滚**：实测会让集成测试目标链接到另一个 checkout 的库（假的 E0599，且可能拿旧库判绿），正确做法是每个 worktree 用自己的 target/。门预算 2400s——v1.0.6 由 1200s 上调，因为门现在真跑全量测试而 worktree 的 target/ 可能还冷；失败会走
  `fix_test`/如实回评，不会静默放行）。若开始超时，收窄成按 crate 的 `-p` 列表。

## 输入契约（keeper → run）

```json
{
  "repo_full": "jeffkit/recursive", "issue_number": 17,
  "title": "...", "author": "okguitar",
  "body_file": "<artifact_dir>/00-issue.md",       // keeper 预写并截断(8-16KB)
  "screener_verdict": "safe",                      // 入口闸，非 safe 直接拒
  "main_clone": "/Users/kong/projects/infra4agent/recursive",
  "worktree_dir": "<main_clone>/.worktrees/issue-17",
  "branch_name": "issue-17",
  "artifact_dir": "/Users/kong/.issue-keeper/pipeline/recursive-17",
  "test_command": "python3 <flows>/gates/gate_runner.py --spec <artifact>/gates.json --cwd .",
                                                   // per-repo：单命令原样；多门=gate_runner
  "gate_timeout_secs": 9000,                       // 多门=Σgate+300；单命令默认 2400
  "base_branch": "main",                           // v0.3：argusai 家族 develop 等
  "setup_command": "pnpm install --frozen-lockfile", // v0.3：worktree 建立后跑一次
  "setup_timeout_secs": 1800,
  "readonly": false,                               // v0.3：true=investigate 后早退
  "review_notes": "...", "triage_notes": "...", "doc_notes": "...",  // v0.3：仓规注入
  "investigate_timeout": 2100, "plan_timeout": 1200, "implement_timeout": 4200,
  "review_timeout": 2700, "fix_review_timeout": 2700, "fix_test_timeout": 900,
  "document_timeout": 300,                         // v0.3：per-repo 可覆盖
  "review_mode": "auto",                           // auto | human（human 且 high 才 HITL）
  "push_mode": "branch",                           // branch(默认) | pr | main | none
  "console": {                                     // 可选（混合形态）；缺省=纯本地定义
    "url": "http://127.0.0.1:8123", "api_key": "...",
    "flow_id": "issue-pipeline", "refresh_secs": 300,
    "cache_path": "~/.issue-keeper/pipeline/flow-cache.json"
  },
  "observability_redis": "redis://localhost:6379/0" // 可选；非空才上报 console 观测面
}
```

返回（end output）：`{status: done|rejected|blocked|invalid|nochange|abort|partial|
guarded|onhold|readonly, tests_passed, pushed, merged, comment_posted, kanban_ok}`。
keeper 按 status 决定重派/告警/转人工；`comment_posted=false` 必须告警
（readonly=只调查不开工的终态，v0.3 新增）。状态名 #17 未新增：triage 解析失败
不再产生 `blocked`（判不出 ≠ 判为否），该 run 走后续真实终态。

**comment_posted 归一化**：成功路径直接返回 `comment_posted`；业务早退路径历史返回
`posted`——`pipeline_bridge.py` 在出口统一补齐别名（缺 `comment_posted` 时用 `posted`
填充，引擎异常无 `posted` 则为 false）。keeper 侧兼容双读。早退终态已发回评 ≠ 故障，
告警文案区分「引擎异常无回评」与「终态但回评未发出」（issue #1 误报修复）。

## 混合形态：定义与观测归 console，执行留本地（2026-09-28）

console 侧 cancel 不杀进程树（本地档纯改状态、队列档只在节点边界生效且首节点
不可取消——真机验证见验证记录），故**执行不迁 console**；killpg 孤儿清理语义
留在 keeper。bridge 新增两条 console 集成（均 fail-open，缺配置=旧行为）：

- **定义源**：`payload.console` 有 url+api_key 时拉 console 已发布定义（semver
  最高），TTL 内用缓存；console 不可达退 stale 缓存；缓存也没有退仓内
  `issue-pipeline.flow.json`。台账记 `flow_source`/`flow_version`。
  改 flow 的发布环：`build_issue_pipeline.py` 重编译 → console 建/存/发布新
  semver（`POST /api/flows`、`PUT /api/flows/{id}/versions/{v}`、
  `POST /api/flows/{id}/publish`）；当前已发布 **v2.1.2**（2.1.2：`wt_prep` 幂等建树 +
  setup 缺 cwd 降级回评，#13；1.0.1：review/fix_review 600→1800s；1.0.2：code 节点显式
   `sandbox_backend="subprocess"`——编译器修好后节点级字段才真正进 IR。此行此前长期
  滞后于 console 实际版本，以 console 最高已发布 semver 为准）。
- **观测上报**：`payload.observability_redis` 非空时，每次 run 写
  `plaita:execution:{id}`（console 执行列表/详情可见，30 天过期）+ 逐节点
  publish `plaita:execution:events:{id}`（`/executions/{id}/stream` SSE 实时
  推送）；nodes 轨迹含每节点 status/duration_ms/input/output（长串截断 2KB）。
  Langfuse 另需进程 env `LANGFUSE_PUBLIC_KEY`（trace id = 执行 id）。
  console 侧展示需其进程配 `PLAITA_CONSOLE_NODE_MODULES=plaita_nodes`
  （2026-09-28 已配进 `~/.plaita-console/env.sh` 并重启生效，否则定义校验
  拒收 code 节点）。

## 已知缺口（按优先级）

1. **引擎层异常（agentrun 超时/非零退出、code 节点抛错）默认 abort 终态且当前不可续跑**，
   不经过业务出害口——「必有回评」的最终兜底在 keeper（见上），长期应推动 plaita 支持
   error 态续跑或 per-node errorHandler。
2. 段级 checkpoint = 节点级持久化；30min 的 implement 段内部无 checkpoint，重投整段重跑
   （幂等护栏已覆盖副作用）。
3. 修复回环仅一轮（fix_test→retest）；更长回环用 loop 节点 + 迭代上限（v0.3）。
4. **批次编排已落地地板（2026-09-28），判断层待做**：keeper 侧已有 ①依赖拓扑排序
   （`_dependency_first_order`——被依赖的 issue 先派，串行管线下依赖者开工时能看到
   已合并成果）②依赖唤醒（blocked 记 `wakeup_deps` 监视，依赖闭合——关闭或修复
   commit 进 origin/main——即清 processed 重跑，治「blocked 即永久沉默」）。**未做**：
   批次指挥 LLM（隐式依赖/并行分组/umbrella 上下文注入 triage INPUT），应做成
   plaita flow 走 console 发布流，与 supervisor 方向同构。
5. ~~**`sandbox_backend` 没进 IR**~~ **已修（2026-09-28）**：@flow 源码里的节点级字段
   曾被编译器静默丢弃——`plaita/dsl/codeflow/_nodes.py` 的 CODE 分支只搬
   `code`/`language`/`input`，`sandbox_backend` 一律丢，编译产物里是 `None`，运行期只能
   吃 `register_code_node(default_backend=...)`。deliver/merge 这类要跑 `git push` 的
   code 节点因此被默认 subprocess 后端的 **10s** 墙钟掐死（#41 死在 deliver、#45 的孤儿
   run 死在 merge，都是「push 其实已成功、包装层被杀」的假失败）。
   修复：plaita `fix/codeflow-code-node-fields`（CODE 分支透传 `sandbox_backend` + 回归
   测试）；bridge 侧另留 `PLAITA_SANDBOX_TIMEOUT=900` 兜底
   （`pipeline_bridge.ensure_sandbox_timeout`）。flow 源码同步改为显式
   `sandbox_backend="subprocess"`（声明实际在跑的档位，而不是依赖运营者默认），
   重建后发布 console **v1.0.2**。
6. **deliver 早退不 commit（2026-09-28 发现）**：`deliver` 见分支已在远端就直接返回
   `pushed=True`，跳过 `git add/commit/push`——重复投递时工作区里新产生的改动被静默
   丢弃（#45：第二份 run 的改动没进任何提交，只留在 worktree）。重跑语义要么先比
   `HEAD` 与远端，要么无条件 commit 后再判 push。
7. ~~**`wt_add` 非幂等（2026-09-28 发现）**~~ **已修（#13，2026-10-06）**：`git worktree
   add <dir> -b <branch> origin/main` 在 worktree/分支已存在时失败，而 capture 节点对
   非零退出不中止流程——重投会静默复用**旧的** worktree 基线（#45 的两份「实施记录」
   正是两个 run 挤在同一 worktree）。旧建议「判存在并显式 reset 到 `origin/main`」**已被
   否决**：管线分支上可能有 keeper 的 `wip(issue-N)` 快照提交，reset 会把它抹掉。
   现行语义 = 幂等 `wt_prep`（见「issue #13 处置」）：worktree 就绪则复用；否则
   `worktree prune` 后**复用残留分支重建**（保留 wip 提交）；目录缺失且分支不存在才
   按 `-b <branch> origin/<base>` 首发；仍失败 → `reply_prep_fail` 回评 partial。
   **禁止** `-B` / `reset --hard` / `branch -D` 任何形式的「清干净再来」。
8. **keeper 串行阻塞（2026-09-28）**：`_invoke_pipeline` 同步等 bridge，单 run 最长
   `pipeline_timeout_secs`（5400s），期间整个 17 仓轮询停摆（#45 实测卡 28 分钟）。
   与第 4 条的 per-repo 队列化一并做。

## 可观测与改进闭环（L1-L4，2026-09-30）

- **L1 数据地基**：bridge 的 `MetricsRecorder`（始终启用、fail-open）把每 run 的
  节点级事实（时长/agent 模型与 token 用量/门级 PASS-FAIL/契约快照）落盘
  `~/.issue-keeper/pipeline/metrics/<YYYY-MM>/<execution_id>.json`——本地永久，
  是看板/巡检/数据集的单一事实源（Redis trace 只当 30 天实时窗口）。台账新增
  `gate_failed/tokens_total/slowest_node`。
- **L2 看板**：dashboard 新增 `/api/pipeline/{summary,runs,runs/{id}}` 与前端
  「管线观测」页（按仓成功率/时长/段耗时堆叠条/失败门排名/token/console 深链）。
- **L3 经验闭环**：`supervisor_patrol` 升级——按仓观测摘要 + 确定性规则产出
  契约变更提案（`~/.issue-keeper/pipeline/proposals/`）：门超时→提预算
  （exit 124）、agent 段超时≥2 次→提段预算、门连续失败→manual 人工。
  审批：`python -m issue_keeper proposals list|show|apply <id>|reject <id>`
  （数值类自动应用：备份→改数字→load_config 校验→失败回滚）。
- **L4 benchmark**：`python -m issue_keeper benchmarks build/list/label/eval`——
  从观测+产物构建 triage 评测集（auto-label 来自 run 结果，正文过消毒，
  provenance 齐全），人工纠正即金标，回放评测打分入 manifest。
  flow 新版本发布前跑 eval 不达标不发布（与 screener 的评测集+人签发布同模式）。
