import { useState } from "react";
import { createIssue } from "../api";
import type { ActorType, Kind } from "../types";

interface Props {
  project: string;
  actorName: string;
  actorType: ActorType;
  onClose: () => void;
  onCreated: () => void;
}

export function CreateIssueModal({ project, actorName, actorType, onClose, onCreated }: Props) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [kind, setKind] = useState<Kind>("issue");
  const [actorTypeLocal, setActorTypeLocal] = useState<ActorType>(actorType);
  const [author, setAuthor] = useState(actorName);
  const [labelsText, setLabelsText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function submit() {
    if (!title.trim()) return;
    setBusy(true);
    setErr("");
    try {
      const labels = labelsText.split(",").map((s) => s.trim()).filter(Boolean);
      await createIssue(project, {
        title, body,
        author: author || "anonymous",
        actor_type: actorTypeLocal,
        kind,
        labels,
      });
      onCreated();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="detail-head">
          <div className="detail-title">在 {project} 提新 issue</div>
          <button className="link" onClick={onClose}>✕</button>
        </div>

        {err && <div className="banner error">{err}</div>}

        <label>类型</label>
        <select value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
          <option value="issue">issue</option>
          <option value="pr">PR</option>
        </select>

        <label>标题</label>
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="必填" />

        <label>正文</label>
        <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={5} />

        <label>作者</label>
        <input value={author} onChange={(e) => setAuthor(e.target.value)} />

        <label>角色</label>
        <select
          value={actorTypeLocal}
          onChange={(e) => setActorTypeLocal(e.target.value as ActorType)}
        >
          <option value="human">human（默认进 inbox）</option>
          <option value="agent">agent（默认进 todo）</option>
        </select>

        <label>标签（逗号分隔）</label>
        <input
          value={labelsText}
          onChange={(e) => setLabelsText(e.target.value)}
          placeholder="bug, ai, ..."
        />

        <div className="right">
          <button className="link" onClick={onClose}>取消</button>
          <button className="primary" disabled={busy || !title.trim()} onClick={submit}>
            创建
          </button>
        </div>
      </div>
    </div>
  );
}
