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

import calendar
import json
import re
from datetime import date, timedelta
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


def room_count_phrase(n: int) -> str:
    return f"{n} {_ua_plural(n, 'номера', 'номери', 'номерів')}"


def quote_line(room_type, adults, children_ages, checkin, checkout, nights, price,
               ubd=False, room_count=1) -> str:
    """The single, rigid quote format. For >1 room of the SAME type it states the count
    explicitly ("Вартість за 2 номери типу …") and `price` is the combined total."""
    head = (f"Вартість за {room_count_phrase(room_count)} типу {room_type}, "
            if room_count > 1 else f"Вартість номеру типу {room_type}, ")
    line = (
        head + f"для {guests_phrase(adults, children_ages)}, "
        f"на {nights_phrase(nights)} ({dates_phrase(checkin, checkout)}), "
        f"буде вартувати - {price} грн"
    )
    if ubd:
        line += " (з урахуванням знижки УБД -20%)"
    return line


def build_quote_reply(priced_rooms: List[Dict], ubd_booking: bool = False) -> str:
    """Render the quote(s). Identical rooms (same type/dates/guests) COLLAPSE into one
    "за N номери типу X" line with the combined price (Bug 3, Fix A). The grand total sums
    only DISTINCT room groups — the same room is never counted twice (type ALTERNATIVES
    arrive via finalize_quote_all, not here, so they are never summed).

    УБД (2026-06-23): the -20% discount applies to the ENTIRE booking total (all rooms),
    not per room. So per-room lines stay at full price and the discount is shown on the
    grand total (or, for a single room, on its only line)."""
    def sig(r):
        return (r["room_type"], r["checkin"], r["checkout"], r["adults"],
                tuple(r.get("children_ages") or []))

    groups: List[List[Dict]] = []
    for r in priced_rooms:
        for g in groups:
            if sig(g[0]) == sig(r):
                g.append(r)
                break
        else:
            groups.append([r])

    meta = []  # (per, count, group_price)
    total = 0
    for g in groups:
        per, cnt = g[0], len(g)
        group_price = per["price"] * cnt
        total += group_price
        meta.append((per, cnt, group_price))

    discounted_total = pricing_engine.apply_military_discount(total) if ubd_booking else total

    if len(meta) == 1:
        per, cnt, group_price = meta[0]
        shown = discounted_total if ubd_booking else group_price
        line = quote_line(per["room_type"], per["adults"], per["children_ages"],
                          per["checkin"], per["checkout"], per["nights"], shown,
                          ubd=ubd_booking, room_count=cnt)
        reply = line + "\nБажаєте забронювати? 💙"
    else:
        lines = [quote_line(per["room_type"], per["adults"], per["children_ages"],
                            per["checkin"], per["checkout"], per["nights"], gp,
                            ubd=False, room_count=cnt) for per, cnt, gp in meta]
        if ubd_booking:
            total_line = (f"Загальна вартість: {discounted_total} грн "
                          f"(з урахуванням знижки УБД -20%)")
        else:
            total_line = f"Загальна вартість: {total} грн"
        reply = "\n".join(lines) + f"\n\n{total_line}\nБажаєте забронювати? 💙"

    if ubd_booking:
        reply += "\n\n" + templates.MILITARY
    return reply


# --- planning (slots -> decision) ------------------------------------------

OFFERABLE_ROOMS = ["Стандарт", "Стандарт +", "Напівлюкс"]
_ROOM_EMOJI = {"Стандарт": "🏔", "Стандарт +": "🌿", "Напівлюкс": "✨"}
_OFFSEASON_WORDS = ["січ", "лют", "берез", "квіт", "трав", "верес", "жовт", "листопад", "груд"]

# Default scan year for fuzzy periods (bookings are 2026; only summer is priced).
_FUZZY_YEAR = 2026
# Longest stems first so "серпн" wins before "серп", etc.
_PRICED_MONTH_STEMS = [("червн", 6), ("черв", 6), ("липн", 7), ("лип", 7),
                       ("серпн", 8), ("серп", 8)]


def _fuzzy_month(text: str) -> Optional[int]:
    for stem, month in _PRICED_MONTH_STEMS:
        if stem in text:
            return month
    return None


