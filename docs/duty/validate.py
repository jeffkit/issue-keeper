"""duty 协议 Schema 的契约测试。

跑法：`python3 docs/duty/validate.py`（或 `pytest docs/duty/validate.py`）

为什么要有这个文件：
    协议迁到结构化形态后，**schema 就是新的"红线"**——它替换了原来写在
    提示词里的中文句子。所以它必须像代码一样被测试，否则：
      · 样本漂移（真实轮报不再符合）无人发现；
      · schema 被放宽到拦不住坏形态（比如 done 回执不带证据），
        等于把刚建立的约束又退回散文时代。

两层验证：
    1. 正向——examples/ 下的**真实数据样本**必须通过；
    2. 反向——**已知坏形态**必须被拦住（每个用例对应一次真实教训）。
"""
from __future__ import annotations

import json
import pathlib
import sys

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

HERE = pathlib.Path(__file__).resolve().parent
SCHEMA_DIR = HERE / "schema"
EXAMPLES_DIR = HERE / "examples"


def _registry() -> Registry:
    reg = Registry()
    for p in sorted(SCHEMA_DIR.glob("*.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        res = Resource.from_contents(s)
        reg = reg.with_resource(uri=s["$id"], resource=res)
        # 同时用文件名注册，允许 $ref 写 "roster.schema.json#/$defs/Role"
        reg = reg.with_resource(uri=p.name, resource=res)
    return reg


#: (schema 文件, 样本文件, 说明)——正向用例
POSITIVE = [
    ("roster.schema.json", "roster.example.json"),
    ("state.schema.json", "state.example.json"),
    ("directives.schema.json", "directives.example.json"),
    ("handoff.schema.json", "handoff.example.json"),
]


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def _load_example(name: str) -> dict:
    return json.loads((EXAMPLES_DIR / name).read_text(encoding="utf-8"))


def _mutate(example: str, path: list, value=None, *, drop: bool = False) -> dict:
    """复制样本并在指定路径上改值或删键。"""
    doc = _load_example(example)
    node = doc
    for key in path[:-1]:
        node = node[key]
    last = path[-1]
    if drop:
        node.pop(last, None)
    else:
        node[last] = value
    return doc


def _negative_cases() -> list[tuple[str, str, dict]]:
    """反向用例：每条对应一个真实教训或一条协议纪律。"""
    return [
        # —— directives：回执纪律（现役"回执带时间与证据"的机器化）——
        (
            "done 回执缺 evidence（防'口头完成'）",
            "directives.schema.json",
            _mutate("directives.example.json", ["directives", 2, "acks", 0, "evidence"], drop=True),
        ),
        (
            "failed 回执缺 reason",
            "directives.schema.json",
            _mutate("directives.example.json", ["directives", 3, "acks", 0, "reason"], drop=True),
        ),
        (
            "非规范指令号 'D-27'（现役为四位）",
            "directives.schema.json",
            _mutate("directives.example.json", ["directives", 0, "id"], "D-27"),
        ),
        (
            "targets 为空（指令投给谁不明确）",
            "directives.schema.json",
            _mutate("directives.example.json", ["directives", 0, "targets"], []),
        ),
        # —— state：能力枚举（防'提示词里的隐形能力'）——
        (
            "未登记 capability（绕过能力矩阵）",
            "state.schema.json",
            _mutate("state.example.json", ["actions", 0, "capability"], "rm_rf_anything"),
        ),
        (
            "非法 severity",
            "state.schema.json",
            _mutate("state.example.json", ["findings", 0, "severity"], "urgent"),
        ),
        (
            "缺 role",
            "state.schema.json",
            _mutate("state.example.json", ["role"], drop=True),
        ),
        # —— roster：GATE 判定的输入，缺了就无法授权 ——
        (
            "缺 capabilities 表（GATE 无法判定 → 必须拒）",
            "roster.schema.json",
            _mutate("roster.example.json", ["capabilities"], drop=True),
        ),
        (
            "非法档位 'maybe'",
            "roster.schema.json",
            _mutate("roster.example.json", ["capabilities", "by_role", "B", "close_issue"], "maybe"),
        ),
        (
            "未声明字段（防 schema 漂移）",
            "roster.schema.json",
            _mutate("roster.example.json", ["unknown_field"], 1),
        ),
        (
            "path_rule 缺 verdict",
            "roster.schema.json",
            _mutate("roster.example.json", ["capabilities", "path_rules", 0, "verdict"], drop=True),
        ),
        # —— handoff：结构化是重点，关键项缺了就等于退回散文 ——
        (
            "in_flight 缺 ref",
            "handoff.schema.json",
            _mutate("handoff.example.json", ["in_flight", 0, "ref"], drop=True),
        ),
        (
            "in_flight.state 非法",
            "handoff.schema.json",
            _mutate("handoff.example.json", ["in_flight", 0, "state"], "maybe-dead"),
        ),
        # —— 隐形的重变更（2026-10-08 主控自行决定调度器放哪，被 jeffkit 指正）——
        (
            "未登记的 change_infrastructure（隐形权力）",
            "roster.schema.json",
            _mutate(
                "roster.example.json",
                ["capabilities", "by_role", "controller", "move_scheduler"],
                "autonomous",
            ),
        ),
    ]


def run(verbose: bool = True) -> int:
    reg = _registry()
    failures: list[str] = []

    for schema_name, sample_name in POSITIVE:
        validator = Draft202012Validator(_load_schema(schema_name), registry=reg)
        errors = sorted(validator.iter_errors(_load_example(sample_name)), key=lambda e: list(e.path))
        if errors:
            failures.append(f"正向 {sample_name}")
            if verbose:
                print(f"✗ {sample_name} 不符合 {schema_name}（{len(errors)} 处）")
                for e in errors[:5]:
                    loc = "/".join(str(x) for x in e.path) or "(root)"
                    print(f"    [{loc}] {e.message[:200]}")
        elif verbose:
            print(f"✓ {sample_name} 符合 {schema_name}")

    for label, schema_name, doc in _negative_cases():
        validator = Draft202012Validator(_load_schema(schema_name), registry=reg)
        errors = list(validator.iter_errors(doc))
        if errors:
            if verbose:
                print(f"✓ 已拦住：{label}")
        else:
            failures.append(f"反向 {label}")
            if verbose:
                print(f"✗ 未拦住：{label}  ← schema 过宽，约束形同虚设")

    if verbose:
        total = len(POSITIVE) + len(_negative_cases())
        print(f"\n{'全部通过' if not failures else '有失败'}：{total - len(failures)}/{total}")
    return 1 if failures else 0


def test_duty_schemas_contract() -> None:
    """pytest 入口。"""
    assert run(verbose=False) == 0


if __name__ == "__main__":
    sys.exit(run())
