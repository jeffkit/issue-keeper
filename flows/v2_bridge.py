#!/usr/bin/env python3
"""v2 engine bridge——keeper 派发契约的 self-improve v2 适配层。

与 pipeline_bridge 完全同一派发契约：dispatch.json（argv 传入）/ 台账 ledger /
`RESULT {json}` stdout 行——keeper 的 reaper/兜底回评/看板无需感知差异。
差异只在中段：不跑 issue-pipeline flow，改为调 recursive 仓的 self-improve
v2 引擎（.dev/flows/self_improve_bridge_v2.py，agentrun/gate/git_publish
库节点版）；screener/triage/回评仍由 keeper 负责。

verdict 映射：
  committed        → status=done,      pushed/merged=True
  skip-commit      → status=done,      pushed/merged=False（无改动）
  failed-preserved → status=failed,    error=why（worktree/现场已保全）
  engine_error     → status=engine_error, error=why

超时：实际预算墙由 keeper 注入的 RECURSIVE_RUN_DEADLINE 决定（派发时刻 +
pipeline_timeout_secs - run_deadline_margin_secs，2026-10-03 起，宿主到点
优雅收尾：checkpoint/verdict 落盘且台账带 node_retry_exhausted）；V2_TIMEOUT_SECS
（默认 8h）仅是 keeper 未注入时的兜底，与 pipeline_timeout_secs（默认 5400s）
并不对齐——不注入时 keeper 侧 reaper 先到点 killpg。到点杀进程树（recursive
桥自身也有 killpg 层）并落 engine_error。
"""
from __future__ import annotations
import os

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pipeline_bridge as pb  # noqa: E402  (复用 append_ledger / normalize_result)

# launchd PATH 没有 ~/.cargo/bin——v2 的 GATE 节点跑 cargo 会 FileNotFoundError
# （#40 旧同款、#67 v2 首单复刻：impl 成功、门禁全灭）。在派生子进程前补齐
# PATH（plaita 沙箱白名单的 PATH 从本进程 os.environ 取，改这里即全局生效）。
pb.ensure_tool_path()

V2_TIMEOUT_SECS = 28800


def _read_verdict(main_clone: str, run_id: str) -> dict:
    state = Path(main_clone) / ".flowcast" / "runs" / run_id / "state.json"
    try:
        return json.loads(state.read_text(encoding="utf-8")).get("verdict") or {}
    except Exception:
        return {}


def _finish(result: dict, ok: bool, started: str, payload: dict, t0: float,
            extra: dict | None = None) -> None:
    result = pb.normalize_result(result)
    pb.append_ledger({
        "ts": started,
        "repo": payload.get("repo_full"),
        "issue": payload.get("issue_number"),
        "author": payload.get("author"),
        "status": result.get("status"),
        "error": result.get("error"),
        "comment_posted": result.get("comment_posted"),
        "pushed": result.get("pushed"),
        "ok": ok,
        "duration_secs": round(time.time() - t0, 1),
        "flow_source": "v2",
        "flow_version": "self-improve-v2",
        **(extra or {}),
    })
    print("RESULT " + json.dumps(result, ensure_ascii=False, default=str))


def main() -> None:
    t0 = time.time()
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    payload = (json.load(open(sys.argv[1], encoding="utf-8")) if len(sys.argv) > 1
               else json.load(sys.stdin))
    main_clone = payload.get("main_clone") or ""
    v2 = Path(main_clone) / ".dev" / "flows" / "self_improve_bridge_v2.py" if main_clone else None
    if not v2 or not v2.exists():
        _finish({"status": "engine_error", "error": f"v2 bridge not found: {v2}"},
                False, started, payload, t0)
        return

    artifact = Path(payload["artifact_dir"])
    body = ""
    bf = payload.get("body_file")
    if bf and Path(bf).exists():
        body = Path(bf).read_text(encoding="utf-8")
    goal = (f"#{payload.get('issue_number')} {payload.get('title', '')}\n\n"
            f"{body}").strip()
    gf = artifact / "v2-goal.md"
    gf.write_text(goal, encoding="utf-8")

    run_id = f"pipeline-{payload.get('issue_number')}-{time.strftime('%m%d%H%M%S')}"
    # 全链 DeepSeek flash（2026-10-04 起 GLM 限流切回）。实现/评审同 preset
    # （executor/maxSteps/env 全同构，~/.plaita/agents.json），per-repo 覆盖走
    # dispatch payload 的 agent/reviewer 字段（pipeline_repos 契约）。
    cmd = [sys.executable, str(v2),
           "--goal-file", str(gf), "--repo", main_clone, "--run-id", run_id,
           "--agent", payload.get("agent") or "deepseek-flash",
           "--reviewer", payload.get("reviewer") or "deepseek-flash"]
    if payload.get("dry_run"):
        cmd.append("--dry-run")

    extra_env = payload.get("engine_env") or {}
    log_path = artifact / "v2-run.log"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=V2_TIMEOUT_SECS,
                           env={**os.environ, **extra_env})
        log_path.write_text(
            (r.stdout or "")[-8000:] + "\n--- stderr ---\n" + (r.stderr or "")[-4000:])
        verdict = _read_verdict(main_clone, run_id)
        if not verdict:
            verdict = {"verdict": "engine_error",
                       "why": f"v2 run exit={r.returncode} without verdict（见 v2-run.log）"}
    except subprocess.TimeoutExpired:
        verdict = {"verdict": "engine_error",
                   "why": f"v2 run timeout after {V2_TIMEOUT_SECS}s"}

    v = verdict.get("verdict")
    if v == "committed":
        _finish({"status": "done", "pushed": True, "merged": True,
                 "note": verdict.get("via") or ""}, True, started, payload, t0,
                {"run_id": run_id})
    elif v == "skip-commit":
        _finish({"status": "done", "pushed": False, "merged": False,
                 "note": verdict.get("why") or "no changes"}, True,
                started, payload, t0, {"run_id": run_id})
    elif v == "retry-later":
        # 环境性失败（磁盘守卫等）：keeper 不消费、自动重派（daily-limit 兜底）
        _finish({"status": "retry-later", "pushed": False, "merged": False,
                 "stage": verdict.get("stage"),
                 "error": str(verdict.get("why") or "retry-later")[-500:]},
                False, started, payload, t0, {"run_id": run_id})
    elif v == "failed-preserved":
        _finish({"status": "failed", "pushed": False, "merged": False,
                 "stage": verdict.get("stage"),
                 "error": str(verdict.get("why") or verdict.get("gate") or "failed")[-500:]},
                False, started, payload, t0, {"run_id": run_id})
    else:
        # 台账 extra 透传 node_retry_exhausted（DESIGN-local-distributed-host §5）：
        # 宿主已判定节点重试耗尽/预算墙——keeper reaper 见标记跳过自动重派直接
        # 升级。不透传的话 reaper 只见 status=engine_error，会把耗尽 run 再白烧
        # 一轮（implement 1-2h）才升级。
        extra = {"run_id": run_id}
        if verdict.get("node_retry_exhausted"):
            extra["node_retry_exhausted"] = True
        _finish({"status": "engine_error",
                 "error": str(verdict.get("why") or "unknown")[-500:]},
                False, started, payload, t0, extra)


if __name__ == "__main__":
    main()
