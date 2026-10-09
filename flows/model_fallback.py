#!/usr/bin/env python3
"""值守：GLM 配额烧穿自动切 DeepSeek（每 5 分钟探一次，供 launchd/cron 调用）。

jeffkit 2026-10-10：「今晚如果 glm 又烧光了，换上 deepseek 跑吧」。
本脚本把「发现烧穿 → 切档」做成确定性动作，不依赖值守 Agent 在场：

  探 GLM anthropic 端点 → 若返回 429/1301/1308（限额）→ 调 model_tier.sh deepseek
  → 记录到 rounds.log；恢复（200）且当前在 deepseek 档 → **不自动切回**
  （避免抖动；由值守或人显式 `model_tier.sh glm`）。

用法： model_fallback.py [--dry-run]
退出码：0=无动作或切档成功；1=切档失败（会打日志）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HOST = "tcloud_gz"
TIER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_tier.sh")
ROUNDS = os.path.expanduser("~/.issue-keeper/duty/rounds.log")
BASE = "https://open.bigmodel.cn/api/anthropic"
MODEL = "glm-5.3-flash"


def _log(msg: str) -> None:
    line = "%s model-fallback %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S+08:00"), msg)
    try:
        os.makedirs(os.path.dirname(ROUNDS), exist_ok=True)
        with open(ROUNDS, "a") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001 — 日志失败不影响判定
        pass
    print(line.strip())


def _sh(cmd: str, timeout: int = 40) -> str:
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=15", HOST, cmd],
                       capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def current_tier() -> str:
    out = _sh("sed -n '/^agent_env:/,/^pipeline:/p' ~/.issue-keeper/config.yaml")
    if "api.deepseek.com" in out:
        return "deepseek"
    if "bigmodel.cn" in out:
        return "glm"
    return "unknown"


def probe_glm() -> tuple[int, str]:
    """在 VM 上探 GLM（key 只存在于 VM 的 env.sh，不下发到本机）。"""
    py = (
        "import json,os,subprocess,urllib.request\n"
        "k=os.environ.get('GLM_API_KEY','')\n"
        "req=urllib.request.Request('" + BASE + "/v1/messages',\n"
        "  data=json.dumps({'model':'" + MODEL + "','max_tokens':8,"
        "'messages':[{'role':'user','content':'ping'}]}).encode(),\n"
        "  headers={'x-api-key':k,'anthropic-version':'2023-06-01','content-type':'application/json'})\n"
        "try:\n"
        "    r=urllib.request.urlopen(req,timeout=25); print(r.status, r.read(200).decode('utf-8','replace'))\n"
        "except Exception as e:\n"
        "    code=getattr(e,'code',0)\n"
        "    body=''\n"
        "    try: body=e.read(300).decode('utf-8','replace')\n"
        "    except Exception: pass\n"
        "    print(code, body)\n"
    )
    out = _sh("set -a; . ~/.issue-keeper/env.sh 2>/dev/null; set +a; python3 -c \"%s\"" %
              py.replace('"', '\\"'))
    m = re.match(r"\s*(\d+)\s*(.*)", out.strip().splitlines()[-1] if out.strip() else "")
    if not m:
        return 0, out[:200]
    return int(m.group(1)), m.group(2)


def main() -> int:
    dry = "--dry-run" in sys.argv
    tier = current_tier()
    code, body = probe_glm()
    exhausted = code == 429 or ("1301" in body) or ("1308" in body) or ("limit" in body.lower())

    if exhausted and tier == "glm":
        _log("GLM 限额烧穿（HTTP %s）→ 切 DeepSeek%s" % (code, "（dry-run）" if dry else ""))
        if dry:
            return 0
        r = subprocess.run(["bash", TIER, "deepseek"], capture_output=True, text=True, timeout=90)
        ok = "deepseek" in r.stdout
        _log("切档%s" % ("成功 ✓" if ok else "失败 ✗: " + (r.stderr or "")[:200]))
        # 切档是**事件**：递一张工单，duty-watch 插件会往值守会话 inbox 投递
        # （这样无需我每 10 分钟轮询；只有真发生切换时才唤醒值守）。
        _file_ticket(ok, code, body)
        return 0 if ok else 1

    if exhausted:
        _log("GLM 仍限额，但当前已是 %s 档（不重复切）" % tier)
        return 0
    if code == 200 and tier == "deepseek":
        _log("GLM 已恢复（HTTP 200），当前 deepseek 档——**不自动切回**（防抖动，需显式切）")
        return 0
    _log("GLM 正常（HTTP %s），当前 %s 档，无动作" % (code, tier))
    return 0


if __name__ == "__main__":
    sys.exit(main())


def _file_ticket(ok: bool, code: int, body: str) -> None:
    """切档后递工单（open 状态 → duty-watch 插件注入值守会话）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    req = os.path.join(here, "duty_request.py")
    title = ("[配额] GLM 烧穿已自动切 DeepSeek" if ok
             else "[配额] GLM 烧穿自动切档**失败**")
    ctx = {"detected_http": code, "glm_body": (body or "")[:200],
           "action": "model_tier.sh deepseek" if ok else "需人工排查 agent_env",
           "revert": "恢复 GLM：bash flows/model_tier.sh glm（备份额度 config.yaml.bak-<日期>-tier）"}
    opts = "keep,switch_back_glm,escalate_human" if ok else "escalate_human"
    try:
        subprocess.run(
            ["python3", req, "create", "--from-flow", "model-fallback",
             "--kind", "quota-switch", "--severity", "critical" if not ok else "warn",
             "--title", title, "--context-json", json.dumps(ctx, ensure_ascii=False),
             "--options", opts],
            capture_output=True, text=True, timeout=60, check=False)
    except Exception as e:  # noqa: BLE001 — 递单失败不影响切档本身
        _log("递工单失败（忽略）: %s" % str(e)[:120])