def fuzzy_period_range(fuzzy: str, year: int = _FUZZY_YEAR):
    """Map a fuzzy Ukrainian period to an inclusive ('YYYY-MM-DD', 'YYYY-MM-DD')
    window so the proactive scan (Fix 1) stays inside the month/part the client
    actually named ("початок серпня" -> 1–10 Aug, "друга половина липня" -> 16–31 Jul).

    Returns None when no priced month can be identified (scan left unconstrained).
    """
    t = (fuzzy or "").lower()
    month = _fuzzy_month(t)
    if not month:
        return None
    last = calendar.monthrange(year, month)[1]
    start_day, end_day = 1, last
    if "перш" in t and "половин" in t:
        start_day, end_day = 1, 15
    elif "друг" in t and "половин" in t:
        start_day, end_day = 16, last
    elif "початок" in t or "на початку" in t or "початку" in t:
        start_day, end_day = 1, 10
    elif "середин" in t:
        start_day, end_day = 11, 20
    elif "кінец" in t or "кінці" in t or "наприкінці" in t:
        start_day, end_day = max(1, last - 10), last
    m_after = re.search(r"післ\w*\s*(\d{1,2})", t)   # "після 6 серпня" -> from the 6th
    if m_after:
        start_day = max(start_day, min(int(m_after.group(1)), last))
    if start_day > end_day:
        start_day, end_day = 1, last
    return (date(year, month, start_day).isoformat(),
            date(year, month, end_day).isoformat())


def _has_dates(room: Dict) -> bool:
    return bool(room.get("checkin") and room.get("checkout"))


def _has_guests(room: Dict) -> bool:
    return (room.get("adults") or 0) >= 1 or bool(room.get("children_ages")) or bool(room.get("children_count"))


def _child_count(room: Dict) -> int:
    cc = room.get("children_count")
    return cc if cc is not None else len(room.get("children_ages") or [])


def _room_guests(room: Dict) -> int:
    """Total guests in ONE room object (adults + children, babies included)."""
    return (room.get("adults") or 0) + _child_count(room)


def _fuzzy_offseason(fuzzy: str) -> bool:
    t = (fuzzy or "").lower()
    return any(w in t for w in _OFFSEASON_WORDS)


def has_off_season_dates(slots: Dict) -> bool:
    for r in slots.get("rooms") or []:
        if r.get("checkin") and r.get("checkout") and not pricing_engine.stay_is_priced(
                r["checkin"], r["checkout"]):
            return True
    return False


def _nights(room: Dict) -> Optional[int]:
    """Number of nights from exact dates, else the `nights` slot, else None."""
    if room.get("checkin") and room.get("checkout"):
        return len(pricing_engine.night_dates(room["checkin"], room["checkout"]))
    n = room.get("nights")
    return n if isinstance(n, int) and n > 0 else None


def find_nearest_window(availability, room_type, after, nights, room_count=1, horizon=90):
    """Forward-scan up to `horizon` days (default 90) after `after` for the first block
    of `nights` consecutive dates where `room_type` has >= room_count free."""
    key = bot_logic.match_availability_key(availability, room_type)
    if not key:
        return None
    avail = availability.get(key) or {}
    start = pricing_engine._as_date(after)
    for off in range(1, horizon + 1):
        d0 = start + timedelta(days=off)
        block = [(d0 + timedelta(days=i)).isoformat() for i in range(nights)]
        if all(avail.get(x, 0) >= room_count for x in block):
            return (d0.isoformat(), (d0 + timedelta(days=nights)).isoformat())
    return None


def _free_windows(avail: Dict, dates: List[str], min_nights: int, room_count: int):
    """Maximal continuous free stretches (>= min_nights) within `dates` (sorted)."""
    windows, run, prev = [], [], None
    for d in dates:
        if avail.get(d, 0) < room_count:
            if len(run) >= min_nights:
                windows.append((run[0], run[-1]))
            run, prev = [], None
            continue
        cur = pricing_engine._as_date(d)
        if prev is not None and (cur - prev).days != 1:
            if len(run) >= min_nights:
                windows.append((run[0], run[-1]))
            run = []
        run.append(d)
        prev = cur
    if len(run) >= min_nights:
        windows.append((run[0], run[-1]))
    return windows


