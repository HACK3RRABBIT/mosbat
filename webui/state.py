"""
mosbat WebUI — process + state management
Watches the two bot processes (telegram_bot.py, bale_bot.py) via the
mosbat-master supervisor, and exposes their status to the API/templates.
"""
from __future__ import annotations

import os
import json
import time
import threading

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(PROJ, "webui", "mosbat_master.py")
VENV_PY = os.path.join(PROJ, ".venv", "bin", "python")

STATE_FILE = os.path.join(PROJ, "webui", "webui_state.json")
_lock = threading.Lock()
_state = {"sudo_password": "", "authed": False}


def load_state():
    global _state
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        with _lock:
            _state["sudo_password"] = data.get("sudo_password", "")
    except Exception:
        pass


def save_state():
    with _lock:
        data = {"sudo_password": _state["sudo_password"]}
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def set_password(pw: str):
    with _lock:
        _state["sudo_password"] = pw
    save_state()


def get_password() -> str:
    with _lock:
        return _state["sudo_password"]


def set_authed(v: bool):
    with _lock:
        _state["authed"] = v


def is_authed() -> bool:
    with _lock:
        return _state["authed"]


# ── Live process status (read from master's status file) ──

def _read_master_status() -> dict:
    """Read the JSON status published by mosbat_master.py."""
    path = os.path.join(PROJ, "webui", "master_status.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def get_bots() -> list[dict]:
    st = _read_master_status().get("bots", {})
    order = ["telegram_bot", "bale_bot"]
    out = []
    for name in order:
        info = st.get(name, {})
        out.append({
            "name": name,
            "script": f"{name}.py",
            "running": bool(info.get("running")),
            "intended": info.get("intended", True),
            "needs_login": bool(info.get("needs_login")),
            "pid": info.get("pid"),
            "exit_code": info.get("exit_code"),
            "last_log": info.get("last_log", ""),
        })
    return out


def master_running() -> bool:
    st = _read_master_status()
    return bool(st.get("master_pid")) and os.path.exists(
        f"/proc/{st.get('master_pid')}")


def master_pid() -> int | None:
    return _read_master_status().get("master_pid")
