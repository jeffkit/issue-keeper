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
import re
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


#: /tmp 下验证临时目录的命名模式（验证类 agent 自行 clone+构建的产物）。
#: 只匹配这三类**明确是验证副产物**的名字，避免误伤无关的 /tmp 内容。
TMP_PATTERNS = [
    re.compile(r"^i\d+(-target)?$"),   # i181 / i173-target
    re.compile(r"^verify\d+.*$"),      # verify173 / verify184
    re.compile(r"^wt\d+.*$"),          # wt176 / wt183
    re.compile(r"^v\d+(-target)?$"),   # v3 / v183
    re.compile(r"^issue\d+-verify$"),  # issue173-verify
]
# 说明（2026-10-12 补）：`i<NNN>`（无 -target 后缀）是同一类验证工作目录
# ——实测 `/tmp/i181` 单目录 **6.1G**，内含 e2e_compile.log/gates.log，
# 是六轮磁盘告警中最大的一笔。首版模式只写了 `i\d+-target` 因而**漏掉它**
# （clean_local_runs 实跑时 removed 列表里没有 i181）。补为 `i\d+(-target)?`。
# 安全性不靠"名字像不像"，而靠下游**双条件**（mtime 陈旧 + lsof 无占用）：
# 实测 i181 在构建期间 lsof 非空会被 `tmp_skipped_busy` 跳过，构建结束后才可清。


def tmp_enabled() -> bool:
    """清 /tmp 验证目录的开关（默认开；`CLEAN_TMP=0` 关闭便于回滚）。"""
    return os.environ.get("CLEAN_TMP", "1") not in ("0", "false", "no")


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

    # ── /tmp 验证临时目录（2026-10-12 加，连续六轮磁盘告警的根因）──────
    # 现象：VM 反复在 40min~1h 内被顶到守卫线以下，甚至「挡单」。
    # 定位：验证类 agent（`agentproc → claude → bash → timeout → cargo → rustc`）
    # 自行在 /tmp 下 clone+构建，命名形如 `i<NNN>-target` / `verify<NNN>` /
    # `wt<NNN>`；**任务结束即遗留且无回收**，单个可占 6G+。
    #
    # ⚠️ 必须**双条件**，缺一即会误杀在途构建（本轮实测反例：/tmp/i181
    # 6.2G 且 30s 内仍 +5M，删它会打断正在跑的 cargo/rustc）：
    #   ① mtime 陈旧（>keep_hours，默认 24h）；
    #   ② **无进程持有**（lsof 计数为 0）。
    # 只在空间闸触发时执行（与上方 runs 清理同一门槛），平时不做无谓扫描。
    if tmp_enabled():
        import subprocess as _sp
        for p in sorted(pathlib.Path("/tmp").glob("*")):
            if not p.is_dir():
                continue
            if not any(pat.match(p.name) for pat in TMP_PATTERNS):
                continue
            sp = str(p)
            if any(bad in sp for bad in FORBID):
                continue
            try:
                if p.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            out["scanned"] += 1
            # ② 无进程持有：lsof 有输出即跳过（保守，宁可漏清不可误杀）
            try:
                held = _sp.run(["lsof", "-w", "+D", sp], capture_output=True,
                               text=True, timeout=30).stdout.strip()
            except Exception:
                held = "unknown"          # 探测失败 ⇒ 保守跳过
            if held:
                out.setdefault("tmp_skipped_busy", []).append(p.name)
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

    # ── 主仓构建缓存 `<repo>/target`（2026-10-12 加，/tmp 类被管住后的新增长点）──
    # 现象：/tmp 验证目录遗留已被本脚本管住后，VM 的新增长点换成 **主仓 target/**
    # （实测 `recursive/target` 6.8G，内容仅 CACHEDIR.TAG/debug/tmp = 可重建的
    # 构建产物）。
    # 判据与上面完全同款（**双条件**）：mtime 陈旧（> keep_hours）+ **无进程持有**
    # （lsof 计数为 0；探测失败保守跳过）——缺一即会打断正在跑的 cargo/rustc。
    # 只清**一级** `<repo>/target`，不递归（避免误伤 runs 内的 worktree target，
    # 那些随 run 目录整体回收）。
    if ROOT.is_dir():
        for base in sorted(ROOT.iterdir()):
            tgt = base / "target"
            if not tgt.is_dir():
                continue
            sp = str(tgt)
            if any(bad in sp for bad in FORBID):
                continue
            try:
                if tgt.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            out["scanned"] += 1
            try:
                held = _sp.run(["lsof", "-w", "+D", sp], capture_output=True,
                               text=True, timeout=30).stdout.strip()
            except Exception:
                held = "unknown"
            if held:
                out.setdefault("tmp_skipped_busy", []).append(base.name + "/target")
                continue
            sz = _dir_size(tgt)
            if args.dry_run:
                out["removed"] += 1
                out["freed_gib"] = round(out["freed_gib"] + sz / 2 ** 30, 2)
                out["removed_dirs"].append(base.name + "/target")
                continue
            try:
                shutil.rmtree(tgt)
                out["removed"] += 1
                out["freed_gib"] = round(out["freed_gib"] + sz / 2 ** 30, 2)
                if len(out["removed_dirs"]) < 20:
                    out["removed_dirs"].append(base.name + "/target")
            except OSError as e:
                print("warn: 跳过 %s: %s" % (sp, str(e)[:80]), file=sys.stderr)

    out["free_after"] = round(_free_gib(), 1)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
