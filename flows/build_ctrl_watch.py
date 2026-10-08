"""把 ctrl_watch_flow.py（@flow 源码）编译成 flows/ctrl-watch.flow.json。

@flow 源码是审查主体；JSON 只是编译产物，不要手改——改源码后重跑本脚本：
    PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_ctrl_watch.py
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita")
sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita-nodes/src")

import plaita_nodes  # noqa: F401,E402

plaita_nodes.register_all()
from plaita.node import register_code_node  # E402

register_code_node(default_backend="subprocess")
from plaita.dsl.codeflow import compile_source  # noqa: E402


def main() -> None:
    src = (HERE / "ctrl_watch_flow.py").read_text(encoding="utf-8")
    ir = compile_source(src)
    out = HERE / "ctrl-watch.flow.json"
    out.write_text(json.dumps(ir, ensure_ascii=False, indent=2), encoding="utf-8")
    nodes = ir.get("nodes", [])
    kinds = [n.get("type") for n in nodes]
    print(f"ctrl-watch.flow.json：{len(nodes)} 节点 {kinds}")


if __name__ == "__main__":
    main()
