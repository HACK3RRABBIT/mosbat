"""
reports.py — Persian-language rejection notices and the periodic Jev report.

Everything is computed from dataset/*.jsonl (written by ai_filter.py), not from
in-memory counters, so a report still covers the whole window after a restart.
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter

import ai_filter

CATEGORY_FA = {
    "news": "خبر",
    "religious": "مذهبی / عرفانی",
    "ad": "تبلیغ",
    "self_promo": "تبلیغ کانال",
    "chatter": "حاشیه و گپ",
    "other": "بی‌محتوا / سایر",
}
EVENT_FA = {
    "link": "لینک‌دار (قبل از Jev)",
    "empty": "متن خالی (مثلاً فقط لینک)",
    "media": "رسانهٔ بدون کپشن (قبل از Jev)",
    "jev_error": "خطای Jev (بدون فیلتر منتشر شد)",
}

_FA_DIGITS = str.maketrans("0123456789.", "۰۱۲۳۴۵۶۷۸۹٫")


def fa(x) -> str:
    return str(x).translate(_FA_DIGITS)


def _read_since(path: str, since: float, until: float) -> list:
    rows = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if since <= r.get("ts", 0) < until:
                    rows.append(r)
    except FileNotFoundError:
        pass
    return rows


def rejection_notice(source_name: str, text: str, decision: dict) -> str:
    preview = (text or "").strip().replace("\n", " ")
    if len(preview) > 350:
        preview = preview[:350] + "…"
    if decision.get("dropped") == "duplicate":
        match = (decision.get("match") or "").strip().replace("\n", " ")[:160]
        why = f"🔁 **تکراری** (احتمال {fa(round(decision.get('dup_prob', 0), 2))})\n↩️ همان خبرِ: «{match}»"
    else:
        cat = decision.get("category", "")
        conf = decision.get("confidence")
        why = f"🏷 **{CATEGORY_FA.get(cat, cat)}**"
        if conf is not None:
            why += f" (اطمینان {fa(round(conf, 2))})"
    return f"🚫 **رد شد توسط Jev**\n**منبع:** {source_name}\n{why}\n\n{preview}"


def build_report(since: float, until: float, source_names: dict | None = None) -> str:
    source_names = source_names or {}
    decisions = _read_since(ai_filter.DECISIONS_FILE, since, until)
    events = _read_since(ai_filter.EVENTS_FILE, since, until)
    labels = _read_since(ai_filter.LABELS_FILE, since, until)

    outcomes = Counter(d.get("outcome") for d in decisions)
    cats = Counter(d["quality"]["choice"] for d in decisions if d.get("outcome") == "useless")
    ev = Counter(e.get("kind") for e in events)
    total = len(decisions) + ev["link"] + ev["media"] + ev["empty"]

    relayed = outcomes["relay"]
    dup = outcomes["duplicate"]
    useless = outcomes["useless"]
    confs = [d["quality"]["confidence"] for d in decisions]
    borderline = sum(1 for c in confs if c < 0.6)

    by_src = Counter(d.get("source", "") for d in decisions if d.get("outcome") == "relay")
    dup_ps = [max((c["p"] for c in d.get("dup", [])), default=0) for d in decisions if d.get("outcome") == "duplicate"]

    hours = round((until - since) / 3600)
    lines = [f"📊 **گزارش کارکرد Jev — {fa(hours)} ساعت اخیر**", ""]
    if total == 0:
        lines.append("در این بازه هیچ پیامی از کانال‌های منبع دریافت نشد.")
        return "\n".join(lines)

    lines += [
        f"📥 دریافتی: **{fa(total)}**",
        f"✅ منتشر شده: **{fa(relayed)}**",
        f"🔁 تکراری (رد شد): **{fa(dup)}**",
        f"🚫 نامرتبط (رد شد): **{fa(useless)}**",
    ]
    for cat, n in cats.most_common():
        lines.append(f"    • {CATEGORY_FA.get(cat, cat)}: {fa(n)}")
    pre = ev["link"] + ev["media"] + ev["empty"]
    if pre or ev["jev_error"]:
        lines.append(f"⏭ رد قبل از Jev: **{fa(pre)}**")
        for k in ("link", "media", "empty"):
            if ev[k]:
                lines.append(f"    • {EVENT_FA[k]}: {fa(ev[k])}")
        if ev["jev_error"]:
            lines.append(f"⚠️ {EVENT_FA['jev_error']}: **{fa(ev['jev_error'])}**")

    lines.append("")
    if confs:
        lines.append(f"🎯 میانگین اطمینان دسته‌بندی: {fa(round(sum(confs) / len(confs), 2))}"
                     + (f" — {fa(borderline)} مورد مرزی (زیر ۰٫۶)" if borderline else ""))
    if dup_ps:
        lines.append(f"🔁 میانگین احتمال تکراری‌ها: {fa(round(sum(dup_ps) / len(dup_ps), 2))}")
    if by_src:
        top = "، ".join(f"{source_names.get(s, s)} ({fa(n)})" for s, n in by_src.most_common(5))
        lines.append(f"📰 بیشترین منتشرشده: {top}")

    lab = Counter(l.get("label") for l in labels)
    if lab:
        lines.append(f"👤 بازبینی شما: تأیید {fa(lab['approved'] + lab['approved_edited'])}"
                     f"، رد {fa(lab['rejected'])}"
                     + (f" (ویرایش‌شده {fa(lab['approved_edited'])})" if lab['approved_edited'] else ""))
    return "\n".join(lines)


def next_slot(now: float, hours=(8, 20)) -> float:
    """Next local 08:00 or 20:00 after `now`."""
    lt = time.localtime(now)
    base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    for day in (0, 1):
        for h in hours:
            t = base + day * 86400 + h * 3600
            if t > now:
                return t
    return now + 12 * 3600
