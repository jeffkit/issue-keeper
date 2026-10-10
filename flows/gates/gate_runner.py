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
      {"name": "fmt", "command": "cargo fmt --all --check", "timeout_secs": 300,
       "autofix": "cargo fmt --all"},
      {"name": "tui-mutants",
       "command": "bash .dev/scripts/tui-mutants.sh", "timeout_secs": 3600,
       "paths": ["crates/recursive-tui/**"]}
    ]}
autofix（可选）：门失败时先跑该命令再重检一次，通过则记 PASS（autofix 后重检
通过）；autofix 自身失败则保留原失败并附 autofix 输出。只配机械可修的门。

判定「本次改动」：未提交改动（diff HEAD + untracked）∪ 分支已提交（diff
origin/<base>...HEAD）——门跑在未提交工作区上，路径条件必须两边都看。

输出：每门一行 PASS/FAIL/SKIP(未触及)/TIMEOUT，stdout/stderr 尾部留档；跑门之前
先做**命令头预检**（PRECHECK，见 preflight）——命令头在本次执行环境解析不到
可执行文件就整体失败，一门都不跑（环境缺工具，改仓内代码修不了）。
退出码：0 全过；1 有门失败/超时/预检不过（GATE 节点按退出码判 passed）。
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import pathlib
import re
import shlex
import shutil
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


# bash 内建命令与关键字：`bash -c` 自己解析，不落 PATH，`command -v` 探不到是正常的
_SHELL_KEYWORDS = {
    "alias", "bg", "bind", "break", "builtin", "caller", "cd", "command", "compgen",
    "complete", "compopt", "continue", "declare", "dirs", "disown", "echo", "enable",
    "eval", "exec", "exit", "export", "false", "fc", "fg", "getopts", "hash", "help",
    "history", "jobs", "kill", "let", "local", "logout", "mapfile", "popd", "printf",
    "pushd", "pwd", "read", "readarray", "readonly", "return", "set", "shift", "shopt",
    "source", "suspend", "test", "time", "times", "trap", "true", "type", "typeset",
    "ulimit", "umask", "unalias", "unset", "wait", "if", "then", "else", "elif", "fi",
    "case", "esac", "for", "select", "while", "until", "do", "done", "in", "coproc",
    "function", ".", ":", "[", "]", "[[", "]]", "{", "}", "!", "(", ")", "((",
}
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _split_segments(command: str) -> list[tuple[str, bool]] | None:
    """按**顶层** `&&` / `||` / `;` / `|` / 换行切段，返回 [(段文本, 是否不可信)]。

    顶层 = 引号外且不在 `$(…)` / `(…)` / 反引号里：引号与替换里的分隔符只是普通
    字符（`grep -qE "fix|feat" f` 是一个段，不是两段）。段内出现引号外的命令替换/
    子 shell / 反引号时 token 化结果不可信（`FOO=$(a && b) cargo test` 的段界与
    词界都被吃掉），标 True 由调用方跳过。整体引号不配（段界无从判断）时返回 None。

    为什么这么小心（#21 复核实测）：早先按正则无脑切分，`feat"`、`done'`、
    `print(2)"'` 这类残片会被当成可执行头，于是一个工具齐备的门在预检阶段整体
    失败（exit 1、一门不跑），比漏探贵得多。
    """
    segments: list[tuple[str, bool]] = []
    buf: list[str] = []
    quote = ""
    depth = 0
    opaque = False
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and quote != "'":  # 单引号内反斜杠是普通字符
            buf.append(command[i:i + 2])
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = ""
            buf.append(ch)
            i += 1
            continue
        if ch in "'\"`":
            opaque = opaque or ch == "`"
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if command.startswith("$(", i):
            opaque = True
            depth += 1
            buf.append("$(")
            i += 2
            continue
        if ch == "(":
            opaque = True
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue
        if not depth and (command.startswith("&&", i) or command.startswith("||", i)):
            segments.append(("".join(buf), opaque))
            buf, opaque = [], False
            i += 2
            continue
        if not depth and ch in ";|\n":
            segments.append(("".join(buf), opaque))
            buf, opaque = [], False
            i += 1
            continue
        buf.append(ch)
        i += 1
    if quote or depth:
        return None
    segments.append(("".join(buf), opaque))
    return segments


def command_heads(command: str) -> list[str]:
    """shell 命令串里各段简单命令的可执行头。

    段界见 `_split_segments`；每段跳过前导 `VAR=val` 环境前缀与 shell 关键字
    （`cd x && FOO=1 cargo test` → ["cargo"]）。段界不可信（引号不配）或段
    token 化不了时**跳过**——预检是尽力而为的，漏探只是少一道提示，而从残片里
    编出的 head 会把好门拦死，两害相权取轻。
    """
    segments = _split_segments(str(command or ""))
    if segments is None:
        return []
    heads: list[str] = []
    for segment, opaque in segments:
        if opaque:
            continue
        segment = segment.strip()
        if not segment:
            continue
        try:
            toks = shlex.split(segment, posix=True)
        except ValueError:
            continue
        while toks and _ENV_ASSIGN_RE.match(toks[0]):
            toks = toks[1:]
        if not toks:
            continue
        head = toks[0]
        if head in _SHELL_KEYWORDS or head.startswith("$") or head.startswith("("):
            continue
        heads.append(head)
    return heads


