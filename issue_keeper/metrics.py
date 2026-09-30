"""run 观测数据聚合器：消费 bridge MetricsRecorder 落盘的 metrics JSON。

数据源：~/.issue-keeper/pipeline/metrics/<YYYY-MM>/<execution_id>.json
（节点级时长/agent 模型与 token 用量/门级明细 + 契约快照），本地永久保留。
为 dashboard（L2）、supervisor 巡检（L3）、benchmark 构建（L4）提供统一读取与
按仓聚合口径。纯函数、无副作用，方便测试。
"""

from __future__ import annotations

import json
import pathlib
import time
from collections import Counter
from statistics import median

METRICS_DIR = pathlib.Path("~/.issue-keeper/pipeline/metrics").expanduser()
LEDGER_PATH = pathlib.Path("~/.issue-keeper/pipeline/runs.jsonl").expanduser()

# agent/gate 节点才参与「段耗时」口径（if/assignment 等 glue 节点无意义）
SEGMENT_TYPES = ("agentrun", "gate")


def _ledger_records(days: int | None, repo: str | None,
                    ledger_path: pathlib.Path | None = None) -> list[dict]:
    """台账兜底记录：metrics 自 2026-09-30 才落盘，此前的 run 只在 runs.jsonl。

    台账没有节点级事实——把 gate_failed/tokens_total 映射成合成节点，保持
    下游（summarize/segments）单一数据形状；duration_ms 为 None 不进段耗时。
    """
    path = ledger_path or LEDGER_PATH
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    cutoff = time.time() - days * 86400 if days else None
    for line in lines:
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not r.get("execution_id"):
            continue
        if repo and r.get("repo") != repo:
            continue
        if cutoff is not None and not _ts_after(r.get("ts") or "", cutoff):
            continue
        nodes: list[dict] = []
        if r.get("gate_failed"):
            nodes.append({"id": "gate", "type": "gate", "status": "error",
                          "duration_ms": None, "gate": r["gate_failed"], "passed": False})
        if r.get("tokens_total"):
            nodes.append({"id": "agentrun", "type": "agentrun", "status": "success",
                          "duration_ms": None,
                          "tokens": {"input": r["tokens_total"], "output": 0}})
        out.append({
            "schema": 1,
            "execution_id": r.get("execution_id"),
            "repo": r.get("repo"), "issue": r.get("issue"),
            "started": r.get("ts"), "ended": None,
            "status": r.get("status"), "ok": r.get("ok"),
            "error": r.get("error"),
            "duration_secs": r.get("duration_secs"),
            "flow_source": r.get("flow_source"), "flow_version": r.get("flow_version"),
            "nodes": nodes, "source": "ledger",
        })
    return out


def iter_runs(days: int | None = None, repo: str | None = None,
              metrics_dir: pathlib.Path | None = None,
              ledger_fallback: bool = True,
              ledger_path: pathlib.Path | None = None) -> list[dict]:
    """按新→旧返回 run 记录；days/window 与 repo 可选过滤。

    metrics 优先；ledger_fallback=True 时用 runs.jsonl 补齐 metrics 里没有的
    execution_id（历史 run 只在台账）。
    """
    root = metrics_dir or METRICS_DIR
    cutoff = time.time() - days * 86400 if days else None
    out: list[dict] = []
    if root.exists():
        for path in sorted(root.glob("*/*.json"), reverse=True):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(rec, dict) or not rec.get("execution_id"):
                continue
            if repo and rec.get("repo") != repo:
                continue
            if cutoff is not None and not _ts_after(rec.get("started") or "", cutoff):
                continue
            rec["_path"] = str(path)
            out.append(rec)
    if ledger_fallback:
        seen = {r.get("execution_id") for r in out}
        for rec in _ledger_records(days, repo, ledger_path):
            if rec["execution_id"] not in seen:
                out.append(rec)
    out.sort(key=lambda r: r.get("started") or "", reverse=True)
    return out


def run_detail(execution_id: str, metrics_dir: pathlib.Path | None = None) -> dict | None:
    for rec in iter_runs(metrics_dir=metrics_dir, ledger_fallback=False):
        if rec.get("execution_id") == execution_id:
            return rec
    return None


