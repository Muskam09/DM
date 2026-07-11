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
from datetime import date
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


# The client ever sees ONLY these 3 public room categories. OtelMS exposes granular
# internal sub-types ("Стандарт сімейний В+Д", "Стандарт 4х 2Л + Д", "Стандарт +
# Сімейний В+Д"…) which must NEVER leak into a reply — they are folded into the public
# type below.
PUBLIC_ROOM_TYPES = ("Стандарт", "Стандарт +", "Напівлюкс")


def public_room_type(name: str):
    """Map any OtelMS category name to its public type, or None if it's not offerable.
    '+' is checked before the bare 'Стандарт' so 'Стандарт + Сімейний' -> 'Стандарт +'."""
    n = "".join((name or "").lower().split())
    if n.startswith("напівлюкс") or n.startswith("напивлюкс"):
        return "Напівлюкс"
    if n.startswith("стандарт+"):
        return "Стандарт +"
    if n.startswith("стандарт"):
        return "Стандарт"
    return None


def build_simplified_availability(
    availability_data: Dict, ignore_categories: List[str] = IGNORE_CATEGORIES
) -> Dict:
    """Reduce raw scraper output to ``{public_type: {date: free_count}}``. Blacklisted
    categories (Колиба / Басейн / Overbooking) are dropped, and every OtelMS sub-type is
    FOLDED into one of the 3 public categories (`public_room_type`) with per-date counts
    SUMMED — so internal names never reach the client and Стандарт-class availability is
    aggregated. ``free_count`` is the per-date "total_available" map from the scraper.
    """
    simplified: Dict = {}
    for room_type, r_data in (availability_data or {}).items():
        if any(ignore.lower() in room_type.lower() for ignore in ignore_categories):
            continue
        if not (isinstance(r_data, dict) and "total_available" in r_data):
            continue
        pub = public_room_type(room_type)
        if not pub:
            continue
        dest = simplified.setdefault(pub, {})
        for d, cnt in (r_data["total_available"] or {}).items():
            dest[d] = dest.get(d, 0) + (cnt or 0)
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
        if not target:
            continue
        # NEVER cross-match "Стандарт" and "Стандарт +" — they are DISTINCT public types
        # ("стандарт" is a substring of "стандарт+"). Without this guard, a scrape holding only
        # "Стандарт" would report "Стандарт +" as available and the bot would blindly recommend
        # it (owner fix #275: only recommend types actually confirmed in the calendar).
        if ("+" in target) != ("+" in nk):
            continue
        if target in nk or nk in target:
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
        # The room category is entirely absent from the scrape (e.g. a total scraper
        # failure) — we genuinely cannot confirm; stay lenient rather than block.
        return "unknown"
    avail = simplified.get(key) or {}
    if not night_dates:
        return "unknown"
    # Owner rule (2026-06-24): a date MISSING from a scraped room's calendar defaults to
    # SOLD OUT (count 0), never "available" — never quote a date OtelMS didn't confirm free.
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


# --- blogger / barter / PR collab detection ---------------------------------
# Unlike spam, the hotel WANTS these deals (free stays for bloggers, взаємопіар).
# The bot must NOT answer (a human negotiates), but it MUST tag the conversation
# Instagram so the operator follows up — so barter is detected BEFORE spam and
# routed to a silent hand-off-with-label (see bot_server).
BARTER_MARKERS = [
    "бартер", "блогер", "блогерк", "рілс", "reels", "взаємопіар", "взаємний піар",
    "колаборац", "співпраця за проживання", "огляд за проживання", "проживання за рекламу",
    "реклама за проживання", "запросити на огляд", "знімаю контент за", "обмін на проживання",
]


def is_barter(text: str) -> bool:
    """Detect a blogger/PR/barter collaboration pitch (wanted, but human-handled)."""
    t = (text or "").lower()
    return any(marker in t for marker in BARTER_MARKERS)


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
    if "де" in t and ("знаход" in t or "розташ" in t or "готель" in t):
        return True
    return any(k in t for k in ["адрес", "локац", "на карті", "де ви є", "де ви знаход",
                                "розташув", "де готель", "де ваш готель", "в якому місці"])


# FAQ intent must override slot-collection: a terse "собачка" / "харчування?" gets
# answered immediately, not after rounds of "which dates?". Maps keywords -> template.
# NB: "басейн" is handled separately (see _pool_template) — it splits into two FAQs.
_FAQ_OVERRIDE = [
    (["соба", "песик", "пёс", "пекінес", "тварин", "кіт", "котик", "кота", "улюбленц"], "PETS"),
    (["харчув", "сніданок", "сніданк", "їжа", "їсти", "поїсти", "меню", "обід", "годуєте", "перекус"], "FOOD_PRICES"),
    (["сауна", "чани", "чан ", "баня", "лазн"], "SAUNA_VATS"),
    (["трансфер", "парковк", "паркінг"], "TRANSFER_PARKING"),
    (["добра", "доїх", "дістат", "як до вас", "залізниц", "потяг", "електричк"], "HOW_TO_GET_THERE"),
    (["курит", "палит", "куріння", "паління"], "SMOKING"),
    (["фен", "фена", "феном"], "HAIRDRYER"),
    (["фото", "відео", "світлин", "знімк"], "MEDIA"),
    (["що входить", "входить у вартість", "входить у ціну", "що включ", "включено у вартіст",
      "що входе"], "INCLUDED_IN_THE_PRICE"),
]


# A "басейн" question splits into TWO different FAQs and the small LLM kept collapsing
# both into POOL, so we disambiguate deterministically:
#   GUEST_POOL — price for NON-residents / a day-visit / swimming without staying
#                ("ціна басейну", "покупатись у басейні", "приїхати на басейн на день").
#   POOL       — the pool as an amenity for guests ("чи є басейн", "він з підігрівом?",
#                "графік роботи", "розмір/глибина").
_GUEST_POOL_MARKERS = [
    "покупат", "купат", "поплава", "скупат", "купанн",
    "не прожива", "без прожива", "не проживаюч", "тільки басейн", "лише басейн", "лиш басейн",
    "відвідат", "відвідуван", "на день",
    "приїхати в басейн", "приїхати на басейн", "приїхати до басейн", "приїхати скупат",
    "прийти в басейн", "прийти на басейн", "прийти до басейн",
    "просто в басейн", "просто на басейн", "просто скупат",
]
# "ціна/вартість басейну" as a service => GUEST_POOL; BUT "чи входить басейн у вартість
# (проживання)" is the amenity answer => POOL (so we exclude "вход…").
_POOL_PRICE_MARKERS = ["ціна", "цін", "вартіст", "скільки кошт", "почому", "по чому", "скільки за"]


