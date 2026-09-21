"""
webui/login_flow.py — web-driven Telegram login for mosbat.

Telegram userbot sessions (forwarder_user, forwarder_bot, forwarder_bale_dl)
normally require an interactive terminal. This module drives the same login
flow from the WebUI by persisting state to disk between HTTP requests:

    Step 0  idle
    Step 1  ask for phone  -> send_code  (Telegram delivers a code to the app)
    Step 2  ask for code   -> sign_in    (-> password if 2FA enabled)
    Step 3  ask for 2FA    -> sign_in(password)
    Step 4  done           -> forwarder_user.session written

Telethon's client methods are coroutines, so they are run on a dedicated
background event loop (one per login attempt) via the _run() helper. State
(phone, step, errors, whether a code was actually sent) lives in
webui/login_state.json so the multi-step exchange survives between requests.
"""
from __future__ import annotations

import os
import json
import time
import asyncio
import threading

from telethon import TelegramClient

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(PROJ, "webui", "login_state.json")
CONFIG_FILE = os.path.join(PROJ, "config.py")

_lock = threading.Lock()
_client: TelegramClient | None = None
_loop: asyncio.AbstractEventLoop | None = None
_sessions = {"phone": None, "step": "idle", "error": None,
             "needs": [], "done": False, "code_sent": False}


def _read_config() -> dict:
    out = {}
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("cfg", CONFIG_FILE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out["API_ID"] = getattr(mod, "API_ID", None)
        out["API_HASH"] = getattr(mod, "API_HASH", None)
    except Exception:
        pass
    return out


def load_state():
    global _sessions
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        with _lock:
            _sessions.update(data)
    except Exception:
        pass


def save_state():
    with _lock:
        data = dict(_sessions)
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def session_exists(name: str) -> bool:
    return os.path.exists(os.path.join(PROJ, f"{name}.session"))


def facts() -> dict:
    cfg = _read_config()
    return {
        "api_id": cfg.get("API_ID"),
        "api_hash": bool(cfg.get("API_HASH")),
        "tg_user": session_exists("forwarder_user"),
        "tg_bot": session_exists("forwarder_bot"),
        "bale_dl": session_exists("forwarder_bale_dl"),
    }


def status() -> dict:
    with _lock:
        s = dict(_sessions)
    s["active"] = _client is not None and _client.is_connected()
    s["config"] = facts()
    return s


def _api_creds():
    cfg = _read_config()
    if not cfg.get("API_ID") or not cfg.get("API_HASH"):
        raise RuntimeError("API_ID / API_HASH missing from config.py")
    return cfg["API_ID"], cfg["API_HASH"]


def reset():
    global _client, _loop, _thread
    with _lock:
        if _client is not None and _loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(_client.disconnect(), _loop).result(5)
            except Exception:
                pass
        _stop_loop()
        _client = None
        _loop = None
        _thread = None
        _sessions.update(phone=None, step="idle", error=None,
                         needs=[], done=False, code_sent=False)
    save_state()


# The Telethon client runs in its OWN daemon thread with its OWN event loop.
# FastAPI/uvicorn already owns the request thread's loop, so running the
# client's loop on the request thread throws "loop already running". We bridge
# coroutines across threads with run_coroutine_threadsafe.
_thread: threading.Thread | None = None


def _stop_loop():
    global _loop
    if _loop is not None:
        try:
            _loop.call_soon_threadsafe(_loop.stop)
        except Exception:
            pass
        # give the loop thread a moment to exit
        if _thread is not None:
            _thread.join(timeout=3)


def _ensure_client(phone_session: str = "forwarder_user"):
    """Create the client + a dedicated loop running in a daemon thread."""
    global _client, _loop, _thread
    with _lock:
        if _client is not None and _loop is not None:
            return _client
        api_id, api_hash = _api_creds()
        _loop = asyncio.new_event_loop()

        def _run_loop():
            asyncio.set_event_loop(_loop)
            _loop.run_forever()

        _thread = threading.Thread(target=_run_loop, daemon=True)
        _thread.start()
        _client = TelegramClient(
            os.path.join(PROJ, phone_session), api_id, api_hash, loop=_loop,
        )
        return _client


def _run(coro, timeout: float = 30):
    """Run a coroutine on the client's thread loop, thread-safe."""
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout)


