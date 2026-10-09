import { useEffect, useMemo, useState } from "react";
import { benchmarksList, pipelineRunDetail, pipelineRuns, pipelineSummary } from "../api";
import type { BenchmarkInfo, PipelineRunDetail, PipelineRunRow, PipelineSummary } from "../types";

const CONSOLE_BASE = (window as any).__IK_CONSOLE_URL__ || "";

function fmtDuration(sec?: number | null): string {
  if (sec == null) return "—";
  if (sec < 90) return `${Math.round(sec)}s`;
  if (sec < 5400) return `${Math.round(sec / 60)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}

function fmtTokens(n?: number | null): string {
  if (!n) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function StatusChip({ status }: { status?: string }) {
  const cls =
    status === "done" ? "st-done" :
    status === "partial" || status === "abort" || status === "engine_error" ? "st-bad" :
    status === "readonly" || status === "blocked" || status === "invalid" || status === "nochange" ? "st-early" :
    "st-mid";
  return <span className={`chip ${cls}`}>{status || "—"}</span>;
}

/** 段耗时堆叠条：investigate/plan/implement/review/门 等各占一色。 */
const SEG_COLORS: Record<string, string> = {
  investigate: "#6ea8fe", plan: "#9de1f5", implement: "#5cb85c", review: "#f0ad4e",
  fix_review: "#e69500", fix_test: "#d9534f", gate: "#8e44ad", retest: "#b07cc6",
  document: "#95a5a6", reply: "#c8c8c8", triage: "#ffd166", deps: "#cfcfcf",
};

function SegmentBar({ segments }: { segments: Record<string, number> }) {
  const entries = Object.entries(segments).filter(([, ms]) => ms > 0);
  const total = entries.reduce((s, [, ms]) => s + ms, 0);
  if (!total) return <span className="muted">—</span>;
  return (
    <div className="segbar" title={entries.map(([k, v]) => `${k}: ${(v / 1000).toFixed(0)}s`).join("\n")}>
      {entries.map(([k, ms]) => (
        <div key={k} className="seg" style={{ width: `${(ms / total) * 100}%`, background: SEG_COLORS[k] || "#aab" }} />
      ))}
    </div>
  );
}

function RunDetailDrawer({ executionId, onClose }: { executionId: string; onClose: () => void }) {
  const [detail, setDetail] = useState<PipelineRunDetail | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    pipelineRunDetail(executionId).then(setDetail).catch((e) => setErr(String(e)));
  }, [executionId]);
  return (
    <div className="drawer">
      <div className="drawer-head">
        <b>run {executionId.slice(0, 12)}…</b>
        {CONSOLE_BASE && (
          <a className="mini" href={`${CONSOLE_BASE}/executions/${executionId}`} target="_blank" rel="noreferrer">
            console 执行详情 ↗
          </a>
        )}
        <button className="mini" onClick={onClose}>关闭</button>
      </div>
      {err && <div className="banner error">{err}</div>}
      {detail && (
        <table className="nodes">
          <thead>
            <tr><th>节点</th><th>类型</th><th>状态</th><th>耗时</th><th>模型 / 结果</th></tr>
          </thead>
          <tbody>
            {(detail.nodes || []).map((n: any, i: number) => (
              <tr key={i} className={n.status === "error" ? "row-bad" : ""}>
                <td>{n.id}</td>
                <td>{n.type}</td>
                <td>{n.status}{n.timed_out ? "（超时）" : ""}</td>
                <td>{n.duration_ms != null ? `${(n.duration_ms / 1000).toFixed(1)}s` : "—"}</td>
                <td className="muted">
                  {n.model ? `${n.model} · ` : ""}
                  {n.type === "gate" ? `passed=${n.passed}${n.gate ? ` (${n.gate})` : ""}` : ""}
                  {n.tokens ? ` tokens=${(n.tokens.input || 0) + (n.tokens.output || 0)}` : ""}
                  {n.pushed != null ? ` pushed=${n.pushed}` : ""}
                  {n.parse_ok != null ? ` verdict=${n.verdict}` : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** 门失败 Top N 横条图：summary.gate_failures 已按门聚合。 */
function GateBars({ gates }: { gates: Record<string, number> }) {
  const entries = Object.entries(gates).sort((a, b) => b[1] - a[1]).slice(0, 6);
  const max = Math.max(...entries.map(([, v]) => v), 1);
  return (
    <div className="gate-bars">
      {entries.map(([g, v]) => (
        <div key={g} className="gate-bar" title={`${g}：窗口内失败 ${v} 次`}>
          <span className="g">{g}</span>
          <i className="track"><em style={{ width: `${(v / max) * 100}%` }} /></i>
          <span className="v">{v}</span>
        </div>
      ))}
    </div>
  );
}

export function PipelineView() {
  const [days, setDays] = useState(30);
  const [repo, setRepo] = useState("");
  const [summary, setSummary] = useState<PipelineSummary | null>(null);
  const [runs, setRuns] = useState<PipelineRunRow[]>([]);
  const [openRun, setOpenRun] = useState<string | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    pipelineSummary(days).then(setSummary).catch((e) => setErr(String(e)));
  }, [days]);

  useEffect(() => {
    pipelineRuns(days, repo).then(setRuns).catch((e) => setErr(String(e)));
  }, [days, repo]);

  const repos = useMemo(() => Object.entries(summary?.by_repo || {}), [summary]);

  return (
    <div className="pipeline">
      <div className="pl-controls">
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={7}>近 7 天</option>
          <option value={30}>近 30 天</option>
          <option value={90}>近 90 天</option>
        </select>
        <select value={repo} onChange={(e) => setRepo(e.target.value)}>
          <option value="">全部仓</option>
          {repos.map(([name]) => <option key={name} value={name}>{name}</option>)}
        </select>
        <span className="muted">
          {summary ? `${summary.total_runs} runs · 成功率 ${summary.success_rate ?? "—"} · p50 ${fmtDuration(summary.duration_secs?.p50)}` : "加载中…"}
        </span>
      </div>
      {err && <div className="banner error">{err}</div>}

      <div className="repo-cards">
        {repos.map(([name, s]) => (
          <div key={name} className={`repo-card${repo === name ? " active" : ""}`}
               onClick={() => setRepo(repo === name ? "" : name)}>
            <div className="rc-name">{name}</div>
            <div className="rc-stats">
              <span>{s.runs} runs</span>
              <span className={s.success_rate != null && s.success_rate < 0.5 ? "bad" : ""}>
                成功率 {s.success_rate != null ? `${Math.round(s.success_rate * 100)}%` : "—"}
              </span>
              <span>p50 {fmtDuration(s.duration_secs?.p50)}</span>
              <span>{fmtTokens(s.tokens)} tok</span>
            </div>
            {Object.keys(s.gate_failures || {}).length > 0 && (
              <div className="rc-gates bad">
                门失败：{Object.entries(s.gate_failures).map(([g, c]) => `${g}×${c}`).join("、")}
              </div>
            )}
            <div className="rc-statuses muted">
              {Object.entries(s.by_status || {}).map(([k, v]) => `${k}:${v}`).join(" · ")}
            </div>
          </div>
        ))}
      </div>

      {summary && Object.keys(summary.gate_failures || {}).length > 0 && (
        <div className="gate-fails">
          <h3>门失败 Top（窗口 {days} 天 · 多次失败的门优先排查）</h3>
          <GateBars gates={summary.gate_failures} />
        </div>
      )}

      <table className="runs">
        <thead>
          <tr>
            <th>时间</th><th>仓</th><th>issue</th><th>状态</th><th>耗时</th>
            <th>段耗时</th><th>门失败</th><th>tokens</th><th>flow</th><th></th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.execution_id} onClick={() => setOpenRun(r.execution_id)} className="run-row">
              <td className="muted">{(r.started || "").slice(5, 16)}</td>
              <td>{(r.repo || "").split("/")[1]}</td>
              <td>#{r.issue}</td>
              <td><StatusChip status={r.status} /></td>
              <td>{fmtDuration(r.duration_secs)}</td>
              <td><SegmentBar segments={r.segments || {}} /></td>
              <td>{r.gate_failed ? <span className="bad">{r.gate_failed}</span> : ""}</td>
              <td>{fmtTokens(r.tokens_total)}</td>
              <td className="muted">{r.flow_version || "—"}</td>
              <td>▸</td>
            </tr>
          ))}
          {runs.length === 0 && (
            <tr><td colSpan={10} className="muted">窗口内没有 run（metrics 自 v0.3 部署后开始积累）</td></tr>
          )}
        </tbody>
      </table>

      {openRun && <RunDetailDrawer executionId={openRun} onClose={() => setOpenRun(null)} />}

      <DatasetStrip />
    </div>
  );
}

function DatasetStrip() {
  const [datasets, setDatasets] = useState<BenchmarkInfo[]>([]);
  useEffect(() => {
    benchmarksList().then(setDatasets).catch(() => setDatasets([]));
  }, []);
  if (!datasets.length) return null;
  return (
    <div className="datasets">
      <b>benchmark 数据集</b>
      {datasets.map((d) => {
        const v = d.versions?.[String(d.latest_version)] || {};
        return (
          <span key={d.name} className="dataset-chip">
            {d.name} v{d.latest_version} · {v.count ?? "?"} cases
            {v.by_expected ? ` · ${Object.entries(v.by_expected).map(([k, c]) => `${k}:${c}`).join("/")}` : ""}
          </span>
        );
      })}
      <span className="muted">（构建/标注/评测：python -m issue_keeper benchmarks …）</span>
    </div>
  );
}
