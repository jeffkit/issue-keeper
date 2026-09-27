#!/usr/bin/env python3
"""issue-pipeline 端到端真跑（recursive #34）。产物日志末尾打印 RESULT 行。"""
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita")
sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita-nodes/src")

import plaita_nodes  # noqa: F401,E402
from plaita.node import register_code_node  # E402

register_code_node(default_backend="subprocess")

from plaita.dsl.ir_validate import build_flow  # E402

REPO = "jeffkit/recursive"
NUM = 34
MAIN = "/Users/kong/projects/infra4agent/recursive"
ART = "/Users/kong/.issue-keeper/pipeline/recursive-34"

pathlib.Path(ART).mkdir(parents=True, exist_ok=True)
body = subprocess.run(
    ["gh", "issue", "view", str(NUM), "-R", REPO, "--json", "body", "--jq", ".body"],
    capture_output=True, text=True, timeout=60,
).stdout
pathlib.Path(ART, "00-issue.md").write_text(body[:16000], encoding="utf-8")

d = json.load(open(ROOT / "issue-pipeline.flow.json"))
flow = build_flow(d)
result = flow.run(
    repo_full=REPO,
    issue_number=NUM,
    title="docs: 新增 docs/llm-gateway-compat.md 汇总 LLM 网关兼容约束（#15/#16/#17 教训）",
    author="kongjie",
    body_file=f"{ART}/00-issue.md",
    screener_verdict="safe",
    main_clone=MAIN,
    worktree_dir=f"{MAIN}/.worktrees/e2e-issue-34",
    branch_name="e2e/issue-34",
    artifact_dir=ART,
    test_command="",
    review_mode="auto",
    push_mode="branch",
)
print("RESULT " + json.dumps(result, ensure_ascii=False, default=str))