def _pool_template(t: str) -> str:
    """Disambiguate a 'басейн' question into GUEST_POOL (non-resident day-visit / price to
    swim) vs POOL (general amenity). `t` is the already-lowercased message text."""
    if any(m in t for m in _GUEST_POOL_MARKERS):
        return "GUEST_POOL"
    if any(m in t for m in _POOL_PRICE_MARKERS) and "вход" not in t:
        return "GUEST_POOL"
    return "POOL"


# A CHILDREN'S pool question ("чи є дитячий басейн?", "басейн для дітей") gets its own
# answer (size 3x2, depth 30 cm, water from 28°C) — owner fix #278. Detected when the pool
# question is explicitly about a CHILDREN'S pool (not the general/adult pool).
_CHILD_POOL_MARKERS = ["дитячий басейн", "дитячого басейн", "дитячим басейн", "дитячі басейн",
                       "дитбасейн", "басейн для дітей", "басейн для діток", "басейн для малеч",
                       "дитячий бесейн", "дитячого бесейн"]


def is_children_pool_question(text: str) -> bool:
    """True for a question specifically about the CHILDREN'S pool (owner fix #278)."""
    t = (text or "").lower()
    return any(m in t for m in _CHILD_POOL_MARKERS)


# Persona 17: "чи можна приходити зі своїм [їжею/напоями]" -> NO (we have a kolyba/bar).
_OWN_FOOD_MARKERS = [
    "зі своїм", "зі своєю", "зі своїми", "свою їжу", "своєю їжею", "своя їжа", "своєї їжі",
    "свої напої", "своїми напоями", "свій алкоголь", "своє спиртне", "власну їжу",
    "власною їжею", "власні напої", "власний алкоголь", "приносити своє", "принести своє",
    "принести свою", "приходити зі своїм", "прийти зі своїм",
]


def is_outside_food_question(text: str) -> bool:
    """True for "can we bring our own food/drinks?" (owner: NO — we have a kolyba/bar).
    Excludes pet ("зі своїм собакою") and car ("зі своїм авто") so those aren't hijacked."""
    t = (text or "").lower()
    if mentions_pet(t) or "авто" in t or "машин" in t or "паркув" in t:
        return False
    return any(m in t for m in _OWN_FOOD_MARKERS)


# Persona 17: "вартість, якщо не будемо плавати, а просто посидіти" -> same price (entrance
# to the territory / recreation area, not per-swim).
_POOL_SIT_MARKERS = [
    "не будем плава", "не будемо плава", "не буду плава", "не плаваю", "не плаватим",
    "не будем купат", "не будемо купат", "не буду купат", "не купатись", "не купатися",
    "не купаємось", "не купаюсь", "без купання", "не хочемо плавати", "не хочу плавати",
    "просто посидіти", "просто посидим", "просто посидіть", "просто відпочи", "лише посидіти",
]


def is_pool_sit_question(text: str) -> bool:
    """True for "how much if we don't swim, just sit?" (owner: same price — entrance fee)."""
    t = (text or "").lower()
    return any(m in t for m in _POOL_SIT_MARKERS)


# Persona 18: any Booking.com question -> the bot can't help, hand to a human (+ Instagram tag).
def is_booking_com_question(text: str) -> bool:
    """True for a Booking.com-related question (reservation/prepayment made on Booking.com)."""
    t = (text or "").lower()
    return "booking" in t or "букінг" in t or "букинг" in t


# Owner 2026-07-10: "Які там страви подають?" / "яке меню?" asks about the DISHES, not the price.
_MENU_MARKERS = ["страв", "що готують", "які страви", "яке меню", "меню яке", "що подають",
                 "чим годують", "склад меню", "меню"]
_MENU_PRICE_WORDS = ["скільки кошт", "вартіст", "ціна", "по чому", "почому", "прайс"]


def is_menu_question(text: str) -> bool:
    """True for a question about WHICH DISHES are served (menu content). A price word ("вартість
    меню") flips it back to the FOOD_PRICES list."""
    t = (text or "").lower()
    if not any(m in t for m in _MENU_MARKERS):
        return False
    return not any(w in t for w in _MENU_PRICE_WORDS)


_MEAL_ANY = ("харчув", "разове", "разов харч", "повне харч", "сніданок", "обід", "вечер")


def parse_meal_request(text: str):
    """Deterministically extract a meal-cost request from ONE message (owner 2026-07-10) — food
    math must not depend on the LLM. Returns a meals dict or None. Handles the owner's phrasing:
    "3-разове на 4 особи: 2 дні повне харчування, а останній день лише сніданок" ->
    {persons:4, three_meals_days:2, breakfast_days:1, ...}."""
    t = (text or "").lower()
    if not any(w in t for w in _MEAL_ANY):
        return None
    m = {"persons": None, "three_meals_days": 0, "two_meals_days": 0,
         "breakfast_days": 0, "lunch_days": 0, "dinner_days": 0}
    mp = re.search(r"на\s+(\d+)\s*(?:особ|осіб|людин|гост|дорослих|чол)", t)
    if mp:
        m["persons"] = int(mp.group(1))
    _DAY = r"(?:дн\w*|діб|доб|день)"
    for pat in (r"(\d+)\s*" + _DAY + r"[^\d]{0,25}(?:повн\w*\s*харч|3[-\s]?разов|триразов)",
                r"(?:повн\w*\s*харч|3[-\s]?разов|триразов)\w*[^\d]{0,20}(\d+)\s*" + _DAY):
        for g in re.finditer(pat, t):
            m["three_meals_days"] += int(g.group(1))
    for pat in (r"(\d+)\s*" + _DAY + r"[^\d]{0,25}(?:2[-\s]?разов|дворазов)",
                r"(?:2[-\s]?разов|дворазов)\w*[^\d]{0,20}(\d+)\s*" + _DAY):
        for g in re.finditer(pat, t):
            m["two_meals_days"] += int(g.group(1))
    if re.search(r"(?:лише|тільки|лиш|тілько)\s+сніданок", t) or re.search(r"останн\w*\s+день\s+сніданок", t):
        m["breakfast_days"] += 1
    if not any(m[k] for k in ("three_meals_days", "two_meals_days", "breakfast_days",
                              "lunch_days", "dinner_days")):
        return None
    return m


