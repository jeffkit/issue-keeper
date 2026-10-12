#!/usr/bin/env bash
# 静态检查：`$VAR` 紧跟全角标点 —— bash 3.2（macOS 的 /bin/bash）会把多字节字符
# 吞进变量名，报 `VAR<garbage>: unbound variable`。
#
# 为什么需要它（本会话实测踩了 **4 次**）：
#   · watch-deliveries.sh 的 `$STATE_DIR）`
#   · periodic-patrol.sh 的 `$STATE_DIR）`、`$last）`、`$SEEN_FILE，`
# 每次都是"写完自认为没问题、跑起来才崩"，而且**只在 macOS 的 bash 3.2 下崩**，
# 在 bash 5 上完全正常 —— 属典型的"本地能跑、换环境就炸"。
#
# 用法：bash scripts/lint-shell-cjk-vars.sh [目录...]
# 退出码：0 = 干净；1 = 有命中
set -uo pipefail
TARGETS=("${@:-$(cd "$(dirname "$0")/.." && pwd)}")

# 全角标点集合（与本仓文案里实际会用到的一致）
CJK='（），。：；、「」【】？！％'
hits=0
for t in "${TARGETS[@]}"; do
    [[ -e "$t" ]] || continue
    while IFS= read -r f; do
        # 逐行检查，跳过纯注释行（注释不参与 shell 展开，不会崩）
        while IFS= read -r line; do
            [[ "$line" =~ ^[[:space:]]*# ]] && continue
            # 匹配 $VAR 或 ${VAR} 之后紧跟全角标点；${VAR} 形式是安全的，排除掉
            if printf '%s' "$line" | grep -qE "\\\$[A-Za-z_][A-Za-z0-9_]*[$CJK]"; then
                printf '  %s: %s\n' "$f" "$(printf '%s' "$line" | sed 's/^[[:space:]]*//' | cut -c1-100)"
                hits=$((hits+1))
            fi
        done < "$f"
    done < <(find "${TARGETS[@]}" -name '*.sh' -type f 2>/dev/null)
done

if [[ $hits -eq 0 ]]; then
    echo "OK: 未发现 \$VAR 紧跟全角标点（bash 3.2 安全的写法）"
    exit 0
fi
echo "发现 $hits 处：\`\$VAR\` 紧跟全角标点 ⇒ macOS bash 3.2 下会 unbound variable。"
echo "修法：写成 \${VAR}（带花括号）。"
exit 1
