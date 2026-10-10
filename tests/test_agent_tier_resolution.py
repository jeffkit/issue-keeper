"""管线 agent/reviewer 档位解析：per-repo → 全局 → 硬编码兜底（2026-10-10）。

## 为什么要有全局层（实证）

沙箱流（`engine=sbx`）的 agent 端点来自 **`plaita-nodes` 的 provider 翻译**
（`~/.plaita/providers.json` 的 `apiBase`/`apiKey` → `RECURSIVE_API_BASE`），
**不是** keeper 的 `agent_env`。所以 GLM 5 小时配额烧穿时，只切 `agent_env`
**管不到沙箱**：

- 2026-10-10 07:33 实测：keeper `agent_env` 已切 deepseek，但沙箱内 agent
  仍在打 `429`（`agent.step: transient HTTP error … status=429`）秒退
  → `exited 1` → 无限重投（`engine_error 9`、近 10min 重投 43 次）；
- 根因：`_dispatch_pipeline` 把 `"agent": pc.agent or "glm53-flash"` **写死**，
  而 `pc.agent` 是 per-repo 配置（默认空）→ 全仓都吃硬编码的 glm 档。

## 契约

优先级：`pipeline_repos[repo].agent` → `pipeline_default_agent` → `"glm53-flash"`；
reviewer 同理，但缺省**跟随** `pipeline_default_agent`（impl/reviewer 同档是
历史行为）。两处派发（主 + 影子副本）必须一致——影子走同一 provider 翻译。
"""
from __future__ import annotations

import pytest

from issue_keeper.config import Config, PipelineRepoConfig

from tests.test_pipeline_dispatch_guard import _res
from tests.test_failed_escalation import _pipeline_on_cfg


def _cfg_with(global_agent: str = "", global_reviewer: str = "",
              repo_agent: str = "", repo_reviewer: str = ""):
    cfg = _pipeline_on_cfg()
    cfg.pipeline_default_agent = global_agent
    cfg.pipeline_default_reviewer = global_reviewer
    pc = PipelineRepoConfig(agent=repo_agent, reviewer=repo_reviewer)
    return cfg, pc


def _resolve(cfg, pc):
    """复刻 keeper 的档位解析表达式（主 + 影子两处同式）。"""
    agent = pc.agent or cfg.pipeline_default_agent or "glm53-flash"
    reviewer = (pc.reviewer or cfg.pipeline_default_reviewer
                or cfg.pipeline_default_agent or "glm53-flash")
    return agent, reviewer


class TestAgentTierResolution:
    def test_default_is_hardcoded_glm(self):
        """全空 → 硬编码 glm53-flash（存量行为不变）。"""
        cfg, pc = _cfg_with()
        assert _resolve(cfg, pc) == ("glm53-flash", "glm53-flash")

    def test_global_agent_applies_when_repo_unset(self):
        """★ 核心：全局档位必须生效——这是「配额烧穿切沙箱档」的唯一开关。"""
        cfg, pc = _cfg_with(global_agent="deepseek-flash")
        assert _resolve(cfg, pc) == ("deepseek-flash", "deepseek-flash"), (
            "全局档位未生效 ⇒ GLM 烧穿时沙箱 agent 仍打 429（实证 #73 次生事故）"
        )

    def test_repo_overrides_global(self):
        """per-repo 优先于全局（保留按仓定制能力）。"""
        cfg, pc = _cfg_with(global_agent="deepseek-flash", repo_agent="glm53-flash")
        assert _resolve(cfg, pc)[0] == "glm53-flash"

    def test_reviewer_follows_global_agent_by_default(self):
        """reviewer 缺省跟随全局 agent（impl/reviewer 同档的历史行为）。"""
        cfg, pc = _cfg_with(global_agent="deepseek-flash")
        assert _resolve(cfg, pc)[1] == "deepseek-flash"

    def test_explicit_global_reviewer_wins(self):
        """显式全局 reviewer 优先于「跟随 agent」。"""
        cfg, pc = _cfg_with(global_agent="deepseek-flash",
                            global_reviewer="glm53-flash")
        assert _resolve(cfg, pc) == ("deepseek-flash", "glm53-flash")

    def test_config_fields_exist_with_empty_default(self):
        """配置字段存在且默认空（不改变未配置部署的行为）。"""
        c = Config()
        assert getattr(c, "pipeline_default_agent", None) == ""
        assert getattr(c, "pipeline_default_reviewer", None) == ""


