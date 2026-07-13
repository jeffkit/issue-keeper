import { useDraggable } from "@dnd-kit/core";
import type { Issue } from "../types";

interface IssueCardProps {
  issue: Issue;
  onSelect: (issue: Issue) => void;
  selected: boolean;
}

export function IssueCard({ issue, onSelect, selected }: IssueCardProps) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: issue.number,
  });
  return (
    <div
      ref={setNodeRef}
      className={`card${selected ? " selected" : ""}${isDragging ? " dragging-src" : ""}`}
      {...listeners}
      {...attributes}
    >
      <div className="card-row">
        <span className="card-num">#{issue.number}</span>
        <span className={`badge ${issue.actor_type}`}>{issue.actor_type}</span>
      </div>
      <div className="card-title" onClick={() => onSelect(issue)} title="点击查看详情">
        {issue.title}
      </div>
      <div className="card-meta">
        <span>{issue.author || "—"}</span>
        {issue.assignee && <span className="assignee">→ {issue.assignee}</span>}
      </div>
    </div>
  );
}