# --- deterministic date parsing (fallback when the LLM drops a date) --------
# Dates normally come from the LLM extractor, but it sometimes DROPS a date a client sent right
# after a group-split proposal ("З 15 чи 16 липня") — leaving the bot to silently re-propose the
# SAME split, which the anti-spam guard then suppresses (silence — the owner's Sprint-4 Test 21
# bug). This PURE parser is a safety net: it fires ONLY on an explicit month name or a dotted date
# (never a bare number, so it can't misfire on prices/ages/counts), so bot_server can fill in a
# check-in the extractor missed. Bookings are 2026 (any month parses; only summer is priced later).
_DATE_YEAR = 2026

# Longest stems first so "серпн" wins before "серп", etc. Genitive + short forms cover "15 липня",
# "1 серпня", the "лип"/"серп" abbreviations, etc.
_UA_MONTH_STEMS = [
    ("січн", 1), ("січ", 1), ("лютог", 2), ("лют", 2), ("березн", 3), ("берез", 3),
    ("квітн", 4), ("квіт", 4), ("травн", 5), ("трав", 5), ("червн", 6), ("черв", 6),
    ("липн", 7), ("лип", 7), ("серпн", 8), ("серп", 8), ("вересн", 9), ("верес", 9),
    ("жовтн", 10), ("жовт", 10), ("листопад", 11), ("грудн", 12), ("груд", 12),
]


def _month_from_word(word: str):
    w = (word or "").lower()
    for stem, mon in _UA_MONTH_STEMS:
        if w.startswith(stem):
            return mon
    return None


def _safe_iso(day: int, month: int, year: int = _DATE_YEAR):
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


# A trailing ordinal suffix ("1-го", "15 го", "20 числа") sits between the day number and the
# range connector; tolerate it so "з 1 го по 11 серпня" parses as 1 → 11.
_ORD = r"(?:\s*-?\s*(?:го|е|му|числа))?"


def parse_date_request(text, year: int = _DATE_YEAR):
    """Best-effort deterministic check-in (and, when stated, check-out) from ONE message.

    Fires ONLY on an explicit month name ("15 липня", "15 чи 16 липня", "з 13 по 17 липня") or a
    dotted date ("13.07", "13.07-17.07", "24-26.07") — never a bare number — so it is a SAFE
    fallback for a date the LLM extractor dropped right after a group-split proposal. A CHOICE
    ("15 чи 16 липня") returns the earliest option as the check-in (check-out is derived later from
    the remembered nights). Returns {"checkin", "checkout"?} (ISO) or None.
    """
    t = (text or "").lower()

    # 1) day-range with a DOTTED month: "24-26.07(.26)" -> 24..26 of month 07. The first day must
    #    NOT be the tail of an already-dotted date (so "13.07-17.07" is handled by the dotted rule,
    #    not mis-read as "07-17.07").
    m = re.search(r"(?<![\d.])(\d{1,2})\s*[-–—]\s*(\d{1,2})\.(\d{1,2})(?:\.\d{2,4})?", t)
    if m:
        ci = _safe_iso(int(m.group(1)), int(m.group(3)), year)
        co = _safe_iso(int(m.group(2)), int(m.group(3)), year)
        if ci:
            out = {"checkin": ci}
            if co and co > ci:
                out["checkout"] = co
            return out

    # 2) dotted date, optional dotted range: "13.07-17.07" / "20.07" / "20.07.26".
    dotted = re.findall(r"\b(\d{1,2})\.(\d{1,2})(?:\.\d{2,4})?", t)
    if dotted:
        ci = _safe_iso(int(dotted[0][0]), int(dotted[0][1]), year)
        co = _safe_iso(int(dotted[1][0]), int(dotted[1][1]), year) if len(dotted) > 1 else None
        if ci:
            out = {"checkin": ci}
            if co and co > ci:
                out["checkout"] = co
            return out

    # 3) month-name based, two days joined by a connector: range ("13-17"/"з 13 по 17") sets a
    #    check-out; a CHOICE ("15 чи 16") gives only the earliest check-in.
    m = re.search(r"(\d{1,2})" + _ORD + r"\s*(?:[-–—]|по|чи|або|/|,)\s*(\d{1,2})" + _ORD
                  + r"\s+([а-яіїєґ']+)", t)
    if m and _month_from_word(m.group(3)) is not None:
        mon = _month_from_word(m.group(3))
        d1, d2, sep = int(m.group(1)), int(m.group(2)), m.group(0)
        ci = _safe_iso(d1, mon, year)
        if ci:
            out = {"checkin": ci}
            if (("-" in sep) or ("–" in sep) or ("—" in sep) or ("по" in sep)) and d2 > d1:
                co = _safe_iso(d2, mon, year)
                if co:
                    out["checkout"] = co
            return out

    # 4) a single "<day> <month>" ("15 липня", "з 20 серпня").
    m = re.search(r"(\d{1,2})" + _ORD + r"\s+([а-яіїєґ']+)", t)
    if m and _month_from_word(m.group(2)) is not None:
        ci = _safe_iso(int(m.group(1)), _month_from_word(m.group(2)), year)
        if ci:
            return {"checkin": ci}
    return None


