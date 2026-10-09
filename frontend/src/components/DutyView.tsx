import { useCallback, useEffect, useState } from "react";
import { dutyHumanQueue, dutyOverview, dutyStats, dutyTopology, type ThroughputRepoWindow } from "../api";
import type { DutyOverview, DutyRequest, DutyStats, DutyTopology, HumanQueue } from "../api";

/** 值守总览——业务级协同的可视化：拓扑 + 心跳 + 统计图表（配色随全站深色主题）。 */

const STATE_META: Record<string, { color: string; label: string }> = {
  working: { color: "#f5a623", label: "工作中" },
  idle: { color: "#3ddc84", label: "空闲/正常" },
  warn: { color: "#ffd166", label: "降级" },
  error: { color: "#ff6b6b", label: "异常" },
  unknown: { color: "#8a93a3", label: "未知" },
};

function fmtAge(sec?: number): string {
  if (sec == null) return "—";
  if (sec < 90) return `${Math.round(sec)}s`;
  if (sec < 5400) return `${Math.round(sec / 60)}m`;
  if (sec < 172800) return `${(sec / 3600).toFixed(1)}h`;
  return `${(sec / 86400).toFixed(1)}d`;
}

/** 按显示宽度截断：CJK 记 2 单位、其余 1 单位。 */
function fit(text: string, budget: number): string {
  let w = 0, out = "";
  for (const ch of text || "") {
    const cw = /[\u3000-\u9fff\uff00-\uffef]/.test(ch) ? 2 : 1;
    if (w + cw > budget) return out + "…";
    w += cw; out += ch;
  }
  return out;
}

/* ---------------- 拓扑（固定坐标，零重叠） ----------------
   行1 摄入   y=20   : external → github → shadow
   行2 执行   y≈96-170: improve/sbx → reaper → accept → ext_check
   行3 值守   y=300  : bwatch → duty ← ctrl
   行4 底座   y=380  : core（在 duty 右下）
   回流边：ext_check ↓ 绕行 y=250 ← github（验收通过→关单）
--------------------------------------------------------- */

const NODE_W = 172, NODE_H = 58;
const POS: Record<string, { x: number; y: number }> = {
  external: { x: 16, y: 20 },
  github: { x: 216, y: 20 },
  shadow: { x: 416, y: 20 },
  improve: { x: 616, y: 96 },
  sbx: { x: 616, y: 170 },
  reaper: { x: 816, y: 120 },
  accept: { x: 1016, y: 120 },
  external_check: { x: 1216, y: 120 },
  bwatch: { x: 216, y: 300 },
  duty: { x: 616, y: 300 },
  ctrl: { x: 1016, y: 300 },
  core: { x: 1016, y: 380 },
};
const CANVAS = { w: 1420, h: 460 };

function Edge({ d, label, lx, ly, dash }: { d: string; label: string; lx: number; ly: number; dash?: boolean }) {
  const tw = (label || "").length * 7 + 10;
  return (
    <g>
      <path d={d} fill="none" stroke="#3a4150" strokeWidth={1.6}
        strokeDasharray={dash ? "6 5" : undefined} markerEnd="url(#duty-arrow)" />
      {label && (
        <>
          <rect x={lx - tw / 2} y={ly - 9} width={tw} height={16} rx={4} fill="var(--bg)" opacity={0.92} />
          <text x={lx} y={ly + 2} textAnchor="middle" className="duty-edge-label">{label}</text>
        </>
      )}
    </g>
  );
}

