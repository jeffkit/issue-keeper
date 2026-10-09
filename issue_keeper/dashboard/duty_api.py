"""值守总览 API（/api/duty/*）——业务级协同的可视化数据源。

三个只读端点（公开可访问，无副作用）：
- GET /api/duty/overview   三班心跳 + roster 当班 + 调度健康 + 队列/在途 + GLM 计数
- GET /api/duty/stats      沙箱 24h 用量（实例小时按状态）+ 管线吞吐 + DLQ
- GET /api/duty/topology   工作流拓扑（节点/边 + live 状态），前端画图用

数据源（多源、全防御、带缓存——任何一路失败降级为 null 字段而非 500）：
- 本机 duty 内核 ~/.issue-keeper/duty/（A/C 心跳、roster、rounds 计数）
- plaita console 127.0.0.1:8323（经隧道；执行记录/沙箱 run）
- AGS 云沙箱 list（经 plaita/.venv 子进程——e2b SDK 只装在那）
- 远端 VM 经 ssh（keeper state、schedule service、队列/DLQ、shadow 对账）
- controller/pipeline_stats.py（gh 提报/关闭吞吐，自带缓存）
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/api")

DUTY_DIR = Path("~/.issue-keeper/duty").expanduser()
CONTROLLER_DIR = Path("~/.issue-keeper/pipeline/controller").expanduser()
PLAITA_VENV_PY = "/Users/kong/projects/infra4agent/plaita/.venv/bin/python"
CONSOLE = "http://127.0.0.1:8323/api"
CONSOLE_KEY = "b4b5042ee7d1b937633c08f3f50d4c8efbca88d33ece8a03"
E2B_ENV = {
    "E2B_DOMAIN": "ap-guangzhou.tencentags.com",
    "E2B_API_KEY": "e2b_725235357335be8d27367c596c9e3199cf3c5eeb",
}

_CACHE: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn):
    """简单 TTL 缓存——远端/慢源专用；失败返回上次成功值。"""
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
        _CACHE[key] = (now, val)
        return val
    except Exception as exc:  # 降级：旧值或 None
        if hit:
            return hit[1]
        return {"error": str(exc)[:160]}


def _sh(cmd: str, timeout: int = 25) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _console(path: str) -> Any:
    import urllib.request
    req = urllib.request.Request(
        f"{CONSOLE}{path}", headers={"X-Admin-API-Key": CONSOLE_KEY})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


# ---------- overview ----------

def _read_duty_state(role: str) -> dict:
    try:
        doc = json.loads((DUTY_DIR / f"state-{role}.json").read_text())
        rounds = doc.get("rounds") or []
        if not rounds:
            return {}
        from datetime import datetime
        last = rounds[-1]
        age = int(time.time() - datetime.fromisoformat(last["finished_at"]).timestamp())
        return {"round": last.get("round"), "status": last.get("status"),
                "finished_at": last.get("finished_at"), "age_sec": age,
                "narrative": (last.get("narrative") or "")[:160]}
    except Exception:
        return {}


def _roster() -> dict:
    try:
        r = json.loads((DUTY_DIR / "roster.json").read_text())
        return {"generation": r.get("generation"),
                "on_duty": {k: {"session_ref": v.get("session_ref"),
                                "since": v.get("since"),
                                "model_tier": v.get("model_tier")}
                            for k, v in (r.get("on_duty") or {}).items()}}
    except Exception:
        return {}


def _b_line() -> str:
    return _sh(f"grep ' B-flow' {CONTROLLER_DIR}/rounds.log 2>/dev/null | tail -1")


def _b_summary() -> dict:
    """B 班轮报行 → 结构化摘要（前端与拓扑共用，避免原始日志串外泄）。"""
    line = _b_line()
    import re
    t = (re.search(r"(\d{2}:\d{2})\s+B-flow", line) or [None, ""])[1]
    kind = (re.search(r"轮次=keeper-watch（([^）]+)）", line) or [None, ""])[1]
    if "简报发 #3" in line:
        brief = "简报已发 #3"
    elif not line:
        brief = "无轮报"
    else:
        brief = line.split("落地=")[-1][:40] if "落地=" in line else line[:40]
    return {"time": t or "—", "kind": kind, "brief": brief,
            "text": f"{t} {kind}轮 · {brief}".strip() if t else "无 B 班轮报",
            "raw": line[:200]}


def _vm_facts() -> dict:
    """远端控制面事实（ssh，聚合一次）。"""
    out = _sh(
        "ssh -o ConnectTimeout=8 tcloud_gz '"
        "echo SCHED=$(systemctl is-active plaita-schedule-service 2>/dev/null);"
        "echo KEEPER=$(systemctl is-active issue-keeper-worker 2>/dev/null);"
        "echo DISK=$(df -h / | tail -1 | awk \"{print \\$4}\");"
        "echo DLQ=$(docker exec langfuse-v4-redis-1 redis-cli -a 6ace3bde72955c70b9f264e24f57b343 "
        "-n 1 --no-auth-warning XLEN plaita:flow:queue:ctrl:dlq 2>/dev/null);"
        "'",
        timeout=30,
    )
    facts: dict[str, Any] = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.strip().partition("=")
            facts[k.strip().lower()] = v.strip()
    return facts or {"error": "ssh 不可达"}


def _inflight() -> list[dict]:
    out = _sh(
        "ssh -o ConnectTimeout=8 tcloud_gz "
        "'python3 ~/.issue-keeper/inflight_query.py 2>/dev/null'",
        timeout=30,
    )
    try:
        return json.loads(out)
    except Exception:
        return []


def _shadow() -> dict:
    out = _sh("ssh -o ConnectTimeout=8 tcloud_gz 'cat ~/.issue-keeper/shadow/latest.json 2>/dev/null'",
              timeout=20)
    try:
        d = json.loads(out)
        disp = d.get("dispatch") or {}
        return {"generated_at": d.get("generated_at"),
                "budget_left": disp.get("budget_left"),
                "would_dispatch": d.get("would_dispatch"),
                "skip": d.get("skip_by_reason") or {}}
    except Exception as e:
        return {"error": str(e)[:120]}


def overview() -> dict:
    roles = {}
    for role in ("issue-accept", "controller"):
        roles[role] = _read_duty_state(role)
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "roster": _roster(),
        "a_shift": roles["issue-accept"],
        "controller": roles["controller"],
        "b_shift": _cached("b_round", 120, _b_summary),
        "vm": _cached("vm_facts", 60, _vm_facts),
        "inflight": _cached("inflight", 60, _inflight),
        "shadow": _cached("shadow", 120, _shadow),
    }


# ---------- stats ----------

def _sandbox_inventory() -> dict:
    """经 plaita venv 子进程调 e2b SDK 列活实例。"""
    code = (
        "from e2b import Sandbox\n"
        "from e2b.sandbox.sandbox_api import SandboxQuery\n"
        "import datetime, json\n"
        "pag = Sandbox.list(SandboxQuery(metadata={}))\n"
        "items = pag.next_items() if hasattr(pag, 'next_items') else list(pag)\n"
        "now = datetime.datetime.now(datetime.timezone.utc)\n"
        "rows = []\n"
        "for it in (items or []):\n"
        "    m = getattr(it, 'metadata', {}) or {}\n"
        "    st = getattr(it, 'started_at', None)\n"
        "    age = None\n"
        "    try:\n"
        "        st = st if isinstance(st, datetime.datetime) else datetime.datetime.fromisoformat(str(st).replace('Z','+00:00'))\n"
        "        if st.tzinfo is None: st = st.replace(tzinfo=datetime.timezone.utc)\n"
        "        age = round((now - st).total_seconds()/3600, 2)\n"
        "    except Exception: pass\n"
        "    rows.append({'id': str(getattr(it, 'sandbox_id', ''))[:14], 'age_h': age,\n"
        "                 'exec': str((m.get('plaita_execution') or '?'))[:10]})\n"
        "print(json.dumps(rows))\n"
    )
    out = _sh(f"E2B_DOMAIN={E2B_ENV['E2B_DOMAIN']} E2B_API_KEY={E2B_ENV['E2B_API_KEY']} "
              f"{PLAITA_VENV_PY} -c \"{code}\"", timeout=45)
    try:
        return {"instances": json.loads(out)}
    except Exception:
        return {"instances": [], "error": (out or "e2b 查询失败")[:120]}


def _sandbox_stats() -> dict:
    import datetime
    KEYT = CONSOLE_KEY
    out = _sh(
        f"curl -s 'http://127.0.0.1:8323/api/executions?flow_id=self-improve-v2-sbx&limit=30' "
        f"-H 'X-Admin-API-Key: {KEYT}'", timeout=15)
    now = datetime.datetime.now()
    runs = []
    try:
        for e in (json.loads(out).get("executions") or []):
            st = e.get("start_time", "")[:19]
            en = (e.get("end_time") or "")[:19]
            s = datetime.datetime.fromisoformat(st)
            en_dt = datetime.datetime.fromisoformat(en) if en else None
            hrs = ((en_dt or now) - s).total_seconds() / 3600
            runs.append({"status": e.get("status"), "hrs": round(hrs, 2),
                         "start": st[:16]})
    except Exception:
        pass
    total = sum(r["hrs"] for r in runs)
    by: dict[str, float] = {}
    for r in runs:
        by[r["status"]] = round(by.get(r["status"], 0) + r["hrs"], 2)
    return {"window_hours": 24, "runs": len(runs), "instance_hours": round(total, 2),
            "by_status": by, "runs_detail": runs[:12]}


def _throughput() -> dict:
    out = _sh(
        "python3 ~/.issue-keeper/pipeline/controller/pipeline_stats.py --days 7 --json 2>/dev/null",
        timeout=60)
    try:
        return {"days": 7, "data": json.loads(out)}
    except Exception:
        return {"days": 7, "data": None, "error": (out or "pipeline_stats 失败")[:120]}


def stats() -> dict:
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "sandbox": _cached("sbx_stats", 300, _sandbox_stats),
        "sandbox_live": _cached("sbx_live", 120, _sandbox_inventory),
        "throughput": _cached("throughput", 600, _throughput),
    }


# ---------- 等人工队列 ----------

HITL_LEDGER = DUTY_DIR / "hitl-notifications.jsonl"
HITL_BASE = "http://127.0.0.1:8081"


def _needs_human() -> dict:
    """跨仓扫 needs-human 标签（keeper 升级人工的单在这里浮出来）。"""
    import datetime
    out = _sh("gh search issues --owner jeffkit --label needs-human --state open "
              "--json repository,number,title,updatedAt,url --limit 30", timeout=60)
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = []
    try:
        for it in json.loads(out):
            up = str(it.get("updatedAt") or "")
            age_h = None
            try:
                t = datetime.datetime.fromisoformat(up.replace("Z", "+00:00"))
                age_h = round((now - t).total_seconds() / 3600, 1)
            except Exception:
                pass
            rows.append({"repo": (it.get("repository") or {}).get("nameWithOwner", "?"),
                         "number": it.get("number"), "title": (it.get("title") or "")[:90],
                         "url": it.get("url"), "updated_at": up[:16].replace("T", " "),
                         "age_h": age_h})
    except Exception as e:
        return {"items": [], "error": str(e)[:120]}
    rows.sort(key=lambda r: -(r.get("age_h") or 0))
    return {"items": rows, "count": len(rows)}


def _hitl_recent() -> dict:
    """本机 HITL 推送留痕（hitl_notify.py 写的 jsonl）。"""
    rows = []
    try:
        lines = HITL_LEDGER.read_text(encoding="utf-8").splitlines()[-20:]
        for line in lines:
            try:
                r = json.loads(line)
            except Exception:
                continue
            rows.append({"ts": time.strftime("%m-%d %H:%M", time.localtime(float(r.get("ts") or 0))),
                         "title": (r.get("title") or "")[:70],
                         "status": r.get("status") or ("sent" if r.get("sent") else "failed"),
                         "session_id": (r.get("session_id") or "")[:12],
                         "feedback_url": r.get("feedback_url") or "",
                         "waited": r.get("wait_secs") or 0,
                         "replies": (r.get("replies") or [])[:1]})
    except Exception:
        pass
    rows.reverse()
    return {"items": rows[:12], "count": len(rows)}


def _hil_pending() -> dict:
    """hitl-server 里仍在等回复的会话（仅 wait 模式建会话）。"""
    import datetime
    try:
        import urllib.request
        with urllib.request.urlopen(f"{HITL_BASE}/admin/api/hil/sessions", timeout=6) as resp:
            d = json.load(resp)
    except Exception as e:
        return {"items": [], "error": str(e)[:80]}
    now = datetime.datetime.now()
    items = []
    for s in d.get("sessions") or []:
        if str(s.get("status")) in ("pending", "waiting", "open"):
            try:
                exp = datetime.datetime.fromisoformat(str(s.get("expire_at")).replace("Z", ""))
                left_min = max(0, int((exp - now).total_seconds() // 60))
            except Exception:
                left_min = None
            items.append({"short_id": s.get("short_id"), "message": (s.get("message") or "")[:70],
                          "created_at": str(s.get("created_at"))[5:16].replace("T", " "),
                          "left_min": left_min})
    return {"items": items, "total": d.get("total", 0)}


def human_queue() -> dict:
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "needs_human": _cached("needs_human", 120, _needs_human),
        "hitl_recent": _cached("hitl_recent", 30, _hitl_recent),
        "hil_pending": _cached("hil_pending", 60, _hil_pending),
        "requests": _cached("duty_requests", 20, _requests),
    }


# ---------- 工单收件箱（值守 Agent 的决策队列）----------

REQ_DIR = DUTY_DIR / "requests"
_STATUS_ORDER = {"escalated": 0, "answered": 1, "open": 2, "decided": 3, "resolved": 4, "dismissed": 5}


def _requests() -> dict:
    """读 duty/requests/*.json（flow 递来的决策工单 + 值守 Agent 的处置留痕）。"""
    rows = []
    try:
        paths = sorted(REQ_DIR.glob("req-*.json")) if REQ_DIR.is_dir() else []
    except Exception:
        paths = []
    for p in paths:
        try:
            d = json.load(open(p))
        except Exception:
            continue
        dec = d.get("decision") or {}
        hum = d.get("human") or {}
        rows.append({
            "id": d.get("id"), "status": d.get("status"), "severity": d.get("severity"),
            "from_flow": d.get("from_flow"), "kind": d.get("kind"),
            "title": (d.get("title") or "")[:120],
            "created_at": str(d.get("created_at") or "")[5:16].replace("T", " "),
            "action": dec.get("action") or "", "by": dec.get("by") or "",
            "rationale": (dec.get("rationale") or "")[:200],
            "human_reply": (hum.get("reply") or "")[:160],
            "human_notified": bool(hum.get("session_id")),
        })
    rows.sort(key=lambda r: (_STATUS_ORDER.get(r["status"] or "", 9), r.get("created_at") or ""))
    counts: dict = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    active = [r for r in rows if r["status"] in ("escalated", "answered", "open")]
    done = [r for r in rows if r["status"] not in ("escalated", "answered", "open")]
    return {"items": (active + done)[:20], "counts": counts,
            "active": len(active), "total": len(rows)}


# ---------- topology ----------

def _last_run_age() -> float | None:
    """最近一次 run 收尾距今秒数（runs.jsonl 尾行）——reaper 活跃度信号。"""
    out = _sh("ssh -o ConnectTimeout=8 tcloud_gz "
              "'tail -1 ~/.issue-keeper/pipeline/runs.jsonl 2>/dev/null'", timeout=20)
    try:
        ts = str(json.loads(out).get("ts") or "")
        import datetime as _dt
        t = _dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
        return max(0.0, (_dt.datetime.now() - t).total_seconds())
    except Exception:
        return None


def topology() -> dict:
    ov = _cached("ov_topo", 60, overview)
    sbx = _cached("sbx_live", 120, _sandbox_inventory)
    inflight = ov.get("inflight") or []
    vm = ov.get("vm") or {}
    a = ov.get("a_shift") or {}
    shadow = ov.get("shadow") or {}
    b = ov.get("b_shift") or {}

    def node(nid: str, label: str, kind: str, state: str, detail: str = "",
             meta: dict | None = None) -> dict:
        return {"id": nid, "label": label, "kind": kind, "state": state,
                "detail": detail, **(meta or {})}

    sbx_n = len((sbx.get("instances") or []))
    # 在途归属：inflight_query.py 顺带读 console-exec.json 的 flow_id（-sbx=灰度）
    main_n = sum(1 for i in inflight if i.get("engine") == "main")
    sbx_run_n = sum(1 for i in inflight if i.get("engine") == "sbx")

    import datetime as _dt

    def _age_min(ts: str | None) -> float | None:
        if not ts:
            return None
        try:
            t = _dt.datetime.strptime(str(ts)[:16], "%Y-%m-%d %H:%M")
            return (_dt.datetime.now() - t).total_seconds() / 60
        except Exception:
            return None

    last_run = _cached("last_run_age", 120, _last_run_age)  # 秒；None=取不到信号
    sh_age = _age_min(shadow.get("generated_at"))
    a_status, a_age = a.get("status"), (a.get("age_sec") or 0)
    c = ov.get("controller") or {}
    c_status, c_age = c.get("status"), (c.get("age_sec") or 0)
    b_age = None
    if b.get("time"):
        try:
            t = _dt.datetime.strptime(
                f"{_dt.date.today().isoformat()} {b['time']}", "%Y-%m-%d %H:%M")
            b_age = (_dt.datetime.now() - t).total_seconds() / 60
            if b_age < 0:
                b_age += 1440  # 跨零点
        except Exception:
            pass
    dlq_raw = str(vm.get("dlq") or "")
    dlq_n = int(dlq_raw) if dlq_raw.isdigit() else 0
    sched_ok, keeper_ok = vm.get("sched") == "active", vm.get("keeper") == "active"

    # 节点状态口径（前端配色）：working=橙·在干活 / idle=绿·空闲 /
    # warn=黄·降级 / error=红·异常 / unknown=灰·取不到信号。
    # 全部由实时信号推导（在途数/心跳新鲜度/末次收尾/服务状态），不再写死。
    nodes = [
        node("external", "外部用户 / jeffkit", "actor",
             "idle", "提 issue · /accept 验收"),
        node("github", "GitHub Issues", "store", "idle",
             "tunely 仓 + 各子仓", {"repo": "jeffkit/*"}),
        node("shadow", "keeper-shadow 派发", "flow",
             "working" if (sh_age is not None and sh_age < 6 and (shadow.get("would_dispatch") or 0) > 0)
             else "idle" if (sh_age is not None and sh_age < 20)
             else "warn" if sh_age is not None else "error",
             f"对账 {str(shadow.get('generated_at', ''))[5:16]} · budget={shadow.get('budget_left')}"),
        node("improve", "self-improve-v2 开发", "flow",
             "working" if main_n > 0 else "idle",
             f"主版在途 {main_n} / 全局在途 {len(inflight)}"),
        node("sbx", "沙箱版 sbx（灰度）", "flow",
             "working" if sbx_run_n > 0 else ("warn" if sbx_n > 0 else "idle"),
             f"AGS 实例 ×{sbx_n} · 灰度在途 {sbx_run_n}"),
        node("reaper", "reaper 收尾回评", "flow",
             "working" if last_run is not None and last_run < 1800
             else "idle" if last_run is not None and last_run < 28800
             else "warn" if last_run is not None else "unknown",
             f"末次收尾 {f'{last_run / 60:.0f}min 前' if last_run is not None else '—'}"),
        node("accept", "A 班 issue-accept 验收", "flow",
             "working" if a_status == "running"
             else "error" if a_status in ("failed", "aborted")
             else "idle" if a_age < 1800 else "warn" if a_age < 3600 else "error",
             f"轮{a.get('round')} · {a_age // 60}min 前"),
        node("external_check", "外部验收 /accept", "gate",
             "idle", "验收通过 → 合并/关单"),
        node("bwatch", "B 班 keeper-watch", "flow",
             "working" if b_age is not None and b_age < 120
             else "idle" if b_age is not None and b_age < 480
             else "warn" if b_age is not None else "unknown",
             f"{b.get('time', '—')} {b.get('kind') or ''}轮 · {b.get('brief', '')}".strip()),
        node("ctrl", "主控 ctrl-watch", "flow",
             "working" if c_status == "running"
             else "error" if c_status in ("failed", "aborted")
             else "warn" if c_status == "attention" or c_age > 7200
             else "idle" if c_status == "ok" else "unknown",
             "看门狗 + 升级路由"),
        node("duty", "duty 内核（状态权威）", "store",
             "idle" if (ov.get("roster") or {}).get("generation") else "error",
             f"roster gen={(ov.get('roster') or {}).get('generation')}"),
        node("core", "plaita 内核 + console", "infra",
             "idle" if sched_ok and keeper_ok and dlq_n == 0
             else "error" if not sched_ok and not keeper_ok else "warn",
             f"sched={vm.get('sched')} keeper={vm.get('keeper')} DLQ={vm.get('dlq') or '—'}"),
    ]
    edges = [
        {"from": "external", "to": "github", "label": "提 issue"},
        {"from": "github", "to": "shadow", "label": "轮询扫单"},
        {"from": "shadow", "to": "improve", "label": "派发（主版）"},
        {"from": "shadow", "to": "sbx", "label": "派发（灰度）"},
        {"from": "improve", "to": "reaper", "label": "终态"},
        {"from": "sbx", "to": "reaper", "label": "终态"},
        {"from": "reaper", "to": "accept", "label": "done 回评"},
        {"from": "accept", "to": "external_check", "label": "请验收"},
        {"from": "external_check", "to": "github", "label": "/accept"},
        {"from": "github", "to": "accept", "label": "验收信号", "dash": True},
        {"from": "accept", "to": "github", "label": "关单致谢"},
        {"from": "bwatch", "to": "duty", "label": "轮报/handoff"},
        {"from": "ctrl", "to": "duty", "label": "看门狗"},
        {"from": "accept", "to": "duty", "label": "轮报/handoff"},
        {"from": "duty", "to": "core", "label": "状态权威", "dash": True},
    ]
    return {"nodes": nodes, "edges": edges, "ts": time.strftime("%H:%M:%S")}


# ---------- 路由 ----------

@router.get("/duty/overview")
def duty_overview() -> dict:
    return _cached("duty_overview", 45, overview)


@router.get("/duty/stats")
def duty_stats() -> dict:
    return _cached("duty_stats", 120, stats)


@router.get("/duty/topology")
def duty_topology() -> dict:
    return _cached("duty_topology", 45, topology)


@router.get("/duty/human-queue")
def duty_human_queue() -> dict:
    return _cached("duty_human_queue", 60, human_queue)
