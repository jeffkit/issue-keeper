import { useEffect, useState } from "react";
import { listTeam } from "../api";
import type { TeamMember } from "../types";

export function TeamPanel() {
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    setLoading(true);
    listTeam()
      .then(setMembers)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="banner">加载团队成员…</div>;
  if (error) return <div className="banner error">{error}</div>;
  if (members.length === 0) {
    return (
      <div className="banner">
        团队为空。请在项目目录运行 <code>python -m issue_keeper team sync --config config.yaml</code> 生成花名册。
      </div>
    );
  }

  return (
    <div className="team-grid">
      {members.map((m) => (
        <div className="team-card" key={m.agent_label}>
          <div className="team-avatar">{initials(m.agent_label)}</div>
          <div className="team-body">
            <div className="team-name">{m.agent_label}</div>
            <div className="team-project">@ {m.project}</div>
            {m.intro ? (
              <p className="team-intro">{m.intro}</p>
            ) : (
              <p className="team-intro empty">（暂无介绍）</p>
            )}
            {m.cwd && <div className="team-cwd" title={m.cwd}>{m.cwd}</div>}
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
