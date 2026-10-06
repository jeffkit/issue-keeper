"""影子模式（放量迁移首阶）回归测试。

影子 = 本地主执行照常，**同时**旁路派一份到 console 供对账。硬约束：
1. 影子**绝不**写 console-exec.json（否则 reaper 把本地台账误判为 console 在途）；
2. 影子失败**绝不**影响主执行（任何异常都不外溢）；
3. 影子结果**只读**落 shadow-result.json，不进台账、不发评论。
"""
import json

import pytest

from issue_keeper import keeper
from issue_keeper.config import Config, PipelineRepoConfig, RepoBinding
from issue_keeper.sources import Resource


def _res(number: int = 7) -> Resource:
    return Resource(kind="issue", number=number, title="t", body="正文", state="open",
                    labels=[], author="bob", created_at="", updated_at="",
                    status="inbox", actor_type="human")


class _FakeClient:
    def __init__(self, ok=True, eid="exec-1"):
        self.ok = ok
        self.eid = eid
        self.calls = []

    def start_execution(self, flow_id, params):
        self.calls.append((flow_id, params))
        if not self.ok:
            from issue_keeper.console_exec import ConsoleExecError
            raise ConsoleExecError("boom")
        return self.eid

    def get_execution(self, eid):
        return {"status": "completed", "start_time": "s", "end_time": "e", "error": None}


def _cfg(tmp_path, **repo_kw):
    cfg = Config()
    cfg.pipeline_repos = {"x/y": PipelineRepoConfig(**repo_kw)}
    return cfg


def test_shadow_config_defaults_off():
    pc = PipelineRepoConfig()
    assert pc.shadow is False
    assert pc.shadow_flow_id == ""


def test_shadow_does_not_write_console_anchor(tmp_path, monkeypatch):
    """红线：影子不写 console-exec.json（主锚），只写 shadow-exec.json。"""
    client = _FakeClient()
    monkeypatch.setattr("issue_keeper.console_exec.client_from_config", lambda c: client)
    binder = RepoBinding(repo="x/y", profile="p", cwd=str(tmp_path))
    cfg = _cfg(tmp_path, shadow=True)
    keeper._dispatch_shadow_execution(cfg, binder, _res(), "x/y #7",
                                      cfg.pipeline_repos["x/y"], tmp_path)
    assert (tmp_path / keeper.SHADOW_EXEC_RECORD).exists()
    assert not (tmp_path / keeper.CONSOLE_EXEC_RECORD).exists(), \
        "影子写了主锚 → reaper 会误判本地台账为 console 在途"
    rec = json.loads((tmp_path / keeper.SHADOW_EXEC_RECORD).read_text())
    assert rec["engine"] == "shadow"
    assert rec["execution_id"] == "exec-1"


def test_shadow_failure_is_silent(tmp_path, monkeypatch):
    """影子派发失败只记日志，绝不抛（不能影响主执行）。"""
    client = _FakeClient(ok=False)
    monkeypatch.setattr("issue_keeper.console_exec.client_from_config", lambda c: client)
    binder = RepoBinding(repo="x/y", profile="p", cwd=str(tmp_path))
    cfg = _cfg(tmp_path, shadow=True)
    # 不应抛
    keeper._dispatch_shadow_execution(cfg, binder, _res(), "x/y #7",
                                      cfg.pipeline_repos["x/y"], tmp_path)
    assert not (tmp_path / keeper.CONSOLE_EXEC_RECORD).exists()


def test_shadow_console_unavailable_is_silent(tmp_path, monkeypatch):
    from issue_keeper.console_exec import ConsoleExecError

    def _raise(c):
        raise ConsoleExecError("no console")
    monkeypatch.setattr("issue_keeper.console_exec.client_from_config", _raise)
    binder = RepoBinding(repo="x/y", profile="p", cwd=str(tmp_path))
    cfg = _cfg(tmp_path, shadow=True)
    keeper._dispatch_shadow_execution(cfg, binder, _res(), "x/y #7",
                                      cfg.pipeline_repos["x/y"], tmp_path)
    assert not (tmp_path / keeper.SHADOW_EXEC_RECORD).exists()


def test_collect_shadow_result_writes_only_readonly_artifact(tmp_path, monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr("issue_keeper.console_exec.client_from_config", lambda c: client)
    binder = RepoBinding(repo="x/y", profile="p", cwd=str(tmp_path))
    cfg = _cfg(tmp_path, shadow=True)
    (tmp_path / keeper.SHADOW_EXEC_RECORD).write_text(
        json.dumps({"execution_id": "exec-1", "flow_id": "f", "run_id": "r"}), encoding="utf-8")
    keeper._collect_shadow_result(cfg, binder, tmp_path, "x/y #7")
    out = json.loads((tmp_path / keeper.SHADOW_RESULT_RECORD).read_text())
    assert out["shadow_status"] == "completed"
    # 锚已删（避免重复轮询）+ 绝不产主锚
    assert not (tmp_path / keeper.SHADOW_EXEC_RECORD).exists()
    assert not (tmp_path / keeper.CONSOLE_EXEC_RECORD).exists()


def test_collect_shadow_still_running_keeps_anchor(tmp_path, monkeypatch):
    class _RunningClient(_FakeClient):
        def get_execution(self, eid):
            return {"status": "running"}
    monkeypatch.setattr("issue_keeper.console_exec.client_from_config",
                        lambda c: _RunningClient())
    binder = RepoBinding(repo="x/y", profile="p", cwd=str(tmp_path))
    cfg = _cfg(tmp_path, shadow=True)
    (tmp_path / keeper.SHADOW_EXEC_RECORD).write_text(
        json.dumps({"execution_id": "exec-1"}), encoding="utf-8")
    keeper._collect_shadow_result(cfg, binder, tmp_path, "x/y #7")
    assert (tmp_path / keeper.SHADOW_EXEC_RECORD).exists(), "在途应保留锚等下轮"
    assert not (tmp_path / keeper.SHADOW_RESULT_RECORD).exists()