def propose_windows(spec: Dict, availability: Dict, count: int = 2) -> str:
    """Fix 1 — proactive exploratory scan. Client gave a fuzzy period + guests, so
    we scan the calendar WITHIN that period for continuous free stretches and offer
    real windows (never bounce back asking for exact dates).

    Unknown nights -> default to 2-night blocks. If nothing is free inside the named
    period, fall back to scanning the whole visible window forward (never give up,
    Fix 2); only NEAREST_NONE when the entire visible calendar is full.
    """
    room = spec.get("room_type") or OFFERABLE_ROOMS[0]
    room_count = spec.get("room_count") or 1
    min_nights = spec.get("nights") or 2     # Fix 1: default 2–3-night blocks
    fuzzy = spec.get("fuzzy_date") or ""
    key = bot_logic.match_availability_key(availability, room)
    avail = (availability.get(key) or {}) if key else {}
    all_dates = sorted(avail.keys())

    def _offer(windows):
        found = ", або ".join(dates_phrase(w[0], w[1]) for w in windows[:count])
        if fuzzy:
            return (templates.PROPOSE_WINDOWS
                    .replace("{fuzzy_date}", fuzzy).replace("{found_dates}", found))
        # No named period (client said "дати ще не знаю") -> open-calendar phrasing.
        return templates.PROPOSE_WINDOWS_OPEN.replace("{found_dates}", found)

    period = fuzzy_period_range(fuzzy)
    in_period = [d for d in all_dates if period is None or period[0] <= d <= period[1]]
    windows = _free_windows(avail, in_period, min_nights, room_count)
    if windows:
        return _offer(windows)

    # Nothing free inside the VISIBLE part of the named period.
    vis_max = all_dates[-1] if all_dates else None
    if period is not None and vis_max is not None and period[1] > vis_max:
        # The named period EXTENDS beyond the open calendar window (OtelMS shows only
        # ~6-7 weeks ahead) and the visible slice has nothing free — we cannot honestly
        # scan the rest. Ask for exact dates instead of proposing dates from the wrong
        # month; exact out-of-window dates still quote via the 'unknown' path.
        return templates.ACKNOWLEDGE_FUZZY.replace("{fuzzy_date}", fuzzy or "ці дати")

    # The period is fully visible but booked there -> offer the nearest real window.
    nearest = _free_windows(avail, all_dates, min_nights, room_count)
    if nearest:
        w = nearest[0]
        return templates.SOLD_OUT_FOUND_NEAREST.replace("{dates}", dates_phrase(w[0], w[1]))
    return templates.NEAREST_NONE


def first_offered_window(spec: Dict, availability: Dict):
    """The (checkin, checkout) a bare 'Так' should accept after `propose_windows` showed
    free віконця — the first concrete free window, honoring the requested nights when
    known (else the displayed run). Returns None when no window would have been offered
    (e.g. the period is beyond the visible calendar)."""
    room = spec.get("room_type") or OFFERABLE_ROOMS[0]
    room_count = spec.get("room_count") or 1
    min_nights = spec.get("nights") or 2
    fuzzy = spec.get("fuzzy_date") or ""
    key = bot_logic.match_availability_key(availability, room)
    avail = (availability.get(key) or {}) if key else {}
    all_dates = sorted(avail.keys())
    period = fuzzy_period_range(fuzzy)
    in_period = [d for d in all_dates if period is None or period[0] <= d <= period[1]]
    windows = _free_windows(avail, in_period, min_nights, room_count)
    if not windows:
        vis_max = all_dates[-1] if all_dates else None
        if period is not None and vis_max is not None and period[1] > vis_max:
            return None    # propose_windows asks for exact dates here -> nothing to accept
        windows = _free_windows(avail, all_dates, min_nights, room_count)
    if not windows:
        return None
    run0, run_last = windows[0]
    ci = pricing_engine._as_date(run0)
    last = pricing_engine._as_date(run_last)
    nights = spec.get("nights")
    if isinstance(nights, int) and nights > 0:
        co = ci + timedelta(days=nights)
        if co > last:
            co = last
    else:
        co = last
    if co <= ci:
        co = ci + timedelta(days=1)
    return (run0, co.isoformat())


def nearest_window_any(availability: Dict, after, nights: int, room_count: int = 1):
    """Earliest free window across ANY offerable room type — for auto-proposing the
    nearest dates when the client's exact dates are sold out (Bug 2: don't ask permission)."""
    cands = [find_nearest_window(availability, rt, after, nights, room_count)
             for rt in OFFERABLE_ROOMS]
    cands = [c for c in cands if c]
    return min(cands, key=lambda w: w[0]) if cands else None


