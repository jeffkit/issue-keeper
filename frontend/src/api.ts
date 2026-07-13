import type {
  Issue,
  IssueDetail,
  Project,
  Status,
  ActorType,
  Kind,
} from "./types";

// 同源 / 开发代理都走 /api 相对路径
const BASE = "/api";

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
