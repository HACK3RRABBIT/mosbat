#!/usr/bin/env python3
"""
mosbat WebUI — FastAPI application.

A single cohesive control panel for the mosbat news-forwarder:
  • Dashboard  – host metrics, queue depth, bot status
  • Bots       – start / stop / restart each bot (+ all)
  • Logs       – live tail of bot logs
  • Setup      – Telegram login flow (phone → code → 2FA) + config editor

The Telegram login is driven entirely from the web via webui/login_flow.py,
creating the user-sessions the bots require (no terminal needed).
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
import subprocess
import importlib.util
from pathlib import Path

# Make the webui package importable when run as `python -m webui.app`
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import asyncio
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

try:
    import psutil
except Exception:
    psutil = None

from state import get_bots, master_running, master_pid
from login_flow import status as login_status, load_state as load_login_state
import ai_filter

PROJ = Path(__file__).resolve().parent.parent
WEBUI = Path(__file__).resolve().parent
CTRL_FILE = WEBUI / "master_control.json"
QUEUE_FILE = "/tmp/feeder_queue.jsonl"
LOG_FILES = {"telegram": PROJ / "telegram_bot.log", "bale": PROJ / "bale_bot.log"}
CONFIG_FILE = PROJ / "config.py"

SECRET = os.environ.get("MOSBAT_WEBUI_PASS", "funlife")
SESSION = {"authed": False}

app = FastAPI(title="mosbat WebUI")
templates = Jinja2Templates(directory=str(WEBUI / "templates"))
app.mount("/static", StaticFiles(directory=str(WEBUI / "static")), name="static")

load_login_state()


# ── Auth ────────────────────────────────────────

def check_auth(request: Request):
    if SESSION["authed"]:
        return
    if request.cookies.get("mosbat_token") == SECRET:
        SESSION["authed"] = True
        return
    raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
def do_login(password: str = Form(...)):
    if password == SECRET:
        SESSION["authed"] = True
        resp = JSONResponse({"ok": True})
        resp.set_cookie("mosbat_token", SECRET, httponly=True, samesite="lax")
        return resp
    raise HTTPException(status_code=401, detail="bad password")


@app.get("/logout")
def logout():
    SESSION["authed"] = False
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("mosbat_token")
    return resp


# ── Helpers ─────────────────────────────────────

def issue_control(action: str, target: str = "all"):
    try:
        CTRL_FILE.write_text(json.dumps({"action": action, "target": target}))
        return True
    except Exception:
        return False


def tail_lines(path: Path, n: int = 300) -> list[str]:
    if not path.exists():
        return []
    try:
        out = subprocess.run(["tail", "-n", str(n), str(path)],
                             capture_output=True, text=True)
        return out.stdout.splitlines()
    except Exception:
        try:
            return path.read_text().splitlines()[-n:]
        except Exception:
            return []


def host_info() -> dict:
    info = {"platform": sys.platform, "python": sys.version.split()[0],
            "hostname": os.uname().nodename}
    if psutil:
        try:
            info.update({
                "cpu_percent": psutil.cpu_percent(interval=0.3),
                "mem_percent": psutil.virtual_memory().percent,
                "mem_used_mb": round(psutil.virtual_memory().used / 1e6, 1),
                "mem_total_mb": round(psutil.virtual_memory().total / 1e6, 1),
                "disk_percent": psutil.disk_usage("/").percent,
                "boot_secs": int(time.time() - psutil.boot_time()),
                "load_avg": [round(x, 2) for x in os.getloadavg()],
                "procs": len(psutil.pids()),
            })
        except Exception:
            pass
    return info


def queue_stats() -> dict:
    lines = 0
    last = None
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE) as f:
                for line in f:
                    if line.strip():
                        lines += 1
                        try:
                            last = json.loads(line).get("source_username")
                        except Exception:
                            pass
        except Exception:
            pass
    return {"pending_lines": lines, "last_source": last}


def read_config() -> dict:
    cfg = {}
    try:
        spec = importlib.util.spec_from_file_location("cfg", CONFIG_FILE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for k in ("API_ID", "API_HASH", "BOT_TOKEN", "TG_MAIN_ADMIN",
                  "TARGET_CHANNEL", "BALE_BOT_TOKEN", "BALE_TARGET_CHANNEL",
                  "BALE_MAX_VIDEO_BYTES"):
            cfg[k] = getattr(mod, k, None)
        cfg["SOURCE_CHANNELS"] = getattr(mod, "SOURCE_CHANNELS", [])
        cfg["ADMIN_IDS"] = getattr(mod, "ADMIN_IDS", [])
        cfg["BALE_ADMIN_IDS"] = getattr(mod, "BALE_ADMIN_IDS", [])
        cfg["SOURCE_NAMES"] = getattr(mod, "SOURCE_NAMES", {})
    except Exception as e:
        cfg["_error"] = str(e)
    return cfg


# ── Pages ───────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    check_auth(request)
    return templates.TemplateResponse(request, "index.html", {
        "bots": get_bots(),
        "master_running": master_running(),
        "host": host_info(),
        "queue": queue_stats(),
    })


@app.get("/bots", response_class=HTMLResponse)
def bots_page(request: Request):
    check_auth(request)
    return templates.TemplateResponse(request, "bots.html", {
        "bots": get_bots(), "master_running": master_running(),
    })


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, which: str = "telegram"):
    check_auth(request)
    return templates.TemplateResponse(request, "logs.html", {"which": which})


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    check_auth(request)
    return templates.TemplateResponse(request, "setup.html", {
        "cfg": read_config(),
        "login": login_status(),
    })


# ── API: status / control ───────────────────────

@app.get("/api/status")
def api_status(request: Request):
    check_auth(request)
    return JSONResponse({
        "bots": get_bots(),
        "master_running": master_running(),
        "master_pid": master_pid(),
        "host": host_info(),
        "queue": queue_stats(),
        "ai": {"settings": _ai_public_settings(), "stats": ai_filter.read_stats_file()},
        "ts": int(time.time()),
    })


@app.post("/api/control")
async def api_control(request: Request, action: str = Form(...), target: str = Form("all")):
    check_auth(request)
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "bad action")
    if target not in ("all", "telegram_bot", "bale_bot"):
        raise HTTPException(400, "bad target")
    return JSONResponse({"ok": issue_control(action, target)})


# ── API: logs ───────────────────────────────────

@app.get("/api/logs")
def api_logs(request: Request, which: str = "telegram", n: int = 400):
    check_auth(request)
    return PlainTextResponse("\n".join(tail_lines(LOG_FILES.get(which, LOG_FILES["telegram"]), n)))


@app.get("/api/logs/tail")
async def api_logs_tail(request: Request, which: str = "telegram"):
    check_auth(request)
    path = LOG_FILES.get(which, LOG_FILES["telegram"])

    async def gen():
        inode = None
        offset = 0
        if path.exists():
            try:
                with open(path) as f:
                    f.seek(0, os.SEEK_END)
                    offset = f.tell()
                inode = os.stat(path).st_ino
            except Exception:
                pass
        while True:
            if await request.is_disconnected():
                break
            try:
                if not path.exists():
                    await asyncio.sleep(1)
                    continue
                if os.stat(path).st_ino != inode:
                    inode = os.stat(path).st_ino
                    offset = 0
                with open(path) as f:
                    f.seek(offset)
                    for line in f:
                        yield (line if line.endswith("\n") else line + "\n").encode()
                    offset = f.tell()
            except Exception:
                pass
            await asyncio.sleep(0.8)
    return StreamingResponse(gen(), media_type="text/plain")


# ── API: login flow ─────────────────────────────

@app.get("/api/login/status")
def api_login_status(request: Request):
    check_auth(request)
    return JSONResponse(login_status())


@app.post("/api/login/phone")
async def api_login_phone(request: Request, phone: str = Form(...)):
    check_auth(request)
    from login_flow import start_phone
    try:
        return JSONResponse(start_phone(phone))
    except Exception as e:
        return JSONResponse({"step": "idle", "error": str(e), "needs": [], "done": False})


@app.post("/api/login/code")
async def api_login_code(request: Request, code: str = Form(...)):
    check_auth(request)
    from login_flow import submit_code
    try:
        return JSONResponse(submit_code(code))
    except Exception as e:
        return JSONResponse({"step": "code", "error": str(e), "needs": ["code"], "done": False})


@app.post("/api/login/password")
async def api_login_password(request: Request, password: str = Form(...)):
    check_auth(request)
    from login_flow import submit_password
    try:
        return JSONResponse(submit_password(password))
    except Exception as e:
        return JSONResponse({"step": "password", "error": str(e), "needs": ["password"], "done": False})


@app.post("/api/login/reset")
def api_login_reset(request: Request):
    check_auth(request)
    from login_flow import reset, status as lstatus
    reset()
    return JSONResponse(lstatus())


@app.post("/api/login/makebale")
def api_login_makebale(request: Request):
    check_auth(request)
    from login_flow import create_bale_dl_session, status as lstatus
    ok = create_bale_dl_session()
    return JSONResponse({"ok": ok, **lstatus()})


# ── API: AI news filter ──────────────────────────

@app.get("/api/ai")
def api_ai(request: Request):
    check_auth(request)
    return JSONResponse({
        "settings": _ai_public_settings(),
        "stats": ai_filter.read_stats_file(),
    })


@app.post("/api/ai/toggle")
async def api_ai_toggle(request: Request, enabled: str = Form("true")):
    check_auth(request)
    on = str(enabled).lower() in ("1", "true", "on", "yes")
    new_state = ai_filter.set_enabled(on)
    return JSONResponse({"ok": True, "enabled": new_state})


@app.post("/api/ai/media/toggle")
async def api_ai_media_toggle(request: Request, enabled: str = Form("true")):
    check_auth(request)
    on = str(enabled).lower() in ("1", "true", "on", "yes")
    try:
        try:
            with open(ai_filter.SETTINGS_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {}
        data["media_gate"] = bool(on)
        tmp = ai_filter.SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ai_filter.SETTINGS_FILE)
        ai_filter.load_settings()
        return JSONResponse({"ok": True, "media_gate": bool(on)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/ai/reset")
def api_ai_reset(request: Request):
    check_auth(request)
    ai_filter.reset_stats()
    return JSONResponse({"ok": True, "stats": ai_filter.read_stats_file()})


@app.post("/api/ai/save")
async def api_ai_save(request: Request,
                      base_url: str = Form(""), model: str = Form(""),
                      api_key: str = Form(""), dedup_ttl_seconds: str = Form("")):
    check_auth(request)
    try:
        try:
            with open(ai_filter.SETTINGS_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {}
        if base_url.strip():
            data["base_url"] = base_url.strip()
        if model.strip():
            data["model"] = model.strip()
        # Only overwrite the key if a real (non-masked) value was provided.
        if api_key.strip() and not api_key.strip().startswith("sk-…"):
            data["api_key"] = api_key.strip()
        if dedup_ttl_seconds.strip():
            try:
                data["dedup_ttl_seconds"] = int(dedup_ttl_seconds)
            except Exception:
                pass
        tmp = ai_filter.SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ai_filter.SETTINGS_FILE)
        ai_filter.load_settings()
        return JSONResponse({"ok": True, "settings": _ai_public_settings()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


# ── API: config editor ──────────────────────────

@app.get("/api/config")
def api_config(request: Request):
    check_auth(request)
    return JSONResponse(read_config())


@app.post("/api/config/channels")
async def api_config_channels(request: Request, channels: str = Form(...)):
    """channels: newline/space separated @handles."""
    check_auth(request)
    handles = []
    for tok in re.split(r"[\s,]+", channels.strip()):
        tok = tok.strip()
        if not tok:
            continue
        if not tok.startswith("@"):
            tok = "@" + tok
        if tok not in handles:
            handles.append(tok)
    try:
        _set_config_list("SOURCE_CHANNELS", handles)
        return JSONResponse({"ok": True, "channels": handles})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/config/field")
async def api_config_field(request: Request, key: str = Form(...), value: str = Form(...)):
    check_auth(request)
    allowed = {"BOT_TOKEN", "TG_MAIN_ADMIN", "TARGET_CHANNEL",
               "BALE_BOT_TOKEN", "BALE_TARGET_CHANNEL", "BALE_MAX_VIDEO_BYTES",
               "API_ID", "API_HASH"}
    if key not in allowed:
        raise HTTPException(400, "field not editable")
    try:
        if key in ("API_ID",):
            _set_config_value(key, int(value))
        elif key == "BALE_MAX_VIDEO_BYTES":
            _set_config_value(key, int(value))
        else:
            _set_config_value(key, value)
        return JSONResponse({"ok": True, key: key})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


def _ai_public_settings() -> dict:
    s = ai_filter.get_settings()
    out = {k: v for k, v in s.items() if k != "api_key"}
    key = s.get("api_key") or ""
    out["api_key_set"] = bool(key)
    out["api_key_masked"] = ("sk-…" + key[-4:]) if len(key) > 4 else ""
    out["enabled"] = ai_filter.is_enabled()
    out["media_gate"] = bool(s.get("media_gate", True))
    return out


def _config_lines() -> list[str]:
    return CONFIG_FILE.read_text().splitlines()


def _write_config_lines(lines: list[str]):
    CONFIG_FILE.write_text("\n".join(lines) + "\n")


def _set_config_value(key: str, value) -> None:
    lines = _config_lines()
    rendered = repr(value)
    patched = False
    out = []
    for ln in lines:
        m = re.match(rf"^(\s*{re.escape(key)}\s*=\s*).*$", ln)
        if m:
            out.append(m.group(1) + rendered)
            patched = True
        else:
            out.append(ln)
    if not patched:
        out.append(f"{key} = {rendered}")
    _write_config_lines(out)


def _set_config_list(key: str, items: list) -> None:
    """Replace the whole `key = [ ... ]` block (multi-line safe)."""
    lines = _config_lines()
    rendered = [f"{key} = ["] + [f'    "{it}",' for it in items] + ["]"]
    out = []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if re.match(rf"^\s*{re.escape(key)}\s*=\s*\[", ln):
            # consume until the matching closing bracket (track nesting)
            depth = 0
            while i < n:
                depth += ln.count("[") - ln.count("]")
                if depth <= 0 and "]" in ln:
                    i += 1
                    break
                i += 1
                if i < n:
                    ln = lines[i]
            out.extend(rendered)
            continue
        out.append(ln)
        i += 1
    _write_config_lines(out)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MOSBAT_WEBUI_PORT", "5678"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
