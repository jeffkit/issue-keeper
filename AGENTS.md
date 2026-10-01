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
- `issue_keeper/keeper.py` — 主循环与 Agent 调用；pipeline 派发含 per-repo 契约
  门控（`_pipeline_repo_cfg`：无真门的仓不进管线，回退 legacy）
- `issue_keeper/screener.py` — 安全过滤层
- `issue_keeper/reply.py` — 回评礼仪化（发布前消毒 + LLM 改写，防过程叙述/隐私外泄）
- `issue_keeper/config.py` / `team.py` — 配置与项目绑定；`pipeline_repos` =
  per-repo 管线契约（基线/安装/门/交付/仓规注入/预算，v0.3）
- `issue_keeper/sources/` — `github.py` / `internal.py`
- `issue_keeper/dashboard/` — FastAPI 看板 API
- `frontend/src/` — 看板前端
- `flows/` — issue-pipeline flow（pipeline_mode 时新 issue 首响应的 plaita 管线）
  + `flows/gates/gate_runner.py`（多门调度器：per-gate 预算 + diff 路径条件）
- `config.example.yaml` — 全局配置模板（screener / patrol / reply_polish /
  pipeline_repos 等）
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

# 人工重派：把被消费掉的 issue 放回队列（日限误跳/引擎异常/孤儿 run 之后用）
python -m issue_keeper reopen --config config.yaml jeffkit/recursive 41 42

python -m issue_keeper team list
python -m issue_keeper onboard ~/projects/foo --agent-label foo-agent --gen-intro

python -m issue_keeper internal board <project>

# L3 经验闭环：观测数据驱动的契约变更提案（generate/list/show/apply/reject）
python -m issue_keeper proposals list

pytest tests/
```

## 当前状态

**当前里程碑：** {待人工填写}

## 深入阅读

| 文档 | 说明 |
|------|------|
| `README.md` | 机制、防循环、状态机、HitL |
| `config.example.yaml` | 配置项与 CLI 示例 |
