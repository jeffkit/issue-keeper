# 体系全图（2026-10-09 现状）

## 主机与角色

| 主机 | 作用 | 关键进程 / 端口 |
|---|---|---|
| **Mac**（本机，`kong`） | 交互侧 + flow 执行 + 看板 + HITL server | `worker-mac-1`（v2 队列，并发 3）、`worker-ctrl-1`（ctrl 队列，并发 1）、dashboard `:7433`、hitl-server `:8081`、tunnely（公网 `/ik/`） |
| **VM `tcloud_gz`**（腾讯云广州，ssh 别名） | 单实例权威侧 | keeper daemon（`issue-keeper-worker.service`）、`plaita-schedule-service`、`worker-tcg1`（v2 队列，并发 1）、Redis db1（`:16379`）、plaita-console API（`:8323`） |

- Mac 上 redis/console 端口通过 ssh 隧道转发（`cc.agentstudio.console-forward-tunnel` 等）。
- **两处 checkout 都会被人/管线同时改**：Mac 上是 `~/projects/infra4agent/*`，VM 上是 `~/projects/infra4agent/*`。**在任一处 `git pull --rebase` 前先 `git status`**（VM 上遇过 rebase 冲突把生产树搞成带冲突标记状态）。

## 值守 flow（console 调度，8 个）

| flow | 节奏 | 干什么 |
|---|---|---|
| `issue-accept`（A 值守） | `*/10` | 多仓验收闭环：初验 done 单 → 回评请验收 → 外部 `/accept` 合并关单（tunely + plaita） |
| `keeper-watch`（B 值守） | `40 */2` | B 班轮报：值守落地 + 简报 + handoff |
| `ctrl-watch`（主控） | `*/30` | **meta 看门狗**：A/B 心跳、shadow 派单对账、调度健康 → 递工单（不再直发 #2） |
| `sandbox-watch`（沙箱） | `*/30` | AGS 实例：孤儿清查（杀）+ 静默卡死判定 + 配额压力 |
| `inflight-watch`（在途） | `*/15` | run 级进度：末节点停滞 / 终态未收尸 / 超长跑 → 递工单；真卡死自动 resume 一次 |
| `pipeline-patrol`（管线） | `0 */4` | L3 巡检：台账/metrics/keeper 日志 → 观测报告 + 契约变更提案（**跑在 VM 上**，数据在那） |
| `disk-hygiene`（磁盘） | `*/30` | 磁盘观测 + 清 >24h run worktree + 清磁盘守卫退避（6h 退避曾让整线瘫） |
| `hitl-inbox`（HITL 收件箱） | `*/5` | 轮询 HITL 会话取人回复 → 留痕 → 落 issue → **回填工单**（闭环） |

执行侧 flow：`keeper-shadow`（派发决策，keeper 直调，非 cron）、`self-improve-v2`（主管线）、`self-improve-v2-sbx`（沙箱灰度）。
独立兜底（**故意不在 flow 里**，独立于调度器进程）：launchd `cc.agentstudio.external-watchdog`（`*/5` 重拉停摆服务）、`ags-orphan-sweep`（成本安全网）。

## duty 内核（文件即真相）`~/.issue-keeper/duty/`

```
rounds.log                 各 flow 每轮一行（体系脉搏，第一手）
state-<role>.json          轮次滚动窗口 20（role: A/B/controller/sandbox/issue-accept/inflight/disk/patrol/hitl）
requests/req-*.json        ★ 决策工单（flow → 值守 Agent 的唯一正式通道）
handoffs/                  B 班 handoff
roster.json                值班表
hitl-notifications.jsonl  我发出的 HITL 通知留痕（session_id / status / 回复）
inflight-resume.json       在途巡检的"每个执行只自动 resume 一次"账本
```
规范与 schema：`issue-keeper/docs/duty/{DUTY-PROTOCOL,CAPABILITY-MATRIX}.md`、`docs/duty/schema/`。
**三层协同与值守 Agent 定位**见 DUTY-PROTOCOL.md 末节。

## keeper（业务真值）

- `~/.issue-keeper/state.json`：**per-issue 权威状态**（`processed` / `blocked` / `retry_after` / `in_flight_since` / `manual_reopen_at` / `comment_tasks`…）。
  每轮重载、保存走**三方合并**（外部原子改写安全）。
- `~/.issue-keeper/config.yaml`：绑定 17 个子仓 + per-repo 管线契约（`setup_command`/`gates`/`push_mode`）+ 全局旋钮
  （`pipeline_max_in_flight`（现 6）、`pipeline_priority_repos`、`pipeline_priority_issues`、`console_zombie_secs`（默认 7200）、`pipeline_needs_human_label`）。
  **每轮 live-reload**（改完即生效，无需重启 keeper）；改前备份 `.bak-<日期>`。
- keeper CLI（**必须在 `~/projects/infra4agent/issue-keeper` 目录下、用 `~/.venvs/issuekeeper/bin/python`**）：
  `-m issue_keeper reopen -c ~/.issue-keeper/config.yaml jeffkit/<repo> <num...>`（清 processed/blocked **并自动摘 needs-human**；**不清 `retry_after`**）
- 僵尸线语义（别记错）：`issue_keeper/console_exec.py:zombie()` = `status=running` 且 `last_update_time` 年龄 > `console_zombie_secs`（默认 2h）。

## plaita console / 队列

- API：`http://127.0.0.1:8323/api/{flows,executions,schedules}`，头 `X-Admin-API-Key: b4b5042ee7d1b937633c08f3f50d4c8efbca88d33ece8a03`。
- 发布：`PUT /flows/<id>/versions/<v>`（体 `{definition: "<JSON 字符串>", layout:"{}", created_by:"jeffkit"}`）→ `POST /flows/<id>/publish {"version":"<v>"}`。
- 队列（Redis db1，密码 `6ace3bde72955c70b9f264e24f57b343`，Mac 隧道 `127.0.0.1:16379`）：
  `plaita:flow:queue:v2`（执行：Mac 3 + VM 1）、`plaita:flow:queue:ctrl`（值守：Mac 1）。
- 执行详情：`GET /api/executions/<eid>`（**时间戳是本地 naive，别当 UTC**）；`resume`/`cancel` 见 toolbox。

## HITL（人机通道）

```
我 --hitl_notify.py--> hitl-server(:8081) --长轮询--> 微信/企微（jeffkit）
                                                        │ 人回复
   工单回填 <--duty_request.py answer-- hitl_inbox.py <-- 会话（poll）
```
- **发送必须 `wait_reply=true`**（否则不建会话 → 人的回复被服务端丢弃、你永远收不到；2026-10-09 实测踩过）。
- 会话 TTL 24h（`HITL_SESSION_TTL_SECS`）；留痕 `duty/hitl-notifications.jsonl`。

## 看板（人看的窗口）

- 本地 `http://127.0.0.1:7433/?view=duty`（launchd `cc.agentstudio.issue-keeper-dashboard`；改前端后 `npm run build`）。
- 面板：三班心跳卡、工作流拓扑、**工单收件箱（值守 Agent 的决策队列）**、等人工队列、沙箱用量、吞吐/投仓统计。
- 公网 `dsht.agentstudio.cc/ik/`（tunely，带站点登录）。
