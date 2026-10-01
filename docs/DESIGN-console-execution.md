# DESIGN: keeper 管线切换 plaita-console 执行——断点续跑（L3）的正式路线

> 状态：草案（待拍板排期）｜作者：值守 agent｜日期：2026-10-01
> 关联：recursive `.dev/flows/self_improve_flow_v2.py`（L1 work 级续跑已落地 550bc6a）、
> plaita `plaita/core/strategies.py::DistributedStrategy`、`plaita/server/flow_worker.py`、
> plaita-console backend（`/api/executions/{eid}/resume`）。

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

- **DistributedStrategy**：每节点执行后 checkpoint 持久化（`saved_context`），
  `flow_worker.py` 强制 `ExecutionMode.DISTRIBUTED`；
- **resume 端点**：`POST /api/executions/{eid}/resume`（resume_type=event/cancel/timeout），
  挂起→恢复全链路有测试（`test_approval_suspend_then_resume`）；
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
       └─ 失败恢复（本设计新增语义）    └─ 失败节点重跑（本设计新增）
v2_bridge.py 退役为「goal 组装器」（仅构造 params，不再持有执行）
```

## 4. 差距清单（实现前必须补齐）

### G1. 失败节点的重跑语义（引擎侧，最大缺口）

`DistributedStrategy._handle_resume` 仅接受「EventNode 挂起且 status=pending」；
崩溃 run 的 checkpoint 里节点是 failed/running，现语义直接 `ResumeError`。

**方案**：新增 `resume_type="retry"`——从 checkpoint 的 `last_node_id` 起，
把该节点状态重置为待执行后按分布式步进继续。改动集中在
`strategies.py::_handle_resume` + `ResumeType` 枚举 + worker 的恢复入口分派。
（engine_error 自动重试的既有 keeper 侧语义保留为外层兜底。）

### G2. 业务节点在 worker 环境可用

AGENTRUN/GATE/GIT_PUBLISH 来自 plaita-nodes；worker 已有按 entry-point
`register_all()` 的装载机制（flow_worker.py:874-889）。需要：
- worker 部署环境 `pip install plaita-nodes`（或 requirements 声明）；
- recursive run 的 env 注入（GLM_/RECURSIVE_ 等前缀）经 execution 的
  env 配置传递——**注意 G4 的密钥问题**。

### G3. AGENTRUN 的子进程树管理归属

LOCAL 模式下 bridge 对 agent 子进程有 killpg/超时击杀；worker 模式下这一层
移交给谁：节点内 `agentproc_run(timeout_secs=…)` 已有进程内超时，但 worker
级强制回收（execution cancel/worker 重启）需要确认 killpg 语义在 worker
环境成立（**09-28 评估的遗留**：console cancel 曾「只在节点边界生效且不杀
进程树」——迁移前必须复验并修复，列为 P0 验收项）。

### G4. checkpoint 里的敏感信息

worker 已对 `$ENV` 快照入 checkpoint 告警（flow_worker.py:379-384）。AGENTRUN
的 provider key 走环境注入——迁移时必须确保 key 走 worker 进程环境而非
execution 参数，checkpoint 不落密钥（审计项）。

### G5. keeper 侧的派发/收尾改造

- 派发：`_dispatch_pipeline` 从 spawn bridge 改为 `POST /api/executions`
  （携带 flow 定义引用 + params）；
- 收尾：`_reap_pipelines` 的「run.lock pid 存活判定」换成「execution 状态
  轮询」；WIP 快照逻辑（dc9d47b）从 bridge 移到「execution 终态 webhook/
  轮询回调」——或保留 bridge 作为终态钩子；
- run.lock/台账/兜底回评的契约不变（keeper 语义层不动）。

### G6. 双轨过渡与回滚

`pipeline_repos["jeffkit/recursive"].engine` 增加 `v2-console` 档：
灰度单仓切换、保留 `v2`（本地 bridge）为即时回滚档。配置一行切回。

## 5. 分阶段实施

| 阶段 | 内容 | 验收 | 预估 |
|---|---|---|---|
| P0 复验 | console worker 环境跑通一个含 AGENTRUN 的最小 flow；cancel/超时杀进程树复验（G3） | e2e：审批挂起→resume；impl 超时→进程树清 | 0.5-1 天 |
| P1 引擎 | resume_type="retry"（G1）+ 单测：failed 节点重跑 / 挂起恢复并存 | 单测绿 + 手工崩溃注入恢复成功 | 1-2 天 |
| P2 keeper | engine=v2-console 档（G5/G6）：派发走 API、reaper 轮询、终态 WIP 快照保留 | 单仓灰度：一单 issue 全链 committed | 1-2 天 |
| P3 演练 | kill -9 worker / 磁盘打满 / LLM 端点断三类注入；断点恢复成功率 | ≥80% 崩溃 run 从断点续跑成功 | 0.5 天 |
| P4 收尾 | 默认档切 v2-console；v2 bridge 降级为 goal 组装器；文档 | 批次无回归 | 0.5 天 |

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| console cancel 不杀进程树（09-28 遗留）复发 | P0 首项复验；不达标则先修 worker 再继续（killpg 下沉到节点） |
| checkpoint 格式随 plaita 演化漂移 | worker/引擎同仓同版本部署；checkpoint 兼容测试进 CI |
| 与 plaita 当前并行改造撞车 | P1 动 `strategies.py` 前与该线对齐窗口；feature flag 隔离 |
| 双执行路径行为分叉（评审口径/回评礼仪） | keeper 语义层（screener/reply/reaper 契约）不动；台账字段对齐 |
| 断点续跑放大脏基线（半成品+半成品叠加） | L1 的基线继承只取**最新**分支；评审环内容性否决仍可整体推倒（agent 有推倒权） |

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

- 崩溃 run 的断点续跑成功率 ≥80%（P3 注入口径）；
- 失败重做的平均浪费时长从 ~45min（impl 重做）降到 ≤10min（单节点重跑）；
- keeper 侧值守 reopen 次数归零（retry-later + 断点续跑 + 自动重试全覆盖）。
