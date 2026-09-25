"""
Telegram Channel Forwarder Bot
Requirements: pip install telethon deep-translator
Run: python telegram_bot.py
"""

import asyncio
import logging
import io
import gc
import json
import os
import time as _time
from logging.handlers import RotatingFileHandler

from telethon import TelegramClient, events, Button, utils
from telethon.tl.types import MessageMediaWebPage, PeerChannel
from telethon.tl.functions.channels import JoinChannelRequest

from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS, TG_MAIN_ADMIN, TARGET_CHANNEL,
    SOURCE_CHANNELS, PERSIAN_CHANNELS, SOURCE_NAMES, TG_FOOTER,
)
from shared import (
    translate, clean_text, has_link, detect_media_type, media_emoji,
    make_key, has_blockquote, extract_blockquote_and_rest,
    build_caption, build_quote_caption, get_doc_mime,
)
import ai_filter

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        RotatingFileHandler("telegram_bot.log", maxBytes=5 * 1024 * 1024, backupCount=2),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

# Hard cap on media buffered into RAM per message — a userbot on a 900MB box
# has no business holding an unbounded video in memory. Mirrors BALE_MAX_VIDEO_BYTES.
MAX_MEDIA_BYTES = 50 * 1024 * 1024

userbot = TelegramClient("forwarder_user", API_ID, API_HASH)
bot     = TelegramClient("forwarder_bot",  API_ID, API_HASH)

# Boot timestamp — the intake watchdog uses this to ignore stale on-disk
# telemetry from a previous run (so it can't think intake is frozen at boot).
_BOT_STARTED_AT = _time.time()

mode             = "manual"
tg_publish       = True   # True = send to Telegram channel, False = skip
tg_active        = True   # False = completely silent (no approvals, no TG posts), queue still written for Bale
pending          = {}
approved         = {}
sent_map         = {}
album_buffer     = {}
album_timers     = {}
edit_pending     = {}
sent_keys        = set()
join_pending     = {}   # key -> channel handle, for the "join this channel?" Yes/Skip buttons

# Runtime-editable source channels (loaded from config, can be changed via commands
# or the WebUI). active_channel_ids is the resolved-entity-id mirror of
# active_channels that the live event filter actually uses — see the note above
# on_new_message() for why this indirection exists.
active_channels  = list(SOURCE_CHANNELS)
active_persian   = set(PERSIAN_CHANNELS)
active_names     = dict(SOURCE_NAMES)
active_channel_ids = set()   # resolved Telethon entity ids for active_channels
_channels_mtime  = -1.0

QUEUE_FILE      = "/tmp/feeder_queue.jsonl"
MAX_QUEUE_LINES = 200
CHANNELS_FILE   = "/tmp/feeder_channels.json"  # shared with bale_bot
STATE_FILE      = "/tmp/feeder_tg_state.json"
_queue_write_count = 0
QUEUE_TRIM_EVERY   = 20  # only re-read+trim the queue file every N writes

PROJ_DIR        = os.path.dirname(os.path.abspath(__file__))


# ── Admins (hot-reloaded from admins.json, edited from the WebUI) ──
# config.py's ADMIN_IDS / TG_MAIN_ADMIN only seed this the first time. Command
# handlers check admin_ids live via func= rather than from_users=, because
# Telethon resolves from_users= once at first dispatch and never again — an
# admin added later would be silently ignored, the same trap as chats=.

ADMINS_FILE     = os.path.join(PROJ_DIR, "admins.json")
admin_ids       = set(ADMIN_IDS) | {TG_MAIN_ADMIN}
main_admin_id   = TG_MAIN_ADMIN
_admins_mtime   = -1.0


def _maybe_reload_admins():
    global admin_ids, main_admin_id, _admins_mtime
    try:
        mtime = os.path.getmtime(ADMINS_FILE)
    except OSError:
        return
    if mtime == _admins_mtime:
        return
    try:
        with open(ADMINS_FILE) as f:
            data = json.load(f)
        ids = {int(x) for x in data.get("admins", [])}
        main = int(data.get("main_admin") or 0)
        if ids:
            admin_ids = ids | ({main} if main else set())
            main_admin_id = main or next(iter(ids))
        _admins_mtime = mtime
        log.info("Admins loaded: %s (main=%s)", sorted(admin_ids), main_admin_id)
    except Exception as e:
        log.warning("Could not load %s: %s", ADMINS_FILE, e)


def current_admins() -> list:
    _maybe_reload_admins()
    return sorted(admin_ids)


def _is_admin(event) -> bool:
    _maybe_reload_admins()
    return event.sender_id in admin_ids


def _is_main_admin(event) -> bool:
    _maybe_reload_admins()
    return event.sender_id == main_admin_id


