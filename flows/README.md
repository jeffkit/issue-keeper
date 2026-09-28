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
4. runner 启动：`register_code_node(default_backend="subprocess")`；所有 code 节点已显式
   `sandbox_backend="unsafe"`（本机可信部署；多租户环境必须另行收窄）。

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
  `POST /api/flows/{id}/publish`）；当前已发布 **v1.0.0**。
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
