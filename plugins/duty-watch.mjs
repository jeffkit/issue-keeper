// duty-watch.mjs — 值守事件触发器：把「需要我动」的事件**主动送进值守会话**。
//
// 背景（2026-10-09 jeffkit 拍板 #2 + 追加「HITL 回复也要进收件箱」）：
// 值守此前是「工单躺在收件箱，等我 30 分钟一轮醒来才看到」。DSH 原生有 Agent inbox
// （agent/inbox/inserted），schedule 插件正是靠往 inbox 插消息投递提醒；本插件复用
// 同一入口：sessionController.prompt(...) → agent.followup(message)。
//
// 监听三类事件（都只投递通知、不做判断）：
//   ① duty/requests/*.json —— 新 critical 且 open/escalated（60min cooldown/单）
//   ② 同上，status=answered —— 人工已回复工单（一次性）
//   ③ duty/hitl-notifications.jsonl —— 人工回复了 HITL 通知（每 session 一次性）
//
//   duty/ 三个来源 → fs 轮询（默认 15s）→ sessionController.prompt → 值守会话 inbox
//
// 载入方式：profile patch 层（~/.dsh/profiles/lavs/cordis.patch.yml）：
//   - insert:
//       - id: duty-watch
//         name: /Users/kong/.dsh/plugins/duty-watch.mjs
//         config: { intervalMs: 15000, cooldownMin: 60 }
//
// 配置（环境可覆盖）：DUTY_WATCH_DIR / DUTY_WATCH_SESSION_ID /
// DUTY_WATCH_INTERVAL_MS / DUTY_WATCH_COOLDOWN_MIN
//
// 纪律：本插件**只投递事件通知，不做任何判断与动作**——判断是值守 Agent 的活。

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const name = 'duty-watch'

// Cordis：声明依赖服务，等服务就绪再启动（同时挂到 apply 上，兼容两种加载约定）
const inject = ['sessionController']
apply.inject = inject