# ── Bot state persistence ─────────────────────

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"tg_active": tg_active, "tg_publish": tg_publish}, f)
    except Exception as e:
        log.warning("save_state failed: %s", e)

def load_state():
    global tg_active, tg_publish
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            tg_active  = data.get("tg_active", True)
            tg_publish = data.get("tg_publish", True)
            log.info("State loaded: tg_active=%s tg_publish=%s", tg_active, tg_publish)
        except Exception as e:
            log.warning("load_state failed: %s", e)


# ── Channel state persistence ─────────────────

def save_channels():
    try:
        with open(CHANNELS_FILE, "w") as f:
            json.dump({
                "channels": active_channels,
                "persian":  list(active_persian),
                "names":    active_names,
            }, f, ensure_ascii=False)
    except Exception as e:
        log.warning("save_channels failed: %s", e)

def load_channels():
    global active_channels, active_persian, active_names, _channels_mtime
    if os.path.exists(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE) as f:
                data = json.load(f)
            active_channels = data.get("channels", list(SOURCE_CHANNELS))
            active_persian  = set(data.get("persian", list(PERSIAN_CHANNELS)))
            active_names    = data.get("names", dict(SOURCE_NAMES))
            _channels_mtime = os.path.getmtime(CHANNELS_FILE)
            log.info("Loaded %d source channels from disk", len(active_channels))
        except Exception as e:
            log.warning("load_channels failed: %s", e)

def get_channel_list_text():
    lines = ["📋 **Source channels:**\n"]
    for ch in active_channels:
        name    = active_names.get(ch, "—")
        persian = "🇮🇷" if ch in active_persian else "🌐"
        lines.append(f"{persian} `{ch}` — {name}")
    return "\n".join(lines)


# ── Live channel-entity resolution ────────────
# events.NewMessage(chats=...) resolves its `chats` argument to a fixed set of
# entity ids ONCE, when the handler is registered — reassigning the
# active_channels list afterward (from /addchannel or a WebUI edit) has no
# effect on what the listener actually receives. So instead of a chats= filter,
# on_new_message()/on_edited_message() filter manually against
# active_channel_ids, which this function keeps in sync with active_channels.

CHANNEL_IDS_CACHE_FILE = os.path.join(PROJ_DIR, "channel_ids_cache.json")  # handle -> marked peer id


def _load_channel_id_cache() -> dict:
    try:
        with open(CHANNEL_IDS_CACHE_FILE) as f:
            cache = json.load(f)
    except Exception:
        return {}
    # Older entries stored a channel's raw (positive) id; convert to marked.
    return {h: (utils.get_peer_id(PeerChannel(v)) if v > 0 else v) for h, v in cache.items()}


def _save_channel_id_cache(cache: dict):
    try:
        tmp = CHANNEL_IDS_CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, CHANNEL_IDS_CACHE_FILE)
    except Exception as e:
        log.warning("Could not save channel id cache: %s", e)


async def sync_channel_ids():
    """(Re)resolve active_channels to Telethon entity ids, using a persistent
    cache so a routine restart never needs to re-hit Telegram's
    ResolveUsernameRequest for a channel it has already resolved before.

    Without this, every restart re-resolves all channels over the network —
    and enough of those in a short window trips Telegram's flood-wait
    protection, which then blocks intake for those channels for minutes (seen
    in production: a config change triggered a restart, which triggered a
    flood-wait, which meant most channels weren't monitored for a while).
    Never removes an id for a channel that fails to resolve (e.g. a transient
    network hiccup) unless it was actually dropped from active_channels.

    Two more things keep this from re-tripping that flood-wait once it clears:
    firing get_entity() for a dozen channels back-to-back with no spacing looks
    like abuse to Telegram, so freshly-resolved lookups are spaced out; and if
    a FloodWaitError actually happens, resolution stops immediately instead of
    hammering the remaining channels into the same wall (seen in production:
    retrying into an active flood-wait made the wait longer each time, not
    shorter — 97s -> 242s -> 392s -> 404s across repeated attempts).
    """
    from telethon.errors.rpcerrorlist import FloodWaitError
    global active_channel_ids
    cache = _load_channel_id_cache()
    resolved = set()
    from_cache = 0
    dirty = False
    first_fresh = True
    for ch in active_channels:
        cid = cache.get(ch)
        if cid is not None:
            resolved.add(cid)
            from_cache += 1
            continue
        if not first_fresh:
            await asyncio.sleep(1.5)
        first_fresh = False
        try:
            ent = await userbot.get_entity(ch)
            pid = utils.get_peer_id(ent)  # marked id — matches event.chat_id
            resolved.add(pid)
            cache[ch] = pid
            dirty = True
        except FloodWaitError as e:
            log.warning("Flood-wait resolving %s (%ss) — stopping channel "
                        "resolution for this run rather than hitting it again", ch, e.seconds)
            break
        except Exception as e:
            log.warning("Could not resolve channel %s: %s", ch, e)
    if dirty:
        _save_channel_id_cache(cache)
    active_channel_ids = resolved
    log.info("Resolved %d/%d source channels to live entity ids (%d from cache, %d freshly resolved)",
              len(resolved), len(active_channels), from_cache, len(resolved) - from_cache)

