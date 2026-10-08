"""benchmark 数据集（L4）：从观测与产物沉淀可复用的评测集。

复刻 screener 已验证的「版本 + 评测集 + 人签发布」模式：
  - build：从 metrics + artifacts 抽 case，auto-label 来自 run 结果，
    正文过消毒（reply.redact_text）再入集，带 provenance（execution_id/flow_version）
  - label：人工抽检纠正 auto-label（纠正即金标，labeled_by=human）
  - eval：用数据集冻结的提示词回放 triage 段（真实 LLM 单段调用），按 expected 打分

存储：~/.issue-keeper/benchmarks/<name>/v<N>.jsonl（cases）+ manifest.json。
jsonl 便于追加与 diff；manifest 记 schema/构建口径/得分历史。
"""

from __future__ import annotations

import json
import pathlib
import time
from collections import Counter

from . import metrics
from .reply import sanitize as redact_text

BENCH_ROOT = pathlib.Path("~/.issue-keeper/benchmarks").expanduser()
SCHEMA = 1

# triage 三值判定空间（与 flow 的 PARSE_JSON choices 一致）
TRIAGE_CHOICES = ("actionable", "blocked", "invalid")

# 冻结进 manifest 的评测提示词模板（与 flow triage 段语义对齐；case 字段注入）。
# 刻意与 flow 源码解耦：数据集版本固定提示词，跨版本评测才可比。
EVAL_TRIAGE_PROMPT = (
    "你是 issue 管线分诊员。仓库 {repo}，issue #{issue}，标题《{title}》。\n"
    "正文：\n{body}\n\n"
    "依赖预检结果：{deps}\n\n"
    "判定规则：已在 main 修复或无需改动 → invalid；已有 PR/分支在途 → blocked；"
    "依赖其他 issue/PR 未合入 → blocked；其余 → actionable。\n"
    "只输出一行严格 JSON："
    '{{"verdict":"actionable|blocked|invalid","notes":"..."}}'
)


def _artifact_dir(repo: str, issue) -> pathlib.Path:
    slug = str(repo).split("/")[-1]
    return pathlib.Path(f"~/.issue-keeper/pipeline/{slug}-{issue}").expanduser()


def _redact(v):
    return redact_text(v) if isinstance(v, str) else v


def build_triage(days: int = 60, name: str = "triage",
                 root: pathlib.Path | None = None) -> dict:
    """从观测窗构建 triage 数据集新版本。auto-label 规则：
    done→actionable；blocked→blocked；invalid→invalid；其余（partial/abort/
    readonly 等）不自动标（expected=None，进人工标注队列）。"""
    root = root or BENCH_ROOT
    cases: list[dict] = []
    seen: set[tuple] = set()
    for rec in metrics.iter_runs(days=days):
        repo = rec.get("repo")
        issue = rec.get("issue")
        key = (repo, issue)
        if key in seen or not repo or issue is None:
            continue  # 同一 issue 取最新一条 run
        seen.add(key)
        status = rec.get("status")
        if status not in ("done", "blocked", "invalid"):
            expected = None       # 人工队列
        else:
            expected = "actionable" if status == "done" else status
        nodes = rec.get("nodes") or []
        triage_verdict = next((n.get("verdict") for n in nodes if n.get("id") == "parsed"), None)
        art = _artifact_dir(repo, issue)
        body_file = art / "00-issue.md"
        title = ""
        deps = "[]"
        try:
            body = body_file.read_text(encoding="utf-8")
        except OSError:
            continue  # 产物已清理的 run 跳过
        deps_node = next((n for n in nodes if n.get("id") == "deps"), None)
        dispatch = art / "dispatch.json"
        if dispatch.exists():
            try:
                title = json.loads(dispatch.read_text(encoding="utf-8")).get("title") or ""
            except Exception:
                pass
        if deps_node is not None:
            out = deps_node.get("output")
            deps = json.dumps(out.get("deps_json"), ensure_ascii=False) if isinstance(out, dict) else "[]"
        cases.append({
            "id": f"{str(repo).replace('/', '-')}-{issue}",
            "repo": repo, "issue": issue, "title": _redact(title),
            "body": _redact(body[:8000]),
            "deps_json": deps,
            "expected": expected,
            "predicted_at_run": triage_verdict,   # 当时管线的判定（对照用）
            "labeled_by": "auto" if expected else None,
            "provenance": {"execution_id": rec.get("execution_id"),
                           "flow_version": rec.get("flow_version")},
        })

    # 版本目录
    name_dir = root / name
    name_dir.mkdir(parents=True, exist_ok=True)
    versions = sorted(int(p.stem[1:]) for p in name_dir.glob("v*.jsonl"))
    version = (versions[-1] + 1) if versions else 1
    vfile = name_dir / f"v{version}.jsonl"
    with vfile.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    manifest = _read_manifest(name_dir) or {"name": name, "schema": SCHEMA, "versions": {}}
    manifest["versions"][str(version)] = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "window_days": days, "count": len(cases),
        "by_expected": dict(Counter(str(c["expected"]) for c in cases)),
        "eval_prompt": EVAL_TRIAGE_PROMPT,
        "scores": manifest.get("versions", {}).get(str(version), {}).get("scores", []),
    }
    _write_manifest(name_dir, manifest)
    return {"name": name, "version": version, "count": len(cases), "path": str(vfile)}


