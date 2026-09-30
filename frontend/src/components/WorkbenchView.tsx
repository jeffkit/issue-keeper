import { useEffect, useState } from "react";
import { workbench } from "../api";
import type { WorkbenchData, WorkbenchCard } from "../types";

const GROUPS: Array<{ key: string; label: string; cls: string }> = [
  { key: "needs-human", label: "需要你", cls: "wb-needs" },
  { key: "doing", label: "在途", cls: "wb-doing" },
  { key: "blocked", label: "受阻", cls: "wb-blocked" },
  { key: "queued", label: "排队", cls: "wb-queued" },
  { key: "settled", label: "已有结论", cls: "wb-settled" },
];

function fmtDur(sec?: number | null): string {
  if (sec == null) return "";
  if (sec < 90) return `${Math.round(sec)}s`;
  if (sec < 5400) return `${Math.round(sec / 60)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}

function Card({ c }: { c: WorkbenchCard }) {
  return (
    <div className={`wb-card ${c.stage === "needs-human" ? "wb-card-hot" : ""}`}>
      <div className="wb-card-title">
        <a href={c.url} target="_blank" rel="noreferrer" title={c.title}>
          #{c.issue} {c.title}
        </a>
      </div>
      <div className="wb-card-meta">
        <span className="muted">{c.repo.split("/")[1]}</span>
        {c.in_flight && <span className="chip st-mid">run 进行中{c.running_since ? ` · ${c.running_since.slice(11, 16)} 起` : ""}</span>}
        {c.reason && <span className="wb-reason">{c.reason}</span>}
        {c.last_run?.gate_failed && <span className="bad">门失败：{c.last_run.gate_failed}</span>}
        {c.last_run?.duration_secs ? <span className="muted">{fmtDur(c.last_run.duration_secs)}</span> : ""}
      </div>
    </div>
  );
}

export function WorkbenchView() {
  const [data, setData] = useState<WorkbenchData | null>(null);
  const [err, setErr] = useState("");
  const [updatedAt, setUpdatedAt] = useState<string>("");

  function refresh() {
    workbench(45).then((d) => {
      setData(d);
      setUpdatedAt(new Date().toLocaleTimeString());
      setErr("");
    }).catch((e) => setErr(String(e)));
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 60_000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="workbench">
      <div className="pl-controls">
        <span className="muted">
          {data ? `GitHub open issues：${Object.values(data.counts).reduce((a, b) => a + b, 0)} 张卡 · 其中 ${data.counts["needs-human"] || 0} 条需要你` : "加载中…"}
        </span>
        <span className="muted">更新于 {updatedAt}（每 60s 自动刷新）</span>
        <button className="mini" onClick={refresh}>刷新</button>
      </div>
      {err && <div className="banner error">{err}</div>}
      {data && Object.keys(data.errors || {}).length > 0 && (
        <div className="banner">
          部分仓拉取失败：{Object.entries(data.errors).map(([r, e]) => `${r}（${e}）`).join("；")}
        </div>
      )}
      <div className="wb-columns">
        {GROUPS.map(({ key, label, cls }) => {
          const cards = data?.groups?.[key] || [];
          return (
            <div key={key} className={`wb-col ${cls}`}>
              <div className="wb-col-head">{label} <span className="muted">{cards.length}</span></div>
              {cards.map((c) => <Card key={`${c.repo}-${c.issue}`} c={c} />)}
              {cards.length === 0 && <div className="wb-empty muted">空</div>}
            </div>
          );
        })}
      </div>
      <div className="muted wb-note">
        派生只读：GitHub 是 issue 的唯一权威（改状态/评论请点标题去 GitHub）；这里只叠加管线阶段——需要你/在途/受阻来自 run 状态，排队 = 提了但没跑过管线。
      </div>
    </div>
  );
}
