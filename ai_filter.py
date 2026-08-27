"""
ai_filter.py — LLM news gate for mosbat.

One async entry point, decide(text, source), used by telegram_bot.py BEFORE a
message is written to the queue (which feeds both the Telegram approval flow
and the Bale bot). Each message gets a single LLM call that returns JSON:

    {"relay": bool, "topic": "<canonical phrase>", "reason": "<brief>"}

Three jobs in that one call:
  1. Useless-news filter — drop ads, "follow us" promos, pure link-spam,
     chatter, sports scores, etc.
  2. Deduplication — the LLM returns a short canonical `topic` phrase; we
     compare it against recently relayed topics (TTL-bounded) so the same
     story from IRNA and ISNA collides and only the first is sent.
  3. Logging — every decision is logged for the WebUI Logs view + stats.

Fail-open: if the LLM is unreachable or times out, we RELAY (don't silently
drop real news). We log the failure instead.

State: a JSONL store under the project (gitignored) holding recent topics
with timestamps. Kept small (max_history entries, TTL expiry).
"""
from __future__ import annotations

import os
import re
import json
import time
import asyncio
import threading
import logging

log = logging.getLogger("ai_filter")

PROJ = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(PROJ, "ai_settings.json")
STORE_FILE = os.path.join(PROJ, "ai_dedup_store.jsonl")
STATS_FILE = os.path.join(PROJ, "ai_stats.json")

_lock = threading.Lock()
_settings: dict = {}
_settings_mtime: float = -1.0    # so external edits (e.g. WebUI toggle) reload live
_store: list[dict] = []          # [{topic, ts}]
_stats = {"checked": 0, "relayed": 0, "dropped_useless": 0, "dropped_dup": 0,
          "dropped_media": 0, "errors": 0}
# Monotonic telemetry (epoch seconds): the newest message we saw / relayed.
# Lets the WebUI show "last news" age so a stalled intake is obvious.
_last_received = 0.0
_last_relayed = 0.0


def load_settings() -> dict:
    global _settings
    defaults = {
        "base_url": "http://51.15.120.148:20128/v1",
        "api_key": "",
        "model": "UN/claude-opus-5",
        "enabled": True,
        "dedup_ttl_seconds": 86400,
        "max_history": 800,
        "timeout_seconds": 25,
        "decide_timeout_seconds": 45,
        "max_tokens": 1200,
        "dedup_jaccard": 0.6,
        "media_gate": True,
        "media_gate_types": ["photo", "video", "album_photo", "album_video", "file"],
    }
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    # env overrides
    if os.environ.get("MOSBAT_AI_KEY"):
        data["api_key"] = os.environ["MOSBAT_AI_KEY"]
    if os.environ.get("MOSBAT_AI_BASE"):
        data["base_url"] = os.environ["MOSBAT_AI_BASE"]
    if os.environ.get("MOSBAT_AI_MODEL"):
        data["model"] = os.environ["MOSBAT_AI_MODEL"]
    defaults.update(data)
    with _lock:
        _settings = defaults
    try:
        globals()["_settings_mtime"] = os.path.getmtime(SETTINGS_FILE)
    except Exception:
        globals()["_settings_mtime"] = -1.0
    return defaults


def _maybe_reload_settings() -> None:
    """Reload settings if the JSON file changed on disk (e.g. a WebUI toggle).

    Cheap: only stat()s the file, and only re-parses when the mtime moves."""
    try:
        mtime = os.path.getmtime(SETTINGS_FILE)
    except Exception:
        return
    if mtime == _settings_mtime:
        return
    with _lock:
        load_settings()  # re-reads file + env overrides, then sets _settings
        globals()["_settings_mtime"] = mtime


def get_settings() -> dict:
    _maybe_reload_settings()
    with _lock:
        return dict(_settings)


def is_enabled() -> bool:
    _maybe_reload_settings()
    with _lock:
        return bool(_settings.get("enabled")) and bool(_settings.get("api_key"))


def set_enabled(on: bool) -> bool:
    """Persist the enabled flag and reload in-memory. Returns new state."""
    try:
        try:
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {}
        data["enabled"] = bool(on)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
        with _lock:
            globals()["_settings_mtime"] = os.path.getmtime(SETTINGS_FILE)
        load_settings()
        return bool(on)
    except Exception as e:
        log.warning("ai_filter set_enabled failed: %s", e)
        return is_enabled()


# ── Dedup store ──────────────────────────────────

def _load_store():
    global _store
    try:
        with open(STORE_FILE) as f:
            _store = [json.loads(l) for l in f if l.strip()]
    except Exception:
        _store = []


def _prune_store():
    ttl = _settings.get("dedup_ttl_seconds", 86400)
    now = time.time()
    cutoff = now - ttl
    _store[:] = [e for e in _store if e.get("ts", 0) > cutoff]
    max_h = _settings.get("max_history", 800)
    if len(_store) > max_h:
        _store[:] = _store[-max_h:]


