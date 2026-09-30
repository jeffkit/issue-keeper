import type {
  Issue,
  BenchmarkInfo,
  PipelineRunDetail,
  PipelineRunRow,
  PipelineSummary,
  IssueDetail,
  Project,
  Status,
  ActorType,
  Kind,
  Role,
  TeamMember,
  WorkbenchData,
} from "./types";

// 同源相对路径：本地 127.0.0.1:7433 与公网 /ik/ 前缀下都解析正确
const BASE = "./api";

async function j<T>(resP: Promise<Response>): Promise<T> {
  const res = await resP;
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export function listProjects(): Promise<Project[]> {
  return j(fetch(`${BASE}/projects`));
}

export function createProject(data: {
  name: string;
  agent_label: string;
  cwd: string;
  profile: string;
  source: "internal" | "github_cli" | "github_token";
  github_token?: string;
  monitor_prs?: boolean;
  env?: Record<string, string>;
  role: Role;
}): Promise<{ project: string; agent_label: string; role: Role; created: boolean }> {
  return j(
    fetch(`${BASE}/projects`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(data),
    }),
  );
}

export function updateProject(
  name: string,
  data: { role?: Role; agent_label?: string; cwd?: string; intro?: string },
): Promise<{ project: string; role: Role; agent_label: string; cwd: string; intro: string }> {
  return j(
    fetch(`${BASE}/projects/${encodeURIComponent(name)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(data),
    }),
  );
}

export function deleteProject(name: string): Promise<{ project: string; deleted: boolean }> {
  return j(
    fetch(`${BASE}/projects/${encodeURIComponent(name)}`, { method: "DELETE" }),
  );
}

export function listTeam(): Promise<TeamMember[]> {
  return j(fetch(`${BASE}/team`));
}

export function listIssues(project: string, kind: Kind = "issue"): Promise<Issue[]> {
  return j(fetch(`${BASE}/projects/${encodeURIComponent(project)}/issues?kind=${kind}`));
}

export function getIssue(project: string, number: number, kind: Kind = "issue"): Promise<IssueDetail> {
  return j(fetch(`${BASE}/projects/${encodeURIComponent(project)}/issues/${number}?kind=${kind}`));
}

export function createIssue(
  project: string,
  data: {
    title: string;
    body: string;
    author: string;
    actor_type: ActorType;
    kind: Kind;
    labels?: string[];
  },
): Promise<Issue> {
  return j(
    fetch(`${BASE}/projects/${encodeURIComponent(project)}/issues`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(data),
    }),
  );
}

export function moveIssue(
  project: string,
  number: number,
  data: { to_status: Status; actor: string; actor_type: ActorType; comment?: string },
  kind: Kind = "issue",
): Promise<Issue> {
  return j(
    fetch(`${BASE}/projects/${encodeURIComponent(project)}/issues/${number}/move?kind=${kind}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(data),
    }),
  );
}

export function addComment(
  project: string,
  number: number,
  data: { body: string; author: string; actor_type: ActorType },
  kind: Kind = "issue",
): Promise<{ id: string }> {
  return j(
    fetch(`${BASE}/projects/${encodeURIComponent(project)}/issues/${number}/comments?kind=${kind}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(data),
    }),
  );
}

export function closeIssue(
  project: string,
  number: number,
  data: { actor: string; actor_type: ActorType },
  kind: Kind = "issue",
): Promise<Issue> {
  return j(
    fetch(`${BASE}/projects/${encodeURIComponent(project)}/issues/${number}/close?kind=${kind}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(data),
    }),
  );
}

// ── Pipeline 观测面（L2）─────────────────────────────────────────────

export function pipelineSummary(days: number): Promise<PipelineSummary> {
  return j(fetch(`${BASE}/pipeline/summary?days=${days}`));
}

export function pipelineRuns(days: number, repo: string): Promise<PipelineRunRow[]> {
  const q = new URLSearchParams({ days: String(days) });
  if (repo) q.set("repo", repo);
  return j(fetch(`${BASE}/pipeline/runs?${q}`));
}

export function pipelineRunDetail(executionId: string): Promise<PipelineRunDetail> {
  return j(fetch(`${BASE}/pipeline/runs/${encodeURIComponent(executionId)}`));
}

export function benchmarksList(): Promise<BenchmarkInfo[]> {
  return j(fetch(`${BASE}/benchmarks`));
}

export function workbench(days: number): Promise<WorkbenchData> {
  return j(fetch(`${BASE}/workbench?days=${days}`));
}
