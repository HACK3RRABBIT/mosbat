"""
ai_filter.py — TypeSafe news gate for mosbat.

One async entry point, decide(text, source), used by telegram_bot.py BEFORE a
message is written to the queue (which feeds both the Telegram approval flow
and the Bale bot). Each message gets a single TypeSafe System One request that
asks two kinds of typed questions in parallel:

  1. Useless-news filter — a Choice question classifies the item as news, an
     ad, self-promotion, chatter, or other. Only "news" relays.
  2. Deduplication — one Noul question per recently-relayed item ("does this
     report the same underlying event as recent item i?"), batched into the
     SAME request. This asks the model directly rather than comparing an
     LLM-invented "topic phrase" via local word overlap, so a paraphrased
     duplicate from a different source (different wording, same event) is
     still caught.

Both questions ride in one System One call — TypeSafe's own guidance is that
adding more Noul questions barely changes response time, since they all run
in parallel against the same state. The recent-items window is kept small
(max_history, TTL-bounded) so the per-message question count stays bounded.

Fail-open: if TypeSafe is unreachable or times out, we RELAY (don't silently
drop real news). We log the failure instead.

State: a JSONL store under the project (gitignored) holding recent relayed
item texts with timestamps, used to build the dedup candidate list.
"""
from __future__ import annotations

import os
import re
import json
import time
import hashlib
import asyncio
import threading
import logging

log = logging.getLogger("ai_filter")

PROJ = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(PROJ, "ai_settings.json")
STORE_FILE = os.path.join(PROJ, "ai_dedup_store.jsonl")
STATS_FILE = os.path.join(PROJ, "ai_stats.json")
# Training data for a future fine-tuned local model: every judged item with Jev's
# full answer distribution, plus the admin's approve/reject tap as the human label.
DATASET_DIR = os.path.join(PROJ, "dataset")
DECISIONS_FILE = os.path.join(DATASET_DIR, "decisions.jsonl")
LABELS_FILE = os.path.join(DATASET_DIR, "labels.jsonl")

_lock = threading.Lock()
_settings: dict = {}
_settings_mtime: float = -1.0    # so external edits (e.g. WebUI toggle) reload live
_store: list[dict] = []          # [{text, ts}], oldest first
_stats = {"checked": 0, "relayed": 0, "dropped_useless": 0, "dropped_dup": 0,
          "dropped_media": 0, "errors": 0}
# Monotonic telemetry (epoch seconds): the newest message we saw / relayed.
# Lets the WebUI show "last news" age so a stalled intake is obvious.
_last_received = 0.0
_last_relayed = 0.0


def load_settings() -> dict:
    global _settings
    defaults = {
        "api_key": "",
        "model": "jev-latest",
        "enabled": True,
        "dedup_ttl_seconds": 10800,     # 3h — same-event cross-source coverage window
        "max_history": 30,              # cap on dedup candidates sent per request
        "dedup_threshold": 0.72,        # Noul probability above which we call it a dup
        "decide_timeout_seconds": 30,
        "dataset_max_mb": 500,          # stop collecting (and warn) past this size
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


# ── Dedup store (recent relayed item texts, used as TypeSafe Noul candidates) ──

def _load_store():
    global _store
    try:
        with open(STORE_FILE) as f:
            _store = [json.loads(l) for l in f if l.strip()]
    except Exception:
        _store = []


def _prune_store():
    ttl = _settings.get("dedup_ttl_seconds", 10800)
    now = time.time()
    cutoff = now - ttl
    _store[:] = [e for e in _store if e.get("ts", 0) > cutoff]
    max_h = _settings.get("max_history", 30)
    if len(_store) > max_h:
        _store[:] = _store[-max_h:]


_last_store_flush = 0.0
_STORE_FLUSH_EVERY = 60.0  # seconds


def _rewrite_store():
    """Rewrite STORE_FILE to match the pruned in-memory _store.

    _remember() below only *appends* on the hot path — pruning by TTL/
    max_history only trims the in-memory list, so without this the file would
    grow forever regardless of the configured cap (a real disk-space leak on a
    storage-constrained box). Throttled like _flush_throttled() so a full
    rewrite doesn't happen on every single message.
    """
    global _last_store_flush
    now = time.time()
    if now - _last_store_flush < _STORE_FLUSH_EVERY:
        return
    _last_store_flush = now
    try:
        tmp = STORE_FILE + ".tmp"
        with open(tmp, "w") as f:
            for e in _store:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, STORE_FILE)
    except Exception as e:
        log.warning("ai_filter _rewrite_store failed: %s", e)


