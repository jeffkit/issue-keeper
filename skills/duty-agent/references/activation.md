# 如何唤出值守 Agent / 设定时任务 / 被事件激活

## 一、在新会话里唤出"我"

技能装在 `~/.dsh/skills/duty-agent`（软链到 `issue-keeper/skills/duty-agent`）。新会话里任一句即可触发：

- 「**值守，起一轮**」/「值守体系现在怎么样？」/「看看工单收件箱」
- 「读 duty-agent skill，按流程处置工单」
- 「唤出值守 Agent」/「检查在途、磁盘、看门狗」
- 或直接描述意图：「去看看流水线为什么卡了」「处置一下 needs-human」

触发后，新的"我"会按 `SKILL.md` §1 的标准动作起轮：读工单 → 处置 → 巡检四问 → 留痕。
**认知是完整的**：体系全图（architecture）、能力矩阵边界、处置阶梯、踩坑清单（playbooks）、命令端点（toolbox）都在 references 里——不依赖任何会话记忆。

> 若新会话看不到该技能：检查软链 `ls -la ~/.dsh/skills/duty-agent`，必要时重建：
> `ln -snf ~/projects/infra4agent/issue-keeper/skills/duty-agent ~/.dsh/skills/duty-agent`

## 二、设定时任务（两种，用途不同）

### A. **确定性 7×24**（推荐用于探测/机械动作）→ 用 console flow 调度
值守 flow 已经是这个形态（8 个，见 architecture）。要新增一个定时探测器：

```bash
# 1) 写 @flow 源码（flows/<name>_flow.py）→ 2) 编译 → 3) 建 flow → 4) 发布 → 5) 建调度
PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
  python3 ~/projects/infra4agent/issue-keeper/flows/build_flows.py <short>   # 记得先在 build_flows.py 的 FLOWS 表登记
# 然后 PUT /api/flows/<id>/versions/<v> + POST /publish + POST /api/schedules（见 toolbox）
```
**纪律**：flow 里**不要放判断逻辑**——探测 + 机械动作 + **递工单**（`duty_request.py create`）。

### B. **唤醒"我"这个会话**（有上下文的处置）→ 用 DSH 会话定时
在当前会话（或新会话）里让 agent 调 `schedule_create`，例如：

```
每 30 分钟提醒我：「值守轮次：读 duty/requests 收件箱与 rounds.log，处置待办，必要时 HITL。」
   → schedule_create(every_seconds=1800, title="值守轮次", prompt="值守轮次：...")
或工作日 09:30 起轮：schedule_create(weekly={weekdays:[1,2,3,4,5], time:"09:30:00", time_zone:"Asia/Shanghai"}, ...)
```
注意：会话定时**只在会话/应用存活时投递**；深度宕机后只补最近一次。所以"必须可靠发现"的事
（体系停摆、磁盘涨满、孤儿沙箱）要放 **A（flow/launchd）**，"需要我判断"的事放 B。

> ⚠️ **换会话要重新 arm**（2026-10-09 实测）：DSH schedule 是**会话绑定**的——存储里
> `tables.tasks[*].sessionId` 钉死创建它的那个会话，新会话不会继承。所以在**新会话**里起
> 值守时，让 agent 用 `schedule_create` 重建 30 分钟轮，否则该会话没有常规巡检节奏。
> 事件类感知**不受影响**：`duty-watch` 插件（critical 工单 / 工单回复 / HITL 回复）会
> **优先投给持有值守定时任务的会话**，没有则退回最近活跃会话；launchd `duty-escalation`
> 与会话完全无关。

## 三、被事件激活（现在是怎么接的）

| 事件 | 谁发现 | 怎么到达我 |
|---|---|---|
| run 卡死 / 终态未收尸 / 超长跑 | `inflight-watch` `*/15` | 写工单 `duty/requests/`（critical 且越线时还会自动 resume 一次） |
| AGS 孤儿 / 实例级卡死 | `sandbox-watch` `*/30`（+ launchd `ags-orphan-sweep`） | 写工单 / 直接杀孤儿 |
| 磁盘逼近守卫线 / 守卫退避挡住流水线 | `disk-hygiene` `*/30` | 自动清理 + 清退避 + 写工单 |
| 体系自身停摆（心跳/调度） | `ctrl-watch` `*/30` + launchd `external-watchdog` `*/5` | `ctrl-watch` 写工单（不再直发 GitHub） |
| 管线指标/契约提案 | `pipeline-patrol` `0 */4` | 写轮报 + 工单 |
| 人回复了 HITL | `hitl-inbox` `*/5` | **回填工单**（`status=answered`）→ 我下轮据此执行 |
| 需要人拍板 | **我**（判不了时） | `hitl_notify.py` 发 HITL + 监听回复 |

**闭环**：flow 递工单 → 我处置（自决/转人）→ 人回复回填工单 → 我执行 → 工单 `resolved`。

### 想加新的"事件源"？
1. 写一个 flow 或 launchd 脚本做探测；
2. 触发时调 `duty_request.py create --from-flow <名> --kind <类> --severity <级> --title ... --context-json ...`；
3. 我在下一轮（被会话定时唤醒，或人唤出）就会看到并处置。
**不要**让探测脚本自己动手做判断类动作——那是我的活。

## 四、工单优先级与超时（约定）

- `severity`: `critical`（在跑的东西坏了/在烧钱）> `warn`（趋势不对）> `info`。
- 我的处置时限：critical **当轮必须给出结论**（自决或转人），warn 可同轮合并处理。
- 转人的工单：`status=escalated` 且 `human.session_id` 有值；人回复后 `answered`；我执行后 `resolved`。
- 长期无人回复的 escalated 工单：不要重复推送骚扰（稳定 dedupe 键）；在看板/轮报里挂着即可。
