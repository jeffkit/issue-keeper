#!/usr/bin/env bash
# 周期巡检 —— 覆盖**事件驱动看护不到的**那一类问题。
#
# 为什么事件驱动不够（2026-10-13 用户指出）：
# `watch-deliveries.sh` 只比对**已知的 5 个目标**的评论指纹。它**看不见**：
#   1. 目标之外的新 issue / 新 PR（交付方换单、别人提新单）
#   2. **watcher 自身挂了** —— 静默(exit 10) 与 死掉 表现完全相同（本会话反复踩的坑）
#   3. 我自己的运维任务健康（ags-orphan-sweep 曾静默死 14h）
#   4. main 漂移（有人推了我不知道的提交）
#   5. 长期无人认领的单是否被他人接管
#
# 所以本脚本**不查交付内容**（那是 watcher 的活），只查"**系统是否还健康、有没有我没看见的变化**"。
# 输出：每条一行；**全绿时打印 `ALL OK` 后 exit 0**；任一异常 exit 1。
# 用法：periodic-patrol.sh [--json]
set -uo pipefail
# ⚠ 必须在 `cd` **之前**解析脚本自身目录——`cd` 之后 `dirname $0` 会解析成错的地方
#（实测踩过：得到 `/`，状态文件落到 `/.patrol-seen-issues`）。
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/../../plaita" 2>/dev/null || cd "$HOME/projects/infra4agent/plaita"

JSON=0
[[ "${1:-}" == "--json" ]] && JSON=1

issues=()
note() { issues+=("$1"); }

# ⚠ 坑（2026-10-13 实测）：`launchctl list` 在受限/沙箱环境下**返回 0 行**，
# 而 job 其实好好地加载着（`launchctl print` 有 49 行输出）。
# 若用 `launchctl list` 判活，会**误报 WATCHER_DOWN / SWEEP_DOWN** ——
# 监控脚本自己说谎，正是本会话反复撞见的"静默的坏"。
# 改用 `launchctl print gui/<uid>/<label>`：能取到输出即视为已加载。
job_loaded() {  # $1=label
    launchctl print "gui/$(id -u)/$1" >/dev/null 2>&1
}

# 取 job 的最近退出码（从 print 输出里解析 last exit code；取不到则回显 "-"）
job_last_rc() {  # $1=label
    launchctl print "gui/$(id -u)/$1" 2>/dev/null \
        | awk -F'= ' '/last exit code/{gsub(/[^0-9-]/,"",$2); print $2; found=1; exit} END{if(!found) print "-"}'
}

# ── 1. watcher 是否还活着（核心：静默≠健康）─────────────────────────────
wt_log="$HOME/.plaita-console/delivery-watch.log"
# 已读位置：沙箱下我**改不动** watcher 的日志（launchd 在沙箱外，它能写、我不能清），
# 所以用**自己的**已读文件名记录"处理到哪一条日志了"，避免 WATCHER_HIT 永久卡住。
wt_seen="$HOME/.issue-keeper/duty/watch-log-seen"
if [[ ! -w "$(dirname "$wt_seen")" ]] 2>/dev/null; then
    wt_seen="$SCRIPT_DIR/../.watch-log-seen"
fi
touch "$wt_seen" 2>/dev/null
if ! job_loaded cc.agentstudio.delivery-watch; then
    note "WATCHER_DOWN: cc.agentstudio.delivery-watch 未加载 —— 交付检测**已失效**"
else
    wt_rc="$(job_last_rc cc.agentstudio.delivery-watch)"
    # 退出码 10=无变化、0=有变化 都正常；其它（如 78=找不到 gh）是配置故障
    case "$wt_rc" in
        ""|-|0|10) ;;
        *) note "WATCHER_ERR: delivery-watch 退出码 ${wt_rc}（78=找不到 gh？）" ;;
    esac
    # 日志有内容说明检测到过变化，值得看一眼。
    # 但只在**内容与我上次处理的不同**时报——否则会永久重复（沙箱下我清不掉该日志）。
    if [[ -s "$wt_log" ]]; then
        cur_sig="$(wc -c < "$wt_log" 2>/dev/null | tr -d ' ')-$(tail -1 "$wt_log" 2>/dev/null)"
        if [[ "$cur_sig" != "$(cat "$wt_seen" 2>/dev/null)" ]]; then
            note "WATCHER_HIT: 有新的检测结果 —— $(tail -1 "$wt_log" | sed 's/^ *//')"
            printf '%s' "$cur_sig" > "$wt_seen" 2>/dev/null
        fi
    fi
