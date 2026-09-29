#!/usr/bin/env python3
"""把 issue_pipeline_flow.py（@flow 源码）编译成 flows/issue-pipeline.flow.json。

@flow 源码是审查主体；JSON 只是编译产物，不要手改——改源码后重跑本脚本：
    PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_issue_pipeline.py
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent

# 注册自定义节点类型（agentrun/gate/capture/writefile/hitl）与 code 节点，
# compile_source 的大写占位符解析依赖 registry。
sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita")
sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita-nodes/src")

import plaita_nodes  # noqa: F401,E402
plaita_nodes.register_all()  # 显式注册全部节点——不依赖 pip dist-info entry-points 新鲜度
from plaita.node import register_code_node  # E402

register_code_node(default_backend="subprocess")

from plaita.dsl.codeflow import compile_source  # noqa: E402


def main() -> None:
    src = (HERE / "issue_pipeline_flow.py").read_text(encoding="utf-8")
    ir = compile_source(src)
    out = HERE / "issue-pipeline.flow.json"
    out.write_text(json.dumps(ir, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    kinds = {}
    for n in ir.get("nodes", []):
        kinds[n["type"]] = kinds.get(n["type"], 0) + 1
    print(f"OK {out.name}: {len(ir.get('nodes', []))} nodes {kinds}")


if __name__ == "__main__":
    main()