function Topology({ topo }: { topo: DutyTopology | null }) {
  if (!topo) return <div className="muted">拓扑加载中…</div>;
  const R = (id: string) => { const p = POS[id]; return { x: p.x + NODE_W, y: p.y + NODE_H / 2, cy: p.y + NODE_H / 2, cx: p.x + NODE_W / 2, top: p.y, bottom: p.y + NODE_H }; };
  const L = (id: string) => { const p = POS[id]; return { x: p.x, y: p.y + NODE_H / 2, cx: p.x + NODE_W / 2, top: p.y, bottom: p.y + NODE_H }; };

  const edges: { d: string; label: string; lx: number; ly: number; dash?: boolean }[] = [
    // 行1：窄走廊（框距 28px）→ 下绕 U 形弧，标签贴弧底（经几何求解无碰撞）
    { d: `M 102 78 C 102 102, 302 102, 302 78`, label: "提 issue", lx: 202, ly: 106 },
    { d: `M 302 78 C 302 102, 502 102, 502 78`, label: "轮询扫单", lx: 402, ly: 106 },
    // shadow → improve / sbx
    { d: `M ${R("shadow").cx} ${R("shadow").bottom} C ${R("shadow").cx} 92, ${L("improve").x} 74, ${L("improve").x} ${L("improve").y}`, label: "派发·主版", lx: 640, ly: 82 },
    { d: `M ${R("shadow").cx} ${R("shadow").bottom} C ${R("shadow").cx} 168, ${L("sbx").x} 158, ${L("sbx").x} ${L("sbx").y}`, label: "派发·灰度", lx: 560, ly: 176 },
    // improve / sbx → reaper
    { d: `M ${R("improve").x} ${R("improve").y} C 800 125, 800 140, ${L("reaper").x - 6} ${L("reaper").y}`, label: "终态", lx: 806, ly: 103 },
    { d: `M ${R("sbx").x} ${R("sbx").y} C 800 199, 800 180, ${L("reaper").x - 6} ${L("reaper").y + 12}`, label: "", lx: 0, ly: 0 },
    // reaper → accept → ext_check（标签置于框带上方净空）
    { d: `M ${R("reaper").x} ${R("reaper").y} L ${L("accept").x - 6} ${L("accept").y}`, label: "done 回评", lx: 1002, ly: 105 },
    { d: `M ${R("accept").x} ${R("accept").y} L ${L("external_check").x - 6} ${L("external_check").y}`, label: "请验收", lx: 1202, ly: 105 },
    // 回流：ext_check ↓ y=250 ← github ↑
    {
      d: `M ${R("external_check").cx} ${R("external_check").bottom} L ${R("external_check").cx} 250 L ${L("github").cx} 250 L ${L("github").cx} ${L("github").bottom + 6}`,
      label: "验收通过 → 关单", lx: 760, ly: 250, dash: true,
    },
    // 值守层
    { d: `M ${R("bwatch").x} ${R("bwatch").y} L ${L("duty").x - 6} ${L("duty").y}`, label: "轮报 / handoff", lx: 510, ly: 320 },
    { d: `M ${L("ctrl").x} ${L("ctrl").y} L ${R("duty").x + 6} ${R("duty").y}`, label: "看门狗", lx: 900, ly: 320 },
    { d: `M ${R("duty").x} ${R("duty").y + 18} C 940 372, 980 380, ${L("core").x - 6} ${L("core").y}`, label: "状态权威", lx: 908, ly: 396 },
  ];

  return (
    <div className="duty-topo-wrap">
      <svg viewBox={`0 0 ${CANVAS.w} ${CANVAS.h}`} className="duty-topo" role="img" aria-label="值守工作流拓扑">
        <defs>
          <marker id="duty-arrow" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto">
            <path d="M0,0 L7,3 L0,6 Z" fill="#5b6474" />
          </marker>
        </defs>
        {edges.map((e, i) => <Edge key={i} {...e} />)}
        {topo.nodes.map((n) => {
          const p = POS[n.id];
          if (!p) return null;
          const meta = STATE_META[n.state] || STATE_META.unknown;
          return (
            <g key={n.id} transform={`translate(${p.x},${p.y})`}>
              <title>{`${n.label}｜${meta.label}｜${n.detail}`}</title>
              <rect width={NODE_W} height={NODE_H} rx={9} fill="var(--panel-2)" stroke={meta.color} strokeWidth={1.6} />
              <circle cx={NODE_W - 14} cy={14} r={5} fill={meta.color} />
              <text x={12} y={24} className="duty-node-title">{fit(n.label, 22)}</text>
              <text x={12} y={43} className="duty-node-detail">{fit(n.detail, 38)}</text>
            </g>
          );
        })}
      </svg>
      <div className="duty-legend">
        {Object.entries(STATE_META).map(([k, v]) => (
          <span key={k}><i style={{ background: v.color }} />{v.label}</span>
        ))}
      </div>
    </div>
  );
}

/* ---------------- 图表 ---------------- */

