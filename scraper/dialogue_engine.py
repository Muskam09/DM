"""
dialogue_engine.py — DETERMINISTIC reply builder for the booking/pricing path.

The LLM is reduced to extraction/classification only (it returns structured slots).
Everything that touches money or the calendar is computed HERE, in Python:

  * day-of-week / weekday-vs-weekend / nights / totals  -> pricing_engine
  * availability gating (never quote a sold-out room)    -> bot_logic
  * the exact, rigid quote wording                       -> this module

So the bot can no longer mis-date July 5th or hallucinate a price: the number and
the format are produced by tested code, and the LLM only supplies the slots.

Pure: stdlib + templates + pricing_engine + bot_logic (no FastAPI / google-genai).
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

import bot_logic
import pricing_engine
import templates

ENGINE = pricing_engine.PricingEngine()

_GEN_MONTHS = {
    1: "січня", 2: "лютого", 3: "березня", 4: "квітня", 5: "травня", 6: "червня",
    7: "липня", 8: "серпня", 9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня",
}
_MONTH_TEMPLATE = {6: templates.PRICE_JUNE, 7: templates.PRICE_JULY, 8: templates.PRICE_AUGUST}


# --- slot parsing ----------------------------------------------------------

def parse_slots(text: str) -> Dict:
    """Robustly parse the extractor LLM's JSON. On any failure, fall back to a
    safe 'greeting' (which makes the bot ask for the missing info)."""
    if not text:
        return {"topic": "greeting", "rooms": []}
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.IGNORECASE).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return {"topic": "greeting", "rooms": []}
    if not isinstance(data, dict):
        return {"topic": "greeting", "rooms": []}
    data.setdefault("topic", "greeting")
    data.setdefault("rooms", [])
    if not isinstance(data["rooms"], list):
        data["rooms"] = []
    return data


# --- formatting helpers ----------------------------------------------------

def _ua_plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def nights_phrase(n: int) -> str:
    return f"{n} {_ua_plural(n, 'ніч', 'ночі', 'ночей')}"


def guests_phrase(adults: int, children_ages: List[int]) -> str:
    children_ages = children_ages or []
    parts = []
    if adults:
        parts.append(f"{adults} {_ua_plural(adults, 'дорослого', 'дорослих', 'дорослих')}")
    if children_ages:
        cnt = len(children_ages)
        word = _ua_plural(cnt, "дитини", "дітей", "дітей")
        ages = ", ".join(str(a) for a in children_ages)
        parts.append(f"{cnt} {word} ({ages} р.)")
    return " та ".join(parts) if parts else "гостей"


def dates_phrase(checkin, checkout) -> str:
    ci = pricing_engine._as_date(checkin)
    co = pricing_engine._as_date(checkout)
    if ci.month == co.month:
        return f"{ci.day} - {co.day} {_GEN_MONTHS[co.month]}"
    return f"{ci.day} {_GEN_MONTHS[ci.month]} - {co.day} {_GEN_MONTHS[co.month]}"


def quote_line(room_type, adults, children_ages, checkin, checkout, nights, price,
               ubd=False) -> str:
    """The single, rigid quote format mandated by the business."""
    line = (
        f"Вартість номеру типу {room_type}, для {guests_phrase(adults, children_ages)}, "
        f"на {nights_phrase(nights)} ({dates_phrase(checkin, checkout)}), "
        f"буде вартувати - {price} грн"
    )
    if ubd:
        line += " (з урахуванням знижки УБД -20%)"
    return line


def build_quote_reply(priced_rooms: List[Dict]) -> str:
    lines = [
        quote_line(r["room_type"], r["adults"], r["children_ages"],
                   r["checkin"], r["checkout"], r["nights"], r["price"], r.get("ubd", False))
        for r in priced_rooms
    ]
    if len(lines) == 1:
        reply = lines[0] + "\nБажаєте забронювати? 💙"
    else:
        total = sum(r["price"] for r in priced_rooms)
        reply = "\n".join(lines) + f"\n\nЗагальна вартість: {total} грн\nБажаєте забронювати? 💙"
    if any(r.get("ubd") for r in priced_rooms):
        reply += "\n\n" + templates.MILITARY
    return reply


# --- planning (slots -> decision) ------------------------------------------

OFFERABLE_ROOMS = ["Стандарт", "Стандарт +", "Напівлюкс"]
_ROOM_EMOJI = {"Стандарт": "🏔", "Стандарт +": "🌿", "Напівлюкс": "✨"}
_OFFSEASON_WORDS = ["січ", "лют", "берез", "квіт", "трав", "верес", "жовт", "листопад", "груд"]


def _has_dates(room: Dict) -> bool:
    return bool(room.get("checkin") and room.get("checkout"))


def _has_guests(room: Dict) -> bool:
    return (room.get("adults") or 0) >= 1 or bool(room.get("children_ages"))


def _fuzzy_offseason(fuzzy: str) -> bool:
    """True if a fuzzy period clearly names an unpriced (non-summer) month."""
    t = (fuzzy or "").lower()
    return any(w in t for w in _OFFSEASON_WORDS)


def has_off_season_dates(slots: Dict) -> bool:
    """True if any concrete stay in the slots falls (partly) in an unpriced month."""
    for r in slots.get("rooms") or []:
        if r.get("checkin") and r.get("checkout") and not pricing_engine.stay_is_priced(
                r["checkin"], r["checkout"]):
            return True
    return False


def plan(slots: Dict) -> Dict:
    """Decide the booking-path reply from extracted slots WITHOUT touching the LLM.

    Returns {"action": "reply", "reply": str}, {"action": "quote", "rooms": [...]}
    (specific room(s) -> calendar check + price), or {"action": "quote_all",
    "spec": {...}} (exact dates but no chosen room -> price every available type).
    """
    rooms = slots.get("rooms") or []

    # Off-season guard — an exact OR a clearly-named fuzzy month that isn't priced.
    for r in rooms:
        ci, co = r.get("checkin"), r.get("checkout")
        if ci and not pricing_engine.is_priced_month(ci):
            return {"action": "reply", "reply": templates.OFF_SEASON}
        if ci and co and not pricing_engine.stay_is_priced(ci, co):
            return {"action": "reply", "reply": templates.OFF_SEASON}
    fuzzy = next((r.get("fuzzy_date") for r in rooms if r.get("fuzzy_date")), None)
    if fuzzy and _fuzzy_offseason(fuzzy):
        return {"action": "reply", "reply": templates.OFF_SEASON}

    any_exact = any(_has_dates(r) for r in rooms)
    any_guests = any(_has_guests(r) for r in rooms)

    # Fix 4: an EXACT date range + guests MUST hit the calendar (never a generic range).
    if any_exact and any_guests:
        dated = [r for r in rooms if _has_dates(r)]
        chosen = [r for r in dated if r.get("room_type")]
        if chosen:
            return {"action": "quote", "rooms": chosen}            # specific room(s)
        return {"action": "quote_all", "spec": dated[0]}           # price every type

    # Exact dates but guests unknown -> ask ONLY for guests (Fix 1).
    if any_exact and not any_guests:
        return {"action": "reply", "reply": templates.ASK_GUESTS_ONLY}

    # Fix 3: a fuzzy period (no exact dates) -> acknowledge it, ask for exact dates.
    if fuzzy:
        return {"action": "reply",
                "reply": templates.ACKNOWLEDGE_FUZZY.replace("{fuzzy_date}", fuzzy)}

    # Fix 1: guests known but no dates at all -> ask ONLY for dates.
    if any_guests:
        return {"action": "reply", "reply": templates.ASK_DATES_ONLY}

    # Nothing usable (or only a room type) -> the full first-contact question.
    return {"action": "reply", "reply": templates.QUESTION_ALL_MISSING}


# --- finalisation (availability gate -> price -> exact format) -------------

def finalize_quote(rooms: List[Dict], simplified_availability: Dict, engine=ENGINE) -> str:
    """Gate on availability, compute the price deterministically, format rigidly.

    AVAILABILITY GATING: if ANY requested room is sold out on the dates, we return
    the Polite Close template and never quote a price.
    """
    priced = []
    for r in rooms:
        room_type = r.get("room_type")
        checkin, checkout = r.get("checkin"), r.get("checkout")
        nights = pricing_engine.night_dates(checkin, checkout)

        status = bot_logic.is_room_available(simplified_availability, room_type, nights)
        if status == "sold_out":
            # Case 4: other room types are still free on these dates -> offer them.
            # Case 5: nothing free at all -> offer to find the nearest free dates.
            free = bot_logic.free_room_types(simplified_availability, nights)
            if free:
                return (templates.ROOM_BOOKED
                        .replace("{тип номеру}", room_type)
                        .replace("{вільні_номери}", ", ".join(free)))
            return templates.SOLD_OUT_NEAREST

        adults = r.get("adults") or 0
        children_ages = r.get("children_ages") or []
        if adults == 0 and not children_ages:
            adults = 2  # sensible default if the extractor missed the count
        try:
            guests = pricing_engine.make_guests(adults=adults, children_ages=children_ages)
            quote = engine.quote(room_type, checkin, checkout, guests)
        except pricing_engine.OffSeasonError:
            return templates.OFF_SEASON
        except KeyError:
            return templates.PRESENTATION_ROOMS  # unknown room type -> present options

        # УБД (combat-veteran) 20% discount, deterministic, per requested room.
        ubd = bool(r.get("ubd"))
        price = pricing_engine.apply_military_discount(quote.total) if ubd else quote.total

        priced.append({
            "room_type": quote.room_type, "adults": adults, "children_ages": children_ages,
            "checkin": checkin, "checkout": checkout, "nights": quote.nights,
            "price": price, "ubd": ubd,
        })

    return build_quote_reply(priced)


def finalize_quote_all(spec: Dict, simplified_availability: Dict, engine=ENGINE) -> str:
    """Exact dates but no chosen room -> price EVERY available room type (Fix 4).

    Skips sold-out / unpriced types; if nothing is free -> SOLD_OUT_NEAREST.
    """
    checkin, checkout = spec.get("checkin"), spec.get("checkout")
    nights = pricing_engine.night_dates(checkin, checkout)
    adults = spec.get("adults") or 0
    children_ages = spec.get("children_ages") or []
    if adults == 0 and not children_ages:
        adults = 2
    ubd = bool(spec.get("ubd"))

    lines = []
    for room_type in OFFERABLE_ROOMS:
        if bot_logic.is_room_available(simplified_availability, room_type, nights) == "sold_out":
            continue
        try:
            guests = pricing_engine.make_guests(adults=adults, children_ages=children_ages)
            quote = engine.quote(room_type, checkin, checkout, guests)
        except (pricing_engine.OffSeasonError, KeyError):
            continue
        price = pricing_engine.apply_military_discount(quote.total) if ubd else quote.total
        lines.append(f"{_ROOM_EMOJI.get(room_type, '•')} {room_type} — {price} грн")

    if not lines:
        return templates.SOLD_OUT_NEAREST

    header = (f"На дати {dates_phrase(checkin, checkout)} ({nights_phrase(len(nights))}) "
              f"для {guests_phrase(adults, children_ages)} доступні такі номери:")
    reply = header + "\n" + "\n".join(lines) + "\nЯкий тип номеру обираєте? 💙"
    if ubd:
        reply += "\n\n" + templates.MILITARY
    return reply
