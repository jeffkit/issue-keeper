# issue-keeper

监控 GitHub 仓库的 issue（可选 PR），发现新内容时先用一次**无本地权限的安全过滤**审一道，确认安全后再通过 [AgentProc](https://agentproc.dev/) 调用 agent 处理，并把 agent 的回复作为评论发回 GitHub（或自建 issue 系统）。

## 工作机制

1. 读取配置文件，获取「仓库 → AgentProc profile → issue 来源」绑定 + screener 凭据。
2. 通过 **IssueSource 适配器**轮询每个仓库的 open issue（可选 open PR）与评论。
3. 发现新 issue/PR 或新评论时：
   - **第一步：安全过滤（screener）**——在 keeper 进程内发一次纯 HTTP 的 LLM 调用，判定内容是否含指令注入 / 越权诱导。这一层不起子进程、不读写本地文件，架构上保证无本地权限。
   - **第二步：调主 agent**——判定安全后，通过 `agentproc` CLI 调用对应 profile 的 agent（动态指定 `--cwd`，会话自动续接）。agent 有 bash 等工具能力，能直接读写代码、调用 issue-keeper CLI 跨项目提 issue。
4. 把 agent 的回复作为评论发回对应 issue/PR。
5. 状态持久化到本地 JSON，记录已处理的资源 / 评论 / agent 会话，重启不重复处理。

## 防循环（三层保险）

issue-keeper 既会回复评论，也可能自己提 issue。为防止 AI 自己触发自己（"自己给自己写日记"），采用三层识别，任一命中即跳过：

| 层 | 标识 | 用途 |
|---|---|---|
| 1. 隐藏 marker | `<!-- issue-keeper-bot -->` | 机器识别，必带 |
| 2. 可见前缀 | `[issue-keeper:<agent_label>]` | 人眼识别 + 备份识别 |
| 3. self_identity | 当前 source 的 GitHub 账号 | 账号级兜底 |

**资源层（issue/PR 本体）也识别 marker**：

- AI 自己提的 issue（body 含 marker 或可见前缀）→ 不触发首次 agent 回复
- 当前 source 账号自己提的 issue → 也不自己回自己
- 但评论层照常处理——别人/别的 agent 来评论时仍会触发响应

这让你可以放心让 AI 自己提 issue（带 marker），不会陷入循环。

## 可见前缀与 agent 身份

每条 agent 发出的评论正文开头都会带可见前缀：

```
<!-- issue-keeper-bot -->
[issue-keeper:proj-a-agent]
<agent 的回复正文>
```

- `agent_label` 在每个 `repos` 条目里配，默认 fallback 到全局 `agent_from_user`
- 用途：
  1. 在 GitHub 网页一眼区分不同 repo 的 agent 产出（解决"agent 产出和我手工产出混在一起"的痛点）
  2. 防循环备份识别
  3. 跨项目时让别的仓库识别"是哪个 agent 来的"

## 状态机与看板（internal source）

internal source 的 issue 有 6 个看板状态：

| 状态 | 含义 |
|---|---|
| `inbox` | 收件箱，人提的原始 issue，还没规划 |
| `todo` | 已规划待处理（agent 提的 issue 默认进这里） |
| `doing` | 处理中 |
| `review` | 待 review |
| `done` | 已完成 |
| `closed` | 已关闭/归档 |

### 自动流转

keeper 在处理 issue 时自动推状态：

| 触发 | 状态变化 |
|---|---|
| 人提新 issue | → `inbox` |
| agent 提新 issue | → `todo` |
| keeper 开始调 agent | → `doing` |
| agent 回复完 | → `review` |
| review 通过 | → `done` |
| close | → `closed` |
| done/closed 收到新评论 | → `doing`（重新打开） |

### Review 机制

- **agent 提的 issue** → 由该 agent 自己 review（下轮扫到 review 状态自动通过 → `done`）
- **人提的 issue** → 由配置的 `review_agent` 先 review；需要人二次 review 时人接手

```yaml
# config.yaml 里的全局默认 review agent
default_review_agent: "reviewer-agent"
```

> per-repo `review_agent` 覆盖暂未入库（db `projects` 表未存该字段），目前用全局 `default_review_agent`。确需 per-project review agent 可后续加列。

### 角色区分（actor_type）

每个 issue/评论都有 `actor_type`：`human` 或 `agent`。CLI 创建时显式指定：

```bash
# 人提的（默认）
python -m issue_keeper internal create proj-a --title "..." --actor-type human --author alice

# agent 提的
python -m issue_keeper internal create proj-a --title "..." --actor-type agent --author alpha-agent
```

agent 提的 issue 默认进 `todo`（直接待处理），人提的进 `inbox`（等规划）。

### CLI 状态管理

```bash
# 改状态
python -m issue_keeper internal move proj-a 3 --status review --author alice --comment "处理完了"

# 看板视图（CLI 简陋版）
python -m issue_keeper internal board proj-a

# 查看 issue 详情（含状态历史）
python -m issue_keeper internal show proj-a 3

# 列出所有项目
python -m issue_keeper internal projects
```

### 状态历史

每次状态变更都记到 `status_history` 表，`show` 命令会展示完整流转记录：

```
状态历史（4 条）：
  — → inbox     by alice（human）     2026-07-11T02:54:09
  inbox → doing  by alpha-agent（agent）  备注: 开始处理
  doing → review by alpha-agent（agent）  备注: 处理完成，待 review
  review → done  by alpha-agent（agent）  备注: 自动 review 通过
```

> Web 看板（dashboard）已实现：FastAPI 后端 + Vite/React 前端，支持拖拽改状态，见下文「Web 看板」一节。

## 安全过滤（screener）

GitHub issue/PR 是公开输入面，任何人都能在里面塞内容诱导 agent 做破坏性操作（读 `~/.ssh`、执行命令、泄露 `.env`……）。screener 在主 agent 之前做一次预审：

- **零本地权限**：screener 只发一次 HTTP POST 到 LLM API，绝不 spawn 子进程、绝不读写本地文件（除模块自身加载），因此不存在「配错 cwd 就越权」的可能。
- **双协议支持**：
  - `provider: openai`（默认）—— OpenAI 兼容协议，支持 **DeepSeek**（推荐）/ OpenAI / Moonshot / Together 等。
  - `provider: anthropic` —— Anthropic messages API（GLM anthropic 兼容端点等）。
- **凭据复用**：可以直接配 `api_key`/`base_url`/`model`（推荐 DeepSeek），也可以用 `credentials_from_profile` 复用某个 AgentProc profile 的凭据。
- **fail-safe**：必须显式声明 `screener.enabled`。不写 `screener` 段、或 `enabled: true` 但缺凭据，程序都拒绝启动。
- **判定不安全时**：
  - `on_unsafe: skip`（默认）——静默跳过 + WARNING 日志，不在 GitHub 发任何东西。
  - `on_unsafe: comment` ——跳过 + 在对应 issue/PR 上发一条「已被安全过滤跳过」的提示评论（措辞中性，不含原文）。

## 可插拔的 issue 来源（IssueSource）

issue-keeper 的核心循环对来源不敏感：任何实现了 `IssueSource` 协议的上游适配器都能接入。这样未来可以接入各种「能让 agent 用独立身份说话」的上游，包括完全脱离 GitHub 的自建 issue 系统。

```
                 ┌─────────────────────────────────┐
                 │  keeper 主循环（不关心来源）     │
                 │  扫描 → screener → agent → 回复  │
                 └────────────┬────────────────────┘
                              │ IssueSource 协议
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
  github_cli            github_token           internal（未来）
  （用 gh CLI）          （用 PAT，REST API）   （自建 issue 系统）
                                                
                 未来还可加：github_app / discord / http ...
```

**当前支持**：

| source | 说明 | 凭据 |
|---|---|---|
| `github_cli` | 用本地 `gh` CLI 访问 GitHub，以 `gh auth status` 登录的账号身份读写 | 无需额外配置 |
| `github_token` | 用 PAT 直接调 GitHub REST API，可指定任意账号身份 | `github_token` 字段或 `GITHUB_TOKEN`/`GH_TOKEN` 环境变量 |
| `internal` | 自建 issue 系统，SQLite 存储，完全脱离 GitHub | `internal_db` 字段（可选，默认 `~/.issue-keeper/internal.db`） |

`github_token` 相比 `github_cli`：

- 不依赖本地 `gh` CLI 已登录
- 支持分页（自动翻页，无 100 条限制）
- 可指定任意账号身份（你自己的、bot 账号的、未来每项目一个 token）

### 自建 issue 系统（internal source）

终极形态：完全脱离 GitHub，用 SQLite 存储 issue/PR 和评论。给 agent 之间互相沟通用——每个 `repo` 配置项是一个"项目"（名字任意），所有项目共享一个 SQLite 文件。

**为什么需要**：

- 你之前提的痛点："agent 的产出和我手工产出混在一起"——internal source 让 agent 有完全独立的沟通渠道，和人用的 GitHub 物理隔离
- 跨项目沟通天然：A 的 keeper 配置里加 B 项目作为另一个 repo binding，A 以 `proj-a-agent` 身份在 B 那边提 issue / 留评论
- agent 之间能互相提 issue（通过 CLI 子命令）

**存储**：单个 SQLite 文件（WAL 模式，支持多进程并发读写）。schema 两张表：`issues`、`comments`。issue 和 PR 同表，用 `kind` 字段区分；同一项目内 issue 和 PR 编号独立递增。

**身份模型**：

- `self_identity()` 返回 `binding.agent_label`——这就是 agent 在 internal 系统里的身份
- agent 通过 `post_comment` 发评论时，author 自动记为 `agent_label`
- 通过 CLI 提 issue / 评论时，author 由 `--author` 指定（模拟外部人/别的 agent）

**管理 CLI**：

```bash
# 提一个新 issue（给你或别的 agent 用）
python -m issue_keeper internal create proj-a --title "登录 bug" \
    --body "点登录无响应" --author alice

# 在某 issue 下评论（模拟外部人留言 / agent 互相留言）
python -m issue_keeper internal comment proj-a 1 --body "我也遇到了" --author external-user

# 列出某项目的 open issue
python -m issue_keeper internal list proj-a

# 查看某 issue 详情和评论
python -m issue_keeper internal show proj-a 1

# 关闭 issue
python -m issue_keeper internal close proj-a 1

# PR 也支持（--kind pr）
python -m issue_keeper internal create proj-a --title "修复 PR" --kind pr --body "..." --author carol

# 带标签创建（可多次 --label）
python -m issue_keeper internal create proj-a --title "登录 bug" --author alice --label bug --label ai

# 按标签过滤列出（交集，大小写不敏感）
python -m issue_keeper internal list proj-a --label bug
```

CLI 不依赖 `config.yaml`，直接用 `--db` 指定 SQLite 路径（或默认 `~/.issue-keeper/internal.db`）。这样既给人手工用，也能让 agent 在处理 issue 时直接用 bash 调用——agent 有 bash 工具能力，想给别的项目提 issue 时直接 `python -m issue_keeper internal create ...` 即可，不需要 keeper 参与解析。

**防循环**：三层防循环机制在 internal source 上同样工作。agent 自己提的 issue（body 含 marker 或可见前缀）不会触发首次回复，但评论层照常——别人/别的 agent 来评论时仍会触发响应。

**当前不做**（留给后续）：

- HTTP server（让外部系统通过 HTTP 提 issue）
- 用户认证系统

### Web 看板（dashboard）

读 internal.db 的 Web 看板，支持拖拽改状态、点开看详情/评论/状态历史、新建 issue、加评论。后端 FastAPI，前端 Vite + React（`@dnd-kit` 拖拽）。

**首次使用需构建前端**：

```bash
cd frontend
npm install
npm run build        # 产出 frontend/dist/，由后端一并托管
```

**启动**：

```bash
# 默认读 ~/.issue-keeper/internal.db，监听 127.0.0.1:7433
python -m issue_keeper dashboard

# 自定义端口 / db / 操作身份
python -m issue_keeper dashboard --port 8080 --db /path/to/internal.db --agent-label alice
```

浏览器打开 `http://127.0.0.1:7433` 即可。顶栏可切项目、切 issue/PR、设置「当前身份」（你以谁的名义发评论/改状态，会存 localStorage）。卡片在六列之间拖拽即触发 `move`；点卡片标题打开详情面板。顶栏「看板 / 团队成员」切换看板视图与团队视图。

### 团队成员（team）

多项目协作时，dashboard 的「团队成员」页展示参与工作的所有 agent 及其自我介绍。数据存在 `internal.db` 的 `projects` 表——既是团队介绍、也是项目绑定的**单一配置源**。`team` CLI 维护：

```bash
# 新增/更新项目绑定（写 db；on conflict 保留已写好的 intro）
python -m issue_keeper team add proj-a --agent-label proj-a-agent --cwd ~/projects/proj-a

# 给某个 agent 写自我介绍（dashboard 团队页展示）
python -m issue_keeper team set-intro agentproc-agent --intro "我是 ..."

# 列出项目绑定 + 团队介绍
python -m issue_keeper team list

# 从旧版 config.yaml（repos 段）一次性迁移进 db
python -m issue_keeper team import --config <旧 config.yaml>
```

介绍最好由 agent 自己生成（最准确）：经 `invoke_agent` 调一次 agent 让它读自己项目仓库后写一段 100-200 字介绍，再用 `team set-intro` 写入（`onboard --gen-intro` 自动做这步）。dashboard 的 `GET /api/team` 与 `GET /api/projects` 都直接读 `projects` 表（`/api/projects` LEFT JOIN issue 计数，0 issue 的项目也显示）。

> 旧版本用 `~/.issue-keeper/team.json` 存介绍。`team import` 时若发现旧 team.json，会把 intro 一次性迁进 db，并把 team.json 改名为 `team.json.migrated` 保留备份。

### onboarding 新项目（onboard）

把一个新项目快速注册进协同，一条命令完成「写 db 绑定 + 生成介绍」：

```bash
DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY python3 -m issue_keeper onboard \
  /path/to/<新项目目录> \
  --agent-label <新项目>-agent --gen-intro
```

参数：`--repo`（项目名，默认取目录名）、`--agent-label`（默认 `<repo>-agent`）、`--profile`（默认 `claude-code`）、`--gen-intro`（调 agent 自动写介绍并写入 db）、`--reload`（立即重载 keeper daemon；不加重载则等下一轮 live-reload 自动生效）。直接写 db 的 `projects` 表，keeper 下一轮自动接手。

### 人类如何参与协同

- **作为参与者**：在 dashboard 选项目提 issue / 评论（顶栏设「当前身份」），或用 `internal create` CLI。注意 issue 正文写成正常 bug/需求，不要写成对 AI 的直接指令（会被 screener 当注入跳过）。
- **作为管理者**：`issue-keeper` 项目本身有 `issue-keeper-agent`（cwd 就是 issue-keeper 仓，有 bash 能力），充当「协同管家」。在 `issue-keeper` 项目提管理类 issue（如「把 foo 项目加进协同」「更新某 agent 介绍」），issue-keeper-agent 会调 `onboard`/`team` CLI 执行并回复。

**开发模式**（改前端代码热更新）：

```bash
# 终端 1：后端
python -m issue_keeper dashboard --port 7433
# 终端 2：前端 dev server（5173），/api 代理到 7433
cd frontend && npm run dev
```

开发时打开 `http://127.0.0.1:5173`。后端开 CORS 允许前端跨域。

> 若未构建前端就启动 dashboard，根路径会提示去 `npm run build`，但 `/api` 仍可用。

dashboard 不依赖 `config.yaml`，直接用 `--db` 指向 internal source 的 SQLite。

### PR 支持

每个 `repos` 条目可选 `monitor_prs: true`，开启后会同时扫描该仓库的 open PR：

- PR 与 issue 的 agent 会话互相隔离（状态 key 为 `pr:N` vs 纯数字 `N`）。
- 处理 PR 本体 + PR 级别的普通评论 + **行内 review comments**。review comments 的 body 会前置 `📍 path:line` 定位行，方便 agent 看上下文。
- PR 可单独配置 label 过滤（`pr_labels`），不写则回退到 `labels`。

## 前置依赖

- Python 3.11+，安装依赖：`pip install -r requirements.txt`
- `agentproc` CLI 已安装——用于调 agent。推荐 PyPI 包（wire 0.3，含 hub 缓存 `_shared` 修复）：`pip install "agentproc>=0.7.1"`；旧版 npm 包 `npm install -g agentproc`（0.4.x，wire 0.2）也能跑但建议升级。若 PATH 上 `agentproc` 解析到非预期版本，用环境变量 `AGENTPROC_BIN=/abs/path/agentproc` 显式指定
- 至少一个 AgentProc profile：`binding.profile` 可以是 hub 名（如 `claude-code`，agentproc 自动从 CDN 拉取并缓存），或本地 `.yaml` 路径
- 对应 agent CLI 已安装并配好凭据（如 `deepseek` CLI + `DEEPSEEK_API_KEY`、`claude` CLI + `ANTHROPIC_API_KEY`）。凭据通过 `binding.env` 注入，支持 `${VAR}` 插值
- `gh` CLI 已安装并登录 —— 仅 `source: github_cli` 需要
- `source: github_token` 需要一个 PAT（classic 或 fine-grained），配在 `github_token` 字段或 `GITHUB_TOKEN` 环境变量
- screener 用的 LLM API 凭据（DeepSeek 推荐）

## 配置

`config.yaml` 只放**引导 + 全局旋钮 + screener + 全局 agent env 模板**。项目绑定（repo/agent_label/cwd/profile/source/monitor_prs/env/intro）存在 `internal.db` 的 `projects` 表——这是 issue-keeper 的**单一配置源**，keeper 每轮 live-reload，db 改动即时生效。

```yaml
poll_interval_secs: 300
state_file: ~/.issue-keeper/state.json
bot_marker: "<!-- issue-keeper-bot -->"
default_timeout_secs: 600
agent_from_user: "issue-keeper"
default_review_agent: ""        # 留空：人提的 issue 处理完停在 review 等人接手

internal_db: ~/.issue-keeper/internal.db   # 项目绑定 + issue 共用库

screener:
  enabled: true
  provider: openai
  api_key: ${DEEPSEEK_API_KEY}
  base_url: "https://api.deepseek.com/v1"
  model: deepseek-chat
  on_unsafe: skip
  max_chars: 8000

# 所有 agent 共用的 LLM 连接（密钥 ${VAR} 引用，实际值在环境变量）。
# load_config 把这份模板套到 db 里每个项目绑定上；某项目要单独 env 时在其 env 列覆盖。
agent_env:
  ANTHROPIC_API_KEY: ${DEEPSEEK_API_KEY}
  ANTHROPIC_BASE_URL: "https://api.deepseek.com/anthropic"
  CLAUDE_MODEL: "deepseek-chat"
```

项目绑定用 CLI 管理（写 db）：

```bash
# 新增/更新一个项目绑定（internal source）
python -m issue_keeper team add proj-a --agent-label proj-a-agent \
  --cwd ~/projects/proj-a --profile claude-code

# github source 也支持（github_token 用 ${VAR} 占位）
python -m issue_keeper team add owner/repo --agent-label proj-a-agent \
  --cwd ~/projects/proj-a --source github_token --github-token '${GITHUB_TOKEN}' \
  --monitor-prs --env ANTHROPIC_API_KEY=${DEEPSEEK_API_KEY}

python -m issue_keeper team remove proj-a
python -m issue_keeper team list
```

从旧版（`repos:` 写在 yaml）升级，一次性迁移：`python -m issue_keeper team import --config <旧 config.yaml>`（旧 team.json 的 intro 也会一并迁进 db，team.json 改名 `.migrated` 备份）。

> db `projects` 表目前存：name / agent_label / cwd / profile / source / github_token / monitor_prs / env / intro。少数旧字段（per-repo `labels` / `pr_labels` / `review_agent` / `timeout_secs`）暂未入库，用全局默认；确需 per-project 差异可后续加列。

### AgentProc profile

`binding.profile` 字段指定 AgentProc profile，issue-keeper 通过 `agentproc` CLI 调用：

| profile 值 | 含义 | 等价命令 |
|---|---|---|
| `deepseek` | hub 名，agentproc 自动拉取缓存 | `agentproc hub run deepseek ...` |
| `claude-code` | hub 名 | `agentproc hub run claude-code ...` |
| `kimi-code` / `codex` / `gemini-cli` / ... | hub 名，见 `agentproc hub list` | 同上 |
| `./profiles/my.yaml` / `/abs/path/x.yaml` | 本地 profile 路径 | `agentproc --profile <path> ...` |

调用时 issue-keeper 自动传：

- `--cwd <binding.cwd>`：agent 工作目录。**这是动态切换项目上下文的关键**——一个 hub profile 通吃所有项目，不用每项目自建 profile
- `--session <session_id>`：续接同一 issue/PR 的 agent 会话
- `--from <agent_from_user>`：来源标识
- `--stdin`：消息通过 stdin 管道传入，避免命令行长度限制（ARG_MAX）。注：agentproc 0.4.x 的 `--stdin` 曾有 bug（消息为空），wire 0.3（NDJSON turn object）已修复，现切回 --stdin
- `--env KEY=VALUE`：`binding.env` 里每个变量都经 `--env` 透传给 agent。**agentproc 0.7.0+ 不再继承父进程全量 env**（只传 infra 集 + profile env 块的 allowlist 变量 + CLI `--env`），所以非 allowlist 的变量（如 `ANTHROPIC_BASE_URL`）必须走 `--env` 才能到 agent；0.4.x 也支持 `--env`，故两版兼容

agent 收到的消息会明确告诉它：

- 你的回复会被作为评论发出，直接说话，不要写"草稿"或"回复如下"
- 你有 bash 工具能力，可以调 `python -m issue_keeper internal create <项目> --title ... --body ... --author <你的 agent_label>` 跨项目提 issue

agent 的工具调用完全由 agent CLI 自己决定（如 deepseek CLI 有 bash 能力），issue-keeper 不参与解析或转发。

## 运行

```bash
# 一次性扫描（适合调试 / cron）
python -m issue_keeper keep --config config.yaml --once

# 常驻 daemon 轮询
python -m issue_keeper keep --config config.yaml

# 开启调试日志（含 screener 判定详情）
python -m issue_keeper keep --config config.yaml --once --log-level DEBUG

# 管理 internal source 的 issue（不依赖 config.yaml）
python -m issue_keeper internal create proj-a --title "..." --body "..." --author alice
python -m issue_keeper internal list proj-a
python -m issue_keeper internal show proj-a 1

# 启动 Web 看板
python -m issue_keeper dashboard            # 默认 127.0.0.1:7433
```

## 测试

```bash
python -m pytest -q                         # 67 个用例：screener / config / internal / 防循环 / github 解析 / dashboard API
```

## 状态文件

默认 `~/.issue-keeper/state.json`，结构：

```json
{
  "repos": {
    "owner-repo-name": {
      "items": {
        "42": {
          "processed": true,
          "session_id": "<agent 返回的会话 uuid>",
          "processed_comment_ids": [123, 456],
          "blocked": false
        },
        "pr:5": {
          "processed": true,
          "session_id": "<PR 独立的会话 uuid>",
          "processed_comment_ids": [],
          "blocked": false
        }
      }
    }
  }
}
```

- 资源 key 规范：issue 为纯数字（如 `"42"`），PR 为 `"pr:5"`，二者会话隔离。
- `blocked: true` 表示该资源被安全过滤拦截，后续不再自动处理。
- 想重新处理某个资源：删除该条目（或整个文件）即可。
- 旧版 state.json（字段名为 `issues` 而非 `items`）会被自动迁移。

## 设计说明

- **回复方式**：issue-keeper 把 agent 的回复作为评论发出，profile 无关，任何 AgentProc profile 都能用。
- **会话连续性**：每个 issue/PR 持有一个 agent 会话 uuid，新评论复用该会话，agent 保持上下文。issue 与 PR 互相隔离。
- **监控范围**：默认只监控 open issue；可按 label 过滤；`monitor_prs` 开启 PR；PR 的普通评论与行内 review comments 均纳入监控（github_token source 自动分页，不丢评论）。
- **防循环**：三层保险（隐藏 marker / 可见前缀 / self_identity）。资源层也识别 marker——AI 自己提的 issue 不触发首次回复，但评论照常处理。
- **安全过滤**：所有投递给主 agent 的内容先过 screener（纯 HTTP LLM 调用，无本地权限）。fail-safe：配置不全拒绝启动。
- **可插拔来源**：keeper 主循环依赖 `IssueSource` 协议而非具体 GitHub。当前支持 `github_cli` / `github_token`。加新来源（github_app / internal / discord / http）是纯加法。
- **运行模式**：常驻 daemon 轮询（默认 300s）+ `--once` 一次性扫描。
