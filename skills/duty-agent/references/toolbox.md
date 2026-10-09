# 工具与端点速查

变量：`KEY=b4b5042ee7d1b937633c08f3f50d4c8efbca88d33ece8a03`（console admin key）、
`IK=~/projects/infra4agent/issue-keeper`、`PY=~/.venvs/issuekeeper/bin/python`（在 VM 上；Mac 上用系统 python3 即可跑 flows 脚本）。

## 工单（我的收件箱）

```bash
R=$IK/flows/duty_request.py
python3 $R list                                  # 全部（open/escalated/answered 在最前）
python3 $R list --status open,escalated --json    # 机器可读
python3 $R get --id req-YYYYMMDD-HHMMSS-xxxxxx
python3 $R update --id <id> --status decided \
  --decision-json '{"by":"值守 Agent（resident）","action":"resume_execution","args":{...},"rationale":"..."}' \
  --note "已验证"
python3 $R answer --session-id <sid> --text "人的回复原文"   # hitl_inbox 自动调用
```
状态机：`open → decided|escalated → answered → resolved|dismissed`。

## HITL（找人 / 收回复）

```bash
python3 $IK/flows/hitl_notify.py --title "值守上报：<一句话>" --body "<自足正文：是什么/卡在哪/选项+后果>" \
  --dedupe-key "<稳定键，如 duty:<工单id>>" --feedback-url https://github.com/jeffkit/<repo>/issues/<n>
python3 $IK/flows/hitl_inbox.py            # 收回复（flow hitl-inbox */5 已自动跑）
curl -s http://127.0.0.1:8081/admin/api/hil/sessions | python3 -m json.tool   # 会话现状
```
- 发送端**必须建会话**（脚本已强制 `wait_reply=true`）。
- **消息自足**：人没有上下文——写清"是什么 / 卡在哪 / 需要他决定什么 / 选项 + 各自后果"。

## plaita console（flow / 执行 / 调度）

```bash
# 看
curl -s "http://127.0.0.1:8323/api/executions?flow_id=<f>&limit=3" -H "X-Admin-API-Key: $KEY"
curl -s "http://127.0.0.1:8323/api/executions/<eid>" -H "X-Admin-API-Key: $KEY"   # status/node_timings/error
curl -s "http://127.0.0.1:8323/api/schedules" -H "X-Admin-API-Key: $KEY"
# 动
curl -s -X POST "http://127.0.0.1:8323/api/executions/<eid>/resume" -H "X-Admin-API-Key: $KEY" \
  -H "Content-Type: application/json" -d '{"resume_type":"retry"}'
curl -s -X POST "http://127.0.0.1:8323/api/executions/<eid>/cancel" -H "X-Admin-API-Key: $KEY"
# 发布 flow（先编译产物，再 PUT + publish；版本号递增）
PYTHONPATH=~/projects/infra4agent/plaita:~/projects/infra4agent/plaita-nodes/src \
  python3 $IK/flows/build_flows.py <short-name>          # 统一编译入口（含 --check 字节比对）
python3 -c "import json;d=json.load(open('$IK/flows/<name>.flow.json'));json.dump({'definition':json.dumps(d,ensure_ascii=False),'layout':'{}','created_by':'jeffkit'},open('/tmp/save.json','w'))"
curl -s -X PUT "http://127.0.0.1:8323/api/flows/<id>/versions/<v>" -H "X-Admin-API-Key: $KEY" -H "Content-Type: application/json" --data @/tmp/save.json
curl -s -X POST "http://127.0.0.1:8323/api/flows/<id>/publish" -H "X-Admin-API-Key: $KEY" -H "Content-Type: application/json" -d '{"version":"<v>"}'
# 建/改调度
curl -s -X POST "http://127.0.0.1:8323/api/schedules" -H "X-Admin-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"name":"...","flow_id":"<f>","cron":"*/15 * * * *","enabled":true,"params":{...}}'
curl -s -X PUT "http://127.0.0.1:8323/api/schedules/<sid>" -H "X-Admin-API-Key: $KEY" -H "Content-Type: application/json" -d '{"enabled":false}'
```

## keeper（业务真值，**在 VM 上跑**）

```bash
ssh tcloud_gz
cd ~/projects/infra4agent/issue-keeper
~/.venvs/issuekeeper/bin/python -m issue_keeper reopen -c ~/.issue-keeper/config.yaml jeffkit/plaita 48 49   # 清终态 + 自动摘 needs-human
tail -50 ~/.issue-keeper/keeper.log
python3 -c "import json;st=json.load(open('/home/ubuntu/.issue-keeper/state.json'));print([(s,n) for s,rv in (st.get('repos') or {}).items() for n,it in (rv.get('items') or {}).items() if it.get('in_flight_since')])"
```
- 改配置：编辑 `~/.issue-keeper/config.yaml`（**先 `cp` 备份**）→ keeper 下轮 live-reload 生效；
  验证要用 keeper 的解析器：`~/.venvs/issuekeeper/bin/python -c "from issue_keeper.config import load_config;c=load_config('/home/ubuntu/.issue-keeper/config.yaml');print(c.pipeline_max_in_flight)"`。
- **插队**：`pipeline_priority_issues: [jeffkit/repo#N]`（keeper-shadow 的排序键会把它放最前）。
- 原子改 state（如清退避）：读 JSON → 改 → `tempfile` + `os.replace`；**先备份**。

## 沙箱（AGS）

```bash
export E2B_DOMAIN=ap-guangzhou.tencentags.com E2B_API_KEY=e2b_725235357335be8d27367c596c9e3199cf3c5eeb
/Users/kong/projects/infra4agent/plaita/.venv/bin/python $IK/flows/ags-list.py      # 实例清单（JSON）
/Users/kong/projects/infra4agent/plaita/.venv/bin/python $IK/flows/ags-orphan-sweep.py --min-age 0.5
# 杀单个：plaita/.venv/bin/python -c "from e2b import Sandbox; print(Sandbox.kill('<完整 sandbox_id>'))"
```

## 看板 / 截图（自查视觉与数据）

```bash
launchctl kickstart -k gui/$(id -u)/cc.agentstudio.issue-keeper-dashboard   # 重启看板
cd $IK/frontend && npm run build                                            # 改前端后
curl -s http://127.0.0.1:7433/api/duty/overview | python3 -m json.tool      # 心跳/在途/调度/GLM
curl -s http://127.0.0.1:7433/api/duty/human-queue | python3 -m json.tool   # 工单收件箱 + needs-human + HITL
node $IK/flows/shot-duty.js "http://127.0.0.1:7433/?view=duty" /tmp/duty     # playwright 截图 + 样式自检
```

## 独立兜底（launchd，故意不在 flow 里）

```bash
launchctl list | grep -E "external-watchdog|ags-orphan-sweep|issue-keeper-dashboard"
tail -20 ~/.issue-keeper/duty/external-watchdog.log
```
