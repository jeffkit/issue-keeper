#!/usr/bin/env bash
# 值守工具：agent 档位一键切换（GLM ↔ DeepSeek），配额烧穿时热切换。
#
# 背景（2026-10-10 jeffkit 指示）：GLM 5 小时限额烧穿后管道 agent 每次调用秒退
# （429/1308），run 反复重投空转、白烧沙箱实例小时。切 DeepSeek 继续消化队列。
#
# 改 VM keeper config 的 agent_env（= 管道 agent 真实端点）+ screener.model。
# 生效：keeper 下轮 live-reload（≤3min）；**worker 无需重启**（端点由 keeper 经 --env 透传）。
# 用法： model_tier.sh deepseek | glm | status
set -eo pipefail
HOST=tcloud_gz

case "${1:-status}" in
  glm)      BASE='https://open.bigmodel.cn/api/anthropic'; MODEL='glm-5.3-flash';   KEY='${GLM_API_KEY}' ;;
  deepseek|ds) BASE='https://api.deepseek.com/anthropic';  MODEL='deepseek-v4-flash'; KEY='${DEEPSEEK_API_KEY}' ;;
  status)
    ssh -o ConnectTimeout=15 "$HOST" "sed -n '/^agent_env:/,/^pipeline:/p' ~/.issue-keeper/config.yaml | head -6"
    exit 0 ;;
  *) echo "用法: $0 deepseek|glm|status" >&2; exit 2 ;;
esac

# 用环境变量传参（避免 heredoc 参数在 set -u 下被早求值 + 防本地展开 ${...}）
ssh -o ConnectTimeout=20 "$HOST" \
  "BASE='$BASE' MODEL='$MODEL' KEY='$KEY' bash -s" <<'EOS'
set -eo pipefail
cfg=~/.issue-keeper/config.yaml
cp -n "$cfg" "$cfg.bak-$(date +%Y%m%d)-tier" 2>/dev/null || true
python3 - "$cfg" "$BASE" "$MODEL" "$KEY" <<'PY'
import re, sys
cfg, base, model, key = sys.argv[1:5]
s = open(cfg).read()
s = re.sub(r'(ANTHROPIC_BASE_URL:\s*).*',  lambda m: m.group(1)+base,  s, count=1)
s = re.sub(r'(CLAUDE_MODEL:\s*).*',        lambda m: m.group(1)+model, s, count=1)
s = re.sub(r'(ANTHROPIC_AUTH_TOKEN:\s*).*',lambda m: m.group(1)+key,   s, count=1)
s = re.sub(r'(ANTHROPIC_API_KEY:\s*).*',   lambda m: m.group(1)+key,   s, count=1)
# 沙箱流档位（2026-10-10）：agent profile 名，**不是**端点——
# 端点由 plaita-nodes 从 ~/.plaita/providers.json 翻译，keeper agent_env 管不到。
agent_name = {'glm-5.3-flash': 'glm53-flash'}.get(model, 'deepseek-flash')
if re.search(r'^pipeline_default_agent:', s, re.M):
    s = re.sub(r'^pipeline_default_agent:.*$',
               'pipeline_default_agent: ' + agent_name, s, flags=re.M)
else:
    s = re.sub(r'^(pipeline_max_in_flight:.*)$',
               r'\1\npipeline_default_agent: ' + agent_name, s, count=1, flags=re.M)
open(cfg, 'w').write(s)
PY
echo "--- 核读 agent_env（本地/legacy 流）---"
sed -n '/^agent_env:/,/^pipeline:/p' "$cfg" | head -6
echo "--- 核读 pipeline_default_agent（沙箱流，2026-10-10 接线）---"
python3 - "$cfg" <<'PY'
import re, sys
s = open(sys.argv[1]).read()
m = re.search(r'^pipeline_default_agent:.*$', s, re.M)
print(m.group(0) if m else "pipeline_default_agent: (未设置 → 回退硬编码 glm53-flash)")
PY
EOS
echo "已切档：model=$MODEL（keeper ≤3min live-reload 生效；worker 无需重启）"
