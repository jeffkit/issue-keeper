#!/usr/bin/env python3
"""keeper ↔ issue-pipeline 桥（混合形态，2026-09-28）。

stdin 收 input JSON（契约见 flows/README.md），stdout 输出一行：
    RESULT {json}

混合形态：定义与观测归 plaita-console，执行留本仓——start_new_session + 超时
killpg 的孤儿清理语义在 keeper._invoke_pipeline（2026-09-27 孤儿事故的教训），
不外迁。console 侧 cancel 不杀进程树（2026-09-28 真机验证），故不走 console 执行。

定义解析（fail-open 三级降级，payload.console 提供连接信息时启用）：
  1. console 已发布定义（semver 最高），TTL（refresh_secs）内用本地缓存；
  2. console 不可达 → stale 缓存；
  3. 无 console 配置 / 缓存也没有 → 仓内 issue-pipeline.flow.json（旧行为）。

观测上报（payload.observability_redis 非空时；任何失败静默跳过，绝不影响主管线）：
  - plaita:execution:{id} —— console 执行列表/详情页数据源（nodes 节点级 trace）；
  - plaita:execution:events:{id} —— SSE 实时推送频道（每节点 publish 全量状态）；
  - LangfuseCallback —— 进程 env 配了 LANGFUSE_PUBLIC_KEY 且装了 plaita[langfuse]
    才启用，trace id = execution_id（console 深链字段暂不回填，Langfuse UI 直查）。
  执行键 30 天过期（console/worker 自写的键无 TTL，bridge 侧收敛防堆积）。

每次运行追加台账 ~/.issue-keeper/pipeline/runs.jsonl（supervisor 巡检数据源；
含 error 字段——2026-09-28 补 #33 暴露的错误可见性债）。
"""
import json
import shutil
import os
import pathlib
import sys
import time
import urllib.request
import uuid

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita")
sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita-nodes/src")

# plaita 的 code 节点把整段 run() 包在 subprocess 沙箱里，wall-clock 上限取
# import 时刻的 PLAITA_SANDBOX_TIMEOUT（默认 10s，见 plaita/node/code.py）。
# 本 flow 的 deliver / merge 节点要跑 git ls-remote / commit / push（网络 IO），
# 10s 常态不够——2026-09-28 实证：#41 死在 deliver、#45 的孤儿 run 死在 merge，
# 都是「push 其实已经成功、包装层被墙钟杀掉」的假失败。
# flow 源码本意是 sandbox_backend="unsafe"（本机可信部署），但 @flow 编译器没把
# 这个 kwarg 带进 IR，运行期只能吃 register_code_node 的 subprocess 默认值。
# 这里把预算放宽（env 可覆盖）；编译器/IR 的根因另记，不靠这一步掩盖。
SANDBOX_TIMEOUT_DEFAULT_SECS = 2400


def ensure_sandbox_timeout(default_secs: int = SANDBOX_TIMEOUT_DEFAULT_SECS) -> None:
    """没配（或配成空串）PLAITA_SANDBOX_TIMEOUT 时给个够用的默认值。

    必须在本模块 import plaita 之前调用：plaita/node/code.py 在 import 期就把
    该 env 读成模块常量，之后再改不生效；空串会让 int('') 直接抛 ValueError。
    2400（2026-09-29 由 900 上调）：merge 节点在 main 前进时会 rebase 分支并重跑
    质量门（fmt+clippy+全量测试），900s 不够。
    """
    if not os.environ.get("PLAITA_SANDBOX_TIMEOUT", "").strip():
        os.environ["PLAITA_SANDBOX_TIMEOUT"] = str(default_secs)


