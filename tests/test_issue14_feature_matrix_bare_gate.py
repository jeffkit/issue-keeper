"""#14：模板的 recursive 档需补 feature-matrix-bare 门（no-default-features 盲区）。

recursive 的 CI 跑 feature matrix（bare + 7 个单特性），本地门禁只跑
--all-features：#94 的 feature-gating 编译错误（`factory.rs` unused import +
unused variable）本地全绿、合入后 CI 8 变体红 7 才暴露。门清单是数据，改的是
模板 config.example.yaml，用 Config.load（load_config）解析按名核对。
"""

from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"

BARE_COMMAND = "cargo clippy --lib --no-default-features -- -D warnings"
BARE_PATHS = ["src/**", "Cargo.toml", "Cargo.lock"]


def _recursive_repo_cfg(monkeypatch, tmp_path):
    # HOME 指到 tmp：模板的 internal_db 默认 ~/.issue-keeper/internal.db，
    # 不隔离就会读/建真实库。
    monkeypatch.setenv("HOME", str(tmp_path))
    from issue_keeper.config import load_config

    return load_config(EXAMPLE).pipeline_repo_cfg("jeffkit/recursive")


def test_recursive_template_has_feature_matrix_bare_gate(monkeypatch, tmp_path):
    pc = _recursive_repo_cfg(monkeypatch, tmp_path)
    by_name = {g.name: g for g in pc.gates}
    assert "feature-matrix-bare" in by_name, [g.name for g in pc.gates]
    g = by_name["feature-matrix-bare"]
    assert g.command == BARE_COMMAND
    assert g.timeout_secs == 600
    assert g.paths == BARE_PATHS


def test_recursive_existing_gates_unchanged(monkeypatch, tmp_path):
    pc = _recursive_repo_cfg(monkeypatch, tmp_path)
    by_name = {g.name: g for g in pc.gates}
    assert by_name["fmt"].command == "cargo fmt --all --check"
    assert by_name["clippy"].command == (
        "cargo clippy --workspace --all-targets --all-features -- -D warnings")
    assert by_name["test"].command == "cargo test --workspace --no-fail-fast"
    # 整门预算公式不变：各 gate timeout 之和 + 300 缓冲
    assert pc.effective_gate_timeout() == sum(g.timeout_secs for g in pc.gates) + 300
