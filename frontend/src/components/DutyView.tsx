import { useCallback, useEffect, useState } from "react";
import { dutyOverview, dutyStats, dutyTopology } from "../api";
import type { DutyOverview, DutyStats, DutyTopology } from "../api";

/** 值守总览——业务级协同的可视化：拓扑 + 心跳 + 统计图表（配色随全站深色主题）。 */

const STATE_META: Record<string, { color: string; label: string }> = {
  healthy: { color: "#3ddc84", label: "健康" },
  idle: { color: "#8a93a3", label: "空闲" },
  degraded: { color: "#f5a623", label: "降级" },
  stalled: { color: "#ff6b6b", label: "停摆" },
};

function fmtAge(sec?: number): string {
  if (sec == null) return "—";
  if (sec < 90) return `${Math.round(sec)}s`;
  if (sec < 5400) return `${Math.round(sec / 60)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
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
          const meta = STATE_META[n.state] || STATE_META.idle;
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

function DayBars({ by }: { by: Record<string, number> }) {
  const entries = Object.entries(by).sort();
  const max = Math.max(...entries.map(([, v]) => v), 1);
  if (!entries.length) return <div className="muted">暂无数据</div>;
  return (
    <div className="duty-daybars">
      {entries.map(([d, v]) => (
        <div key={d} className="duty-daybar" title={`${d}: 提报 ${v}`}>
          <div className="val">{v}</div>
          <div className="bar" style={{ height: `${Math.max(4, (v / max) * 100)}%` }} />
          <div className="lbl">{d.slice(5)}</div>
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
  const [err, setErr] = useState("");

  const refresh = useCallback(() => {
    dutyOverview().then(setOv).catch((e) => setErr(String(e)));
    dutyStats().then(setSt).catch((e) => setErr(String(e)));
    dutyTopology().then(setTopo).catch((e) => setErr(String(e)));
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
  const tput = (st?.throughput?.data?.created_by_day as Record<string, number>) || {};

  // B 班轮报行 → 紧凑摘要："10-09 08:59 B-flow 轮次=keeper-watch（值守）落地=…"
  const bLine = ov?.b_shift?.last_line || "";
  const bTime = (bLine.match(/(\d{2}:\d{2})\s+B-flow/) || [])[1] || "—";
  const bKind = (bLine.match(/轮次=keeper-watch（([^）]+)）/) || [])[1] || "";
  const bBrief = bLine.includes("简报发 #3") ? "简报已发 #3" : fit(bLine.replace(/^\S+\s+\S+\s+\S+\s+/, ""), 40);

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
          <div className="k">在途 / 闸</div>
          <div className="v">{inflight.length} / 4</div>
          <div className="s duty-list">
            {inflight.length
              ? inflight.map((i) => <span key={`${i.repo}#${i.issue}`}>{i.repo.replace("jeffkit/", "")}#{i.issue} <em>{Math.round(i.minutes)}m</em></span>)
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
      </div>

      <section className="duty-section">
        <h3>工作流拓扑（状态实时）</h3>
        <Topology topo={topo} />
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
        <h3>提报吞吐（近 7 天 · 新建 issue 数）</h3>
        <DayBars by={tput} />
      </section>
    </div>
  );
}
