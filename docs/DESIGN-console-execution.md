# DESIGN: keeper 管线切换 plaita-console 执行——断点续跑（L3）的正式路线

> 状态：v2（按 2026-10-01 评审意见修订，见讨论区 D1-D7）｜作者：值守 agent｜日期：2026-10-01
> 关联：recursive `.dev/flows/self_improve_flow_v2.py`（L1 work 级续跑已落地 550bc6a）、
> plaita `plaita/core/strategies.py::DistributedStrategy`、`plaita/core/runner.py`、
> `plaita/server/flow_worker.py`、plaita-console backend（`/api/executions/{eid}/resume`）。

## 1. 背景与动机

keeper 的 issue 管线（engine=v2）当前经 `flows/v2_bridge.py` 以**本地子进程**直调
`FlowExecution`（LOCAL 模式）。一次 run = impl(15-60min) → 三门(20-40min) → 评审(5-15min)
→ 发布。失败形态的实测分布（2026-09-30～10-01，recursive okguitar 批次）：

| 失败点 | 占比观察 | 现状处置 |
|---|---|---|
| impl 长会话超时/端点断（AgentRunError） | 高（#63/#69/#74） | engine_error 自动重试×1，**重做全 run** |
| 门禁/评审内容性失败 | 中 | 线性修复环 / failed-preserved |
| 磁盘守卫 preflight | 环境性 | retry-later 不消费自动重派 |
| ff 合并竞态 | 并发同仓固有 | 分支保留人审 |

核心浪费：**任何阶段崩溃都要从 impl 从头重做**。三层对策中 L1（work 级：WIP 快照到
分支、重跑继承基线）已落地并保住代码资产；本设计补 **L3（执行级断点续跑）**——
让 run 在**节点粒度**恢复，死在评审就只重跑评审。

## 2. 为什么选"切 console 执行"而不是给 bridge 造状态机

plaita-console 已建好的面（实测代码，非规划）：

- **DistributedStrategy**：每节点执行后 checkpoint 持久化（`saved_context`，
  `PERSIST_EVERY_N_STEPS=1`），`flow_worker.py` 强制 `ExecutionMode.DISTRIBUTED`；
- **resume 端点**：`POST /api/executions/{eid}/resume`
  （resume_type=continue/event/cancel/timeout），挂起→恢复全链路有测试
  （`test_approval_suspend_then_resume`）；
- **观测/取消/审计**：execution 列表、事件流、租约（lease）防并发恢复。

自己造同等能力 = bridge 内重写状态机 + 持久化 + 恢复语义，且与 plaita 引擎的
演化长期分叉。**结论：复用 console，不重造。**

## 3. 现状 vs 目标架构

```
现状：
keeper daemon ──spawn──> v2_bridge.py ──LOCAL FlowExecution──> plaita 图
                            └─ state.json（仅观测，非恢复点）

目标：
keeper daemon ──console API──> console/flow_worker（DISTRIBUTED + checkpoint）
       │                              ├─ 每节点持久化 saved_context
       ├─ 轮询 execution 状态          ├─ EventNode 挂起→resume（现有）
       ├─ 台账落账（自 v2_bridge 移入） ├─ error 态 execution retry 续跑（本设计新增）
       └─ 失败恢复编排（§G5 决策表）    └─ worker 生命周期子进程清场（G3 扩展）
v2_bridge.py 退役为「goal 组装器」（仅构造 params，不再持有执行与落账）
```

## 4. 差距清单（实现前必须补齐）

### G1. error 态 execution 的 retry 续跑（引擎/worker 侧，主缺口）

**机制修正（v1 草案设想有误，按实测修正）**：节点执行抛错发生在
`runner.py::_execute_with_retry` 内，此时 `last_node_id` 与 `update_node_result`
**均未执行**——checkpoint 的 `last_node_id` 停在**上一个成功节点**，失败节点在
checkpoint 里**没有条目、无状态可重置**。

因此 retry 的正确语义是「**error 状态放行 + 按 continue 步进**」：

1. 主缺口在 `flow_worker.py:272` 的终态短路——`status=error` 被当终态直接
   `already_terminal` 返回。放行「error + resume_type=retry」，把状态翻回 running；
