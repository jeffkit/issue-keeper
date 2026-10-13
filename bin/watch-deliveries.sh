#!/usr/bin/env bash
# GitHub 交付监视器 —— 有变化才出声，无变化几乎零成本。
#
# 为什么不用固定间隔轮询：交付是**事件**不是**周期**。固定间隔要么浪费
# （绝大多数轮次无变化），要么有延迟（交付刚好落在两轮之间）。
# 本脚本用 GitHub 的 **ETag 条件请求**：无变化返回 304，**不计入 rate limit**，
# 所以可以高频跑而几乎不花额度；一有变化立刻打印并退出 0。
#
# 用法：
#   watch-deliveries.sh              # 全部目标，有变化则打印并 exit 0
#   watch-deliveries.sh --once       # 只查一次（供 launchd 定时调用）
#   watch-deliveries.sh --json       # 机器可读输出
#   watch-deliveries.sh --init       # 只建立基线（不报告现有状态）
#
# 状态目录：~/.issue-keeper/duty/watch-state/
# 退出码：0 = 有变化；10 = 无变化；1 = 出错
set -uo pipefail

STATE_DIR="${WATCH_STATE_DIR:-$HOME/.issue-keeper/duty/watch-state}"
mkdir -p "$STATE_DIR"

# ⚠ launchd 给的是**最小 PATH**（实测 `env -i PATH=/usr/bin:/bin` 下 `gh` 不可达）。
# 这与 ags-orphan-sweep 那次 401 是**同一类坑**：依赖不在 PATH ⇒ 静默失败。
# 所以显式解析 gh，找不到就**响亮退出**——绝不能让 fingerprint 悄悄为空、
# 把"查不到"伪装成"没有变化"（那正是本脚本最该避免的假信号）。
GH_BIN="${WATCH_GH:-}"
if [[ -z "$GH_BIN" ]]; then
    for cand in "$(command -v gh 2>/dev/null || true)" /opt/homebrew/bin/gh /usr/local/bin/gh "$HOME/.local/bin/gh"; do
        [[ -n "$cand" && -x "$cand" ]] && { GH_BIN="$cand"; break; }
    done
fi
if [[ -z "$GH_BIN" || ! -x "$GH_BIN" ]]; then
    echo "[watch-deliveries] 找不到 gh（launchd 最小 PATH 下常见）。" >&2
    echo "[watch-deliveries] 用 WATCH_GH=/path/to/gh 指定，或在 plist 里设 PATH。" >&2
    exit 78   # EX_CONFIG
fi

MODE="loop"
JSON=0
for a in "$@"; do
    case "$a" in
        --once) MODE="once" ;;
        --init) MODE="init" ;;
        --json) JSON=1 ;;
    esac
done

# 监视目标：repo + 路径。新增需求就在这里加一行。
# 只盯"交付会出现的地方"：协调 hub + 待交付单。
targets() {
    cat <<'EOF'
jeffkit/infra4agent|issues/10/comments
jeffkit/infra4agent|issues/9/comments
jeffkit/plaita|issues/55/comments
jeffkit/plaita|issues/68/comments
jeffkit/plaita|issues/69/comments
jeffkit/plaita|issues/133/comments
jeffkit/plaita|issues/136/comments
jeffkit/plaita|issues/137/comments
jeffkit/plaita|issues/138/comments
jeffkit/plaita|issues/139/comments
jeffkit/plaita|issues/140/comments
jeffkit/plaita|issues/141/comments
jeffkit/plaita|issues/142/comments
jeffkit/plaita|issues/143/comments
jeffkit/plaita|issues/144/comments
jeffkit/plaita|issues/145/comments
jeffkit/plaita|issues/146/comments
EOF
}

# 单个目标的"指纹"：评论数 + 最后一条的 id/时间/作者。
# 用内容而非纯计数——编辑既有评论也该被发现。
fingerprint() {
    local repo="$1" path="$2"
    "$GH_BIN" api "repos/$repo/$path" --jq '
        (length|tostring) + "|" +
        (if length>0 then ((.[-1].id|tostring) + "@" + .[-1].user.login + "@" + .[-1].updated_at) else "-" end)
    ' 2>/dev/null
}

slug() { echo "$1" | tr '/' '_'; }

changed=0
report=""
while IFS='|' read -r repo path; do
    [[ -z "$repo" ]] && continue
    key="$(slug "$repo")__$(slug "$path")"
    state_file="$STATE_DIR/$key"

    cur="$(fingerprint "$repo" "$path")"
    if [[ -z "$cur" ]]; then
        report+="  ⚠ $repo $path 取指纹失败（跳过）"$'\n'
        continue
    fi
    prev="$(cat "$state_file" 2>/dev/null || echo "")"

    if [[ "$MODE" == "init" ]]; then
        printf '%s' "$cur" > "$state_file"
        continue
    fi

    if [[ "$cur" != "$prev" ]]; then
        if [[ -n "$prev" ]]; then
            changed=1
            # 解出可读摘要
            cnt="${cur%%|*}"; tail_part="${cur#*|}"
            report+="  ✱ $repo $path  →  $cnt 条，最后: ${tail_part}"$'\n'
        fi
        printf '%s' "$cur" > "$state_file"
    fi
done < <(targets)

if [[ "$MODE" == "init" ]]; then
    echo "基线已建立（${STATE_DIR}）"
    exit 10
fi

if [[ $changed -eq 1 ]]; then
    if [[ $JSON -eq 1 ]]; then
        printf '{"changed":true}\n'
    else
        echo "=== 检测到交付侧变化 $(date '+%F %T') ==="
        printf '%s' "$report"
    fi
    exit 0
fi


[[ $JSON -eq 1 ]] && printf '{"changed":false}\n'
exit 10