def head_problem(head: str, cwd: str) -> str:
    """命令头在本执行环境不可用的原因；空串 = 可用（PATH 里的可执行文件，或存在
    且有执行位的路径）。"""
    if os.sep in head or head.startswith("~"):
        p = pathlib.Path(head).expanduser()
        if not p.is_absolute():
            p = pathlib.Path(cwd) / p
        if not p.exists():
            return "找不到"
        return "" if os.access(p, os.X_OK) else "存在但没有执行权限"
    return "" if shutil.which(head) is not None else "找不到"


def preflight(gates: list[dict], changed: set[str], cwd: str) -> list[tuple[str, str, str, str]]:
    """跑任何门之前，探测各门命令头在本执行环境能否解析。

    为什么（#21，2026-10-08 实证）：plaita 的 lint 门模板写 `uvx ruff check …`，
    而 AGS 沙箱镜像里没有 uvx（宿主有、沙箱没有——门跑在沙箱里，任何宿主侧的检查
    都拦不住）——每单都跑到 g1 才 `exit=127 command not found`，再被当"代码失败"
    烧掉一轮 fix-loop（20-40min agent 预算）。工具装没装是**执行环境契约**（沙箱/
    宿主镜像的属性），与本次改动无关，所以在执行环境里一次全探清、立刻失败，并
    明确标注「环境缺工具，改代码修不了」。

    收益边界（如实）：跨门一次报全所有缺失工具，并把失败从"测试红了"里摘出来
    单独标明环境问题（否则 `break` 只让人看见第一个撞墙的门，且混在输出里难辨）。
    它**不**省掉 fix-loop——GATE 节点只看退出码，flow 侧尚未消费 PRECHECK 标记，
    fix_test/retest/「两轮未过」仍按原路径跑；那需要在 flow 里按标记短路。

    只探会真正跑的门：paths 未触及的条件门不探（其工具不必在场）。autofix 也
    不探——它只在门已失败后才跑，探它会凭空制造预检失败。
    返回 [(门名, 命令, 命令头, 不可用原因)]，空 = 全部可解析。
    """
    missing: list[tuple[str, str, str, str]] = []
    for gate in gates:
        if not gate_applies(gate, changed):
            continue
        name = str(gate.get("name") or "gate")
        command = str(gate.get("command") or "")
        for head in command_heads(command):
            reason = head_problem(head, cwd)
            if reason:
                missing.append((name, command, head, reason))
    return missing


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
    missing = preflight(gates, changed, cwd)
    if missing:
        print("PRECHECK FAIL: 门命令的执行头在本执行环境不可解析"
              "（镜像/宿主缺工具——环境问题，不是被测代码失败）")
        for name, command, head, reason in missing:
            print(f"  - {name}: {reason} `{head}`；命令={command}")
        print("修法：在该执行环境装上工具，或把门命令改成环境已有的等价形式"
              "（如 `uvx <tool> …` → `python3 -m <tool> …`）后重派。"
              "别指望 fix-loop：改仓内代码修不了环境缺工具。")
        return 1
    failures = 0
    for gate in gates:
        name = str(gate.get("name") or "gate")
        if not gate_applies(gate, changed):
            print(f"SKIP {name}: 未触及 paths {gate.get('paths')}")
            continue
        print(f"== {name} ==")
        status, detail = run_gate(gate, cwd)
        # 确定性自愈（2026-09-30 #70）：fmt 这类机械可修的门失败时，先跑 autofix
        # 再重检一次——别把 fix-loop 的 LLM 预算烧在 `cargo fmt` 能解决的事情上。
        # 只给 fmt 配；clippy/test 等语义门不配，仍走 fix-loop。
        fixed_note = ""
        autofix = str(gate.get("autofix") or "").strip()
        if status != "ok" and autofix:
            print(f"AUTO-FIX {name}: {autofix}")
            fx_status, fx_detail = run_gate(
                {"name": f"{name}:autofix", "command": autofix,
                 "timeout_secs": max(30, int(gate.get("autofix_timeout_secs", 600)))},
                cwd)
            if fx_status == "ok":
                fixed_note = "（autofix 后重检通过）"
                status, detail = run_gate(gate, cwd)
            else:
                detail = f"autofix 自身失败:\n{fx_detail}\n--- 原门失败 ---\n{detail}"
        if status == "ok":
            print(f"PASS {name}{fixed_note}")
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
