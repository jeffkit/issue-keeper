export type Status =
  | "inbox"
  | "todo"
  | "doing"
  | "review"
  | "done"
  | "closed";

export type ActorType = "human" | "agent";
export type Kind = "issue" | "pr";

export interface Issue {
  kind: Kind;
  number: number;
  title: string;
  body: string;
  state: string;
  status: Status;
  labels: string[];
  author: string;
  actor_type: ActorType;
  assignee: string;
  created_at: string;
  updated_at: string;
}

export interface Comment {
  id: string;
  author: string;
  body: string;
  created_at: string;
}

export interface HistoryEntry {
  id: number;
  project: string;
  kind: Kind;
  issue_number: number;
  from_status: string | null;
  to_status: string;
  actor: string;
  actor_type: ActorType;
  comment: string | null;
  created_at: string;
}

export interface IssueDetail extends Issue {
  comments: Comment[];
  history: HistoryEntry[];
}

export type Role = "agent" | "keeper";

export interface Project {
  project: string;
  total: number;
  open: number;
  role: Role;
  agent_label: string;
  intro: string;
}

export interface TeamMember {
  project: string;
  agent_label: string;
  cwd: string;
  intro: string;
  role: Role;
}

export const STATUS_ORDER: Status[] = [
  "inbox",
  "todo",
  "doing",
  "review",
  "done",
  "closed",
];

export const STATUS_LABEL: Record<Status, string> = {
  inbox: "收件箱",
  todo: "待处理",
  doing: "进行中",
  review: "待 Review",
  done: "已完成",
  closed: "已关闭",
};
