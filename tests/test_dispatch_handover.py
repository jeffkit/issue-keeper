"""派发权移交（`pipeline_dispatch_owner: flow`）的闸门语义。

背景（2026-10-07 keeper→flow Phase 3）：intake（screener + 首次派发）移交给
`keeper-shadow` flow 后，keeper 必须**跳过管线仓的首次处理段**——否则：
①双筛：两处 screener 会对同一单各发一份通告（unsafe/低置信）；
②双派：keeper 与 flow 同时提交 execution。

本闸只对「有管线契约的仓」生效（`_pipeline_repo_cfg(...)[0] is not None`）：
无契约/readonly 走 legacy 的仓不受影响；评论处理/reaper 也不受影响。
"""
from __future__ import annotations

import logging

import pytest

from issue_keeper.config import Config, PipelineRepoConfig

from tests.test_failed_escalation import _defer_env, _pipeline_on_cfg
from tests.test_pipeline_dispatch_guard import _res


def test_owner默认keeper行为不变(tmp_path, monkeypatch):
    """未配置时 = keeper（默认）——派发路径照常走到。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    dispatched: list = []
    monkeypatch.setattr("issue_keeper.keeper._dispatch_pipeline",
                        lambda *a, **kw: dispatched.append(kw) or {"status": "dispatched"})
    cfg = _pipeline_on_cfg(pipeline_dispatch_owner="keeper")
    handled = _process_resource_cfg(cfg)
    assert handled == 1
    assert len(dispatched) == 1, "owner=keeper 时派发必须照常发生"


def test_owner_flow时跳过首次处理_不派发(tmp_path, monkeypatch, caplog):
    """owner=flow：管线仓不派发、且留下移交日志（可观测）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    dispatched: list = []
    monkeypatch.setattr("issue_keeper.keeper._dispatch_pipeline",
                        lambda *a, **kw: dispatched.append(kw) or {"status": "dispatched"})
    cfg = _pipeline_on_cfg(pipeline_dispatch_owner="flow")
    with caplog.at_level(logging.INFO, logger="issue-keeper"):
        handled = _process_resource_cfg(cfg)
    assert handled == 0, "移交后 keeper 不再处理首次响应"
    assert dispatched == [], "owner=flow 时 keeper 不得派发（防双派）"
    assert any("派发权已移交 flow" in r.getMessage() for r in caplog.records), \
        "移交必须可观测（INFO 日志含移交说明）"


def test_owner_flow不影响无契约仓(tmp_path, monkeypatch):
    """无管线契约的仓（legacy 路径）不受移交影响。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    dispatched: list = []
    monkeypatch.setattr("issue_keeper.keeper._dispatch_pipeline",
                        lambda *a, **kw: dispatched.append(kw) or {"status": "dispatched"})
    cfg = _pipeline_on_cfg(pipeline_repos={}, pipeline_dispatch_owner="flow")
    handled = _process_resource_cfg(cfg)
    assert handled >= 0  # 不因移交而异常；legacy 分支行为与 owner=keeper 一致
    assert dispatched == [], "无契约仓本就不走管线派发"


def _process_resource_cfg(cfg: Config) -> int:
    from issue_keeper.keeper import _process_resource
    return _process_resource(**_defer_env(cfg))