def _is_duplicate(topic: str) -> bool:
    if not topic:
        return False
    key = _norm(topic)
    thresh = _settings.get("dedup_jaccard", 0.6)
    for e in _store:
        if _norm(e["topic"]) == key:
            return True
        if _jaccard(e["topic"], topic) >= thresh:
            return True
    return False


def _remember(topic: str):
    _store.append({"topic": topic, "ts": time.time()})
    _prune_store()
    try:
        with open(STORE_FILE, "a") as f:
            f.write(json.dumps(_store[-1], ensure_ascii=False) + "\n")
    except Exception:
        pass


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^\w\s؀-ۿ]", " ", s)   # keep persian + word chars
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokens(s: str) -> set[str]:
    return set(_norm(s).split())


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ── LLM call (SSE streaming, extract final JSON) ──

_PROMPT = """You are a strict news editor for a Persian news relay channel.
Decide whether the item below should be relayed to the channel.

Relay (relay:true) only genuine news: politics, government, economy, war/conflict,
accidents/disasters, major court rulings, science/health breakthroughs, weather
alerts, significant sports RESULTS only if nationally important.

Do NOT relay (relay:false) if the item is: advertising or sponsored content,
self-promotion ("follow us", "subscribe", "join our channel"), pure link-sharing
with no real substance, routine social/chatter, polls, trivia, or an exact
repost of something already covered.

Also detect duplicates: if this is the SAME underlying event as something already
reported (different wording is fine), set relay:false and topic to the existing
event phrase.

CRITICAL: You MUST finish by printing a single JSON object on its own line.
Do not stop inside a thinking block. If you think, do it briefly, THEN output
exactly this (no markdown fences, no commentary, no trailing text):

{"relay": true|false, "topic": "<canonical phrase, <=8 words, in Persian if source is Persian, identifying the unique event>", "reason": "<one short sentence>"}

Source channel: __SOURCE__
Item text:
__TEXT__"""


def _extract_json(text: str) -> dict | None:
    # strip a <think>...</think> reasoning block (hy3-free emits one)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


async def _call_llm(text: str, source: str) -> dict:
    import httpx
    s = get_settings()
    payload = {
        "model": s["model"],
        "max_tokens": s.get("max_tokens", 200),
        "messages": [{"role": "user", "content": _PROMPT.replace("__SOURCE__", source).replace("__TEXT__", text)}],
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {s['api_key']}",
        "Content-Type": "application/json",
    }
    # Explicit connect + read timeouts. A read timeout is what actually bounds a
    # streaming response that never emits [DONE] — without it the loop waits
    # forever and Telethon stops receiving updates.
    to = httpx.Timeout(connect=10.0, read=s.get("timeout_seconds", 25), write=10.0, pool=10.0)
    collected = []
    async with httpx.AsyncClient(timeout=to) as client:
        async with client.stream("POST", f"{s['base_url']}/chat/completions",
                                 headers=headers, json=payload) as resp:
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    delta = obj.get("choices", [{}])[0].get("delta", {})
                    if "content" in delta:
                        collected.append(delta["content"])
    full = "".join(collected)
    parsed = _extract_json(full)
    if parsed is None:
        raise ValueError(f"no JSON in LLM output: {full[:120]}")
    return parsed


# ── Public API ───────────────────────────────────

def media_gate_enabled() -> bool:
    _maybe_reload_settings()
    with _lock:
        return bool(_settings.get("media_gate"))


def media_gate_check(mtype: str, text: str) -> tuple[bool, str]:
    """Deterministic gate for caption-less media.

    Photos, photo/video albums, standalone videos and documents that carry NO
    real caption (the user wants only captioned media relayed) are dropped. This
    is a cheap syntactic rule — no LLM call — so it runs before decide().

    Returns (dropped: bool, reason: str).
    """
    if not media_gate_enabled():
        return (False, "")
    if mtype not in _settings.get("media_gate_types", ["photo", "video", "album_photo", "album_video", "file"]):
        return (False, "")
    if (text or "").strip():
        return (False, "")
    with _lock:
        _stats["dropped_media"] += 1
    return (True, f"caption-less {mtype}")


# ── Public API ───────────────────────────────────

# match lone media/decorative emoji so "📸" / "🎥" alone counts as no caption
_EMOJI_RE = re.compile(
    "[\U00002600-\U000027BF\U0001F000-\U0001FAFF️‍⃣"
    "\U000024C2-\U0001F251]+", flags=re.UNICODE)


def _caption_is_real(text: str) -> bool:
    """A caption counts as real only if it has substantive text beyond a lone
    media emoji (📸 🎥 📝 etc.). Lone-emoji captions are treated as no caption."""
    if not text or not text.strip():
        return False
    stripped = _EMOJI_RE.sub("", text).strip()
    return len(stripped) >= 3