# --- multi-room total-vs-per-room stabiliser (Bug 3, owner Sprint 5) --------------------------
# The extractor flip-flops between "2 Стандарт for 4 adults TOTAL" (2+2) and "4 adults per room"
# (4+4=8), which trips a bogus over-capacity ROOM_TOO_SMALL. Deterministically parse the stated
# ROOMS count and the stated TOTAL adults (digits OR Ukrainian number words) and redistribute.
_NUM_WORD = {
    "один": 1, "одного": 1, "одна": 1, "одному": 1,
    "два": 2, "дві": 2, "двох": 2, "двоє": 2, "двома": 2, "вдвох": 2,
    "три": 3, "трьох": 3, "троє": 3, "трьома": 3, "втрьох": 3,
    "чотири": 4, "чотирьох": 4, "четверо": 4, "чотирма": 4, "вчотирьох": 4,
    "п'ять": 5, "пять": 5, "п’ять": 5, "п'ятьох": 5, "пятьох": 5, "п'ятеро": 5, "пятеро": 5,
    "шість": 6, "шести": 6, "шістьох": 6, "шестеро": 6,
    "сім": 7, "семи": 7, "сімох": 7, "семеро": 7,
}
_NUM_ALT = "|".join(sorted((re.escape(w) for w in _NUM_WORD), key=len, reverse=True))
_ROOMS_COUNT_RE = re.compile(r"(\d+|" + _NUM_ALT + r")\s*(?:номер\w*|кімнат\w*)", re.IGNORECASE)
_ADULTS_TOTAL_RE = re.compile(
    r"(\d+|" + _NUM_ALT + r")[\s\-–—]*(?:х|ти|ьох|ох|и)?\s*(?:дорослих|дорослі|осіб|особи|людей|гост)",
    re.IGNORECASE)
_ADULTS_FOR_RE = re.compile(r"\bна\s+(\d+|двох|трьох|чотирьох|п'ятьох|пятьох|шістьох|сімох)", re.IGNORECASE)
# An EXPLICIT per-room breakdown ("в одному 4, в іншому 3", "2 номери: 3 і 2", "по 4 в кожному") must
# be respected verbatim — never redistributed. ("по" is gated to "по N осіб/в кожному" so a DATE like
# "з 13 по 17 липня" is not mistaken for a per-room split.)
_PER_ROOM_SPLIT_RE = re.compile(
    r"в одному\s*\d|в іншому\s*\d|в другому\s*\d|в першому\s*\d"
    r"|по\s*\d+\s*(?:в кожн|особ|дорослих|осіб|гост)"
    r"|номер\w*\s*[:—\-]\s*\d+\s*(?:і|та|,)\s*\d+",
    re.IGNORECASE)


def _int_token(tok):
    tok = (tok or "").strip().lower()
    return int(tok) if tok.isdigit() else _NUM_WORD.get(tok)


def _explicit_rooms_count(text):
    m = _ROOMS_COUNT_RE.search(text or "")
    return _int_token(m.group(1)) if m else None


def _explicit_adults_total(text):
    m = _ADULTS_TOTAL_RE.search(text or "")
    if m:
        return _int_token(m.group(1))
    m2 = _ADULTS_FOR_RE.search(text or "")
    return _int_token(m2.group(1)) if m2 else None


