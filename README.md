# issue-keeper

监控 GitHub 仓库的 issue，发现新 issue 或新评论时，通过本地 [ilink-hub-bridge](https://jeffkit.github.io/ilink-hub-bridge/) 的 bridge profile 调用 agent 处理，并把 agent 的回复作为评论发到 issue 上。

## 工作机制

1. 读取配置文件，获取「仓库 → bridge profile」绑定。
2. 用本地 `gh` CLI 轮询每个仓库的 open issue 与评论。
3. 发现新 issue / 新评论时，按 P0 协议（env var 输入 + stdout 输出）调用对应 profile 的 agent。
   - 同一个 issue 的后续评论复用同一 agent 会话（`AGENT_SESSION_ID`），agent 能记住上下文。
4. 把 agent 的 stdout 作为评论发到 GitHub issue。
5. 状态持久化到本地 JSON，记录已处理的 issue / 评论 / agent 会话，重启不重复处理。

### 避免死循环

agent 发的每条评论都会带上隐藏标记 `<!-- issue-keeper-bot -->`；监控时遇到带此标记的评论、或当前 `gh` 登录账号自己发的评论，都会跳过，不会再次触发 agent。

## 前置依赖

- `gh` CLI 已安装并登录（`gh auth status` 检查）
- `ilink-hub-bridge` 已安装（仅 `type: claude-code` 的 profile 会调用它；`script:` / `command:` 直接运行对应脚本/命令）
- Python 3.11+，安装依赖：`pip install -r requirements.txt`
- 至少一个已发布的 bridge profile：`~/.ilink-hub-bridge/profiles/<name>.yaml`

## 配置

复制 `config.example.yaml` 为 `config.yaml`，按需修改：

```yaml
poll_interval_secs: 300
state_file: ~/.issue-keeper/state.json
bot_marker: "<!-- issue-keeper-bot -->"
default_timeout_secs: 600
agent_from_user: "issue-keeper"

repos:
  - repo: "owner/repo-name"
    profile: "ilink-claude"      # ~/.ilink-hub-bridge/profiles/ilink-claude.yaml
    labels: []                   # 可选：只监控带这些 label 的 issue
    # poll_interval_secs: 600    # 可选：覆盖全局轮询间隔
    # timeout_secs: 900          # 可选：覆盖全局超时
```

### 支持的 profile 类型

| profile 类型 | 调用方式 |
|---|---|
| `type: claude-code` | `ilink-hub-bridge profile claude-code`（自动管理 `--resume` 会话续接） |
| `script:` | 按扩展名推断运行时（`.py`→python3, `.js`→node, `.ts`→npx tsx, `.sh`→bash, `.rb`→ruby） |
| `command:` | 执行 `command` + `args`，支持 `{{MESSAGE}}` / `{{SESSION_ID}}` 占位符 |

> profile YAML 里的 `env` 会被注入子进程；`cwd` 作为工作目录；`timeout_secs` 覆盖默认超时。
> profile YAML 里的 `routing.default_profile` 决定实际使用哪条 profile 条目。

## 运行

```bash
# 一次性扫描（适合调试 / cron）
python -m issue_keeper --config config.yaml --once

# 常驻 daemon 轮询
python -m issue_keeper --config config.yaml

# 开启调试日志
python -m issue_keeper --config config.yaml --once --log-level DEBUG
```

## 状态文件

默认 `~/.issue-keeper/state.json`，结构：

```json
{
  "repos": {
    "owner-repo-name": {
      "issues": {
        "42": {
          "processed": true,
          "session_id": "<agent 返回的会话 uuid>",
          "processed_comment_ids": [123, 456]
        }
      }
    }
  }
}
```

想重新处理某个 issue：删除该 issue 条目（或整个文件）即可。

## 设计说明

- **回复方式**：issue-keeper 把 agent 的 stdout 作为 GitHub 评论发出，profile 无关，任何 P0 profile 都能用。
- **会话连续性**：每个 issue 持有一个 agent 会话 uuid，新评论复用该会话，agent 保持上下文。
- **监控范围**：默认只监控 open issue；可按 label 过滤；忽略 bot 自身评论避免死循环。
- **运行模式**：常驻 daemon 轮询（默认 300s）+ `--once` 一次性扫描。