def _remember(text: str):
    _store.append({"text": text[:400], "ts": time.time()})
    before = len(_store)
    _prune_store()
    pruned = len(_store) < before
    # Always append so the new entry is never lost even if the compacting
    # rewrite below is throttled — _rewrite_store() will clean up stale
    # appended-but-pruned lines the next time it actually runs.
    try:
        with open(STORE_FILE, "a") as f:
            f.write(json.dumps(_store[-1], ensure_ascii=False) + "\n")
    except Exception:
        pass
    if pruned:
        _rewrite_store()


# ── TypeSafe System One call ──────────────────────

_CATEGORIES = {
    "news": ("A report of a concrete, current event or development: politics, government "
             "decisions, economy, war or conflict, accidents or disasters, court rulings, "
             "science or health, weather alerts, or nationally significant sports results. "
             "A report ABOUT a religious event, figure or institution (an official statement, "
             "a pilgrimage and its numbers, a ruling, an attack on a shrine) is news."),
    "religious": ("Religious, spiritual, mystical or devotional content that does not report a "
                  "news event: prayers and supplications, Quran verses, hadith, sermons, moral "
                  "or mystical advice, poetry about God, faith or the Imams, or greetings and "
                  "condolences for religious occasions."),
    "ad": "Advertising or sponsored content.",
    "self_promo": 'Self-promotion: "follow us", "subscribe", "join our channel".',
    "chatter": "Routine social chatter, polls, or trivia with no real news content.",
    "other": "Pure link-sharing with no real substance, or anything else that isn't genuine news.",
}


async def _call_typesafe(text: str, source: str, candidates: list[str]) -> dict:
    from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul
    s = get_settings()

    questions = {
        "quality": Choice(
            instructions=f"What kind of content is this item from Persian news source '{source}'?",
            criteria=_CATEGORIES,
        ),
    }
    for i in range(len(candidates)):
        questions[f"dup_{i}"] = Noul(
            instructions=(
                f"Does `article.text` report the same underlying news event as "
                f"`recent[{i}].text`? Different wording, source, or level of "
                f"detail describing the same event still counts as yes."
            ),
        )

    state = {
        "article": {"text": text},
        "recent": [{"text": c} for c in candidates],
    }

    async with AsyncTypeSafeClient(api_key=s["api_key"]) as client:
        response = await client.system_one(
            state=state, questions=questions, model=s.get("model", "jev-latest"))

    category = response.choices["quality"].choice
    threshold = s.get("dedup_threshold", 0.72)
    dup_index, dup_prob = None, 0.0
    for i in range(len(candidates)):
        p = response.nouls[f"dup_{i}"].noul
        if p >= threshold and p > dup_prob:
            dup_index, dup_prob = i, p

    q = response.choices["quality"]
    return {"category": category, "dup_index": dup_index, "dup_prob": dup_prob,
            "confidence": q.confidence, "probabilities": dict(q.probabilities),
            "dup_probs": [response.nouls[f"dup_{i}"].noul for i in range(len(candidates))]}


# ── Dataset collection ───────────────────────────

def text_id(text: str) -> str:
    """Stable id for an item. Candidates in the dedup store are text[:400], so the id is
    taken over the same prefix — a candidate id then points at the record that judged it."""
    return hashlib.sha1((text or "")[:400].encode("utf-8")).hexdigest()[:16]


_dataset_full_warned = False


