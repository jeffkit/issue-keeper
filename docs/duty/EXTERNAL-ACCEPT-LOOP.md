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

## 22:3x 夜间事故两起（已处置，ctrl-watch/僵尸线兜底验证中）

1. **schedule service 挂死（22:30:00）**：成功入队 22:30 两笔后扫描循环静默死亡
   （systemd active 但零日志；症状属 plaita#48「Redis 读超时炸穿」族）。影响=全部
   cron 调度停摆（A/主控/B 三班都无法醒），且 **ctrl-watch 自己也被它调度，无法自察**
   ——「看门狗必须独立于被看护系统」的实锤。处置：22:34 kill 进程由 systemd 重拉，
   22:34:05 新实例健康，22:40:00 准点触发 keeper-watch+issue-accept ✓。
   **P1**：外置看门狗（Mac launchd 或另一台机，curl console 查各班心跳新鲜度 → 报警）。
   **P1**：schedule trigger/cron 入队消息偶发消失（20:07、22:34 两例；22:40 cron 正常）
   ——plaita#50 族的现场样本，已具足以提报。
2. **v1.0.7 回退性发布（22:1x，自纠）**：从落后 origin/main 的检出编译发布 v1.0.7，
   漏掉 `3c0a4ab8`（#148 kill-stale 互杀修复，同改 flow 源码+产物）。发现于 pull
   冲突 → skip 被包含的自提交 → 以 3c0a4ab8 产物发 **v1.0.8** 纠正（逐字节核验 ✓）。
   教训：**发布前必须 fetch + 确认检出 == origin/main**（compile_v2 的对账只对
   「同源码」负责，不负责「源码是不是最新」）。

## 二进制部署（22:2x，jeffkit 令「漂移今晚处理」）

- Mac：`3c0a4ab8` 全量修复构建（增量 5m41s），双位置安装
  `/opt/homebrew/bin/recursive`（原 Oct 2 旧货，已备份 /tmp）+ `~/.local/bin/recursive`
  （恢复为实体文件）。两处均验证可执行。
- ⚠️ **法证**：`~/.local/bin/recursive`（Oct 8 00:08 构建）在 21:30-22:30 间被未知
  方式删除——嫌疑窗口内有 B 班看守轮的磁盘动作（21:22 轮自述红线检查全过且只动 VM）
  与管线 run 清理；**删除者未明，P1 由 ctrl-watch/B 班追**。
- VM：构建两次受阻——①旧产物检出（已对齐 3c0a4ab8）；②**工具链 1.75 < edition-2024
  要求（1.85+），发行版 rustup 拒装工具链** → VM 二进制维持 0.8.3 可用态，升级需装
  官方 rustup（会动 VM 全部 cargo 构建环境含 tunely rust 门，白天做）→ **P1**。
- 部署可追溯：版本号未 bump（仍 0.8.3），以「部署时刻 + 源 SHA=3c0a4ab8」记录于本文件。

## 当夜临时调整与恢复记录
- ~~远端 config：`pipeline_max_in_flight` 4→5、priority +tunely、tunely 仓限 1→2~~
  **已全部恢复原状**（19:5x；为 #26 插队而设，#26 被 VM 磁盘守卫另行阻塞）。
- keeper-watch schedule 由 paused → **enabled**（B 班复活，非临时改动）。
- 取消了 ctrl 队列上的过期恢复专火（keeper-watch f123946a，其使命 17:09 恢复已完成）。
- VM 磁盘处置（20:1x，jeffkit 拍板「VM 不跑 rust，不需要 20G 红线，放开」）：
  ①回收 recursive/.worktrees 两个陈旧 worktree（16G；issue-134 的 5 个未提交文件
  已按 reaper 同款语义 wip 快照到分支 `fix/134-eval-batch-ptc` 后删除，成果零丢失；
  issue-147 分支干净直删。两分支均保留）→ **VM 28G 可用**；
  ②tunely `engine_env` 加 `RECURSIVE_MIN_FREE_DISK_GIB=8`（阈值机制在
  self-improve-v2 preflight，默认 20；经 dispatch env 注入，下轮派发生效）。
  至此 #26 的磁盘阻塞解除，等 tunely#3 释放仓配额后自然派发。

## 已知后续（P1/P2）

1. 外部作者的 `/accept` 会触发 keeper 评论 agent 回复（噪音，无害）——评论路径
   应对「awaiting-accept 状态的 issue」豁免或降级。
2. `verify` AGENTRUN 对非空单的成本/时长需观测（#20 实际走了初验并给出准确结论 ✓）。
3. console 执行详情的 `output` 字段未回传 flow 返回值（cosmetic；duty 文件才是记录）。
4. `/reject` 目前只升级不自动处置——后续可接「按 reject 描述重开管线」。
5. **schedule trigger 端点行为不稳**（18:19 有效、20:07 入队消息消失）——待查，
   短期用 enable+定时火兜底。
6. **VM 磁盘**：已按 jeffkit 拍板处置（16G worktree 回收 + tunely 阈值 8G），余量 28G。

## 漂移审计（2026-10-08 22:1x，jeffkit 令「今晚处理」）

| flow | 发现 | 处置 |
|---|---|---|
| `self-improve-v2` | 产物落后源码 1288 行（`6c65905b`#83 于 10-08 01:57 落地后未重编译未发布） | ✅ **已闭合**：重编译（100+52 节点）→ `flow_v2_paths.py` 42/42 → console **v1.0.7** 发布 → 与本地产物逐字节一致（recursive `5fb1dfec`） |
| `keeper-watch` | 疑似 console(0.0.3, 2700s) ≠ repo(1500s) | ✅ **误报**：`1b5dea0` 已同步三者（v0.0.3 预算 1500→2700 有据：17:15 恢复专火超时实证） |
| `self-improve-v2-sbx` | JSON(12:41) 新于源码(01:12) | ✅ 无漂移 |
| **recursive 二进制** | 两台 worker 均为 **0.8.3（Mac 构建于 00:08）**，早于 `6c65905b`（01:57）→ **缺 #83 的 agent.rs 确定性修复**（execute_single 双计时器竞态；运行时影响小） | ⏳ **P1 待部署**：release 构建 + 双机安装；建议顺带把版本号 bump 纪律立起来（改动落了、版本没动，导致无法用版本判断部署状态） |

结构性观察（P2）：#83 的看门狗接线在**宿主层 bridge**（`self_improve_bridge_v2.main()`），
而生产走 console/worker 路径——**该层在 console 路径上不存在**。若要让看门狗覆盖
console 执行，需要把接线沉到 flow_worker 侧或做成 plaita-nodes 层装饰（另立设计）。
