import { useEffect, useState } from "react";
import { listTeam, updateProject } from "../api";
import type { TeamMember } from "../types";

export function TeamPanel() {
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busyLabel, setBusyLabel] = useState<string>("");

  function refresh() {
    setLoading(true);
    listTeam()
      .then(setMembers)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }

  useEffect(refresh, []);

  async function toggleKeeper(m: TeamMember) {
    const next = m.role === "keeper" ? "agent" : "keeper";
    setBusyLabel(m.agent_label);
    try {
      await updateProject(m.project, { role: next });
      refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyLabel("");
    }
  }

  if (loading) return <div className="banner">加载团队成员…</div>;
  if (error) return <div className="banner error">{error}</div>;
  if (members.length === 0) {
    return (
      <div className="banner">
        团队为空。点顶栏「+ 项目」新建一个项目，或在项目目录运行
        <code> python -m issue_keeper onboard &lt;目录&gt; --gen-intro</code>。
      </div>
    );
  }

  return (
    <div className="team-grid">
      {members.map((m) => (
        <div className={"team-card" + (m.role === "keeper" ? " keeper" : "")} key={m.agent_label}>
          <div className="team-avatar">{initials(m.agent_label)}</div>
          <div className="team-body">
            <div className="team-name-row">
              <span className="team-name">{m.agent_label}</span>
              {m.role === "keeper" ? (
                <span className="role-badge keeper" title="管理向 keeper：帮人类管 issue / 代理提问 / 优先解答 / HitL 联系人类">keeper</span>
              ) : (
                <span className="role-badge">agent</span>
              )}
            </div>
            <div className="team-project">@ {m.project}</div>
            {m.intro ? (
              <p className="team-intro">{m.intro}</p>
            ) : (
              <p className="team-intro empty">（暂无介绍）</p>
            )}
            {m.cwd && <div className="team-cwd" title={m.cwd}>{m.cwd}</div>}
            <div className="team-actions">
              <button
                className="mini"
                disabled={busyLabel === m.agent_label}
                onClick={() => toggleKeeper(m)}
                title="切换 keeper / agent 角色"
              >
                {m.role === "keeper" ? "取消 keeper" : "标为 keeper"}
              </button>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function initials(label: string): string {
  const parts = label.replace(/-agent$/i, "").split(/[-_]/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}
