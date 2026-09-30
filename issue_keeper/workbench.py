"""工作台（V1，派生只读）：GitHub open issues × 管线状态 → 「需要我的东西」队列。

不做镜像不做写回（GitHub 是 issue 的唯一权威；看板是派生视图）：
  数据面 = ①GitHubSource 实时 open issues（带 TTL 缓存）
          ②metrics 最新 run 终态（bridge MetricsRecorder 落盘）
          ③state.json（blocked/in_flight 簿记）+ run.lock 存活检测
阶段推导（先命中先算）：
  doing       在途 run（lock 活着）
  needs-human onhold/guarded/partial/abort/engine_error/rejected、安全过滤拦截、
              blocked 终态但未确认发出回评（recursive#2：回评没出去就得有人看）
  blocked     依赖未就绪（wakeup 监视中；已回评说明原因）
  settled     done（已落地）/ readonly/nochange/invalid（有结论无改动）
  queued      其余（新 issue 待派 / 无 run 记录）
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from collections import Counter

from . import metrics
from .sources import Resource
from .sources.github import GitHubSource
from .state import load_state

PIPELINE_DIR = pathlib.Path("~/.issue-keeper/pipeline").expanduser()
STATE_PATH = pathlib.Path("~/.issue-keeper/state.json").expanduser()
LEDGER_PATH = pathlib.Path("~/.issue-keeper/pipeline/runs.jsonl").expanduser()
GH_CACHE_TTL = 120.0

# run 终态 → 工作台阶段与理由
_STATUS_STAGE = {
    "onhold": ("needs-human", "HITL 计划待批"),
    "guarded": ("needs-human", "diff 护栏拦截（越界改动）"),
    "partial": ("needs-human", "全量测试两轮未过"),
    "abort": ("needs-human", "独立 review 叫停"),
    "engine_error": ("needs-human", "管线引擎异常"),
    "rejected": ("needs-human", "安全初筛未过，待人工复核"),
    "blocked": ("blocked", "依赖未就绪"),
    "done": ("settled", "已落地（issue 仍 open，确认后可关）"),
    "readonly": ("settled", "调查结论已回评（只读仓）"),
    "nochange": ("settled", "调查后无需改动"),
    "invalid": ("settled", "已在 main 修复/无需改动"),
}

_GH_CACHE: dict[str, tuple[float, list[Resource] | None, str]] = {}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _in_flight(repo: str, issue, lock_root: pathlib.Path | None) -> tuple[bool, str | None]:
    """run.lock 里的 bridge pid 还活着 → 在途，返回 (在途, 起跑时刻)。"""
    slug = str(repo).split("/")[-1]
    lock = (lock_root or PIPELINE_DIR) / f"{slug}-{issue}" / "run.lock"
    try:
        pid = int(lock.read_text(encoding="utf-8").strip())
    except Exception:
        return False, None
    if not _pid_alive(pid):
        return False, None
    started = None
    try:
        started = time.strftime(
            "%Y-%m-%dT%H:%M:%S%z", time.localtime(lock.stat().st_mtime))
    except OSError:
        pass
    return True, started


def gh_open_issues(repo: str, fetcher=None, cache_ttl: float = GH_CACHE_TTL,
                   now: float | None = None):
    """open issues（带进程内 TTL 缓存与 stale 兜底）。fetcher 可注入供测试。"""
    now = now if now is not None else time.time()
    hit = _GH_CACHE.get(repo)
    if hit and now - hit[0] < cache_ttl:
        return hit[1], hit[2]
    try:
        src = fetcher() if fetcher else GitHubSource()
        issues = src.list_open(repo, kinds=["issue", "pr"] if False else ["issue"])
        _GH_CACHE[repo] = (now, issues, "")
        return issues, ""
    except Exception as e:  # noqa: BLE001 —— 拉不到用 stale 缓存
        if hit and hit[1] is not None:
            return hit[1], f"stale（刷新失败: {str(e)[:80]}）"
        _GH_CACHE[repo] = (now, None, str(e)[:120])
        return None, str(e)[:120]


def _ledger_last_runs(days: int, ledger_path: pathlib.Path | None = None) -> dict:
    """台账兜底：metrics 2026-09-30 才启用，此前的 run 只在 runs.jsonl。

    台账没有节点级数据（故无 gate_failed），阶段推导只依赖 status。
    """
    out: dict[tuple, dict] = {}
    path = ledger_path or LEDGER_PATH
    cutoff = time.time() - days * 86400
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
            ts = time.mktime(time.strptime((r.get("ts") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        if ts < cutoff:
            continue
        key = (r.get("repo"), int(r.get("issue") or -1))
        prev = out.get(key)
        if prev is None or (r.get("ts") or "") > (prev.get("started") or ""):
            out[key] = {
                "execution_id": r.get("execution_id"),
                "status": r.get("status"),
                "comment_posted": r.get("comment_posted"),
                "started": r.get("ts"),
                "duration_secs": r.get("duration_secs"),
                "gate_failed": r.get("gate_failed"),
                "flow_version": r.get("flow_version"),
                "source": "ledger",
            }
    return out


def _metric_last_runs(days: int, metrics_dir: pathlib.Path | None) -> dict:
    """metrics 口径的最新 run（含节点级事实，优先于台账）。"""
    out: dict[tuple, dict] = {}
    for rec in metrics.iter_runs(days=days, metrics_dir=metrics_dir):
        key = (rec.get("repo"), int(rec.get("issue") or -1))
        if key in out:
            continue
        nodes = rec.get("nodes") or []
        out[key] = {
            "execution_id": rec.get("execution_id"),
            "status": rec.get("status"),
            "comment_posted": rec.get("comment_posted"),
            "started": rec.get("started"),
            "duration_secs": rec.get("duration_secs"),
            "gate_failed": next((n.get("gate") for n in nodes
                                 if n.get("type") == "gate" and n.get("passed") is not True), None),
            "flow_version": rec.get("flow_version"),
            "source": "metrics",
        }
    return out


def build_workbench(bindings: list[dict], *, days: int = 45,
                    metrics_dir: pathlib.Path | None = None,
                    state_path: pathlib.Path | None = None,
                    lock_root: pathlib.Path | None = None,
                    ledger_path: pathlib.Path | None = None,
                    gh_fetcher=None, now: float | None = None) -> dict:
    """bindings: [{name, source}]（source 以 github 开头的才进工作台）。

    返回 {groups, errors, generated_at}；groups 五个阶段各含卡片数组。
    """
    now = now if now is not None else time.time()
    repos = [b["name"] for b in bindings if str(b.get("source", "")).startswith("github")]
    state = None
    try:
        state = load_state((state_path or STATE_PATH).expanduser())
    except Exception:
        state = None

    # 最新 run：metrics（节点级）优先，台账兜底（历史 run 只在台账）
    last_run = _ledger_last_runs(days, ledger_path)
    last_run.update(_metric_last_runs(days, metrics_dir))

    groups: dict[str, list[dict]] = {k: [] for k in
                                     ("needs-human", "doing", "blocked", "queued", "settled")}
    errors: dict[str, str] = {}
    counts: Counter = Counter()

    for repo in repos:
        issues, err = gh_open_issues(repo, fetcher=gh_fetcher, now=now)
        if err and issues is None:
            errors[repo] = err
            continue
        if err:
            errors[repo] = err
        slug = repo.replace("/", "-")
        for res in issues or []:
            it = state.repo(slug).item(str(res.number)) if state is not None else None
            in_flight, since = _in_flight(repo, res.number, lock_root)
            reason = ""
            stage = "queued"
            if in_flight:
                stage = "doing"
            elif it is not None and it.blocked:
                stage, reason = "needs-human", "安全过滤命中（已回评待人工复核）"
            else:
                rec = last_run.get((repo, res.number))
                if rec is not None:
                    stage, reason = _STATUS_STAGE.get(rec.get("status") or "",
                                                      ("queued", ""))
                    if rec.get("status") == "blocked":
                        # blocked 语义分叉（recursive#2）：回评发出 = 等依赖（已回评
                        # 说明原因）；没发出 = issue 上没有任何解释，必须有人看。
                        # 历史 None（旧台账漏记）按已回评算，不夸大警报。
                        if rec.get("comment_posted") is False:
                            stage, reason = "needs-human", "blocked 终态但未确认发出回评"
                        else:
                            stage, reason = "blocked", "依赖未就绪（已回评说明，唤醒监视中）"
            rec = last_run.get((repo, res.number))
            card = {
                "repo": repo,
                "issue": res.number,
                "title": res.title,
                "url": f"https://github.com/{repo}/issues/{res.number}",
                "author": res.author,
                "created_at": res.created_at,
                "labels": res.labels,
                "stage": stage,
                "reason": reason,
                "in_flight": in_flight,
                "running_since": since,
                "last_run": rec,
            }
            if rec is not None and stage == "doing" and rec.get("started"):
                card["running_since"] = card.get("running_since") or rec.get("started")
            groups[stage].append(card)
            counts[stage] += 1

    for stage, cards in groups.items():
        cards.sort(key=lambda c: (c["repo"], c["issue"] or 0))
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
        "window_days": days,
        "counts": dict(counts),
        "groups": groups,
        "errors": errors,
    }
