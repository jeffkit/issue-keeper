import { useCallback, useEffect, useState } from "react";
import { dutyOverview, dutyStats, dutyTopology } from "../api";
import type { DutyOverview, DutyStats, DutyTopology } from "../api";

/** 值守总览——业务级协同的可视化：拓扑 + 心跳 + 统计图表。 */

const STATE_COLOR: Record<string, string> = {
  healthy: "#2e9e5b",
  idle: "#8a97a5",
  stalled: "#d9534f",
  degraded: "#e69500",
};

function fmtAge(sec?: number): string {
  if (sec == null) return "—";
  if (sec < 90) return `${Math.round(sec)}s`;
  if (sec < 5400) return `${Math.round(sec / 60)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}

/* ---------------- 拓扑图（固定布局 SVG） ---------------- */

const NODE_POS: Record<string, { x: number; y: number }> = {
  external: { x: 20, y: 40 },
  github: { x: 170, y: 40 },
  shadow: { x: 320, y: 40 },
  improve: { x: 470, y: 10 },
  sbx: { x: 470, y: 90 },
  reaper: { x: 620, y: 50 },
  accept: { x: 770, y: 50 },
  external_check: { x: 920, y: 50 },
  bwatch: { x: 470, y: 210 },
  ctrl: { x: 620, y: 210 },
  duty: { x: 770, y: 210 },
  core: { x: 920, y: 210 },
};

function Topology({ topo }: { topo: DutyTopology | null }) {
  if (!topo) return <div className="muted">拓扑加载中…</div>;
  const byId = Object.fromEntries(topo.nodes.map((n) => [n.id, n]));
  const W = 1090, H = 290;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="duty-topo">
      <defs>
        <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
          <path d="M0,0 L7,3 L0,6 Z" fill="#7a8a99" />
        </marker>
      </defs>
      {topo.edges.map((e, i) => {
        const a = NODE_POS[e.from], b = NODE_POS[e.to];
        if (!a || !b) return null;
        const x1 = a.x + 150, y1 = a.y + 27, x2 = b.x, y2 = b.y + 27;
        const mx = (x1 + x2) / 2;
        const d = `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
        return (
          <g key={i}>
            <path d={d} fill="none" stroke="#7a8a99" strokeWidth={1.4}
              strokeDasharray={e.dash ? "5 4" : undefined} markerEnd="url(#arrow)" />
            <text x={mx} y={(y1 + y2) / 2 - 6} textAnchor="middle" className="duty-edge-label">{e.label}</text>
          </g>
        );
      })}
      {topo.nodes.map((n) => {
        const p = NODE_POS[n.id];
        if (!p) return null;
        const color = STATE_COLOR[n.state] || "#8a97a5";
        const full = byId[n.id];
        return (
          <g key={n.id} transform={`translate(${p.x},${p.y})`}>
            <title>{full?.detail}</title>
            <rect width="150" height="54" rx="8" fill="#f7f9fb" stroke={color} strokeWidth={2} />
            <circle cx="138" cy="12" r="5" fill={color} />
            <text x="12" y="21" className="duty-node-title">{n.label}</text>
            <text x="12" y="40" className="duty-node-detail">{(full?.detail || "").slice(0, 26)}</text>
          </g>
        );
      })}
    </svg>
  );
}

/* ---------------- 小图表 ---------------- */

function StackedBar({ by, colors }: { by: Record<string, number>; colors: Record<string, string> }) {
  const entries = Object.entries(by).filter(([, v]) => v > 0);
  const total = entries.reduce((s, [, v]) => s + v, 0) || 1;
  return (
    <div>
      <div className="duty-stackbar">
        {entries.map(([k, v]) => (
          <div key={k} style={{ width: `${(v / total) * 100}%`, background: colors[k] || "#8a97a5" }}
            title={`${k}: ${v}h`} />
        ))}
      </div>
      <div className="duty-legend">
        {entries.map(([k, v]) => (
          <span key={k}><i style={{ background: colors[k] || "#8a97a5" }} />{k} {v}h</span>
        ))}
      </div>
    </div>
  );
}

