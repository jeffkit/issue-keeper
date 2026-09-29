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

// ── Pipeline 观测面（L2）─────────────────────────────────────────────

export interface PipelineRepoSummary {
  runs: number;
  done: number;
  success_rate: number | null;
  by_status: Record<string, number>;
  duration_secs: { p50: number | null; max: number | null };
  tokens: number | null;
  gate_failures: Record<string, number>;
}

export interface PipelineSummary {
  window_days: number;
  total_runs: number;
  success_rate: number | null;
  duration_secs: { p50: number | null; p90: number | null };
  tokens_total: number | null;
  by_repo: Record<string, PipelineRepoSummary>;
  gate_failures: Record<string, number>;
  failure_nodes: Record<string, number>;
  flow_versions: Record<string, number>;
}

export interface PipelineRunRow {
  execution_id: string;
  repo: string;
  issue: number;
  started: string;
  status: string;
  ok: boolean;
  error?: string | null;
  duration_secs: number | null;
  flow_version?: string | null;
  pushed?: boolean | null;
  merged?: boolean | null;
  comment_posted?: boolean | null;
  base_branch?: string | null;
  push_mode?: string | null;
  gate_failed?: string | null;
  tokens_total?: number | null;
  segments: Record<string, number>;
}

export interface PipelineRunDetail {
  execution_id: string;
  repo: string;
  issue: number;
  status: string;
  nodes: Array<Record<string, any>>;
  [k: string]: any;
}

export interface BenchmarkInfo { name: string; latest_version: number | null; versions: Record<string, any>; }
