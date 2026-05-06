"""
Bale Channel Forwarder Bot
- Reads queue from telegram_bot via /tmp/feeder_queue.jsonl
- Downloads media directly from Telegram (no bytes in RAM between bots)
- Sends to Bale channel with approval

Requirements: pip install requests telethon
Run: python bale_bot.py
"""

import io
import json
import logging
import os
import time
import gc

import requests
from telethon.sync import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

from config import (
    API_ID, API_HASH,
    BALE_BOT_TOKEN, BALE_ADMIN_IDS, BALE_MAIN_ADMIN, BALE_TARGET_CHANNEL,
    BALE_FOOTER, BALE_MAX_VIDEO_BYTES, SOURCE_NAMES,
)
from shared import bold_first_para, clean_text

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bale_bot.log"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

BASE_URL   = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}"
QUEUE_FILE = "/tmp/feeder_queue.jsonl"
SEEN_FILE  = "/tmp/bale_seen.json"
MEDIA_DIR  = "/tmp/feeder_media"

os.makedirs(MEDIA_DIR, exist_ok=True)

bale_mode    = "manual"
bale_pending = {}
sent_keys    = set()
_counter     = 0

# Telethon sync client — only for downloading media, reuses existing session
tg = TelegramClient("forwarder_bale_dl", API_ID, API_HASH)


def _next_key():
    global _counter
    _counter += 1
    return f"bale_{_counter}"


# ── Seen tracking ─────────────────────────────

OFFSET_FILE   = "/tmp/bale_offset.txt"
CHANNELS_FILE = "/tmp/feeder_channels.json"

def _load_channels_data():
    if os.path.exists(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE) as f:
                return json.load(f)
        except:
            pass
    from config import SOURCE_CHANNELS, PERSIAN_CHANNELS, SOURCE_NAMES
    return {"channels": list(SOURCE_CHANNELS), "persian": list(PERSIAN_CHANNELS), "names": dict(SOURCE_NAMES)}

def _save_channels_data(data):
    try:
        with open(CHANNELS_FILE, "w") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        log.warning("save_channels_data failed: %s", e)

def _load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE) as f:
                return set(json.load(f))
        except:
            pass
    return set()

def _save_seen(seen):
    try:
        items = list(seen)[-500:]
        with open(SEEN_FILE, "w") as f:
            json.dump(items, f)
    except:
        pass

def _load_offset():
    try:
        if os.path.exists(OFFSET_FILE):
            with open(OFFSET_FILE) as f:
                return int(f.read().strip())
    except:
        pass
    return 0

def _save_offset(offset):
    try:
        with open(OFFSET_FILE, "w") as f:
            f.write(str(offset))
    except:
        pass


# ── Download media from Telegram to disk ──────

def download_media_to_disk(tg_chat_id, tg_msg_id):
    """Download media from Telegram to temp disk file. Returns (path, mime) or (None, None)."""
    path, mime, _ = get_tg_file_url(tg_chat_id, tg_msg_id)
    return path, mime

