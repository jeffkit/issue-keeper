import { useEffect, useState } from "react";
import { addComment, closeIssue, getIssue, moveIssue } from "../api";
import {
  STATUS_LABEL,
  STATUS_ORDER,
  type ActorType,
  type Issue,
  type IssueDetail,
} from "../types";

interface Props {
  project: string;
  issue: Issue;
  actorName: string;
  actorType: ActorType;
  onClose: () => void;
  onChanged: () => void;
}

export function IssueDetail({ project, issue, actorName, actorType, onClose, onChanged }: Props) {
  const [detail, setDetail] = useState<IssueDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [commentBody, setCommentBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  function load() {
    setLoading(true);
    getIssue(project, issue.number, issue.kind)
      .then((d) => setDetail(d))
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }

  useEffect(load, [project, issue.number, issue.kind]);

  async function doMove(to: string) {
    setBusy(true);
    setErr("");
    try {
      await moveIssue(project, issue.number, {
        to_status: to as Issue["status"],
        actor: actorName,
        actor_type: actorType,
      }, issue.kind);
      onChanged();
      load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function doComment() {
    if (!commentBody.trim()) return;
    setBusy(true);
    setErr("");
    try {
      await addComment(project, issue.number, {
        body: commentBody,
        author: actorName,
        actor_type: actorType,
      }, issue.kind);
      setCommentBody("");
      onChanged();
      load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function doClose() {
    setBusy(true);
    setErr("");
    try {
      await closeIssue(project, issue.number, {
        actor: actorName,
        actor_type: actorType,
      }, issue.kind);
      onChanged();
      onClose();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="overlay" onClick={onClose}>
      <div className="detail" onClick={(e) => e.stopPropagation()}>
        <div className="detail-head">
          <div>
            <span className="card-num">#{issue.number}</span>{" "}
            <span className="detail-title">{issue.title}</span>
          </div>
          <button className="link" onClick={onClose}>✕</button>
        </div>

        <div className="detail-meta">
          <span>状态：<b>{STATUS_LABEL[issue.status]}</b></span>
          <span>作者：{issue.author || "—"}（{issue.actor_type}）</span>
          {issue.assignee && <span>负责人：{issue.assignee}</span>}
          <span>创建：{issue.created_at}</span>
        </div>

        <div className="status-actions">
          {STATUS_ORDER.map((s) => (
            <button
              key={s}
              className={`mini${s === issue.status ? " active" : ""}`}
              disabled={busy || s === issue.status}
              onClick={() => doMove(s)}
            >
              {STATUS_LABEL[s]}
            </button>
          ))}
          <button className="mini danger" disabled={busy} onClick={doClose}>关闭</button>
        </div>

        {err && <div className="banner error">{err}</div>}

        {issue.body && (
          <section>
            <h4>正文</h4>
            <pre className="body">{issue.body}</pre>
          </section>
        )}

        {loading ? (
          <div className="banner">加载详情…</div>
        ) : detail && (
          <>
            <section>
              <h4>状态历史（{detail.history.length}）</h4>
              <ul className="history">
                {detail.history.map((h) => (
                  <li key={h.id}>
                    <span className="step">
                      {h.from_status ? STATUS_LABEL[h.from_status as keyof typeof STATUS_LABEL] : "—"}
                      {" → "}
                      {STATUS_LABEL[h.to_status as keyof typeof STATUS_LABEL]}
                    </span>
                    <span className="step-meta">by {h.actor}（{h.actor_type}） · {h.created_at}</span>
                    {h.comment && <div className="step-cmt">{h.comment}</div>}
                  </li>
                ))}
              </ul>
            </section>

            <section>
              <h4>评论（{detail.comments.length}）</h4>
              {detail.comments.length === 0 && <div className="empty">（暂无评论）</div>}
              <ul className="comments">
                {detail.comments.map((c) => (
                  <li key={c.id}>
                    <div className="cmt-head">
                      <b>{c.author}</b> <span>{c.created_at}</span>
                    </div>
                    <pre className="body">{c.body}</pre>
                  </li>
                ))}
              </ul>
            </section>

            <section>
              <h4>添加评论</h4>
              <textarea
                value={commentBody}
                onChange={(e) => setCommentBody(e.target.value)}
                rows={3}
                placeholder="以当前身份留言…"
              />
              <div className="right">
                <button className="primary" disabled={busy || !commentBody.trim()} onClick={doComment}>
                  发表
                </button>
              </div>
            </section>
          </>
        )}
      </div>
    </div>
  );
}
