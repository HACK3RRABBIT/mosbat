# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`mosbat` (formerly `mosbateiran`) is a news-forwarding pipeline: it reads posts from a
list of Telegram source channels, translates/cleans/filters them, and relays the
result to a Telegram channel and a Bale channel, with an optional human-approval
step and an LLM-based news/dedup gate. A FastAPI WebUI provides a control panel for
both bots plus a supervisor process that keeps them alive.

## Running

No `requirements.txt` is committed (it's gitignored on purpose); install deps ad hoc
into a venv at `.venv` — the supervisor and systemd units assume `.venv/bin/python`:

```bash
python -m venv .venv
source .venv/bin/activate
pip install telethon deep-translator requests fastapi uvicorn httpx jinja2 python-multipart
```

Run bots directly (each needs a Telethon session — first run prompts for phone/code
interactively):

```bash
python telegram_bot.py     # creates forwarder_user.session, forwarder_bot.session
python bale_login.py       # creates forwarder_bale_dl.session (bale_bot's TG download client)
python bale_bot.py
```

Or run everything through the supervisor (what production/systemd actually does):

```bash
python webui/mosbat_master.py   # spawns + restarts telegram_bot.py and bale_bot.py
python webui/app.py             # or via uvicorn; FastAPI control panel on :5678
```

In production these run as systemd **user** services (`mosbat-webui`,
`mosbat-master`); see `webui/README.md` for the systemctl commands and the
one-time interactive login flow.

There is no test suite — `test_bale.py` is a standalone manual script that pokes
the Bale HTTP API directly (`python test_bale.py`), not a pytest file.

## Architecture

**Two independent forwarder bots share state only through files in `/tmp`**, not
through in-process calls or a message queue library:

- `telegram_bot.py` (Telethon: a userbot `forwarder_user` for reading source
  channels/sending to the target channel, plus a bot account `forwarder_bot` for
  the admin approval UI) is the intake side. It listens to `SOURCE_CHANNELS`,
  cleans/translates each message (`shared.py`), runs it through the caption-less
  media gate and the AI news gate (`ai_filter.py`), and — regardless of whether TG
  itself is active — appends a record to `/tmp/feeder_queue.jsonl`. That queue file
  is the sole handoff to `bale_bot.py`.
- `bale_bot.py` tails `/tmp/feeder_queue.jsonl`, re-downloads media straight from
  Telegram via its own Telethon client (`forwarder_bale_dl`) rather than passing
  bytes between processes, and posts to Bale via plain HTTP (`tapi.bale.ai`) with
  its own manual/auto approval flow.
- Runtime-editable source-channel list lives in `/tmp/feeder_channels.json`
  (written by `telegram_bot.py`'s `/addchannel`/`/removechannel` commands, read by
  both bots) — `config.py`'s `SOURCE_CHANNELS`/`PERSIAN_CHANNELS`/`SOURCE_NAMES`
  are only the defaults used to seed it.
- `telegram_bot.py` persists its own on/off state to `/tmp/feeder_tg_state.json`
  (`tg_active`, `tg_publish`) so a restart doesn't silently flip the bot back on.

**`ai_filter.py` is a single-call LLM gate** invoked from `telegram_bot.py` before
anything is queued (queuing is shared by both bots, so this is the one interposition
point). One LLM call does three jobs at once: drop non-news/ads, return a canonical
`topic` string used for Jaccard-similarity dedup against `ai_dedup_store.jsonl`
(TTL + max-history bounded), and get logged for the WebUI. It is fail-open (LLM
error/timeout → relay, never silently drop real news) and runs via
`asyncio.to_thread` + `wait_for` specifically so a hung LLM call can't block
Telethon's event loop and stall intake — this was a real production incident, see
the comments around `intake_watchdog()` in `telegram_bot.py`. Settings
(`ai_settings.json`) are hot-reloaded by mtime so the WebUI can flip them without
restarting the bot. There is also a separate, non-LLM "media gate"
(`media_gate_check`) that deterministically drops captionless photos/videos/albums
before the LLM is even called.

**`webui/` is a self-contained FastAPI app + supervisor, decoupled from the bots
via more `/tmp`-adjacent JSON files** (this time under `webui/`, not `/tmp`):
`webui/mosbat_master.py` is a long-running supervisor that spawns
`telegram_bot.py`/`bale_bot.py` as subprocesses, restarts them on crash, and
detects a bot that died because it needs an interactive Telegram login
(`needs_login`, distinguished from a real crash by exit speed + stderr content) so
it doesn't crash-loop — it publishes status once/sec to `webui/master_status.json`.
`webui/app.py` (FastAPI, port 5678, single shared-password cookie auth) never
touches the bot processes directly; it writes intents to
`webui/master_control.json` and reads back `master_status.json`. `webui/state.py`
is just the read side of that status file. `webui/login_flow.py` drives the
interactive Telethon login (phone → code → 2FA password) from the WebUI so a human
never needs a real terminal to complete it.

**`shared.py`** holds everything both bots need for message shaping: emoji/URL/
promo-line stripping (`clean_text`), Google-Translate-to-Persian (skipped for
sources already in `PERSIAN_CHANNELS`), blockquote extraction/formatting, and
media-type detection (single photo/video vs. albums vs. generic file) that drives
which Telethon `send_file` path each bot takes.

`config.py` is the single source of truth for both bots' credentials, admin ID
lists, and channel lists — it's committed to the repo (not gitignored), so tokens
in it are effectively already exposed to anyone with repo access.
