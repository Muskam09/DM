"""
bot_logic.py — Pure, dependency-free helpers extracted from bot_server.py.

These functions hold the deterministic, side-effect-free decision logic of the
bot (availability filtering & gating, spam/phone detection, message splitting,
greeting injection). Keeping them here lets the automated test suite assert the
bot's behaviour without importing FastAPI / google-genai / Playwright, and
guarantees the live server and the tests exercise the very same code.
"""

from __future__ import annotations

import re
from typing import Dict, List

# Categories that must NEVER be offered to a client as a bookable room.
IGNORE_CATEGORIES = ["Колиба", "Басейн", "Overbooking"]

GREETING = (
    "Доброго дня! Вас вітає D&T Hotel ⛰\n"
    "Раді, що зацікавились нашим готелем!\n"
    "[SPLIT]\n"
)
# Stable marker used to detect that the greeting is already present in a reply.
GREETING_MARKER = "Доброго дня! Вас вітає D&T Hotel"


def build_simplified_availability(
    availability_data: Dict, ignore_categories: List[str] = IGNORE_CATEGORIES
) -> Dict:
    """Reduce raw scraper output to ``{room_type: {date: free_count}}``, dropping
    any blacklisted category (Колиба / Басейн / Overbooking) so the bot can never
    propose them as a room. ``free_count`` is the per-date "total_available" map
    produced by the scraper.
    """
    simplified: Dict = {}
    for room_type, r_data in (availability_data or {}).items():
        if any(ignore.lower() in room_type.lower() for ignore in ignore_categories):
            continue
        if isinstance(r_data, dict) and "total_available" in r_data:
            simplified[room_type] = r_data["total_available"]
    return simplified


def free_room_types(simplified: Dict, night_dates: List[str]) -> List[str]:
    """Room types that have a free room (count > 0) on EVERY requested night.

    `simplified` is the output of build_simplified_availability; `night_dates` are
    the nights to stay (the checkout day is excluded by the caller). A room with a
    `0` on any night is treated as booked (Cases 4/5/8).
    """
    if not night_dates:
        return []
    return [
        room_type
        for room_type, avail in (simplified or {}).items()
        if all(avail.get(d, 0) > 0 for d in night_dates)
    ]


def is_sold_out(simplified: Dict, night_dates: List[str]) -> bool:
    """True when NO room type is free on all requested nights (full sold-out, Case 5)."""
    return len(free_room_types(simplified, night_dates)) == 0


def _norm_room(s: str) -> str:
    return "".join((s or "").lower().split())


def match_availability_key(simplified: Dict, room_type: str):
    """Find the availability key matching a requested room type (handles spacing/
    case, then a lenient substring match). Returns the key or None."""
    if not simplified or not room_type:
        return None
    if room_type in simplified:
        return room_type
    target = _norm_room(room_type)
    for key in simplified:                       # exact, normalised
        if _norm_room(key) == target:
            return key
    for key in simplified:                       # lenient substring
        nk = _norm_room(key)
        if target and (target in nk or nk in target):
            return key
    return None


def is_room_available(simplified: Dict, room_type: str, night_dates: List[str]) -> str:
    """Availability of a specific room over the requested nights.

    Returns 'available' (free every night), 'sold_out' (booked on >=1 night), or
    'unknown' (room not found in the scraped data). Used to GATE pricing: the bot
    must never quote a 'sold_out' room.
    """
    key = match_availability_key(simplified, room_type)
    if key is None:
        return "unknown"
    avail = simplified.get(key) or {}
    if night_dates and all(avail.get(d, 0) > 0 for d in night_dates):
        return "available"
    return "sold_out"


# --- B2B spam detection -----------------------------------------------------
# Markers of vendor/outreach DMs (sticker shops, chatbot/SMM/targeting sellers,
# content agencies, scams). When detected the bot stays SILENT — no Chatwoot reply.
SPAM_MARKERS = [
    "стікер", "чат-бот", "чатбот", "чат бот", "таргетолог", "таргетинг",
    "smm", "сммщик", "просування сторінк", "просуванні сторінк", "накрутк",
    "пробний тариф", "рекламні макет", "контент для соцмереж", "візуальний контент",
    "ведення сторінк", "ведення інстаграм", "ведення профіл", "холодний трафік",
    "розробка сайт", "розробку сайт", "веб-сайт", "комерційну пропозицію",
    "комерційна пропозиція", "автоматизуйте", "автоматизація сервіс",
    "лідогенерац", "збільшити продаж", "залучення клієнт", "співпрацю по бартер",
    "пропоную співпрацю", "інвестиц", "заробіток від", "криптовалют", "казино",
]


def is_spam(text: str) -> bool:
    """Detect B2B outreach / spam so the bot can ignore it entirely (no reply)."""
    t = (text or "").lower()
    return any(marker in t for marker in SPAM_MARKERS)


# --- large-group / event detection (deterministic, not left to the LLM) -----
# Big groups (40+) and events must ALWAYS be redirected to the co-owner — too
# important to rely on fuzzy classification, so we detect it in code.
_GROUP_NUM_RE = re.compile(
    r"(\d{2,3})\s*[-–—+]?\s*\d{0,3}\s*(?:осіб|чол|людей|людин|дітей|діток|гостей|дорослих|учн)",
    re.IGNORECASE,
)
_EVENT_WORDS = [
    "весілл", "банкет", "корпоратив", "табір", "спортивні збори",
    "проведення заход", "відсвяткувати", "ювіле",
]


def looks_like_large_group(text: str) -> bool:
    """True if the conversation is clearly a 40+ group or an event/banquet."""
    t = (text or "").lower()
    for m in _GROUP_NUM_RE.finditer(t):
        try:
            if int(m.group(1)) >= 40:
                return True
        except ValueError:
            pass
    return any(w in t for w in _EVENT_WORDS)


# --- phone-number capture ---------------------------------------------------
# A customer who leaves a phone number is handed to a human (reply PHONE_RECEIVED,
# stop). Match a token with >= 9 digits (UA: 0XXXXXXXXX / +380XXXXXXXXX), which
# excludes dates ("10-15") and prices ("2400 грн").
_PHONE_RE = re.compile(r"\+?\d[\d\-\s()]{7,}\d")


def contains_phone_number(text: str) -> bool:
    for match in _PHONE_RE.finditer(text or ""):
        if 9 <= sum(ch.isdigit() for ch in match.group()) <= 13:
            return True
    return False


def prepend_greeting_if_needed(clean_message: str, bot_has_spoken: bool) -> str:
    """On the bot's very first message, guarantee the greeting + [SPLIT] prefix."""
    if not bot_has_spoken and GREETING_MARKER not in clean_message:
        return GREETING + clean_message
    return clean_message


def split_messages(clean_message: str) -> List[str]:
    """Split on the [SPLIT] marker into the non-empty chunks to send in sequence."""
    return [part.strip() for part in (clean_message or "").split("[SPLIT]") if part.strip()]