async def channels_reload_task(check_every: int = 20):
    """Hot-reload CHANNELS_FILE (written by /addchannel, /removechannel, or the
    WebUI) so channel management actually takes effect without a bot restart."""
    global _channels_mtime
    while True:
        await asyncio.sleep(check_every)
        try:
            if not os.path.exists(CHANNELS_FILE):
                continue
            mtime = os.path.getmtime(CHANNELS_FILE)
            if mtime == _channels_mtime:
                continue
            load_channels()
            await sync_channel_ids()
            await ensure_channels_joined()
        except Exception as e:
            log.warning("channels_reload_task error: %s", e)


# ── Join-request workflow ─────────────────────
# Resolving a public channel's username is not the same as being a member of
# it — and Telegram does not reliably push live update notifications for a
# channel the account hasn't joined, even though history/entity lookups still
# work. A channel that resolves fine but never produces any news is a likely
# symptom of exactly this. Per the user's explicit ask: never join silently —
# ask about each channel individually, and only join the ones approved.

JOIN_SKIPPED_FILE = os.path.join(PROJ_DIR, "join_skipped.json")  # handles the admin said "skip" for


def _load_join_skipped() -> set:
    try:
        with open(JOIN_SKIPPED_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_join_skipped(skipped: set):
    try:
        tmp = JOIN_SKIPPED_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(sorted(skipped), f, ensure_ascii=False)
        os.replace(tmp, JOIN_SKIPPED_FILE)
    except Exception as e:
        log.warning("Could not save join-skipped list: %s", e)


def join_buttons(key):
    return [[
        Button.inline("✅ Join", data=f"joinyes_{key}"),
        Button.inline("❌ Skip", data=f"joinno_{key}"),
    ]]


async def ensure_channels_joined():
    """Ask the admin, one channel at a time, before joining any configured
    source channel the userbot account isn't a member of yet.

    Checks membership with a single get_dialogs() pass rather than one
    request per channel — after the flood-wait incident with
    ResolveUsernameRequest, minimizing per-channel network calls here on
    purpose.
    """
    skipped = _load_join_skipped()
    cache = _load_channel_id_cache()
    try:
        joined_ids = {d.id async for d in userbot.iter_dialogs()}
    except Exception as e:
        log.warning("Could not list joined dialogs (skipping join-check this round): %s", e)
        return

    for ch in list(active_channels):
        if ch in skipped or ch in join_pending.values():
            continue
        cid = cache.get(ch)
        if cid is None or cid in joined_ids:
            continue  # not resolved yet, or already a member
        key = ch.lstrip("@")
        join_pending[key] = ch
        name = active_names.get(ch, ch)
        text = (
            f"📡 **Not joined yet:** `{ch}` ({name})\n\n"
            f"The account isn't a member of this channel, so live updates may "
            f"never arrive even though it resolves fine. Join it?"
        )
        sent_to_any = False
        for admin_id in current_admins():
            try:
                await bot.send_message(admin_id, text, buttons=join_buttons(key), parse_mode="markdown")
                sent_to_any = True
            except Exception as e:
                log.warning("Could not send join prompt for %s to admin %s: %s", ch, admin_id, e)
        if sent_to_any:
            log.info("Asked admin whether to join %s", ch)
        await asyncio.sleep(1.5)  # one at a time, not a burst of messages


# ── Queue writer ──────────────────────────────

def write_to_queue(entry: dict):
    global _queue_write_count
    try:
        source_ids = entry.get("source_ids", [])
        record = {
            "caption":         entry["caption"],
            "type":            entry["type"],
            "raw_text":        entry["raw_text"],
            "source_username": entry.get("source_username", ""),
            "ts":              _time.time(),
            "tg_chat_id":      source_ids[0][0] if source_ids else None,
            "tg_msg_ids":      [mid for _, mid in source_ids],
        }
        with open(QUEUE_FILE, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        # Trimming to MAX_QUEUE_LINES needs a full read+rewrite — too expensive
        # to do on every single message, so only check periodically. The file
        # can drift a bit above the cap between checks; that's fine, it's just
        # a rolling window for bale_bot's catch-up, not a hard limit.
        _queue_write_count += 1
        if _queue_write_count % QUEUE_TRIM_EVERY != 0:
            return
        with open(QUEUE_FILE, "r") as f:
            lines = [l for l in f.readlines() if l.strip()]
        if len(lines) > MAX_QUEUE_LINES:
            with open(QUEUE_FILE, "w") as f:
                f.writelines(lines[-MAX_QUEUE_LINES:])
    except Exception as e:
        log.warning("Queue write failed: %s", e)


# ── Download media ────────────────────────────

async def download_media_bytes(msg):
    try:
        return await userbot.download_media(msg, file=bytes)
    except Exception as e:
        log.warning("Could not download media msg %s: %s", msg.id, e)
        return None

async def prepare_entry(msgs, source_username=""):
    first   = msgs[0]
    raw     = next((m.message for m in msgs if m.message), "")
    mtype   = detect_media_type(msgs)
    emoji   = media_emoji(mtype)
    skip_tr = source_username in active_persian

    quote, rest = extract_blockquote_and_rest(first)
    caption = (
        await build_quote_caption(emoji, quote, rest, skip_translate=skip_tr)
        if quote else
        await build_caption(emoji, raw, skip_translate=skip_tr)
    )

    reply_to_msg_id = None
    if first.reply_to and hasattr(first.reply_to, "reply_to_msg_id"):
        src_key = make_key(first.chat_id, first.reply_to.reply_to_msg_id)
        reply_to_msg_id = sent_map.get(src_key)

    files = []
    for m in msgs:
        if m.media and not isinstance(m.media, MessageMediaWebPage):
            doc_size = getattr(getattr(m.media, "document", None), "size", 0) or 0
            if doc_size > MAX_MEDIA_BYTES:
                log.info("Skipping media msg %s: %.1fMB exceeds %.0fMB cap",
                         m.id, doc_size / 1e6, MAX_MEDIA_BYTES / 1e6)
                continue
            data = await download_media_bytes(m)
            if data:
                mime = ("image/jpeg"
                        if hasattr(m.media, "photo") and not hasattr(m.media, "document")
                        else get_doc_mime(m.media.document) if hasattr(m.media, "document") else "")
                files.append({"data": data, "mime": mime})

    return {
        "files":           files,
        "caption":         caption,
        "type":            mtype,
        "raw_text":        clean_text(raw),
        "source_ids":      [(m.chat_id, m.id) for m in msgs],
        "source_username": source_username,
        "reply_to_msg_id": reply_to_msg_id,
    }


# ── Send to Telegram channel ──────────────────

async def send_to_channel(entry) -> int | None:
    if not tg_publish:
        log.info("tg_publish=OFF — skipping channel send")
        return None

    caption      = entry["caption"]
    files        = entry["files"]
    mtype        = entry["type"]
    reply_msg_id = entry.get("reply_to_msg_id")
    kw           = dict(parse_mode="markdown")
    if reply_msg_id:
        kw["reply_to"] = reply_msg_id

    try:
        if mtype == "text" or not files:
            sent = await userbot.send_message(TARGET_CHANNEL, caption + TG_FOOTER, **kw)
            return sent.id

        bio = io.BytesIO(files[0]["data"])

        if mtype == "photo":
            bio.name = "photo.jpg"
            sent = await userbot.send_file(TARGET_CHANNEL, file=bio,
                                           caption=caption + TG_FOOTER,
                                           force_document=False, **kw)
        elif mtype == "video":
            bio.name = "video.mp4"
            sent = await userbot.send_file(TARGET_CHANNEL, file=bio,
                                           caption=caption + TG_FOOTER,
                                           force_document=False,
                                           supports_streaming=True, **kw)
        elif mtype in ("album_photo", "album_video"):
            bios = []
            for i, f in enumerate(files):
                b = io.BytesIO(f["data"])
                b.name = f"video_{i}.mp4" if f.get("mime","").startswith("video/") else f"photo_{i}.jpg"
                bios.append(b)
            sent = await userbot.send_file(TARGET_CHANNEL, file=bios,
                                           caption=caption + TG_FOOTER,
                                           force_document=False,
                                           supports_streaming=True, **kw)
            return sent[0].id if isinstance(sent, list) else sent.id
        else:
            bio.name = "file"
            sent = await userbot.send_file(TARGET_CHANNEL, file=bio,
                                           caption=caption + TG_FOOTER, **kw)
        return sent.id

    except Exception as e:
        log.error("Failed to send to channel: %s", e)
        return None
    finally:
        for f in entry.get("files", []):
            f["data"] = b""


# ── Approval buttons ──────────────────────────

def approval_buttons(key):
    return [[
        Button.inline("✅ Yes",  data=f"yes_{key}"),
        Button.inline("✏️ Edit", data=f"edit_{key}"),
        Button.inline("❌ No",   data=f"no_{key}"),
    ]]


# ── Process messages ──────────────────────────

async def process_msgs(msgs, source_username=""):
    global mode
    entry = await prepare_entry(msgs, source_username=source_username)
    if not entry:
        return

    # ── Caption-less media gate ──────────────
    # Photos, photo/video albums, standalone videos and documents with NO real
    # caption are dropped before queueing (the user wants only captioned media
    # relayed). Pure syntactic check — no LLM call.
    mtype = entry.get("type", "text")
    judge_text = entry.get("raw_text") or entry.get("caption") or ""
    dropped, reason = ai_filter.media_gate_check(mtype, judge_text)
    if dropped:
        log.info("Media gate dropped %s from %s (no caption)", mtype, source_username)
        for f in entry.get("files", []):
            f["data"] = b""
        return

    # ── AI news gate ──────────────────────────
    # Drop useless news / dedupe before anything is queued (the queue feeds
    # BOTH the Telegram approval flow and the Bale bot, so this is the only
    # place we need to interpose). Media-only posts with a caption reach here
    # and are judged on that caption's text.
    if ai_filter.is_enabled() and judge_text.strip():
        decision = await ai_filter.decide(judge_text, source_username)
        entry["judge_id"] = decision.get("id")  # links the admin's approve/reject to the dataset record
        if not decision["relay"]:
            log.info("AI filter dropped %s: %s — %s",
                     decision["dropped"], source_username, decision["reason"])
            # free media bytes, do not queue
            for f in entry.get("files", []):
                f["data"] = b""
            return

    write_to_queue(entry)  # always write — Bale bot reads this regardless of tg_active

    if not tg_active:
        # TG is stopped: free media bytes immediately and return
        for f in entry.get("files", []):
            f["data"] = b""
        return

    source_name = active_names.get(source_username, source_username or "ناشناس")

    if mode == "auto":
        sent_id = await send_to_channel(entry)
        if sent_id:
            for (chat_id, msg_id) in entry["source_ids"]:
                k = make_key(chat_id, msg_id)
                approved[k] = sent_id
                sent_map[k] = sent_id
            log.info("Auto-sent message to channel")
    else:
        key = make_key(msgs[0].chat_id, msgs[0].id)
        entry["ts"] = _time.time()
        pending[key] = entry

        mtype   = entry["type"]
        preview = entry["caption"].split("\n\n🇮🇷")[0].strip()[:400] or "*(no text)*"
        has_r   = "↩️" if entry.get("reply_to_msg_id") else ""
        has_q   = "💬" if any(has_blockquote(m) for m in msgs) else ""
        extras  = "  ".join(filter(None, [has_r, has_q]))
        tlabel  = {
            "text": "📝 Text", "photo": "📸 Photo", "video": "🎥 Video", "file": "📎 File",
            "album_photo": f"🖼 Album ({len(msgs)})", "album_video": f"🎥 Album ({len(msgs)})",
        }.get(mtype, "📎 Media")

        pub_status = "🟢 ON" if tg_publish else "🔴 OFF"
        text = (
            f"📨 **New message**\n"
            f"**Source:** {source_name}\n"
            f"**Type:** {tlabel}{('  ' + extras) if extras else ''}\n"
            f"**TG publish:** {pub_status}\n\n"
            f"**Preview:**\n{preview}"
        )
        sent_to_any = False
        for admin_id in current_admins():
            try:
                await bot.send_message(admin_id, text,
                                       buttons=approval_buttons(key),
                                       parse_mode="markdown")
                sent_to_any = True
            except Exception as e:
                # One admin's send failing (e.g. Telethon has no cached input
                # entity for an admin who has never messaged this bot token)
                # must never stop delivery to the others in this loop.
                log.error("Failed to send approval request to admin %s: %s", admin_id, e)
        if sent_to_any:
            log.info("Approval requested for message id=%s", msgs[0].id)


# ── Bot commands ──────────────────────────────

@bot.on(events.NewMessage(func=_is_admin, pattern="/start"))
async def cmd_start(event):
    label  = "🤖 Auto" if mode == "auto" else "👤 Manual"
    pub    = "🟢 ON" if tg_publish else "🔴 OFF"
    active = "🟢 RUNNING" if tg_active else "🛑 STOPPED"
    await event.respond(
        f"👋 **Feeder Bot**\n\n"
        f"TG bot: **{active}** | Mode: **{label}** | TG publish: **{pub}**\n\n"
        f"/mode — switch Auto/Manual\n"
        f"/status — current status\n"
        f"/tgstop — completely stop TG bot (Bale keeps running)\n"
        f"/tgstart — resume TG bot\n"
        f"/tgon — enable TG channel publish\n"
        f"/tgoff — disable TG channel publish\n"
        f"/channels — list source channels\n"
        f"/addchannel — add source channel\n"
        f"/removechannel — remove source channel",
        parse_mode="markdown"
    )

@bot.on(events.NewMessage(func=_is_admin, pattern="/status"))
async def cmd_status(event):
    label  = "🤖 Auto" if mode == "auto" else "👤 Manual"
    pub    = "🟢 ON" if tg_publish else "🔴 OFF"
    active = "🟢 RUNNING" if tg_active else "🛑 STOPPED"
    await event.respond(
        f"TG bot: **{active}** | Mode: **{label}** | TG publish: **{pub}**\n"
        f"Pending: {len(pending)}",
        parse_mode="markdown"
    )

@bot.on(events.NewMessage(func=_is_admin, pattern="/mode"))
async def cmd_mode(event):
    current = "🤖 Auto" if mode == "auto" else "👤 Manual"
    await event.respond(
        f"Current: **{current}**\n\nChoose:",
        buttons=[[
            Button.inline("🤖 Auto",   data="set_auto"),
            Button.inline("👤 Manual", data="set_manual"),
        ]],
        parse_mode="markdown"
    )

@bot.on(events.NewMessage(func=_is_main_admin, pattern="/tgon"))
async def cmd_tgon(event):
    global tg_publish
    tg_publish = True
    save_state()
    await event.respond("🟢 **TG channel publish: ON**", parse_mode="markdown")
    log.info("TG publish → ON")

@bot.on(events.NewMessage(func=_is_main_admin, pattern="/tgoff"))
async def cmd_tgoff(event):
    global tg_publish
    tg_publish = False
    save_state()
    await event.respond("🔴 **TG channel publish: OFF**\n_(messages still go to Bale)_", parse_mode="markdown")
    log.info("TG publish → OFF")

@bot.on(events.NewMessage(func=_is_main_admin, pattern="/tgstop"))
async def cmd_tgstop(event):
    global tg_active
    tg_active = False
    save_state()
    await event.respond(
        "🛑 **Telegram bot: STOPPED**\n"
        "_(no approvals, no channel posts — Bale still receives messages)_\n\n"
        "Use /tgstart to resume.",
        parse_mode="markdown"
    )
    log.info("TG bot → STOPPED")

@bot.on(events.NewMessage(func=_is_main_admin, pattern="/tgstart"))
async def cmd_tgstart(event):
    global tg_active
    tg_active = True
    save_state()
    pub = "🟢 ON" if tg_publish else "🔴 OFF"
    await event.respond(
        f"🟢 **Telegram bot: STARTED**\n"
        f"TG publish: **{pub}**",
        parse_mode="markdown"
    )
    log.info("TG bot → STARTED")

@bot.on(events.NewMessage(func=_is_main_admin, pattern="/channels"))
async def cmd_channels(event):
    await event.respond(get_channel_list_text(), parse_mode="markdown")

@bot.on(events.NewMessage(func=_is_main_admin, pattern=r"/addchannel (.+)"))
async def cmd_addchannel(event):
    # Format: /addchannel @username ChannelName [persian]
    parts = event.pattern_match.group(1).strip().split()
    if len(parts) < 2:
        await event.respond("Usage: `/addchannel @username ChannelName [persian]`\nExample: `/addchannel @BBCPersian بی‌بی‌سی persian`", parse_mode="markdown")
        return
    ch    = parts[0] if parts[0].startswith("@") else f"@{parts[0]}"
    name  = parts[1]
    is_fa = len(parts) > 2 and parts[2].lower() == "persian"

    if ch not in active_channels:
        active_channels.append(ch)
    active_names[ch] = name
    if is_fa:
        active_persian.add(ch)
    else:
        active_persian.discard(ch)
    save_channels()
    try:
        ent = await userbot.get_entity(ch)
        pid = utils.get_peer_id(ent)
        active_channel_ids.add(pid)
        cache = _load_channel_id_cache()
        cache[ch] = pid
        _save_channel_id_cache(cache)
    except Exception as e:
        log.warning("Could not resolve new channel %s: %s", ch, e)

    flag = "🇮🇷" if is_fa else "🌐"
    await event.respond(f"✅ Added: {flag} `{ch}` — {name}", parse_mode="markdown")
    log.info("Added channel: %s (%s)", ch, name)

@bot.on(events.NewMessage(func=_is_main_admin, pattern=r"/removechannel (.+)"))
async def cmd_removechannel(event):
    ch = event.pattern_match.group(1).strip()
    if not ch.startswith("@"):
        ch = f"@{ch}"
    if ch in active_channels:
        active_channels.remove(ch)
        active_names.pop(ch, None)
        active_persian.discard(ch)
        save_channels()
        await sync_channel_ids()  # recompute — simplest way to drop the right id
        await event.respond(f"✅ Removed: `{ch}`", parse_mode="markdown")
        log.info("Removed channel: %s", ch)
    else:
        await event.respond(f"⚠️ `{ch}` not in list.", parse_mode="markdown")

@bot.on(events.NewMessage(func=_is_admin))
async def handle_edit_reply(event):
    msg = event.message
    if not msg.reply_to:
        return
    replied_id = msg.reply_to.reply_to_msg_id
    for k, v in list(edit_pending.items()):
        if v.get("edit_msg_id") == replied_id:
            new_text = msg.text.strip()
            v["caption"] = new_text
            v["edited"] = True
            pending[k] = v
            edit_pending.pop(k)
            await bot.send_message(
                event.sender_id,
                f"📨 **Edited preview:**\n{new_text[:400]}",
                buttons=approval_buttons(k),
                parse_mode="markdown"
            )
            await event.delete()
            return

@bot.on(events.CallbackQuery())
async def on_button(event):
    if not _is_admin(event):
        return
    global mode
    data = event.data.decode()
    try:
        if data == "set_auto":
            mode = "auto"
            await event.edit("✅ **🤖 Auto** mode", parse_mode="markdown")

        elif data == "set_manual":
            mode = "manual"
            await event.edit("✅ **👤 Manual** mode", parse_mode="markdown")

        elif data.startswith("edit_"):
            key   = data[5:]
            entry = pending.get(key)
            if not entry:
                await event.edit("⚠️ Already handled.")
                return
            sent = await bot.send_message(
                event.sender_id,
                f"✏️ **Reply with your edited text:**\n\n{entry.get('caption', '')}",
                parse_mode="markdown"
            )
            edit_pending[key] = {**entry, "edit_msg_id": sent.id}
            await event.edit("✏️ Reply to the message below with new text.")

        elif data.startswith("yes_"):
            key = data[4:]
            if key in sent_keys:
                await event.edit("✅ Already sent.")
                return
            entry = pending.pop(key, None)
            edit_pending.pop(key, None)
            if not entry:
                await event.edit("⚠️ Already handled.")
                return
            sent_keys.add(key)
            if entry.get("edited"):
                ai_filter.record_label(entry.get("judge_id"), "approved_edited", entry.get("caption"))
            else:
                ai_filter.record_label(entry.get("judge_id"), "approved")
            if tg_publish:
                await event.edit("⏳ Sending...")
                sent_id = await send_to_channel(entry)
                if sent_id:
                    for (chat_id, msg_id) in entry["source_ids"]:
                        k = make_key(chat_id, msg_id)
                        approved[k] = sent_id
                        sent_map[k] = sent_id
                    await event.edit("✅ Sent to channel.")
                else:
                    sent_keys.discard(key)
                    await event.edit("⚠️ Failed to send.")
            else:
                await event.edit("✅ Approved _(TG publish is OFF — not sent to channel)_", parse_mode="markdown")

        elif data.startswith("no_"):
            key = data[3:]
            entry = pending.pop(key, None)
            edit_pending.pop(key, None)
            if entry:
                ai_filter.record_label(entry.get("judge_id"), "rejected")
            await event.edit("❌ Skipped.")

        elif data.startswith("joinyes_"):
            key = data[8:]
            ch = join_pending.pop(key, None)
            if not ch:
                await event.edit("⚠️ Already handled.")
                return
            await event.edit(f"⏳ Joining `{ch}`...", parse_mode="markdown")
            try:
                ent = await userbot.get_entity(ch)
                await userbot(JoinChannelRequest(ent))
                await event.edit(f"✅ Joined `{ch}`", parse_mode="markdown")
                log.info("Joined channel %s", ch)
            except Exception as e:
                await event.edit(f"⚠️ Failed to join `{ch}`: {e}", parse_mode="markdown")
                log.warning("Failed to join %s: %s", ch, e)

        elif data.startswith("joinno_"):
            key = data[7:]
            ch = join_pending.pop(key, None)
            if ch:
                skipped = _load_join_skipped()
                skipped.add(ch)
                _save_join_skipped(skipped)
            await event.edit(f"⏭️ Skipped `{ch or key}`", parse_mode="markdown")

    except Exception as e:
        log.error("Button handler error: %s", e)


# ── Userbot listeners ─────────────────────────

async def handle_album(grouped_id, source_username):
    msgs = album_buffer.pop(grouped_id, [])
    album_timers.pop(grouped_id, None)
    if not msgs:
        return
    msgs.sort(key=lambda m: m.id)
    if has_link(next((m.message for m in msgs if m.message), "")):
        return
    await process_msgs(msgs, source_username=source_username)

@userbot.on(events.NewMessage())
async def on_new_message(event):
    # No chats= filter here on purpose: Telethon resolves a chats= argument to a
    # fixed set of entity ids ONCE, at handler-registration time, so it could
    # never reflect channels added/removed later via /addchannel or the WebUI.
    # active_channel_ids is instead kept live by sync_channel_ids() /
    # channels_reload_task(), and checked here on every event.
    if event.chat_id not in active_channel_ids:
        return
    msg = event.message
    if has_link(msg.message or ""):
        log.info("Skipped message id=%s (link)", msg.id)
        return
    ai_filter.mark_received()  # telemetry: intake loop is alive
    chat = await event.get_chat()
    src  = f"@{chat.username}" if getattr(chat, "username", None) else ""

    if msg.grouped_id:
        gid = msg.grouped_id
        album_buffer.setdefault(gid, []).append(msg)
        if gid in album_timers:
            album_timers[gid].cancel()
        loop = asyncio.get_event_loop()
        album_timers[gid] = loop.call_later(
            2.0, lambda u=src: asyncio.ensure_future(handle_album(gid, u))
        )
        return
    await process_msgs([msg], source_username=src)

@userbot.on(events.MessageEdited())
async def on_edited_message(event):
    if event.chat_id not in active_channel_ids:
        return
    msg = event.message
    key = make_key(msg.chat_id, msg.id)
    if key not in approved:
        return
    chat    = await event.get_chat()
    src     = f"@{chat.username}" if getattr(chat, "username", None) else ""
    skip_tr = src in active_persian
    quote, rest = extract_blockquote_and_rest(msg)
    mtype   = detect_media_type([msg])
    emoji   = media_emoji(mtype)
    new_cap = (
        await build_quote_caption(emoji, quote, rest, skip_translate=skip_tr)
        if quote else
        await build_caption(emoji, msg.message or "", skip_translate=skip_tr)
    )
    try:
        await userbot.edit_message(TARGET_CHANNEL, approved[key],
                                   new_cap + TG_FOOTER, parse_mode="markdown")
        log.info("Updated channel message id=%s", approved[key])
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.error("Failed to edit: %s", e)


# ── Intake watchdog ─────────────────────────
# If Telegram updates stop arriving for a while while the userbot still claims
# to be connected, the connection is wedged (seen: a stalled HTTP call blocked
# the event loop, leaving a CLOSE-WAIT socket and no news). Force a reconnect so
# intake resumes without waiting for Telethon's own (slow/quiet) reconnect.

async def intake_watchdog(stale_after: int = 2700, check_every: int = 60):
    """Restart the bot if no message has arrived from any source channel for a
    long time while the userbot still thinks it's connected (a wedged
    connection — seen in production once).

    stale_after is deliberately long: a couple of quiet minutes on news
    channels is normal (especially overnight), and each restart wipes the
    in-memory approval queue. With a 120s threshold this killed the bot every
    time the news went quiet for two minutes. Reads the in-process timestamp
    that on_new_message() updates, not the stats file (which is only flushed
    on relays, so it lagged behind real intake)."""
    await asyncio.sleep(20)  # grace period on startup
    while True:
        await asyncio.sleep(check_every)
        last = max(ai_filter._last_received, _BOT_STARTED_AT)
        if (_time.time() - last) >= stale_after and userbot.is_connected():
            log.warning("Intake stale %.0fs — restarting telegram_bot "
                        "(supervisor will respawn a fresh connection)",
                        _time.time() - last)
            import os as _os
            _os._exit(1)


# ── Periodic cleanup ─────────────────────────

async def cleanup_task():
    while True:
        await asyncio.sleep(86400)  # every 24 hours
        cutoff = _time.time() - 86400
        cleared = 0
        for k in list(pending.keys()):
            if pending[k].get("ts", 0) < cutoff:
                for f in pending[k].get("files", []):
                    f["data"] = b""
                del pending[k]
                cleared += 1
        if cleared:
            log.info("24h cleanup: cleared %d old pending entries", cleared)
            gc.collect()
        # Keep sent_keys and approved bounded
        if len(sent_keys) > 2000:
            for k in list(sent_keys)[:500]:
                sent_keys.discard(k)
        if len(approved) > 2000:
            for k in list(approved.keys())[:500]:
                del approved[k]
        log.info("Daily cleanup done. pending=%d sent_keys=%d", len(pending), len(sent_keys))


# ── Main ─────────────────────────────────────

async def main():
    load_channels()
    load_state()
    ai_filter.mark_received()  # telemetry: this boot is the baseline, not stale disk data
    await userbot.start()
    log.info("Userbot logged in.")
    await sync_channel_ids()
    await bot.start(bot_token=BOT_TOKEN)
    log.info("Telegram bot started. tg_active=%s tg_publish=%s", tg_active, tg_publish)
    await ensure_channels_joined()  # needs `bot` started to prompt the admin
    await asyncio.gather(
        userbot.run_until_disconnected(),
        bot.run_until_disconnected(),
        cleanup_task(),
        intake_watchdog(),
        channels_reload_task(),
    )

if __name__ == "__main__":
    asyncio.run(main())