2. 策略层把 `retry` 路由到 `execute` 的 continue 步进分支（从 `last_node_id`
   的后继继续），失败节点自然被重跑——**不必扩 `_handle_resume`**（那是
   EventNode 专用路径）；`ResumeType` 枚举加一项照旧；
3. 已完成节点不重放（checkpoint 只含已完成节点，逐节点校验断言进单测）。

验收口径：error 状态的 execution 经 resume(retry) 后从断点步进、失败节点恰好
重跑一次、已完成节点不重放。

### G1b. 副作用节点的幂等性（retry = at-least-once 重放）

retry 重跑的是**死掉的节点**，而它可能死在副作用中途：`git_publish` 已 push 未
返回、`github_comment` 已发出未落账——重跑即重复副作用。策略：

- **出害口节点（git_publish / github_comment）排除出 retry 窗口**：失败即按
  全 run 重派（走既有 engine_error/failed-preserved 路径），不走 resume-retry；
- impl/gate/评审等纯工作节点重跑无害，是 retry 的适用面；
- 边界记录：`runner.py:230-238` 的 error-handler 路径（配了 retryTimes/
  errorHandler 的节点以 error result「正常完成」写入 checkpoint）——当前 v2 flow
  未配节点级重试，未来配置后需重审 retry 语义分叉。

### G2. 业务节点装载与 env 传递（拍板：env 走 worker 部署级）

- 节点装载：worker 经 `PLAITA_NODE_MODULES` 环境变量 import + `register_all()`
  装载业务节点（flow_worker.py:874-889，**非 packaging entry-point**——那是
  console 侧的路子）。worker 部署需 `pip install plaita-nodes` + 配置该变量；
- **env 传递拍板（D3）**：`StartFlowRequest` 仅 flow_id/version/params，无 env
  字段；且 params 进 `$INPUT` 会随每步 checkpoint 持久化——**走 params 传密钥
  直接违反 G4，此路不通**。定案：**GLM_/RECURSIVE_ 等前缀 env 落 worker 进程
  环境（部署清单管理），API 不动**；per-repo 差异交 plaita-nodes `config.py`
  的 `${VAR}` 插值 + agents/providers 配置层消化。

### G3. worker 生命周期全路径的子进程清场（含 worktree 并发写竞态）

> **⚠️ 2026-10-02 修订**：本节的「四路径清场」验收口径已被两组后续事实改写，
> 现行口径见 §5.6。cancel 路径经 plaita 取消语义设计稿
> （`plaita/docs/DESIGN-cancellation-and-lease.md`，波次①②④+引擎传播已进
> plaita main 7315c9c）拍板为**步界软中断**；SIGKILL/OOM 路径改走
> **kill-before-start**（§5.6，已实现）。

LOCAL 模式的 killpg/超时击杀在 worker 模式的归属问题，**不止 cancel 一条路径**：

1. **cancel**：只在节点边界生效（09-28 遗留，已知）；
2. **worker 被 kill -9 / OOM**：agent CLI 经 `start_new_session` 脱离进程组
   （agentproc runner.py:603-606），worker 死后无人回收——recursive 会继续跑完
   剩余几十分钟并持续写 worktree；
3. **SIGTERM 部署重启**：`flow_worker.py:958-961` 的处理器 `worker.stop(); sys.exit(0)`
   会打断在途消息且不清理子进程；
4. **由此引出真正的危害**：断点续跑后新 impl 节点的 recursive 与孤儿 recursive
   **并发写同一 worktree**——演练若只看续跑成功率会掩盖竞态甚至假成功。

**验收口径（P0，从「cancel 杀进程树」扩为全路径）**：cancel / SIGTERM / SIGKILL /
OOM 四路径的子进程清场；**P3 成功率口径同步改为「无孤儿竞态的干净续跑」**。
可行方向：worker stop() 优雅等待当前消息完成再退；节点启动前对 worktree 做
存活检测/清场（flock 或进程组探测）。先例：plaita 本仓 e13d296 已给 code 沙箱
补过同类「进程组击杀不留孤儿」，下沉到节点/runner 层有据可循。

### G4. checkpoint 里的敏感信息