function StackedBar({ by }: { by: Record<string, number> }) {
  const colors: Record<string, string> = { cancelled: "#ff6b6b", completed: "#3ddc84", running: "#4f8cff" };
  const entries = Object.entries(by).filter(([, v]) => v > 0);
  const total = entries.reduce((s, [, v]) => s + v, 0) || 1;
  return (
    <div>
      <div className="duty-stackbar">
        {entries.map(([k, v]) => (
          <div key={k} style={{ width: `${(v / total) * 100}%`, background: colors[k] || "#8a93a3" }} title={`${k}: ${v}h`} />
        ))}
      </div>
      <div className="duty-legend">
        {entries.map(([k, v]) => (
          <span key={k}><i style={{ background: colors[k] || "#8a93a3" }} />{k} {v}h</span>
        ))}
      </div>
    </div>
  );
}

/** 提报 vs 关闭双柱 + 净积压趋势线（窗口累计新建−累计关闭）。
 *  数据全部来自 pipeline_stats --json 的透传，无需额外请求。 */
function ThroughputChart({ created, closed }: { created: Record<string, number>; closed: Record<string, number> }) {
  const days = Array.from(new Set([...Object.keys(created || {}), ...Object.keys(closed || {})])).sort();
  if (!days.length) return <div className="muted">暂无数据</div>;
  const g = (m: Record<string, number>, d: string) => m?.[d] || 0;
  const max = Math.max(...days.map((d) => Math.max(g(created, d), g(closed, d))), 1);
  let c = 0, cl = 0;
  const backlog = days.map((d) => { c += g(created, d); cl += g(closed, d); return c - cl; });
  const bMin = Math.min(0, ...backlog), bMax = Math.max(1, ...backlog);
  const PLOT_H = 120;
  const y = (v: number) => PLOT_H - 6 - ((v - bMin) / (bMax - bMin)) * (PLOT_H - 12);
  const pts = backlog.map((v, i) => `${((i + 0.5) / days.length) * 100},${y(v)}`).join(" ");
  const totC = days.reduce((s, d) => s + g(created, d), 0);
  const totCl = days.reduce((s, d) => s + g(closed, d), 0);
  return (
    <div>
      <div className="duty-tp-vals">
        {days.map((d) => (
          <div key={d} className="val">{g(created, d)}/{g(closed, d)}</div>
        ))}
      </div>
      <div className="duty-tp-plot">
        {days.map((d) => (
          <div key={d} className="tp-pair" title={`${d}：新建 ${g(created, d)} · 关闭 ${g(closed, d)}`}>
            <div className="bar cr" style={{ height: `${Math.max(2, (g(created, d) / max) * 100)}%` }} />
            <div className="bar cl" style={{ height: `${Math.max(2, (g(closed, d) / max) * 100)}%` }} />
          </div>
        ))}
        <svg className="tp-line" viewBox={`0 0 100 ${PLOT_H}`} preserveAspectRatio="none" aria-hidden>
          <polyline points={pts} fill="none" stroke="#f5a623" strokeWidth={2} vectorEffect="non-scaling-stroke" />
        </svg>
      </div>
      <div className="duty-tp-lbls">
        {days.map((d) => <div key={d} className="lbl">{d.slice(5)}</div>)}
      </div>
      <div className="duty-legend">
        <span><i style={{ background: "#4f8cff" }} />新建 {totC}</span>
        <span><i style={{ background: "#3ddc84" }} />关闭 {totCl}</span>
        <span><i style={{ background: "#f5a623" }} />净积压（窗口累计差）期末 {backlog[backlog.length - 1] ?? 0}</span>
      </div>
    </div>
  );
}

/** keeper run 按日终态堆叠柱（done/failed/engine_error/退避…）+ 值守落地数。 */
const RUN_COLORS: Record<string, string> = {
  done: "#3ddc84", failed: "#ff6b6b", engine_error: "#b07cc6", "retry-later": "#f5a623",
};