def offered_window(decision: Dict, availability: Dict):
    """The (checkin, checkout) the bot is offering in a window-offer reply, for ANY
    decision path (explore windows, a chosen room's SOLD_OUT_FOUND_NEAREST, quote_all
    sold-out, or nearest). Stored as `_pending_window` so a later bare 'Так' quotes EXACTLY
    these dates instead of re-searching. None when the reply isn't a concrete window."""
    act = decision.get("action")
    if act == "explore":
        return first_offered_window(decision.get("spec") or {}, availability)
    if act == "nearest":
        spec = decision.get("spec") or {}
        room, after, n = spec.get("room_type"), spec.get("checkin"), _nights(spec)
        if room and after and n:
            return find_nearest_window(availability, room, after, n)
        return None
    if act == "quote":
        for r in decision.get("rooms") or []:
            rt, ci, co = r.get("room_type"), r.get("checkin"), r.get("checkout")
            if not (rt and ci and co):
                continue
            nights = pricing_engine.night_dates(ci, co)
            if bot_logic.is_room_available(availability, rt, nights) == "sold_out" \
                    and not bot_logic.free_room_types(availability, nights):
                return find_nearest_window(availability, rt, ci, len(nights))
        return None
    if act == "quote_all":
        spec = decision.get("spec") or {}
        ci, co = spec.get("checkin"), spec.get("checkout")
        if ci and co:
            return nearest_window_any(availability, ci, len(pricing_engine.night_dates(ci, co)))
        return None
    return None


def plan(slots: Dict) -> Dict:
    """Slots -> decision. action in {reply, quote, quote_all, explore, nearest}."""
    rooms = slots.get("rooms") or []

    # Off-season (exact OR a clearly-named fuzzy month that isn't priced).
    for r in rooms:
        ci, co = r.get("checkin"), r.get("checkout")
        if ci and not pricing_engine.is_priced_month(ci):
            return {"action": "reply", "reply": templates.OFF_SEASON}
        if ci and co and not pricing_engine.stay_is_priced(ci, co):
            return {"action": "reply", "reply": templates.OFF_SEASON}
    fuzzy = next((r.get("fuzzy_date") for r in rooms if r.get("fuzzy_date")), None)
    if fuzzy and _fuzzy_offseason(fuzzy):
        return {"action": "reply", "reply": templates.OFF_SEASON}

    # A2 Step 3: user agreed to / insists on a nearest-date search for a chosen room.
    if slots.get("topic") == "nearest_dates":
        r = next((x for x in rooms if x.get("room_type")), None)
        if r:
            return {"action": "nearest", "spec": r}

    any_exact = any(_has_dates(r) for r in rooms)
    any_guests = any(_has_guests(r) for r in rooms)

    # 6+ guests cannot share one room (max ~5 in Напівлюкс) -> ask how to distribute
    # them BEFORE quoting (owner rule 2026-06-23). Only when the client packed everyone
    # into ONE room object; an explicit multi-room request (>1 room) is already split.
    if len(rooms) == 1 and _room_guests(rooms[0]) >= 6:
        return {"action": "reply", "reply": templates.ASK_ROOM_DISTRIBUTION}

    cc = max((_child_count(r) for r in rooms), default=0)
    ages = max((len(r.get("children_ages") or []) for r in rooms), default=0)
    ages_missing = cc > ages

    # Exact dates + guests -> calendar quote (never a generic monthly range).
    if any_exact and any_guests:
        if ages_missing:
            return {"action": "reply", "reply": templates.QUESTION_MISSING_AGE}
        dated = [r for r in rooms if _has_dates(r)]
        chosen = [r for r in dated if r.get("room_type")]
        if chosen:
            return {"action": "quote", "rooms": chosen}
        return {"action": "quote_all", "spec": dated[0]}

    # Exact dates but guests unknown -> granular "missing guests".
    if any_exact and not any_guests:
        return {"action": "reply", "reply": templates.QUESTION_MISSING_GUESTS}

    # No exact dates, guests known.
    if any_guests:
        nights = max((_nights(r) or 0 for r in rooms), default=0)
        if fuzzy:
            # Fix 1: guests + a fuzzy period -> PROACTIVELY scan that period NOW. Never
            # bounce back asking for exact dates. Unknown nights -> the scan defaults to
            # 2–3-night blocks (propose_windows). Bug 3: if a child's age is still missing,
            # ACKNOWLEDGE the period (so the client feels heard) and ask ONLY the age.
            if ages_missing:
                return {"action": "reply",
                        "reply": templates.ACKNOWLEDGE_FUZZY_AGE.replace("{fuzzy_date}", fuzzy)}
            fuzzy_room = next((r for r in rooms if r.get("fuzzy_date")), rooms[0])
            spec = {**fuzzy_room, "nights": nights or 0, "fuzzy_date": fuzzy}
            return {"action": "explore", "spec": spec}
        if ages_missing:   # No period to scan -> we genuinely need both dates AND ages.
            return {"action": "reply", "reply":
                    (templates.QUESTION_MISSING_DATES_1_CHILD if cc == 1
                     else templates.QUESTION_MISSING_DATES_CHILDREN)}
        return {"action": "reply", "reply": templates.QUESTION_ONLY_DATES}   # A1: dates only

    # No guests. A fuzzy period -> acknowledge once; else the full first-contact question.
    if fuzzy:
        return {"action": "reply",
                "reply": templates.ACKNOWLEDGE_FUZZY.replace("{fuzzy_date}", fuzzy)}
    return {"action": "reply", "reply": templates.QUESTION_ALL_MISSING}


