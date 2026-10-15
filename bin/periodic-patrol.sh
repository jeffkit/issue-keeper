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
ACK=0
for a in "$@"; do
    case "$a" in
        --json) JSON=1 ;;
        --ack)  ACK=1 ;;   # 处置完这一轮后调用：把已看过的交付指纹记为已读
        *) echo "未知参数: $a" >&2; exit 2 ;;
    esac
done

issues=()
note() { issues+=("$1"); }
# ⚠ COOP_GAP 单独成列：它是**唯一一类"对方已经说过话而我从未回应"**的信号，
# 处置优先级高于所有"可能有事"的信号（见 4b 段注释）。输出时排最前。
coop=()
# DELIVERY? 的指纹也要收集起来：**只在人类真正看过这一轮之后**才允许落盘，
# 否则脚本自己就把信号标记成"已读"了——那正是本会话反复踩的"判据依赖我处理到哪，
# 就必须由我（而不是脚本）推进状态"。故这里只收集，落盘由 `--ack` 显式触发。
deliv=()
deliv_desc=()

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

# ── 2c. 队列/DLQ 只读观测（**绝不 purge** —— 用户铁律）───────────────────
# ⚠ key 名必须准确：实测存在 `plaita:flow:queue:v2:dlq`（真 DLQ，224 条）
# 与 `plaita:flow:queue:ctrl:dlq`（控制面，1 条）两个**不同**的 DLQ；
# 而 `plaita:flow:queue:dlq` **根本不存在**（`TYPE` 返回 none）。
# 我自己曾用错 key 得出"DLQ 从 224 清零"的**假警报** —— 故 key 硬编码在此并注明。
# 本项只做**观测**：DLQ 增长说明消费端在丢消息。
# **任何情况下都不清除**（铁律：恒不 purge DLQ）。
RCLI="${PATROL_REDIS_CLI:-redis-cli}"
RPORT="${PATROL_REDIS_PORT:-16379}"
RPASS="${PATROL_REDIS_PASS:-c2f9036c06e32474b39e291f1af50b274d2b1defa461084a}"
rlen() { "$RCLI" -h 127.0.0.1 -p "$RPORT" -a "$RPASS" -n 1 --no-auth-warning XLEN "$1" 2>/dev/null; }
if command -v "$RCLI" >/dev/null 2>&1; then
    d="$(rlen "plaita:flow:queue:v2:dlq")"
    if [[ -n "$d" ]]; then
        # 只报**增长**（首次跑记基线），避免每轮重复报历史存量——否则又是一次警报疲劳。
        dq_seen="$SCRIPT_DIR/../.patrol-dlq-baseline"
        prev_d="$(cat "$dq_seen" 2>/dev/null || echo "")"
        if [[ -z "$prev_d" ]]; then
            printf '%s' "$d" > "$dq_seen" 2>/dev/null
        elif [[ "$d" =~ ^[0-9]+$ && "$prev_d" =~ ^[0-9]+$ && "$d" -gt "$prev_d" ]]; then
            note "DLQ_GROWTH: DLQ $prev_d → ${d}（消费端在丢消息；**不 purge**，仅报告）"
            printf '%s' "$d" > "$dq_seen" 2>/dev/null
        fi
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