def normalize_rooms_for_total(rooms, text):
    """Stabilise "N rooms for M adults TOTAL". Reads an explicit ROOMS count ("2 номери", "два
    номери") and an explicit TOTAL adults ("4 дорослих", "четверо", "на чотирьох") from `text` and
    rebuilds exactly N pure-adult rooms summing to M. Fires ONLY when: no children, no explicit
    per-room breakdown, N>=2, M>=N, and the current rooms don't already equal (N rooms summing to M).
    So a real per-room split ("в одному 4, в іншому 3") and family bookings are never touched.
    Pass CLIENT-side text only (never bot messages — PRESENTATION_ROOMS mentions "3 дорослих")."""
    rooms = [r for r in (rooms or []) if isinstance(r, dict)]
    if _PER_ROOM_SPLIT_RE.search(text or ""):
        return rooms
    n_rooms = _explicit_rooms_count(text)
    total = _explicit_adults_total(text)
    if not n_rooms or not total or n_rooms < 2 or n_rooms > 8 or total < n_rooms:
        return rooms
    if any(r.get("children_ages") or r.get("children_count") for r in rooms):
        return rooms
    if len(rooms) == n_rooms and sum((r.get("adults") or 0) for r in rooms) == total:
        return rooms
    base = rooms[0] if rooms else {}
    shared = {k: base.get(k) for k in ("checkin", "checkout", "nights", "fuzzy_date", "ubd")}
    rt = base.get("room_type")
    per = [total // n_rooms + (1 if i < total % n_rooms else 0) for i in range(n_rooms)]
    return [{**shared, "room_type": rt, "adults": a, "children_count": 0, "children_ages": []}
            for a in per]


# --- Bug 2 (owner Sprint 5): never scrape / claim "booked" without a COMPLETE, explicit stay ----
# A vague availability probe ("чи є вільні місця", "ще є місця") or a nights-RANGE ("днів 4-5") is
# NOT a bookable stay — the bot must ask for exact dates / offer windows, never claim "booked".
_VAGUE_PROBE_MARKERS = [
    "чи є вільн", "ще є вільн", "є вільні місц", "є вільне місц", "є вільні номер", "є вільний номер",
    "є місця", "є місце", "чи є місц", "маєте вільн", "маєте щось", "щось вільн", "є щось вільн",
    "залишились вільн", "залишилися вільн", "чи є щось", "чи вільн",
]
_NIGHTS_RANGE_RE = re.compile(
    r"\b\d{1,2}\s*[-–—]\s*\d{1,2}\s*(?:дн|діб|доб|ноч|ніч)|(?:дн\w*|діб|доб|ноч\w*|ніч)\w*\s*\d{1,2}\s*[-–—]\s*\d{1,2}",
    re.IGNORECASE)


def is_vague_availability_probe(text: str) -> bool:
    """True for a vague "is there availability?" probe with no committed exact dates."""
    t = (text or "").lower()
    return any(m in t for m in _VAGUE_PROBE_MARKERS)


def mentions_nights_range(text: str) -> bool:
    """True when the client's stay length is a RANGE ("днів 4-5", "3-5 діб") — not a definite stay."""
    return bool(_NIGHTS_RANGE_RE.search(text or ""))


def has_committed_stay(text: str) -> bool:
    """True when the client committed a DEFINITE stay somewhere in `text`: 'з N по N', an explicit
    DATE range ("24-26.07", "17-19 липня" — a "N-M" NOT adjacent to a nights word), OR an explicit
    single nights ("на 5 ночей"). A lingering nights-RANGE ("Днів 4-5") does NOT count (owner Sprint 5,
    Bug 2). This is text-only; the caller also treats a single integer `nights` slot as committed."""
    t = (text or "").lower()
    if re.search(r"\bз\s+\d{1,2}\s+по\s+\d{1,2}", t):
        return True
    if re.search(r"\bна\s+\d{1,2}\s*(?:ноч\w*|діб|доб)", t):        # explicit "на N ночей/діб"
        return True
    for m in re.finditer(r"\d{1,2}\s*[-–—]\s*\d{1,2}", t):          # a "N-M" that is a DATE range
        before = t[max(0, m.start() - 9):m.start()]
        after = t[m.end():m.end() + 9]
        near_nights = bool(re.search(r"(?:дн|діб|доб|ноч|ніч)\w*\s*$", before)
                           or re.match(r"\s*(?:дн|діб|доб|ноч|ніч)", after))
        if not near_nights:
            return True
    return False


# --- Bug 1 (owner Sprint 5): real bed-configuration availability from the RAW per-sub-type scrape ---
# Owner-CONFIRMED OtelMS sub-types (see BED_CONFIG_MAP). "Separate" = rooms with individual single
# beds (twin 2Л+Д, or the plain 3-single Стандарти); "double" = rooms with a big double bed.
BED_SEPARATE_CATEGORIES = ["Стандарт 4х 2Л + Д", "Стандарт", "Стандарт +"]
BED_DOUBLE_CATEGORIES = ["Стандарт сімейний В+Д", "Стандарт + Сімейний В+Д", "Напівлюкс"]


def _raw_category_free_all_nights(raw_availability, category, nights) -> bool:
    """True if `category` (a RAW OtelMS sub-type) has >=1 free room on EVERY night."""
    if not nights:
        return False
    entry = (raw_availability or {}).get(category) or {}
    ta = entry.get("total_available") if isinstance(entry, dict) else None
    ta = ta or {}
    return all((ta.get(d, 0) or 0) > 0 for d in nights)


def bed_config_availability(raw_availability, night_dates) -> Dict:
    """From the RAW (un-folded) scraper output, which bed configurations have >=1 room free on EVERY
    requested night. Returns {"separate": bool, "double": bool}. Used to answer a bed question with
    REAL availability instead of a generic template (owner Sprint 5, Bug 1)."""
    return {
        "separate": any(_raw_category_free_all_nights(raw_availability, c, night_dates)
                        for c in BED_SEPARATE_CATEGORIES),
        "double": any(_raw_category_free_all_nights(raw_availability, c, night_dates)
                      for c in BED_DOUBLE_CATEGORIES),
    }


def collapse_duplicate_group_rooms(rooms):
    """Collapse the LLM's date-CHOICE duplication of a single over-capacity party.

    The extractor sometimes turns "З 15 чи 16 липня" (a flexible START date for ONE group) into
    SEVERAL room objects that each carry the WHOLE party (e.g. 6 adults + 4 children in room A dated
    the 15th AND again in room B dated the 16th). That doubles the guest count (10 -> 20) and falsely
    trips the 20+ large-group redirect. When every emitted room has the IDENTICAL guest composition,
    that composition already OVERFLOWS one room (>=6 people, physically impossible per room), and the
    rooms differ only by DATE (a choice, not a real per-room split), collapse them to ONE group room
    (earliest check-in; check-out re-derived from the nights) so the 6+ split path handles it. A real
    multi-room booking (distinct compositions, or identical rooms sharing the SAME dates) is untouched.
    """
    rooms = [r for r in (rooms or []) if isinstance(r, dict)]
    if len(rooms) < 2:
        return rooms

    def comp(r):
        return ((r.get("adults") or 0), tuple(sorted(a for a in (r.get("children_ages") or []) if a is not None)))

    def size(r):
        cc = r.get("children_count")
        return (r.get("adults") or 0) + (cc if cc is not None else len(r.get("children_ages") or []))

    def datesig(r):
        return (r.get("checkin"), r.get("checkout"), r.get("fuzzy_date"), r.get("nights"))

    if not all(comp(r) == comp(rooms[0]) for r in rooms) or size(rooms[0]) < 6:
        return rooms                       # not a single over-capacity party -> real multi-room ask
    if len({datesig(r) for r in rooms}) < 2:
        return rooms                       # identical over-capacity rooms on the SAME dates -> leave it

    merged = dict(rooms[0])
    merged.pop("checkout", None)           # re-derive the check-out from the earliest check-in + nights
    for r in rooms[1:]:
        for k in ("nights", "fuzzy_date", "room_type", "ubd"):
            if not merged.get(k) and r.get(k):
                merged[k] = r[k]
    checkins = sorted(r.get("checkin") for r in rooms if r.get("checkin"))
    if checkins:
        merged["checkin"] = checkins[0]    # a date CHOICE -> take the earliest offered start
    return [merged]


# Persona 25 (Sprint 4): bed-configuration question — twin/separate beds vs one double bed.
_BED_CONFIG_MARKERS = [
    "роздільн", "окремі ліжк", "окремими ліжк", "два ліжк", "дві ліжк", "двоспальн",
    "односпальн", "одне ліжко", "одним ліжком", "тип ліжк",
    "які ліжк", "яке ліжко", "конфігурац ліжк", "ліжка окремо",
]

# Owner-CONFIRMED (2026-07-11) authoritative bed configuration per OtelMS internal sub-type. NOT
# shown verbatim to clients (rule 6 — no sub-type leak); templates.BED_CONFIG describes these by
# CONFIGURATION only. Kept here as the knowledge-base source of truth for future per-type matching.
BED_CONFIG_MAP = {
    "Стандарт сімейний В+Д": "велике двоспальне ліжко + диван",
    "Стандарт + Сімейний В+Д": "велике двоспальне ліжко + диван",
    "Стандарт 4х 2Л + Д": "2 роздільні односпальні ліжка + диван",
    "Напівлюкс": "двоспальне + односпальне ліжко + диван",
    # every OTHER Стандарт / Стандарт + sub-type:
    "_default": "3 односпальні ліжка",
}


def is_bed_config_question(text: str) -> bool:
    """True for a question about the BED configuration (twin/separate beds vs one double bed)."""
    t = (text or "").lower()
    return any(m in t for m in _BED_CONFIG_MARKERS)


# Owner-CONFIRMED (2026-07-11): the hotel has NO air conditioners in ANY room -> AIR_CONDITIONING.
_AC_MARKERS = ["кондиціон", "кондіціон", "кондицион", "клімат-контрол", "клімат контрол"]


def is_ac_question(text: str) -> bool:
    """True for a question about air conditioning (owner: there are NO air conditioners)."""
    t = (text or "").lower()
    return any(m in t for m in _AC_MARKERS)


# Owner-CONFIRMED (2026-07-11): fridge/balcony questions are answered STRICTLY from the per-type
# GENERAL_INFORMATION template (Стандарт: none; Стандарт+: balcony + fridge; Напівлюкс: fridge).
_ROOM_AMENITY_MARKERS = ["холодильник", "холодильн", "балкон", "мінібар", "міні-бар", "міні бар"]


def is_room_amenity_question(text: str) -> bool:
    """True for a fridge / balcony / mini-bar question -> the per-room-type GENERAL_INFORMATION list.
    Excludes a smoking-on-the-balcony question (that is SMOKING, not an amenity list)."""
    t = (text or "").lower()
    if any(s in t for s in ("курит", "палит", "курін", "палін")):
        return False
    return any(m in t for m in _ROOM_AMENITY_MARKERS)


# Persona 27 (Sprint 4): the client asks for a COTTAGE, which the hotel does not have (owner Q10f:
# "у нас готель") — answer honestly and pivot to rooms.
def is_cottage_question(text: str) -> bool:
    """True when the client asks about a cottage / separate house (we have none — offer rooms)."""
    t = (text or "").lower()
    return "котедж" in t or "котеж" in t or "будинок" in t or "будиночок" in t


def is_split_offer_message(text: str) -> bool:
    """True if the bot's previous message PROPOSED a room split — so a bare 'Так, порахуйте'
    means: accept that distribution and quote it (owner #15/#21, 2026-07-10)."""
    t = (text or "").lower()
    return "розподілити" in t or "розділити вашу компанію" in t


_SPLIT_ACCEPT_MARKERS = ["порахуйте", "розрахуйте", "порахувати", "підходить", "підійде",
                         "згоден", "згодна", "давайте", "так", "добре", "гаразд", "ок"]


def accepts_split(text: str) -> bool:
    """True when the client AGREES to the proposed room distribution. A message that carries its
    OWN numbers ("давайте 2 номери: 4 і 3") is a COUNTER-proposal, not acceptance — let the
    extractor/plan handle that. So acceptance requires an agreement word and NO digits."""
    t = (text or "").lower()
    if any(c.isdigit() for c in t):
        return False
    return any(m in t for m in _SPLIT_ACCEPT_MARKERS)


# Payment-RULES questions (prepayment / deposit / "pay on arrival") -> BOOK_ROOM.
# Pinned deterministically because the extractor, in a booking context, keeps routing these
# to a booking topic (or CHECK_IN_OUT, on the word "приїзд") instead of the deposit rules.
# NB: a COMPLETED payment ("оплатив"/"квитанція") is caught earlier by is_payment_intent;
# this is about ASKING the rules. "приїзд" alone = check-in; "приїзд" + оплата = prepayment.
_PAYMENT_RULES_MARKERS = [
    "по приїзду оплат", "приїзду оплат", "оплатити по приїзд", "оплата по приїзд",
    "оплатити на місці", "оплата на місці", "оплатити при заселен", "оплата при заселен",
    "без передоплат", "без предоплат", "яка передоплата", "яка предоплата",
    "потрібна передоплата", "потрібна предоплата", "чи є передоплата", "чи потрібна передоплата",
    "правила оплат", "як оплачувати", "коли оплачувати", "коли платити", "коли вносити",
    "потрібен аванс", "який аванс", "розмір авансу", "розмір передоплати",
]


def is_payment_rules_question(text: str) -> bool:
    """True for a question about HOW/WHEN to pay (prepayment / deposit / pay-on-arrival)."""
    t = (text or "").lower()
    return any(m in t for m in _PAYMENT_RULES_MARKERS)


# --- discount / promotions question -> DISCOUNTS (deterministic) -------------
# A GENERAL "do you have discounts?" question was falling through to a room description
# (GENERAL_INFORMATION). Pin it: the DISCOUNTS template lists only the bot's real discounts
# (children + military) and points to Instagram for other promos. Phrase-based (not a bare
# "знижк") so a booking that merely mentions "зі знижкою УБД" is NOT hijacked into an FAQ.
_DISCOUNT_MARKERS = [
    # promotions — safe as bare stems (a booking never contains these).
    "акці", "промокод", "промо-код", "промо код", "скидк", "распродаж",
    # discounts — PHRASE-based so a booking that only mentions "зі знижкою УБД" is NOT hijacked.
    "чи є знижк", "є знижк", "є якісь знижк", "якісь знижк", "які знижк", "яка знижк",
    "у вас знижк", "вас знижк", "знижки є", "знижок", "маєте знижк", "надаєте знижк",
    "робите знижк", "даєте знижк", "діють знижк", "будуть знижк", "щодо знижок", "про знижки",
]
# When the discount question is specifically about the military discount, keep the more
# precise MILITARY answer (it names the УБД certificate requirement) instead of DISCOUNTS.
_MILITARY_MARKERS = ["військ", "убд", "ветеран", "зсу", "всу", "боєц", "бійц", "атовц"]


# A question about the monthly pricing POLICY / month-to-month comparison must be ANSWERED,
# not swallowed by fuzzy-date extraction (owner Rule 4, 2026-07-06: the "В серпні актуальна,
# як і на липень, цінова політика?" drop).
def is_price_policy_question(text: str) -> bool:
    """True for a question about whether prices differ by month / stay the same
    ("цінова політика", "ціни такі самі як у липні", "в серпні так само?")."""
    t = (text or "").lower()
    if "цінова політ" in t or "ціновій політ" in t or "цінову політ" in t:
        return True
    if ("цін" in t or "вартіст" in t or "так само" in t) and any(
            m in t for m in ["так сам", "такі сам", "ті сам", "однаков", "не змін", "змінюються",
                             "відрізня", "як у лип", "як в лип", "як липень", "як і лип",
                             "як у серп", "як в серп", "як червень", "як у червн"]):
        return True
    return False


def asks_for_child_ages(text: str) -> bool:
    """True if a bot message is asking for the children's ages (any of the age-question
    templates). Used to insist firmly when the client ignores the ask (owner rule 2026-07-06)."""
    t = (text or "").lower()
    return "вік діт" in t or "вік дит" in t


def has_child_of_unknown_age(slots) -> bool:
    """True if any room has a child whose age is still UNKNOWN (children_count exceeds the
    number of known ages). The ONLY situation where we insist on ages — the generic first-
    contact prompts also say 'вік діток', but an adult-only booking must NOT trigger the
    insist (owner Rule 3 guard, 2026-07-06 — caught live in Persona 7)."""
    for r in (slots.get("rooms") or []):
        cc = r.get("children_count")
        cnt = cc if cc is not None else len(r.get("children_ages") or [])
        if cnt > len(r.get("children_ages") or []):
            return True
    return False


def is_discount_question(text: str) -> bool:
    """True for a GENERAL question about discounts / promotions ('чи є знижки?',
    'які у вас акції?'). Phrase-based to avoid hijacking a booking that only mentions
    a discount in passing ('порахуйте зі знижкою УБД')."""
    t = (text or "").lower()
    return any(m in t for m in _DISCOUNT_MARKERS)


# --- pet mention (for the "+300 грн/доба" note on a quote) -------------------
_PET_MARKERS = ["соба", "песик", "пёс", "пес ", "пекінес", "тварин", "котик", "кота ",
                "улюблен", "вихован", "цуцик", "хвостик"]


def mentions_pet(text: str) -> bool:
    """True if the dialogue mentions bringing a pet (so a quote can flag the +300 грн/night)."""
    t = (text or "").lower()
    return any(m in t for m in _PET_MARKERS)


# Surcharge-specific words ONLY (доплата/доплачувати/приплата) so a normal booking that merely
# mentions a pet ("з собакою, яка ВАРТІСТЬ Стандарт?") is NOT hijacked into a pet-fee answer.
_PET_FEE_MARKERS = ["доплач", "доплат", "приплат", "доплатити"]


def is_pet_surcharge_question(text: str) -> bool:
    """True for a specific question about the pet FEE ("треба за собаку доплачувати?",
    "чи є доплата за тваринку?") — answered concisely so it isn't swallowed by the verbose PETS
    template's anti-dedup (owner Rule 4, 2026-07-06)."""
    t = (text or "").lower()
    return mentions_pet(t) and any(m in t for m in _PET_FEE_MARKERS)


# --- pure thanks / goodbye (the bot must always have the last word) ----------
_THANKS_MARKERS = ["дякую", "дякуємо", "спасибі", "вдячн", "до побачення", "всього добр",
                   "на все добре", "бувайте", "гарного дня", "гарного вечора", "до зустріч"]
# A substantive word means it's NOT just a thanks — there's a real request to answer.
_THANKS_STOP = ["цін", "вартіст", "скільки", "коли", "номер", "дата", "можна", "фото",
                "відео", "басейн", "харчув", "вільн", "які дати", "трансфер", "знижк"]


def is_pure_thanks(text: str) -> bool:
    """True when a SHORT message is essentially just thanks / a goodbye (no new request),
    so the bot can close warmly instead of staying silent. A confirm word ("дякую,
    бронюю") or a substantive word ("дякую, а яка ціна?") means it is NOT a pure close."""
    t = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in (text or "").lower())
    words = t.split()
    if not words or len(words) > 4 or any(c.isdigit() for c in t):
        return False
    if any(w in _CONFIRM_WORDS for w in words):
        return False
    if any(s in t for s in _THANKS_STOP):
        return False
    return any(m in t for m in _THANKS_MARKERS)