worker 已对 `$ENV` 快照入 checkpoint 告警（flow_worker.py:379-384）。按 G2 的
拍板（env 走 worker 进程环境、API 不传），checkpoint 天然不含密钥——保留为
P0 审计项：迁移后抽检 checkpoint 内容确认无 key。

### G5. keeper 侧改造：派发/收尾/台账写入责任的迁移

- 派发：`_dispatch_pipeline` 从 spawn bridge 改为 `POST /api/executions`
  （flow 定义引用 + params）；
- **台账写入责任迁移（D5，v1 草案遗漏）**：ledger 行与 verdict→status 映射
  （committed/skip-commit/retry-later/failed-preserved/engine_error）当前都在
  `v2_bridge.py::_finish`。切 console 后没有 bridge 进程，reaper 的
  `_latest_pipeline_record` 返回空 → 全部误判 engine_error。**写入方移到
  keeper**：reaper 从 execution 终态 + 最终节点输出自行落账（映射函数抽成
  keeper 侧共享模块）；
- 收尾：`_reap_pipelines` 的「run.lock pid 存活判定」换成「execution 状态轮询 +
  worker 心跳年龄判 zombie」（D6：impl 15-60min 的场景等 8h `pipeline_timeout_secs`
  才升级太慢；plaita 已有 `scripts/reap_zombie_executions.py` 可参考）；
- WIP 快照逻辑（dc9d47b）保留在终态钩子（见 G5b 决策表），契约不变；
- **两层重试决策表（D5）**——keeper 既有「engine_error 自动重试×1（重派新 run）」
  与 L3「resume-retry（续原 execution）」的分工：

  | 错误类型 | 判定源 | 处置 |
  |---|---|---|
  | 引擎/节点崩溃（AgentRunError 等，非出害口） | execution status=error | **resume-retry ×1**（续原 execution） |
  | 出害口节点失败（git_publish/github_comment） | 失败节点 id ∈ 出害口集合 | **全 run 重派**（新 execution，既有语义） |
  | 环境性（磁盘守卫 preflight） | verdict=retry-later | **不消费自动重派**（现有 retry-later 语义） |
  | 内容性（门/评审否决） | failed-preserved + failure log | 既有线性修复环 / 值守处置 |
  | retry-retry 后仍 error | 连续 2 次 | 升级人工 + 兜底回评（既有 engine_error 升级语义） |
  | worker zombie（心跳过期） | 心跳年龄 > 阈值 | 取消 execution + resume-retry 或重派 |

### G6. 双轨过渡与回滚

`pipeline_repos["jeffkit/recursive"].engine` 增加 `v2-console` 档：
灰度单仓切换、保留 `v2`（本地 bridge）为即时回滚档。配置一行切回。

## 5. 分阶段实施

| 阶段 | 内容 | 验收 | 预估 |
|---|---|---|---|
| P0 复验+清场 | worker 环境跑通含 AGENTRUN 的最小 flow；**四路径子进程清场**（cancel/SIGTERM/SIGKILL/OOM，G3）+ checkpoint 无密钥审计（G4）+ env 部署形态落地（G2） | e2e：审批挂起→resume；四路径杀净无孤儿 | 1-2 天 |
| P1 引擎 | resume_type="retry"（G1，worker 终态短路放行 + continue 路由）+ 出害口排除（G1b）+ 单测 | error 态 resume 从断点步进、失败节点恰重跑一次、已完成不重放 | 0.5-1 天 |
| P2 keeper | engine=v2-console 档（G5/G6）：派发走 API、**台账写入迁移**、reaper 轮询+zombie 判定、重试决策表落地 | 单仓灰度：一单 issue 全链 committed | 2-3 天 |
| P3 演练 | kill -9 worker / 磁盘打满 / LLM 端点断三类注入 | **无孤儿竞态的干净续跑**成功率 ≥80% | 0.5 天 |
| P4 收尾 | 默认档切 v2-console；v2 bridge 降级为 goal 组装器；文档 | 批次无回归 | 0.5 天 |

## 5.5 P0 验收结果（2026-10-01 实测，判定：**不达标——先修 worker 再进 P1**）