fi

# ── 2. 我负责的运维任务健康（曾静默死 14h 的那个）──────────────────────
if ! job_loaded cc.agentstudio.ags-orphan-sweep; then
    note "SWEEP_DOWN: ags-orphan-sweep 未加载"
else
    sw_rc="$(job_last_rc cc.agentstudio.ags-orphan-sweep)"
    case "$sw_rc" in
        ""|-|0) ;;
        *) note "SWEEP_ERR: ags-orphan-sweep 退出码 $sw_rc" ;;
    esac
fi

# ── 2b. 我自己的 shell 脚本是否有 bash 3.2 崩溃隐患 ─────────────────────
# `$VAR` 紧跟全角标点会被 bash 3.2 吞进变量名 ⇒ unbound variable（本会话踩了 4 次，
# 其中 external-watchdog.sh 是**看门狗**：一旦告警就崩，恰在最需要它的时候）。
# 把 lint 接进巡检，避免只在"下次手滑时"才发现。
if [[ -x "$SCRIPT_DIR/../scripts/lint-shell-cjk-vars.sh" ]]; then
    if ! lint_out="$(bash "$SCRIPT_DIR/../scripts/lint-shell-cjk-vars.sh" 2>&1)"; then
        note "SHELL_LINT: $(printf '%s' "$lint_out" | grep -c '^  /') 处 \$VAR 紧跟全角标点（bash 3.2 会崩）"
    fi
fi


local_head=$(git rev-parse main 2>/dev/null)
remote_head=$(git rev-parse origin/main 2>/dev/null)
if [[ -n "$local_head" && -n "$remote_head" && "$local_head" != "$remote_head" ]]; then
    note "MAIN_DRIFT: 本地 $local_head ≠ origin/main $remote_head"
fi

# ── 4. 视野之外的新 issue / 新 PR（watcher 只盯已知目标）──────────────
#
# ⚠ 设计修正（2026-10-13）：最初按"近 1h 有变动"判，那是**时间窗**而非**未读状态**
# ⇒ 同一批 issue 会被连续报警 1 小时，训练我忽略它（**警报疲劳**，
# 与"监控自己说谎"同类的坏味道）。
# 现改为**已读集合**：把处理过的 issue 号记进 seen 文件，只报**没见过的**。
# 状态文件：优先 ~/.issue-keeper（沙箱下可能不可写，实测 `Operation not permitted`），
# 退化到**仓内**路径。用开头已解析好的 SCRIPT_DIR（绝对路径）。
SEEN_FILE="${PATROL_SEEN_FILE:-$HOME/.issue-keeper/duty/patrol-seen-issues}"
if ! mkdir -p "$(dirname "$SEEN_FILE")" 2>/dev/null || ! touch "$SEEN_FILE" 2>/dev/null; then
    SEEN_FILE="$SCRIPT_DIR/../.patrol-seen-issues"
    touch "$SEEN_FILE" 2>/dev/null
fi
new_issues=$(gh issue list --repo jeffkit/plaita --state all --limit 40 \
    --json number,updatedAt,createdAt,title \
    --jq '[.[] | select((.updatedAt > (now-86400|todate)) or (.createdAt > (now-86400|todate)))] | .[] | "\(.number)\t\(.title)"' 2>/dev/null)
unseen=""
while IFS=$'\t' read -r num title; do
    [[ -z "$num" ]] && continue
    if ! grep -qx "$num" "$SEEN_FILE" 2>/dev/null; then
        unseen+="  #${num} ${title:0:44}"$'\n'
        # ⚠ 必须**落盘**——否则每轮都报同一批（"警报疲劳"）。
        # 我第一版只 report 不 write，导致 #138–#141 连续两轮重复报；
        # 与 NEW_ACTIVITY 的时间窗、WATCHER_HIT 的"非空"是**同一类错**：
        # 判据依赖"我处理到哪了"，就必须把状态持久化。
        printf '%s\n' "$num" >> "$SEEN_FILE" 2>/dev/null
    fi
