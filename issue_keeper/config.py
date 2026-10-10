"""配置加载与校验。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .reply import ReplyPolishConfig
from .screener import ScreenerConfig


@dataclass
class KeeperPatrolConfig:
    """keeper 巡检配置：代人类 review / 主动分诊 / 出站通知。"""
    enabled: bool = True
    # 每 N 轮 daemon 跑一次巡检（默认 4 × poll_interval ≈ 20min），防每轮都打扰人类。
    interval_cycles: int = 4
    # inbox 停留超过此秒数的人提 issue 纳入巡检（人类没及时规划）；0 = 只巡检 review 状态。
    stale_inbox_secs: int = 7200
    # 每轮巡检最多处理多少条候选 issue（防爆工作堆积）。
    max_per_cycle: int = 5


@dataclass
class PipelineConsoleConfig:
    """issue-pipeline 定义源（console）：混合形态下定义归 console、本地文件兜底。

    url 为空 = 不接 console，bridge 直接用仓内 issue-pipeline.flow.json（旧行为）。
    """
    url: str = ""
    api_key: str = ""
    flow_id: str = "issue-pipeline"
    # 已发布定义的拉取 TTL（秒）；TTL 内用本地缓存，失败退 stale 缓存。
    refresh_secs: int = 300
    cache_path: str = "~/.issue-keeper/pipeline/flow-cache.json"


@dataclass
class PipelineConfig:
    """issue-pipeline 管线运行配置（混合形态，2026-09-28）。"""
    console: PipelineConsoleConfig = field(default_factory=PipelineConsoleConfig)
    # 影子模式专用 console 目标（放量迁移首阶）：影子副本派往**新系统**，
    # 与 conduit/既有 pipeline.console（旧链路）解耦——改这里不影响 screener/
    # bridge。未配（url/api_key 空）时影子回退 pipeline.console（单机演练用）。
    shadow_console: PipelineConsoleConfig = field(default_factory=PipelineConsoleConfig)
    # 非空时 bridge 把执行上报进 console 观测面（写 plaita:execution:{id} 键 +
    # SSE pubsub 频道）。指向 console 所用 Redis，如 redis://localhost:6379/0。
    observability_redis: str = ""


@dataclass
class GateSpec:
    """单条质量门：命令 + 预算 + 可选 diff 路径条件（fnmatch，空 = 恒触发）。

    gate_runner.py 按序执行；paths 命中本次改动文件（含未提交 + 分支上已提交
    vs base）才跑。条件门的出处是 recursive 的 tui-mutants/cli-mutants——
    触及特定 crate 才需要跑、且预算要 40-60min，单命令门模型装不下。
    """
    name: str
    command: str
    timeout_secs: int = 900
    paths: list = field(default_factory=list)
    # 确定性自愈命令（可选）：门失败时先跑它再重检一次。只配机械可修的门
    # （fmt → `cargo fmt --all`）；语义门（clippy/test）别配，交给 fix-loop。
    autofix: str = ""


@dataclass
class PipelineRepoConfig:
    """per-repo 管线契约（v0.3）：把「基线/安装/验收/交付/仓规」从全局硬编码
    降为每仓自带。

    背景：通用 flow 曾只参数化了 test_command 一项——基线硬编码 origin/main
    （argusai 家族是 develop）、push_mode 全局直推 main（绕过 PR 制仓的发布流）、
    17/18 仓空门跑 `true` 恒过、review 红线内嵌 recursive 专属内容。
    """
    # false = 该仓不走管线（回退 legacy 单 agent 路径）
    enabled: bool = True
    # pipeline = issue-pipeline flow（默认）；v2 = self-improve v2 引擎
    # （recursive .dev/flows 的库节点版：agentrun/gate/git_publish）
    engine: str = "pipeline"
    # full = 九段管线；readonly = 只调查不开工不交付（数据/分发/镜像仓）
    mode: str = "full"
    # worktree 基线 & 集成分支（argusai 家族 = develop；deepseek-harness = master）
    base_branch: str = "main"
    # worktree 建立后跑一次（fresh worktree 无 node_modules/.venv，TS/Python 仓必须装）
    setup_command: str = ""
    setup_timeout_secs: int = 1800
    # 单命令门（gates 优先；都没有 = 不进管线，防「空门恒过」回评撒谎）
    test_command: str = ""
    gates: list = field(default_factory=list)      # list[GateSpec]
    gate_timeout_secs: int = 0                     # 0 = gates 预算和 + 300s 缓冲
    # 空 = 继承全局 pipeline_push_mode / pipeline_review_mode
    push_mode: str = ""                            # branch | pr | main | none
    review_mode: str = ""                          # auto | human
    # v2 引擎 AGENTRUN 的 agent/reviewer 名（agents.json 键）。空 = 引擎默认
    # （v2_bridge：实现/评审均 glm53-flash）。这是 dispatch payload 里
    # agent/reviewer 字段的上游——此前只读不写，是死旋钮（2026-10-01 impl
    # 模型切档时发现并接线）。
    agent: str = ""
    reviewer: str = ""
    # v2 引擎宿主的额外 env（如 RECURSIVE_HOST_V3=1 灰度开关、
    # RECURSIVE_NODE_RETRIES、RECURSIVE_RUN_DEADLINE）。经 dispatch payload
    # 透传给 v2_bridge 子进程（照 agent/reviewer 字段先例）。
    engine_env: dict = field(default_factory=dict)
    # engine=v2-console 时派发到 console 的 flow id（已发布的 self-improve v2
    # flow）。空 = "self-improve-v2"。
    console_flow_id: str = ""
    # 影子模式（放量迁移首阶）：本地照常主执行（engine 不变），**同时**旁路派发
    # 一份到 console 只做验证——影子那份不落地（不发评论/不 push/不改仓）。用于
    # 「新旧并行对账」：同一 issue 两套结论差异是影子期核心产出。默认关。
    # 红线：影子副本绝不写 console-exec.json 在途锚（否则 reaper 会把本地主的
    # 台账当成 console 在途而误判收尾）。
    shadow: bool = False
    # 影子副本派发到 console 的 flow id（空 = 复用 console_flow_id / 默认 flow）。
    shadow_flow_id: str = ""
    # 注入各 agent 段提示词的仓内知识（红线/路由/文档惯例）——替代硬编码在
    # 通用 flow 里的 recursive 专属内容
    review_notes: str = ""
    triage_notes: str = ""
    doc_notes: str = ""
    # .github/** 禁改名单的显式例外（#11，ilink-hub #39 实证）：CI 修复单的
    # 修复对象就是 .github/workflows/*，硬拦会让实现全绿却在 guard 被拦死。
    # 这里声明允许触碰的 .github 子路径前缀（如 [".github/workflows/"]）；
    # **只在 issue 带显式 opt-in 标签（ci-fix）时生效**——声明权在仓库契约，
    # 触发权在单条 issue，注入者两者都拿不到就仍被拦。
    allow_github_paths: list = field(default_factory=list)

    def __post_init__(self):
        # #11：前缀语义，剥掉 glob 星号——`'.github/workflows/**'` 与
        # `'.github/workflows/'` 必须等价，否则 startswith 门槛两头对不上
        self.allow_github_paths = [str(p).strip().rstrip("*")
                                   for p in (self.allow_github_paths or [])
                                   if str(p).strip()]
    # 段级预算覆盖（键：investigate/plan/implement/review/fix_review/fix_test/
    # document）；缺省用 flow 内置值（按 cargo 冷构建调优的那组）
    timeout_overrides: dict = field(default_factory=dict)

    def resolved_push_mode(self, global_default: str) -> str:
        return self.push_mode or global_default

    def resolved_review_mode(self, global_default: str) -> str:
        return self.review_mode or global_default

    def effective_gate_timeout(self) -> int:
        """整门预算：显式 > gates 之和+缓冲 > 单命令默认 2400。"""
        if self.gate_timeout_secs > 0:
            return self.gate_timeout_secs
        if self.gates:
            return sum(int(g.timeout_secs) for g in self.gates) + 300
        return 2400

    def has_gate(self) -> bool:
        return bool(self.test_command.strip() or self.gates)


@dataclass
class RepoBinding:
    repo: str
    profile: str
    labels: list[str] = field(default_factory=list)
    monitor_prs: bool = False
    pr_labels: list[str] = field(default_factory=list)
    source: str = "github_cli"
    # agent 可见身份标签。出现在 agent 发出的每条评论正文前缀里：
    #   [issue-keeper:<agent_label>]
    # 也用于跨项目时让别的仓库识别"是哪个 agent 来的"。默认 fallback 到 agent_from_user。
    agent_label: str = ""
    # github_token source 用的 PAT。支持 ${ENV_VAR} 展开。空则回退到环境变量 GITHUB_TOKEN/GH_TOKEN。
    github_token: str = ""
    # internal source 用的 SQLite 路径。支持 ${ENV_VAR} 展开。空则用全局默认 ~/.issue-keeper/internal.db。
    internal_db: str = ""
    # agent 工作目录（agentproc --cwd）。agent 会以这个目录为上下文跑。
    # 对 GitHub source 应为仓库代码本地路径；对 internal source 是项目代码路径。
    cwd: str = ""
    # 传给 agent 子进程的额外 env（API key、模型等）。支持 ${VAR} 插值。
    env: dict[str, str] = field(default_factory=dict)
    # 该仓库专用的 review agent（覆盖全局 default_review_agent）。
    # 人提的 issue 处理完后由此 agent review。
    review_agent: str = ""
    poll_interval_secs: int | None = None
    session_prefix: str = "issue-keeper"
    timeout_secs: int | None = None
    # 角色：'agent'（默认，负责本仓代码与 issue）/ 'keeper'（管理向，帮人类管 issue、
    # 代理人类跨项目提问、回弹给人类时优先解答、必要时用 HitL 联系人类）。keeper 角色
    # 在 keeper.py 里会拿到一套专属系统提示词，区别于普通代码 agent。
    role: str = "agent"

    def effective_poll_interval(self, default: int) -> int:
        return self.poll_interval_secs if self.poll_interval_secs is not None else default

    def effective_timeout(self, default: int) -> int:
        return self.timeout_secs if self.timeout_secs is not None else default

    def effective_review_agent(self, global_default: str) -> str:
        """该仓库的 review agent。优先 binding.review_agent，否则用全局默认。"""
        return self.review_agent or global_default

    @property
    def repo_slug(self) -> str:
        """repo 标识里非法字符替换为 -，用于 session id / 状态 key。

        只保留 [A-Za-z0-9_.-]，其余（包括 / : + ~ 空格等）统一替换为 -，
        并合并连续 -，避免污染文件系统路径与 JSON key。
        """
        import re
        slug = re.sub(r"[^\w.\-]", "-", self.repo)
        slug = re.sub(r"-{2,}", "-", slug)
        return slug.strip("-")


@dataclass
class Config:
    poll_interval_secs: int = 300
    state_file: Path = Path("~/.issue-keeper/state.json")
    bot_marker: str = "<!-- issue-keeper-bot -->"
    default_timeout_secs: int = 600
    agent_from_user: str = "issue-keeper"
    # 默认 review agent：人提的 issue 处理完后，由这个 agent 先 review。
    # 为空则人提的 issue 处理完停在 review 状态等人接手。
    default_review_agent: str = ""
    screener: ScreenerConfig = field(default_factory=lambda: ScreenerConfig(
        enabled=False, provider="openai", api_key=None, base_url=None, model=None,
        on_unsafe="skip", max_chars=8000,
    ))
    repos: list[RepoBinding] = field(default_factory=list)
    # 单一人类模型：keeper 代谁 review / HitL 推给谁。所有人类都统一推给这一个人。
    human_label: str = "human"
    keeper_patrol: KeeperPatrolConfig = field(default_factory=KeeperPatrolConfig)
    # keeper agent 的调用超时（秒）。keeper 可能用 HitL 等人类回复（最长 1 小时），
    # 所以默认比普通 agent（default_timeout_secs）大。仅对 role=keeper 的绑定生效。
    keeper_timeout_secs: int = 3900
    # ── plaita 管线模式（v0.2.1，2026-09-27）─────────────────────────
    # true 时新 issue 首响应交给 issue-pipeline flow（flows/pipeline_bridge.py
    # 子进程），单体 claude CLI 路径保留为回退。评论层处理仍走 legacy。
    pipeline_mode: bool = False
    # ── 派发权归属（2026-10-07，keeper→flow 迁移）──────────────────────
    # "keeper"（默认）：intake（screener + 首次派发）由 keeper 承担，行为不变。
    # "flow"：派发权已移交 keeper-shadow flow——keeper 跳过管线仓的「首次处理」
    # 段（screener + 派发），只保留 reaper / 评论 / reopen / 巡检等收尾职责；
    # 防双筛（两处 screener 会给同单发两份通告）与双派。
    pipeline_dispatch_owner: str = "keeper"
    pipeline_bridge: Path = Path(
        "~/projects/infra4agent/issue-keeper/flows/pipeline_bridge.py")
    # 桥子进程整跑超时（秒）：到点 killpg 整个进程组（agent 子树一并清）
    pipeline_timeout_secs: int = 5400
    # ── engine=v2-console（G5/G6，2026-10-02）────────────────────────
    # zombie 判定兜底阈（秒）：running 执行既无活性信号也无节点史时，
    # last_update_time 年龄超过它 → 判死（cancel + 台账 engine_error 行走重派）。
    # last_update_time 只在步界持久化时刷新——长 impl 节点（≤70min）期间正常
    # 老化，阈值须高于最长节点预算。
    console_zombie_secs: int = 7200
    # 活性停滞判死阈（秒）：节点史里最新 ended_at 停滞超过它且无租约/心跳/
    # 在跑节点 → 确证死亡收尸。必须远大于节点间调度空档（末节点结束到下一
    # 节点启动/终态落盘只有秒级）以免 reaper 误杀健康 run，但应明显小于
    # console_zombie_secs（有节点史的 run 不该再等满无 TTL 键的兜底线）。
    console_node_stale_secs: int = 1800
    # 兜底收尸线（秒，plaita#28 验收第 2 条）：v2-console run 在途总时长越过它
    # 即按 engine_error 收尾重派——**不要求**先满足「0 租约/无心跳」。治理
    # 「执行键无 TTL 永不消失 + console 终态滞后（plaita#52）→ run 无限期挂在
    # 在途集合」。取值须 ≥ 该 flow 全程总时长上限：节点预算之和（非单节点
    # 上限）再叠加节点重试放大（实测单节点重试过 4 次，plaita#28）——健康
    # 长跑贴近总预算属正常形态，靠心跳/节点活性区分死活，不要靠压低预算。
    # 0 = 关闭兜底线，只靠 zombie 判据。
    console_inflight_budget_secs: int = 10800
    # error 执行 resume-retry 次数上限（G1：续原 execution 从断点步进，免整跑
    # 重做）。超限落 engine_error 台账行走既有重派/升级语义。
    console_retry_max: int = 1
    # 「已派发未消费」宽限期（秒，plaita#18）：console POST 只把任务入队 Redis，
    # 执行记录由 worker **消费时**才首次落盘——派发到被消费之间
    # GET /api/executions/<id> 恒为 404。在此宽限期内把 404 判为**排队中**
    # （不动作、不重派），超期仍 404 才落既有 engine_error 自愈路径。宽限期须
    # > 最长排队等待（背压队列 + worker 满载时的排队是设计内行为），默认 1800s。
    console_queue_grace_secs: int = 1800
    # run 级 deadline 注入的提前量（秒）：派发 engine=v2 的 run 时，keeper 把
    # RECURSIVE_RUN_DEADLINE = 派发时刻 + pipeline_timeout_secs - 本值 注入
    # engine_env，让 recursive v3 宿主在 reaper SIGKILL 前先到点优雅退出
    # （checkpoint/verdict 落盘、台账带 node_retry_exhausted，reaper 不再自动
    # 重派白烧一轮）。engine_env 里显式配了该键则不覆盖。0 = 不留收尾窗口
    # （护栏退回纯 killpg）。
    run_deadline_margin_secs: int = 300
    pipeline_push_mode: str = "branch"     # branch | main（main 需自行接受直推风险）
    pipeline_review_mode: str = "auto"     # auto | human（human 且 risk=high 才 HITL）
    # repo_full → 质量门命令（如 "cargo test --workspace"）；缺省/空 = 跳过门禁并注明
    pipeline_test_commands: dict = field(default_factory=dict)
    # repo_full → PipelineRepoConfig（v0.3 per-repo 契约）。未登记的仓：无真门
    # 不进管线（回退 legacy），除非旧 pipeline_test_commands 给了命令（兼容）。
    pipeline_repos: dict = field(default_factory=dict)
    # 作者 allowlist：非空时仅名单内作者的新 issue 触发 agent（大小写不敏感）
    author_allowlist: list = field(default_factory=list)
    # 显式豁免标签（大小写不敏感）：带任一标签的 issue/PR 被 keeper 完全跳过——
    # 首响、评论、screener 一律不碰。用途：人工协调线程/公告等不想被 automation
    # 消费的 issue；在所有触发门槛之前生效，摘掉标签后下一轮恢复处理。
    opt_out_labels: list = field(default_factory=lambda: ["keeper-ignore"])
    # 豁免作者日限的作者名单（大小写不敏感）：名单内作者触发次数不限。
    # 典型用途：核心贡献者账号批量提 issue 时不被日限积压（2026-09-28 okguitar）。
    author_daily_limit_exempt: list = field(default_factory=list)
    # 同作者每日最多触发次数（读 pipeline runs.jsonl 台账，防资源滥用）
    author_daily_limit: int = 3
    # 同一 issue 每日最多管线 run 次数（读 runs.jsonl 台账）。防终态（guarded/
    # engine_error 等）被反复重派的空转：单次 run 半小时起步，#40 实证一夜连烧
    # 5 轮全部超时/拦截。0 = 不限；到顶后本轮跳过、不消费首次响应（次日自动重试，
    # 人工处置后也可 reopen 立即重派）。
    pipeline_issue_daily_limit: int = 2
    # 评论层异步 agent 的全局并发上限（2026-10-01 评论层后台化）：跨仓计数，
    # 同 issue 内天然串行（同 issue 有在途任务则不再派发新评论）。
    comment_max_in_flight: int = 3
    # 同时刻在跑的管线 run 上限（跨仓；worktree 天然隔离不同 issue）。
    # 2026-09-29 派发解耦后 keeper 不再被长 run 阻塞，这个池子才有意义。
    pipeline_max_in_flight: int = 2
    # 管线 agent/reviewer 档位的**全局兜底**（agents.json 键，如 "glm53-flash"
    # / "deepseek-flash"）。per-repo 的 `pipeline_repos[repo].agent` 优先级更高，
    # 二者都空才回退到硬编码 `glm53-flash`。
    #
    # 为什么要有全局开关（2026-10-10 实证）：沙箱流（sbx）的 agent 端点来自
    # `plaita-nodes` 的 provider 翻译（`~/.plaita/providers.json` 的
    # apiBase/apiKey），**不是** keeper 的 `agent_env`——所以 GLM 配额烧穿时
    # 只切 `agent_env` 管不到沙箱，沙箱 agent 仍打 GLM 429 秒退、run 无限重投
    # （实测 07:33 沙箱内仍 429；`agent=glm53-flash` 写死在派发 params 里）。
    # 有了本字段，切档 = 改一处配置（`model_tier.sh` 已同步改写），
    # 全仓下次派发即生效（无需重启 worker）。
    pipeline_default_agent: str = ""
    # reviewer 档位独立于 impl：空 = 跟随 pipeline_default_agent。
    pipeline_default_reviewer: str = ""
    # 派发优先级仓（仓库全名，如 ["owner/repo"]）：名单内的仓在每轮扫描序里排最前，
    # 槽位释放时先被派发——治理「排末位的仓被前序仓的失败-重试循环长期饿死」
    # （2026-10-05 实证：recursive 批次被饿 8h）。空默认=纯扫描序，行为不变。
    pipeline_priority_repos: tuple[str, ...] = ()
    # 按仓派发配额（S5，2026-10-07）：{仓库全名: 本仓在途 run 上限}，与全局
    # pipeline_max_in_flight 叠加生效——全局闸管总量，本闸管单仓。治理「长任务仓
    # （Rust 构建 30-60min+）长期占满全部槽位、轻仓队列零派发」（2026-10-07 实证：
    # recursive 独占 3/3，plaita 队列 6+ 单持续等槽）。空默认=不设限，行为不变；
    # 上限 0 = 该仓暂停派发。
    pipeline_repo_limits: dict[str, int] = field(default_factory=dict)
    # 派发时在 issue 上发一条「已认领」评论：多会话/多人并行的机器可读信号
    # （2026-09-29 与另一会话在同一 issue 撞车的教训）。
    pipeline_claim_comment: bool = True
    # 失败类（status=failed）自动重派额度（#8）：首败自动重试一轮（台账 +1 run
    # 记录，退避一个 poll 周期），耗尽后近 12h 连续 ≥2 次失败才升级人工。
    # 0 = 不重试（单次 failed 直接终态收尾；连击 ≥2 仍升级人工）。
    failed_auto_retry: int = 1
    # 升级人工时给 issue 打的标签（#8）：需先在仓库内存在（本工具不自动建标签）；
    # internal 看板源无 label 接口，退化为仅评论。勿写进 opt_out_labels。
    pipeline_needs_human_label: str = "needs-human"
    # 混合形态（定义归 console + 观测进 console，执行留本地 bridge）
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    # 回评礼仪化：agent 原始输出发布前消毒 + LLM 改写为 issue 礼仪评论。
    # 连接字段缺省继承 screener 的 LLM 凭据（零配置可用）。
    reply_polish: ReplyPolishConfig = field(default_factory=ReplyPolishConfig)

    @property
    def state_path(self) -> Path:
        return self.state_file.expanduser()

    def pipeline_repo_cfg(self, repo_full: str) -> PipelineRepoConfig:
        """该仓的管线契约：pipeline_repos 登记 > 旧 pipeline_test_commands 兜底 >
        全默认（无门 → 不进管线）。返回副本，调用方可安全改。"""
        cfg = self.pipeline_repos.get(repo_full)
        if cfg is None:
            cfg = PipelineRepoConfig(test_command=self.pipeline_test_commands.get(repo_full, ""))
        elif not cfg.test_command and not cfg.gates:
            legacy = self.pipeline_test_commands.get(repo_full, "")
            if legacy:
                cfg.test_command = legacy
        return cfg


def _load_pipeline_repos(raw: Any) -> dict[str, PipelineRepoConfig]:
    """解析 pipeline_repos 映射（repo_full → 契约）。逐项校验，坏值大声报错。"""
    if not isinstance(raw, dict):
        raise ValueError("pipeline_repos 必须是映射（repo_full: 契约）")
    out: dict[str, PipelineRepoConfig] = {}
    for repo, item in raw.items():
        repo = str(repo).strip()
        if not isinstance(item, dict):
            raise ValueError(f"pipeline_repos[{repo}] 必须是映射")
        mode = str(item.get("mode") or "full").strip()
        if mode not in ("full", "readonly"):
            raise ValueError(f"pipeline_repos[{repo}].mode 只能是 full|readonly，得到 {mode!r}")
        engine = str(item.get("engine") or "pipeline").strip()
        if engine not in ("pipeline", "v2", "v2-console"):
            raise ValueError(f"pipeline_repos[{repo}].engine 只能是 pipeline|v2|v2-console，得到 {engine!r}")
        push_mode = str(item.get("push_mode") or "").strip()
        if push_mode and push_mode not in ("branch", "pr", "main", "none"):
            raise ValueError(
                f"pipeline_repos[{repo}].push_mode 只能是 branch|pr|main|none，得到 {push_mode!r}")
        review_mode = str(item.get("review_mode") or "").strip()
        if review_mode and review_mode not in ("auto", "human"):
            raise ValueError(f"pipeline_repos[{repo}].review_mode 只能是 auto|human")
        base_branch = str(item.get("base_branch") or "main").strip() or "main"

        gates: list[GateSpec] = []
        for i, g in enumerate(item.get("gates") or []):
            if not isinstance(g, dict) or not str(g.get("name") or "").strip() \
                    or not str(g.get("command") or "").strip():
                raise ValueError(
                    f"pipeline_repos[{repo}].gates[{i}] 需要 name 与 command 字段")
            gates.append(GateSpec(
                name=str(g["name"]).strip(),
                command=str(g["command"]),
                timeout_secs=max(30, int(g.get("timeout_secs", 900))),
                paths=[str(p) for p in (g.get("paths") or []) if str(p).strip()],
                autofix=str(g.get("autofix") or "").strip(),
            ))

        timeout_overrides = {str(k): max(60, int(v))
                             for k, v in (item.get("timeout_overrides") or {}).items()}
        out[repo] = PipelineRepoConfig(
            enabled=bool(item.get("enabled", True)),
            mode=mode,
            engine=engine,
            base_branch=base_branch,
            setup_command=str(item.get("setup_command") or ""),
            setup_timeout_secs=max(60, int(item.get("setup_timeout_secs", 1800))),
            test_command=str(item.get("test_command") or ""),
            gates=gates,
            gate_timeout_secs=max(0, int(item.get("gate_timeout_secs", 0))),
            push_mode=push_mode,
            review_mode=review_mode,
            agent=str(item.get("agent") or "").strip(),
            reviewer=str(item.get("reviewer") or "").strip(),
            engine_env={str(k): str(v) for k, v in (item.get("engine_env") or {}).items()},
            console_flow_id=str(item.get("console_flow_id") or "").strip(),
            shadow=bool(item.get("shadow", False)),
            shadow_flow_id=str(item.get("shadow_flow_id") or "").strip(),
            review_notes=str(item.get("review_notes") or ""),
            triage_notes=str(item.get("triage_notes") or ""),
            doc_notes=str(item.get("doc_notes") or ""),
            allow_github_paths=[str(p).strip().rstrip("*")
                                for p in (item.get("allow_github_paths") or [])
                                if str(p).strip()],
            timeout_overrides=timeout_overrides,
        )
    return out


def _expand_path(v: Any) -> Path:
    return Path(v).expanduser()


def _expand_env(value: Any) -> str:
    """展开字符串里的 ${VAR}。其他类型先转 str 再展开。"""
    s = str(value)
    for k, mv in os.environ.items():
        s = s.replace(f"${{{k}}}", mv)
    return s


def _load_screener(raw: dict[str, Any]) -> ScreenerConfig:
    """解析 screener 段。enabled 默认 None（未声明即报错），强制用户显式选择。"""
    enabled_raw = raw.get("enabled")
    if enabled_raw is None:
        raise ValueError(
            "必须显式配置 screener.enabled（true 启用安全过滤；false 明确放行）。"
            "不配置 screener 段同样会报错——这是 fail-safe 设计。"
        )
    enabled = bool(enabled_raw)

    on_unsafe = (raw.get("on_unsafe") or "skip").strip()
    if on_unsafe not in ("skip", "comment"):
        raise ValueError("screener.on_unsafe 只能是 'skip' 或 'comment'")

    provider = (raw.get("provider") or "openai").strip().lower()
    if provider not in ("openai", "anthropic"):
        raise ValueError("screener.provider 只能是 'openai' 或 'anthropic'")

    backend = (raw.get("backend") or "classic").strip().lower()
    if backend not in ("classic", "decision", "flow"):
        raise ValueError("screener.backend 只能是 'classic'、'decision' 或 'flow'")
    min_confidence = float(raw.get("min_confidence", 0.8))
    if not 0 < min_confidence <= 1:
        raise ValueError("screener.min_confidence 需在 (0, 1] 区间")

    api_key = _expand_env(raw.get("api_key") or "").strip() or None
    base_url = _expand_env(raw.get("base_url") or "").strip() or None
    model = _expand_env(raw.get("model") or "").strip() or None

    creds_profile = (raw.get("credentials_from_profile") or "").strip()
    if creds_profile:
        from .screener import _load_credentials_from_profile
        creds = _load_credentials_from_profile(creds_profile)
        # profile 推断出的 provider / api_key / base_url / model 作为兜底，显式配置优先
        provider = provider if raw.get("provider") else creds["provider"]
        api_key = api_key or creds["api_key"]
        base_url = base_url or creds["base_url"]
        model = model or creds["model"]

    max_chars = int(raw.get("max_chars", 8000))

    if backend == "decision" and provider == "anthropic":
        # 放在 credentials_from_profile 推断之后：profile 也可能推出 anthropic
        raise ValueError(
            "screener.backend=decision 暂只支持 provider: openai"
            "（anthropic 协议请用 backend: classic）")

    console = raw.get("console") or {}
    if not isinstance(console, dict):
        raise ValueError("screener.console 需要是映射（url/api_key/flow_id/refresh_secs/cache_path）")

    extra_body = raw.get("extra_body")
    if extra_body is not None and not isinstance(extra_body, dict):
        raise ValueError("screener.extra_body 需要是映射（如 {reasoning_effort: low}）")

    trusted_raw = raw.get("trusted_authors") or []
    if not isinstance(trusted_raw, (list, tuple)):
        raise ValueError("screener.trusted_authors 需要是列表（GitHub 登录名，如 [alice, bob]）")
    trusted_authors = tuple(str(a).strip().lower() for a in trusted_raw if str(a).strip())

    cfg = ScreenerConfig(
        enabled=enabled,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        on_unsafe=on_unsafe,
        max_chars=max_chars,
        extra_body=extra_body,
        backend=backend,
        min_confidence=min_confidence,
        console_url=_expand_env(console.get("url") or "").strip() or None,
        console_api_key=_expand_env(console.get("api_key") or "").strip() or None,
        console_flow_id=(console.get("flow_id") or "issue-screener").strip(),
        console_refresh_secs=int(console.get("refresh_secs", 300)),
        console_cache_path=_expand_env(console.get("cache_path") or "").strip() or None,
        trusted_authors=trusted_authors,
    )

    if cfg.enabled:
        if backend == "flow":
            missing = [k for k in ("console_url", "console_api_key") if not getattr(cfg, k)]
            if missing:
                raise ValueError(
                    f"screener.backend=flow 但缺少: {', '.join(missing)}。"
                    f"请在 screener.console 下配置 url 与 api_key。"
                )
        else:
            missing = [k for k in ("api_key", "base_url", "model") if not getattr(cfg, k)]
            if missing:
                raise ValueError(
                    f"screener.enabled=true 但缺少: {', '.join(missing)}。"
                    f"请配置 screener.api_key/base_url/model，或 screener.credentials_from_profile。"
                )
    return cfg


def _load_repos_from_db(internal_db: str, agent_env: dict[str, str]) -> list[RepoBinding]:
    """项目绑定从 db 的 projects 表加载（单一源）。

    每个绑定的 env = 全局 agent_env 模板 + 该项目 env 列的覆盖（都已展开 ${VAR}）。
    internal_db 用全局路径（projects 表与 issue 同库）。
    """
    from .sources.internal import InternalSource

    loader_binding = RepoBinding(
        repo="", profile="", source="internal",
        agent_label="config-loader", internal_db=internal_db,
    )
    src = InternalSource(binding=loader_binding)
    repos: list[RepoBinding] = []
    for m in src.list_projects_meta():
        # 项目 env 列存的是 ${VAR} 占位，这里展开后合并到全局模板（项目覆盖全局）
        proj_env = {str(k): _expand_env(v) for k, v in (m.get("env") or {}).items()}
        env = {**agent_env, **proj_env}
        repos.append(
            RepoBinding(
                repo=m["name"],
                profile=m.get("profile") or "claude-code",
                source=m.get("source") or "internal",
                agent_label=m.get("agent_label") or "",
                github_token=_expand_env(m.get("github_token") or ""),
                cwd=m.get("cwd") or "",
                monitor_prs=bool(m.get("monitor_prs", False)),
                env=env,
                internal_db=internal_db,
                role=m.get("role") or "agent",
            )
        )
    return repos


def _load_reply_polish(raw: dict[str, Any], screener: ScreenerConfig) -> ReplyPolishConfig:
    """解析 reply_polish 段。连接字段未显式配置时继承 screener 的 LLM 凭据。"""
    rp_raw = raw.get("reply_polish") or {}
    if not isinstance(rp_raw, dict):
        raise ValueError("reply_polish 必须是映射（enabled/provider/api_key/base_url/model/…）")
    provider = str(rp_raw.get("provider") or screener.provider or "openai").strip().lower()
    if provider not in ("openai", "anthropic"):
        raise ValueError("reply_polish.provider 只能是 'openai' 或 'anthropic'")
    extra_body = rp_raw.get("extra_body")
    if extra_body is not None and not isinstance(extra_body, dict):
        raise ValueError("reply_polish.extra_body 需要是映射（如 {reasoning_effort: low}）")
    return ReplyPolishConfig(
        enabled=bool(rp_raw.get("enabled", True)),
        provider=provider,
        api_key=_expand_env(rp_raw.get("api_key") or "").strip() or screener.api_key,
        base_url=_expand_env(rp_raw.get("base_url") or "").strip() or screener.base_url,
        model=_expand_env(rp_raw.get("model") or "").strip() or screener.model,
        extra_body=extra_body if extra_body is not None else screener.extra_body,
        max_chars=max(1000, int(rp_raw.get("max_chars", 16000))),
        timeout_secs=max(10, int(rp_raw.get("timeout_secs", 60))),
        min_chars=max(0, int(rp_raw.get("min_chars", 120))),
    )


def _load_pipeline(raw: dict) -> PipelineConfig:
    """解析 pipeline 段（混合形态：定义源 console + 观测上报 Redis）。url 缺省=纯本地模式。"""
    p = raw.get("pipeline") or {}
    if not isinstance(p, dict):
        raise ValueError("pipeline 需要是映射（console / observability_redis）")
    console_raw = p.get("console") or {}
    if not isinstance(console_raw, dict):
        raise ValueError("pipeline.console 需要是映射（url/api_key/flow_id/refresh_secs/cache_path）")
    shadow_raw = p.get("shadow_console") or {}
    if not isinstance(shadow_raw, dict):
        raise ValueError("pipeline.shadow_console 需要是映射（url/api_key/flow_id）")
    return PipelineConfig(
        console=PipelineConsoleConfig(
            url=_expand_env(console_raw.get("url") or "").strip(),
            api_key=_expand_env(console_raw.get("api_key") or "").strip(),
            flow_id=(console_raw.get("flow_id") or "issue-pipeline").strip(),
            refresh_secs=max(30, int(console_raw.get("refresh_secs", 300))),
            cache_path=_expand_env(console_raw.get("cache_path") or "").strip()
            or "~/.issue-keeper/pipeline/flow-cache.json",
        ),
        shadow_console=PipelineConsoleConfig(
            url=_expand_env(shadow_raw.get("url") or "").strip(),
            api_key=_expand_env(shadow_raw.get("api_key") or "").strip(),
            flow_id=(shadow_raw.get("flow_id") or "").strip(),
            refresh_secs=max(30, int(shadow_raw.get("refresh_secs", 300))),
        ),
        observability_redis=_expand_env(p.get("observability_redis") or "").strip(),
    )


def _order_repos(repos: list["RepoBinding"], priority: tuple[str, ...]) -> list["RepoBinding"]:
    """按派发优先级重排仓库：priority 名单内的仓排最前，组内保持原序（稳定）。

    keeper 每轮按本序扫描并在同轮内先到先得地占 pipeline_max_in_flight 槽位；
    把要优先消化的仓放前面，即可在每次槽位释放时先被派发。空名单 = 原序返回
    （纯扫描序，行为不变）。"""
    prio = {str(p).strip() for p in priority if str(p).strip()}
    if not prio:
        return repos
    return ([b for b in repos if b.repo in prio]
            + [b for b in repos if b.repo not in prio])


def load_config(path: str | os.PathLike) -> Config:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    state_file = raw.get("state_file") or "~/.issue-keeper/state.json"

    screener_raw = raw.get("screener")
    if screener_raw is None:
        raise ValueError(
            "配置文件缺少 screener 段。issue-keeper 必须显式声明安全过滤策略：\n"
            "  screener:\n"
            "    enabled: true             # 启用过滤（推荐）\n"
            "    credentials_from_profile: issue-keeper-glm\n"
            "  或明确放行（不推荐，仅用于本地调试）：\n"
            "  screener:\n"
            "    enabled: false\n"
        )
    screener = _load_screener(screener_raw)

    # 全局 agent env 模板（所有 agent 共用的 LLM 连接，密钥用 ${VAR} 引用）
    agent_env_raw = raw.get("agent_env") or {}
    if not isinstance(agent_env_raw, dict):
        raise ValueError("agent_env 必须是映射（key: value）")
    agent_env = {str(k): _expand_env(v) for k, v in agent_env_raw.items()}

    # 项目绑定所在 db（projects 表 + issue 同库）
    internal_db = os.path.expanduser(
        _expand_env(raw.get("internal_db") or "~/.issue-keeper/internal.db")
    )

    repos = _load_repos_from_db(internal_db, agent_env)

    # 单一人类标签（keeper 代人类 review / HitL 推送对象）
    human_label = (raw.get("human_label") or "human").strip() or "human"

    # keeper 巡检配置
    patrol_raw = raw.get("keeper_patrol") or {}
    patrol = KeeperPatrolConfig(
        enabled=bool(patrol_raw.get("enabled", True)),
        interval_cycles=max(1, int(patrol_raw.get("interval_cycles", 4))),
        stale_inbox_secs=max(0, int(patrol_raw.get("stale_inbox_secs", 7200))),
        max_per_cycle=max(1, int(patrol_raw.get("max_per_cycle", 5))),
    )

    pipeline_test_raw = raw.get("pipeline_test_commands") or {}
    if not isinstance(pipeline_test_raw, dict):
        raise ValueError("pipeline_test_commands 必须是映射（repo_full: 测试命令）")
    pipeline_repos = _load_pipeline_repos(raw.get("pipeline_repos") or {})
    allowlist_raw = raw.get("author_allowlist") or []
    if not isinstance(allowlist_raw, list):
        raise ValueError("author_allowlist 必须是列表")

    priority_raw = raw.get("pipeline_priority_repos") or []
    if not isinstance(priority_raw, (list, tuple)):
        raise ValueError("pipeline_priority_repos 需要是列表（仓库全名，如 [owner/repo]）")
    pipeline_priority_repos = tuple(str(x).strip() for x in priority_raw if str(x).strip())
    limits_raw = raw.get("pipeline_repo_limits") or {}
    if not isinstance(limits_raw, dict):
        raise ValueError("pipeline_repo_limits 需要是映射（仓库全名 → 上限，如 {owner/repo: 2}）")
    pipeline_repo_limits = {str(k).strip(): max(0, int(v))
                            for k, v in limits_raw.items() if str(k).strip()}
    repos = _order_repos(repos, pipeline_priority_repos)

    cfg = Config(
        poll_interval_secs=int(raw.get("poll_interval_secs", 300)),
        state_file=_expand_path(state_file),
        bot_marker=raw.get("bot_marker", Config.bot_marker),
        default_timeout_secs=int(raw.get("default_timeout_secs", 600)),
        agent_from_user=(raw.get("agent_from_user") or "issue-keeper").strip() or "issue-keeper",
        default_review_agent=(raw.get("default_review_agent") or "").strip(),
        screener=screener,
        repos=repos,
        human_label=human_label,
        keeper_patrol=patrol,
        keeper_timeout_secs=max(60, int(raw.get("keeper_timeout_secs", 3900))),
        pipeline_mode=bool(raw.get("pipeline_mode", False)),
        pipeline_dispatch_owner=str(raw.get("pipeline_dispatch_owner") or "keeper").strip().lower(),
        pipeline_bridge=_expand_path(
            raw.get("pipeline_bridge")
            or "~/projects/infra4agent/issue-keeper/flows/pipeline_bridge.py"),
        pipeline_timeout_secs=max(300, int(raw.get("pipeline_timeout_secs", 5400))),
        run_deadline_margin_secs=max(0, int(raw.get("run_deadline_margin_secs", 300))),
        # 2026-10-07 修复：console_* 三旋钮此前只写进 dataclass 默认值、无人读
        # yaml——与 pipeline_max_in_flight 同款死旋钮。宽限期尤其致命：背压队列
        # 排队 > 默认 1800s 时 404 仍被误判 engine_error → 重派（plaita#18）。
        console_queue_grace_secs=max(0, int(raw.get("console_queue_grace_secs", 1800))),
        console_zombie_secs=max(0, int(raw.get("console_zombie_secs", 7200))),
        console_node_stale_secs=max(0, int(raw.get("console_node_stale_secs", 1800))),
        console_inflight_budget_secs=max(
            0, int(raw.get("console_inflight_budget_secs", 10800))),
        console_retry_max=max(0, int(raw.get("console_retry_max", 1))),
        # 2026-09-30 修复：此前 yaml 旋钮 pipeline_max_in_flight 无人读取，
        # 恒为 dataclass 默认 2（「调并发」实际不生效）。
        pipeline_max_in_flight=max(1, int(raw.get("pipeline_max_in_flight", 2))),
        # 管线档位全局层（2026-10-10）：**必须显式读取**——本 dataclass 的
        # yaml 装配是逐字段白名单式（同款「死旋钮」陷阱见上方 max_in_flight
        # / daily_limit 两处注释），漏读则恒为 dataclass 默认值，切档静默失效。
        pipeline_default_agent=(raw.get("pipeline_default_agent") or "").strip(),
        pipeline_default_reviewer=(raw.get("pipeline_default_reviewer") or "").strip(),
        pipeline_priority_repos=pipeline_priority_repos,
        pipeline_repo_limits=pipeline_repo_limits,
        comment_max_in_flight=max(1, int(raw.get("comment_max_in_flight", 3))),
        # 2026-09-30 修复：与 max_in_flight 同款死旋钮——yaml 无人读取，恒为
        # 默认 2，#67/#70（各 2 run）被误判日上限锁死（runtime yaml 实配 10）。
        pipeline_issue_daily_limit=max(1, int(raw.get("pipeline_issue_daily_limit", 3))),
        pipeline_push_mode=(raw.get("pipeline_push_mode") or "branch").strip(),
        pipeline_review_mode=(raw.get("pipeline_review_mode") or "auto").strip(),
        # #8：0 有意义（= 不自动重试），不能套 max(1, ...)
        failed_auto_retry=max(0, int(raw.get("failed_auto_retry", 1))),
        pipeline_needs_human_label=(
            (raw.get("pipeline_needs_human_label") or "needs-human").strip() or "needs-human"),
        pipeline_test_commands={str(k): str(v) for k, v in pipeline_test_raw.items()},
        pipeline_repos=pipeline_repos,
        author_allowlist=[str(a).strip() for a in allowlist_raw if str(a).strip()],
        opt_out_labels=[str(x).strip()
                        for x in (raw.get("opt_out_labels") or ["keeper-ignore"])
                        if str(x).strip()],
        author_daily_limit_exempt=[str(a).strip() for a in (raw.get("author_daily_limit_exempt") or [])
                                   if str(a).strip()],
        author_daily_limit=max(1, int(raw.get("author_daily_limit", 3))),
        reply_polish=_load_reply_polish(raw, screener),
        pipeline=_load_pipeline(raw),
    )

    if cfg.poll_interval_secs <= 0:
        raise ValueError("poll_interval_secs 必须为正整数")
    return cfg
