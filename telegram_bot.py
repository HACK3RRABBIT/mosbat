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

from telethon import TelegramClient, events, Button
from telethon.tl.types import MessageMediaWebPage

from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS, TG_MAIN_ADMIN, TARGET_CHANNEL,
    SOURCE_CHANNELS, PERSIAN_CHANNELS, SOURCE_NAMES, TG_FOOTER,
)
from shared import (
    translate, clean_text, has_link, detect_media_type, media_emoji,
    make_key, has_blockquote, extract_blockquote_and_rest,
    build_caption, build_quote_caption, get_doc_mime,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("telegram_bot.log"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

userbot = TelegramClient("forwarder_user", API_ID, API_HASH)
bot     = TelegramClient("forwarder_bot",  API_ID, API_HASH)

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

# Runtime-editable source channels (loaded from config, can be changed via commands)
active_channels  = list(SOURCE_CHANNELS)
active_persian   = set(PERSIAN_CHANNELS)
active_names     = dict(SOURCE_NAMES)

QUEUE_FILE      = "/tmp/feeder_queue.jsonl"
MAX_QUEUE_LINES = 200
CHANNELS_FILE   = "/tmp/feeder_channels.json"  # shared with bale_bot
STATE_FILE      = "/tmp/feeder_tg_state.json"

MAIN_ADMIN_ID   = TG_MAIN_ADMIN


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
    global active_channels, active_persian, active_names
    if os.path.exists(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE) as f:
                data = json.load(f)
            active_channels = data.get("channels", list(SOURCE_CHANNELS))
            active_persian  = set(data.get("persian", list(PERSIAN_CHANNELS)))
            active_names    = data.get("names", dict(SOURCE_NAMES))
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


# ── Queue writer ──────────────────────────────

def write_to_queue(entry: dict):
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
        gc.collect()


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

    write_to_queue(entry)  # always write — Bale bot reads this regardless of tg_active

    if not tg_active:
        # TG is stopped: free media bytes immediately and return
        for f in entry.get("files", []):
            f["data"] = b""
        gc.collect()
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
        try:
            for admin_id in ADMIN_IDS:
                await bot.send_message(admin_id, text,
                                       buttons=approval_buttons(key),
                                       parse_mode="markdown")
            log.info("Approval requested for message id=%s", msgs[0].id)
        except Exception as e:
            log.error("Failed to send approval request: %s", e)


# ── Bot commands ──────────────────────────────

@bot.on(events.NewMessage(from_users=ADMIN_IDS, pattern="/start"))
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

@bot.on(events.NewMessage(from_users=ADMIN_IDS, pattern="/status"))
async def cmd_status(event):
    label  = "🤖 Auto" if mode == "auto" else "👤 Manual"
    pub    = "🟢 ON" if tg_publish else "🔴 OFF"
    active = "🟢 RUNNING" if tg_active else "🛑 STOPPED"
    await event.respond(
        f"TG bot: **{active}** | Mode: **{label}** | TG publish: **{pub}**\n"
        f"Pending: {len(pending)}",
        parse_mode="markdown"
    )

@bot.on(events.NewMessage(from_users=ADMIN_IDS, pattern="/mode"))
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

@bot.on(events.NewMessage(from_users=[MAIN_ADMIN_ID], pattern="/tgon"))
async def cmd_tgon(event):
    global tg_publish
    tg_publish = True
    save_state()
    await event.respond("🟢 **TG channel publish: ON**", parse_mode="markdown")
    log.info("TG publish → ON")

@bot.on(events.NewMessage(from_users=[MAIN_ADMIN_ID], pattern="/tgoff"))
async def cmd_tgoff(event):
    global tg_publish
    tg_publish = False
    save_state()
    await event.respond("🔴 **TG channel publish: OFF**\n_(messages still go to Bale)_", parse_mode="markdown")
    log.info("TG publish → OFF")

@bot.on(events.NewMessage(from_users=[MAIN_ADMIN_ID], pattern="/tgstop"))
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

@bot.on(events.NewMessage(from_users=[MAIN_ADMIN_ID], pattern="/tgstart"))
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

@bot.on(events.NewMessage(from_users=[MAIN_ADMIN_ID], pattern="/channels"))
async def cmd_channels(event):
    await event.respond(get_channel_list_text(), parse_mode="markdown")

@bot.on(events.NewMessage(from_users=[MAIN_ADMIN_ID], pattern=r"/addchannel (.+)"))
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

    flag = "🇮🇷" if is_fa else "🌐"
    await event.respond(f"✅ Added: {flag} `{ch}` — {name}", parse_mode="markdown")
    log.info("Added channel: %s (%s)", ch, name)

@bot.on(events.NewMessage(from_users=[MAIN_ADMIN_ID], pattern=r"/removechannel (.+)"))
async def cmd_removechannel(event):
    ch = event.pattern_match.group(1).strip()
    if not ch.startswith("@"):
        ch = f"@{ch}"
    if ch in active_channels:
        active_channels.remove(ch)
        active_names.pop(ch, None)
        active_persian.discard(ch)
        save_channels()
        await event.respond(f"✅ Removed: `{ch}`", parse_mode="markdown")
        log.info("Removed channel: %s", ch)
    else:
        await event.respond(f"⚠️ `{ch}` not in list.", parse_mode="markdown")

@bot.on(events.NewMessage(from_users=ADMIN_IDS))
async def handle_edit_reply(event):
    msg = event.message
    if not msg.reply_to:
        return
    replied_id = msg.reply_to.reply_to_msg_id
    for k, v in list(edit_pending.items()):
        if v.get("edit_msg_id") == replied_id:
            new_text = msg.text.strip()
            v["caption"] = new_text
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
    if event.sender_id not in ADMIN_IDS:
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
            pending.pop(key, None)
            edit_pending.pop(key, None)
            await event.edit("❌ Skipped.")

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

@userbot.on(events.NewMessage(chats=active_channels))
async def on_new_message(event):
    msg = event.message
    if has_link(msg.message or ""):
        log.info("Skipped message id=%s (link)", msg.id)
        return
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

@userbot.on(events.MessageEdited(chats=active_channels))
async def on_edited_message(event):
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
    await userbot.start()
    log.info("Userbot logged in.")
    await bot.start(bot_token=BOT_TOKEN)
    log.info("Telegram bot started. tg_active=%s tg_publish=%s", tg_active, tg_publish)
    await asyncio.gather(
        userbot.run_until_disconnected(),
        bot.run_until_disconnected(),
        cleanup_task(),
    )

if __name__ == "__main__":
    asyncio.run(main())