# ⚠ 设计修正（2026-10-15，本轮）：**第六次同因——判据不是"当前需要我动作吗"**。
# 原判据"末条评论不是我发的"会在**我已经处置过该轮**之后**永久为真**：
# 只要我处置时不在该单回帖（而是去协调线程汇总），每 30 分钟就重报一次
# `DELIVERY?: #68/#69/#133`。实测连报数十轮、全是同一批旧评论 ⇒ 典型警报疲劳。
# 正确判据：**该末条评论我处理过没有**。用"已读指纹"持久化，与 WATCHER_HIT /
# NEW_ACTIVITY 同一套做法（本会话已因同因修过三次，这是第四次，故写进注释）。
#
# 指纹 = "issue号:末条评论id"。处置完一轮后把指纹写进 seen 文件即静音；
# 交付方**新发**评论 ⇒ 指纹变化 ⇒ 重新报（这才是真信号）。
#
# ⚠ 监控目标列表也**不能硬编码**：实测这份列表落后了（仍写着 `#69 排 #68 后`、
# `#68 在 rework`、`#113 等入主`），而 `#113/#117/#122/#141` 早已落地关闭。
# 硬编码清单会**既漏报新目标、又对已关闭的单空转**。改为**动态发现**：
# 取所有 OPEN 且**末条非我**的单——"单还开着 + 对方最后说话"就是"可能待我验收"的
# 充分条件，不需要我手工维护名单。
discovery="$(
    gh issue list --repo jeffkit/plaita --state open --limit 100 \
        --json number,updatedAt \
        --jq 'sort_by(.updatedAt)|reverse|.[]|.number' 2>/dev/null
)"
# ⚠⚠ 2026-10-15 晚 **重大修正**：删掉 `DELIV_SEEN` / `--ack`（我今晚自己造的坑，代价 33 小时）
#
# 我今晚早些时候给 `DELIVERY?` 加了"已读指纹"文件 + `--ack` 显式静音，理由写的是
# "处置过后判据永久为真 ⇒ 每 30 分钟重报 ⇒ 警报疲劳"。**那个理由本身是错的**：
#   判据 `末条不是我` 在**我回帖之后会自动变假**（末条变成我的）——**本来就是自清除的**，
#   根本不需要额外状态。我当时觉得"连报数十轮"是噪声，实际是——**我确实没在那些单回过帖**，
#   警报是**对的**，是我把"准确的警报"当成了噪声。
#   而 `--ack` 只记指纹、**不校验我是否真的回了帖** ⇒ 它**只能**静音"我没处理的"，
#   永远静音不了"我已处理的"（因为那些早已被自清除跳过）。
#   后果实测：`#68`（落地流水线头部）被我 `--ack` 静音，而我从 04:11Z 起 **33 小时没回**
#   交付方的三条诊断请求 —— 交付方以为我罢工。**这就是"监控自己说谎"的最坏形态：
#   不是报错，是把真信号亲手关掉。**
#
# 正确判据（保留，不加状态）：**该单末条评论不是我的** ⇒ 我还没回过 ⇒ 报。
# 我回帖 ⇒ 末条变我的 ⇒ 自动不报。**自清除，无状态文件，无法被伪造。**
# 若某条确实无需回帖（认领/状态类），在下面的 `case` 里**按内容具名豁免**，不靠"已读"。
for n in $discovery; do
    # 一次取回末条的 (login, id, body)——原来是三次 gh 调用，慢且易抖
    read -r last last_id < <(gh issue view "$n" --repo jeffkit/plaita --json comments \
        -q 'if (.comments|length)>0 then "\(.comments[-1].author.login) \(.comments[-1].id)" else " -" end' 2>/dev/null)
    [[ -z "$last" || "$last" == "-" || "$last" == "$ME" ]] && continue
    deliv+=("$n:$last_id")
    deliv_desc+=("#$n(${last})")
