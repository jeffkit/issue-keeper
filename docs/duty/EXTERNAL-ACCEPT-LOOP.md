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

## 2026-10-08 夜临时调整（E2E 完毕恢复）

- 远端 config：`pipeline_max_in_flight` 4→**5**、`pipeline_priority_repos` +`jeffkit/tunely`
  （均为热加载，只为让 #26 插队验证；备份 `config.yaml.bak-20261008-external`）。
- 取消了 ctrl 队列上的过期恢复专火（keeper-watch f123946a，其使命 17:09 恢复已完成）。

## 已知后续（P1/P2）

1. 外部作者的 `/accept` 会触发 keeper 评论 agent 回复（噪音，无害）——评论路径
   应对「awaiting-accept 状态的 issue」豁免或降级。
2. `verify` AGENTRUN 对非空单的成本/时长需观测（今晚只实证了空单路径）。
3. console 执行详情的 `output` 字段未回传 flow 返回值（cosmetic；duty 文件才是记录）。
4. `/reject` 目前只升级不自动处置——后续可接「按 reject 描述重开管线」。
