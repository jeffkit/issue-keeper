"""review 判定解析回归（2026-09-28 起，v1.0.13 迁移到 parse_json 节点）。

issue #43 实证：独立审查员**输出了**要求的那行严格 JSON，但正文里带了花括号
（`"preset resolves to: type={}, model={}"` —— 该 issue 本身就是在谈这行输出格式）。
朴素的 `raw[raw.find('{'):raw.rfind('}')+1]` 从**第一个** `{` 切到**最后一个**
`}`，把正文和 JSON 粘成一段 → json.loads 失败 → fail-safe abort，把一次本该
approve/fix 的 review 误判成叫停，整轮 ~22 分钟白跑。

v1.0.13 起该解析不再内联在 flow code 节点里，沉淀为 plaita-nodes 的
`parse_json` 节点（健壮策略与单测都在那边）。本文件从编译产物里取出
verdict 节点的 **choices/default 配置**，动态构造同一个节点来跑——锁的仍是
console/运行期真正执行的定义。
"""
import json
import pathlib
import sys

FLOW_JSON = (pathlib.Path(__file__).resolve().parent.parent
             / "flows" / "issue-pipeline.flow.json")

sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita-nodes/src")
from plaita_nodes.parse_json import ParseJsonNode  # noqa: E402


def _verdict_node() -> ParseJsonNode:
    flow = json.loads(FLOW_JSON.read_text(encoding="utf-8"))
    node = next(n for n in flow["nodes"] if n.get("id") == "verdict")
    assert node["type"] == "parse_json", "verdict 应由 parse_json 节点承接（v1.0.13）"
    assert node["choices"] == ["approve", "fix", "abort"]
    fields = {k: node[k] for k in ("id", "text", "choices", "default", "join_fields") if k in node}
    return ParseJsonNode(**fields)


_NODE = _verdict_node()
_CURRENT = {"text": ""}


class _Exec:
    def evaluate(self, v):
        return _CURRENT["text"]

    def get_global_variable(self, key, default=None):
        return default


def run_verdict(payload: dict) -> dict:
    _CURRENT["text"] = payload.get("text", "")
    return _NODE.execute(_Exec())


# ── 回归：#43 的形态（正文带花括号 + 末尾一行 JSON）──────────────────

def test_prose_with_braces_then_json_line_is_parsed():
    text = (
        "Evidence gathered: diff = 1 line in `main.rs:1248`; tests pass; **fmt check fails**.\n"
        "\n"
        "- 旧格式串 `\"preset resolves to: type={}, model={}\"` 仍在 providers.toml:43-48，\n"
        "  所以测试里的负向断言是真回归锁。\n"
        "\n"
        '{"verdict":"fix","notes":"cargo fmt --all --check 失败，需修 tests/config_show.rs:34"}'
    )
    out = run_verdict({"text": text})
    assert out["verdict"] == "fix"
    assert "config_show.rs" in out["notes"]


def test_approve_after_brace_heavy_prose():
    text = ('正文里全是 { } 花括号 {a} {b}\n'
            '{"verdict":"approve","notes":"ok"}')
    assert run_verdict({"text": text})["verdict"] == "approve"


# ── 正常形态 ─────────────────────────────────────────────────────────

def test_bare_json_line():
    out = run_verdict({"text": '{"verdict":"approve","notes":"干净"}'})
    assert out["verdict"] == "approve"


def test_json_fenced_in_markdown():
    text = '结论如下：\n```json\n{"verdict":"fix","notes":"补个边界用例"}\n```\n'
    assert run_verdict({"text": text})["verdict"] == "fix"


def test_pretty_printed_json_multiline():
    text = 'Review:\n{\n  "verdict": "approve",\n  "notes": "多行形态"\n}'
    assert run_verdict({"text": text})["verdict"] == "approve"


def test_trailing_prose_after_json():
    text = '{"verdict":"abort","notes":"方向不对"}\n\n以上，请人工确认。'
    assert run_verdict({"text": text})["verdict"] == "abort"


# ── fail-safe 语义必须保留 ───────────────────────────────────────────

def test_no_json_aborts_as_unparseable():
    out = run_verdict({"text": "我看了 diff，没问题。（没有给 JSON）"})
    assert out["verdict"] == "abort"
    assert "无法解析" in out["notes"]
    assert out["parse_ok"] is False


def test_invalid_verdict_value_aborts():
    out = run_verdict({"text": '{"verdict":"looks-good","notes":"x"}'})
    assert out["verdict"] == "abort"
    assert "非法 verdict" in out["notes"]


def test_empty_text_aborts():
    out = run_verdict({"text": ""})
    assert out["verdict"] == "abort"
    assert out["parse_ok"] is False