def summarize(days: int = 30, repo: str | None = None,
              metrics_dir: pathlib.Path | None = None,
              ledger_path: pathlib.Path | None = None) -> dict:
    """按仓聚合观测口径：成功率/时长分布/状态分布/失败门 top/token/段耗时。

    success_rate 只把 status=done 记成功；readonly/blocked/invalid/nochange 等
    业务早退按各自状态计数（不算失败，也不算 done）。
    """
    runs = iter_runs(days=days, repo=repo, metrics_dir=metrics_dir,
                     ledger_path=ledger_path)
    by_repo: dict[str, dict] = {}
    gate_failures: Counter[str] = Counter()
    failure_nodes: Counter[str] = Counter()
    flow_versions: Counter[str] = Counter()
    all_durations: list[float] = []
    tokens_total = 0

    for rec in runs:
        r = rec.get("repo") or "(unknown)"
        status = rec.get("status") or "unknown"
        agg = by_repo.setdefault(r, {
            "runs": 0, "done": 0, "by_status": Counter(),
            "durations": [], "tokens": 0, "gate_failures": Counter(),
        })
        agg["runs"] += 1
        agg["by_status"][status] += 1
        if status == "done":
            agg["done"] += 1
        dur = rec.get("duration_secs")
        if isinstance(dur, (int, float)) and dur > 0:
            agg["durations"].append(dur)
            all_durations.append(dur)
        nodes = rec.get("nodes") or []
        repo_tokens = 0
        for n in nodes:
            if n.get("type") == "gate" and n.get("passed") is not True:
                name = n.get("gate") or n.get("id") or "gate"
                gate_failures[name] += 1
                agg["gate_failures"][name] += 1
            if n.get("type") == "agentrun" and (n.get("status") == "error" or n.get("timed_out")):
                failure_nodes[n.get("id") or "?"] += 1
            t = n.get("tokens") or {}
            repo_tokens += (t.get("input") or 0) + (t.get("output") or 0)
        agg["tokens"] += repo_tokens
        tokens_total += repo_tokens
        if rec.get("flow_version"):
            flow_versions[str(rec["flow_version"])] += 1

    def _finalize(agg: dict) -> dict:
        durations = sorted(agg["durations"])
        return {
            "runs": agg["runs"],
            "done": agg["done"],
            "success_rate": round(agg["done"] / agg["runs"], 3) if agg["runs"] else None,
            "by_status": dict(agg["by_status"]),
            "duration_secs": {
                "p50": round(median(durations), 1) if durations else None,
                "max": round(durations[-1], 1) if durations else None,
            },
            "tokens": agg["tokens"] or None,
            "gate_failures": dict(agg["gate_failures"]),
        }

    return {
        "window_days": days,
        "total_runs": len(runs),
        "success_rate": _success_rate(runs),
        "duration_secs": {
            "p50": round(median(all_durations), 1) if all_durations else None,
            "p90": round(all_durations[int(0.9 * (len(all_durations) - 1))], 1)
            if all_durations else None,
        },
        "tokens_total": tokens_total or None,
        "by_repo": {r: _finalize(a) for r, a in sorted(by_repo.items())},
        "gate_failures": dict(gate_failures),
        "failure_nodes": dict(failure_nodes),
        "flow_versions": dict(flow_versions),
    }


def segments_of(rec: dict) -> dict[str, int]:
    """单次 run 的段耗时（毫秒）：agent/gate 节点 id → duration_ms。"""
    return {n["id"]: n["duration_ms"] for n in (rec.get("nodes") or [])
            if n.get("type") in SEGMENT_TYPES and isinstance(n.get("duration_ms"), int)}


def _success_rate(runs: list[dict]) -> float | None:
    if not runs:
        return None
    return round(sum(1 for r in runs if r.get("status") == "done") / len(runs), 3)


def _ts_after(ts: str, cutoff_epoch: float) -> bool:
    try:
        t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        return t >= cutoff_epoch
    except Exception:
        return True  # 解析不了的不因时间窗过滤掉
