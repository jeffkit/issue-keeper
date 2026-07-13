import { Column } from "./Column";
import type { Issue, Status } from "../types";

interface BoardProps {
  statusOrder: Status[];
  byStatus: Record<Status, Issue[]>;
  onSelect: (issue: Issue) => void;
  selectedNumber: number | null;
}

export function Board({ statusOrder, byStatus, onSelect, selectedNumber }: BoardProps) {
  return (
    <div className="board">
      {statusOrder.map((s) => (
        <Column
          key={s}
          status={s}
          issues={byStatus[s]}
          onSelect={onSelect}
          selectedNumber={selectedNumber}
        />
      ))}
    </div>
  );
}