function apply(ctx, config = {}) {
  const dir = String(
    config.requestsDir || process.env.DUTY_WATCH_DIR
    || path.join(os.homedir(), '.issue-keeper', 'duty', 'requests'),
  )
  const ledger = String(config.ledgerPath || path.join(path.dirname(dir), 'hitl-notifications.jsonl'))
  const intervalMs = Number(config.intervalMs || process.env.DUTY_WATCH_INTERVAL_MS || 15000)
  const cooldownMs = Number(config.cooldownMin || process.env.DUTY_WATCH_COOLDOWN_MIN || 60) * 60_000
  const fixedSession = String(config.sessionId || process.env.DUTY_WATCH_SESSION_ID || '')
  const log = ctx.logger ?? console

  const seen = new Map()      // critical 工单 id -> 上次投递 ms（cooldown）
  const seenOnce = new Set()  // 回复类事件 key -> 已投递（一次性）
  let priming = true          // 首次 tick 只给回复类事件做基线，不投历史回复
  let running = false
  const scheduleStore = String(config.scheduleStore || path.join(os.homedir(), '.dsh', 'storages', 'schedule.json'))

  // 哪些会话持有「值守」定时任务 → 事件的优先投递目标（新会话起了值守轮即自动接棒）
  function dutySessionIds() {
    try {
      const doc = JSON.parse(fs.readFileSync(scheduleStore, 'utf8'))
      const ids = new Set()
      for (const task of Object.values(doc?.tables?.tasks ?? {})) {
        const title = String(task?.record?.title ?? '')
        const sid = String(task?.sessionId ?? '')
        if (sid && title.includes('值守')) ids.add(sid)
      }
      return ids
    } catch {
      return new Set()
    }
  }

  async function resolveTarget() {
    if (fixedSession) return fixedSession
    try {
      const value = await ctx.sessionController.list({}, new AbortController().signal)
      const items = (value?.items ?? []).filter(it => it?.sessionId && it.origin !== 'subagent')
      // 优先投给「持有值守定时任务」的会话（读 DSH schedule 存储）——否则新开一个
      // 随便聊两句的会话会把值守事件抢走。没有值守会话时退回最近活跃会话。
      const duty = dutySessionIds()
      const preferred = items.filter(it => duty.has(String(it.sessionId)))
      const pool = preferred.length ? preferred : items
      pool.sort((a, b) => Number(b.updatedAt ?? 0) - Number(a.updatedAt ?? 0))
      const pick = pool.find(it => it.running) ?? pool[0]
      return pick?.sessionId ?? ''
    } catch (error) {
      log.warn?.(`[duty-watch] resolve target failed: ${error}`)
      return ''
    }
  }

  async function inject(key, text) {
    const sessionId = await resolveTarget()
    if (!sessionId) {
      log.warn?.('[duty-watch] 无可用目标会话，跳过投递')
      return false
    }
    try {
      await ctx.sessionController.prompt({
        requestId: `duty-watch-${key}-${Date.now()}`,
        sessionId,
        mode: 'queue',
        content: [{ type: 'text', text }],
      }, new AbortController().signal)
      log.info?.(`[duty-watch] injected ${key} → ${sessionId}`)
      return true
    } catch (error) {
      log.warn?.(`[duty-watch] inject failed for ${key}: ${error}`)
      return false
    }
  }

  function readLedgerReplies() {
    const out = []
    let raw = ''
    try {
      raw = fs.readFileSync(ledger, 'utf8')
    } catch {
      return out
    }
    for (const line of raw.split('\n')) {
      if (!line.trim()) continue
      try {
        const rec = JSON.parse(line)
        if (rec?.session_id && rec?.replied_at) out.push(rec)
      } catch { /* 半行/损坏行忽略 */ }
    }
    return out
  }

  async function tick() {
    if (running) return
    running = true
    try {
      const scanning = priming
      // ① critical 工单（open/escalated）
      let files = []
      try {
        files = fs.readdirSync(dir).filter(f => f.startsWith('req-') && f.endsWith('.json'))
      } catch { /* 目录还没建 */ }
      for (const file of files) {
        let doc
        try {
          doc = JSON.parse(fs.readFileSync(path.join(dir, file), 'utf8'))
        } catch {
          continue
        }
        const id = String(doc?.id || file)
        // ② 人工已回复工单（status=answered，一次性）
        if (doc?.status === 'answered') {
          const key = `answered:${id}`
          if (!seenOnce.has(key)) {
            seenOnce.add(key)
            if (!scanning) {
              await inject(key, [
                '【值守事件·工单已被人工回复】' + String(doc.title || id),
                `单号 ${id} · 来源 ${doc.from_flow || '?'}`,
                '请按回复内容执行（矩阵内自决直接办，判不了才再 HITL）。',
              ].join('\n'))
            }
          }
          continue
        }
        // ① 新 critical 工单
        if (doc?.severity !== 'critical') continue
        if (!['open', 'escalated'].includes(doc?.status)) continue
        const last = seen.get(id) ?? 0
        if (Date.now() - last < cooldownMs) continue
        const ok = await inject(id, [
          '【值守事件·critical 工单】' + String(doc.title || id),
          `来源 ${doc.from_flow || '?'} · 类别 ${doc.kind || '?'} · 单号 ${id}`,
          '请立即起一轮：先跑 duty_probe，再读该工单并按能力矩阵处置（能自决就自决，判不了才 HITL）。',
        ].join('\n'))
        if (ok) seen.set(id, Date.now())
      }

      // ③ HITL 人工回复（ledger 里带 replied_at 的记录，每 session 一次性）
      for (const rec of readLedgerReplies()) {
        const sid = String(rec.session_id)
        const key = `hitl:${sid}`
        if (seenOnce.has(key)) continue
        seenOnce.add(key)
        if (scanning) continue
        const text = String(
          rec.reply_text
          || (Array.isArray(rec.replies) ? rec.replies.join(' / ') : '')
          || '',
        ).slice(0, 400)
        await inject(key, [
          '【值守事件·人工已回复 HITL】' + String(rec.title || '(无标题)').slice(0, 70),
          `回复：${text || '(空)'}`,
          '请按回复执行（矩阵内直接办）；如需回复人，用 hitl_notify.py 白话三行。',
        ].join('\n'))
      }

      if (priming) {
        priming = false
        log.info?.(`[duty-watch] 基线已建（工单 ${files.length} 个 / 历史回复已标记，不重投）`)
      }
    } finally {
      running = false
    }
  }

  const timer = setInterval(() => { tick().catch(error => log.warn?.(`[duty-watch] tick: ${error}`)) }, intervalMs)
  ctx.effect(() => () => clearInterval(timer))
  const kick = setTimeout(() => { tick().catch(() => {}) }, 5000)
  ctx.effect(() => () => clearTimeout(kick))
  log.info?.(`[duty-watch] watching ${dir} + ${path.basename(ledger)} every ${intervalMs}ms → ${fixedSession || '(最近活跃会话)'}`)
}

export { apply, inject, name }
