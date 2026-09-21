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
    psutil.cpu_percent(interval=None)  # prime the internal baseline (see host_info)
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
CHANNELS_FILE = Path("/tmp/feeder_channels.json")  # shared, live source of truth (see telegram_bot.py)

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
    """Read the last n lines without spawning a `tail` subprocess — log files
    are now rotation-capped at 5MB (see telegram_bot.py/bale_bot.py), so a
    plain read is cheap and a fork+exec per page load isn't worth it."""
    if not path.exists():
        return []
    try:
        return path.read_text(errors="replace").splitlines()[-n:]
    except Exception:
        return []


def host_info() -> dict:
    info = {"platform": sys.platform, "python": sys.version.split()[0],
            "hostname": os.uname().nodename}
    if psutil:
        try:
            info.update({
                # interval=None is non-blocking: it returns the delta since the
                # last call (primed once at import time) instead of sleeping
                # 300ms inside every request — this endpoint is polled every
                # 2.5s by the dashboard, and a 300ms block on a single-core box
                # steals real scheduling time from the bots.
                "cpu_percent": psutil.cpu_percent(interval=None),
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


# ── Channels (the live source-channel list) ─────
#
# /tmp/feeder_channels.json is the actual runtime source of truth: both bots
# read it, and telegram_bot.py hot-reloads it every ~20s (see
# channels_reload_task() in telegram_bot.py) so edits here take effect without
# a restart. config.py's SOURCE_CHANNELS/PERSIAN_CHANNELS/SOURCE_NAMES are only
# the seed used the very first time a bot runs before this file exists — the
# WebUI intentionally never touches those anymore, to avoid two disagreeing
# sources of truth.

def read_channels() -> dict:
    if CHANNELS_FILE.exists():
        try:
            with open(CHANNELS_FILE) as f:
                data = json.load(f)
            return {
                "channels": data.get("channels", []),
                "persian": set(data.get("persian", [])),
                "names": data.get("names", {}),
            }
        except Exception:
            pass
    cfg = read_config()
    return {
        "channels": cfg.get("SOURCE_CHANNELS", []),
        "persian": set(),
        "names": cfg.get("SOURCE_NAMES", {}),
    }


def write_channels(data: dict) -> None:
    tmp = str(CHANNELS_FILE) + ".tmp"
    payload = {
        "channels": data["channels"],
        "persian": list(data["persian"]),
        "names": data["names"],
    }
    with open(tmp, "w") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, CHANNELS_FILE)


def channels_public() -> list[dict]:
    d = read_channels()
    return [
        {"handle": ch, "name": d["names"].get(ch, ""), "persian": ch in d["persian"]}
        for ch in d["channels"]
    ]


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


@app.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request):
    check_auth(request)
    return templates.TemplateResponse(request, "channels.html", {
        "channels": channels_public(),
    })


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
                      model: str = Form(""),
                      api_key: str = Form(""), dedup_ttl_seconds: str = Form("")):
    check_auth(request)
    try:
        try:
            with open(ai_filter.SETTINGS_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {}
        if model.strip():
            data["model"] = model.strip()
        # Only overwrite the key if a real (non-masked) value was provided.
        if api_key.strip() and not api_key.strip().startswith("•••…"):
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


def _norm_handle(raw: str) -> str:
    h = raw.strip()
    if not h:
        raise ValueError("channel handle required")
    return h if h.startswith("@") else f"@{h}"


@app.get("/api/channels")
def api_channels(request: Request):
    check_auth(request)
    return JSONResponse({"channels": channels_public()})


@app.post("/api/channels/add")
async def api_channels_add(request: Request, handle: str = Form(...),
                           name: str = Form(""), persian: str = Form("false")):
    check_auth(request)
    try:
        ch = _norm_handle(handle)
        d = read_channels()
        if ch not in d["channels"]:
            d["channels"].append(ch)
        d["names"][ch] = name.strip() or ch
        is_fa = str(persian).lower() in ("1", "true", "on", "yes")
        if is_fa:
            d["persian"].add(ch)
        else:
            d["persian"].discard(ch)
        write_channels(d)
        return JSONResponse({"ok": True, "channels": channels_public()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/channels/edit")
async def api_channels_edit(request: Request, handle: str = Form(...),
                            name: str = Form(""), persian: str = Form("false")):
    check_auth(request)
    try:
        ch = _norm_handle(handle)
        d = read_channels()
        if ch not in d["channels"]:
            return JSONResponse({"ok": False, "error": f"{ch} not found"}, status_code=404)
        d["names"][ch] = name.strip() or ch
        is_fa = str(persian).lower() in ("1", "true", "on", "yes")
        if is_fa:
            d["persian"].add(ch)
        else:
            d["persian"].discard(ch)
        write_channels(d)
        return JSONResponse({"ok": True, "channels": channels_public()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/channels/remove")
async def api_channels_remove(request: Request, handle: str = Form(...)):
    check_auth(request)
    try:
        ch = _norm_handle(handle)
        d = read_channels()
        if ch in d["channels"]:
            d["channels"].remove(ch)
        d["names"].pop(ch, None)
        d["persian"].discard(ch)
        write_channels(d)
        return JSONResponse({"ok": True, "channels": channels_public()})
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
    out["api_key_masked"] = ("•••…" + key[-4:]) if len(key) > 4 else ""
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


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MOSBAT_WEBUI_PORT", "5678"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