def ensure_tool_path() -> str:
    """把常用工具目录补进 PATH（launchd 起的 keeper 没有它们）。

    2026-09-28：keeper 由 launchd 启动，PATH 只有 /opt/homebrew/bin:/Users/kong/.local/bin:
    /usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin —— **没有 ~/.cargo/bin**；而 plaita 的
    subprocess 沙箱只继承 env 白名单里的 PATH（plaita/node/code.py），于是 gate 节点跑
    `cargo fmt --all --check && cargo test ...` 直接
    FileNotFoundError: [Errno 2] No such file or directory: 'cargo'（#40 的 run 就这么死在
    最后一步，它的实现其实已经过 review+fix_review）。agent 段没事是因为 agent 的 shell
    会读 profile 自己把 ~/.cargo/bin 加回来。

    返回补进去的目录（冒号分隔），无需补时返回 ""。
    """
    home = pathlib.Path.home()
    wanted = [
        str(home / ".cargo" / "bin"),
        str(home / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    added = [p for p in wanted if p not in parts and pathlib.Path(p).is_dir()]
    if not added:
        return ""
    os.environ["PATH"] = os.pathsep.join(added + parts)
    return os.pathsep.join(added)


ensure_sandbox_timeout()

import plaita_nodes  # noqa: F401,E402
plaita_nodes.register_all()  # 显式注册——dist-info entry-points 可能滞后于本地 src
from plaita.node import register_code_node  # E402

register_code_node(default_backend="subprocess")

from plaita.core.callback import FlowCallback  # E402
from plaita.core.executor import FlowExecution  # E402
from plaita.dsl.ir_validate import build_flow  # E402

LEDGER = pathlib.Path("~/.issue-keeper/pipeline/runs.jsonl").expanduser()
FLOW_FILE = HERE / "issue-pipeline.flow.json"
EXEC_KEY_TTL_SECS = 30 * 86400
# 节点 trace 里 input/output 截断长度（agentrun 的 prompt/输出很大，防 Redis 键膨胀）
TRACE_STR_MAX = 2000


def append_ledger(record: dict) -> None:
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass  # 台账失败不影响主管线


def normalize_result(result: dict) -> dict:
    """key 归一化：早退路径历史返回 posted，成功路径返回 comment_posted——
    在出口统一补齐别名，keeper 兜底判定只看 comment_posted，
    flow 新增早退路径不必各写各的（issue #1「未发出回评」误报根因）。
    值收敛为严格布尔：显式 None（历史中间态/未接线的分支）也兜成 False，
    keeper 侧 `not comment_posted` 判定不再被 None 迷惑（2026-09-28）。"""
    posted = result.get("comment_posted")
    if posted is None:
        posted = result.get("posted")
    result["comment_posted"] = bool(posted)
    return result


# ── 定义解析：console → TTL 缓存 → stale 缓存 → 本地文件 ─────────────────

def _semver_key(version: str):
    parts = []
    for seg in str(version or "").split("."):
        num = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def _fetch_published_definition(console: dict) -> tuple[str, str]:
    """拉 console 上 semver 最高的已发布版本。网络/认证失败抛异常由调用方兜底。"""
    headers = {"X-Admin-API-Key": console.get("api_key") or ""}
    base = (console.get("url") or "").rstrip("/")
    flow_id = console.get("flow_id") or "issue-pipeline"

    def _get(path: str) -> dict:
        req = urllib.request.Request(f"{base}{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)

    flow = _get(f"/api/flows/{flow_id}")
    published = [v for v in flow.get("versions", []) if v.get("status") == "published"]
    if not published:
        raise ValueError(f"flow {flow_id} 没有已发布版本")
    current = max(published, key=lambda v: _semver_key(v.get("version", "")))
    detail = _get(f"/api/flows/{flow_id}/versions/{current['version']}")
    definition = detail.get("definition") or ""
    if not definition:
        raise ValueError(f"flow {flow_id}@{current['version']} 定义为空")
    return str(current["version"]), definition


def resolve_definition(console: dict | None) -> tuple[dict, str, str]:
    """返回 (definition_dict, source, version)。source ∈ console/cache/stale/local。"""
    if console and console.get("url") and console.get("api_key"):
        cache_path = pathlib.Path(
            console.get("cache_path") or "~/.issue-keeper/pipeline/flow-cache.json"
        ).expanduser()
        cached = None
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cached = None

        ttl = max(30, int(console.get("refresh_secs", 300)))
        if cached and (time.time() - cached.get("fetched_at", 0)) < ttl:
            try:
                return json.loads(cached["definition"]), "cache", str(cached.get("version", ""))
            except Exception:
                pass
        try:
            version, definition = _fetch_published_definition(console)
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(
                    {"flow_id": console.get("flow_id"), "version": version,
                     "definition": definition, "fetched_at": time.time()},
                    ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
            return json.loads(definition), "console", version
        except Exception as exc:
            print(f"[bridge] console 定义拉取失败，退 stale/本地: {exc}", file=sys.stderr)
            if cached and cached.get("definition"):
                try:
                    return json.loads(cached["definition"]), "stale", str(cached.get("version", ""))
                except Exception:
                    pass
    return json.loads(FLOW_FILE.read_text(encoding="utf-8")), "local", ""


# ── 观测上报：console 执行记录 + 节点 trace + SSE 频道（fail-open）────────

def _safe(value, depth: int = 0):
    """JSON 化任意节点输入/输出：不可序列化转 str，长串截断，深层的直接摘要。"""
    if depth > 4:
        return "…"
    if isinstance(value, str):
        return value[:TRACE_STR_MAX] + ("…[截断]" if len(value) > TRACE_STR_MAX else "")
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k)[:200]: _safe(v, depth + 1) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [_safe(v, depth + 1) for v in value[:50]]
    return str(value)[:TRACE_STR_MAX]


class ConsoleReporter:
    """把本地执行的 run 镜像进 console 观测面。所有调用点 fail-open。"""

    def __init__(self, redis_url: str, execution_id: str, flow_id: str, flow_version: str):
        import redis as redis_lib
        self._r = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._r.ping()  # 连不上直接抛，由调用方跳过上报
        self.execution_id = execution_id
        self.flow_id = flow_id
        self.flow_version = flow_version
        self.started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self._nodes: list = []
        self._input_slim: dict = {}
        self._node_t0 = time.monotonic()

    def _state(self, status: str, output=None, error=None) -> dict:
        return {
            "execution_id": self.execution_id,
            "flow_id": self.flow_id,
            "flow_version": self.flow_version or None,
            "status": status,
            "tenant_id": "default",
            "start_time": self.started,
            "end_time": None,
            "last_update_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "context": {"$INPUT": self._input_slim},
            "error": error,
            "invoker": "keeper-bridge",
            "nodes": self._nodes,
            "output": output,
            "langfuse_trace_url": None,
        }

    def _flush(self, status: str, output=None, error=None, end: bool = False) -> None:
        state = self._state(status, output=output, error=error)
        if end:
            state["end_time"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        body = json.dumps(state, ensure_ascii=False, default=str)
        key = f"plaita:execution:{self.execution_id}"
        self._r.set(key, body, ex=EXEC_KEY_TTL_SECS)
        # SSE 频道：console /executions/{id}/stream 订阅此频道转发 update 事件
        self._r.publish(f"plaita:execution:events:{self.execution_id}", body)

    def start(self, input_slim: dict) -> None:
        self._input_slim = input_slim
        self._flush("running")

    def node_start(self, node) -> None:
        self._nodes.append({
            "id": node.id,
            "type": getattr(node, "node_type", type(node).__name__),
            "name": getattr(node, "name", "") or node.id,
            "input": _safe(getattr(node, "input", None)),
            "output": None,
            "status": "running",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "duration_ms": None,
        })
        self._node_t0 = time.monotonic()
        self._flush("running")

    def node_end(self, node, result=None, error=None) -> None:
        duration_ms = int((time.monotonic() - self._node_t0) * 1000)
        for entry in reversed(self._nodes):
            if entry["id"] == node.id and entry["status"] == "running":
                entry["output"] = _safe(result)
                entry["duration_ms"] = duration_ms
                if error:
                    entry["status"] = "error"
                    entry["error"] = str(error)[:TRACE_STR_MAX]
                else:
                    entry["status"] = "success"
                break
        self._flush("running")

    def finish(self, status: str, output=None, error=None) -> None:
        self._flush(status, output=output, error=error, end=True)


class ArtifactPersistCallback(FlowCallback):
    """把每个节点的终态写进 artifact_dir/nodes/<id>.json。

    2026-09-29 教训：run 中途失败时，上一轮的审查/计划结论只存在于 Redis trace，
    重跑等于失忆——implement 看不到上一轮 review 指出的问题，只能从头再错一遍。
    落盘之后，提示词就可以引用上一轮结论（续跑而非重来）。review/verdict 另存
    人类可读的 03-review.md / 04-verdict.json。
    """

    def __init__(self, artifact_dir: pathlib.Path):
        self._artifact_dir = artifact_dir
        self._dir = artifact_dir / "nodes"

    def bind_execution(self, execution: FlowExecution) -> None:
        return None

    def on_flow_start(self, flow, **kwargs) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[bridge] 产物目录创建失败: {exc}", file=sys.stderr)

    def on_node_start(self, flow, node, **kwargs) -> None:
        return None

    def on_node_end(self, flow, node, result=None, error=None, exception=None, **kwargs) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            node_id = str(getattr(node, "id", "") or "node")
            err = str(error or exception or "")
            payload = {
                "id": node_id,
                "status": "error" if err else "success",
                "output": result if isinstance(result, (dict, str, int, float, bool, type(None))) else str(result),
                "error": err or None,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            (self._dir / f"{node_id}.json").write_text(
                json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
            # 人类可读别名
            if node_id == "review" and isinstance(result, dict):
                text = result.get("text") or json.dumps(result, ensure_ascii=False)
                (self._artifact_dir / "03-review.md").write_text(text, encoding="utf-8")
            if node_id == "verdict" and isinstance(result, dict):
                (self._artifact_dir / "04-verdict.json").write_text(
                    json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            print(f"[bridge] 产物持久化失败: {exc}", file=sys.stderr)


class BridgeTraceCallback(FlowCallback):
    """节点级 trace 回调：形状对齐 console local_executor 的 _LocalTraceCallback，
    落点从 sqlite 换成 ConsoleReporter（Redis）。"""

    def __init__(self, reporter: ConsoleReporter):
        self._reporter = reporter
        self._execution = None

    def bind_execution(self, execution: FlowExecution) -> None:
        self._execution = execution

    def on_flow_start(self, flow, **kwargs) -> None:
        # $EXECUTION_ID 覆写为 bridge 的执行 id，流程内 {$EXECUTION_ID} 与
        # console 执行页显示一致（镜像 _LocalTraceCallback）。
        if self._execution is not None:
            self._execution.set_state(
                f"{self._execution.express_prefix}EXECUTION_ID", self._reporter.execution_id)

    def on_node_start(self, flow, node, **kwargs) -> None:
        try:
            self._reporter.node_start(node)
        except Exception as exc:
            print(f"[bridge] 观测上报失败（node_start）: {exc}", file=sys.stderr)

    def on_node_end(self, flow, node, result=None, error=None, exception=None, **kwargs) -> None:
        try:
            self._reporter.node_end(node, result=result, error=error or exception)
        except Exception as exc:
            print(f"[bridge] 观测上报失败（node_end）: {exc}", file=sys.stderr)


def _build_langfuse_callback():
    """Langfuse 观测（可选）：配了 LANGFUSE_PUBLIC_KEY 才启用，失败只告警一次。"""
    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        return None
    try:
        from plaita.obs import LangfuseCallback
        return LangfuseCallback()
    except Exception as exc:  # noqa: BLE001 —— 观测缺依赖/缺凭据不阻塞主管线
        print(f"[bridge] Langfuse 观测未启用: {exc}", file=sys.stderr)
        return None


def _slim_input(payload: dict) -> dict:
    """上报用的输入快照：剥离连接字段（console.api_key 等），其余原样。"""
    return {k: v for k, v in payload.items() if k not in ("console", "observability_redis")}


# 注：2026-09-29 曾在此安装 pre-push 守卫拦「工作树直推 main」，当天已移除——
# 那次误判了另一会话的正常落地，钩子也会静默挡住合法流程；main 是否绿改由 CI 说了算。
#
# ⚠️ 不要再让管线共用主 clone 的 CARGO_TARGET_DIR（v1.0.8 试过，已回滚）
# 2026-09-28 曾把 CARGO_TARGET_DIR 指到 <main_clone>/target 想让 worktree 复用热依赖，
# 2026-09-29 实测发现它会**链接到另一个 checkout 的库**：同一工作区在两个目录下共用
# target 时，集成测试目标（tests/*.rs）拿到的是 main clone 的 lib rlib，于是
# `cargo test --workspace` 在 worktree 里报 4 个假的 E0599
# （no method named `with_wall_timeout_secs` found for struct `AgentTool`——那个方法
# 明明就在 worktree 的 src/tools/agent.rs 里）。换私有 target 立刻编过（2m22s）。
# 假失败只是表象，真正危险的是对称情形：**可能拿旧库判绿**，让门测到错的代码。
# 正确做法：每个 worktree 用自己的 target/（跨 run 复用会自然变热），
# 并用提示词限制 agent 的自检范围，而不是共用 target。


def main() -> None:
    t0 = time.time()
    # 后台派发（keeper 2026-09-29 解耦）把 payload 写进 dispatch.json 从 argv 传入；
    # 兼容旧的 stdin 方式（手工调试用）。
    payload = (json.load(open(sys.argv[1], encoding="utf-8")) if len(sys.argv) > 1
               else json.load(sys.stdin))
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    if shutil.which("sccache"):
        os.environ.setdefault("RUSTC_WRAPPER", "sccache")
        print("[bridge] RUSTC_WRAPPER=sccache（跨 worktree 共享编译缓存）", file=sys.stderr)

    added_path = ensure_tool_path()
    if added_path:
        print(f"[bridge] PATH 补齐: {added_path}（launchd 的 keeper 没有 ~/.cargo/bin）",
              file=sys.stderr)
    artifact_dir_flag = pathlib.Path(payload["artifact_dir"]) if payload.get("artifact_dir") else None

    console = payload.get("console") or {}
    definition, flow_source, flow_version = resolve_definition(console if console.get("url") else None)
    if flow_source != "local":
        print(f"[bridge] flow 定义: {console.get('flow_id')}@{flow_version} (source={flow_source})",
              file=sys.stderr)

    execution_id = uuid.uuid4().hex
    reporter = None
    if payload.get("observability_redis"):
        try:
            reporter = ConsoleReporter(
                payload["observability_redis"], execution_id,
                console.get("flow_id") or "issue-pipeline", flow_version)
            reporter.start(_slim_input(payload))
        except Exception as exc:
            print(f"[bridge] console 观测初始化失败（跳过上报）: {exc}", file=sys.stderr)
            reporter = None

    trace_cb = BridgeTraceCallback(reporter) if reporter is not None else None
    langfuse_cb = _build_langfuse_callback()
    artifact_cb = ArtifactPersistCallback(artifact_dir_flag)
    handlers = [cb for cb in (langfuse_cb, trace_cb, artifact_cb) if cb is not None]

    try:
        flow = build_flow(definition)
        if handlers:
            execution = FlowExecution(callback_handlers=handlers)
            if trace_cb is not None:
                trace_cb.bind_execution(execution)
            result = execution.run_compatible(flow, False, **payload)
        else:
            result = flow.run(**payload)
        result = result if isinstance(result, dict) else {"status": str(result)}
        ok = True
    except Exception as e:  # 引擎层异常（含超时）：结构化为失败结果，让 keeper 兜底回评
        result = {"status": "engine_error", "error": str(e)[:500]}
        ok = False

    result = normalize_result(result)

    if reporter is not None:
        try:
            reporter.finish(
                "completed" if ok else "failed",
                output=result if ok else None,
                error=None if ok else {"message": result.get("error", ""), "type": "engine_error"},
            )
        except Exception as exc:
            print(f"[bridge] 观测上报失败（finish）: {exc}", file=sys.stderr)
    if langfuse_cb is not None:
        try:
            langfuse_cb.finalize()
        except Exception:
            pass

    append_ledger({
        "ts": started,
        "repo": payload.get("repo_full"),
        "issue": payload.get("issue_number"),
        "author": payload.get("author"),
        "status": result.get("status"),
        "error": result.get("error"),
        "comment_posted": result.get("comment_posted"),
        "pushed": result.get("pushed"),
        "ok": ok,
        "duration_secs": round(time.time() - t0, 1),
        "flow_source": flow_source,
        "flow_version": flow_version,
        "execution_id": execution_id,
    })
    print("RESULT " + json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
