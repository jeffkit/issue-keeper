#!/usr/bin/env python3
"""flows/ 全部 @flow 源码的**统一编译入口**（plaita CLI 薄壳）。

@flow 源码是审查主体；`*.flow.json` 是编译产物不要手改——改源码后重跑：

    PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
        python3 flows/build_flows.py [--check] [name ...]

`name` 取 FLOWS 表短名（缺省全部编译/校验）。本脚本只做三件事：选 flow、
把大仓相邻的 plaita / plaita-nodes/src 注入 sys.path、逐个透传
`python -m plaita build`。编译、IR 校验（DEFAULT_RULES 硬门）、canonical
序列化、`--check` 字节比对全在 plaita 侧——2026-10-09 起六个 per-flow
build_*.py 样板脚本收敛于此，产物格式统一切到 plaita canonical
（console 正典形态；与旧 compile_source 直出 IR parse 等价，
pipeline_bridge 的 definition 消费两种都吃）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 短名 → (源码, 产物)
FLOWS: dict[str, tuple[str, str]] = {
    "issue-pipeline": ("issue_pipeline_flow.py", "issue-pipeline.flow.json"),
    "issue-accept": ("issue_accept_flow.py", "issue-accept.flow.json"),
    "keeper-watch": ("keeper_watch_flow.py", "keeper-watch.flow.json"),
    "keeper-shadow": ("keeper_shadow_flow.py", "keeper-shadow.flow.json"),
    "ctrl-watch": ("ctrl_watch_flow.py", "ctrl-watch.flow.json"),
    "sandbox-watch": ("sandbox_watch_flow.py", "sandbox-watch.flow.json"),
    "pipeline-patrol": ("pipeline_patrol_flow.py", "pipeline-patrol.flow.json"),
    "inflight-watch": ("inflight_watch_flow.py", "inflight-watch.flow.json"),
    "disk-hygiene": ("disk_hygiene_flow.py", "disk-hygiene.flow.json"),
}


def _bootstrap_syspath() -> None:
    """注入大仓相邻的 plaita / plaita-nodes/src（显式注册，不依赖 pip
    dist-info entry-points 新鲜度）。大仓根取 INFRA4AGENT_ROOT，缺省按本仓
    位置上溯。"""
    root = os.environ.get("INFRA4AGENT_ROOT") or str(
        HERE.parent.parent)  # flows → issue-keeper → 大仓根
    for rel in ("plaita", "plaita-nodes/src"):
        p = str(Path(root) / rel)
        if Path(p).is_dir() and p not in sys.path:
            sys.path.insert(0, p)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="issue-keeper flows 统一编译（plaita CLI 薄壳）")
    ap.add_argument("names", nargs="*", choices=sorted(FLOWS),
                    help="编哪些 flow（缺省全部）")
    ap.add_argument("--check", action="store_true",
                    help="不落盘；校验现有产物与重编译结果逐字节一致")
    args = ap.parse_args(argv)

    _bootstrap_syspath()
    from plaita.cli import main as plaita_main

    names = args.names or sorted(FLOWS)
    worst = 0
    for name in names:
        src, out = FLOWS[name]
        cmd = ["build", str(HERE / src), "-o", str(HERE / out),
               "--register", "plaita_nodes", "--code-backend", "subprocess",
               "--embed-source"]
        if args.check:
            cmd.append("--check")
        rc = plaita_main(cmd)
        worst = max(worst, rc)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
