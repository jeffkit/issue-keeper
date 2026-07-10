"""issue-keeper 入口。

用法:
  python -m issue_keeper --config config.yaml --once      # 一次性扫描
  python -m issue_keeper --config config.yaml             # daemon 轮询
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .keeper import run_daemon, run_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="issue-keeper", description="监控 GitHub issue 并调用本地 bridge profile agent 处理")
    parser.add_argument("--config", "-c", required=True, help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="只执行一轮扫描后退出（默认常驻轮询）")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"加载配置失败: {e}", file=sys.stderr)
        return 2

    if not config.repos:
        print("配置里没有绑定任何仓库 (repos 为空)", file=sys.stderr)
        return 2

    if args.once:
        handled = run_once(config)
        print(f"完成，本轮处理 {handled} 条。")
        return 0

    run_daemon(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