def latest_version(name: str, root: pathlib.Path | None = None) -> int | None:
    name_dir = (root or BENCH_ROOT) / name
    versions = sorted(int(p.stem[1:]) for p in name_dir.glob("v*.jsonl"))
    return versions[-1] if versions else None


def load_cases(name: str, version: int | None = None,
               root: pathlib.Path | None = None) -> list[dict]:
    v = version or latest_version(name, root)
    if v is None:
        return []
    vfile = (root or BENCH_ROOT) / name / f"v{v}.jsonl"
    if not vfile.exists():
        return []
    out = []
    for line in vfile.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def label_case(name: str, case_id: str, expected: str, version: int | None = None,
               root: pathlib.Path | None = None) -> dict:
    """人工纠正标注：写入金标（labeled_by=human）。"""
    if expected not in TRIAGE_CHOICES + (None,):
        raise ValueError(f"expected 只能是 {TRIAGE_CHOICES} 或 null")
    v = version or latest_version(name, root)
    vfile = (root or BENCH_ROOT) / name / f"v{v}.jsonl"
    cases = load_cases(name, v, root)
    hit = None
    for c in cases:
        if c["id"] == case_id:
            c["expected"] = expected
            c["labeled_by"] = "human"
            hit = c
        # 整文件重写
    if hit is None:
        raise ValueError(f"找不到 case {case_id}")
    with vfile.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    return {"labeled": True, "case": case_id, "expected": expected, "version": v}


def eval_triage(name: str, version: int | None = None, agent: str = "glm-turbo",
                limit: int = 30, root: pathlib.Path | None = None) -> dict:
    """回放评测：对有金标的 case 用冻结提示词跑一次 triage agent，按 expected 打分。

    需要本机 agentproc agents.json 与 LLM 凭据（与管线同一套）。无金标 case 跳过。
    """
    cases = [c for c in load_cases(name, version, root) if c.get("expected")]
    cases = cases[:limit]
    if not cases:
        return {"scored": 0}

    from plaita_nodes.agent_run import AgentRunNode

    class _Exec:  # 最小执行上下文：字段都是字面量
        express_prefix = "$"
        def evaluate(self, v):
            return v
        def get_global_variable(self, k, default=None):
            return default

    hits = 0
    results = []
    for c in cases:
        prompt = EVAL_TRIAGE_PROMPT.format(
            repo=c["repo"], issue=c["issue"], title=c["title"],
            body=c["body"][:6000], deps=c.get("deps_json") or "[]")
        node = AgentRunNode(id="eval", agent=agent, prompt=prompt, timeout_secs=180)
        try:
            out = node.execute(_Exec())
            text = out.get("text") or ""
            verdict = None
            for line in reversed(text.splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        verdict = json.loads(line).get("verdict")
                    except Exception:
                        pass
                    break
        except Exception as e:  # noqa: BLE001 —— 单 case 失败记 miss 不中断
            verdict = f"<error: {str(e)[:80]}>"
        ok = verdict == c["expected"]
        hits += int(ok)
        results.append({"id": c["id"], "expected": c["expected"],
                        "verdict": verdict, "ok": ok})

    score = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "agent": agent, "scored": len(results), "accuracy": round(hits / len(results), 3)
        if results else None,
        "by_expected": dict(Counter(r["expected"] for r in results)),
        "misses": [r for r in results if not r["ok"]],
    }
    # 得分写进 manifest 版本历史
    v = version or latest_version(name, root)
    name_dir = (root or BENCH_ROOT) / name
    manifest = _read_manifest(name_dir) or {"name": name, "schema": SCHEMA, "versions": {}}
    manifest.setdefault("versions", {}).setdefault(str(v), {}).setdefault("scores", [])
    manifest["versions"][str(v)]["scores"].append(score)
    _write_manifest(name_dir, manifest)
    return score


def list_datasets(root: pathlib.Path | None = None) -> list[dict]:
    root = root or BENCH_ROOT
    out = []
    if not root.exists():
        return out
    for name_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        manifest = _read_manifest(name_dir)
        if not manifest:
            continue
        out.append({
            "name": manifest.get("name") or name_dir.name,
            "latest_version": latest_version(name_dir.name, root),
            "versions": manifest.get("versions", {}),
        })
    return out


def _read_manifest(name_dir: pathlib.Path) -> dict | None:
    p = name_dir / "manifest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_manifest(name_dir: pathlib.Path, manifest: dict) -> None:
    (name_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