done <<< "$new_issues"
if [[ -n "$unseen" ]]; then
    note "NEW_ISSUES: 有未见过的单（已记入 ${SEEN_FILE}，不再重复报）：$(printf '%s' "$unseen" | tr '\n' ' ')"
fi
# 待验收的交付出现在"对方的**最后一条**评论"里。
# ⚠ 不能用 `评论数 > 1` 这种**静态阈值**——我一回帖它就永久为真，
# 于是每轮都报同一件已处理的事（**警报疲劳**；本会话已因此修过 NEW_ACTIVITY
# 与 WATCHER_HIT 两处，这是第三次同因）。
# 正确判据：**最后一条评论不是我自己发的**。
# 进一步细化（避免误报"认领/状态"类评论）：
#   · 只查**仍 OPEN** 的单（已 CLOSED 的不再需要验收）；
#   · 末条**不是我**发的；
#   · 末条**不是** claim/status 类（那类不需要我动作）。
ME="$(gh api user --jq .login 2>/dev/null || echo jeffkit)"
for n in 68 69 55 133 136 137; do
    st="$(gh issue view "$n" --repo jeffkit/plaita --json state -q .state 2>/dev/null)"
    [[ "$st" != "OPEN" ]] && continue          # 已关闭 ⇒ 无需验收
    last="$(gh issue view "$n" --repo jeffkit/plaita --json comments \
        -q 'if (.comments|length)>0 then .comments[-1].author.login else "" end' 2>/dev/null)"
    [[ -z "$last" || "$last" == "$ME" ]] && continue
    body="$(gh issue view "$n" --repo jeffkit/plaita --json comments \
        -q 'if (.comments|length)>0 then .comments[-1].body else "" end' 2>/dev/null)"
    # 认领/排期类评论不需要我动作
    case "$body" in
        *"Claim —"*|*"out of the automated pipeline"*) continue ;;
    esac
    note "DELIVERY?: #$n 最后一条来自 ${last}（非我，且非认领类）— 可能待验收"
done

# ── 5. 无人认领的单是否被他人接管 ──────────────────────────────────────
# ⚠ 判据必须**同时看 state**：只用"评论数 > 0"会在单被认领并**关闭**后永久为真
#（实测：`#106` 我落地关闭后仍每轮报 `CLAIMED?`）。本会话第五次同因——
# 判据必须是"当前需要我关注吗"，而不是"历史上发生过什么"。
for n in 113 118 109 117 110; do
    read -r st c <<<"$(gh issue view "$n" --repo jeffkit/plaita --json state,comments \
        -q '"\(.state) \(.comments|length)"' 2>/dev/null)"
    [[ "$st" != "OPEN" ]] && continue          # 已关闭 ⇒ 不再是"待认领"
    last="$(gh issue view "$n" --repo jeffkit/plaita --json comments \
        -q 'if (.comments|length)>0 then .comments[-1].author.login else "" end' 2>/dev/null)"
    [[ "$last" == "$ME" ]] && continue          # 只有我自己说过话 ⇒ 仍无人接管
    [[ -n "$c" && "$c" -gt 0 ]] && note "CLAIMED?: #$n 有 $c 条评论（末条来自 ${last}，可能被认领）"
done

# ── 输出 ────────────────────────────────────────────────────────────────
if [[ ${#issues[@]} -eq 0 ]]; then
    [[ $JSON -eq 1 ]] && echo '{"ok":true,"issues":[]}' || echo "ALL OK"
    exit 0
fi
if [[ $JSON -eq 1 ]]; then
    printf '{"ok":false,"issues":['
    for i in "${!issues[@]}"; do
        [[ $i -gt 0 ]] && printf ','
        printf '"%s"' "${issues[$i]//\"/}"
    done
    printf ']}\n'
else
    echo "=== 巡检发现 ${#issues[@]} 项 $(date '+%F %T') ==="
    for i in "${issues[@]}"; do echo "  ✱ $i"; done
fi
exit 1
