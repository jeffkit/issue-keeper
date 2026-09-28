#!/usr/bin/env python3
"""keeper ↔ issue-pipeline 桥。

stdin 收 input JSON（契约见 flows/README.md），stdout 输出一行：
    RESULT {json}

每次运行追加台账 ~/.issue-keeper/pipeline/runs.jsonl（supervisor 巡检数据源）。
由 keeper 以子进程方式调用（start_new_session + 超时 killpg，见 keeper._invoke_pipeline）。
"""
import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita")
sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita-nodes/src")

import plaita_nodes  # noqa: F401,E402
from plaita.node import register_code_node  # E402

register_code_node(default_backend="subprocess")

from plaita.dsl.ir_validate import build_flow  # E402

LEDGER = pathlib.Path("~/.issue-keeper/pipeline/runs.jsonl").expanduser()


def append_ledger(record: dict) -> None:
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass  # 台账失败不影响主管线


def normalize_result(result: dict) -> dict:
    """key 归一化：早退路径历史返回 posted，成功路径返回 comment_posted——
    在出口统一补齐别名，keeper 兜底判定只看 comment_posted，
    flow 新增早退路径不必各写各的（issue #1「未发出回评」误报根因）。"""
    if "comment_posted" not in result:
        result["comment_posted"] = result.get("posted", False)
    return result


def main() -> None:
    t0 = time.time()
    payload = json.load(sys.stdin)
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    flow = build_flow(json.load((HERE / "issue-pipeline.flow.json").open()))
    try:
        result = flow.run(**payload)
        result = result if isinstance(result, dict) else {"status": str(result)}
        ok = True
    except Exception as e:  # 引擎层异常（含超时）：结构化为失败结果，让 keeper 兜底回评
        result = {"status": "engine_error", "error": str(e)[:500]}
        ok = False

    result = normalize_result(result)

    append_ledger({
        "ts": started,
        "repo": payload.get("repo_full"),
        "issue": payload.get("issue_number"),
        "author": payload.get("author"),
        "status": result.get("status"),
        "comment_posted": result.get("comment_posted"),
        "pushed": result.get("pushed"),
        "ok": ok,
        "duration_secs": round(time.time() - t0, 1),
    })
    print("RESULT " + json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
