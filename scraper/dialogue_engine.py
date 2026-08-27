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
_MONTH_TEMPLATE = {
    6: templates.PRICE_JUNE,
    7: templates.PRICE_JULY,
    8: templates.PRICE_AUGUST,
    9: templates.PRICE_SEPTEMBER,
    10: templates.PRICE_OCTOBER}


# --- slot parsing ----------------------------------------------------------

# The extractor LLM sometimes emits the STRING "null" / "none" / "" for a field instead
# of JSON null (a real live-LLM quirk offline mocks never reproduce: it made a "двомісний
# номер" request scan a bogus room type "null" -> empty availability -> a false NEAREST_NONE).
# Normalise these stringy-nulls to Python None at the LLM->core boundary so nothing
# downstream (room-key match, date parsing, quoting) is fed a fake value.
_NULLISH = {"null", "none", "nil", "n/a", "na", "-", ""}


def _denull(v):
    """String forms of 'null' from the LLM -> Python None; everything else unchanged."""
    if isinstance(v, str) and v.strip().lower() in _NULLISH:
        return None
    return v


def _clean_room(r: Dict) -> Dict:
    if not isinstance(r, dict):
        return {}
    return {k: _denull(v) for k, v in r.items()}


def parse_slots(text: str) -> Dict:
    """Robustly parse the extractor LLM's JSON. On any failure, fall back to a
    safe 'greeting' (which makes the bot ask for the missing info). Stringy-null
    values ('null'/'none'/'') are normalised to real None (live-LLM hardening)."""
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
    if not isinstance(data.get("topic"), str) or data["topic"].strip().lower() in _NULLISH:
        data["topic"] = "greeting"
    data.setdefault("rooms", [])
    if not isinstance(data["rooms"], list):
        data["rooms"] = []
    data["rooms"] = [_clean_room(r) for r in data["rooms"]]
    if "faq_template" in data:
        data["faq_template"] = _denull(data["faq_template"])
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
    # UX (owner 2026-06-24): make the child discount explicit when a 6-11 y.o. is present.
    if any(a is not None and pricing_engine.child_place_key(a) == pricing_engine.CHILD_PLACE_KEY
           for a in (children_ages or [])):
        line += " (враховано знижку 50% на дитяче місце)"
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


# --- planning (slots -> decision) ----------------Міжсезоння--------------------------

OFFERABLE_ROOMS = ["Стандарт", "Стандарт +", "Напівлюкс"]
_ROOM_EMOJI = {"Стандарт": "🏔", "Стандарт +": "🌿", "Напівлюкс": "✨"}
_OFFSEASON_WORDS = ["січ", "лют", "берез", "квіт", "трав", "листопад", "груд"]

# Default scan year for fuzzy periods (bookings are 2026; only summer is priced).
_FUZZY_YEAR = 2026
# Longest stems first so "серпн" wins before "серп", etc.
_PRICED_MONTH_STEMS = [("червн", 6), ("черв", 6), ("липн", 7), ("лип", 7),
                       ("серпн", 8), ("серп", 8), ("вересн", 9), ("верес", 9), ("жовтн", 10), ("жовт", 10)]


def _fuzzy_month(text: str) -> Optional[int]:
    for stem, month in _PRICED_MONTH_STEMS:
        if stem in text:
            return month
    # The extractor sometimes normalises a fuzzy period to "YYYY-MM" (e.g. "2026-08" for
    # "початок серпня"). Parse the month so we still scan the RIGHT month (bug 2026-07-05:
    # an August period was offering July windows because this returned None).
    m = re.search(r"20\d\d[-/.](\d{1,2})", text)
    if m:
        mon = int(m.group(1))
        if mon in pricing_engine.PRICED_MONTHS:
            return mon
    return None


# A compound fuzzy period can name several months/parts joined by a connector
# ("друга половина липня АБО після 6 серпня", "липень чи серпень", "у липні і серпні").
# Split on these so EVERY named month is scanned — dropping the 2nd month made an August
# request silently offer only July windows (date-horizon bug).
_PERIOD_SPLIT_RE = re.compile(r"\s+(?:або|чи|та|і)\s+|\s*[,;/]\s*", re.IGNORECASE)


def _single_period_range(t: str, year: int):
    """Map ONE fuzzy Ukrainian sub-period (already lowercased) to an inclusive
    ('YYYY-MM-DD', 'YYYY-MM-DD') window, or None when no priced month is identifiable."""
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


