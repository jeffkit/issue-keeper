# 处置阶梯与踩坑清单

> 通用纪律：**先取证**（日志 + state + console 三处对齐）→ 按阶梯**先便宜后昂贵** → 动作留痕 → 事后验证。
> 阶梯的共同形状：**观测 → 低风险自救（resume/清理）→ 有损自救（cancel+reopen）→ 找人**。

## 1. 在途 run 疑似卡死（最常见）

判据（**别只看"末节点结束多久"**）：impl 类节点单跑 60-120 分钟且中途不写状态，
所以 `末节点结束 60min` 在长节点里**正常**。真卡死是**两者的交集**：

```
末节点结束 > 120min 且 进度龄(last_update_time 年龄) > 120min   ← 超过 keeper 僵尸线仍未收
```

阶梯：
1. `POST /api/executions/<eid>/resume {"resume_type":"retry"}`（**先救命稻草**）；等 3 分钟看节点数/进度龄是否动；
   有效→留痕；无效→下一步。**同一执行只自动 resume 一次**（账本 `duty/inflight-resume.json`）；
2. `POST .../cancel` → **keeper reopen**（VM 上，见 toolbox）→ 让流水线换新沙箱重跑；
3. 若其沙箱实例仍在跑 → 杀（见 §3）。

## 2. 执行已终态但 keeper 仍算在途（"收尸滞后"）

keeper reaper 是**轮询制**，1-3 分钟延迟正常。**>10 分钟**才报。
先看 `~/.issue-keeper/keeper.log` 有没有 `console execution 终态落账`；没有 → 看 reaper 是否在跑（keeper 轮次日志）、
有无异常堆栈；必要时按 §6 处理 keeper。

## 3. AGS 沙箱实例（成本）

```bash
export E2B_DOMAIN=ap-guangzhou.tencentags.com E2B_API_KEY=e2b_725235357335be8d27367c596c9e3199cf3c5eeb
/Users/kong/projects/infra4agent/plaita/.venv/bin/python ~/projects/infra4agent/issue-keeper/flows/ags-list.py
# 杀：Sandbox.kill(<完整 sandbox_id>)  ← 必须完整 ID（截断会 404）
```
- **孤儿**：run 已终态（completed/failed/error/cancelled）但实例仍 running → 杀（`sandbox-watch` */30 也会杀；`ags-orphan-sweep` 是独立兜底）。
- **卡死**：实例存活、其执行 running 但 >30 分钟无节点更新 → 先按 §1 处置执行，再决定杀实例。
- 成本观感：24h 实例小时按终态分 running/completed/cancelled；cancelled 占比高=事故浪费，值得复盘。

## 4. 磁盘守卫（"整线瘫痪但没任何告警"）

症状：keeper 日志 `pipeline retry-later（disk XX GiB < min）——不消费，第 N 次，21600s 后重试`。
根因：`recursive/.dev/flows/self_improve_flow_v2.py` 的 preflight 读 `RECURSIVE_MIN_FREE_DISK_GIB`（**默认 20**），
不足则**不启动**且退避 6h → run 从未开始，**所有看门狗都不报**（不是卡死、不是孤儿）。

阶梯：
1. 释放磁盘（`disk-hygiene` flow 会自动做；手动同等动作：清 `~/projects/infra4agent/*/.flowcast/runs/` 下 mtime>24h 的目录、陈旧 build 目录、`~/.cache/{pip,npm,uv}`）；
2. **清退避**（`reopen` **不会**清 `retry_after`）：原子改写 VM `state.json`，对 `retry_after > now` 且 `retry_later_streak ≥ 2` 的条目置 `None`/`0`，**先备份**；
3. 验证：下一次派发真的起来（keeper 日志/在途数）。

## 5. needs-human（有人在等 / 标签挂着）

1. 读升级评论 + keeper state（`processed` 是否为 True）；
2. **查真因**（多数是契约/配置/环境问题）：典型是 setup/门/依赖路径；改配置或用例，别只清标签；
3. 处置：`reopen`（会**自动摘标**）→ 若该单要紧，加入 `pipeline_priority_issues`（插队，见 toolbox）；
4. 标签语义：**只是"正在等人"的徽标**，无任何闸门；`reopen` 时自动摘；单人不需要时也可手工摘。

## 6. 调度器 / worker 停摆

- **先确认火已经到点**（我看错过一次：10:25 查 10:30 的火，误判"挂了"）。判据：`~/.plaita-console/schedule-service.log` 中**某个已过去的触发时刻缺行**。
- 调度器：`ssh tcloud_gz 'pkill -f "services.__main__.*schedule_service"'`（systemd `Restart=always` 拉起）或 `sudo -n systemctl restart plaita-schedule-service`。
- **重启 worker / keeper = human-in-loop**（需请示）；重启会**从 checkpoint 重放在途 run**（at-least-once，业务节点须幂等）——挑窗口、先看在途数。
- 调度器停摆的**发现**靠 `external-watchdog`（launchd，独立于调度器）与 `ctrl-watch` 心跳。

## 7. 踩坑清单（都是真金白银换的）

| 坑 | 现象 | 教训 |
|---|---|---|
| console 时间戳是**本地 naive** | 当 UTC 解析 → 进度龄 -8h | 统一折算本地 naive 再比 |
| `wait_reply=false` 的 HITL | 人回复"AI 收不到"；`/admin/api/hil/sessions` 为空 | **必建会话**；回复才可 poll |
| 长节点 vs 卡死 | impl 60-120min 内"末节点结束已久"是**正常** | 阈值 60 warn / 120 且进度龄>120 才 critical |
| 告警去重键用内容哈希 | 摘要含分钟数 → 每分钟算新告警（30min 推 4 次） | 用**稳定键**（run+种类） |
| keeper 僵尸判据 | 不是"0 租约"（那是文档口误）；实际=last_update_time 年龄>2h | **先读实现再下结论** |
| 磁盘守卫退避 | `reopen` 不清 `retry_after` | 直接原子改 state + 备份 |
| VM `git pull --rebase` | 与管线分支冲突，生产树留下冲突标记 | 先 `git status`；冲突时 `git rebase --abort` 再从容合并 |
| 共享 checkout | 别人（kongjie）也在同一目录提交 | 提交前 `git status`/`git log -1`，别把他人改动裹进自己的提交 |
| 沙箱 `Sandbox.kill` | 截断 ID → 404 | 用完整 sandbox_id |
| `ssh host "python3 -c '...'"` | 引号被 shell 吃掉，静默返回空 | 脚本走 **stdin**：`ssh host python3 - < script` |
