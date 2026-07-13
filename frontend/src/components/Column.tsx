import { useDroppable } from "@dnd-kit/core";
import { STATUS_LABEL, type Issue, type Status } from "../types";
import { IssueCard } from "./IssueCard";

interface ColumnProps {
  status: Status;
  issues: Issue[];
  onSelect: (issue: Issue) => void;
  selectedNumber: number | null;
}

export function Column({ status, issues, onSelect, selectedNumber }: ColumnProps) {
  const { setNodeRef, isOver } = useDroppable({ id: status });
  return (
    <div className={`column${isOver ? " over" : ""}`} ref={setNodeRef}>
      <div className="column-head">
        <span className="col-title">{STATUS_LABEL[status]}</span>
        <span className="col-count">{issues.length}</span>
      </div>
      <div className="column-body">
        {issues.map((it) => (
          <IssueCard
            key={`${it.kind}-${it.number}`}
            issue={it}
            onSelect={onSelect}
            selected={selectedNumber === it.number}
          />
        ))}
        {issues.length === 0 && <div className="empty">（空）</div>}
      </div>
    </div>
  );
}