function RunDaily({ runsByDay, landByDay }: {
  runsByDay: Record<string, Record<string, number>>;
  landByDay: Record<string, number>;
}) {
  const days = Array.from(new Set([...Object.keys(runsByDay || {}), ...Object.keys(landByDay || {})])).sort();
  if (!days.length) return <div className="muted">暂无数据</div>;
  const totalOf = (d: string) =>
    Object.values(runsByDay?.[d] || {}).reduce((a, b) => a + b, 0);
  const max = Math.max(...days.map(totalOf), 1);
  return (
    <div>
      <div className="duty-runbars">
        {days.map((d) => {
          const entries = Object.entries(runsByDay?.[d] || {}).filter(([, v]) => v > 0);
          const total = totalOf(d);
          const land = landByDay?.[d] || 0;
          const tip = `${d}：${entries.map(([k, v]) => `${k} ${v}`).join(" · ") || "无 run"}${land ? ` · 值守落地 ${land}` : ""}`;
          return (
            <div key={d} className="run-day" title={tip}>
              <div className="val">{total || "·"}</div>
              <div className="stack" style={{ height: 110 }}>
                {entries.map(([k, v]) => (
                  <div key={k} className="seg" style={{ height: `${(v / max) * 100}%`, background: RUN_COLORS[k] || "#8a93a3" }} />
                ))}
              </div>
              <div className="lbl">{d.slice(5)}</div>
              <div className="sub">{land ? `落地 ${land}` : " "}</div>
            </div>
          );
        })}
      </div>
      <div className="duty-legend">
        {Object.entries(RUN_COLORS).map(([k, c]) => (
          <span key={k}><i style={{ background: c }} />{k === "done" ? "done（管线完成）" : k === "retry-later" ? "retry-later（退避）" : k}</span>
        ))}
        <span><i style={{ background: "#8a93a3" }} />其他终态</span>
      </div>
    </div>
  );
}

/** 按仓对比：在册 open / 窗口新建 vs 关闭（横条）/ 全期关闭率。 */
function RepoCompare({ byRepo }: { byRepo: Record<string, ThroughputRepoWindow> }) {
  const rows = Object.entries(byRepo || {})
    .sort((a, b) => b[1].open - a[1].open || b[1].created - a[1].created);
  if (!rows.length) return <div className="muted">暂无数据</div>;
  const max = Math.max(...rows.flatMap(([, r]) => [r.created, r.closed]), 1);
  return (
    <div className="duty-repos">
      {rows.map(([name, r]) => (
        <div key={name} className="repo-row" title={`${name}｜在册 ${r.open} · 窗口新建 ${r.created} · 窗口关闭 ${r.closed}`}>
          <span className="name">{name.split("/")[1] || name}</span>
          <span className="open">在册 {r.open}</span>
          <span className="bars">
            <i className="track"><em className="cr" style={{ width: `${(r.created / max) * 100}%` }} /></i>
            <b className="num">新 {r.created}</b>
            <i className="track"><em className="cl" style={{ width: `${(r.closed / max) * 100}%` }} /></i>
            <b className="num">关 {r.closed}</b>
          </span>
          <span className="rate">{r.close_rate != null ? `${Math.round(r.close_rate * 100)}%` : "—"}</span>
        </div>
      ))}
    </div>
  );
}

/* ---------------- 主组件 ---------------- */

