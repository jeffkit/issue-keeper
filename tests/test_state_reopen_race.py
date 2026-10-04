"""#9 复现：daemon 轮尾整份写回覆盖轮中途的 CLI `reopen`。

竞态形状（窗口 = 整轮，最长 poll_interval + 一轮处理时长）：

    daemon: 轮首 load_state(:661) ──────────── 轮尾 save_state(:675)
    CLI   :              reopen_issues(:912-923)   ← 落在这段窗口里就整份蒸发

`state._state_lock` 只保证两次写串行，不保证合并：daemon 写的是轮首载入的内存副本，
CLI 的 processed/blocked/wakeup_deps=已清 被整份盖回旧值。

预期（修复后）：轮尾写前重读 + 按字段合并 → CLI 的清理留存，且 daemon 轮内推进的
字段（processed_comment_ids）不被 CLI 侧的旧快照回滚。
"""

from __future__ import annotations

from issue_keeper import keeper
from issue_keeper.config import Config, RepoBinding
from issue_keeper.keeper import reopen_issues, run_once
from issue_keeper.state import State, load_state, save_state

CLI_REOPENED = 6   # 人工 reopen 的那条（daemon 轮首看到的是「已消费」）
DAEMON_TOUCHED = 5  # daemon 轮内又答掉一条评论的那条


def test_cli_reopen_survives_daemon_round_tail_write(tmp_path, monkeypatch):
    cfg = Config(
        repos=[RepoBinding(repo="a/b", profile="p", source="internal",
                           internal_db=str(tmp_path / "internal.db"))],
        state_file=tmp_path / "state.json",
    )
    st = State()
    watched = st.repo("a-b").item(str(CLI_REOPENED))
    watched.processed = True
    watched.blocked = True
    watched.wakeup_deps = [41]
    round_item = st.repo("a-b").item(str(DAEMON_TOUCHED))
    round_item.processed = True
    round_item.processed_comment_ids = {"c-old"}
    save_state(cfg.state_path, st)

    def _round_with_concurrent_reopen(binding, config, state, profile_cache, source_cache):
        # daemon 轮内的状态推进（只在内存里，轮尾才落盘）
        state.repo("a-b").item(str(DAEMON_TOUCHED)).processed_comment_ids.add("c-round")
        # 轮中途：另一个进程的 CLI reopen 清掉 watched 的终态
        assert reopen_issues(config, "a/b", [CLI_REOPENED]) == [CLI_REOPENED]
        return 0

    monkeypatch.setattr(keeper, "process_repo", _round_with_concurrent_reopen)
    monkeypatch.setattr(keeper, "keeper_patrol", lambda *a, **k: 0)
    monkeypatch.setattr(keeper, "_reap_pipelines", lambda *a, **k: 0)

    run_once(cfg)

    after = load_state(cfg.state_path).repo("a-b")
    reopened = after.item(str(CLI_REOPENED))
    assert (reopened.processed, reopened.blocked, reopened.wakeup_deps) == (False, False, [])
    assert after.item(str(DAEMON_TOUCHED)).processed_comment_ids == {"c-old", "c-round"}
