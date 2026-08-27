#!/usr/bin/env python3
"""
mosbat_master.py — supervisor for the mosbat bots.

Spawns telegram_bot.py and bale_bot.py, restarts them if they die
(unless stopped intentionally), and publishes live status to
webui/master_status.json every second.

Special handling: if a bot dies immediately because it needs an
interactive Telegram login (EOFError / "enter your phone" prompt with no
TTY), it is marked "needs_login" and NOT restarted on a loop — the WebUI
shows a clear "needs login" badge instead of crash-thrashing.

Controlled via the WebUI (start/stop/restart) through a small control file.
Run by the systemd user service  mosbat-master.service
"""
from __future__ import annotations

import os
import sys
import json
import time
import signal
import subprocess
import threading

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(PROJ, ".venv", "bin", "python")
STATUS_FILE = os.path.join(PROJ, "webui", "master_status.json")
CONTROL_FILE = os.path.join(PROJ, "webui", "master_control.json")

BOTS = ["telegram_bot", "bale_bot"]
CHECK_EVERY = 1.0
RESTART_DELAY = 4.0
# If a bot exits faster than this, assume a startup/auth failure
FAST_EXIT = 8.0

procs: dict[str, subprocess.Popen | None] = {b: None for b in BOTS}
last_logs: dict[str, str] = {b: "" for b in BOTS}
stop_flags: dict[str, bool] = {b: False for b in BOTS}   # stay stopped
needs_login: dict[str, bool] = {b: False for b in BOTS}  # auth failure, don't loop
exit_codes: dict[str, int | None] = {b: None for b in BOTS}
start_times: dict[str, float] = {b: 0.0 for b in BOTS}
_lock = threading.Lock()


def _now() -> int:
    return int(time.time())


def write_status():
    snap: dict[str, object] = {
        "master_pid": os.getpid(),
        "ts": _now(),
        "bots": {},
    }
    with _lock:
        for b in BOTS:
            p = procs[b]
            running = p is not None and p.poll() is None
            info = {
                "running": running,
                "pid": p.pid if running else None,
                "intended": (not stop_flags[b]),
                "needs_login": needs_login[b],
                "exit_code": exit_codes[b],
                "last_log": last_logs[b][-300:],
            }
            snap["bots"][b] = info
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, STATUS_FILE)
    except Exception as e:
        sys.stderr.write(f"status write error: {e}\n")


def read_control() -> dict:
    try:
        with open(CONTROL_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def start_bot(name: str):
    global procs, start_times
    script = os.path.join(PROJ, f"{name}.py")
    with _lock:
        needs_login[name] = False
        stop_flags[name] = False
        start_times[name] = time.time()
    p = subprocess.Popen(
        [VENV_PY, script],
        cwd=PROJ,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    with _lock:
        procs[name] = p
    threading.Thread(target=reader, args=(name, p), daemon=True).start()


def reader(name: str, p: subprocess.Popen):
    assert p.stdout is not None
    buf = []
    for line in p.stdout:
        line = line.rstrip("\n")
        last_logs[name] = line
        buf.append(line)
    _ = p.wait()
    rc = p.returncode
    with _lock:
        exit_codes[name] = rc
        # Detect "needs login" crashes: quick exit with a phone prompt / EOF
        joined = "\n".join(buf[-12:])
        if (time.time() - start_times[name] < FAST_EXIT) and (
            "enter your phone" in joined.lower()
            or "EOFError" in joined
            or "eof when reading" in joined.lower()
        ):
            needs_login[name] = True
            stop_flags[name] = True  # do not auto-restart-loop


def stop_bot(name: str):
    global procs
    with _lock:
        p = procs.get(name)
    if p and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=10)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    with _lock:
        procs[name] = None


def handle_control():
    ctrl = read_control()
    if not ctrl:
        return
    action = ctrl.get("action")
    target = ctrl.get("target")  # bot name or "all"
    targets = BOTS if target in (None, "all") else [target]

    if action == "start":
        for b in targets:
            with _lock:
                needs_login[b] = False
            if not stop_flags[b]:
                stop_bot(b)
            start_bot(b)
    elif action == "stop":
        for b in targets:
            stop_flags[b] = True
            stop_bot(b)
    elif action == "restart":
        for b in targets:
            with _lock:
                needs_login[b] = False
            stop_bot(b)
            time.sleep(0.5)
            start_bot(b)

    # clear control so it isn't re-applied
    try:
        os.remove(CONTROL_FILE)
    except Exception:
        pass


def session_present(name: str) -> bool:
    """Does the bot have the Telegram session file it needs?"""
    return os.path.exists(os.path.join(PROJ, f"{name}.session"))


def derive_needs_login() -> None:
    """Self-correct the needs_login flag from reality: a bot only needs login
    if its required session file is missing. Once the file exists (e.g. created
    via the WebUI login flow), the flag clears and the bot becomes startable —
    no manual Start required."""
    for b in BOTS:
        with _lock:
            if not needs_login[b]:
                continue
            if session_present(b):
                needs_login[b] = False
                stop_flags[b] = False


def main():
    # initial start of all bots
    for b in BOTS:
        start_bot(b)

    try:
        while True:
            handle_control()
            derive_needs_login()
            # auto-restart bots that died but are intended to run
            # and are NOT stuck on login
            for b in BOTS:
                with _lock:
                    p = procs.get(b)
                    intent = not stop_flags[b]
                    login = needs_login[b]
                if (p is None or p.poll() is not None) and intent and not login:
                    time.sleep(RESTART_DELAY)
                    start_bot(b)
            write_status()
            time.sleep(CHECK_EVERY)
    except KeyboardInterrupt:
        pass
    finally:
        for b in BOTS:
            stop_bot(b)
        write_status()


if __name__ == "__main__":
    main()