环境事实（好消息）：worker 已配 `PLAITA_NODE_MODULES=plaita_nodes`（editable 安装可导入）、
plaita 引擎并行改造线已收束（0ac9347）、provider key 内联于 `~/.plaita/providers.json`
（worker 无需 GLM_API_KEY 环境变量，G2 的 env 形态比预想更简单）。

**e2e PASS**：重启 worker（旧进程 9/2 起未消费、队列积压 54 条自测噪音已清）后，
`p0-agentrun-min` 全链跑通——recursive CLI → GLM-5.3-flash → "PONG"，usage 完整落
`$NODE.agent`。附带实证：`session_id: ""`（L2 缺口实锤）。

**四路径清场 FAIL（3/4 路径实测，第 4 条同构推定）**：

| 路径 | 结果 | 证据 |
|---|---|---|
| A. cancel | **不可达** | running execution **未注册进 DB**（`/api/executions` total=43 无此行、`?status=running` 为 0），cancel 无从发起——与 09-28 旧结论「首节点期间 state 未落库」吻合，且是可见性层失败（先于进程清场） |
| B. SIGTERM | **孤儿** | worker 日志走完优雅退出（心跳停/注销/已停止）但进程滞留 SN 态，recursive+sleep 全部存活 |
| C. SIGKILL | **孤儿** | worker 立死，agent 进程树无人回收（`start_new_session` 脱离进程组），手工清场 |
| D. OOM | 同 C（同构推定） | — |

**P1 前置修复清单（worker/引擎侧，即 G3 的落地）**——状态截至 2026-10-02：
1. execution 启动即写 DB（修可见性，cancel 的前提）——**✅ 已修（plaita 43828aa）**：
   start_flow 先落 running 行再执行；execution_id 由 BFF start 提交时铸造、随
   队列消息透传（调用方即刻可轮询/取消），worker 认账并以种子喂引擎
   （`context.clean(execution_id=)`），行 id 与 result.execution_id 天然一致；
2. AGENTRUN 节点内进程组追踪 + runner 取消路径 killpg——**被拍板取代**：取消
   语义设计稿 §3.3 + 开放问题#1 拍板「维持软中断」，LLM/AGENTRUN 类在途节点
   有意不硬杀（防 at-least-once 重放副作用双份），取消只在步界生效；code 沙箱
   保留 killpg 例外。agent_run 的 killpg 仍仅限超时看门狗（plaita-nodes
   579084a）；
3. `worker.stop()` 优雅等待在途消息完成或限期后 killpg 清场（SIGTERM 路径）——
   **半修**：ReviewFix D3 已落（SIGTERM 只置位、任务边界自然退出、read 切 ≤1s
   分片），无限期 drain 接受为已知行为，残留孤儿交给第 4 条兜底；
4. （可选）节点启动前 worktree 存活检测/flock，防孤儿与新 run 并发写——
   **已按 §5.6 kill-before-start 实现并升级为必做**（flock 方案被否，理由见 §5.6）。

## 5.7 P1 引擎件 + P2 keeper 件落地（2026-10-02 深夜）

**P1（plaita 43828aa，全套 4341 绿）**：
- **G1**：`ResumeType.RETRY`；DistributedStrategy 把 retry 路由到 continue 步进
  （失败节点在 checkpoint 无条目 → 从 last_node_id 后继步进 = 恰重跑失败节点、
  已完成不重放——验收主用例钉住）；retry 无 saved_context 拒收；挂起中
  EventNode 拒 retry 绕行；worker 终态短路放行 error+retry，放行即翻 running
  落盘，再崩归位 error 仍可再 retry；
- **可见性**：见 §5.5 清单①。

**P2（issue-keeper 本提交，全套 308 绿）**：
- `issue_keeper/console_exec.py`：executions API 客户端（urllib）+
  `verdict_from_execution` / `map_verdict`（D5 台账写入迁移——与
  flows/v2_bridge.py::_finish 同表）/ `zombie`（D6：last_update_time 年龄，
  阈值须高于最长节点预算，默认 7200s）；
- `engine: v2-console` 档（G6）：`_dispatch_pipeline` 分叉到
  `_dispatch_console_execution`（POST /api/executions + v2-goal.md +
  dispatch.json 收尾上下文 + **console-exec.json 在途锚**——daemon 重启不丢）；
  回滚 = 配置一行 `v2-console → v2`；
