import { useState } from "react";
import { createProject } from "../api";
import type { Role } from "../types";

interface Props {
  onClose: () => void;
  onCreated: (project: string) => void;
}

export function CreateProjectModal({ onClose, onCreated }: Props) {
  const [name, setName] = useState("");
  const [agentLabel, setAgentLabel] = useState("");
  const [cwd, setCwd] = useState("");
  const [profile, setProfile] = useState("claude-code");
  const [source, setSource] = useState<"internal" | "github_cli" | "github_token">("internal");
  const [githubToken, setGithubToken] = useState("");
  const [monitorPrs, setMonitorPrs] = useState(false);
  const [role, setRole] = useState<Role>("agent");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  function autoLabel() {
    if (agentLabel.trim()) return;
    const base = name.trim();
    if (base) setAgentLabel(`${base}-agent`);
  }

  async function submit() {
    if (!name.trim()) return;
    setBusy(true);
    setErr("");
    try {
      await createProject({
        name: name.trim(),
        agent_label: agentLabel.trim() || `${name.trim()}-agent`,
        cwd: cwd.trim(),
        profile,
        source,
        github_token: githubToken,
        monitor_prs: monitorPrs,
        role,
      });
      onCreated(name.trim());
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
          <div className="detail-title">新建项目</div>
          <button className="link" onClick={onClose}>✕</button>
        </div>

        {err && <div className="banner error">{err}</div>}

        <label>项目名（对应 binding.repo）</label>
        <input value={name} onChange={(e) => setName(e.target.value)} onBlur={autoLabel} placeholder="如 proj-a 或 owner/repo" />

        <label>agent 身份标签</label>
        <input value={agentLabel} onChange={(e) => setAgentLabel(e.target.value)} placeholder="默认 &lt;项目名&gt;-agent" />

        <label>工作目录（cwd，agent 跑的上下文）</label>
        <input value={cwd} onChange={(e) => setCwd(e.target.value)} placeholder="如 ~/projects/proj-a" />

        <label>profile</label>
        <input value={profile} onChange={(e) => setProfile(e.target.value)} placeholder="claude-code" />

        <label>issue 来源</label>
        <select value={source} onChange={(e) => setSource(e.target.value as typeof source)}>
          <option value="internal">internal（自建 issue 系统）</option>
          <option value="github_token">github_token（PAT）</option>
          <option value="github_cli">github_cli（本地 gh）</option>
        </select>

        {source === "github_token" && (
          <>
            <label>GitHub Token（支持 ${"{VAR}"} 占位）</label>
            <input value={githubToken} onChange={(e) => setGithubToken(e.target.value)} placeholder={"${GITHUB_TOKEN}"} />
          </>
        )}

        <label>角色</label>
        <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
          <option value="agent">agent：负责本仓代码 / issue（默认）</option>
          <option value="keeper">keeper：管理向，帮人类管 issue、代理提问、必要时 HitL 联系人类</option>
        </select>

        <label className="inline">
          <input type="checkbox" checked={monitorPrs} onChange={(e) => setMonitorPrs(e.target.checked)} />
          监控 PR
        </label>

        <div className="right">
          <button className="link" onClick={onClose}>取消</button>
          <button className="primary" disabled={busy || !name.trim()} onClick={submit}>
            创建
          </button>
        </div>
      </div>
    </div>
  );
}
