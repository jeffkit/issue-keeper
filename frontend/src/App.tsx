import { useEffect, useMemo, useState } from "react";
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";

import { listIssues, listProjects, moveIssue } from "./api";
import { STATUS_ORDER, type Issue, type Kind, type Project, type Status } from "./types";
import { Board } from "./components/Board";
import { IssueDetail } from "./components/IssueDetail";
import { CreateIssueModal } from "./components/CreateIssueModal";

export default function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<string>("");
  const [kind, setKind] = useState<Kind>("issue");
  const [issues, setIssues] = useState<Issue[]>([]);
  const [selected, setSelected] = useState<Issue | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>("");
  const [activeId, setActiveId] = useState<number | null>(null);
  const [actorName, setActorName] = useState<string>(() => localStorage.getItem("ik_actor") || "alice");
  const [actorType, setActorType] = useState<"human" | "agent">(
    () => (localStorage.getItem("ik_actor_type") as "human" | "agent") || "human",
  );

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
  );

  useEffect(() => {
    listProjects().then((ps) => {
      setProjects(ps);
      if (!project && ps.length > 0) setProject(ps[0].project);
    }).catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    if (!project) return;
    setLoading(true);
    setError("");
    listIssues(project, kind)
      .then(setIssues)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [project, kind]);

  // 当前身份变化时持久化
  useEffect(() => { localStorage.setItem("ik_actor", actorName); }, [actorName]);
  useEffect(() => { localStorage.setItem("ik_actor_type", actorType); }, [actorType]);

  const byStatus = useMemo(() => {
    const m: Record<Status, Issue[]> = {
      inbox: [], todo: [], doing: [], review: [], done: [], closed: [],
    };
    for (const it of issues) m[it.status]?.push(it);
    return m;
  }, [issues]);

  const activeIssue = useMemo(
    () => issues.find((i) => i.number === activeId) ?? null,
    [activeId, issues],
  );

  function refresh() {
    if (!project) return;
    listIssues(project, kind).then(setIssues).catch((e) => setError(String(e)));
  }

  function onDragStart(e: DragStartEvent) {
    setActiveId(Number(e.active.id));
  }

  async function onDragEnd(e: DragEndEvent) {
    setActiveId(null);
    const { active, over } = e;
    if (!over) return;
    const num = Number(active.id);
    const toStatus = String(over.id) as Status;
    const it = issues.find((i) => i.number === num);
    if (!it || it.status === toStatus) return;
    // 乐观更新
    setIssues((prev) => prev.map((x) => (x.number === num ? { ...x, status: toStatus } : x)));
    try {
      await moveIssue(project, num, {
        to_status: toStatus,
        actor: actorName,
        actor_type: actorType,
      }, kind);
    } catch (err) {
      setError(String(err));
      refresh();
    }
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">▦</span> issue-keeper
        </div>
        <div className="controls">
          <select value={project} onChange={(e) => setProject(e.target.value)} disabled={!projects.length}>
            {projects.length === 0 && <option value="">（无项目）</option>}
            {projects.map((p) => (
              <option key={p.project} value={p.project}>
                {p.project}（{p.open}/{p.total}）
              </option>
            ))}
          </select>
          <select value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
            <option value="issue">issue</option>
            <option value="pr">PR</option>
          </select>
          <button className="primary" onClick={() => setShowCreate(true)} disabled={!project}>
            + 新建
          </button>
          <div className="whoami">
            <input
              value={actorName}
              onChange={(e) => setActorName(e.target.value)}
              placeholder="当前身份"
              title="你以谁的身份操作（发评论/改状态）"
            />
            <select value={actorType} onChange={(e) => setActorType(e.target.value as "human" | "agent")}>
              <option value="human">human</option>
              <option value="agent">agent</option>
            </select>
          </div>
        </div>
      </header>

      {error && <div className="banner error">{error}</div>}
      {loading && <div className="banner">加载中…</div>}
      {!loading && project && issues.length === 0 && (
        <div className="banner">该项目暂无 {kind}，点「+ 新建」提一个。</div>
      )}

      <DndContext sensors={sensors} onDragStart={onDragStart} onDragEnd={onDragEnd}>
        <Board
          statusOrder={STATUS_ORDER}
          byStatus={byStatus}
          onSelect={setSelected}
          selectedNumber={selected?.number ?? null}
        />
        <DragOverlay>
          {activeIssue ? (
            <div className="card dragging">
              <div className="card-title">#{activeIssue.number} {activeIssue.title}</div>
              <div className="card-meta">{activeIssue.author}/{activeIssue.actor_type}</div>
            </div>
          ) : null}
        </DragOverlay>
      </DndContext>

      {selected && (
        <IssueDetail
          project={project}
          issue={selected}
          actorName={actorName}
          actorType={actorType}
          onClose={() => setSelected(null)}
          onChanged={() => { refresh(); /* 详情可能更新，重新拉选中项 */ }}
        />
      )}

      {showCreate && project && (
        <CreateIssueModal
          project={project}
          actorName={actorName}
          actorType={actorType}
          onClose={() => setShowCreate(false)}
          onCreated={() => { setShowCreate(false); refresh(); }}
        />
      )}
    </div>
  );
}