def faq_override(text: str):
    """Return a fixed FAQ template name when the message is clearly one of the
    priority FAQs (payment-rules/location/pets/food/transport/pool/…), else None. Used to
    answer the question immediately instead of continuing slot collection."""
    if is_payment_rules_question(text):
        return "BOOK_ROOM"
    if is_price_policy_question(text):
        return "PRICE_POLICY"
    t = (text or "").lower()
    if is_menu_question(text):                  # "які страви подають?" -> dishes, not prices
        return "FOOD_MENU"
    if "буковел" in t:                          # Persona 20: distance to Bukovel
        return "DISTANCE_BUKOVEL"
    if is_ac_question(text):                    # owner 2026-07-11: NO air conditioners in any room
        return "AIR_CONDITIONING"
    if is_cottage_question(text):               # Persona 27: no cottage -> pivot to rooms
        return "COTTAGE"
    if is_bed_config_question(text):            # Persona 25: twin vs double beds
        return "BED_CONFIG"
    if is_room_amenity_question(text):          # owner 2026-07-11: fridge/balcony -> per-type list
        return "GENERAL_INFORMATION"
    if is_pool_sit_question(text):              # Persona 17: same price if only sitting
        return "POOL_ENTRY_SAME_PRICE"
    if is_outside_food_question(text):          # Persona 17: no own food/drinks
        return "OUTSIDE_FOOD"
    if is_location_question(text):
        return "PLACE"
    if is_pet_surcharge_question(text):        # concise pet-fee answer (before the verbose PETS)
        return "PET_SURCHARGE"
    if is_discount_question(text):
        # A military-specific discount question keeps the precise MILITARY answer; a general
        # "чи є знижки?" gets the DISCOUNTS overview (children + military + Instagram promos).
        return "MILITARY" if any(m in t for m in _MILITARY_MARKERS) else "DISCOUNTS"
    if "басейн" in t or "басейн" in t.replace("бесейн", "басейн"):  # tolerate "бесейн" typo
        # Children's pool is its own answer (owner fix #278): "дитячий басейн" / "басейн для
        # дітей" -> CHILDREN_POOL; everything else splits into POOL / GUEST_POOL.
        if is_children_pool_question(text):
            return "CHILDREN_POOL"
        return _pool_template(t.replace("бесейн", "басейн"))
    for keywords, template in _FAQ_OVERRIDE:
        if any(k in t for k in keywords):
            return template
    return None