def faq_followup(slots: Dict) -> str:
    """Fix 3 — what to append after answering an FAQ mid-dialogue, WITHOUT wiping
    the booking state already collected.

    * nothing gathered yet            -> a gentle "which dates?" nudge;
    * partial booking (some slots)    -> ask ONLY the still-missing piece (never the
                                         monolithic all-missing question, never re-ask
                                         info the client already gave);
    * enough info to price            -> offer to continue (no surprise re-scrape).
    """
    rooms = slots.get("rooms") or []
    if not bot_logic.has_booking_context({"rooms": rooms}):
        return templates.FAQ_DATE_NUDGE
    decision = plan(slots)
    if decision.get("action") == "reply":
        q = decision.get("reply", "")
        # Never dump the first-contact monolith or off-season pitch right after an FAQ.
        if q and q not in (templates.QUESTION_ALL_MISSING, templates.OFF_SEASON):
            return "\n\n" + q
    return templates.FAQ_CONTINUE_NUDGE


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
            if free:  # Step 1: other categories free on the SAME dates -> cross-sell.
                return (templates.ROOM_BOOKED
                        .replace("{тип номеру}", room_type)
                        .replace("{вільні_номери}", ", ".join(free)))
            # Step 2: everything booked -> forward-scan THIS room for the nearest block.
            win = find_nearest_window(simplified_availability, room_type, checkin, len(nights))
            if win:  # exact dates were sold out -> "found nearest" wording (NOT "ще не визначились").
                return templates.SOLD_OUT_FOUND_NEAREST.replace("{dates}", dates_phrase(win[0], win[1]))
            return templates.NEAREST_NONE

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

    # УБД (2026-06-23): -20% applies to the WHOLE booking (a veteran's family), so a single
    # flagged room discounts the entire total. The discount is rendered on the grand total.
    ubd_booking = any(bool(r.get("ubd")) for r in rooms)
    return build_quote_reply(priced, ubd_booking)


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
        # Bug 2: everything sold out -> AUTO-propose the nearest free window (don't ask
        # permission). Only fall back to NEAREST_NONE if nothing free in the whole window.
        win = nearest_window_any(simplified_availability, checkin, len(nights))
        if win:
            return templates.SOLD_OUT_FOUND_NEAREST.replace("{dates}", dates_phrase(win[0], win[1]))
        return templates.NEAREST_NONE

    header = (f"На дати {dates_phrase(checkin, checkout)} ({nights_phrase(len(nights))}) "
              f"для {guests_phrase(adults, children_ages)} доступні такі номери:")

    # Family recommendation (owner rule 2026-06-23): for a family of 4-5 (with children),
    # prioritise Напівлюкс, then offer two separate rooms as a roomier alternative.
    family_note = ""
    if (adults + len(children_ages)) >= 4 and children_ages:
        if any("Напівлюкс" in ln for ln in lines):
            family_note = ("\n💡 Для родини найзручніший Напівлюкс. За бажанням можемо також "
                           "запропонувати два окремі номери — буде ще просторіше.")
        else:
            family_note = ("\n💡 Для вашої родини комфортно підійдуть два окремі номери — "
                           "підкажіть, і я підберу варіанти.")

    reply = header + "\n" + "\n".join(lines) + family_note + "\nЯкий тип номеру обираєте? 💙"
    if ubd:
        reply += "\n\n" + templates.MILITARY
    return reply


def nearest_reply(spec: Dict, availability: Dict) -> str:
    """A2 Step 3: forward-scan for the chosen room and propose real nearest dates."""
    room = spec.get("room_type")
    after = spec.get("checkin")
    nights = _nights(spec)
    if not (room and after and nights):   # Fix 2: need room + exact start + nights to scan
        return templates.QUESTION_ONLY_DATES
    win = find_nearest_window(availability, room, after, nights)
    if win:
        return templates.SOLD_OUT_FOUND_NEAREST.replace("{dates}", dates_phrase(win[0], win[1]))
    return templates.NEAREST_NONE