def test_all_dispatch_sites_use_global_tier():
    """**全部**派发点都必须接线全局档位——只改一处会留半吊子。

    判据演进（2026-10-11）：旧版数 `config.pipeline_default_agent` 在源码里的
    **字面出现次数 ≥4**，是「两处派发各两条解析」的**脆弱代理指标**。该断言在
    把三处重复逻辑抽成 `_identity_payload()` 后必然失败（引用从 4 次降到 1 次），
    但**行为反而更正确**——数出现次数是在测实现形状，不是在测契约。

    现改为**行为契约**：直接调 helper 验三层优先级，并断言**所有**派发点都走它
    （用「不再残留内联解析」做反向检查）。
    """
    import inspect
    import types

    from issue_keeper import keeper

    src = inspect.getsource(keeper)

    # ① 行为：per-repo → 全局 → 硬编码兜底（含 reviewer 跟随 agent）
    cfg = types.SimpleNamespace(
        pipeline_default_agent="deepseek-flash", pipeline_default_reviewer="")
    pc_empty = types.SimpleNamespace(agent="", reviewer="")
    pc_rp = types.SimpleNamespace(agent="cursor-sonnet46", reviewer="glm53-flash")

    base = keeper._identity_payload(cfg, pc_empty)
    assert base["agent"] == "deepseek-flash" and base["reviewer"] == "deepseek-flash", \
        "无 per-repo 时应落到全局层"
    # 按段键恒下发且未配置时 = agent（见 _identity_payload 的长注释：
    # 缺失会导致恢复轮「agent 刷了、impl_agent 没刷」的不一致）
    for k in ("impl_agent", "gatefix_agent", "fix_agent", "landfix_agent"):
        assert base[k] == "deepseek-flash", f"{k} 未配置时应等于 agent"
    rp = keeper._identity_payload(cfg, pc_rp)
    assert rp["agent"] == "cursor-sonnet46" and rp["reviewer"] == "glm53-flash", \
        "per-repo 应优先"

    cfg_blank = types.SimpleNamespace(
        pipeline_default_agent="", pipeline_default_reviewer="")
    assert keeper._identity_payload(cfg_blank, pc_empty)["agent"] == "glm53-flash", \
        "全局也空时应落到硬编码兜底（不改变未配置部署的行为）"

    # ② 覆盖：所有派发点都经 helper（不得残留内联解析——那正是「漏改一处」的来源）
    assert src.count("**_identity_payload(config, pc)") >= 3, (
        "三处派发（主/影子/legacy）都应经统一 helper；"
        f"实际 {src.count('**_identity_payload(config, pc)')} 处"
    )
    for legacy in ('"agent": pc.agent or config.pipeline_default_agent',
                   '"agent": pc.agent,'):
        assert legacy not in src, f"仍有内联档位解析残留：{legacy}"


def test_yaml_values_are_actually_loaded(tmp_path):
    """★ yaml 旋钮必须**真的被读取**——本 dataclass 是逐字段白名单装配。

    「死旋钮」陷阱在本仓已发生**三次**（见 config.py 内 max_in_flight /
    daily_limit / console_queue_grace_secs 三处注释）：dataclass 加了字段、
    yaml 也写了，但 load_config 的构造式没读 ⇒ 恒为默认值、切档静默失效。
    2026-10-10 我自己又踩了一次（`pipeline_default_agent` 解析恒为 ''），
    所以此处用**真实 load_config** 端到端钉住，而不只测 dataclass 默认值。
    """
    import yaml

    from issue_keeper.config import load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "screener": {"enabled": False, "backend": "classic",
                     "api_key": "x", "base_url": "http://x", "model": "m"},
        "pipeline_default_agent": "deepseek-flash",
        "pipeline_default_reviewer": "glm53-flash",
    }, allow_unicode=True), encoding="utf-8")

    c = load_config(str(cfg_file))
    assert c.pipeline_default_agent == "deepseek-flash", (
        "yaml 写了但没被 load_config 读取 ⇒ 死旋钮（切档静默失效）"
    )
    assert c.pipeline_default_reviewer == "glm53-flash"


def test_yaml_absent_keeps_empty_default(tmp_path):
    """未配置 → 空串（回退硬编码 glm53-flash，存量部署行为不变）。"""
    import yaml

    from issue_keeper.config import load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "screener": {"enabled": False, "backend": "classic",
                     "api_key": "x", "base_url": "http://x", "model": "m"},
    }, allow_unicode=True), encoding="utf-8")

    c = load_config(str(cfg_file))
    assert c.pipeline_default_agent == ""
    assert c.pipeline_default_reviewer == ""


def test_segment_agents_passthrough_and_zero_regression():
    """按段档位键**恒下发**，未配置时 = agent（2026-10-11 实测修正）。

    ## 为什么是「恒下发」而不是「配了才发」

    初版设计是「配置了才进 payload」，但 harness s29 **实测变红**，暴露了
    评审 A 项警告的静默失效的具体形态：

    `_refresh_identity` 只刷新 **params 里出现过** 的键。若某段键首派缺失，
    它就不进 context；而 flow 侧 `impl_agent = INPUT.impl_agent or agent` 一旦
    被引擎记入 `$NODE`，恢复轮就会**不一致刷新**——`agent` 刷成本次派发新值、
    `impl_agent` 仍固化首派旧值 ⇒ flow 读 `impl_agent` ⇒ **拿到陈旧档位**，
    且**只在恢复轮出现、日志看着正常**（显示旧模型名）。

    恒下发即消除该整类不一致（代价：payload 多 4 个键）。
    """
    import types

    from issue_keeper import keeper

    cfg = types.SimpleNamespace(
        pipeline_default_agent="deepseek-flash", pipeline_default_reviewer="")
    seg_keys = ("impl_agent", "gatefix_agent", "fix_agent", "landfix_agent")

    # ① 未配置段位 ⇒ 键仍在，且**等于 agent**（保证恢复轮可刷、不残留旧值）
    plain = keeper._identity_payload(cfg, types.SimpleNamespace(agent="", reviewer=""))
    for k in seg_keys:
        assert plain[k] == plain["agent"], f"{k} 未配置时必须等于 agent，实得 {plain.get(k)!r}"

    # ② 配置了就取配置值；未配置的其他段仍 = agent
    seg = keeper._identity_payload(cfg, types.SimpleNamespace(
        agent="", reviewer="",
        segment_agents={"impl_agent": "cursor-sonnet46"}))
    assert seg["impl_agent"] == "cursor-sonnet46"
    for k in seg_keys[1:]:
        assert seg[k] == seg["agent"], f"{k} 未配置时应跟随 agent"

    # ③ 兼容：pc 无 segment_agents 属性时不得抛（旧配置对象/测试桩）
    out2 = keeper._identity_payload(cfg, types.SimpleNamespace(agent="", reviewer=""))
    assert set(seg_keys).issubset(out2), "无该属性时仍须恒下发全部按段键"
