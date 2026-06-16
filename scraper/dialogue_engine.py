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


def quote_line(room_type, adults, children_ages, checkin, checkout, nights, price) -> str:
    """The single, rigid quote format mandated by the business."""
    return (
        f"Вартість номеру типу {room_type}, для {guests_phrase(adults, children_ages)}, "
        f"на {nights_phrase(nights)} ({dates_phrase(checkin, checkout)}), "
        f"буде вартувати - {price} грн"
    )


def build_quote_reply(priced_rooms: List[Dict]) -> str:
    lines = [
        quote_line(r["room_type"], r["adults"], r["children_ages"],
                   r["checkin"], r["checkout"], r["nights"], r["price"])
        for r in priced_rooms
    ]
    if len(lines) == 1:
        return lines[0] + "\nБажаєте забронювати? 💙"
    total = sum(r["price"] for r in priced_rooms)
    return "\n".join(lines) + f"\n\nЗагальна вартість: {total} грн\nБажаєте забронювати? 💙"


# --- planning (slots -> decision) ------------------------------------------

def _has_dates(room: Dict) -> bool:
    return bool(room.get("checkin") and room.get("checkout"))


def _has_guests(room: Dict) -> bool:
    return (room.get("adults") or 0) >= 1 or bool(room.get("children_ages"))


def _missing_dates_question(rooms: List[Dict]) -> str:
    children = [a for r in rooms for a in (r.get("children_ages") or [])]
    if children:
        return (templates.QUESTION_MISSING_DATES_1_CHILD if len(children) == 1
                else templates.QUESTION_MISSING_DATES_CHILDREN)
    return templates.QUESTION_MISSING_DATES


def _monthly_price(rooms: List[Dict]) -> str:
    for r in rooms:
        if r.get("checkin"):                      # a known month is enough
            month = pricing_engine._as_date(r["checkin"]).month
            return _MONTH_TEMPLATE.get(month, templates.OFF_SEASON)
    return templates.QUESTION_MISSING_DATES


def has_off_season_dates(slots: Dict) -> bool:
    """True if any concrete stay in the slots falls (partly) in an unpriced month."""
    for r in slots.get("rooms") or []:
        if r.get("checkin") and r.get("checkout") and not pricing_engine.stay_is_priced(
                r["checkin"], r["checkout"]):
            return True
    return False


def plan(slots: Dict) -> Dict:
    """Decide the booking-path reply from extracted slots WITHOUT touching the LLM.

    Returns {"action": "reply", "reply": str} for everything that can be answered
    immediately, or {"action": "quote", "rooms": [...]} when a live availability
    check + price calculation is required.
    """
    rooms = slots.get("rooms") or []

    # 1) Off-season: a KNOWN check-in month that isn't priced (covers a bare
    #    "ціни на жовтень" where only the month is given) -> we cannot quote.
    for r in rooms:
        ci, co = r.get("checkin"), r.get("checkout")
        if ci and not pricing_engine.is_priced_month(ci):
            return {"action": "reply", "reply": templates.OFF_SEASON}
        if ci and co and not pricing_engine.stay_is_priced(ci, co):
            return {"action": "reply", "reply": templates.OFF_SEASON}

    any_full = any(_has_dates(r) for r in rooms)            # both check-in & check-out
    any_month = any(r.get("checkin") for r in rooms)        # at least a month is known
    any_guests = any(_has_guests(r) for r in rooms)
    any_room = any(r.get("room_type") for r in rooms)

    # 2) Nothing useful at all.
    if not any_month and not any_guests and not any_room:
        return {"action": "reply", "reply": templates.QUESTION_ALL_MISSING}

    # 3) A specific room + full dates + guests -> deterministic price quote.
    bookable = [r for r in rooms if r.get("room_type") and _has_dates(r) and _has_guests(r)]
    if bookable:
        return {"action": "quote", "rooms": bookable}

    # 4) Month + guests known (even without exact days) and no specific room ->
    #    general monthly price (e.g. "ціни на серпень на двох" -> PRICE_AUGUST).
    if any_month and any_guests:
        return {"action": "reply", "reply": _monthly_price(rooms)}

    # 5) Otherwise ask for whatever is still missing.
    if not any_month and not any_full:
        return {"action": "reply", "reply": _missing_dates_question(rooms)}
    if not any_guests:
        return {"action": "reply", "reply": templates.QUESTION_MISSING_GUESTS}
    return {"action": "reply", "reply": templates.QUESTION_MISSING_DATES}


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
            return templates.POLITE_CLOSE

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

        priced.append({
            "room_type": quote.room_type, "adults": adults, "children_ages": children_ages,
            "checkin": checkin, "checkout": checkout, "nights": quote.nights,
            "price": quote.total,
        })

    return build_quote_reply(priced)
