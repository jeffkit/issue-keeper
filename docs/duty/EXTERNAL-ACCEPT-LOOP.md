# EXTERNAL-ACCEPT-LOOP —— 外部验收闭环 runbook（2026-10-08 夜上线）

> 目标（jeffkit 拍板）：外部用户通过 GitHub 提 issue → 系统接单开发 → 自测提交 →
> 回评请验收 → **外部用户自己验收**（/accept）→ 系统合并 PR + 关单。
> **B 班（keeper-watch）流程不参与外部单**，仅供内部批量提 issue / 内部验收场景。
> A 班（验收）自此以 flow 形态值守：`issue-accept`（ctrl 队列，*/10）。

## 全链路

```
外部用户提 issue（作者 ∈ external_authors）
  → keeper-shadow v1.0.6+ 派发（gate 放行 external_authors；screener 照走，
     trusted_authors 直通；keeper 库同款 _dispatch_pipeline，锚/产物/去重一字不差）
  → self-improve-v2 开发 + 质量门 + push_mode=pr 交付
  → reaper 回评「管线收尾：status=done」
  → issue-accept flow（A 班）：读码初验 → 回评 `<!-- duty:awaiting-accept -->` 请验收
  → 外部用户回复 /accept（或「验收通过」）
  → issue-accept：合并 PR → 关单致谢
```

## 组件与配置

| 组件 | 位置 | 要点 |
|---|---|---|
| `issue-accept` flow | console（首发 v1.0.0，2026-10-08） | `flows/issue_accept_flow.py` 为源码主体；JSON 是编译产物 |
| schedule | `sched-20261008181949836`，cron `*/10`，ctrl 队列（Mac worker-ctrl-1） | params: repo/bot/max_verify/max_close/**dryrun** |
| keeper-shadow | v1.0.6 起 gate 认 `external_authors` | repo 与 console 1.0.6 已同步 |
| 外部作者白名单 | 远端 config `external_authors: [okguitar]` | 与 author_allowlist 并列，OR 语义；screener 不豁免（trusted_authors 另算） |
| duty 内核 | `~/.issue-keeper/duty/`（roster/state-*/handoffs/rounds.log） | `issue_keeper/duty.py`；schema 契约 `docs/duty/schema/`（validate.py 18/18） |

## 状态载体（两层）

1. **issue 评论 marker `<!-- duty:awaiting-accept -->`**（随 issue 走、天然幂等）——
   「已初验、待外部验收」；重跑不会重复发。
2. **duty 内核**（跨轮滚动窗口，保留 20 轮）——轮次状态 / 交接 / 审计。

防循环：flow 回评以 `<!-- issue-pipeline -->` 开头（keeper/triage 认作自己人）+
jeffkit 身份（self_identity 层兜底）。

## 验收信号

| 外部用户回复 | 系统动作 |
|---|---|
| `/accept` 或含「验收通过」 | 合并关联 PR（`--merge`）→ 回评致谢 → 关单 |
| `/reject` 或含「验收不通过」 | 仅记 finding + escalate（本轮不自动动作，走人工） |

判定约束：信号作者 ≠ bot（jeffkit），且时间晚于 awaiting marker。

## 初验边界（诚实声明）

issue-accept 的初验 = **读码**（issue + 关联 PR diff + 落地提交），**不重跑质量门**
（门由管线跑过，A 班不重复 8h 级预算）。完整门复跑是 A 班 flow 的后续迭代项。

## 运维

- **dryrun**：schedule params `dryrun: true` → 全流程照跑但零 GitHub 写入（上线验证用）。
- **首跑记录**：2026-10-08 18:33 两轮 dryrun completed（8s/轮，空单 no-op 路径正确），
  duty rounds 3/4 落盘。
- **回滚**：console 删 schedule 即停（issue 上的 marker 不受影响）；
  `external_authors` 清空即关闭外部接单（已有 run 不受影响）。