# Bot messages that are NOTICES, not answers: the greeting, the "checking availability"
# filler, and the LLM-outage holder. When deciding what the client asked but hasn't been
# ANSWERED yet, these must be skipped — otherwise a mid-burst FAQ that landed right before a
# "Секундочку…" notice (during a slow scrape) is wrongly treated as answered and orphaned
# (a live-QA race the offline mocks could not reproduce).
_NOTICE_MARKERS = [
    GREETING_MARKER,
    "Секундочку, перевіряю доступність",
    "система зараз трохи перевантажена",
]


def _is_notice_message(text: str) -> bool:
    t = text or ""
    return any(m in t for m in _NOTICE_MARKERS)


def pending_faq_sequence(raw_history, user_message: str) -> List[str]:
    """Ordered, de-duplicated FAQ template names for the client's UNANSWERED burst — every
    incoming message since the bot's last REAL reply, plus the current message (Fix 3, batch
    ordering). A drip burst is collapsed to ONE processing turn, so the extractor answers
    only the LAST question; this lets the bot answer EVERY FAQ the burst asked, in order,
    before the booking status / call-to-action (Greeting -> FAQs -> Booking -> CTA).

    Notice messages (greeting / "Секундочку" / outage holder) are NOT answers, so the scan
    skips them and keeps looking back — a food FAQ asked just before a slow scrape's
    "Секундочку" notice must still be answered (live-QA finding)."""
    msgs: List[str] = []
    for m in reversed(raw_history or []):
        mtype = m.get("message_type")
        content = m.get("content") or ""
        if mtype in ("outgoing", 1):
            if _is_notice_message(content):
                continue                       # a notice isn't an answer -> keep scanning back
            break                              # a REAL bot reply -> stop
        if mtype in ("incoming", 0) and content:
            msgs.append(content)
    msgs.reverse()                             # back to chronological order
    if user_message and (not msgs or msgs[-1].strip() != user_message.strip()):
        msgs.append(user_message)
    seq: List[str] = []
    for text in msgs:
        f = faq_override(text)
        if f and f not in seq:
            seq.append(f)
    return seq


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
        for f in ("checkin", "checkout", "fuzzy_date"):
            if not out.get(f) and rem.get(f):
                out[f] = rem[f]
    # `nights` is a STAY LENGTH, not a specific date, so it is inherited even when the fresh turn
    # names a NEW check-in ("15 липня" after "дві доби") — that lets bot_server derive the check-out
    # for the dated split re-proposal instead of going silent (owner Sprint-4 Test 21).
    if not out.get("nights") and rem.get("nights"):
        out["nights"] = rem["nights"]
    for f in ("room_type", "adults", "children_count", "children_ages"):
        if not out.get(f) and rem.get(f):
            out[f] = rem[f]
    if not out.get("ubd") and rem.get("ubd"):
        out["ubd"] = rem["ubd"]
    return out