def media_gate_check(mtype: str, text: str) -> tuple[bool, str]:
    """Deterministic gate for caption-less media.

    Photos, photo/video albums, standalone videos and documents that carry NO
    real caption (the user wants only captioned media relayed) are dropped. This
    is a cheap syntactic rule — no LLM call — so it runs before decide(). A bare
    emoji caption (e.g. just '📸') is treated as no caption.

    Returns (dropped: bool, reason: str).
    """
    if not media_gate_enabled():
        return (False, "")
    if mtype not in _settings.get("media_gate_types", ["photo", "video", "album_photo", "album_video", "file"]):
        return (False, "")
    if _caption_is_real(text):
        return (False, "")
    with _lock:
        _stats["dropped_media"] += 1
    return (True, f"caption-less {mtype}")


def _decide_sync(text: str, source: str = "") -> dict:
    """Synchronous core of the AI gate. MUST be run OUTSIDE Telethon's event
    loop via decide() so a slow LLM call can never block Telegram updates."""
    global _stats, _last_received, _last_relayed
    _last_received = time.time()
    with _lock:
        _stats["checked"] += 1

    if not is_enabled():
        return {"relay": True, "reason": "AI filter disabled", "topic": "", "dropped": None}

    # Retry once on a generation flake (model emits only <think></think> and
    # stops before the JSON).
    result = None
    last_err = None
    for attempt in range(2):
        try:
            result = asyncio.run(_call_llm(text, source))
            if isinstance(result, dict) and "relay" in result:
                break
        except Exception as e:
            last_err = e
            continue
    if result is None:
        with _lock:
            _stats["errors"] += 1
        log.warning("ai_filter LLM error (fail-open): %s", last_err)
        return {"relay": True, "reason": f"LLM error, relayed: {last_err}", "topic": "", "dropped": "error"}

    relay = bool(result.get("relay"))
    topic = (result.get("topic") or "").strip()
    reason = (result.get("reason") or "").strip()

    if not relay:
        with _lock:
            _stats["dropped_useless"] += 1
        log.info("AI DROP (useless): src=%s reason=%s", source, reason)
        return {"relay": False, "reason": reason or "not newsworthy", "topic": topic, "dropped": "useless"}

    # dedup against recent topics
    if _is_duplicate(topic):
        with _lock:
            _stats["dropped_dup"] += 1
        log.info("AI DROP (duplicate): topic=%s", topic)
        return {"relay": False, "reason": f"duplicate of recent: {topic}", "topic": topic, "dropped": "duplicate"}

    _remember(topic)
    with _lock:
        _stats["relayed"] += 1
        _last_relayed = time.time()
    _flush_throttled()
    log.info("AI RELAY: topic=%s src=%s", topic, source)
    return {"relay": True, "reason": reason or "news", "topic": topic, "dropped": None}


async def decide(text: str, source: str = "") -> dict:
    """Run the AI gate off Telethon's event loop with a hard wall-clock deadline.

    The LLM call is CPU/IO-bound and can hang; if we awaited it ON the event
    loop, Telegram updates would stop being read and the whole forwarder would
    freeze (seen in production: a stalled SSE stream left a CLOSE-WAIT socket and
    no news for 2h). We run the sync core in a thread with asyncio.wait_for so a
    slow/hung model can only delay that one message — never the intake loop.
    Fail-open: on timeout/error we RELAY.
    """
    deadline = get_settings().get("decide_timeout_seconds", 30)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_decide_sync, text, source), timeout=deadline)
    except Exception as e:
        with _lock:
            _stats["errors"] += 1
        log.warning("ai_filter decide timed out/failed (fail-open): %s", e)
        return {"relay": True, "reason": f"decide error, relayed: {e}", "topic": "", "dropped": "error"}


def mark_received():
    """Record that a message was received from a source channel (telemetry)."""
    global _last_received
    _last_received = time.time()


def stats() -> dict:
    with _lock:
        s = dict(_stats)
    s["last_received"] = _last_received
    s["last_relayed"] = _last_relayed
    # publish through to disk so the WebUI (separate process) can read live
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(s, f)
    except Exception:
        pass
    return s


_LAST_FLUSH = 0.0

def _flush_throttled():
    """Write stats to disk at most every ~3s (called on every decision in the
    bot process, where stats() is otherwise never invoked). Cheap: skips the
    write unless enough time has passed."""
    global _LAST_FLUSH
    now = time.time()
    if now - _LAST_FLUSH < 3.0:
        return
    _LAST_FLUSH = now
    stats()


def read_stats_file() -> dict:
    try:
        with open(STATS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"checked": 0, "relayed": 0, "dropped_useless": 0,
                "dropped_dup": 0, "dropped_media": 0, "errors": 0,
                "last_received": 0, "last_relayed": 0}


def reset_stats():
    global _stats
    with _lock:
        _stats = {"checked": 0, "relayed": 0, "dropped_useless": 0,
                  "dropped_dup": 0, "dropped_media": 0, "errors": 0}
    stats()  # flush


# ── init ─────────────────────────────────────────

load_settings()
_load_store()