def fuzzy_period_ranges(fuzzy: str, year: int = _FUZZY_YEAR):
    """ALL inclusive ('YYYY-MM-DD', 'YYYY-MM-DD') windows named in a fuzzy period, so a
    compound period naming several months/parts is fully scanned ("друга половина липня
    або після 6 серпня" -> [(2026-07-16, 2026-07-31), (2026-08-06, 2026-08-31)]). Returns
    [] when no priced month can be identified (scan left unconstrained)."""
    t = (fuzzy or "").lower()
    segments = [s.strip() for s in _PERIOD_SPLIT_RE.split(t) if s and s.strip()] or [t]
    ranges = []
    for seg in segments:
        r = _single_period_range(seg, year)
        if r and r not in ranges:
            ranges.append(r)
    return ranges


def fuzzy_period_range(fuzzy: str, year: int = _FUZZY_YEAR):
    """The FIRST inclusive window named in a fuzzy period (back-compat single-range view).
    Prefer `fuzzy_period_ranges` for compound periods. Returns None when no priced month."""
    ranges = fuzzy_period_ranges(fuzzy, year)
    return ranges[0] if ranges else None


def _in_period_dates(all_dates, ranges):
    """The subset of sorted calendar dates that fall inside ANY named period range.
    No ranges (unconstrained scan) -> all dates."""
    if not ranges:
        return list(all_dates)
    return [d for d in all_dates if any(lo <= d <= hi for lo, hi in ranges)]


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


def _assign_fitting_type(room: Dict) -> Dict:
    """Give a type-less room the smallest FITTING public type (Стандарт-first, owner Rule 1),
    so a multi-room split ("2 номери: 4 і 3") quotes each room with a real, fitting type."""
    adults = room.get("adults") or 0
    ages = room.get("children_ages") or []
    if adults == 0 and not ages:
        adults = 2
    for rt in OFFERABLE_ROOMS:                       # Стандарт, Стандарт +, Напівлюкс
        if pricing_engine.fits_room(rt, adults, ages):
            return {**room, "room_type": rt}
    return {**room, "room_type": OFFERABLE_ROOMS[-1]}  # largest (Напівлюкс) fallback


# STANDARD-PRIORITY (owner 2026-07-10): the hotel sells Стандарт / Стандарт+ first, so a big
# group is split into Стандарт-sized rooms (comfortably 3 people: base 2 + one extra bed/sofa)
# rather than packed into a few Напівлюкси.
STANDARD_COMFORT_PER_ROOM = 3


