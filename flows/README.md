# issue-pipeline —— issue-keeper 的 plaita 化管线（@flow 源码 + 生成 JSON）

> v0.2.1，2026-09-27。经三方独立审查（plaita DSL 严谨性 / 编排设计缺陷 / 运维安全）
> 后定稿。**审查主体是 `issue_pipeline_flow.py`（@flow 源码），JSON 是编译产物不要手改**：
>
> ```bash
> python3 flows/build_issue_pipeline.py     # 需 PYTHONPATH 含 plaita 与 plaita-nodes/src
> ```
>
> 已验证：codeflow 编译 ✓、plaita `validate_flow_ir` ✓、全节点有出边 ✓。未做端到端真跑。

## 管线形状（v0.2.1，编译后 61 节点）

```
screener闸(INPUT.screener_verdict != safe → 拒评短路)
└─ deps预检(正文 #N 引用 → gh 查 issue/PR 状态 → deps_json)
   └─ triage(agent, 查重+定级, 正文只经 body_file；依赖门硬判据)
      ├─ blocked(依赖未就绪/已有在途) → 回评 → END
      ├─ invalid(已在 main 修复/无需改动) → 回评 → END        ← #17 重派问题根治
      └─ actionable
         └─ git fetch --prune（clone 新鲜度）
            └─ worktree add（并发隔离，基线显式 origin/main，防夹带本地未推送提交）
               └─ investigate(agent 10min, bug 先立失败测试) → 01-investigation.md
                  └─ plan(agent 10min) → 02-plan.md（含 COMMIT_MESSAGE）
                     └─ [review_mode=human 且 risk=high → HITL 1h；未批准 → 「暂缓」回评 END]
                        └─ implement(agent 30min, 禁 .github/**, 不 commit/push)
                           └─ 无改动 → 回评 → END
                              └─ review(agent=deepseek-flash 独立审查, 解析失败=abort)
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
独立 review（deepseek-flash 异构模型 + fail-safe abort）、质量门命令参数化、
HITL 条件化+未批准即停、partial/hold 如实「未推送」、deliver/回评幂等（标记去重+
ls-remote 查重）、diff 护栏（.github/** 与超大 diff）、评论出害前消毒
（本机路径/密钥模式 → [REDACTED]）、git fetch 同步（origin/main 基线）、
triage 区分 invalid/blocked-in-flight、
解析失败 fail-safe（triage→blocked 人工复核；review→abort）。

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

## v1.0.5（2026-09-28）：document 提示词 + 质量门转真

- **`document` 段会张冠李戴**：#43 落地时发现它写的 CHANGELOG 条目描述的是 **#45** 的改动
  （编号与内容都不对）。根因是提示词只说「按本仓惯例补一行」，没告诉它「这次改了什么」，
  它就自己猜。现在注入本 issue 号/标题 + `$NODE.parsed.commit_message`，并要求先 `git diff`
  + `git status -uall` 再动笔、条目必须如实描述本次 diff、拿不准就 SKIP。
- **recursive 的质量门由空转真**：`pipeline_test_commands["jeffkit/recursive"]` 原本是空串，
  gate 节点跑 `true` 恒过——回评里的「质量门已通过」其实只来自 agent 自述。现在设为
  `cargo fmt --all --check && cargo test --workspace --no-fail-fast`（v1.0.8：bridge 把 CARGO_TARGET_DIR 指向主 clone 的 target/，worktree 不再冷编译整个 workspace——#19/#30/#40 三跑都因此被节点预算掐死；agent 预算 investigate 2100 / plan 1200 / implement 3000 / review 2400，keeper 整跑上限 21600s。门预算 2400s——v1.0.6 由 1200s 上调，因为门现在真跑全量测试而 worktree 的 target/ 可能还冷；失败会走
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
  "test_command": "cargo test --workspace",        // per-repo；空=跳过质量门并注明
  "review_mode": "auto",                           // auto | human（human 且 high 才 HITL）
  "push_mode": "branch",                           // branch(默认) | main
  "console": {                                     // 可选（混合形态）；缺省=纯本地定义
    "url": "http://127.0.0.1:8123", "api_key": "...",
    "flow_id": "issue-pipeline", "refresh_secs": 300,
    "cache_path": "~/.issue-keeper/pipeline/flow-cache.json"
  },
  "observability_redis": "redis://localhost:6379/0" // 可选；非空才上报 console 观测面
}
```

返回（end output）：`{status: done|rejected|blocked|invalid|nochange|abort|partial|
guarded|onhold, tests_passed, pushed, merged, comment_posted, kanban_ok}`。
keeper 按 status 决定重派/告警/转人工；`comment_posted=false` 必须告警。

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
  `POST /api/flows/{id}/publish`）；当前已发布 **v1.0.3**（1.0.1：review/fix_review 600→1800s；1.0.2：code 节点显式
   `sandbox_backend="subprocess"`——编译器修好后节点级字段才真正进 IR）。
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
7. **`wt_add` 非幂等（2026-09-28 发现）**：`git worktree add <dir> -b <branch> origin/main`
   在 worktree/分支已存在时失败，而 capture 节点对非零退出不中止流程——于是重投会
   静默复用**旧的** worktree 基线（#45 的两份「实施记录」正是两个 run 挤在同一
   worktree）。应在 `wt_add` 前判存在并显式 reset 到 `origin/main`。
8. **keeper 串行阻塞（2026-09-28）**：`_invoke_pipeline` 同步等 bridge，单 run 最长
   `pipeline_timeout_secs`（5400s），期间整个 17 仓轮询停摆（#45 实测卡 28 分钟）。
   与第 4 条的 per-repo 队列化一并做。