def start_phone(phone: str) -> dict:
    """Step 1: connect and send a login code to the phone number."""
    global _client, _loop
    phone = phone.strip()
    if not phone:
        raise ValueError("Phone number required")
    if not phone.startswith("+"):
        raise ValueError("Use international format, e.g. +98912...")
    _ensure_client("forwarder_user")
    # Connect and request the code — both MUST be awaited on the client loop.
    _run(_client.connect())
    if not _client.is_connected():
        raise RuntimeError("Could not connect to Telegram")
    sent = _run(_client.send_code_request(phone))
    hash_val = getattr(sent, "phone_code_hash", None)
    if not hash_val:
        raise RuntimeError("Telegram did not return a code hash — code not sent")
    with _lock:
        _sessions.update(
            phone=phone, step="code", error=None,
            needs=["code"], done=False, code_sent=True,
            phone_code_hash=hash_val,
        )
    save_state()
    return status()


def submit_code(code: str) -> dict:
    """Step 2: submit the code. May request 2FA password."""
    global _client
    code = code.strip()
    if not code:
        raise ValueError("Code required")
    with _lock:
        if _client is None:
            raise RuntimeError("No login in progress — start over")
        phone = _sessions["phone"]
    try:
        _run(_client.sign_in(phone, code))
        with _lock:
            _sessions.update(step="done", done=True, needs=[], error=None, code_sent=False)
        save_state()
        _disconnect()
        return status()
    except Exception as e:
        msg = str(e)
        if "password" in msg.lower() or "two-step" in msg.lower() or "2fa" in msg.lower():
            with _lock:
                _sessions.update(step="password", needs=["password"], error=None)
            save_state()
            return status()
        with _lock:
            _sessions.update(error=msg)
        save_state()
        raise


def submit_password(password: str) -> dict:
    """Step 3: submit 2FA password."""
    global _client
    with _lock:
        if _client is None:
            raise RuntimeError("No login in progress — start over")
    try:
        _run(_client.sign_in(password=password))
        with _lock:
            _sessions.update(step="done", done=True, needs=[], error=None, code_sent=False)
        save_state()
        _disconnect()
        return status()
    except Exception as e:
        msg = str(e)
        with _lock:
            _sessions.update(error=msg)
        save_state()
        raise


def _disconnect():
    global _client, _loop
    # Disconnect but keep the loop alive so saved session file is flushed.
    if _client is not None:
        try:
            _run(_client.disconnect())
        except Exception:
            pass
        _client = None
    if _loop is not None:
        try:
            _loop.close()
        except Exception:
            pass
        _loop = None


def create_bale_dl_session() -> bool:
    """Build forwarder_bale_dl.session from the logged-in user session.

    forwarder_bale_dl is a SEPARATE session file, so calling start() would
    prompt for login again. Instead we copy the already-authorized
    forwarder_user session FILE into forwarder_bale_dl.session — same account,
    no second code/2FA prompt. A plain file copy reads through SQLite's lock,
    so this works even while telegram_bot has the source session open.
    """
    import shutil
    api_id, api_hash = _api_creds()
    src = os.path.join(PROJ, "forwarder_user.session")
    dst = os.path.join(PROJ, "forwarder_bale_dl.session")
    if not os.path.exists(src):
        return False

    # Copy with retries to tolerate a brief write lock on the source.
    last_err = None
    for attempt in range(10):
        try:
            shutil.copyfile(src, dst)
            break
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    else:
        with _lock:
            _sessions["error"] = f"bale_dl copy: {last_err}"
        save_state()
        return False

    # Verify the copy is actually authorized.
    loop = asyncio.new_event_loop()
    c = TelegramClient(os.path.join(PROJ, "forwarder_bale_dl"), api_id, api_hash, loop=loop)
    try:
        me = loop.run_until_complete(_verify(c))
        if me is None:
            # copy didn't carry auth; remove the broken file
            try:
                os.remove(dst)
            except Exception:
                pass
        return me is not None
    except Exception as e:
        with _lock:
            _sessions["error"] = f"bale_dl: {e}"
        save_state()
        return False
    finally:
        try:
            loop.close()
        except Exception:
            pass


async def _verify(c):
    try:
        await c.connect()
        me = await c.get_me()
        return me
    finally:
        try:
            await c.disconnect()
        except Exception:
            pass


load_state()
