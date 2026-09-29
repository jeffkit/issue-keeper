"""契约变更提案（L3 经验闭环）：观测数据 → 可执行的 per-repo 契约调整。

数据源是 metrics（bridge MetricsRecorder 落盘的节点级记录）。确定性规则把
「反复发生的问题」翻译成「数值类契约变更提案」，人批准后一键应用：
  - R1 门超时（gate exit_code=124）≥1 次 → 该门 timeout_secs 提高 50%
  - R2 agent 段超时 ≥2 次 → timeout_overrides[<段>] 提高 50%
  - R3 某门连续失败 ≥3 次且从未通过 → manual 提案（查门命令/环境，不改数值）
数值类提案应用时只改 config.yaml 对应数字：先备份、后写回、写回后 load_config
校验不过即回滚。结构类问题永远落 manual，由人工改。
"""

from __future__ import annotations

import json
import pathlib
import time

from . import metrics

PROPOSALS_DIR = pathlib.Path("~/.issue-keeper/pipeline/proposals").expanduser()

SEGMENT_KEYS = ("investigate", "plan", "implement", "review",
                "fix_review", "fix_test", "document")


def _load_pending(dir_path: pathlib.Path | None = None) -> list[dict]:
    root = dir_path or PROPOSALS_DIR
    out = []
    for p in sorted(root.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _has_pending(existing: list[dict], repo: str, kind: str, target: str) -> bool:
    # 除 rejected 外都算占位：manual/applied 的同类提案不重复生成（问题已有人接/已修）
    return any(p.get("status") in ("pending", "manual", "applied")
               and p.get("repo") == repo and p.get("kind") == kind
               and p.get("target") == target for p in existing)


def _bump(n: int | None, factor: float = 1.5, minimum: int = 60) -> int:
    return max(minimum, int((n or minimum) * factor))


def generate(days: int = 7, metrics_dir: pathlib.Path | None = None,
             dir_path: pathlib.Path | None = None) -> list[dict]:
    """扫观测窗，产出（去重后的）新提案并落盘。返回本次新建的提案。"""
    root = dir_path or PROPOSALS_DIR
    root.mkdir(parents=True, exist_ok=True)
    existing = _load_pending(root)
    created: list[dict] = []

    def _add(repo: str, kind: str, target: str, current, proposed,
             reason: str, evidence: list[str], manual: bool = False) -> None:
        if _has_pending(existing + created, repo, kind, target):
            return
        prop = {
            "id": f"{time.strftime('%Y%m%d-%H%M%S')}-{len(created)+1}",
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "repo": repo, "kind": kind, "target": target,
            "current": current, "proposed": proposed,
            "reason": reason, "evidence": evidence[:10],
            "status": "manual" if manual else "pending",
        }
        (root / f"{prop['id']}.json").write_text(
            json.dumps(prop, ensure_ascii=False, indent=1), encoding="utf-8")
        created.append(prop)

    # 按仓聚合本窗证据
    per_repo: dict[str, list[dict]] = {}
    for rec in metrics.iter_runs(days=days, metrics_dir=metrics_dir):
        per_repo.setdefault(rec.get("repo") or "(unknown)", []).append(rec)

    for repo, runs in per_repo.items():
        for rec in runs:
            nodes = rec.get("nodes") or []
            eid = rec.get("execution_id") or ""
            for n in nodes:
                # R1 门超时：GateNode 超时自杀返回 exit_code=124
                if n.get("type") == "gate" and n.get("exit_code") == 124:
                    gate_name = n.get("gate") or n.get("id")
                    _add(repo, "raise_gate_timeout", gate_name,
                         current={"timeout_secs": None},
                         proposed={"factor": 1.5},
                         reason=f"门 {gate_name} 在 {days} 天内超时被杀（exit 124）",
                         evidence=[eid])
                # R2 agent 段超时：节点 error 且标记 timed_out
                if (n.get("type") == "agentrun" and n.get("timed_out")
                        and n.get("id") in SEGMENT_KEYS):
                    seg = n["id"]
                    seen = [x for x in per_repo[repo]
                            for y in (x.get("nodes") or [])
                            if y.get("type") == "agentrun" and y.get("id") == seg
                            and y.get("timed_out")]
                    if len(seen) >= 2:
                        _add(repo, "raise_segment_timeout", seg,
                             current={"timeout_overrides[seg]": None},
                             proposed={"factor": 1.5},
                             reason=f"段 {seg} 在 {days} 天内超时 {len(seen)} 次",
                             evidence=[x.get("execution_id") or "" for x in seen])
        # R3 门连续失败且从未通过（窗口内，每仓判一次）
        gate_stats: dict[str, list[bool]] = {}
        for rec2 in runs:
            for n in (rec2.get("nodes") or []):
                if n.get("type") == "gate" and n.get("gate"):
                    gate_stats.setdefault(n["gate"], []).append(n.get("passed") is True)
        for gate_name, results in gate_stats.items():
            if len(results) >= 3 and not any(results):
                _add(repo, "gate_never_passes", gate_name,
                     current={"fails": len(results)},
                     proposed=None,
                     reason=f"门 {gate_name} 窗口内失败 {len(results)} 次且从未通过"
                            "——查门命令/环境/依赖安装，属结构类问题需人工",
                     evidence=[], manual=True)
    return created


def list_proposals(status: str | None = None,
                   dir_path: pathlib.Path | None = None) -> list[dict]:
    props = sorted(_load_pending(dir_path), key=lambda p: p.get("created") or "", reverse=True)
    if status:
        props = [p for p in props if p.get("status") == status]
    return props


def _patch_repo_block(text: str, repo: str, fn) -> tuple[str, bool]:
    """在 config.yaml 文本里定位 repo 的 pipeline_repos 块并原地变换；返回 (新文本, 是否改动)。"""
    marker = f'  "{repo}":'
    start = text.find(marker)
    if start == -1:
        raise ValueError(f"config 里没有 {repo} 的 pipeline_repos 条目")
    # 块终点：下一个同层级仓键或段外的下一个顶级键
    next_candidates = [i for i in (text.find('\n  "', start + 1),
                                   text.find("\n# 作者", start + 1),
                                   text.find("\nauthor_allowlist:", start + 1)) if i != -1]
    end = min(next_candidates) if next_candidates else len(text)
    block = text[start:end]
    new_block = fn(block)
    if new_block == block:
        return text, False
    return text[:start] + new_block + text[end:], True


def _patch_gate_timeout(block: str, gate: str, factor: float) -> str:
    """把 `<gate>` 门条目下的 timeout_secs 乘 factor（找不到该门则原样返回）。"""
    lines = block.splitlines(keepends=True)
    in_gate = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- name:"):
            in_gate = stripped.split("name:", 1)[1].strip() == gate
            continue
        if in_gate and stripped.startswith("timeout_secs:"):
            cur = int(stripped.split(":", 1)[1].strip())
            lines[i] = line.replace(f"timeout_secs: {cur}",
                                    f"timeout_secs: {_bump(cur, factor)}")
            break
    return "".join(lines)


def _patch_segment_timeout(block: str, seg: str, factor: float) -> str:
    """提高 timeout_overrides[seg]；无 overrides 段则插入到 gates: 之前。"""
    lines = block.splitlines(keepends=True)
    in_overrides = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("timeout_overrides:"):
            in_overrides = True
            continue
        if in_overrides:
            if stripped.startswith(f"{seg}:"):
                cur = int(stripped.split(":", 1)[1].strip())
                lines[i] = line.replace(f"{seg}: {cur}", f"{seg}: {_bump(cur, factor)}")
                return "".join(lines)
            if stripped.startswith(("gates:", "review_notes:", "triage_notes:", "doc_notes:",
                                    "test_command:", "setup_command:", "push_mode:", "base_branch:")
                                   ) or (stripped.startswith("- ") and not line.startswith("        ")):
                # overrides 段结束且没有该键：插入
                indent = "      "
                lines.insert(i, f"{indent}{seg}: 900  # proposal: 基线默认（原值未显式配置）\n")
                return "".join(lines)
    # 没有 timeout_overrides 段：插在 gates: 前，或块尾
    for i, line in enumerate(lines):
        if line.strip().startswith("gates:"):
            lines.insert(i, f"    timeout_overrides:\n      {seg}: 900  # proposal: 基线默认\n")
            return "".join(lines)
    return block + f"    timeout_overrides:\n      {seg}: 900  # proposal: 基线默认\n"


def apply_proposal(pid: str, config_path: str, factor: float = 1.5,
                   dir_path: pathlib.Path | None = None) -> dict:
    """应用数值类提案：备份 → 改数字 → load_config 校验（失败回滚）。"""
    root = dir_path or PROPOSALS_DIR
    path = root / f"{pid}.json"
    prop = json.loads(path.read_text(encoding="utf-8"))
    if prop.get("status") != "pending":
        return {"applied": False, "reason": f"提案状态为 {prop.get('status')}，不可应用"}

    config = pathlib.Path(config_path).expanduser()
    text = config.read_text(encoding="utf-8")
    backup = config.with_suffix(f".yaml.bak-proposal-{pid}")
    backup.write_text(text, encoding="utf-8")

    kind, repo, target = prop["kind"], prop["repo"], prop["target"]
    try:
        if kind == "raise_gate_timeout":
            new_text, changed = _patch_repo_block(text, repo,
                                                  lambda b: _patch_gate_timeout(b, target, factor))
        elif kind == "raise_segment_timeout":
            new_text, changed = _patch_repo_block(text, repo,
                                                  lambda b: _patch_segment_timeout(b, target, factor))
        else:
            return {"applied": False, "reason": f"{kind} 是结构类提案，请人工处理"}
    except ValueError as e:
        return {"applied": False, "reason": str(e)}

    if not changed:
        return {"applied": False, "reason": "未找到可修改的目标（门名不匹配或已是目标值）"}

    config.write_text(new_text, encoding="utf-8")
    try:
        # 写回后必须能通过完整配置校验，否则回滚
        from .config import load_config
        load_config(str(config))
    except Exception as e:  # noqa: BLE001
        config.write_text(text, encoding="utf-8")
        return {"applied": False, "reason": f"写回后校验失败已回滚: {e}"}

    prop["status"] = "applied"
    prop["applied_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    prop["backup"] = str(backup)
    path.write_text(json.dumps(prop, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"applied": True, "backup": str(backup)}


def reject_proposal(pid: str, dir_path: pathlib.Path | None = None) -> dict:
    root = dir_path or PROPOSALS_DIR
    path = root / f"{pid}.json"
    prop = json.loads(path.read_text(encoding="utf-8"))
    prop["status"] = "rejected"
    prop["rejected_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path.write_text(json.dumps(prop, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"rejected": True}
