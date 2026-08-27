# mosbat WebUI

A self-contained control panel for the mosbat news-forwarder bots
(`telegram_bot.py`, `bale_bot.py`).

## Access

- URL: `http://<this-server>:5678`
- Login password: `funlife`
- Runs as a systemd **user** service — auto-starts on boot (linger enabled).

## What you can do from the WebUI

- **Dashboard** (`/`): live CPU / memory / disk / load / uptime, queue depth,
  and per-bot status with Start / Stop / Restart (per-bot and "all").
  Auto-refreshes every 2.5s.
- **Logs** (`/logs`): live tail of `telegram_bot.log` and `bale_bot.log`
  with a "follow" toggle.
- **Config** (`/config`): read-only view of `config.py` (channels, tokens
  masked).

## Architecture

```
mosbat-webui.service  ──►  webui/app.py   (FastAPI + uvicorn, :5678)
mosbat-master.service ──►  webui/mosbat_master.py  (supervisor)
                                 ├── telegram_bot.py
                                 └── bale_bot.py
```

The WebUI talks to the supervisor via `webui/master_control.json`
(issued commands) and `webui/master_status.json` (published every 1s).
Bots are auto-restarted on crash unless intentionally stopped, and a bot
that fails because it needs a Telegram login is flagged `NEEDS LOGIN`
instead of crash-looping.

## systemctl commands

```bash
systemctl --user status  mosbat-webui mosbat-master
systemctl --user restart mosbat-webui
systemctl --user stop    mosbat-master
journalctl --user -u mosbat-webui -f
```

## Completing bot login (one-time, interactive)

Both bots use Telethon user-sessions that must be created by logging in
with a real Telegram account. The WebUI shows `NEEDS LOGIN` until you do this.

From the project directory (needs an interactive terminal):

```bash
source .venv/bin/activate
python bale_login.py          # creates forwarder_bale_dl.session  (for bale_bot)
# telegram_bot.py uses forwarder_user / forwarder_bot sessions:
python telegram_bot.py        # first run prompts for phone + code, then Ctrl-C
```

After both sessions exist, press **Restart** (or **Start all**) in the
WebUI and the bots will stay up.

## Files

- `webui/app.py` — FastAPI app (auth, control API, live log streaming)
- `webui/mosbat_master.py` — process supervisor
- `webui/state.py` — status reader
- `webui/templates/`, `webui/static/` — UI
