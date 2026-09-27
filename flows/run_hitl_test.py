#!/usr/bin/env python3
"""HITL 独立验证：最小 flow（start → hitl → end），直连 127.0.0.1:8081。"""
import json
import sys

sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita")
sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita-nodes/src")

import plaita_nodes  # noqa: F401,E402
from plaita.node import register_code_node  # E402

register_code_node(default_backend="subprocess")
from plaita.dsl.ir_validate import build_flow  # E402

IR = {
    "runtime": "python",
    "flow_id": "hitl-mini-test",
    "inputType": {"dataType": "object"},
    "nodes": [
        {"type": "start", "id": "start", "next": "ask"},
        {
            "type": "hitl",
            "id": "ask",
            "message": "【HITL 连通性测试】这是 issue-pipeline 的人工审核链路测试。回复「批准」即代表 HITL 通路正常。",
            "timeout_secs": 600,
            "poll_interval": 5,
        },
        {"type": "end", "id": "out", "output": "$NODE.ask"},
    ],
}
flow = build_flow(IR)
result = flow.run()
print("HITL_RESULT " + json.dumps(result, ensure_ascii=False, default=str))
