#!/usr/bin/env python3
"""多门质量门调度器：按 spec 顺序执行若干门，各自预算，diff 路径条件触发。

为什么存在：通用 issue-pipeline 的 GATE 节点是「单命令 + 单预算」模型，装不下
各仓真实验收——recursive 触及 recursive-tui 才要跑 tui-mutants（且要 40-60min
预算）、plaita 有 mutmut、argusai 要 install→build→type-check→test 序列。
本调度器把「门清单」从 flow 定义外置成数据（keeper 按仓配置，或仓内自带
`.issue-keeper/gates.json`），flow 的 GATE 节点只调本脚本。

用法（GATE 节点按 argv 执行、不经 shell，命令写成）：
    python3 flows/gates/gate_runner.py --spec /path/gates.json --cwd <worktree>

spec 格式（{"base": "main", "gates": [...]}，base 可省略默认 main）：
    {"gates": [
      {"name": "fmt", "command": "cargo fmt --all --check", "timeout_secs": 300},
      {"name": "tui-mutants",
       "command": "bash .dev/scripts/tui-mutants.sh", "timeout_secs": 3600,
       "paths": ["crates/recursive-tui/**"]}
    ]}

判定「本次改动」：未提交改动（diff HEAD + untracked）∪ 分支已提交（diff
origin/<base>...HEAD）——门跑在未提交工作区上，路径条件必须两边都看。

输出：每门一行 PASS/FAIL/SKIP(未触及)/TIMEOUT，stdout/stderr 尾部留档。
退出码：0 全过；1 有门失败/超时（GATE 节点按退出码判 passed）。
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import pathlib
import signal
import subprocess
import sys


def _fix_path() -> None:
    """launchd 起的 keeper 没有用户级工具目录（#40 的 FileNotFoundError）。
    与 repo-tests.sh 同款防御。"""
    home = pathlib.Path.home()
    wanted = [str(home / ".cargo" / "bin"), str(home / ".local" / "bin"),
              "/opt/homebrew/bin", "/usr/local/bin"]
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    added = [p for p in wanted if p not in parts and pathlib.Path(p).is_dir()]
    if added:
        os.environ["PATH"] = os.pathsep.join(added + parts)


def _git(args: list[str], cwd: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def changed_files(cwd: str, base: str) -> set[str]:
    """本次 run 的全部改动文件：未提交（含 untracked）+ 分支上已提交 vs base。"""
    files: set[str] = set()
    for line in _git(["diff", "--name-only", "HEAD"], cwd).splitlines():
        if line.strip():
            files.add(line.strip())
    for line in _git(["status", "--porcelain", "--untracked-files=all"], cwd).splitlines():
        path = line[3:].strip().strip('"')
        # 重命名形态 "old -> new" 两边都算触及
        if " -> " in path:
            path = path.split(" -> ")[-1]
        if path:
            files.add(path)
    # 分支上已提交的改动：优先 origin/<base>（管线形态），无远端时退本地 <base>
    # （测试/离线仓）——两点/三点 diff 均可，改动文件集合一致
    committed = _git(["diff", "--name-only", f"origin/{base}...HEAD"], cwd)
    if not committed:
        committed = _git(["diff", "--name-only", f"{base}...HEAD"], cwd)
    for line in committed.splitlines():
        if line.strip():
            files.add(line.strip())
    return files


def gate_applies(gate: dict, changed: set[str]) -> bool:
    paths = [str(p) for p in (gate.get("paths") or []) if str(p).strip()]
    if not paths:
        return True
    for f in changed:
        for p in paths:
            if fnmatch.fnmatch(f, p):
                return True
    return False


def run_gate(gate: dict, cwd: str) -> tuple[str, str]:
    """跑一门。返回 (status, detail)：status ∈ ok|fail|timeout。"""
    name = gate.get("name") or "gate"
    timeout = max(30, int(gate.get("timeout_secs", 900)))
    cmd = ["bash", "-c", str(gate["command"])]  # shell 语义：允许 && 链 / 环境前缀
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except OSError as e:
        return "fail", f"启动失败: {e}"
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        out, err = proc.communicate()
        return ("timeout", f"超过 {timeout}s 预算被杀（输出可能被截断——recursive "
                          f"AGENTS.md 失败模式 #8：截断的门报告会造成误判）")
    detail = ((out or "")[-1500:] + "\n" + (err or "")[-500:]).strip()
    if proc.returncode == 0:
        return "ok", detail
    return "fail", f"exit={proc.returncode}\n{detail}"


def load_spec(spec_path: str | None, cwd: str) -> dict:
    if spec_path:
        return json.loads(pathlib.Path(spec_path).read_text(encoding="utf-8"))
    # 仓内自带：惯例归仓（各仓 AGENTS.md 是契约主人，门清单同理）
    local = pathlib.Path(cwd) / ".issue-keeper" / "gates.json"
    if local.exists():
        return json.loads(local.read_text(encoding="utf-8"))
    raise SystemExit(f"无 --spec 且 {local} 不存在：没有可执行的门清单")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--spec", help="门清单 JSON 路径；缺省找仓内 .issue-keeper/gates.json")
    ap.add_argument("--cwd", default=".", help="worktree 目录（默认当前目录）")
    ap.add_argument("--base", default="", help="基线分支（覆盖 spec 内 base；默认 main）")
    args = ap.parse_args()

    _fix_path()
    cwd = str(pathlib.Path(args.cwd).resolve())
    spec = load_spec(args.spec, cwd)
    base = args.base or str(spec.get("base") or "main")
    gates = spec.get("gates") or []
    if not gates:
        print("FAIL: 门清单为空（无门不进门是 keeper 侧语义；这里收到空清单 = 配置错误）")
        return 1

    changed = changed_files(cwd, base)
    print(f"gates={len(gates)} base=origin/{base} changed_files={len(changed)}")
    failures = 0
    for gate in gates:
        name = str(gate.get("name") or "gate")
        if not gate_applies(gate, changed):
            print(f"SKIP {name}: 未触及 paths {gate.get('paths')}")
            continue
        print(f"== {name} ==")
        status, detail = run_gate(gate, cwd)
        if status == "ok":
            print(f"PASS {name}")
        else:
            failures += 1
            print(f"FAIL {name} ({status})")
            if detail:
                print(detail)
            print(f"↑ 门失败，后续门跳过（先修再跑）")
            break  # 先修再跑：后面的门在坏基线上跑是浪费
    if failures:
        return 1
    print("ALL GATES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
