# 能力矩阵 —— 红线逐条映射（待 jeffkit 过目）

> 这是 `DUTY-PROTOCOL.md` §5 的展开。把现役 `playbook.md` §4 与各 flow 提示词里的
> **中文红线句子**，逐条映射到「档位 + 实现位置」。
>
> **这张表的用途**：它同时是 ①flow 里 GATE 节点的输入、②职责边界的人类讨论稿。
> `roster.schema.json` 的 `capabilities` 段是它的机器可读形态——**两边必须一致**，
> 由 `validate.py` 的契约测试守着。
>
> 建议重点看 §3（我判错的档位）和 §4（现役无人能执行的红线）。

## 1. 三档语义回顾

| 档 | 语义 | 实现位置 | 违反时 |
|---|---|---|---|
| `autonomous` | 想干就干，事后可审计 | AGENTRUN 内工具 | 只记录 |
| `authorized` | 角色允许才行 | flow 图 **GATE 节点**（AGENTRUN 后、落库前） | 节点失败 + 告警 + **动作不发生** + 留痕 |
| `human-in-loop` | 人允许才行 | **暂停 → resume**（`EventNode` + `/api/executions/{eid}/resume`） | execution 挂起；**超时=拒绝** |
| `forbidden` | 任何情况禁止 | GATE 恒定拒绝 | 同上 |

**判定次序**（GATE 节点内的算法）：

```
1. path_rules 命中 deny            → 拒绝（任何角色/档位都不能覆盖）
2. path_rules 命中 require-evidence → 检查 intent.evidence 是否含指定键，缺则拒绝
3. capabilities.by_role 查表        → forbidden/missing → 拒绝
                                      autonomous → 放行
                                      authorized → 放行（记录 level）
                                      human-in-loop → 挂起等 resume
```

**缺项 = 拒绝（fail-closed）**。这是有意的：新增能力必须显式登记，
防止"写在提示词里的隐形能力"绕过矩阵。

## 2. 红线逐条映射

现役 `playbook.md` §4 + `keeper_watch_flow.py` 提示词中的红线，逐条落位：

| # | 现役红线原文 | 档位 | capability | 实现 | 备注 |
|---|---|---|---|---|---|
| 1 | **绝不动 iOS 模拟器相关**（CoreSimulator / DeviceSupport / Simulator 镜像）——只报告不回收 | `authorized` | `delete_cache` | `path_rule: ios-simulator` → **deny** | jeffkit 2026-10-06 指示，最高优先 |
| 2 | 删任何 `*/target` 或缓存目录前**必查 `~/.local/bin` 软链指向** | `authorized` | `delete_target_dir` / `delete_cache` | `path_rule: target-dir-symlink` → **require-evidence[symlink_check]** | 10-08 事故：致 VM 全 impl 失败 13 分钟 |
| 3 | live run 的 worktree target 不可删 | `authorized` | `delete_target_dir` | `path_rule: live-worktree-target` → **deny** | 会打断在跑 run |
| 4 | `~/.cargo/registry` 不可删 | `authorized` | `delete_cache` | `path_rule: cargo-registry` → **deny** | 共享依赖缓存 |
| 5 | 不重启任何 worker/keeper/调度服务（需要重启 → #2 报请） | `human-in-loop` | `restart_worker` / `restart_keeper` | 挂起 + resume | 且需与沙箱侧对齐窗口 |
| 6 | 磁盘守卫线 20GiB | `autonomous` | `delete_cache` | 无需门；回收动作仍受 1-4 约束 | 阈值本身是配置 |
| 7 | 不代推他人提交 | `forbidden` | `push_others_commit` | GATE 拒绝 | 见 §4 讨论 |
| 8 | DLQ 只记不清 | `forbidden` | `purge_dlq` | GATE 拒绝 | 见 §4 讨论 |
| 9 | 不动 A 班与主控的 automation | `forbidden`（A/B）<br>`authorized`（主控） | `modify_automation` | GATE 按角色判定 | 主控本来就在管 automation |
| 10 | 重大拍板 → #2 留言不擅动 | `human-in-loop` | — | intent `escalate: true` + 挂起 | 与 finding.escalate 联动 |
| 11 | 一切 automation 变更留痕（备份 + 哈希 + #2） | `autonomous` | `modify_automation` | 强制写 `actions[]` | 留痕不是档位，是**必填字段** |
| 12 | 磁盘实操补充：删除前查软链（同 #2） | — | — | 同 #2 | 10-08 教训的正式化 |

**新增（现役未明说，但由职责隐含）**：

| capability | controller | A | B | 理由 |
|---|---|---|---|---|
| `close_issue` | forbidden | **authorized** | forbidden | 验收结论归 A（playbook：主控不替值守做业务判断） |
| `reopen_issue` | forbidden | authorized | **authorized** | 卡单处置归 B；A 验收时发现需重跑也可 reopen |
| `cancel_execution` / `resume_execution` | forbidden | authorized | authorized | 僵尸处置（D-0027 恢复序） |
| `change_concurrency` / `change_model_tier` | human-in-loop | human-in-loop | human-in-loop | 全局影响；jeffkit 拍板 |
| `change_infrastructure` | human-in-loop | human-in-loop | human-in-loop | **新增**：移动/启停调度服务、改部署拓扑。见下方说明 |
| `write_core`（写 duty 内核） | authorized | authorized | authorized | 各自写自己的轮报/handoff |

