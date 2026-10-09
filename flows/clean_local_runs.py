#!/usr/bin/env python3
"""clean_local_runs.py —— 清扫**本机** `<repo>/.flowcast/runs/` 下陈旧的 run worktree。

为什么需要（2026-10-09 根因复盘）：
- `disk-hygiene` flow 的清理脚本硬编码 `/home/ubuntu/...` 并经 ssh 执行——**只清 VM**；
  而 recursive/im-agentproc/plaita 的 run 实际跑在 Mac（Mac 上跑 ctrl + v2 worker）。
- 原有终态回收（self_improve_bridge_v2._recycle_resumed_worktree）只在 run 经 bridge
  走到终态时触发；卡在退避/重试循环里的 run 的 worktree 永久泄漏。
- 于是 Mac 侧累积到 152G（recursive 单仓 108G），可用空间跌破 20G 守卫线 → preflight
  全线挡回、管线停摆 30min+（8 次派发 0 落地）。

用法：
  python3 clean_local_runs.py                  # 只在可用 < min+margin 时清
  python3 clean_local_runs.py --dry-run        # 只报不删
  python3 clean_local_runs.py --force          # 忽略空间闸，强制清 >keep_hours
输出：JSON {"free_before","free_after","removed","scanned","skipped_reason"}
安全：只删 mtime > keep_hours 的 run 目录（默认 24h，活跃 run 不可能命中）；
      不碰 ~/.cargo、不碰 iOS 模拟器、不碰仓库工作树本身。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import time

ROOT = pathlib.Path.home() / "projects" / "infra4agent"
# 红线：绝不进入/删除的路径片段
FORBID = (".cargo", "Library/Developer/CoreSimulator", ".git/objects")


def _free_gib(path: str = "/") -> float:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 2 ** 30


def _dir_size(path: pathlib.Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-hours", type=float, default=24.0)
    ap.add_argument("--min-free-gib", type=float, default=20.0, help="守卫线（与 preflight 对齐）")
    ap.add_argument("--margin-gib", type=float, default=5.0, help="低于 守卫线+余量 才动手")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略空间闸")
    args = ap.parse_args()

    free_before = _free_gib()
    out = {"free_before": round(free_before, 1), "removed": 0, "freed_gib": 0.0,
           "scanned": 0, "skipped_reason": "", "removed_dirs": []}

    if not args.force and free_before >= args.min_free_gib + args.margin_gib:
        out["skipped_reason"] = "free %.1fG >= 线%.0f+余量%.0f，无需清理" % (
            free_before, args.min_free_gib, args.margin_gib)
        print(json.dumps(out, ensure_ascii=False))
        return 0

    cutoff = time.time() - args.keep_hours * 3600
    if not ROOT.is_dir():
        out["skipped_reason"] = "根目录不存在 %s" % ROOT
        print(json.dumps(out, ensure_ascii=False))
        return 0

    for base in sorted(ROOT.iterdir()):
        d = base / ".flowcast" / "runs"
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if not p.is_dir():
                continue
            sp = str(p)
            if any(bad in sp for bad in FORBID):
                continue
            out["scanned"] += 1
            try:
                if p.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            sz = _dir_size(p)
            if args.dry_run:
                out["removed"] += 1
                out["freed_gib"] = round(out["freed_gib"] + sz / 2 ** 30, 2)
                out["removed_dirs"].append(p.name)
                continue
            try:
                shutil.rmtree(p)
                out["removed"] += 1
                out["freed_gib"] = round(out["freed_gib"] + sz / 2 ** 30, 2)
                if len(out["removed_dirs"]) < 20:
                    out["removed_dirs"].append(p.name)
            except OSError as e:
                print("warn: 跳过 %s: %s" % (sp, str(e)[:80]), file=sys.stderr)

    out["free_after"] = round(_free_gib(), 1)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
