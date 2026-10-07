"""plaita-console executions API 客户端 + verdict 映射（G5/G6，engine=v2-console）。

台账写入迁移（D5）：engine=v2-console 下没有 bridge 进程落台账——reaper 从
execution 终态 + 节点输出自行落账，映射函数在本模块（与 flows/v2_bridge.py
的 verdict→status 表同形状，keeper 既有收尾/重试/升级语义零改动复用）。

两层重试决策表（设计稿 §G5）：
| 错误类型           | 判定源                    | 处置                                  |
| 引擎/节点崩溃      | execution status=error    | resume-retry ×1（续原 execution）     |
| retry 后仍 error   | 台账连续 engine_error     | 既有升级人工语义（封顶 2）            |
| 记录未落（排队中） | GET 404 且记录年龄 < 宽限 | 视为 queued，不动作不重派（plaita#18）|
| worker zombie      | last_update_time 年龄超阈 | cancel + 重派（engine_error 台账行）  |
| 环境性             | verdict=retry-later       | 既有 retry-later 不消费语义           |
| 内容性             | verdict=failed-preserved  | 既有 failed 语义                      |

出害口节点（git_publish/github_comment）自身幂等（dedup marker / ls-remote
查重），resume-retry 重放安全；G1b 的「出害口排除」依赖失败节点名上报，当前
execution error 载荷拿不到——按全部可 retry 处理，节点级幂等兜底（记为已知
边界，failure log 出现出害口节点名时值守应人工复核是否重复副作用）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

# v2 flow 末端 verdict 节点 id 约定（console 发布的 self-improve v2 flow 须
# 把 verdict dict 赋进 $NODE.<verdict 节点>；发布契约见 flows/README.md）。
VERDICT_NODE_IDS = ("verdict", "final_verdict")


class ConsoleExecError(RuntimeError):
    """console API 请求失败（HTTP 非 2xx / 响应不可解析）。"""


class ConsoleExecUnavailable(ConsoleExecError):
    """console 不可达/超时——reaper fail-open 跳过本轮，不判死。"""


class ConsoleExecNotFound(ConsoleExecError):
    """execution 不存在（被清理/TTL 过期）——按终态处理。"""


class ConsoleExecClient:
    """executions API 最小客户端（urllib，无新依赖）。"""

    def __init__(self, url: str, api_key: str, timeout: float = 30.0):
        self.base = (url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{self.base}{path}",
            method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "Content-Type": "application/json",
                "X-Admin-API-Key": self.api_key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise ConsoleExecNotFound(f"{path}: 404") from e
            raise ConsoleExecError(f"{method} {path}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ConsoleExecUnavailable(f"{method} {path}: {e}") from e
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError as e:
            raise ConsoleExecError(f"{path}: 响应非 JSON") from e

    # ---- executions API ----

    def start_execution(self, flow_id: str, params: dict,
                        version: str | None = None) -> str:
        """POST /api/executions → execution_id（BFF 提交时铸造，即刻可轮询）。"""
        body: dict = {"flow_id": flow_id, "params": params}
        if version:
            body["version"] = version
        out = self._request("POST", "/api/executions", body)
        eid = str(out.get("execution_id") or "")
        if not eid:
            raise ConsoleExecError(f"start 未返回 execution_id: {out}")
        return eid

    def get_execution(self, execution_id: str) -> dict:
        """GET /api/executions/{id} → 状态载荷（status/context/error/…）。"""
        return self._request("GET", f"/api/executions/{execution_id}")

    def resume(self, execution_id: str, resume_type: str,
               data: dict | None = None) -> dict:
        return self._request("POST", f"/api/executions/{execution_id}/resume",
                             {"resume_type": resume_type, "data": data})

    def cancel(self, execution_id: str) -> dict:
        return self._request("POST", f"/api/executions/{execution_id}/cancel", {})


def client_from_config(config) -> ConsoleExecClient:
    pc = config.pipeline.console
    if not (pc.url and pc.api_key):
        raise ConsoleExecError("pipeline.console 未配置 url/api_key")
    return ConsoleExecClient(pc.url, pc.api_key)


def engine_client_from_config(config) -> ConsoleExecClient:
    """engine=v2-console **真执行**派发/收尾的 client（2026-10-06 修正）。

    与 ``client_from_config``（旧链路 screener/bridge 用 ``pipeline.console``）
    区分：v2-console 的语义是「走新系统执行」，目标应是**新系统 console**
    （``pipeline.shadow_console``，多机 worker 集群所在）。此前两者共用
    ``pipeline.console``，导致金丝雀首次试切把单派进了本机旧 console
    （8123，本地单机档），而非新系统（远端 8323）——实测缺口。

    ``shadow_console`` 未配（url/api_key 空）时回退 ``pipeline.console``：
    单机演练/无新系统场景行为不变（零回归）。
    """
    sc = getattr(config.pipeline, "shadow_console", None)
    if sc is not None and sc.url and sc.api_key:
        return ConsoleExecClient(sc.url, sc.api_key)
    return client_from_config(config)


# ---- verdict 提取与映射（D5：台账写入责任迁到 keeper） ----

def verdict_from_execution(detail: dict) -> dict:
    """execution 终态载荷 → v2 verdict dict（与 v2_bridge._read_verdict 同形状）。

    completed 从 context.$NODE 提取 verdict 节点输出；error/cancelled/suspended
    统一 engine_error（why 带死因）。
    """
    status = str(detail.get("status") or "")
    if status == "completed":
        nodes = ((detail.get("context") or {}).get("$NODE")) or {}
        for nid in VERDICT_NODE_IDS:
            v = nodes.get(nid)
            if isinstance(v, dict) and v.get("verdict"):
                return v
        for v in reversed(list(nodes.values())):  # 兜底：末个含 verdict 的节点
            if isinstance(v, dict) and v.get("verdict"):
                return v
        return {"verdict": "engine_error",
                "why": "execution completed 但无 verdict 节点输出（flow 契约漂移?）"}
    if status == "error":
        err = detail.get("error")
        msg = err.get("message") if isinstance(err, dict) else str(err or "")
        return {"verdict": "engine_error", "why": str(msg or "execution error")[:500]}
    if status == "cancelled":
        return {"verdict": "engine_error", "why": "execution cancelled（zombie 处置/人工）"}
    if status == "suspended":
        return {"verdict": "engine_error",
                "why": "execution suspended（v2 flow 无挂起节点，需人工接手）"}
    return {"verdict": "engine_error", "why": f"unexpected execution status: {status!r}"}


def map_verdict(verdict: dict) -> dict:
    """verdict → 台账行字段（flows/v2_bridge.py::_finish 同表；comment_posted
    恒 False——console 模式回评由 keeper 收尾层负责，见 keeper._console_closing）。"""
    v = str(verdict.get("verdict") or "")
    if v == "committed":
        return {"status": "done", "pushed": True, "merged": True,
                "comment_posted": False, "note": str(verdict.get("via") or "")}
    if v == "skip-commit":
        return {"status": "done", "pushed": False, "merged": False,
                "comment_posted": False,
                "note": str(verdict.get("why") or "no changes")}
    if v == "retry-later":
        return {"status": "retry-later", "pushed": False, "merged": False,
                "comment_posted": False, "stage": verdict.get("stage"),
                "error": str(verdict.get("why") or "retry-later")[-500:]}
    if v == "failed-preserved":
        return {"status": "failed", "pushed": False, "merged": False,
                "comment_posted": False, "stage": verdict.get("stage"),
                "error": str(verdict.get("why") or verdict.get("gate") or "failed")[-500:]}
    return {"status": "engine_error", "pushed": False, "merged": False,
            "comment_posted": False,
            "error": str(verdict.get("why") or "engine_error")[-500:]}


def record_queued(crec: dict, grace_secs: float,
                  inflight_since: float | None = None,
                  now: float | None = None) -> bool:
    """在途记录仍在「已派发未消费」窗口内（plaita#18）？

    时序事实：console POST 只把消息写进 Redis（execution_id 铸造但不落记录），
    执行记录由 worker 消费时才首次落盘——两次之间 `GET /api/executions/<id>`
    恒为 404。背压排队下这个窗口可远超一个 keeper 巡检周期，把 404 判成
    engine_error 会重派同一 issue（重复执行）。

    年龄取记录里的 `dispatched_at`（派发时刻），缺失则退 `inflight_since`
    （reaper 的在途基线，与派发同一时钟读数）。
    """
    ts = str(crec.get("dispatched_at") or "")
    base: float | None = None
    if ts:
        try:
            from datetime import datetime
            base = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            base = None
    if base is None:
        base = inflight_since
    if base is None:
        return False
    return ((now if now is not None else time.time()) - base) < grace_secs


def zombie(detail: dict, threshold_secs: float, now: float | None = None) -> bool:
    """running 执行的 last_update_time 年龄超阈 → zombie（D6）。

    last_update_time 只在步界持久化时刷新——长 impl 节点（≤70min）期间正常
    老化，阈值必须高于最长节点预算（默认 7200s = 2h）。
    """
    if str(detail.get("status") or "") != "running":
        return False
    ts = str(detail.get("last_update_time") or detail.get("start_time") or "")
    if not ts:
        return False
    try:
        from datetime import datetime
        age = (now if now is not None else time.time()) - \
            datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return age > threshold_secs
