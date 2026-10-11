"""缺陷 2 回归——值守 flow 的**便携路径解析** + **三态失败判据**（2026-10-11 事故）。

事故形态（notes/sandbox-watch-empty-list-evidence.md §1.3–1.5）：
`sandbox_watch_flow.py` / `inflight_watch_flow.py` 的 CODE 节点把解释器硬编码成
Mac 专属 `/Users/kong/projects/infra4agent/plaita/.venv/bin/python`，而 flow 实际由
plaita-flow-worker **在 VM tcloud_gz 上**执行（PLAITA_CODE_BACKEND=subprocess）——
该路径在 VM 上不存在 ⇒ lister 起不来（exit=127）⇒ 清单恒失败。

旧代码有两处独立缺陷，本文件各钉一条：

| 缺陷 | 旧行为 | 本文件钉死的新行为 |
|---|---|---|
| A 吞错 | `sh()` 只返回 stdout，非零退出/超时/命令不存在一律折叠成 `""` | 读 returncode + stderr，失败原因进证据 |
| B 脆弱判据 | `error = out[:200]`，用**空串真值性**兼作"有无错误"；stdout 恰为空时真故障被判成健康 → 渲染「实例 0 · 浪费 0.00」（**假阴性**，成本闸瞎成"报绿"） | 显式三态 `ok`/`empty`/`failed`，**failed 一律 critical + escalate** |

反证纪律（falsification）：`TestFalsificationOldPredicate` 在本文件里**复刻旧判据**
并断言它在同样的"解释器不存在"输入下会渲染成「实例 0 · 浪费 0.00」——证明这条
测试确实能抓到旧 bug（旧实现下 `TestFailedIsCritical` 会红）。这不是装饰性断言：
它把"回归会漏"这件事本身也钉住了。

判据口径（有意不矫枉过正）：
- `failed` = 非零退出 / 超时 / 解释器或脚本不存在 / stdout 空 / JSON 不可解析；
- `empty` = 跑通（returncode 0）但清单确实是 `[]` —— **健康**，不得报 critical；
- `ok`   = 跑通且列到实例。
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
FLOWS = HERE.parent / "flows"
SANDBOX_JSON = FLOWS / "sandbox-watch.flow.json"
INFLIGHT_JSON = FLOWS / "inflight-watch.flow.json"


def _node(flow_json: pathlib.Path, node_id: str):
    """从**编译产物**里取 code 节点并 exec —— 与 plaita 运行期执行的字节一致。

    有意读 .flow.json 而非 .py：钉住的是真正上线的东西（源码改了对但忘了
    build_flows.py 重编译，这里就会红）。
    """
    ir = json.loads(flow_json.read_text(encoding="utf-8"))
    spec = next(n for n in ir["nodes"] if n.get("id") == node_id)
    ns: dict = {}
    exec(spec["code"], ns)
    return ns["run"]


@pytest.fixture()
def sandbox_facts():
    return _node(SANDBOX_JSON, "facts")


@pytest.fixture()
def sandbox_triage():
    return _node(SANDBOX_JSON, "triage")


@pytest.fixture()
def inflight_facts():
    return _node(INFLIGHT_JSON, "facts")


@pytest.fixture()
def inflight_triage():
    return _node(INFLIGHT_JSON, "triage")


# ---------------------------------------------------------------------------
# 便携路径解析
# ---------------------------------------------------------------------------

class TestPortableResolution:
    """/Users/kong/... 不得再出现在**可执行的**节点代码里。"""

    @pytest.mark.parametrize("flow_json", [SANDBOX_JSON, INFLIGHT_JSON])
    def test_no_unresolved_interpreter_path_literal(self, flow_json):
        """解释器/辅助脚本必须是**解析出来的**，不能是可执行字面量。

        ⚠️ 取证更正（2026-10-11，本用例逼出来的）：VM 上 `/Users/kong/projects` 是
        `→ /home/ubuntu/projects` 的**符号链接**（root 建于 10-06），故 `/Users/kong/...`
        下的**仓库文件**在 VM 上其实可读——`duty_probe.py` 之类的字面量并非每次都炸。
        真正致命的是**解释器** `/Users/kong/projects/infra4agent/plaita/.venv/bin/python`：
        VM 上没有 `plaita/.venv`，该文件确实 MISSING ⇒ exit=127 ⇒ 清单恒失败。

        所以本用例只钉「解释器 + lister/sweeper 不得写成字面量」这一条（缺陷真实形态），
        不做全仓字符串扫描——避免把"靠符号链接侥幸可读"的其它路径一并误判为致命。
        """
        import ast
        ir = json.loads(flow_json.read_text(encoding="utf-8"))
        for spec in ir["nodes"]:
            code = spec.get("code") or ""
            literals = [n.value for n in ast.walk(ast.parse(code))
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            # ① 解释器绝不允许硬编码：这是 VM 上确实不存在的那个路径
            bad_py = [s for s in literals
                      if "/.venv/bin/python" in s or s.endswith("/bin/python3")]
            assert not bad_py, (
                "%s 节点 %s 仍把解释器写成字面量（VM 上 plaita/.venv 不存在 ⇒ 127）：%r"
                % (flow_json.name, spec.get("id"), bad_py[:3]))
            # ② 本 flow 的两个 lister/sweeper 也必须是解析结果（裸文件名是解析器的
            #    内部候选名，不算硬编码；只有**绝对路径**字面量才是缺陷）
            bad_helper = [s for s in literals
                          if s.startswith("/") and
                          s.endswith(("ags-list.py", "ags-orphan-sweep.py"))]
            assert not bad_helper, (
                "%s 节点 %s 仍把辅助脚本写成字面量：%r"
                % (flow_json.name, spec.get("id"), bad_helper[:3]))

    def test_facts_resolves_paths_that_exist(self, sandbox_facts, monkeypatch):
        """无 VM 也能验的部分：解析出的解释器/lister 必须**真实存在**。

        本机没有 e2b（lister 会失败），但"路径解析"与"lister 能否跑"是两件事——
        这里只钉前者：解释器可执行、脚本文件在。
        """
        out = sandbox_facts({})
        assert pathlib.Path(out["interpreter"]).exists()
        assert pathlib.Path(out["lister"]).exists()
        assert out["lister"].endswith("ags-list.py")
        # 解析结果必须落在本仓 flows/ 里，而不是别的宿主路径
        assert pathlib.Path(out["lister"]).parent.resolve() == FLOWS.resolve()

    def test_explicit_param_wins(self, sandbox_facts, tmp_path):
        """显式 venv_py / lister 优先级最高（调度侧可覆盖）。"""
        fake_py = tmp_path / "python"
        fake_py.write_text("#!/bin/sh\nexit 0\n")
        fake_py.chmod(0o755)
        fake_lister = tmp_path / "lister.py"
        fake_lister.write_text("print('[]')\n")
        out = sandbox_facts({"venv_py": str(fake_py), "lister": str(fake_lister)})
        assert out["interpreter"] == str(fake_py)
        assert out["lister"] == str(fake_lister)

    def test_venv_py_takes_precedence(
            self, sandbox_facts, tmp_path, monkeypatch):
        """顺序钉死：显式 > sys.executable > <repo>/.venv > env > which。

        这里放一个"能跑但输出非 JSON"的解释器，证明走的是它（而非 sys.executable）。
        """
        fake_py = tmp_path / "notpython"
        fake_py.write_text("#!/bin/sh\necho 'not json'\n")
        fake_py.chmod(0o755)
        out = sandbox_facts({"venv_py": str(fake_py)})
        assert out["interpreter"] == str(fake_py)
        assert out["state"] == "failed"          # 输出不可解析 → 失败
        assert "JSON 解析失败" in out["detail"]


# ---------------------------------------------------------------------------
# 缺陷 A：stderr 不得丢弃
# ---------------------------------------------------------------------------

class TestStderrNotDiscarded:
    def test_missing_interpreter_is_failed_with_reason(self, sandbox_facts, tmp_path):
        """解释器不存在（等价于 VM 上没有 Mac 路径）→ failed，且带得出原因。"""
        out = sandbox_facts({"venv_py": str(tmp_path / "no-such-python")})
        assert out["state"] == "failed"
        assert out["error"]
        assert "不存在" in out["detail"] or "退出码" in out["detail"]

    def test_nonzero_exit_stderr_is_captured(self, sandbox_facts, tmp_path):
        """非零退出 + stderr → 失败证据里必须**看得见 stderr**（旧代码全丢）。"""
        py = tmp_path / "py"
        py.write_text("#!/bin/sh\necho 'boom: cannot import e2b' >&2\nexit 127\n")
        py.chmod(0o755)
        lister = tmp_path / "lister.py"
        lister.write_text("print('[]')\n")
        out = sandbox_facts({"venv_py": str(py), "lister": str(lister)})
        assert out["state"] == "failed"
        assert "boom: cannot import e2b" in out["detail"]
        assert "127" in out["detail"]

    def test_real_lister_import_failure_is_visible(self, sandbox_facts):
        """真跑 ags-list.py（本机无 e2b）——旧代码会吞成空串，新代码看得见原因。

        条件化：装了 e2b 的机器上 lister 会成功，跳过本断言（避免环境耦合）。
        """
        out = sandbox_facts({})
        if out["state"] == "ok":
            pytest.skip("本机装有 e2b，lister 成功——该形态由 ok 用例覆盖")
        assert out["state"] == "failed"
        assert "stderr" in out["detail"] or "stdout 为空" in out["detail"]
        assert out["error"]


# ---------------------------------------------------------------------------
# 缺陷 B：三态判据（核心反证）
# ---------------------------------------------------------------------------

class TestFailedIsCritical:
    """**反证主角**：lister 起不来 ⇒ 必须 critical，绝不能是"0 实例 0.00 浪费"。"""

    def _fail_facts(self, tmp_path):
        return {"instances": [], "count": 0, "state": "failed",
                "detail": "解释器不存在: FileNotFoundError",
                "interpreter": str(tmp_path / "missing"), "lister": "ags-list.py",
                "error": "解释器不存在: FileNotFoundError"}

    def test_nonzero_exit_yields_critical(self, sandbox_triage, tmp_path):
        out = sandbox_triage(self._fail_facts(tmp_path))
        crit = [f for f in out["findings"] if f["severity"] == "critical"]
        assert crit, "取清单失败必须产生 critical finding"
        assert any(f.get("escalate") for f in crit), "且必须 escalate（否则静默 5 轮）"
        assert out["state"] == "failed"

    def test_report_never_says_zero_waste_when_failed(self, sandbox_triage, tmp_path):
        out = sandbox_triage(self._fail_facts(tmp_path))
        report = out["report"]
        assert "清单失败" in report
        # 假阴性的精确形态：真故障被渲染成健康的 0
        assert "浪费 0.00" not in report
        assert not report.startswith("实例 0")

    def test_failure_summary_names_interpreter_and_script(self, sandbox_triage, tmp_path):
        out = sandbox_triage(self._fail_facts(tmp_path))
        crit = next(f for f in out["findings"] if f["severity"] == "critical")
        assert str(tmp_path / "missing") in crit["summary"]
        assert "ags-list.py" in crit["summary"]
        assert crit["evidence"]["state"] == "failed"

    @pytest.mark.parametrize("state,detail", [
        ("failed", "lister 退出码 127 | stderr: sh: python: not found"),
        ("failed", "超时 60s 未返回"),
        ("failed", "JSON 解析失败（Expecting value）| stdout[:200]=''"),
    ])
    def test_every_failure_flavor_is_critical(self, sandbox_triage, state, detail):
        out = sandbox_triage({"instances": [], "count": 0, "state": state,
                              "detail": detail, "error": detail,
                              "interpreter": "/x/python", "lister": "/x/ags-list.py"})
        assert any(f["severity"] == "critical" and f.get("escalate")
                   for f in out["findings"]), detail
        assert "浪费 0.00" not in out["report"]

    def test_legacy_error_only_payload_still_fails_closed(self, sandbox_triage):
        """兼容：只有 error（无 state）的旧形态 → 按 failed 处理（fail-closed）。"""
        out = sandbox_triage({"instances": [], "count": 0,
                              "error": "lister 起不来", "detail": "lister 起不来"})
        assert out["state"] == "failed"
        assert any(f["severity"] == "critical" for f in out["findings"])


class TestEmptyIsHealthy:
    """**防矫枉过正**：合法空清单（returncode 0, `[]`）= 健康，不得报 critical。"""

    def test_empty_list_is_empty_state_not_failed(self, sandbox_facts, tmp_path):
        py = tmp_path / "py"
        py.write_text("#!/bin/sh\necho '[]'\n")
        py.chmod(0o755)
        out = sandbox_facts({"venv_py": str(py)})
        assert out["state"] == "empty"
        assert out["error"] is None
        assert out["instances"] == []

    def test_empty_state_triage_has_no_findings(self, sandbox_triage):
        out = sandbox_triage({"instances": [], "count": 0, "state": "empty",
                              "detail": "", "error": None})
        assert out["findings"] == []
        assert out["state"] == "empty"
        # 空清单也要如实报数（不能说成"清单失败"）
        assert "清单失败" not in out["report"]
        assert "实例 0" in out["report"]
        assert "清单为空" in out["report"]

    def test_ok_state_with_instances_reports_truthfully(self, sandbox_triage):
        # 用一个「running 执行」的实例（非孤儿、非 paused）——只验报告口径，
        # 不触发既有的"浪费 ≥2 实例小时"critical（那是另一条已测的规则）。
        out = sandbox_triage({
            "instances": [{"id": "sbx1", "short": "sbx1", "age_h": 2.0,
                           "exec": "e1", "state": "running",
                           "exec_status": "running",
                           "exec_update_age_sec": 60, "open_node": "impl",
                           "impl_budget_sec": 7200}],
            "count": 1, "state": "ok", "detail": "", "error": None})
        assert not [f for f in out["findings"] if f["severity"] == "critical"]
        assert "实例 1" in out["report"]
        assert "清单为空" not in out["report"]
        assert "浪费 0.00 实例小时" in out["report"]   # 如实：确实没有浪费


# ---------------------------------------------------------------------------
# 反证：旧判据确实会漏（证明上面的测试不是空转）
# ---------------------------------------------------------------------------

class TestFalsificationOldPredicate:
    """复刻**旧实现**的判据，证明它在同一输入下渲染成健康的「实例 0 · 浪费 0.00」。

    若这组断言失败，说明"旧代码有假阴性"这一前提不成立，上面的回归测试也就失去了
    意义——所以它是本文件的**反证基线**，不是装饰。
    """

    @staticmethod
    def _old_facts(items, out):
        """旧 `facts` 的返回式：`error = None if items else out[:200]`。"""
        return {"instances": items, "count": len(items),
                "error": None if items else out[:200]}

    @staticmethod
    def _old_report(inst, error):
        """旧 `triage` 的报告式（缺陷 B 的精确形态）。"""
        return ("实例 %d（孤儿 0 / 卡死 0）· 浪费 0.00 实例小时" % len(inst)) \
            if inst or not error else "清单失败"

    def test_old_predicate_renders_healthy_on_silent_failure(self):
        # 模拟"解释器不存在且旧 sh() 返回空串"：items=[]（解析失败）、out=""
        old = self._old_facts([], "")
        assert old["error"] == ""            # 空串而非 None
        report = self._old_report(old["instances"], old["error"])
        # ← 旧代码在这里**报绿**：真故障 = "实例 0 · 浪费 0.00"
        assert report == "实例 0（孤儿 0 / 卡死 0）· 浪费 0.00 实例小时"
        assert "清单失败" not in report

    def test_old_predicate_flips_on_stdout_content(self):
        """同一故障，仅因 stdout 碰巧为空/非空，结论就在报绿与报错间翻转。"""
        assert self._old_report([], "") == "实例 0（孤儿 0 / 卡死 0）· 浪费 0.00 实例小时"
        assert self._old_report([], "ls: no such file") == "清单失败"

    def test_old_predicate_satisfied_by_new_failed_input(self, sandbox_triage):
        """新代码在同一输入下必须**红**——这正是旧代码漏掉的那一条。"""
        out = sandbox_triage({"instances": [], "count": 0, "state": "failed",
                              "detail": "解释器不存在", "error": "解释器不存在"})
        assert "浪费 0.00" not in out["report"]
        assert "清单失败" in out["report"]


# ---------------------------------------------------------------------------
# inflight-watch：同款缺陷（沙箱成本线索）
# ---------------------------------------------------------------------------

class TestInflightSandboxLister:
    def test_failed_lister_is_critical(self, inflight_triage):
        out = inflight_triage({
            "rows": [], "instances": [], "sandbox_state": "failed",
            "sandbox_detail": "lister 退出码 127 | stderr: not found",
            "interpreter": "/x/python", "lister": "/x/ags-list.py"})
        crit = [f for f in out["findings"] if f["severity"] == "critical"]
        assert crit and any(f.get("escalate") for f in crit)
        assert "沙箱清单失败" in out["report"]
        assert "/x/python" in crit[0]["summary"]

    def test_empty_lister_is_not_a_finding(self, inflight_triage):
        out = inflight_triage({"rows": [], "instances": [],
                               "sandbox_state": "empty", "sandbox_detail": ""})
        assert out["findings"] == []
        assert "沙箱清单失败" not in out["report"]

    def test_resolution_is_portable(self, inflight_facts, sandbox_facts):
        """inflight 的解析结果同样必须真实存在（不硬编码 Mac 路径）。"""
        # 不实际 ssh（无被测主机时也应能解析路径）——用显式参数绕开 ssh 分支
        py = sys.executable
        out = inflight_facts({"venv_py": py})
        if out.get("interpreter"):
            assert pathlib.Path(out["interpreter"]).exists()


class TestNonArrayStdoutDoesNotCrash:
    """验收补洞（2026-10-11 独立验收发现）：`json.loads` 成功但顶层非数组。

    旧写法在 `except` 里只改 `state`/`detail`，**没有把 `items` 重置为 []**，
    于是下游 `for it in items` 崩：
        AttributeError: 'str' object has no attribute 'get'
    修复前本类三条**全部报错**（崩溃），修复后必须 failed 且不崩。
    """

    def _lister_with(self, tmp_path, stdout):
        py = tmp_path / "py"
        py.write_text("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\n" % stdout)
        py.chmod(0o755)
        return str(py)

    @pytest.mark.parametrize("stdout,label", [
        ('{"a": 1}', "JSON 对象"),
        ('"just a string"', "JSON 字符串"),
        ('123', "JSON 数字"),
        ('null', "JSON null"),
    ])
    def test_non_array_does_not_crash_and_is_failed(
            self, sandbox_facts, sandbox_triage, tmp_path, stdout, label):
        interp = self._lister_with(tmp_path, stdout)
        out = sandbox_facts({"venv_py": interp})       # ← 修复前在这里就崩
        assert out["state"] == "failed", label
        assert out["instances"] == []                  # 关键：必须被重置为空表
        assert "顶层不是数组" in out["detail"], out["detail"]

        # 端到端：下游不再崩，且产生 critical
        tri = sandbox_triage(out)
        assert tri["state"] == "failed"
        assert any(f["severity"] == "critical" and f.get("escalate")
                   for f in tri["findings"]), label
        assert "浪费 0.00" not in tri["report"]

    def test_items_reset_matches_inflight_behaviour(self, sandbox_facts, tmp_path):
        """与 inflight_watch_flow.py 的同款位置对齐：两处都重置为空表。"""
        interp = self._lister_with(tmp_path, '{"not": "an array"}')
        out = sandbox_facts({"venv_py": interp})
        assert out["instances"] == [] and out["state"] == "failed"


class TestPickPyCopiesInSync:
    """`_pick_py` 在 3 处逐字复制（CODE 节点 code 自包含，无法 import 复用）。

    2026-10-11 验收发现三份已漂移（act 那份漏了 AGS_LISTER_PY，当时行为暂等价）。
    本用例把"三处必须同步"钉死：任何一份漂移即红。
    """

    def _copies(self):
        import ast
        out = []
        for path in (SANDBOX_JSON, INFLIGHT_JSON):
            ir = json.loads(path.read_text(encoding="utf-8"))
            for spec in ir["nodes"]:
                for n in ast.walk(ast.parse(spec.get("code") or "")):
                    if isinstance(n, ast.FunctionDef) and n.name == "_pick_py":
                        out.append((path.name, spec.get("id"), ast.unparse(n)))
        return out

    def test_three_copies_exist(self):
        assert len(self._copies()) == 3

    def test_copies_are_verbatim_identical(self):
        copies = self._copies()
        base = copies[0][2]
        drift = [(f, n) for f, n, body in copies if body != base]
        assert not drift, "解释器解析已漂移（改一处必须同步三处）：%r" % (drift,)

    def test_ags_lister_py_present_in_every_copy(self):
        for f, n, body in self._copies():
            assert "AGS_LISTER_PY" in body, "%s/%s 缺少 AGS_LISTER_PY" % (f, n)

    def test_sync_marker_present(self):
        """三处都要带「改一处必须同步三处」的标注，否则将来仍会漂移。

        注意：标注是**注释**，`ast.unparse` 会把它丢掉——故这里查编译产物里的
        原始 code 文本（每条 `def _pick_py` 之后的那段）。
        """
        for path in (SANDBOX_JSON, INFLIGHT_JSON):
            ir = json.loads(path.read_text(encoding="utf-8"))
            for spec in ir["nodes"]:
                code = spec.get("code") or ""
                for chunk in code.split("def _pick_py")[1:]:
                    head = chunk[:1200]          # 只看到函数体开头
                    assert "同步三处" in head, (
                        "%s 节点 %s 缺同步标注" % (path.name, spec.get("id")))


class TestCompiledArtifactsFresh:
    """编译产物必须与源码同源（改 .py 忘了 build → 这里红）。"""

    @pytest.mark.parametrize("name", ["sandbox-watch", "inflight-watch"])
    def test_build_check_byte_identical(self, name):
        env = {"PYTHONPATH": "%s:%s" % (
            pathlib.Path.home() / "projects/infra4agent/plaita",
            pathlib.Path.home() / "projects/infra4agent/plaita-nodes/src")}
        import os
        run_env = dict(os.environ)
        run_env.update(env)
        r = subprocess.run(
            [sys.executable, str(FLOWS / "build_flows.py"), "--check", name],
            capture_output=True, text=True, timeout=180, env=run_env, cwd=str(FLOWS.parent))
        assert r.returncode == 0, (r.stdout + r.stderr)[-800:]
