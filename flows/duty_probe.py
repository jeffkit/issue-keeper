#!/usr/bin/env python3
"""duty_probe.py —— 值守轮次的**确定性体检探针**：一次跑完，输出一条 VERDICT。

背景（2026-10-09 教训，jeffkit 点名）：
- 磁盘守卫在 Mac(17.2G)/VM(19.2G) 双双跌破 20G，16:14-16:41 间 8 次派发全部
  `pre.ok=False → retry-later`——管线**产出为零**，但 8 个 flow 轮报全绿。
- disk-hygiene **确实**递了 critical 工单（标题含「挡单 5」），但值守那一轮没跑
  「读收件箱」，信号躺在 inbox 里没人看。
- 孤儿沙箱（paused + 执行已终态）无工单路径，只有跑 sweep 才看得见。

结论：**活着 ≠ 出活**。探针把「工单 / 产出 / 磁盘 / 沙箱浪费」四路信号收敛成一条
判决，作为每轮值守的第一步（无例外）。任何一路 DEGRADED/BLOCKED 都必须当场处置。

用法：
  python3 duty_probe.py                 # 人读摘要
  python3 duty_probe.py --json          # 机器读
  python3 duty_probe.py --window-min 90
退出码：0=OK，1=DEGRADED，2=BLOCKED（便于 flow/脚本判级）
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

CONSOLE = "http://127.0.0.1:8323"
KEY = os.environ.get("PLAITA_CONSOLE_ADMIN_API_KEY") or "b4b5042ee7d1b937633c08f3f50d4c8efbca88d33ece8a03"
REQ_DIR = pathlib.Path("~/.issue-keeper/duty/requests").expanduser()
IK = pathlib.Path("~/projects/infra4agent/issue-keeper").expanduser()
FLOWS = ("self-improve-v2", "self-improve-v2-sbx")
# 终态标志：落到基线的成功路径 vs 被 preflight 挡回
LANDED_MARK = ("ok_eq_true", "_n14")
BLOCKED_MARK = "contains_str_why_or_disk"


def _get(path: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(CONSOLE + path, headers={"X-Admin-API-Key": KEY})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def probe_tickets() -> dict:
    """未读工单：open/escalated 即为待我处置（answered=已有回复待执行）。"""
    open_rows, crit = [], 0
    for p in sorted(REQ_DIR.glob("req-*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        st = d.get("status")
        if st in ("open", "escalated", "answered"):
            open_rows.append({"id": d.get("id"), "status": st, "sev": d.get("severity"),
                              "from": d.get("from_flow"), "title": str(d.get("title"))[:90]})
            if d.get("severity") == "critical":
                crit += 1
    return {"open": open_rows, "n_open": len(open_rows), "n_critical": crit}


def probe_throughput(window_min: int, max_fetch: int = 18) -> dict:
    """近 window_min 的派发产出：attempts / blocked(按原因) / error / cancelled / landed。

    ⚠️ 口径（另一值守实例 2026-10-09 20:15 指出并修正）：
    - `attempts` = **窗口内派发**的次数；
    - `landed` = **窗口内落地**（含很久以前派发、但在窗口内完成的 run）——若只按派发窗口
      统计，会出现「明明落地了却报 0 落地」的假 DEGRADED（实测：plaita#33 于 16:41 派发、
      20:0x 落地，落在 90min 窗口外 → 探针误报「近 90min 0 落地」）。
    """
    import time
    cutoff = time.time() - window_min * 60

    # 观测失败计数（2026-10-10 jeffkit 抓到的严重问题）：
    # 此前 `/api/executions` 失败（如 500）被 `except: continue` **静默吞掉**，
    # 统计全部归零，输出成「近90m 派发 0 / 落地 0 / 在跑 0」——**与真实「没有
    # 工作」完全不可区分**。值守据此连报数轮「一切正常」，实际是探针瞎了。
    # 现在显式计数，供上层把它标成 DEGRADED/BLOCKED 而不是 OK。
    probe_errors: list = []

    def _ts(v):
        try:
            return time.mktime(time.strptime(str(v)[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            return None

    rows, fetched = [], 0
    for fid in FLOWS:
        try:
            d = _get(f"/api/executions?flow_id={fid}&limit=30")
        except Exception as e:  # noqa: BLE001 — 但**必须留痕**，不可静默归零
            probe_errors.append(f"列表 {fid}: {type(e).__name__}: {str(e)[:80]}")
            continue
        exs = d if isinstance(d, list) else d.get("executions") or []
        for e in exs:
            st_time = e.get("start_time") or e.get("started_at")
            if not st_time:
                continue
            st_ts = _ts(st_time)
            if st_ts is None:
                continue
            up_ts = _ts(e.get("last_update_time") or st_time)
            in_window_dispatch = st_ts >= cutoff
            in_window_update = up_ts is not None and up_ts >= cutoff
            if not (in_window_dispatch or in_window_update):
                continue
            rows.append({"eid": e["execution_id"], "flow": fid, "status": e.get("status"),
                         "start": str(st_time)[:19], "start_ts": st_ts,
                         "update_ts": up_ts, "in_window_dispatch": in_window_dispatch})
    stats = {"attempts": sum(1 for r in rows if r["in_window_dispatch"]),
             "landed": 0, "blocked_disk": 0, "blocked_other": 0,
             "engine_error": 0, "cancelled": 0, "running": 0, "completed_no_land": 0,
             "block_reasons": {}, "landed_runs": [], "probe_errors": probe_errors}
    for r in rows:
        if r["status"] == "running":
            stats["running"] += 1
            continue
        if r["status"] == "cancelled":
            stats["cancelled"] += 1
            continue
        if fetched >= max_fetch:
            continue
        try:
            full = _get(f"/api/executions/{r['eid']}", timeout=12)
        except Exception as e:  # noqa: BLE001 — 留痕，不可静默
            probe_errors.append(f"详情 {r['eid'][:12]}: {type(e).__name__}: {str(e)[:60]}")
            continue
        fetched += 1
        nd = (full.get("context") or {}).get("$NODE") or {}
        keys = list((full.get("node_timings") or {}).keys())
        inp = (full.get("context") or {}).get("$INPUT") or {}
        rid = str(inp.get("run_id") or "")
        pre = nd.get("pre") or {}
        # 落地判据：① `$NODE.land_push.ok=True`（直推成功的权威信号，2026-10-09 实测
        # plaita#33 的 node_timings 只到 any_eq_false，但 land_push 结果在 $NODE 里）；
        # ② 兜底沿用节点键标记。
        lp = nd.get("land_push") or {}
        landed = (isinstance(lp, dict) and bool(lp.get("ok"))) or any(k in keys for k in LANDED_MARK)
        if landed:
            # 窗口内落地：不论何时派发都计入（这正是修正点）
            stats["landed"] += 1
            stats["landed_runs"].append(rid)
        elif not r["in_window_dispatch"]:
            # 窗口外派发、窗口内仅有一次更新且未落地 → 不参与受阻/异常统计（历史账）
            continue
        elif BLOCKED_MARK in keys or pre.get("ok") is False:
            why = str(pre.get("why") or "unknown")
            if "disk" in why.lower():
                stats["blocked_disk"] += 1
            else:
                stats["blocked_other"] += 1
            key = re.sub(r"[\d.]+", "N", why)[:48]
            stats["block_reasons"][key] = stats["block_reasons"].get(key, 0) + 1
        elif full.get("status") in ("failed", "error"):
            stats["engine_error"] += 1
        else:
            stats["completed_no_land"] += 1
    return stats


def _df(path: str) -> float | None:
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize / 1e9
    except Exception:
        return None


def probe_disk(min_gib: float = 20.0) -> dict:
    """双端磁盘：disk-hygiene flow 只看 VM，Mac 侧今天正是盲区。"""
    out = {"min_gib": min_gib}
    for name, path in (("mac", "/"), ("vm", None)):
        if name == "mac":
            free = _df(path)
        else:
            free = None
            try:
                r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "tcloud_gz",
                                    "df -BG --output=avail / | tail -1"],
                                   capture_output=True, text=True, timeout=25)
                m = re.search(r"(\d+)", r.stdout or "")
                free = float(m.group(1)) if m else None
            except Exception:
                free = None
        out[name] = {"free_gib": round(free, 1) if free is not None else None,
                     "below_min": (free is not None and free < min_gib)}
    return out


def probe_sandbox() -> dict:
    """沙箱浪费：实例 exist 但执行已终态（孤儿）或 paused 且无活跃执行。"""
    out = {"instances": 0, "orphans": 0, "paused_idle": 0, "waste_instance_hours": 0.0, "detail": []}
    env = dict(os.environ, E2B_DOMAIN="ap-guangzhou.tencentags.com",
               E2B_API_KEY=os.environ.get("E2B_API_KEY", "e2b_725235357335be8d27367c596c9e3199cf3c5eeb"))
    py = str(IK.parent / "plaita/.venv/bin/python")
    try:
        r = subprocess.run([py, str(IK / "flows/ags-list.py")], capture_output=True, text=True,
                           timeout=60, env=env)
        inst = json.loads(r.stdout or "[]")
    except Exception as e:
        return {**out, "error": str(e)[:80]}
    for it in inst:
        out["instances"] += 1
        age = float(it.get("age_h") or 0)
        st = it.get("state")
        exec_st = None
        exec_age_min = None
        try:
            d = _get(f"/api/executions/{it.get('exec')}")
            exec_st = d.get("status")
            lu = d.get("last_update_time") or d.get("updated_at")
            if lu:
                exec_age_min = (time.time() - time.mktime(
                    time.strptime(str(lu)[:19], "%Y-%m-%dT%H:%M:%S"))) / 60
        except Exception:
            pass
        waste = False
        if exec_st in ("completed", "failed", "error", "cancelled"):
            out["orphans"] += 1
            waste = True
        elif st in ("paused", "pausing"):
            # E2B 会在空闲期自动 pause/pausing，活跃 run 的沙箱状态本就滚动
            # （2026-10-09 18:0x 实测：4 实例状态 pausing/running 交替，其执行末节点
            #  刚在 18:00:1x 更新——被误计为浪费 2.4 实例小时）。只有执行非 running，
            #  或 running 但进度龄 >30min（真卡住）才算浪费。
            stalled = exec_st != "running" or exec_age_min is None or exec_age_min > 30
            if stalled:
                out["paused_idle"] += 1
                waste = True
        if waste:
            out["waste_instance_hours"] += age
            out["detail"].append({"short": it.get("short"), "age_h": age, "state": st,
                                  "exec_status": exec_st,
                                  "exec_progress_age_min": round(exec_age_min, 1) if exec_age_min is not None else None})
    out["waste_instance_hours"] = round(out["waste_instance_hours"], 2)
    return out


def probe_retry_storm(window_min: int = 30, threshold: int = 5) -> dict:
    """重投风暴探针（2026-10-09 盲区补丁，另一值守实例的发现）。

    当天 90 分钟零落地的真因是「节点确定性失败 → 显式重投同体副本」循环，而探针此前
    完全看不见（node_timings 只在节点边界写、执行又不终态）。指纹：日志里的
    `第 N/5 次重试` —— 当天 598 次全是 `1/5`（plaita#73：重试预算键被任一节点成功清零）。
    这里按执行聚合两端 worker 日志的失败重投次数，超阈值即报。
    """
    import collections
    rx = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*?第 (\d+)/5 次重试，execution_id=([0-9a-f]+)")
    sources: list[list[str]] = []
    mac_log = pathlib.Path("~/.plaita-console/worker-mac.log").expanduser()
    try:
        with open(mac_log, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 2_000_000))
            sources.append(f.read().decode("utf-8", "ignore").splitlines())
    except Exception:
        pass
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "tcloud_gz",
                            "tail -c 2000000 ~/.plaita-console/worker-tcg1.log"],
                           capture_output=True, text=True, timeout=30)
        sources.append((r.stdout or "").splitlines())
    except Exception:
        pass

    now = time.time()
    by_exec: collections.Counter = collections.Counter()
    ordinals: collections.Counter = collections.Counter()
    total = 0
    recent10 = 0
    for lines in sources:
        for line in lines:
            m = rx.search(line)
            if not m:
                continue
            try:
                ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            except Exception:
                continue
            age = now - ts
            if age <= 600:
                recent10 += 1
            if age > window_min * 60:
                continue
            total += 1
            by_exec[m.group(3)[:8]] += 1
            ordinals[m.group(2)] += 1
    worst = by_exec.most_common(1)[0] if by_exec else ("", 0)
    return {"window_min": window_min, "total": total, "recent10": recent10,
            "by_exec": dict(by_exec.most_common(5)), "ordinals": dict(ordinals),
            "worst_exec": worst[0], "worst_count": worst[1], "threshold": threshold,
            # 只有「仍在进行」才算问题：停息后 30 分钟窗口里仍留着一堆历史计数，
            # 若据此报警会连续误报（2026-10-09 18:3x 实测：近 30min 92 次但近 10min 为 0）。
            "active": recent10 >= 3}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-min", type=int, default=90)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    tk = probe_tickets()
    tp = probe_throughput(args.window_min)
    dk = probe_disk()
    sb = probe_sandbox()
    rs = probe_retry_storm(30)

    reasons = []
    level = "OK"
    # 观测链路自检**必须最先判**（2026-10-10 jeffkit 抓到的严重问题）：
    # 若 console API 读不到，下面所有统计都是 0，而 0 会被误读成「没有工作」。
    # 观测失败 = 探针本身不可信 ⇒ 直接 BLOCKED（不是 DEGRADED：这不是「有点
    # 异常」，而是「你根本不知道发生了什么」）。此前靠静默 except 归零，
    # 值守连续数轮拿着假 OK 汇报。
    errs = tp.get("probe_errors") or []
    if errs:
        level = "BLOCKED"
        reasons.append(
            "**观测链路失败**：console API 有 %d 处读取失败，以下统计（派发/落地/"
            "在跑）**全部不可信**，不得据此判断系统健康。首个错误：%s"
            % (len(errs), errs[0])
        )
    if tk["n_critical"] > 0:
        level = "BLOCKED"
        reasons.append("%d 张 critical 工单未处置" % tk["n_critical"])
    if tp["attempts"] >= 3 and tp["landed"] == 0 and (tp["blocked_disk"] + tp["blocked_other"]) >= 3:
        top = max(tp["block_reasons"].items(), key=lambda kv: kv[1])[0] if tp["block_reasons"] else "?"
        if tp["running"] >= 1:
            # 有 run 在跑 = 派发链路当前是通的，历史挡回不等于现在卡死（2026-10-09
            # 实证：磁盘修好后 6 个 run pre.ok=True 在跑，探针仍报 BLOCKED 属误报疲劳）。
            level = max(level, "DEGRADED", key=["OK", "DEGRADED", "BLOCKED"].index)
            reasons.append("恢复中：近 %dmin 历史挡回 %d 次，当前 %d 个 run 在跑"
                           % (args.window_min, tp["blocked_disk"] + tp["blocked_other"], tp["running"]))
        else:
            level = "BLOCKED"
            reasons.append("近 %dmin %d 次派发 0 落地且无 run 在跑，主因=%s"
                           % (args.window_min, tp["attempts"], top))
    elif tp["attempts"] >= 5 and tp["landed"] == 0:
        level = max(level, "DEGRADED", key=["OK", "DEGRADED", "BLOCKED"].index)
        reasons.append("近 %dmin %d 次派发 0 落地" % (args.window_min, tp["attempts"]))
    for side in ("mac", "vm"):
        if dk[side]["below_min"]:
            level = "BLOCKED"
            reasons.append("%s 可用 %.1fG < 守卫线 %.0fG" % (side, dk[side]["free_gib"], dk["min_gib"]))
        elif dk[side]["free_gib"] is not None and dk[side]["free_gib"] < dk["min_gib"] + 5:
            level = max(level, "DEGRADED", key=["OK", "DEGRADED", "BLOCKED"].index)
            reasons.append("%s 余量薄 %.1fG" % (side, dk[side]["free_gib"]))
    if sb["orphans"] > 0:
        level = max(level, "DEGRADED", key=["OK", "DEGRADED", "BLOCKED"].index)
        reasons.append("%d 个孤儿沙箱（浪费 %.1f 实例小时）" % (sb["orphans"], sb["waste_instance_hours"]))
    if sb["paused_idle"] > 0:
        reasons.append("%d 个 paused 空闲实例" % sb["paused_idle"])
    if rs["active"]:
        ordinal_hint = "（全为第 1/5 → 疑 #73 预算键被清零）" if set(rs["ordinals"]) <= {"1"} else ""
        level = max(level, "DEGRADED", key=["OK", "DEGRADED", "BLOCKED"].index)
        reasons.append("重投风暴进行中：执行 %s 近 %dmin 重投 %d 次、近 10min %d 次%s"
                       % (rs["worst_exec"], rs["window_min"], rs["worst_count"], rs["recent10"], ordinal_hint))

    verdict = {"level": level, "reasons": reasons, "tickets": tk, "throughput": tp,
               "disk": dk, "sandbox": sb, "retry_storm": rs}
    # 心跳：独立兜底脚本（duty_escalation.py，launchd */3）据此判断「值守是否在场」——
    # 探针是每轮第 0 步，所以它的 mtime 就是值守活跃度最可靠的代理。
    try:
        hb = REQ_DIR.parent / "probe-heartbeat.json"
        hb.write_text(json.dumps({"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                  "level": level, "reasons": reasons[:3]}, ensure_ascii=False),
                      encoding="utf-8")
    except Exception:
        pass
    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
    else:
        print("VERDICT=%s | %s" % (level, "；".join(reasons) if reasons else "四路信号正常"))
        print("  工单: 待处置 %d（critical %d）" % (tk["n_open"], tk["n_critical"]))
        if errs:
            # 不给裸 0 —— 裸 0 会被读成「没有工作」。显式标注为「不可读」。
            print("  产出: **不可读**（console API %d 处失败，下面的 0 不代表没有工作）"
                  % len(errs))
            for e in errs[:3]:
                print("        · %s" % e)
        else:
            print("  产出: 近%dm 派发 %d / 落地 %d / 磁盘挡回 %d / engine_error %d / cancelled %d / 在跑 %d"
                  % (args.window_min, tp["attempts"], tp["landed"], tp["blocked_disk"],
                     tp["engine_error"], tp["cancelled"], tp["running"]))
        print("  磁盘: mac %sG / vm %sG（线 %.0fG）"
              % (dk["mac"]["free_gib"], dk["vm"]["free_gib"], dk["min_gib"]))
        print("  沙箱: 实例 %d / 孤儿 %d / paused 空闲 %d / 浪费 %.2f 实例小时"
              % (sb["instances"], sb["orphans"], sb["paused_idle"], sb["waste_instance_hours"]))
        print("  重投: 近%dm %d 次 / 近10min %d 次%s%s"
              % (rs["window_min"], rs["total"], rs["recent10"],
                 ("，最多 " + rs["worst_exec"] + "×" + str(rs["worst_count"])) if rs["worst_exec"] else "",
                 "（进行中）" if rs["active"] else "（已停息）"))
    return {"OK": 0, "DEGRADED": 1, "BLOCKED": 2}[level]


if __name__ == "__main__":
    sys.exit(main())
