#!/usr/bin/env bash
# 管线质量门（recursive）。由 flow 的 GATE 节点以 **argv 方式**执行：命令不会被
# shell 解释，所以 `cmd1 && cmd2` 这种写法在 GATE 里必然瞬时失败
# （error: unexpected argument '&&' found）——2026-09-28/29 的每一跑都因此拿到假的
# passed:false，流程一路走 "partial"、永远到不了 deliver/merge。
#
# 约定：cwd = 该 run 的 worktree；退出码即门的结果。
set -uo pipefail
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

echo "== cargo fmt --all --check =="
cargo fmt --all --check || exit 1
# clippy 必须进：2026-09-29 一个 agent 绕过门直接推了 main，挂的正是
# recursive-tui 的 clippy（MutexGuard held across await）——门当时只跑 fmt+test。
echo "== cargo clippy --workspace --all-targets --all-features -- -D warnings =="
cargo clippy --workspace --all-targets --all-features -- -D warnings || exit 1
echo "== cargo test --workspace --no-fail-fast =="
cargo test --workspace --no-fail-fast