def suggest_group_distribution(adults: int, children_ages) -> List[int]:
    """Standard-first split of a group into rooms — the people count per room, biggest first.

    Constraints: <= STANDARD_COMFORT_PER_ROOM (3) people per room, and <= 3 adults per room.
    e.g. 6 adults + 4 kids (2,8,11,14) = 10 people -> [3, 3, 2, 2] (FOUR Стандарти, owner #21),
    never [3, 3, 4] which would push people into a Напівлюкс and violate the Standard priority.
    """
    ages = list(children_ages or [])
    total = adults + len(ages)
    if total <= 0:
        return []
    n = max(1,
            -(-total // STANDARD_COMFORT_PER_ROOM),                   # ceil: <=3 people per room
            -(-adults // pricing_engine.STANDARD_MAX_ADULTS))         # ceil: <=3 adults per room
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]           # biggest rooms first


def rooms_from_split(counts: List[int], adults: int, children_ages, checkin, checkout) -> List[Dict]:
    """Turn an ACCEPTED split (per-room people counts) into concrete room objects. Adults are
    SPREAD round-robin (each room gets >=1 adult where possible, <=3 adults, <= its people count),
    then children fill the remaining slots — so we never propose a kids-only room."""
    kids = list(children_ages or [])
    n = len(counts)
    a_per = [0] * n
    a_left = adults
    changed = True
    while a_left > 0 and changed:                       # round-robin one adult at a time
        changed = False
        for i in range(n):
            if a_left <= 0:
                break
            if a_per[i] < counts[i] and a_per[i] < pricing_engine.STANDARD_MAX_ADULTS:
                a_per[i] += 1
                a_left -= 1
                changed = True
    rooms = []
    for i in range(n):
        k = [kids.pop(0) for _ in range(min(counts[i] - a_per[i], len(kids)))]
        rooms.append({"room_type": None, "checkin": checkin, "checkout": checkout,
                      "adults": a_per[i], "children_count": len(k), "children_ages": k})
    return rooms


def _calendar_min_date(availability: Dict):
    """Earliest date the scraped calendar knows about (None when the scrape is empty)."""
    ds = [d for v in (availability or {}).values() for d in (v or {})]
    return min(ds) if ds else None


def _stay_before_calendar(availability: Dict, checkin) -> bool:
    """True when the requested check-in is EARLIER than anything the calendar shows — i.e. the
    dates have already passed. Data-driven (no clock), so we never claim "all rooms booked" for a
    stay that is simply in the past (owner #288/#299)."""
    lo = _calendar_min_date(availability)
    return bool(lo and checkin and str(checkin) < lo)


def _past_dates_reply(checkin, checkout) -> str:
    return templates.PAST_DATES


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


def _stay_all_free(avail_map: Dict, checkin, checkout, room_count: int = 1) -> bool:
    """THE single source of truth for "is this exact stay bookable": True IFF EVERY night in
    [checkin, checkout) has >= room_count free rooms. A date missing from the scrape counts as
    0 (booked). Owner mandate (#287/#296): a window is valid ONLY when every single one of its
    nights is free — never bridge/hallucinate over a booked day."""
    ci = pricing_engine._as_date(checkin)
    co = pricing_engine._as_date(checkout)
    if co <= ci:
        return False
    night = ci
    while night < co:
        if avail_map.get(night.isoformat(), 0) < room_count:
            return False
        night += timedelta(days=1)
    return True


def find_nearest_window(availability, room_type, after, nights, room_count=1, horizon=90):
    """Earliest bookable `nights`-night window for `room_type`, scanning from `after` forward up
    to `horizon` days. EXACT-INCLUSIVE (off starts at 0): if the requested check-in itself begins
    a fully-free block, that exact window is returned unchanged (owner #274/#288: propose the exact
    dates when free). Every candidate is gated by `_stay_all_free`, so a window is NEVER returned
    if ANY of its nights is booked — no bridging over booked days (#287)."""
    key = bot_logic.match_availability_key(availability, room_type)
    if not key:
        return None
    avail = availability.get(key) or {}
    start = pricing_engine._as_date(after)
    for off in range(0, horizon + 1):
        d0 = start + timedelta(days=off)
        co = d0 + timedelta(days=nights)
        if _stay_all_free(avail, d0.isoformat(), co.isoformat(), room_count):
            return (d0.isoformat(), co.isoformat())
    return None


def _free_windows(avail: Dict, dates: List[str], min_nights: int, room_count: int):
    """Maximal continuous free stretches (>= min_nights) within `dates` (sorted).

    Contiguity is enforced CALENDAR-DAY by calendar-day between the first and last date:
    ANY day that is missing from `dates` OR has fewer than `room_count` free rooms BREAKS
    the run. Non-contiguous free spans are therefore NEVER bridged into one window — the
    owner fix for #271 (the illegal "12 - 22 липня" merge over booked 17-18 липня).
    """
    if not dates:
        return []
    date_set = set(dates)
    lo = pricing_engine._as_date(dates[0])
    hi = pricing_engine._as_date(dates[-1])
    windows, run = [], []
    day = lo
    while day <= hi:
        iso = day.isoformat()
        if iso in date_set and avail.get(iso, 0) >= room_count:
            run.append(iso)                         # this night is free -> extend the run
        else:
            if len(run) >= min_nights:              # a booked/absent day CLOSES the run
                windows.append((run[0], run[-1]))
            run = []
        day += timedelta(days=1)
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

    ranges = fuzzy_period_ranges(fuzzy)
    in_period = _in_period_dates(all_dates, ranges)
    windows = _free_windows(avail, in_period, min_nights, room_count)
    if windows:
        return _offer(windows)

    # Nothing free inside the VISIBLE part of the named period(s).
    vis_max = all_dates[-1] if all_dates else None
    max_end = max((hi for _, hi in ranges), default=None)
    if max_end is not None and vis_max is not None and max_end > vis_max:
        # The named period EXTENDS beyond the open calendar window (OtelMS shows only
        # ~6-7 weeks ahead) and the visible slice has nothing free — we cannot honestly
        # scan the rest. Ask for exact dates instead of proposing dates from the wrong
        # month; exact out-of-window dates still quote via the 'unknown' path.
        return templates.ACKNOWLEDGE_FUZZY.replace("{fuzzy_date}", fuzzy or "ці дати")

    # The period is fully visible but booked there -> offer the nearest real window.
    nearest = _free_windows(avail, all_dates, min_nights, room_count)
    if nearest:
        return _found_nearest_reply(nearest[0])
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
    ranges = fuzzy_period_ranges(fuzzy)
    in_period = _in_period_dates(all_dates, ranges)
    windows = _free_windows(avail, in_period, min_nights, room_count)
    if not windows:
        vis_max = all_dates[-1] if all_dates else None
        max_end = max((hi for _, hi in ranges), default=None)
        if max_end is not None and vis_max is not None and max_end > vis_max:
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


def nearest_window_any(availability: Dict, after, nights: int, room_count: int = 1,
                       fit_adults=None, fit_children=None):
    """Earliest free window across ANY offerable room type — for auto-proposing the
    nearest dates when the client's exact dates are sold out (Bug 2: don't ask permission).

    When a party is given (fit_adults), only room types that can PHYSICALLY hold it are
    scanned — so we never offer a window whose only free room is too small (capacity fix
    2026-06-24)."""
    rooms = OFFERABLE_ROOMS
    if fit_adults is not None:
        rooms = [rt for rt in rooms
                 if pricing_engine.fits_room(rt, fit_adults, fit_children or [])]
    cands = [find_nearest_window(availability, rt, after, nights, room_count) for rt in rooms]
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
            return nearest_window_any(availability, ci, len(pricing_engine.night_dates(ci, co)),
                                      fit_adults=spec.get("adults"),
                                      fit_children=spec.get("children_ages"))
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
        # Show room capacities FIRST (client knows the limits), then handle the group.
        r0 = rooms[0]
        # A dates/period prefix makes a follow-up turn's message DIFFERENT from the first one, so the
        # anti-dedup doesn't silence a date the client sent right after the split proposal ("З 15 чи
        # 16 липня") and the room list isn't repeated (owner #304 + Sprint-4 Test 21). ANY captured
        # date form (exact range / a single check-in / a fuzzy period) yields a non-empty prefix.
        if r0.get("checkin") and r0.get("checkout"):
            prefix = f"На дати {dates_phrase(r0['checkin'], r0['checkout'])}: "
        elif r0.get("checkin"):
            _ci = pricing_engine._as_date(r0["checkin"])
            prefix = f"На дату {_ci.day} {_GEN_MONTHS[_ci.month]}: "
        elif r0.get("fuzzy_date"):
            prefix = f"Орієнтуємось на {r0['fuzzy_date']}: "
        else:
            prefix = ""
        cc0, known_ages0 = _child_count(r0), len(r0.get("children_ages") or [])
        if cc0 == known_ages0:
            # Owner #21: composition fully known -> PROACTIVELY propose a STANDARD-priority split.
            counts = suggest_group_distribution(r0.get("adults") or 0, r0.get("children_ages") or [])
            return {"action": "reply",
                    "split_counts": counts,
                    "reply": templates.PRESENTATION_ROOMS + "[SPLIT]"
                    + templates.SUGGEST_GROUP_SPLIT
                    .replace("{dates_prefix}", prefix)
                    .replace("{rooms}", room_count_phrase(len(counts)))
                    .replace("{distribution}", " + ".join(str(c) for c in counts))}
        # Ages still unknown -> we can't compute a precise valid split; ask how to distribute
        # (dates-prefixed so a follow-up naming the dates isn't anti-dedup-silenced either).
        return {"action": "reply",
                "reply": templates.PRESENTATION_ROOMS + "[SPLIT]" + prefix + templates.ASK_ROOM_DISTRIBUTION}

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
        if len(dated) > 1:
            # Owner fix #282-284: a requested split that packs MORE THAN 3 adults into ONE room
            # can't be honoured (max 3 adults per room). Don't just fail / cram them into a
            # Напівлюкс — suggest a VALID split into enough rooms (7 adults -> 3 номери 2+2+3).
            if any((r.get("adults") or 0) > pricing_engine.STANDARD_MAX_ADULTS for r in dated):
                total_adults = sum((r.get("adults") or 0) for r in dated)
                split = sorted(_adult_split(total_adults))
                return {"action": "reply",
                        "split_counts": split,
                        "reply": templates.SUGGEST_ADULT_SPLIT
                        .replace("{rooms}", room_count_phrase(len(split)))
                        .replace("{distribution}", " + ".join(str(a) for a in split))}
            # Multi-room split WITHOUT chosen types (e.g. "2 номери: 3 і 2") — auto-assign the
            # smallest FITTING type per room (Стандарт-first) and quote EVERY room, never just
            # the first (owner Rule 1 fix, caught live in Persona 15).
            return {"action": "quote", "rooms": [_assign_fitting_type(r) for r in dated]}
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

def _with_ubd_note(reply: str, ubd: bool) -> str:
    """Append the УБД validation note to a NON-quote reply (sold-out alternative / cross-sell)
    so a veteran knows the -20% still applies to the offered window (owner fix 2026-06-24)."""
    return (reply + "\n\n" + templates.MILITARY) if ubd else reply


def _found_nearest_reply(win) -> str:
    """SOLD_OUT_FOUND_NEAREST with BOTH the dates AND the explicit nights count filled in.
    The offered window is exactly `checkout - checkin` nights long, so the stated nights ALWAYS
    match the stated dates (owner fix #274: night count and exact dates must align)."""
    n = pricing_engine.nights_between(win[0], win[1])
    return (templates.SOLD_OUT_FOUND_NEAREST
            .replace("{dates}", dates_phrase(win[0], win[1]))
            .replace("{nights}", nights_phrase(n)))


def _room_too_small_reply(rooms: List[Dict]) -> str:
    """Multi-room booking where one or more chosen rooms can't physically hold their party.
    Name each bad room and ask to redistribute — never silently drop the valid rooms."""
    parts = []
    for r in rooms:
        rt = r.get("room_type") or "Стандарт"
        parts.append(f"«{rt}» — {guests_phrase(r.get('adults') or 0, r.get('children_ages') or [])}")
    # Fix (2026-06-24): show the room capacities FIRST, then the "doesn't fit" explanation.
    return (templates.PRESENTATION_ROOMS + "[SPLIT]"
            + templates.ROOM_TOO_SMALL.replace("{деталі}", "; ".join(parts)))


def _soldout_reply(r: Dict, simplified_availability: Dict, ubd_booking: bool) -> str:
    """The sold-out response for ONE requested room: a capacity-aware cross-sell of other FITTING
    free types on the SAME dates (Case 4), else the nearest free window (Case 5), else NEAREST_NONE."""
    room_type = r.get("room_type")
    checkin = r.get("checkin")
    nights = pricing_engine.night_dates(checkin, r.get("checkout"))
    free = [rt for rt in bot_logic.free_room_types(simplified_availability, nights)
            if pricing_engine.fits_room(rt, r.get("adults") or 0, r.get("children_ages") or [])]
    if free:  # other FITTING categories free on the SAME dates -> cross-sell
        return _with_ubd_note(templates.ROOM_BOOKED
                              .replace("{тип номеру}", room_type)
                              .replace("{вільні_номери}", ", ".join(free)), ubd_booking)
    win = find_nearest_window(simplified_availability, room_type, checkin, len(nights))
    if not win:  # never ask permission — scan ANY fitting room type before giving up
        win = nearest_window_any(simplified_availability, checkin, len(nights),
                                 fit_adults=r.get("adults"), fit_children=r.get("children_ages"))
    if win:
        return _with_ubd_note(_found_nearest_reply(win), ubd_booking)
    return templates.NEAREST_NONE


def finalize_quote(rooms: List[Dict], simplified_availability: Dict, engine=ENGINE) -> str:
    """Gate on availability, compute the price deterministically, format rigidly.

    AVAILABILITY GATING: a sold-out room is never quoted. For a SINGLE room -> the sold-out
    alternative (cross-sell / nearest window). For a MULTI-room booking, a sold-out room must NOT
    short-circuit the others (owner #23): the available rooms are quoted and the booked ones are
    flagged separately. УБД (-20%) applies to the WHOLE booking, so the MILITARY note is appended
    to sold-out alternatives when flagged.
    """
    ubd_booking = any(bool(r.get("ubd")) for r in rooms)
    priced = []
    too_small = []
    sold_out = []
    for r in rooms:
        room_type = r.get("room_type")
        checkin, checkout = r.get("checkin"), r.get("checkout")
        nights = pricing_engine.night_dates(checkin, checkout)

        # Owner #299: dates EARLIER than the whole visible calendar have already passed. Say so —
        # never report "all rooms booked" and never invent an offset window for a past stay.
        if _stay_before_calendar(simplified_availability, checkin):
            return _past_dates_reply(checkin, checkout)

        status = bot_logic.is_room_available(simplified_availability, room_type, nights)
        if status == "sold_out":
            if len(rooms) == 1:
                return _soldout_reply(r, simplified_availability, ubd_booking)
            sold_out.append(r)      # multi-room: collect, handle after the loop (never drop the rest)
            continue

        adults = r.get("adults") or 0
        children_ages = r.get("children_ages") or []
        if adults == 0 and not children_ages:
            adults = 2  # sensible default if the extractor missed the count
        try:
            guests = pricing_engine.make_guests(adults=adults, children_ages=children_ages)
            quote = engine.quote(room_type, checkin, checkout, guests)
        except pricing_engine.OffSeasonError:
            return templates.OFF_SEASON
        except pricing_engine.OverCapacityError:
            # Chosen room is physically too small for the party. Collect it and handle AFTER
            # the loop so a multi-room booking never silently drops its other (valid) rooms.
            too_small.append(r)
            continue
        except KeyError:
            return templates.PRESENTATION_ROOMS  # unknown room type -> present options

        priced.append({
            "room_type": quote.room_type, "adults": adults, "children_ages": children_ages,
            "checkin": checkin, "checkout": checkout, "nights": quote.nights,
            "price": quote.total,
        })

    # A chosen room can't physically hold its party. Single-room booking -> show the room
    # types that DO fit; multi-room -> name the bad split and ask to adjust (never silently
    # drop the valid rooms — review fix 2026-06-24).
    if too_small:
        if len(rooms) == 1:
            return finalize_quote_all(too_small[0], simplified_availability)
        return _room_too_small_reply(too_small)

    # Multi-room booking where SOME rooms are sold out (owner #23): quote the available rooms and
    # flag the booked ones separately — never drop a valid room because a sibling is booked.
    if sold_out:
        if not priced:
            return _soldout_reply(sold_out[0], simplified_availability, ubd_booking)
        types = ", ".join(dict.fromkeys(r.get("room_type") for r in sold_out if r.get("room_type")))
        return (build_quote_reply(priced, ubd_booking) + "\n\n"
                + templates.PARTIAL_MULTIROOM_SOLDOUT.replace("{зайняті}", types))

    # УБД (2026-06-23): -20% applies to the WHOLE booking (a veteran's family), so a single
    # flagged room discounts the entire total. The discount is rendered on the grand total.
    return build_quote_reply(priced, ubd_booking)


# Owner room-distribution priority (2026-07-06): Стандарт / Стандарт+ FIRST (the hotel has
# far more of them); Напівлюкс is the secondary / family option. A group of 4+ adults is
# offered several standard rooms; a small family gets a single Стандарт/Стандарт+.
STANDARD_TYPES = ["Стандарт", "Стандарт +"]


def _min_free_on_nights(availability: Dict, room_type: str, nights: List[str]) -> int:
    """Fewest free rooms of `room_type` across every requested night (0 if absent)."""
    key = bot_logic.match_availability_key(availability, room_type)
    if not key or not nights:
        return 0
    avail = availability.get(key) or {}
    return min((avail.get(d, 0) for d in nights), default=0)


def _adult_split(adults: int, max_per: int = pricing_engine.STANDARD_MAX_ADULTS) -> List[int]:
    """Split N adults across the fewest standard rooms, evenly (4 -> [2,2]; 5 -> [3,2])."""
    rooms = max(1, -(-adults // max_per))          # ceil division
    base, rem = divmod(adults, rooms)
    return [base + (1 if i < rem else 0) for i in range(rooms)]


def _standard_split_options(adults, children_ages, checkin, checkout, nights,
                            availability, engine, ubd):
    """Owner rule 2026-07-06: a group of 4+ adults is offered MULTIPLE Стандарт / Стандарт+
    rooms FIRST (more inventory) rather than a single Напівлюкс. Returns
    [(room_type, split_list, total_price), ...] for the standard types that both physically
    fit each split room AND have enough free rooms on every night."""
    if adults < 4 or children_ages:      # only pure-adult groups split into standard rooms
        return []
    split = _adult_split(adults)
    n = len(split)
    if n < 2:
        return []
    out = []
    for rt in STANDARD_TYPES:
        if any(not pricing_engine.fits_room(rt, a, []) for a in split):
            continue
        if _min_free_on_nights(availability, rt, nights) < n:
            continue
        try:
            total = sum(engine.quote(rt, checkin, checkout,
                                     pricing_engine.make_guests(adults=a)).total for a in split)
        except (pricing_engine.OffSeasonError, KeyError, pricing_engine.OverCapacityError):
            continue
        if ubd:
            total = pricing_engine.apply_military_discount(total)
        out.append((rt, split, total))
    return out


def finalize_quote_all(spec: Dict, simplified_availability: Dict, engine=ENGINE) -> str:
    """Exact dates but no chosen room -> offer rooms by the owner's priority (2026-07-06):
    Стандарт / Стандарт+ first (a group of 4+ adults gets several of them); Напівлюкс is the
    secondary / family option. Skips sold-out / unpriced / over-capacity types; nothing free
    -> nearest window or NEAREST_NONE.
    """
    checkin, checkout = spec.get("checkin"), spec.get("checkout")
    nights = pricing_engine.night_dates(checkin, checkout)
    adults = spec.get("adults") or 0
    children_ages = spec.get("children_ages") or []
    if adults == 0 and not children_ages:
        adults = 2
    ubd = bool(spec.get("ubd"))

    # Owner #299: the requested stay is entirely before the visible calendar -> it has passed.
    if _stay_before_calendar(simplified_availability, checkin):
        return _past_dates_reply(checkin, checkout)

    # Availability status per public type on the requested nights. When the scrape COVERS this
    # window (at least one type has a definite available/sold_out status) we recommend ONLY types
    # confirmed AVAILABLE — never a type OtelMS didn't confirm free (owner fix #275: no blind
    # recommendations). A window entirely OUTSIDE the visible calendar (every type 'unknown')
    # stays lenient (far-future exact dates still list all priced types).
    statuses = {rt: bot_logic.is_room_available(simplified_availability, rt, nights)
                for rt in OFFERABLE_ROOMS}
    window_covered = any(s != "unknown" for s in statuses.values())

    # Single-room options that FIT the party, in Стандарт-first order (Напівлюкс stays LAST).
    single = []  # (room_type, price)
    for room_type in OFFERABLE_ROOMS:
        status = statuses[room_type]
        if status == "sold_out":
            continue
        if window_covered and status != "available":
            continue  # Fix #275: within a covered window, only offer confirmed-available types
        try:
            guests = pricing_engine.make_guests(adults=adults, children_ages=children_ages)
            quote = engine.quote(room_type, checkin, checkout, guests)
        except (pricing_engine.OffSeasonError, KeyError, pricing_engine.OverCapacityError):
            continue  # off-season / unknown / physically can't hold the party -> skip
        price = pricing_engine.apply_military_discount(quote.total) if ubd else quote.total
        single.append((room_type, price))

    # Owner priority: a group of 4+ adults -> several Стандарт / Стандарт+ rooms FIRST.
    splits = _standard_split_options(adults, children_ages, checkin, checkout, nights,
                                     simplified_availability, engine, ubd)

    if not single and not splits:
        # Owner #19 (2026-07-10): the party fits no AVAILABLE single room on these dates (too big,
        # or the only fitting type — Напівлюкс — is booked). Before jumping to another date, offer a
        # STANDARD-priority split across the types that are ACTUALLY free on these nights.
        counts = suggest_group_distribution(adults, children_ages)
        if len(counts) > 1:
            free_std = [rt for rt in STANDARD_TYPES
                        if _min_free_on_nights(simplified_availability, rt, nights) >= 1]
            total_free = sum(_min_free_on_nights(simplified_availability, rt, nights)
                             for rt in STANDARD_TYPES)
            if free_std and total_free >= len(counts):
                reply = (templates.SUGGEST_STANDARD_SPLIT
                         .replace("{dates}", dates_phrase(checkin, checkout))
                         .replace("{nights}", nights_phrase(len(nights)))
                         .replace("{guests}", guests_phrase(adults, children_ages))
                         .replace("{rooms}", room_count_phrase(len(counts)))
                         .replace("{distribution}", " + ".join(str(c) for c in counts))
                         .replace("{типи}", ", ".join(free_std)))
                return _with_ubd_note(reply, ubd)

        # Everything sold out -> AUTO-propose the nearest free window (don't ask permission);
        # only NEAREST_NONE if nothing free in the whole window. УБД -> append MILITARY note.
        win = nearest_window_any(simplified_availability, checkin, len(nights),
                                 fit_adults=adults, fit_children=children_ages)
        if win:
            return _with_ubd_note(_found_nearest_reply(win), ubd)
        return templates.NEAREST_NONE

    dates_ph = dates_phrase(checkin, checkout)
    nights_ph = nights_phrase(len(nights))
    guests_ph = guests_phrase(adults, children_ages)

    # Owner fix #266: put the exact "(з урахуванням знижки УБД -20%)" note on the price line
    # itself, so the discount is unmistakable on the FIRST priced message.
    ubd_note = " (з урахуванням знижки УБД -20%)" if ubd else ""

    if splits:
        # Group of adults -> several standard rooms PRIMARY; a single Напівлюкс only as a
        # secondary "roomier" option (owner rule 2026-07-06).
        lines = [f"{_ROOM_EMOJI.get(rt, '•')} {room_count_phrase(len(split))} {rt} "
                 f"(розподіл {' + '.join(str(a) for a in split)} дорослих) — {total} грн{ubd_note}"
                 for rt, split, total in splits]
        napiv = next((p for (t, p) in single if pricing_engine._is_napivlux(t)), None)
        fallback = (f"\nАбо один просторий Напівлюкс — {napiv} грн{ubd_note} (радше для родини з дітьми)."
                    if napiv is not None else "")
        # Owner fix #266: dropped the "(Стандартів у нас більше, ніж Напівлюксів)" aside.
        header = (f"На дати {dates_ph} ({nights_ph}) для {guests_ph} рекомендуємо кілька "
                  f"окремих номерів:")
        reply = header + "\n" + "\n".join(lines) + fallback + "\nЯкий варіант обираєте? 💙"
        if ubd:
            reply += "\n\n" + templates.MILITARY
        return reply

    # Small party / family -> single-room listing, Стандарт/Стандарт+ first, Напівлюкс last.
    lines = [f"{_ROOM_EMOJI.get(rt, '•')} {rt} — {price} грн{ubd_note}" for (rt, price) in single]
    header = f"На дати {dates_ph} ({nights_ph}) для {guests_ph} доступні такі номери:"

    # Family recommendation (owner rule 2026-07-06, REVERSED from the old Напівлюкс-first):
    # for a family with children, prioritise Стандарт / Стандарт+ (double bed + sofa);
    # Напівлюкс is the roomier LAST resort.
    family_note = ""
    if children_ages and any(t in ("Стандарт", "Стандарт +") for (t, _) in single):
        family_note = ("\n💡 Для вашої компанії добре підійде Стандарт або Стандарт+ "
                       "(з диваном/додатковим ліжком для діток). Напівлюкс — просторіший "
                       "варіант, якщо забажаєте.")

    reply = header + "\n" + "\n".join(lines) + family_note + "\nЯкий тип номеру обираєте? 💙"
    if ubd:
        reply += "\n\n" + templates.MILITARY
    return reply


# --- meals (харчування) — deterministic food math (owner 2026-07-10) -------

def meals_month(slots: Dict) -> Optional[str]:
    """The UA month name to price meals in: the booking's check-in month, else the fuzzy period's
    month. None when we cannot tell (then we don't guess a price)."""
    for r in slots.get("rooms") or []:
        if r.get("checkin"):
            try:
                return pricing_engine.month_name_uk(pricing_engine._as_date(r["checkin"]))
            except Exception:
                pass
    for r in slots.get("rooms") or []:
        if r.get("fuzzy_date"):
            m = _fuzzy_month(str(r["fuzzy_date"]).lower())
            if m:
                return pricing_engine._UA_MONTHS[m]
    return None


def _meal_days(meals: Dict, key: str) -> int:
    v = meals.get(key)
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def has_meal_request(meals) -> bool:
    """True when the extractor captured an ACTIONABLE meal-cost request (>=1 day of something)."""
    if not isinstance(meals, dict):
        return False
    return any(_meal_days(meals, k) > 0 for k in pricing_engine.MEAL_KEYS)


def meals_key(meals) -> tuple:
    """A stable key for a meal request, so an already-answered calc isn't re-emitted every turn."""
    if not isinstance(meals, dict):
        return ()
    return tuple(_meal_days(meals, k) for k in pricing_engine.MEAL_KEYS) + (meals.get("persons"),)


def finalize_meals(meals: Dict, month_uk: str, default_persons: int = 0) -> Optional[str]:
    """Exact food cost, e.g. 3-разове × 2 дні + сніданок × 1 день for 4 people in Серпень:
    (1100*4*2) + (350*4*1) = 10200 грн. Returns None when we can't price it."""
    if not has_meal_request(meals) or not month_uk:
        return None
    persons = meals.get("persons")
    try:
        persons = int(persons)
        if persons <= 0:
            raise ValueError
    except (TypeError, ValueError):
        persons = int(default_persons or 0)     # extractor gave null/garbage -> use the guest count
    if persons <= 0:
        return None
    try:
        q = pricing_engine.meal_cost(
            ENGINE.data, month_uk, persons,
            three_meals_days=_meal_days(meals, "three_meals_days"),
            two_meals_days=_meal_days(meals, "two_meals_days"),
            breakfast_days=_meal_days(meals, "breakfast_days"),
            lunch_days=_meal_days(meals, "lunch_days"),
            dinner_days=_meal_days(meals, "dinner_days"))
    except (pricing_engine.OffSeasonError, KeyError, ValueError):
        return None
    if not q.lines:
        return None
    return (templates.FOOD_CALCULATION
            .replace("{persons}", str(q.persons))
            .replace("{lines}", "\n".join(q.lines))
            .replace("{total}", str(q.total)))


def bed_availability_reply(checkin, checkout, raw_availability) -> str:
    """Bug 1 (owner Sprint 5): answer a bed-configuration question with REAL availability on the
    client's dates. `raw_availability` is the UN-folded scraper output (per OtelMS sub-type), so we
    can tell whether a separate-bed room AND a double-bed room are actually free on every night."""
    nights = pricing_engine.night_dates(checkin, checkout)
    avail = bot_logic.bed_config_availability(raw_availability, nights)
    dates = dates_phrase(checkin, checkout)
    if avail["separate"] and avail["double"]:
        tmpl = templates.BED_AVAIL_BOTH
    elif avail["separate"]:
        tmpl = templates.BED_AVAIL_SEP_ONLY
    elif avail["double"]:
        tmpl = templates.BED_AVAIL_DBL_ONLY
    else:
        tmpl = templates.BED_AVAIL_NEITHER
    return tmpl.replace("{dates}", dates)


def nearest_reply(spec: Dict, availability: Dict) -> str:
    """A2 Step 3: forward-scan for the chosen room and propose real nearest dates."""
    room = spec.get("room_type")
    after = spec.get("checkin")
    nights = _nights(spec)
    if not (room and after and nights):   # Fix 2: need room + exact start + nights to scan
        return templates.QUESTION_ONLY_DATES
    win = find_nearest_window(availability, room, after, nights)
    if win:
        return _found_nearest_reply(win)
    return templates.NEAREST_NONE