- reaper console 分支：轮询 → 终态映射落账 → **共享收尾全复用**（读回校验 /
  engine_error 自动重派与连击升级 / 兜底回评 / WIP 快照 / 看板，零改动）；
  决策表：error → resume-retry ×1（G1 续原 execution）→ 仍 error → engine_error
  行走既有重派/升级；running 心跳超阈 → zombie cancel + engine_error 行；
  非 engine_error 的收尾回评由 keeper 出（console flow 无回评节点契约）；
- **事后修订（plaita#18）**：GET 404 不再无条件判 engine_error——记录年龄在
  `console_queue_grace_secs`（默认 1800s）内视为「已派发未消费」（console
  POST 只入队 Redis，记录由 worker 消费时落盘），不动作不重派；超期仍 404
  才走上述既有自愈路径。
- 在途判定：`_pipeline_in_flight` 认 console-exec.json（哨兵锁「console」仅
  占位，killpg 路径被 console 分支拦截）。

**剩余部署步骤（代码之外的运维活，P3 演练的前置）**：
1. 发布 self-improve v2 flow 到 console（INPUT 契约：goal/repo/run_id/agent/
   reviewer——与 `_dispatch_console_execution` 的 params 对齐）；
2. plaita worker 换血到本提交（新代码：可见性 + retry）；keeper daemon 重启
   （console 分支在 daemon 进程内）——均待维护窗口（中途重启丢在途 state）；
3. P3 演练（kill -9 worker / 磁盘打满 / LLM 端点断，口径=§5.6 表）；
4. G1b 边界：出害口节点排除依赖失败节点名上报（当前 error 载荷拿不到），
   以 git_publish/github_comment 的节点级幂等（dedup marker / ls-remote 查重）
   兜底，failure log 见出害口节点名时值守人工复核。

## 5.6 G3 落地：kill-before-start（2026-10-02 设计并实现）

**决策**：放弃「死时清场」（需要存活于 worker 之外的触发器：PDEATHSIG 仅
Linux、看门狗线程随 worker 同死），改追**接管前清场**——任何新 agent 对同一
workspace 开工之前，先探测并清掉旧进程组。孤儿本身可多活 1-2 分钟（写的还是
旧 attempt 的产物），时序上根除并发写。

**机制（三件，全在 agentproc/plaita-nodes，已实现）**：

1. **遗言锁**（agentproc `run_lock.py`）：spawn 侧 Popen 成功后把
   `pid/command/started_at` 写入 `~/.agentproc/run-locks/<sha256(workspace)>.json`
   （普通文件非 flock——worker 硬死时内核锁随 fd 释放，锁住已死持有者挡不住
   孤儿；文件+pid 探测才带得出「杀谁」。集中目录不进 worktree，keeper WIP
   快照 `git add -A` 不会收进提交）。`RunOptions.run_lock_key` opt-in，
   in-process 与通用 runner 两个 spawn 点都接线；正常收尾清锁，早退路径
   不清是**自愈安全**的（能早退说明子进程已死，残留锁下次探测按 stale 清理；
   wait 之前异常的存活 pid 恰是真孤儿，被杀正是期望行为）。
2. **preflight 清场**（`cleanup_stale_run`）：锁缺失=clean；pid 死=stale；
   pid 活着且**双因子身份核实**通过（pgid==pid 会话领袖 且命令含记录的
   argv0 基名，防 pid 重用误杀）→ killpg 整组、等死（僵尸也算死）后放行；
   活着但身份无法核实（ps 不可用/杀组被拒/杀后不死）→ **不杀不放行**抛
   `RunLockBusy`，由调用方决定（节点=AgentRunError 报错终态；reaper=告警
   继续）。plaita-nodes `agent_run.preflight_workspace` 是编排侧入口：
   AgentRunNode 直跑路径（`repo=`）开工前强制过门，`recursive_stream_turn`
   同款；沙箱路径不走此门（VM 边界 + WorkspaceLease 各管一摊）。
3. **keeper reaper 接线**：`_snapshot_worktree_wip` 快照前先
   `_preflight_orphans`（fail-open）——否则快照会拍进孤儿正在写的半成品。

**验收口径（改写后的四路径）**：