done
# ⚠ 汇总而非逐条：动态发现后待验收项可达 20+ 条，逐条一行的结果是
# **每 30 分钟 30 行**——比原来的"漏报"更坏（这才是真正的警报疲劳）。
# 巡检的职责是"叫我来看"，不是"把清单倒给我"；明细应当**按需拉取**。
if [[ ${#deliv[@]} -gt 0 ]]; then
    note "DELIVERY?: ${#deliv[@]} 单末条非我且我未处置：${deliv_desc[*]}"
fi

# ── 4b. 协同缺口自检：对方发过言、而我从未在该线程回过 ────────────────────
# 起因（2026-10-13 用户指出）：我把"不回帖"做过头，成了"沉默的协同"——
# 对方在本单给了报告，我却在协调线程汇总而不在本单回，**他的视角是"没人读过"**。
# 该做的是"per-issue 报告 per-issue 回"，而不是"尽量少说话"。
# （这是**两套清单**的又一处：协调线程 ≠ 各 issue 线程。）
#
# ⚠ 2026-10-15 优先级修正：`COOP_GAP` **高于** `DELIVERY?`/`WATCHER_HIT`。
# 理由：`DELIVERY?` 只是"可能有事"，`COOP_GAP` 是"对方已经说过话而我**从未**回应"——
# 已实测过一次真实代价：`#141` 的 per-issue 报告我压了约 6h，交付方以为没人读。
# 故本项在报告里**排最前**（见输出段），且目标列表动态化（与 4a 同因：硬编码会失真）。
COOP_TARGETS="${PATROL_COOP_TARGETS:-$discovery}"
for n in $COOP_TARGETS; do
    read -r mine theirs < <(gh issue view "$n" --repo jeffkit/plaita --json comments \
        -q '"\([.comments[]|select(.author.login=="'"$ME"'")]|length) \([.comments[]|select(.author.login!="'"$ME"'")]|length)"' 2>/dev/null)
    if [[ "${mine:-0}" == "0" && -n "$theirs" && "$theirs" != "0" ]]; then
        coop+=("COOP_GAP: #$n 对方发了 ${theirs} 条，我一条都没回（per-issue 报告应 per-issue 回）")
    fi
done

# ── 5. 无人认领的单是否被他人接管 ──────────────────────────────────────
# ⚠ 判据必须**同时看 state**：只用"评论数 > 0"会在单被认领并**关闭**后永久为真
#（实测：`#106` 我落地关闭后仍每轮报 `CLAIMED?`）。本会话第五次同因——
# 判据必须是"当前需要我关注吗"，而不是"历史上发生过什么"。
#
# ⚠ 2026-10-15：这里原本也是硬编码 5 个号（`113 118 109 117 110`），其中
# `#113`/`#117` 已落地关闭 ⇒ 空转。**但本项不能像 4a/4b 那样全量动态化**：
# 本项问的是"这单**长期无人认领**吗"，判据是"创建时间超过阈值 + 仍无对方评论"，
# 与"末条谁发的"无关。故改为**按年龄动态筛**，保留"无人认领"这一语义。
UNCLAIMED_AGE_H="${PATROL_UNCLAIMED_AGE_H:-20}"
old_open="$(gh issue list --repo jeffkit/plaita --state open --limit 100 \
    --json number,createdAt,updatedAt \
    --jq --argjson h "$UNCLAIMED_AGE_H" \
        '.[]|select((.createdAt|fromdateiso8601) < (now - ($h*3600)))|.number' 2>/dev/null)"
for n in $old_open; do
    read -r st c last < <(gh issue view "$n" --repo jeffkit/plaita --json state,comments \
        -q '"\(.state) \(.comments|length) \(if (.comments|length)>0 then .comments[-1].author.login else "-" end)"' 2>/dev/null)
    [[ "$st" != "OPEN" ]] && continue          # 已关闭 ⇒ 不再是"待认领"
    [[ "$last" == "$ME" ]] && continue          # 只有我自己说过话 ⇒ 仍无人接管
    [[ -n "$c" && "$c" -gt 0 ]] && note "CLAIMED?: #$n 有 $c 条评论（末条来自 ${last}，可能被认领）"
done

# ── 输出 ────────────────────────────────────────────────────────────────
# ⚠ 排序即优先级。实测代价：`#141` 的 COOP_GAP 被 DELIVERY? 的噪声淹没，
# 压了约 6h 才处理。故 COOP_GAP **永远排在最前**，其次才是那些"可能有事"的信号。
# ⚠ bash 3.2（macOS 自带）坑：`set -u` 下展开**空数组** `${arr[@]}` 会报
# "unbound variable"。本项目已有一处同类问题（见 2b 段 lint-shell-cjk-vars.sh）。
# 统一用 `${arr[@]+"${arr[@]}"}` 惯用法，空数组时展开为**零个参数**而非报错。
ordered=(${coop[@]+"${coop[@]}"} ${issues[@]+"${issues[@]}"})

# `--ack` 已**移除**（2026-10-15 晚）。它是我今晚自己造的坑：
# 见 DELIVERY? 段顶部的长注释——它只能静音"我没处理的"，因此实测静音了 `#68` 达 33 小时。
# 现在改为**自清除**判据（我回帖 ⇒ 末条变我的 ⇒ 自动不报），不需要任何状态文件。
# 保留 `--ack` 作为**显式报错**，避免旧脚本/旧习惯还在调用它却以为生效了（"静默的坏"）。
if [[ "${ACK:-0}" == "1" ]]; then
    echo "⚠ --ack 已移除：DELIVERY? 现在自清除（我回帖即消）。" >&2
    echo "  若某单确实无需回帖，请在报文里忽略它，或在脚本的 case 豁免里按内容具名豁免。" >&2
    echo "  历史教训：--ack 静音了 #68 达 33 小时，交付方以为我罢工。" >&2
    exit 3
fi

if [[ ${#ordered[@]} -eq 0 ]]; then
    [[ $JSON -eq 1 ]] && echo '{"ok":true,"issues":[]}' || echo "ALL OK"
    exit 0
fi
if [[ $JSON -eq 1 ]]; then
    printf '{"ok":false,"issues":['
    for i in "${!ordered[@]}"; do
        [[ $i -gt 0 ]] && printf ','
        printf '"%s"' "${ordered[$i]//\"/}"
    done
    printf ']}\n'
else
    echo "=== 巡检发现 ${#ordered[@]} 项 $(date '+%F %T') ==="
    for i in "${ordered[@]}"; do echo "  ✱ $i"; done
fi
exit 1