export function DutyView() {
  const [ov, setOv] = useState<DutyOverview | null>(null);
  const [st, setSt] = useState<DutyStats | null>(null);
  const [topo, setTopo] = useState<DutyTopology | null>(null);
  const [hq, setHq] = useState<HumanQueue | null>(null);
  const [err, setErr] = useState("");

  const refresh = useCallback(() => {
    dutyOverview().then(setOv).catch((e) => setErr(String(e)));
    dutyStats().then(setSt).catch((e) => setErr(String(e)));
    dutyTopology().then(setTopo).catch((e) => setErr(String(e)));
    dutyHumanQueue().then(setHq).catch((e) => setErr(String(e)));
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30_000);
    return () => clearInterval(t);
  }, [refresh]);

  const inflight = ov?.inflight || [];
  const vm = ov?.vm || {};
  const shadow = ov?.shadow || {};
  const sbxLive = st?.sandbox_live?.instances || [];
  const sbxBy = st?.sandbox?.by_status || {};
  const tp = st?.throughput?.data || {};
  const tputC = tp.created_by_day || {};
  const tputX = tp.closed_by_day || {};
  const runsByDay = tp.runs_by_day || {};
  const landByDay = tp.landings_by_day || {};
  const byRepoWin = tp.by_repo_window || {};
  const openTotal = tp.open_total;
  const ttcMedian = tp.ttc_median_secs;
  const ttcP90 = tp.ttc_p90_secs;
  const backoffList = tp.backoff || [];
  const blockedList = tp.blocked || [];

  // B 班：后端已结构化为 {time, kind, brief}（避免原始日志串）
  const bTime = ov?.b_shift?.time || "—";
  const bKind = ov?.b_shift?.kind || "";
  const bBrief = ov?.b_shift?.brief || "—";

  const cAge = ov?.controller?.finished_at
    ? fmtAge(Math.round((Date.now() - new Date(ov.controller.finished_at).getTime()) / 1000))
    : "—";

  return (
    <div className="duty-view">
      {err && <div className="banner error">数据源错误：{err}</div>}

      <div className="duty-head">
        <h2>值守总览</h2>
        <span className="muted">roster gen {ov?.roster?.generation ?? "—"} · 刷新于 {ov?.ts?.slice(11, 19) || "…"}</span>
        <button onClick={refresh}>手动刷新</button>
      </div>

      <div className="duty-cards">
        <div className="duty-card">
          <div className="k">A 班 · issue-accept</div>
          <div className="v">{fmtAge(ov?.a_shift?.age_sec)}</div>
          <div className="s">轮{ov?.a_shift?.round ?? "—"} · {ov?.a_shift?.status || "—"}</div>
        </div>
        <div className="duty-card">
          <div className="k">B 班 · keeper-watch</div>
          <div className="v">{bTime}</div>
          <div className="s">{bKind ? `${bKind}轮 · ` : ""}{bBrief}</div>
        </div>
        <div className="duty-card">
          <div className="k">主控 · ctrl-watch</div>
          <div className="v">{cAge}</div>
          <div className="s">轮{ov?.controller?.round ?? "—"} · {ov?.controller?.status || "—"}</div>
        </div>
        <div className="duty-card">
          <div className="k">在途 / 闸（主版/灰度）</div>
          <div className="v">
            {inflight.length} / 4
            <small className="eng-split">
              主 {inflight.filter((i) => i.engine !== "sbx").length} · 沙 {inflight.filter((i) => i.engine === "sbx").length}
            </small>
          </div>
          <div className="s duty-list">
            {inflight.length
              ? inflight.map((i) => (
                  <span key={`${i.repo}#${i.issue}`}>
                    {i.repo.replace("jeffkit/", "")}#{i.issue}
                    {i.engine && <b className={`eng ${i.engine === "sbx" ? "eng-sbx" : "eng-main"}`}>{i.engine === "sbx" ? "沙" : "主"}</b>}
                    <em>{Math.round(i.minutes)}m</em>
                  </span>
                ))
              : "—"}
          </div>
        </div>
        <div className="duty-card">
          <div className="k">远端控制面</div>
          <div className="v">{vm.sched === "active" && vm.keeper === "active" ? "正常" : "异常"}</div>
          <div className="s">sched {vm.sched || "—"} · keeper {vm.keeper || "—"} · 盘 {vm.disk || "—"} · DLQ {vm.dlq ?? "—"}</div>
        </div>
        <div className="duty-card">
          <div className="k">shadow 派发对账</div>
          <div className="v">{shadow.generated_at ? shadow.generated_at.slice(11, 16) : "—"}</div>
          <div className="s">budget {shadow.budget_left ?? "—"} · 可派 {shadow.would_dispatch ?? "—"}</div>
        </div>
        <div className="duty-card">
          <div className="k">在册 / 受阻</div>
          <div className="v">{openTotal ?? "—"}</div>
          <div className="s duty-list">
            {`blocked ${blockedList.length} · 退避 ${backoffList.length}`}
            {blockedList.length ? <em>{blockedList.join(" ")}</em> : ""}
          </div>
        </div>
        <div className="duty-card">
          <div className="k">关闭时效（中位）</div>
          <div className="v">{ttcMedian != null ? fmtAge(Math.round(ttcMedian)) : "—"}</div>
          <div className="s">P90 {ttcP90 != null ? fmtAge(Math.round(ttcP90)) : "—"} · 提报→关闭</div>
        </div>
      </div>

      <section className="duty-section">
        <h3>工作流拓扑（状态实时）</h3>
        <Topology topo={topo} />
      </section>

      <section className="duty-section">
        <h3>
          工单收件箱（值守 Agent 的决策队列）
          <span className="muted" style={{ fontWeight: 400, fontSize: 12, marginLeft: 8 }}>
            待处置 {hq?.requests?.active ?? 0} / 共 {hq?.requests?.total ?? 0}
            {hq?.requests?.counts ? ` · ${Object.entries(hq.requests.counts).map(([k, v]) => `${k} ${v}`).join(" / ")}` : ""}
          </span>
        </h3>
        <div className="duty-reqs">
          {(hq?.requests?.items || []).length === 0 && <div className="muted">暂无工单</div>}
          {(hq?.requests?.items || []).map((r: DutyRequest) => (
            <div key={r.id} className={`duty-req st-${r.status}`}>
              <div className="row1">
                <span className={`badge b-${r.status}`}>{r.status}</span>
                <span className="src">{r.from_flow}</span>
                <span className="sev">{r.severity}</span>
                <span className="t">{fit(r.title, 78)}</span>
                <span className="age">{r.created_at}</span>
              </div>
              {(r.action || r.rationale) && (
                <div className="dec">
                  ▸ 值守决定{r.by ? `（${r.by}）` : ""}：<b>{r.action || "—"}</b>
                  {r.rationale ? ` — ${fit(r.rationale, 110)}` : ""}
                </div>
              )}
              {r.human_reply && <div className="reply">▸ 人回复：{fit(r.human_reply, 110)}</div>}
            </div>
          ))}
        </div>
      </section>

      <section className="duty-section">
        <h3>等人工队列（needs-human 标签 + HITL 推送）</h3>
        <div className="duty-human">
          <div className="duty-human-col">
            <div className="duty-human-title">
              待人工处理 {hq?.needs_human?.count ?? 0} 条
              {hq?.needs_human?.error ? ` · ${hq.needs_human.error}` : ""}
            </div>
            {(hq?.needs_human?.items || []).length === 0 && <div className="muted">队列为空 ✓</div>}
            {(hq?.needs_human?.items || []).map((i: HumanQueue["needs_human"]["items"][number]) => (
              <a key={`${i.repo}#${i.number}`} className="duty-human-row" href={i.url} target="_blank" rel="noreferrer">
                <span className="repo">{i.repo.replace("jeffkit/", "")}#{i.number}</span>
                <span className="title">{fit(i.title, 46)}</span>
                <span className="age">{i.age_h != null ? `${i.age_h}h` : "—"}</span>
              </a>
            ))}
          </div>
          <div className="duty-human-col">
            <div className="duty-human-title">
              HITL 推送最近 {hq?.hitl_recent?.count ?? 0} 条
              {(hq?.hil_pending?.items || []).length > 0 ? ` · 待回复 ${hq!.hil_pending.items.length}` : ""}
            </div>
            {(hq?.hitl_recent?.items || []).length === 0 && <div className="muted">暂无推送</div>}
            {(hq?.hitl_recent?.items || []).map((h: HumanQueue["hitl_recent"]["items"][number], k: number) => (
              <div key={k} className="duty-human-row">
                <span className="age">{h.ts}</span>
                <span className="title">{fit(h.title, 40)}</span>
                <span className={`st-${h.status === "replied" ? "done" : h.status === "sent" ? "mid" : "early"}`}>{h.status}</span>
              </div>
            ))}
            {(hq?.hil_pending?.items || []).map((s2: HumanQueue["hil_pending"]["items"][number]) => (
              <div key={s2.short_id} className="duty-human-row">
                <span className="age">⏳{s2.left_min != null ? `${s2.left_min}m` : "?"}</span>
                <span className="title">{fit(s2.message, 40)}</span>
                <span className="st-mid">等回复</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="duty-section">
        <h3>沙箱用量（24h · 实例小时，按终态）</h3>
        <StackedBar by={sbxBy} />
        <div className="muted duty-note">
          活实例：{sbxLive.length ? sbxLive.map((i) => `${i.id.slice(0, 12)}（${i.age_h}h）`).join(" · ") : "—"}
          {st?.sandbox_live?.error ? ` · ${st.sandbox_live.error}` : ""}
        </div>
      </section>

      <section className="duty-section">
        <h3>提报 vs 关闭（近 7 天 · 全仓 issue，柱上数字为 新建/关闭）</h3>
        <ThroughputChart created={tputC} closed={tputX} />
      </section>

      <section className="duty-section">
        <h3>keeper 按日产出（run 终态 · 值守落地）</h3>
        <RunDaily runsByDay={runsByDay} landByDay={landByDay} />
      </section>

      <section className="duty-section">
        <h3>按仓对比（在册 / 窗口新建 vs 关闭 / 全期关闭率）</h3>
        <RepoCompare byRepo={byRepoWin} />
      </section>
    </div>
  );
}