| 路径 | 现行口径 | 状态 |
|---|---|---|
| cancel | 步界软中断 + code 沙箱即时 killpg（取消语义设计稿 §3.3，拍板落档） | plaita main 已落（波次①②③④） |
| SIGTERM | 优雅 drain：任务边界自然退出、read ≤1s 分片（ReviewFix D3）；无限期 drain 接受，靠第 4 行兜底 | plaita main 已落 |
| SIGKILL/OOM | **接管前清场**：新 agent 开工时遗言锁里的旧组已不存在，无并发写窗口 | 本节机制已实现，待 P3 演练 |
| 并发写竞态 | 同上（kill-before-start 时序根除）；per-attempt worktree 隔离留作 config 兜底档，默认不采（丢「就地续做」L1 语义） | 同上 |

**残余风险（如实记档）**：preflight 只拦走 agent_run/recursive_stream_turn
门口的写入者，有人在 worktree 里手跑 recursive 不经此门——靠遗言锁可见性
（`~/.agentproc/run-locks/` 可扫）+ 运营纪律兜底，不设计防。

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| worker 生命周期孤儿进程 + worktree 并发写竞态（G3） | P0 四路径验收不达标则先修 worker（stop() 优雅退出 + 节点前清场）再继续 |
| checkpoint 格式随 plaita 演化漂移 | worker/引擎同仓同版本部署；checkpoint 兼容测试进 CI |
| 与 plaita 当前并行改造撞车 | P1 动引擎前与该线对齐窗口；feature flag 隔离 |
| 双执行路径行为分叉（评审口径/回评礼仪） | keeper 语义层（screener/reply/reaper 契约）不动；台账字段对齐 |
| 断点续跑放大脏基线（半成品+半成品叠加） | L1 基线继承只取**最新**分支；评审环内容性否决仍可整体推倒（agent 有推倒权） |
| retry 重复副作用（G1b） | 出害口节点排除出 retry 窗口，失败走全 run 重派 |
| 台账迁移期误判 engine_error（G5） | 迁移在 engine=v2-console 档内完成，v2 档不受影响；灰度仓先行 |

## 7. 回滚

任一阶段异常：`pipeline_repos["jeffkit/recursive"].engine: v2-console → v2`
（live-reload）即回本地 bridge 路径；在途 execution 由 reaper 按 engine_error
升级人工，工作资产经 WIP 分支无损（L1 机制与执行面解耦，天然保留）。

## 8. 非目标

- 不改 keeper 的 screener/评论层/依赖闸语义；
- 不在本次解决 AGENTRUN 的 session 级续跑（L2：`recursive resume <session_id>`，
  独立小工程，接口已确认存在）；
- 不迁移 v1 pipeline flow（issue-pipeline 已在 console 定义态，执行面待 v2 验证后跟进）。

## 9. 成功指标

- 崩溃 run 的断点续跑成功率 ≥80%（P3 注入口径，**含无孤儿竞态**）；
- 失败重做的平均浪费时长从 ~45min（impl 重做）降到 ≤10min（单节点重跑）；
- keeper 侧值守 reopen 次数归零（retry-later + 断点续跑 + 自动重试全覆盖）。

---

## 讨论区

> 评审：2026-10-01，基于本地 clone 实测代码复核（plaita @ feat/codeflow-loop-parent-scope、
> plaita-nodes、agentproc、issue-keeper、recursive @ main）。结论先行：**方向认可**——
> 复用 console 而非自造状态机的判断、双轨灰度 + 一行回滚、P0 先复验 cancel 的排序都对；
> §2 的能力描述逐条与代码相符（`PERSIST_EVERY_N_STEPS=1` 确为每节点持久化，
> `test_approval_suspend_then_resume` 存在，租约防并发恢复属实）。
> 以下问题按严重度排列，均附代码依据，供作者修订草案。

### D1（高）G1 的机制描述与 checkpoint 实际形状不符——真实改法比文档写的更小