## 2026-10-08 夜实战记录（E2E 首航）

**闭环在 tunely#20 上端到端走通**（19:49 /accept → 19:50 关单）：

```
#20（security: check-availability 匿名枚举 oracle）
  → shadow 派发 → self-improve-v2 开发 → 直推 main 9711b1a
  → issue-accept 轮9（19:26）初验 + 发 awaiting-accept
  → okguitar（外部角色）/accept（19:49，附 main 核对说明）
  → issue-accept 轮12（19:50）关单致谢 ✅
```

**首航抓出并修复一个真缺陷**：act 节点 PR 匹配用 `str(n) in title`——
dependabot PR 标题"bump @types/node from **20**.19.30"撞上 issue#20 的子串，
误合了无关的 dependabot#15。处置：main revert（`d35ed88`）+ 匹配改为
**只认 `issue-<n>` 分支后缀**（v1.0.1）+ 双向留痕（#20 更正评论 + PR#15 说明）。
教训入档：**合并类动作必须分支↔单号精确对应，标题永不参与匹配**（能力矩阵
§5 的 require-evidence 模式在 flow 层的第一次实战价值证明）。

**同夜上线的值守全 flow 阵容**（zcode 主控 19:36 退勤后）：

| 角色 | 形态 | 心跳 |
|---|---|---|
| A 班 | `issue-accept` flow v1.0.1（*/10） | duty `state-issue-accept.json` |
| B 班 | `keeper-watch` flow v0.2（40 */2，20:40 复活） | controller/rounds.log B-flow 行 |
| 主控 | `ctrl-watch` flow v1.0.0（*/30，20:00 首火） | duty `state-controller.json` |
| 开发 | `self-improve-v2`（既有） | console executions |
| 派发 | `keeper-shadow` v1.0.6（keeper 每 5min 派） | 远端 shadow/latest.json |

**ctrl-watch 首火（20:00）即立功**：抓到 B 班心跳超期 319min（16:40 GLM 1308
失败链后 schedule 被 pause、无人 restart）→ 重新 enable + 手动补火。

## 2026-10-08 夜临时调整（E2E 完毕恢复）

- ~~远端 config：`pipeline_max_in_flight` 4→5、priority +tunely、tunely 仓限 1→2~~
  **已全部恢复原状**（19:5x；为 #26 插队而设，#26 被 VM 磁盘守卫另行阻塞）。
- keeper-watch schedule 由 paused → **enabled**（B 班复活，非临时改动）。
- 取消了 ctrl 队列上的过期恢复专火（keeper-watch f123946a，其使命 17:09 恢复已完成）。
- VM 磁盘回收：docker prune（926MB）+ 僵尸容器清理；**磁盘 16-19G 仍低于 20G 守线**，
  新派发会被 retry-later 挡——#26 的派发等磁盘恢复（recursive 16G 陈旧 worktree
  是最大可回收项，但 recursive#147 评论 agent 活跃于该克隆，今晚不动，留 B 班按域处置）。

## 已知后续（P1/P2）

1. 外部作者的 `/accept` 会触发 keeper 评论 agent 回复（噪音，无害）——评论路径
   应对「awaiting-accept 状态的 issue」豁免或降级。
2. `verify` AGENTRUN 对非空单的成本/时长需观测（#20 实际走了初验并给出准确结论 ✓）。
3. console 执行详情的 `output` 字段未回传 flow 返回值（cosmetic；duty 文件才是记录）。
4. `/reject` 目前只升级不自动处置——后续可接「按 reject 描述重开管线」。
5. **schedule trigger 端点行为不稳**（18:19 有效、20:07 入队消息消失）——待查，
   短期用 enable+定时火兜底。
6. **VM 磁盘**：recursive 克隆 16G（.worktrees 为主）是最大可回收项；#147 评论
   agent 仍活跃于该克隆，回收须等其空闲 + B 班按域处置。
