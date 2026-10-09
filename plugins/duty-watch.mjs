// duty-watch.mjs — 值守事件触发器：critical 工单出现时**主动唤醒值守会话**。
//
// 背景（2026-10-09 jeffkit 拍板 #2）：值守此前是「工单躺在收件箱，等我 30 分钟一轮
// 醒来才看到」——磁盘守卫曾把管线卡停 30 分钟而无人知晓。DSH 原生有 Agent inbox
// （agent/inbox/inserted），schedule 插件正是靠往 inbox 插消息投递提醒；本插件复用
// 同一入口：sessionController.prompt(...) → agent.followup(message)。
//
//   duty/requests/*.json (新 critical 单)
//        │  fs 轮询（默认 15s）
//        ▼
//   ctx.sessionController.prompt({ sessionId, content:[{type:'text',...}] })
//        │
//        ▼  值守会话 inbox → 立即起一轮（不必等定时器）
//
// 载入方式：profile patch 层（~/.dsh/profiles/web/cordis.patch.yml）
//   - insert:
//       - id: duty-watch
//         name: /Users/kong/.dsh/plugins/duty-watch.mjs
//         config:
//           sessionId: session-74ca4d26-373c-43a9-92f4-3cc455fc1ef6   # 可省略 → 取最近活跃
//           intervalMs: 15000
//           cooldownMin: 60
//
// 配置（都可从环境覆盖）：DUTY_WATCH_DIR / DUTY_WATCH_SESSION_ID /
// DUTY_WATCH_INTERVAL_MS / DUTY_WATCH_COOLDOWN_MIN
//
// 纪律：本插件**只投递事件通知，不做任何判断与动作**——判断是值守 Agent 的活。

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const name = 'duty-watch'

// Cordis：声明依赖服务，等服务就绪再启动（同时挂到 apply 上，兼容「读模块命名导出」
// 与「读函数属性」两种插件加载约定）
const inject = ['sessionController']
apply.inject = inject

function apply(ctx, config = {}) {
  const dir = String(
    config.requestsDir || process.env.DUTY_WATCH_DIR
    || path.join(os.homedir(), '.issue-keeper', 'duty', 'requests'),
  )
  const intervalMs = Number(config.intervalMs || process.env.DUTY_WATCH_INTERVAL_MS || 15000)
  const cooldownMs = Number(config.cooldownMin || process.env.DUTY_WATCH_COOLDOWN_MIN || 60) * 60_000
  const fixedSession = String(config.sessionId || process.env.DUTY_WATCH_SESSION_ID || '')
  const log = ctx.logger ?? console

  const seen = new Map() // ticket id -> last injected ms
  let running = false

  async function resolveTarget() {
    if (fixedSession) return fixedSession
    try {
      const value = await ctx.sessionController.list({}, new AbortController().signal)
      const items = (value?.items ?? []).filter(it => it?.sessionId && it.origin !== 'subagent')
      items.sort((a, b) => Number(b.updatedAt ?? 0) - Number(a.updatedAt ?? 0))
      const pick = items.find(it => it.running) ?? items[0]
      return pick?.sessionId ?? ''
    } catch (error) {
      log.warn?.(`[duty-watch] resolve target failed: ${error}`)
      return ''
    }
  }

  async function tick() {
    if (running) return
    running = true
    try {
      let files = []
      try {
        files = fs.readdirSync(dir).filter(f => f.startsWith('req-') && f.endsWith('.json'))
      } catch {
        return // 目录还没建，静默等
      }
      for (const file of files) {
        let doc
        try {
          doc = JSON.parse(fs.readFileSync(path.join(dir, file), 'utf8'))
        } catch {
          continue
        }
        if (doc?.severity !== 'critical') continue
        if (!['open', 'escalated'].includes(doc?.status)) continue
        const id = String(doc.id || file)
        const last = seen.get(id) ?? 0
        if (Date.now() - last < cooldownMs) continue
        const sessionId = await resolveTarget()
        if (!sessionId) {
          log.warn?.('[duty-watch] 无可用目标会话，跳过投递')
          continue
        }
        const text = [
          '【值守事件·critical 工单】' + String(doc.title || id),
          `来源 ${doc.from_flow || '?'} · 类别 ${doc.kind || '?'} · 单号 ${id}`,
          '请立即起一轮：先跑 duty_probe，再读该工单并按能力矩阵处置（能自决就自决，判不了才 HITL）。',
        ].join('\n')
        try {
          await ctx.sessionController.prompt({
            requestId: `duty-watch-${id}-${Date.now()}`,
            sessionId,
            mode: 'queue',
            content: [{ type: 'text', text }],
          }, new AbortController().signal)
          seen.set(id, Date.now())
          log.info?.(`[duty-watch] injected ${id} → ${sessionId}`)
        } catch (error) {
          log.warn?.(`[duty-watch] inject failed for ${id}: ${error}`)
        }
      }
    } finally {
      running = false
    }
  }

  const timer = setInterval(() => { tick().catch(error => log.warn?.(`[duty-watch] tick: ${error}`)) }, intervalMs)
  ctx.effect(() => () => clearInterval(timer))
  // 启动稍后先跑一次（覆盖「插件上线时已有 critical 在悬」的情形）
  const kick = setTimeout(() => { tick().catch(() => {}) }, 5000)
  ctx.effect(() => () => clearTimeout(kick))
  log.info?.(`[duty-watch] watching ${dir} every ${intervalMs}ms → ${fixedSession || '(最近活跃会话)'}`)
}

export { apply, inject, name }