「崩溃 run 的 checkpoint 里节点是 failed/running，把该节点状态重置为待执行」这一设想
与代码不符。实测 `core/runner.py::run_node`（runner.py:214-222）：节点执行抛错时发生在
`_execute_with_retry` 内，**`last_node_id` 与 `update_node_result` 都不会执行**——checkpoint
的 `last_node_id` 停在**上一个成功节点**，失败节点在 checkpoint 里没有条目，无状态可重置。
文档要解的主场景（AGENTRUN raise `AgentRunError`）正是这条路径。

因此 `retry` 的正确语义是「error 状态放行 + 按 continue 步进」：从 `last_node_id` 的后继
继续步进，失败节点自然被重跑。改动集中点也随之变化：

- 主要缺口其实是 `flow_worker.py:272` 的终态短路——`status=error` 被当成终态直接
  `already_terminal` 返回，任何 resume 都进不来。放行「error + resume_type=retry」并
  把状态翻回 running 即可；
- 策略层只需把 `retry` 路由到正常步进路径（`execute` 的 continue 分支），**不必扩
  `_handle_resume`**（那是 EventNode 专用路径）；`ResumeType` 枚举加一项照旧。

验收口径建议改写为：error 状态的 execution 经 resume(retry) 后从断点步进、失败节点恰好
重跑一次、已完成节点不重放。

### D2（高）retry = 失败节点 at-least-once 重放——副作用型节点的幂等性要在 P1 明确

checkpoint 只含已完成节点（每步持久化），所以已完成节点不会重放；但**死掉的节点可能
死在副作用中途**：`git_publish` 已 push 未返回、`github_comment` 已发出未落账——重跑即
重复副作用。impl/gate 重跑无害，出害口节点需要明确的幂等策略（或 retry 窗口排除出害口、
出害口失败仍走全 run 重派）。另注意 `run_node` 的 error-handler 路径（runner.py:230-238）：
配了 retryTimes/errorHandler 的节点会以 error result「正常完成」写入 checkpoint——当前
v2 flow 未配节点级重试，先记为边界条件，防止未来配置后 retry 语义分叉。

### D3（高）G2 的 env 传递机制当前 API 不支持，且按文档思路走会撞 G4

实测 `plaita-console/backend/api/executions.py:53-57`：`StartFlowRequest` 只有
`flow_id / version / params`，**没有 env 字段**。「env 经 execution 的 env 配置传递」
当前无承载。两条路：

- 走 params：params 进 `$INPUT`，随每步 checkpoint 持久化到 Redis——**直接违反 G4**，
  此路不通；
- 落 worker 进程环境（部署级）：API 不动，GLM_/RECURSIVE_ 前缀由 worker 部署清单管理，
  per-repo 差异交 plaita-nodes `config.py` 的 `${VAR}` 插值 + agents/providers 配置层消化。

文档写了「注意 G4 的密钥问题」但没给解法；这个决定影响 worker 部署形态，应在 P0 前
拍板并写进 G2。另：G2 引用的「flow_worker.py:874-889 按 entry-point 装载」实为
`PLAITA_NODE_MODULES` 环境变量 import + `register_all()` 机制，非 packaging entry-point
（后者是 console 侧的路子）——按 entry-point 部署会踩空，建议改措辞。

### D4（高）G3 之外还有更大的进程树洞：worker 生命周期事件全部孤儿化 agent 子进程

cancel 只在节点边界生效（文档已列，P0 复验正确），但同样不杀进程树的还有三条路径，
文档未覆盖：

1. **worker 被 kill -9 / OOM**：agent CLI 经 `start_new_session` 脱离进程组
   （agentproc runner.py:603-606），worker 死后无人回收——recursive 会继续跑完剩余
   几十分钟并持续写 worktree；
2. **SIGTERM 部署重启**：`flow_worker.py:958-961` 的处理器是 `worker.stop(); sys.exit(0)`，
   会打断在途消息且不清理子进程；
3. 由此引出**真正的危害**：keeper resume 断点后，新 impl 节点的 recursive 与孤儿
   recursive **并发写同一 worktree**——P3 的 kill -9 演练若只看「续跑成功率」会掩盖
   该竞态，甚至假成功（孤儿替新 run 干完了活）。