**`change_infrastructure` 的由来（2026-10-08 实证）**：
主控在 17:07 自行决定把调度器放在 Mac（理由"控制面同机+少碰 VM"），
被 jeffkit 指正「定时服务应在远端」后迁回。复盘原文：

> 当时选 Mac 的硬约束是 schedule_service **无 per-schedule queue 路由**……
> 我当时为「控制面同机+少碰 VM」选了 Mac，**未充分权衡常开性**

这类决策**不该由值守角色在巡检轮里拍**——它比"改配置"更重（改变部署拓扑、
影响面跨机器），却没有对应的 capability，等于**隐形权力**。
补上并设为 `human-in-loop`。

## 3. 需要你过目的判断（我可能放错的）

这几条是我替你做的判断，**只有你能定**：

1. **`change_model_tier` 全部设为 `human-in-loop`** —— 但 10-08 的现实是：
   B 班有「12:08 GLM 配额重置后切回 GLM」这样的**时点任务**写在 prompt 里。
   如果每次换档都要人 resume，这类时点任务会退化成"人得在线"。
   **备选**：给 `change_model_tier` 一个 `authorized` + **窗口约束**（只在预设时点 ±10min 内允许）。
   你怎么看？

2. **`restart_worker` 设为 `human-in-loop`** —— 但 playbook §4 的沙箱期约定是
   "与沙箱侧会话对齐窗口"，也就是**优先对齐、不是绝对禁止**。
   如果全设人在环，值守遇到卡死 worker 就只能干等。
   **备选**：改成 `authorized` + 强制 `evidence.aligned_with`（对齐了谁、何时）。

3. **`close_issue` 对 controller 设 `forbidden`** —— 严格按"主控不替值守做业务判断"。
   但如果 A/B 双双掉班，主控是否需要紧急关单能力？还是说那种情况一律升级给你？

4. **`delete_cache` 对 A 设 `forbidden`** —— 因为 playbook 写"磁盘实操归 B 班"。
   但如果 B 掉班且磁盘告急，A 是否该有应急回收权？

## 4. 现役"无人能执行"的红线（重要发现）

对照后发现，现役有几条红线**在系统里没有执行者**——它们只写在提示词里，
没有任何机制在检查：

| 红线 | 现役执行机制 | 问题 |
|---|---|---|
| 「删前必查软链」（#2） | **无** | 10-08 事故证明"写了"≠"被执行"。需要 `require-evidence` |
| 「不代推他人提交」（#7） | 无（靠自觉） | 可审计化：`git push` 前查 author |
| 「DLQ 只记不清」（#8） | 无（靠自觉） | 可加 GATE：DLQ 相关命令一律需授权 |
| 「不动他人 automation」（#9） | 无（靠自觉） | `modify_automation` 可门控；zcode 侧无接口，flow 侧有 |
| 「不与沙箱侧冲突」（§4 沙箱期约定） | 靠人读 playbook | 窗口对齐可结构化为 intent 字段 |

**这一列空白就是迁移动机的量化证据**：现在有 5 条不可逆损失级别的红线，
执行机制是"LLM 记得住"。

## 5. 与 flow 的接线（实施时）

```python
# GATE 节点（CODE，在 AGENTRUN 之后）
def run(input):
    roster = read_core("roster")            # 强校验
    intent = input["intent"]                # AGENTRUN 产出
    # 1) path_rules（恒定，先于角色）
    for rule in roster["capabilities"]["path_rules"]:
        if re.search(rule["pattern"], intent["target"]):
            if rule["verdict"] == "deny":
                return reject(intent, f"path-rule:{rule['id']}")
            if rule["verdict"] == "require-evidence":
                missing = [k for k in rule["requires"] if k not in intent.get("evidence", {})]
                if missing:
                    return reject(intent, f"evidence-missing:{missing[0]}")
    # 2) 角色能力（缺项 = 拒绝）
    level = roster["capabilities"]["by_role"].get(role, {}).get(intent["capability"])
    if level in (None, "forbidden"):
        return reject(intent, "capability-not-granted")
    if level == "human-in-loop":
        return suspend(intent)              # EventNode 挂起，超时=拒绝
    return execute(intent)
```

**拒绝路径必须写 `state.intents_rejected`** —— 这是"越权尝试留痕"，
也是迁移后新增的可观测性（现在是"没人知道"）。

## 6. 测试策略

`validate.py` 已覆盖 schema 层。GATE 节点还需要**行为测试**（实施阶段 2 做）：

| 用例 | 期望 |
|---|---|
| B 删 `~/Library/Developer/CoreSimulator/...` | 拒绝，且**不论 B 有无 `delete_cache` 授权** |
| B 删 `~/recursive-src/target` 未附 `symlink_check` | 拒绝，reason=`evidence-missing:symlink_check` |
| B 删 `~/recursive-src/target` 附了 `symlink_check` | 放行 |
| controller 尝试 `close_issue` | 拒绝，reason=`capability-not-granted` |
| A 尝试 `purge_dlq` | 拒绝 |
| 任意角色 `restart_worker` | 挂起（不执行），超时后拒绝 |
| 未登记 capability | 拒绝（fail-closed） |

这些用例的价值：**它们是"红线"这个东西第一次变得可测试**。
