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
    if not night_dates:
        return "unknown"
    # A date outside the scraped window ("Шахівниця" only shows a few weeks) is
    # NOT the same as sold out — we simply cannot confirm it.
    if any(d not in avail for d in night_dates):
        return "unknown"
    if all(avail.get(d, 0) > 0 for d in night_dates):
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
# Big groups (20+) and events must ALWAYS be redirected to the co-owner — too
# important to rely on fuzzy classification, so we detect it in code.
LARGE_GROUP_MIN = 20   # 20+ guests (or any event/banquet) => redirect to the co-owner
_GROUP_NUM_RE = re.compile(
    r"(\d{2,3})\s*[-–—+]?\s*\d{0,3}\s*(?:осіб|чол|людей|людин|дітей|діток|гостей|дорослих|учн)",
    re.IGNORECASE,
)
_EVENT_WORDS = [
    "весілл", "банкет", "корпоратив", "табір", "спортивні збори",
    "проведення заход", "відсвяткувати", "ювіле",
]


def looks_like_large_group(text: str) -> bool:
    """True if the conversation is clearly a 20+ group or an event/banquet."""
    t = (text or "").lower()
    for m in _GROUP_NUM_RE.finditer(t):
        try:
            if int(m.group(1)) >= LARGE_GROUP_MIN:
                return True
        except ValueError:
            pass
    return any(w in t for w in _EVENT_WORDS)


def slots_total_guests(slots) -> int:
    """Total guests across all rooms in the extracted slots (adults + children).
    Used to redirect 20+ bookings even when the count is split over fields/rooms
    ("10 дорослих і 12 дітей" => 22) and the text regex alone wouldn't catch it."""
    total = 0
    for r in (slots.get("rooms") or []):
        total += (r.get("adults") or 0)
        cc = r.get("children_count")
        total += cc if cc is not None else len(r.get("children_ages") or [])
    return total


# Directions ("how to get there") -> HOW_TO_GET_THERE, NOT the location/maps answer.
_DIRECTIONS_MARKERS = ["добра", "доїх", "дістат", "маршрут", "трансфер", "автобус",
                       "потяг", "залізн", "як до вас", "звідки їхати"]


def is_location_question(text: str) -> bool:
    """True for "where is the hotel?" (-> PLACE / maps). A top intent the LLM keeps
    mislabelling as GENERAL_INFORMATION, so we pin it deterministically. Excludes
    "how do I get there?" (that is HOW_TO_GET_THERE)."""
    t = (text or "").lower()
    if any(k in t for k in _DIRECTIONS_MARKERS):
        return False
    if "де" in t and ("знаход" in t or "розташ" in t):
        return True
    return any(k in t for k in ["адрес", "локац", "на карті", "де ви є", "де ви знаход"])


# FAQ intent must override slot-collection: a terse "собачка" / "харчування?" gets
# answered immediately, not after rounds of "which dates?". Maps keywords -> template.
_FAQ_OVERRIDE = [
    (["соба", "песик", "пёс", "пекінес", "тварин", "кіт", "котик", "кота", "улюбленц"], "PETS"),
    (["харчув", "сніданок", "сніданк", "їжа", "їсти", "поїсти", "меню", "обід", "годуєте", "перекус"], "FOOD_PRICES"),
    (["сауна", "чани", "чан ", "баня", "лазн"], "SAUNA_VATS"),
    (["трансфер", "парковк", "паркінг"], "TRANSFER_PARKING"),
    (["добра", "доїх", "дістат", "як до вас", "залізниц", "потяг", "електричк"], "HOW_TO_GET_THERE"),
    (["курит", "палит", "куріння", "паління"], "SMOKING"),
    (["басейн"], "POOL"),
]


def faq_override(text: str):
    """Return a fixed FAQ template name when the message is clearly one of the
    priority FAQs (location/pets/food/transport/…), else None. Used to answer the
    question immediately instead of continuing slot collection."""
    if is_location_question(text):
        return "PLACE"
    t = (text or "").lower()
    for keywords, template in _FAQ_OVERRIDE:
        if any(k in t for k in keywords):
            return template
    return None


# --- per-conversation slot memory (robust to extractor variance) ------------
# The extractor (a small LLM) sometimes DROPS a slot the client already gave when
# the next message switches topic (e.g. an FAQ). Rather than trust the LLM to
# re-consolidate every turn, Python remembers the booking slots per conversation
# and fills any field the fresh extraction left empty. New non-empty values always
# win; only empty/zero/None fields fall back to memory.
MERGE_FIELDS = ("checkin", "checkout", "fuzzy_date", "nights",
                "room_type", "adults", "children_count", "children_ages", "ubd")


def merge_room(remembered, fresh) -> Dict:
    """Fill MISSING booking fields in `fresh` from `remembered` so a dropped slot
    doesn't make the bot re-ask. Dates are only inherited when the fresh turn says
    NOTHING about dates (so a new/changed date request always overrides cleanly)."""
    out = dict(fresh or {})
    rem = remembered or {}
    fresh_mentions_dates = bool(out.get("checkin") or out.get("checkout") or out.get("fuzzy_date"))
    if not fresh_mentions_dates:
        for f in ("checkin", "checkout", "fuzzy_date", "nights"):
            if not out.get(f) and rem.get(f):
                out[f] = rem[f]
    for f in ("room_type", "adults", "children_count", "children_ages"):
        if not out.get(f) and rem.get(f):
            out[f] = rem[f]
    if not out.get("ubd") and rem.get("ubd"):
        out["ubd"] = rem["ubd"]
    return out


