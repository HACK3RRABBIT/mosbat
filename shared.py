"""
Shared utilities: text cleaning, translation, media helpers.
Used by both telegram_bot.py and bale_bot.py
"""

import re
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from deep_translator import GoogleTranslator
from telethon.tl.types import (
    MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage,
    DocumentAttributeVideo, DocumentAttributeAnimated,
    MessageEntityBlockquote,
)

log = logging.getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=2)  # low for 1-core server


# ── Translation ───────────────────────────────

def _translate_sync(text: str) -> str:
    if not text.strip():
        return text
    try:
        result = GoogleTranslator(source="auto", target="fa").translate(text)
        return result if result else text
    except Exception as e:
        log.warning("Translation failed: %s", e)
        return text

async def translate(text: str, skip: bool = False) -> str:
    if not text or skip:
        return text
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _translate_sync, text)


# ── Text cleaning ─────────────────────────────

def remove_emoji(text: str) -> str:
    pattern = re.compile(
        "[\U00002600-\U000027BF\U0001F300-\U0001F64F\U0001F680-\U0001F6FF"
        "\U0001F700-\U0001F77F\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF"
        "\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
        "\U00002702-\U000027B0\U000024C2-\U0001F251]+", flags=re.UNICODE)
    return pattern.sub("", text)

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"^عاجل[\s:،|–\-]*", "", text.strip())
    # Remove all URLs (with or without protocol)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"www\.\S+", "", text)
    text = re.sub(r"t\.me/\S+", "", text)
    text = re.sub(
        r"\b\w[\w\-]*\.(?:ir|com|net|org|io|ai|co|info|me|tv|news|app|site|online|link|press)(?:/\S*)?",
        "", text, flags=re.IGNORECASE
    )
    # Remove @mentions and #hashtags
    text = re.sub(r"@\S+", "", text)
    text = re.sub(r"#\S+", "", text)
    # Remove emoji
    text = remove_emoji(text)
    # Remove promotional lines
    text = re.sub(
        r"[^\n]*(?:لینک خبر|اخبار|کانال|صفحه)[^\n]*(?:بله|روبیکا|سروش|ایتا|جهان|تلگرام|اینستاگرام|توییتر|ایکس)[^\n]*",
        "", text, flags=re.IGNORECASE
    )
    text = re.sub(r"[^\n]*(?:دنبال کنید|follow)[^\n]*", "", text, flags=re.IGNORECASE)
    # Remove "- Link." or "لینک خبر." lines
    text = re.sub(r"^[\s\-]*link\.?\s*$", "", text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r"^[\s\-]*لینک خبر\.?\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def has_link(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(
        r"https?://|www\.|t\.me/|"
        r"\b\w+\.(?:ir|com|net|org|io|ai|co|info|me|tv|news|app|site|online|link|press)(?:/\S*)?\b",
        text, re.IGNORECASE
    ))

def ensure_period(text: str) -> str:
    text = text.strip()
    if text and text[-1] not in ".!?؟،…":
        text += "."
    return text


# ── Media helpers ─────────────────────────────

def is_video_document(doc) -> bool:
    for attr in doc.attributes:
        if isinstance(attr, (DocumentAttributeVideo, DocumentAttributeAnimated)):
            return True
    return False

def get_doc_mime(doc) -> str:
    return getattr(doc, "mime_type", "") or ""

def detect_media_type(msgs) -> str:
    media_msgs = [m for m in msgs if m.media and not isinstance(m.media, MessageMediaWebPage)]
    if not media_msgs:
        return "text"
    if len(msgs) > 1:
        for m in msgs:
            if isinstance(getattr(m, "media", None), MessageMediaDocument):
                if is_video_document(m.media.document):
                    return "album_video"
        return "album_photo"
    m = media_msgs[0]
    if isinstance(m.media, MessageMediaPhoto):
        return "photo"
    if isinstance(m.media, MessageMediaDocument):
        mime = get_doc_mime(m.media.document)
        if is_video_document(m.media.document) or mime.startswith("video/"):
            return "video"
        if mime.startswith("image/"):
            return "photo"
        return "file"
    return "file"

def media_emoji(mtype: str) -> str:
    return {"text": "📝", "photo": "📸", "video": "🎥",
            "file": "📝", "album_photo": "📸", "album_video": "🎥"}.get(mtype, "📝")

def make_key(chat_id, msg_id) -> str:
    return f"{chat_id}_{msg_id}"

def has_blockquote(msg) -> bool:
    return bool(msg.entities and any(isinstance(e, MessageEntityBlockquote) for e in msg.entities))

def extract_blockquote_and_rest(msg):
    if not msg.entities or not msg.message:
        return None, msg.message or ""
    text = msg.message
    for entity in msg.entities:
        if isinstance(entity, MessageEntityBlockquote):
            quote = text[entity.offset:entity.offset + entity.length].strip()
            rest  = (text[:entity.offset] + text[entity.offset + entity.length:]).strip()
            return quote, rest
    return None, text

def bold_first_para(text: str) -> str:
    """Bold first line, add 🔹 to subsequent lines, ensure blank line between header and body."""
    if not text:
        return text
    lines = [l for l in text.strip().splitlines()]
    if not lines:
        return text

    first = lines[0].strip()
    rest_lines = [l for l in lines[1:] if l.strip()]

    if not rest_lines:
        # Single line — just bold it
        return f"*{first}*"

    # Add 🔹 to each subsequent line
    rest_formatted = "\n".join(f"🔹{l.strip()}" for l in rest_lines)

    return f"*{first}*\n\n{rest_formatted}"


# ── Build captions ────────────────────────────

async def build_caption(emoji: str, text: str, skip_translate: bool = False) -> str:
    cleaned    = clean_text(text)
    translated = await translate(cleaned, skip=skip_translate) if cleaned else ""
    if translated:
        return f"{emoji} {ensure_period(translated)}"
    return emoji

async def build_quote_caption(emoji: str, quote: str, rest: str, skip_translate: bool = False) -> str:
    tq = ensure_period(await translate(clean_text(quote), skip=skip_translate)) if quote else ""
    tr = ensure_period(await translate(clean_text(rest),  skip=skip_translate)) if rest  else ""
    parts = []
    if tq:
        parts.append("\n".join(f"❝ {l}" for l in tq.splitlines() if l.strip()))
    if tr:
        parts.append(tr)
    body = "\n\n".join(parts)
    return f"{emoji} {body}" if body else emoji
