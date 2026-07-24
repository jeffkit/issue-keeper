# AGENTS.md — Issue Keeper

> 监控 GitHub/内部 issue：安全过滤 → AgentProc 调 Agent → 回复写回评论。
> 负责人：jeffkit | 创建：2026-07-10

## 项目概述

issue-keeper 轮询绑定仓库的 issue（可选 PR），先用进程内 HTTP LLM（screener）做无本地权限的注入过滤，再经 `agentproc` 调对应 profile 的 Agent，并把回复作为评论发回。  
项目绑定存在 `internal.db`（非 yaml）；支持 GitHub source 与 internal 看板源；三层防循环（bot marker / 可见前缀 / self_identity）。

**技术栈：** Python, FastAPI, AgentProc, PyYAML, SQLite  
**主仓库：** `git@github.com:jeffkit/issue-keeper.git`

## 架构地图

`keep` 循环：`sources` 拉变更 → `screener` → `keeper` 调 Agent → source 写回评论/状态。  
Dashboard 提供 REST + 前端看板。

关键目录：
- `issue_keeper/__main__.py` — CLI 入口（`python -m issue_keeper`）
- `issue_keeper/keeper.py` — 主循环与 Agent 调用
- `issue_keeper/screener.py` — 安全过滤层
- `issue_keeper/config.py` / `team.py` — 配置与项目绑定
- `issue_keeper/sources/` — `github.py` / `internal.py`
- `issue_keeper/dashboard/` — FastAPI 看板 API
- `frontend/src/` — 看板前端
- `config.example.yaml` — 全局配置模板（screener / patrol 等）
- `tests/` — pytest

## 开发约定

**分支策略：** `main`；PR 合并。

**禁止事项：**
- 禁止关闭或绕过 screener（`screener.enabled` 必须显式配置）
- 禁止把 GitHub token / LLM key 明文写入 yaml 或 db（用 `${ENV}`）
- 禁止去掉防循环 marker / 可见前缀逻辑
- 禁止在 screener 里起子进程或读写业务工作区

## 常用命令

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 再填 screener / agent_env

python -m issue_keeper keep --config config.yaml --once
python -m issue_keeper keep --config config.yaml

python -m issue_keeper team list
python -m issue_keeper onboard ~/projects/foo --agent-label foo-agent --gen-intro

python -m issue_keeper internal board <project>
pytest tests/
```

## 当前状态

**当前里程碑：** {待人工填写}

## 深入阅读

| 文档 | 说明 |
|------|------|
| `README.md` | 机制、防循环、状态机、HitL |
| `config.example.yaml` | 配置项与 CLI 示例 |