def remember_room(room) -> Dict:
    """Project a room dict down to the carried-over booking fields (for storage)."""
    return {f: (room or {}).get(f) for f in MERGE_FIELDS}


def merge_rooms(remembered_list, fresh_list) -> List[Dict]:
    """Multi-room slot memory: merge fresh rooms with remembered rooms BY INDEX. Each
    room's missing fields are filled from the same-index remembered room; remembered
    rooms the fresh turn didn't re-mention are PRESERVED (the bot must never forget a
    second/third room across a chit-chat or FAQ turn)."""
    rem = remembered_list or []
    fresh = fresh_list or []
    n = max(len(rem), len(fresh))
    out = []
    for i in range(n):
        f = fresh[i] if i < len(fresh) else {}
        r = rem[i] if i < len(rem) else {}
        out.append(merge_room(r, f))
    return out


def remember_rooms(rooms_list) -> List[Dict]:
    """Project a full multi-room booking down to carried-over fields (for storage)."""
    return [remember_room(r) for r in (rooms_list or [])]


# --- bare confirmations ("Так" / "Давайте") — handled by CONTEXT ------------
# A bare yes means different things depending on what the bot just said: accept the
# proposed dates, or proceed to payment. Detection is deterministic; the meaning is
# decided from the previous bot message (see bot_server).
_CONFIRM_WORDS = {
    "так", "ага", "ок", "окей", "окк", "добре", "гаразд", "давайте", "давай", "згоден",
    "згодна", "згода", "погоджуюсь", "погоджуюся", "підходить", "влаштовує", "беремо",
    "бронюю", "бронюємо", "бронюйте", "оформлюйте", "оформлюємо", "оформляйте", "ok", "yes",
}


def is_bare_confirmation(text: str) -> bool:
    """True when the message is essentially just an affirmation ("так", "давайте",
    "добре, бронюємо") with no new data (no digits). Combined with the previous bot
    message in bot_server to decide what the client is agreeing to."""
    t = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in (text or "").lower())
    t = t.strip()
    if not t or any(c.isdigit() for c in t):
        return False
    words = t.split()
    if not words or len(words) > 4:
        return False
    return any(w in _CONFIRM_WORDS for w in words)


def is_quote_message(text: str) -> bool:
    """True if the bot's previous message was a price quote that ended with
    'Бажаєте забронювати?' — so a bare 'Так' means: proceed to payment."""
    t = (text or "").lower()
    return "буде вартувати" in t or "загальна вартість" in t


def is_window_offer_message(text: str) -> bool:
    """True if the bot's previous message proposed concrete free date window(s) — so a
    bare 'Так' means: accept the FIRST proposed window."""
    t = (text or "").lower()
    return "вільні віконця" in t or "найближче вільне віконце" in t


def has_booking_context(slots) -> bool:
    """Fix 3 — True if the slots already carry ANY booking data (dates, fuzzy period,
    room, nights, or guests). An FAQ answered mid-booking must preserve this state,
    so the bot can resume by asking only what is still missing — never from scratch.
    """
    for r in slots.get("rooms") or []:
        if (r.get("checkin") or r.get("checkout") or r.get("fuzzy_date")
                or r.get("room_type") or r.get("nights")
                or (r.get("adults") or 0) >= 1
                or r.get("children_count") or r.get("children_ages")):
            return True
    return False


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


# --- payment hand-off & bot muting ------------------------------------------
# The bot must NEVER auto-confirm a booking (it cannot verify a real transfer vs a
# fake screenshot). A payment signal -> hand off to a human admin + tag + mute.
ORDER_LABEL = "Замовлено"                 # Chatwoot label that mutes the bot
MUTE_LABELS = [ORDER_LABEL]               # any of these means a human has taken over

# Fix 2: a completely unrecognized intent is the ONLY thing handed to a manager
# (date searches never are). Such conversations are tagged for human follow-up.
INSTAGRAM_LABEL = "Instagram"

# Completed-action signals only — NOT the bare noun "оплата" (a client ASKING
# "Яка оплата?" must not be mistaken for a submitted payment).
PAYMENT_KEYWORDS = [
    "оплатив", "оплатила", "сплатив", "сплатила", "оплачено",
    "скинув", "скинула", "скидаю", "надіслав оплату", "відправив оплату",
    "квитанц", "чек", "готово", "перерахував", "перерахувала", "переказав",
    "переказала", "переказ", "скрін", "screenshot", "завдаток вніс", "аванс вніс",
]


def is_payment_intent(text: str, has_attachment: bool = False) -> bool:
    """True when the client is submitting a payment — an attachment (screenshot)
    or a payment keyword. Triggers the human hand-off (never an auto-confirmation)."""
    if has_attachment:
        return True
    t = (text or "").lower()
    return any(k in t for k in PAYMENT_KEYWORDS)


def is_muted(labels) -> bool:
    """True if a human admin has taken over this conversation (mute label present)."""
    return any(lbl in (labels or []) for lbl in MUTE_LABELS)


def prepend_greeting_if_needed(clean_message: str, bot_has_spoken: bool) -> str:
    """On the bot's very first message, guarantee the greeting + [SPLIT] prefix."""
    if not bot_has_spoken and GREETING_MARKER not in clean_message:
        return GREETING + clean_message
    return clean_message


def split_messages(clean_message: str) -> List[str]:
    """Split on the [SPLIT] marker into the non-empty chunks to send in sequence."""
    return [part.strip() for part in (clean_message or "").split("[SPLIT]") if part.strip()]