建议：P0 验收从「cancel/超时杀进程树」扩为「worker 生命周期全路径（cancel / SIGTERM /
SIGKILL / OOM）的子进程清场」；P3 的成功率口径改为「无孤儿竞态的干净续跑」。可行方向：
worker stop() 优雅等待当前消息完成再退、节点启动前对 worktree 做存活检测/清场（flock 或
进程组探测）。plaita 本仓刚给 code 沙箱补过同类修复（e13d296「进程组击杀不再留孤儿」），
说明这是已知主题，下沉到节点/runner 层有先例可循。

### D5（中）G5 漏了台账写入责任的迁移，且两层重试的决策表缺失

- 现在 ledger 行与 verdict→status 映射（committed/skip-commit/retry-later/
  failed-preserved/engine_error）都在 `v2_bridge.py::_finish`（v2_bridge.py:47-65）。切
  console 后没有 bridge 进程，reaper 的 `_latest_pipeline_record` 返回空 → 全部误判
  engine_error（keeper.py:1539-1544）。「台账契约不变」没错，但**写入方**必须移到 keeper
  （reaper 从 execution 终态 + 最终节点输出自行落账）。G5 应补这条，P2 的 1-2 天估计
  可能因此偏紧。
- keeper 现有 engine_error 自动重试 ×1（keeper.py:1556-1574，重派**新 run**）与 L3
  resume（续**原 execution**）是两层重试，谁先谁后、什么错误走哪层未定义——不定义清楚
  会出现「resume 失败后又全 run 重派」的双重浪费。建议 P2 给出决策表：
  错误类型（引擎崩溃/环境性/内容性）× 处置（resume-retry / 重派 / 升级人工）。

### D6（中低）running 状态缺死因判定，reaper 轮询需要 zombie 语义

reaper 从「run.lock pid 存活」换成「execution 状态轮询」后，「worker 死了、execution
永远 running」需要新的判定源（worker 心跳/registry TTL；plaita 已有
`scripts/reap_zombie_executions.py` 可参考）。`pipeline_timeout_secs`（8h）兜底存在，
但 impl 15-60min 的场景里等 8h 才升级太慢。建议 P2 明确：reaper 结合 worker 心跳年龄
判 zombie，提前触发 resume/升级。

### D7（低）小勘误

- §2 的 resume_type 实际还有 `continue`（executions.py:62），不止 event/cancel/timeout；
- 其余代码引用（strategies.py:341-349 的 EventNode+pending 校验、flow_worker.py:379-384
  的 $ENV 告警、recursive 550bc6a、gates/agentproc 的 killpg 层）均与实测相符，文档
  的代码功课是扎实的。

### 评审结论

补齐 D1-D5 后方案成立，P0-P4 的阶段划分与回滚设计无需大改。建议的修订动作：
G1 按 D1/D2 重写机制段（改动反而变小）；G2 按 D3 拍板 env 路径；G3 按 D4 扩验收项；
G5 按 D5 补台账迁移与重试决策表；P1 预估可下调、P0 预估上调（1-2 天）。

### 作者回应与修订记录（2026-10-01）

三项承重论断抽验属实（runner.py 错误路径不写 checkpoint、flow_worker.py:272
终态短路、StartFlowRequest 无 env 字段），**评审全盘采纳**，已修订：

- **G1 重写**（D1）：机制改为「error 态放行 + continue 步进」，主缺口定位到
  flow_worker.py:272 终态短路，不动 `_handle_resume`；验收口径同步改写；
- **新增 G1b**（D2）：出害口节点（git_publish/github_comment）排除出 retry 窗口、
  失败走全 run 重派；error-handler 边界记录在案；
- **G2 拍板**（D3）：env 走 worker 部署级进程环境（API 不动、params 传密钥此路
  不通），`PLAITA_NODE_MODULES` 措辞修正；
- **G3 扩展**（D4）：P0 验收扩为四路径子进程清场；P3 口径改「无孤儿竞态的干净
  续跑」；worktree 并发写竞态纳入；
- **G5 补齐**（D5/D6）：台账写入责任迁移（映射函数抽 keeper 侧共享模块）、两层
  重试决策表（六行）、reaper zombie 判定（worker 心跳年龄）；
- **P0 上调 1-2 天、P1 下调 0.5-1 天**（D7 及评审结论采纳）；
- D7 勘误（resume_type 含 continue）已同步 §2。

修订后方案待排期确认即可进入 P0。