def delete_media(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except:
        pass

def cleanup_old_media():
    """Delete media files older than 2 hours."""
    try:
        now = time.time()
        for fname in os.listdir(MEDIA_DIR):
            fpath = os.path.join(MEDIA_DIR, fname)
            if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 7200:
                os.remove(fpath)
    except:
        pass


# ── Bale API ──────────────────────────────────

def api(method, **kwargs):
    try:
        r = requests.post(f"{BASE_URL}/{method}", json=kwargs, timeout=30)
        data = r.json()
        if not data.get("ok"):
            log.warning("Bale API [%s]: %s", method, data.get("description"))
            return None
        return data.get("result")
    except Exception as e:
        log.error("Bale request failed [%s]: %s", method, e)
        return None

def api_send_url(method, url, field_name, payload):
    """Send media to Bale using a direct URL (no upload needed)."""
    try:
        data = {**payload, field_name: url}
        r = requests.post(f"{BASE_URL}/{method}", json=data, timeout=30)
        result = r.json()
        if result.get("ok"):
            return result.get("result")
        log.warning("Bale URL send [%s]: %s", method, result.get("description"))
        return None
    except Exception as e:
        log.error("Bale URL send failed: %s", e)
        return None

def get_tg_file_url(tg_chat_id, tg_msg_id):
    """Get direct download URL for a Telegram message's media."""
    try:
        msg = tg.get_messages(tg_chat_id, ids=tg_msg_id)
        if not msg or not msg.media:
            return None, None, None

        mime = ""
        from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
        if isinstance(msg.media, MessageMediaPhoto):
            mime = "image/jpeg"
        elif isinstance(msg.media, MessageMediaDocument):
            mime = getattr(msg.media.document, "mime_type", "") or ""
            size = getattr(msg.media.document, "size", 0) or 0
            if size > BALE_MAX_VIDEO_BYTES and mime.startswith("video/"):
                log.info("Skipped: video > 20MB")
                return None, None, None

        # Download to temp file
        ext = ".jpg" if mime.startswith("image/") else ".mp4" if mime.startswith("video/") else ".bin"
        tmp_path = os.path.join(MEDIA_DIR, f"bale_{tg_msg_id}{ext}")
        tg.download_media(msg, file=tmp_path)
        return tmp_path, mime, msg
    except Exception as e:
        log.warning("get_tg_file_url failed: %s", e)
        return None, None, None

def send_file_to_bale(method, file_path, field_name, mime, payload, fallback_doc=True):
    """Send file to Bale — one attempt only, fast fail."""
    try:
        with open(file_path, "rb") as fh:
            file_bytes = fh.read()
        fname = os.path.basename(file_path)
        files = {field_name: (fname, file_bytes, mime)}
        data  = {k: str(v) for k, v in payload.items()}
        r = requests.post(f"{BASE_URL}/{method}", files=files, data=data, timeout=30)
        result = r.json()
        if result.get("ok"):
            return True
        log.warning("Bale %s failed: %s", method, result.get("description"))
        return False
    except Exception as e:
        log.error("Bale send_file failed: %s", e)
        return False

def approval_keyboard(key):
    return {"inline_keyboard": [[
        {"text": "✅ بله",     "callback_data": f"yes_{key}"},
        {"text": "✏️ ویرایش", "callback_data": f"edit_{key}"},
        {"text": "❌ خیر",    "callback_data": f"no_{key}"},
    ]]}

def mode_keyboard():
    return {"inline_keyboard": [[
        {"text": "🤖 Auto",   "callback_data": "bale_set_auto"},
        {"text": "👤 Manual", "callback_data": "bale_set_manual"},
    ]]}


# ── Send to Bale channel ──────────────────────

def send_to_channel(entry):
    mtype      = entry["type"]
    caption    = bold_first_para(entry.get("caption", ""))
    media_path = entry.get("media_path")
    mime       = entry.get("mime", "")
    payload    = {"chat_id": BALE_TARGET_CHANNEL, "parse_mode": "Markdown",
                  "caption": caption + BALE_FOOTER}

    try:
        media_sent = False

        if media_path and os.path.exists(media_path) and mtype != "text":
            if mime.startswith("image/") or mtype in ("photo", "album_photo"):
                media_sent = send_file_to_bale("sendPhoto", media_path, "photo",
                                               "image/jpeg", payload, fallback_doc=True)
            elif mime.startswith("video/") or mtype in ("video", "album_video"):
                size = os.path.getsize(media_path)
                if size > BALE_MAX_VIDEO_BYTES:
                    log.info("Skipped Bale channel video > 20MB")
                else:
                    media_sent = send_file_to_bale("sendVideo", media_path, "video",
                                                   "video/mp4",
                                                   {**payload, "supports_streaming": "true"},
                                                   fallback_doc=True)
            else:
                media_sent = send_file_to_bale("sendDocument", media_path, "document",
                                               mime or "application/octet-stream",
                                               payload, fallback_doc=False)

        # If media failed or no media — send text only
        if not media_sent:
            if not media_sent and mtype != "text":
                log.warning("Media upload failed — sending text only to Bale channel")
            api("sendMessage", chat_id=BALE_TARGET_CHANNEL,
                text=caption + BALE_FOOTER, parse_mode="Markdown")

        log.info("Sent to Bale channel (media=%s)", media_sent)
    except Exception as e:
        log.error("Failed to send to Bale channel: %s", e)
    finally:
        gc.collect()


# ── Ask admin approval ────────────────────────

def cleanup_pending():
    cutoff = time.time() - 86400
    cleared = 0
    for k in list(bale_pending.keys()):
        if bale_pending[k].get("ts", 0) < cutoff:
            delete_media(bale_pending[k].get("media_path"))
            del bale_pending[k]
            cleared += 1
    if cleared:
        log.info("24h cleanup: cleared %d old bale_pending entries", cleared)
        gc.collect()
    if len(sent_keys) > 2000:
        for k in list(sent_keys)[:500]:
            sent_keys.discard(k)


def ask_approval(entry):
    key = _next_key()
    entry["ts"] = time.time()
    bale_pending[key] = entry

    src_name = SOURCE_NAMES.get(entry.get("source_username", ""), entry.get("source_username", "ناشناس"))
    preview  = entry.get("caption", "").split("\n\n🇮🇷")[0].strip()[:400] or "*(بدون متن)*"
    tlabel   = {
        "text": "📝 متن", "photo": "📸 عکس", "video": "🎥 ویدیو",
        "file": "📎 فایل", "album_photo": "🖼 آلبوم", "album_video": "🎥 آلبوم",
    }.get(entry["type"], "📎 رسانه")

    text = (
        f"📨 *پیام جدید*\n"
        f"*منبع:* {src_name}\n"
        f"*نوع:* {tlabel}\n\n"
        f"*پیش‌نمایش:*\n{preview}\n\n"
        f"ارسال به کانال بله؟"
    )

    media_path = entry.get("media_path")
    mime       = entry.get("mime", "")

    for admin_id in BALE_ADMIN_IDS:
        api("sendMessage", chat_id=admin_id, text=text,
            parse_mode="Markdown", reply_markup=approval_keyboard(key))

        if media_path and os.path.exists(media_path):
            try:
                prev_payload = {"chat_id": admin_id, "caption": "👆 پیش‌نمایش"}
                if mime.startswith("image/") or entry["type"] in ("photo", "album_photo"):
                    ok = send_file_to_bale("sendPhoto", media_path, "photo", "image/jpeg",
                                          prev_payload, fallback_doc=True)
                    if not ok:
                        api("sendMessage", chat_id=admin_id,
                            text="📸 *(عکس — آپلود ناموفق)*", parse_mode="Markdown")
                elif mime.startswith("video/") or entry["type"] in ("video", "album_video"):
                    ok = send_file_to_bale("sendVideo", media_path, "video", "video/mp4",
                                          {**prev_payload, "supports_streaming": "true"},
                                          fallback_doc=True)
                    if not ok:
                        api("sendMessage", chat_id=admin_id,
                            text="🎥 *(ویدیو — آپلود ناموفق)*", parse_mode="Markdown")
            except Exception as e:
                log.warning("Preview send failed: %s", e)

    log.info("Bale approval requested (key=%s)", key)


# ── Read queue from telegram_bot ──────────────

def _process_one_record(record) -> dict | None:
    """Process a single queue record. Returns entry or None."""
    tg_chat_id = record.get("tg_chat_id")
    tg_msg_ids = record.get("tg_msg_ids", [])
    mtype      = record.get("type", "text")

    media_path, mime = None, ""
    if tg_chat_id and tg_msg_ids and mtype != "text":
        media_path, mime = download_media_to_disk(tg_chat_id, tg_msg_ids[0])

    return {
        "caption":         record.get("caption", ""),
        "type":            mtype,
        "raw_text":        record.get("raw_text", ""),
        "source_username": record.get("source_username", ""),
        "media_path":      media_path,
        "mime":            mime,
    }

def process_queue(seen):
    """Process ALL pending queue items (used on startup catchup)."""
    if not os.path.exists(QUEUE_FILE):
        return seen
    try:
        with open(QUEUE_FILE, "r") as f:
            lines = f.readlines()
    except:
        return seen
    for line in lines:
        line = line.strip()
        if not line:
            continue
        line_key = str(hash(line))
        if line_key in seen:
            continue
        try:
            record = json.loads(line)
            if time.time() - record.get("ts", 0) > 3600:
                seen.add(line_key)
                continue
            entry = _process_one_record(record)
            if bale_mode == "auto":
                send_to_channel(entry)
                delete_media(entry.get("media_path"))
            else:
                ask_approval(entry)
            seen.add(line_key)
        except Exception as e:
            log.warning("Queue parse error: %s | %s", e, line[:80])
            seen.add(line_key)
    _save_seen(seen)
    return seen

def process_queue_one(seen):
    """Process ONE pending queue item per call — keeps bot responsive."""
    if not os.path.exists(QUEUE_FILE):
        return seen
    try:
        with open(QUEUE_FILE, "r") as f:
            lines = f.readlines()
    except:
        return seen
    for line in lines:
        line = line.strip()
        if not line:
            continue
        line_key = str(hash(line))
        if line_key in seen:
            continue
        try:
            record = json.loads(line)
            if time.time() - record.get("ts", 0) > 3600:
                seen.add(line_key)
                _save_seen(seen)
                return seen
            entry = _process_one_record(record)
            if bale_mode == "auto":
                send_to_channel(entry)
                delete_media(entry.get("media_path"))
            else:
                ask_approval(entry)
            seen.add(line_key)
            _save_seen(seen)
            return seen  # only one per loop
        except Exception as e:
            log.warning("Queue parse error: %s | %s", e, line[:80])
            seen.add(line_key)
    return seen


# ── Handle Bale updates ───────────────────────

def handle_update(update):
    global bale_mode
    cb  = update.get("callback_query")
    msg = update.get("message")

    if msg:
        sender_id = msg.get("from", {}).get("id")
        chat_id   = msg.get("chat", {}).get("id")
        text      = msg.get("text", "").strip()

        if text == "/start":
            api("sendMessage", chat_id=chat_id, parse_mode="Markdown", text=(
                "👋 *به دستیار مثبت ایران خوش آمدید!*\n\n"
                "📋 *دستورات:*\n"
                "/mode — تغییر حالت\n"
                "/status — حالت فعلی\n"
                "/pending — پیام‌های در انتظار\n"
                "/channels — لیست کانال‌های منبع\n"
                "/addchannel — اضافه کردن کانال\n"
                "/removechannel — حذف کانال"
            ))
            return

        if sender_id not in BALE_ADMIN_IDS:
            return

        if text == "/mode":
            current = "🤖 Auto" if bale_mode == "auto" else "👤 Manual"
            api("sendMessage", chat_id=chat_id,
                text=f"حالت فعلی: *{current}*\n\nانتخاب کنید:",
                parse_mode="Markdown", reply_markup=mode_keyboard())
            return

        if text == "/status":
            label = "🤖 Auto" if bale_mode == "auto" else "👤 Manual"
            api("sendMessage", chat_id=chat_id,
                text=f"حالت فعلی: *{label}*", parse_mode="Markdown")
            return

        if text == "/pending":
            api("sendMessage", chat_id=chat_id,
                text=f"📋 پیام‌های در انتظار: *{len(bale_pending)}*",
                parse_mode="Markdown")
            return

        if text == "/channels" and sender_id in BALE_ADMIN_IDS:
            ch_data = _load_channels_data()
            channels = ch_data.get("channels", [])
            names    = ch_data.get("names", {})
            persian  = set(ch_data.get("persian", []))
            lines = ["📋 *کانال‌های منبع:*\n"]
            for ch in channels:
                flag = "🇮🇷" if ch in persian else "🌐"
                lines.append(f"{flag} `{ch}` — {names.get(ch, '—')}")
            api("sendMessage", chat_id=chat_id,
                text="\n".join(lines), parse_mode="Markdown")
            return

        if text.startswith("/addchannel ") and sender_id == BALE_MAIN_ADMIN:
            parts = text[len("/addchannel "):].strip().split()
            if len(parts) < 2:
                api("sendMessage", chat_id=chat_id,
                    text="Usage: `/addchannel @username ChannelName [persian]`",
                    parse_mode="Markdown")
                return
            ch    = parts[0] if parts[0].startswith("@") else f"@{parts[0]}"
            name  = parts[1]
            is_fa = len(parts) > 2 and parts[2].lower() == "persian"
            ch_data = _load_channels_data()
            if ch not in ch_data["channels"]:
                ch_data["channels"].append(ch)
            ch_data["names"][ch] = name
            if is_fa:
                if ch not in ch_data["persian"]:
                    ch_data["persian"].append(ch)
            else:
                ch_data["persian"] = [p for p in ch_data["persian"] if p != ch]
            _save_channels_data(ch_data)
            flag = "🇮🇷" if is_fa else "🌐"
            api("sendMessage", chat_id=chat_id,
                text=f"✅ اضافه شد: {flag} `{ch}` — {name}",
                parse_mode="Markdown")
            return

        if text.startswith("/removechannel ") and sender_id == BALE_MAIN_ADMIN:
            ch = text[len("/removechannel "):].strip()
            if not ch.startswith("@"):
                ch = f"@{ch}"
            ch_data = _load_channels_data()
            if ch in ch_data["channels"]:
                ch_data["channels"].remove(ch)
                ch_data["names"].pop(ch, None)
                ch_data["persian"] = [p for p in ch_data["persian"] if p != ch]
                _save_channels_data(ch_data)
                api("sendMessage", chat_id=chat_id,
                    text=f"✅ حذف شد: `{ch}`", parse_mode="Markdown")
            else:
                api("sendMessage", chat_id=chat_id,
                    text=f"⚠️ `{ch}` در لیست نیست.", parse_mode="Markdown")
            return

        # Edit reply
        reply_to = msg.get("reply_to_message", {})
        if reply_to:
            replied_id = reply_to.get("message_id")
            for k, v in list(bale_pending.items()):
                if v.get("bale_edit_msg_id") == replied_id:
                    new_text = text
                    if new_text:
                        bale_pending[k]["caption"] = new_text
                        api("sendMessage", chat_id=chat_id,
                            text=f"✅ متن ویرایش شد.\n\n*پیش‌نمایش:*\n{new_text[:400]}\n\nارسال؟",
                            parse_mode="Markdown", reply_markup=approval_keyboard(k))
                    return
        return

    if not cb:
        return

    query_id  = cb["id"]
    data      = cb.get("data", "")
    chat_id   = cb["message"]["chat"]["id"]
    msg_id    = cb["message"]["message_id"]
    sender_id = cb["from"]["id"]
    api("answerCallbackQuery", callback_query_id=query_id)

    if sender_id not in BALE_ADMIN_IDS:
        return

    if data == "bale_set_auto":
        bale_mode = "auto"
        api("editMessageText", chat_id=chat_id, message_id=msg_id,
            text="✅ حالت *🤖 Auto* فعال شد.", parse_mode="Markdown")
        return

    if data == "bale_set_manual":
        bale_mode = "manual"
        api("editMessageText", chat_id=chat_id, message_id=msg_id,
            text="✅ حالت *👤 Manual* فعال شد.", parse_mode="Markdown")
        return

    if data.startswith("edit_"):
        key   = data[5:]
        entry = bale_pending.get(key)
        if not entry:
            api("editMessageText", chat_id=chat_id, message_id=msg_id,
                text="⚠️ پیام یافت نشد.", parse_mode="Markdown")
            return
        result = api("sendMessage", chat_id=chat_id,
                     text=f"✏️ *ریپلای بزنید و متن جدید را بنویسید:*\n\n{entry.get('caption', '')}",
                     parse_mode="Markdown")
        if result:
            bale_pending[key]["bale_edit_msg_id"] = result["message_id"]
        api("editMessageText", chat_id=chat_id, message_id=msg_id,
            text="✏️ به پیام زیر ریپلای بزنید.", parse_mode="Markdown")
        return

    if data.startswith("yes_"):
        key       = data[4:]
        bale_sent = f"bale_sent_{key}"
        if bale_sent in sent_keys:
            api("editMessageText", chat_id=chat_id, message_id=msg_id,
                text="✅ قبلاً ارسال شد.", parse_mode="Markdown")
            return
        entry = bale_pending.pop(key, None)
        if not entry:
            api("editMessageText", chat_id=chat_id, message_id=msg_id,
                text="⚠️ Already handled.", parse_mode="Markdown")
            return
        sent_keys.add(bale_sent)
        api("editMessageText", chat_id=chat_id, message_id=msg_id,
            text="⏳ در حال ارسال...", parse_mode="Markdown")
        send_to_channel(entry)
        # Clean up media file after send
        delete_media(entry.get("media_path"))
        api("editMessageText", chat_id=chat_id, message_id=msg_id,
            text="✅ به کانال بله ارسال شد.", parse_mode="Markdown")

    elif data.startswith("no_"):
        key = data[3:]
        entry = bale_pending.pop(key, None)
        if entry:
            delete_media(entry.get("media_path"))
        api("editMessageText", chat_id=chat_id, message_id=msg_id,
            text="❌ رد شد.", parse_mode="Markdown")


# ── Main ─────────────────────────────────────

import threading

# Queue for callback updates — processed in separate thread
_callback_queue = []
_callback_lock  = threading.Lock()


def _callback_worker():
    """Process button callbacks in a separate thread — always fast."""
    while True:
        update = None
        with _callback_lock:
            if _callback_queue:
                update = _callback_queue.pop(0)
        if update:
            try:
                handle_update(update)
            except Exception as e:
                log.error("Callback worker error: %s", e)
        else:
            time.sleep(0.1)


def run():
    log.info("Bale bot starting...")

    try:
        tg.start()
        me = tg.get_me()
        if me is None:
            raise Exception("get_me returned None")
        log.info("Telethon session OK — logged in as @%s", me.username)
    except Exception as e:
        log.error("Telegram session error: %s", e)
        log.error("Run: python bale_login.py first.")
        return

    me = api("getMe")
    if not me:
        log.error("Cannot connect to Bale API.")
        return
    log.info("Bale connected as @%s", me.get("username"))

    # Start callback worker thread
    worker = threading.Thread(target=_callback_worker, daemon=True)
    worker.start()
    log.info("Callback worker thread started.")

    seen                 = _load_seen()
    offset               = _load_offset()
    last_cleanup         = time.time()
    last_pending_cleanup = time.time()

    # Skip old updates on first start
    if offset == 0:
        updates = api("getUpdates", offset=-1, timeout=1, limit=1)
        if updates:
            offset = updates[-1]["update_id"] + 1
            _save_offset(offset)
        log.info("Starting from offset %d", offset)

    while True:
        try:
            # Poll Bale for updates
            updates = api("getUpdates", offset=offset, timeout=3, limit=50)
            if updates:
                for update in updates:
                    offset = update["update_id"] + 1
                    cb  = update.get("callback_query")
                    msg = update.get("message")

                    if cb:
                        # Callbacks go to fast worker thread immediately
                        with _callback_lock:
                            _callback_queue.append(update)
                    elif msg:
                        # Commands handled directly (fast — no blocking)
                        try:
                            handle_update(update)
                        except Exception as e:
                            log.error("Message update error: %s", e)
                _save_offset(offset)

            # Process one queue item (may be slow due to download)
            seen = process_queue_one(seen)

            # Hourly media cleanup
            if time.time() - last_cleanup > 3600:
                cleanup_old_media()
                last_cleanup = time.time()
                gc.collect()

            # Daily pending cleanup
            if time.time() - last_pending_cleanup > 86400:
                cleanup_pending()
                last_pending_cleanup = time.time()

            # Keep seen set bounded in memory
            if len(seen) > 1000:
                seen = set(list(seen)[-500:])

        except KeyboardInterrupt:
            log.info("Shutting down.")
            tg.disconnect()
            break
        except Exception as e:
            log.error("Poll error: %s", e)
            time.sleep(5)

if __name__ == "__main__":
    run()