def remember_room(room) -> Dict:
    """Project a room dict down to the carried-over booking fields (for storage)."""
    return {f: (room or {}).get(f) for f in MERGE_FIELDS}


def _shared_booking_dates(rooms) -> Dict:
    """The first room's date fields (checkin/checkout/nights/fuzzy_date), used to propagate
    ONE shared stay across a multi-room split. Empty dict when no room carries dates."""
    for r in (rooms or []):
        if r and (r.get("checkin") or r.get("checkout") or r.get("fuzzy_date")):
            return {k: r.get(k) for k in ("checkin", "checkout", "nights", "fuzzy_date")
                    if r.get(k)}
    return {}


def merge_rooms(remembered_list, fresh_list) -> List[Dict]:
    """Multi-room slot memory: merge fresh rooms with remembered rooms BY INDEX. Each
    room's missing fields are filled from the same-index remembered room; remembered
    rooms the fresh turn didn't re-mention are PRESERVED (the bot must never forget a
    second/third room across a chit-chat or FAQ turn).

    Shared-stay date persistence (6+ split): when the client breaks ONE booking into
    several rooms and names NO new dates, every split room inherits the single remembered
    stay's dates — so the engine SCRAPES with those dates instead of re-asking. Dates the
    client names THIS turn take priority; per-room dates already present are never
    overwritten (so a genuine per-room date override still wins)."""
    rem = remembered_list or []
    fresh = fresh_list or []
    n = max(len(rem), len(fresh))
    out = []
    for i in range(n):
        f = fresh[i] if i < len(fresh) else {}
        r = rem[i] if i < len(rem) else {}
        out.append(merge_room(r, f))
    shared = _shared_booking_dates(fresh) or _shared_booking_dates(rem)
    if shared:
        for room in out:
            if not (room.get("checkin") or room.get("checkout") or room.get("fuzzy_date")):
                for k, v in shared.items():
                    if not room.get(k):
                        room[k] = v
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


_NO_DATES_MARKERS = [
    "дати ще не", "дати поки не", "дати не знаю", "не знаю дат", "ще не знаю дат",
    "ще не знаю", "поки не знаю", "не можу сказати дат", "не можу назвати дат",
    "ще не визначил", "не визначились з дат", "не визначилися з дат", "без конкретних дат",
    "поки без дат", "дати не визначен", "ще не вирішили коли", "коли точно не знаю",
    "ще не визначились", "ще не вирішили",
]


def says_no_dates(text: str) -> bool:
    """True when the client signals they DON'T know their dates yet ("дати ще не можу
    сказати", "ще не знаю"). Used to switch to a PROACTIVE calendar scan once guests are
    known, instead of looping the date question."""
    t = (text or "").lower()
    return any(m in t for m in _NO_DATES_MARKERS)


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
ORDER_LABEL = "Замовлено"                 # Chatwoot label that mutes the bot (transaction complete)
MARKED_LABEL = "Позначено"                # KILL SWITCH (owner Sprint 5): a human has flagged the conv
# Any of these labels is a HARD STOP: the bot halts immediately — no LLM, no reply, no processing.
MUTE_LABELS = [ORDER_LABEL, MARKED_LABEL]

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