def _append_dataset(path: str, record: dict):
    global _dataset_full_warned
    try:
        os.makedirs(DATASET_DIR, exist_ok=True)
        limit = get_settings().get("dataset_max_mb", 500) * 1024 * 1024
        used = sum(os.path.getsize(f) for f in (DECISIONS_FILE, LABELS_FILE) if os.path.exists(f))
        if used > limit:
            if not _dataset_full_warned:
                log.warning("dataset/ reached %d MB cap — no longer collecting training data", used // 1048576)
                _dataset_full_warned = True
            return
        with open(path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("dataset write failed: %s", e)


def record_label(item_id, label: str, edited_text=None):
    """Human outcome from the Telegram approval flow: approved / rejected / approved_edited."""
    if not item_id:
        return
    rec = {"ts": time.time(), "id": item_id, "label": label}
    if edited_text is not None:
        rec["edited_text"] = edited_text
    _append_dataset(LABELS_FILE, rec)


# ── Public API ───────────────────────────────────

def media_gate_enabled() -> bool:
    _maybe_reload_settings()
    with _lock:
        return bool(_settings.get("media_gate"))


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
    is a cheap syntactic rule — no API call — so it runs before decide(). A bare
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


async def _decide_async(text: str, source: str = "") -> dict:
    global _last_received, _last_relayed
    _last_received = time.time()
    with _lock:
        _stats["checked"] += 1

    if not is_enabled():
        return {"relay": True, "reason": "AI filter disabled", "match": None, "dropped": None}

    candidates = [e["text"] for e in _store]
    item_id = text_id(text)
    try:
        result = await _call_typesafe(text, source, candidates)
    except Exception as e:
        with _lock:
            _stats["errors"] += 1
        log.warning("ai_filter TypeSafe error (fail-open): %s", e)
        return {"relay": True, "reason": f"TypeSafe error, relayed: {e}", "match": None,
                "dropped": "error", "id": item_id}

    if result["category"] != "news":
        outcome = "useless"
    elif result["dup_index"] is not None:
        outcome = "duplicate"
    else:
        outcome = "relay"
    _append_dataset(DECISIONS_FILE, {
        "ts": time.time(), "id": item_id, "source": source, "text": text,
        "model": get_settings().get("model"),
        "quality": {"choice": result["category"], "confidence": result["confidence"],
                    "probabilities": result["probabilities"]},
        "dup": [{"cand": text_id(c), "p": p} for c, p in zip(candidates, result["dup_probs"])],
        "outcome": outcome,
    })

    if result["category"] != "news":
        with _lock:
            _stats["dropped_useless"] += 1
        log.info("AI DROP (%s): src=%s", result["category"], source)
        return {"relay": False, "reason": result["category"], "match": None, "dropped": "useless", "id": item_id}

    if result["dup_index"] is not None:
        with _lock:
            _stats["dropped_dup"] += 1
        matched = candidates[result["dup_index"]]
        log.info("AI DROP (duplicate, p=%.2f): src=%s matched=%s",
                 result["dup_prob"], source, matched[:80])
        return {"relay": False, "reason": f"duplicate (p={result['dup_prob']:.2f})",
                 "match": matched, "dropped": "duplicate", "id": item_id}

    _remember(text)
    with _lock:
        _stats["relayed"] += 1
        _last_relayed = time.time()
    _flush_throttled()
    log.info("AI RELAY: src=%s", source)
    return {"relay": True, "reason": "news", "match": None, "dropped": None, "id": item_id}


async def decide(text: str, source: str = "") -> dict:
    """Run the AI gate with a hard wall-clock deadline.

    AsyncTypeSafeClient is asyncio-native, so this awaits directly on
    Telethon's event loop — no thread hop needed. asyncio.wait_for is still
    the enforced deadline regardless of what the SDK's own timeout does
    internally: a slow/hung call must only ever delay this one message, never
    freeze intake (seen in production before: a stalled request left the
    forwarder receiving no news for 2h). Fail-open: on timeout/error we RELAY.
    """
    deadline = get_settings().get("decide_timeout_seconds", 30)
    try:
        return await asyncio.wait_for(_decide_async(text, source), timeout=deadline)
    except Exception as e:
        with _lock:
            _stats["errors"] += 1
        log.warning("ai_filter decide timed out/failed (fail-open): %s", e)
        return {"relay": True, "reason": f"decide error, relayed: {e}", "match": None, "dropped": "error"}


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