function DayBars({ by }: { by: Record<string, number> }) {
  const entries = Object.entries(by).sort();
  const max = Math.max(...entries.map(([, v]) => v), 1);
  return (
    <div className="duty-daybars">
      {entries.map(([d, v]) => (
        <div key={d} className="duty-daybar" title={`${d}: 提报 ${v}`}>
          <div className="bar" style={{ height: `${(v / max) * 100}%` }} />
          <div className="lbl">{d.slice(5)}</div>
          <div className="val">{v}</div>
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

  const aAge = fmtAge(ov?.a_shift?.age_sec);
  const cAge = ov?.controller ? fmtAge(
    Math.round((Date.now() - new Date(ov.controller.finished_at || Date.now()).getTime()) / 1000)) : "—";
  const inflight = ov?.inflight || [];
  const vm = ov?.vm || {};
  const shadow = ov?.shadow || {};
  const sbxLive = st?.sandbox_live?.instances || [];
  const sbxBy = st?.sandbox?.by_status || {};
  const tput = st?.throughput?.data?.created_by_day || {};

  return (
    <div className="duty-view">
      {err && <div className="banner">数据源错误：{err}</div>}
      <div className="duty-head">
        <b>值守总览</b>
        <span className="muted">roster gen {ov?.roster?.generation ?? "—"} · 刷新于 {ov?.ts?.slice(11, 19) || "…"}</span>
        <button className="tab" onClick={refresh}>手动刷新</button>
      </div>

      {/* 心跳卡 */}
      <div className="duty-cards">
        <div className="duty-card">
          <div className="k">A 班 · issue-accept</div>
          <div className="v">{aAge}</div>
          <div className="s">轮{ov?.a_shift?.round ?? "—"} · {ov?.a_shift?.status || "—"}</div>
        </div>
        <div className="duty-card">
          <div className="k">B 班 · keeper-watch</div>
          <div className="v">{ov?.b_shift?.last_line ? ov.b_shift.last_line.slice(0, 5) : "—"}</div>
          <div className="s">{(ov?.b_shift?.last_line || "").slice(5, 80)}</div>
        </div>
        <div className="duty-card">
          <div className="k">主控 · ctrl-watch</div>
          <div className="v">{cAge}</div>
          <div className="s">轮{ov?.controller?.round ?? "—"} · {ov?.controller?.status || "—"}</div>
        </div>
        <div className="duty-card">
          <div className="k">在途 / 闸</div>
          <div className="v">{inflight.length} / 4</div>
          <div className="s">{inflight.map((i) => `${i.repo.replace("jeffkit/", "")}#${i.issue}`).join(" · ") || "—"}</div>
        </div>
        <div className="duty-card">
          <div className="k">远端控制面</div>
          <div className="v">{vm.sched === "active" && vm.keeper === "active" ? "✓" : "⚠"}</div>
          <div className="s">sched {vm.sched || "—"} · keeper {vm.keeper || "—"} · 盘 {vm.disk || "—"} · DLQ {vm.dlq ?? "—"}</div>
        </div>
        <div className="duty-card">
          <div className="k">shadow 派发对账</div>
          <div className="v">{shadow.generated_at ? shadow.generated_at.slice(11, 16) : "—"}</div>
          <div className="s">budget={shadow.budget_left ?? "—"} · 可派 {shadow.would_dispatch ?? "—"}</div>
        </div>
      </div>

      {/* 拓扑 */}
      <div className="duty-section">
        <div className="duty-section-title">工作流拓扑（状态实时）</div>
        <Topology topo={topo} />
      </div>

      {/* 图表 */}
      <div className="duty-section">
        <div className="duty-section-title">沙箱用量（24h · 实例小时，按终态）</div>
        <StackedBar by={sbxBy} colors={{ cancelled: "#d9534f", completed: "#2e9e5b", running: "#6ea8fe" }} />
        <div className="muted" style={{ marginTop: 6 }}>
          活实例：{sbxLive.length ? sbxLive.map((i) => `${i.id}（${i.age_h}h）`).join(" · ") : "—"}
          {st?.sandbox_live?.error ? ` · ${st.sandbox_live.error}` : ""}
        </div>
      </div>

      <div className="duty-section">
        <div className="duty-section-title">提报吞吐（近 7 天 · 新建 issue 数）</div>
        <DayBars by={tput} />
      </div>
    </div>
  );
}
