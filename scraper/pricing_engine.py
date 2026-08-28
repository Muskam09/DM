"""
pricing_engine.py — Deterministic reference implementation of D&T Hotel pricing.

This module is the single, testable source of truth for the hotel's money math.
The live bot (bot_server.py) still asks the LLM to do the calculation inside
<THINK>, but every rule it is instructed to follow is encoded — and unit tested —
here, so the expected numbers can never drift from the spec without a test failing.

Business rules (authoritative, see project_spec.md §5 and skills.md):

* Nights      = checkout_date - checkin_date (the checkout day is never charged
                and never checked for availability).
* Weekday vs. weekend tariff is decided per night, by the day the guest SLEEPS:
    - "вихідні" (weekend rate): the night of Friday and the night of Saturday.
    - "будні"  (weekday rate): Sunday, Monday, Tuesday, Wednesday, Thursday.
* Base room price ("вартість_кімнати") covers up to BASE_CAPACITY (2) paying guests
  — for EVERY room type, including Напівлюкс (extra guests 3+ pay an extra place).
* Children / extra places (per night, added on top of the room price). Owner rule
  (2026-06-23):
    - age < 6   (0–5)       -> FREE (0), shares the parents' bed.
    - 6 <= age < 12 (6–11)  -> charged "дитяче_місце"   (a 50% extra-bed rate).
    - age >= 12 OR an adult -> charged "додаткове_місце" (full extra-bed rate),
      but only for guests BEYOND the base capacity.
* Single occupancy: when exactly one paying guest stays in a room that has an
  "одномісне_поселення" rate, that rate ALWAYS replaces "вартість_кімнати".

Only the summer months June/July/August ("Червень"/"Липень"/"Серпень") have a
price table; other months raise a clear error.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

# --- Constants -------------------------------------------------------------

BASE_CAPACITY = 2  # guests covered by the base room price before extra-place fees

# Child age tiers (owner rule 2026-06-23), as half-open intervals:
#   [0, FREE_CHILD_MAX_AGE)       -> free            (0–5)
#   [FREE_CHILD_MAX_AGE, CHILD_PLACE_MAX_AGE) -> дитяче_місце  (6–11)
#   [CHILD_PLACE_MAX_AGE, ∞)      -> додаткове_місце (12+)
FREE_CHILD_MAX_AGE = 6
CHILD_PLACE_MAX_AGE = 12

# Hard physical occupancy per public room class (owner rule 2026-06-24). Standard-class
# rooms (Стандарт / Стандарт +, incl. their sub-types) hold at most 3 adults AND 4 people
# total; a 3rd-adult party may add at most ONE child UNDER 12 (a child 12+ counts as an
# adult). Напівлюкс holds 5 people. Over-capacity room types are filtered out of quotes.
STANDARD_MAX_ADULTS = 3
STANDARD_MAX_TOTAL = 4
NAPIVLUX_MAX_TOTAL = 5

# Monday=0 ... Sunday=6 (datetime.date.weekday()). Friday=4, Saturday=5.
WEEKEND_NIGHTS = {4, 5}

_UA_MONTHS = {
    1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень", 5: "Травень",
    6: "Червень", 7: "Липень", 8: "Серпень", 9: "Вересень", 10: "Жовтень",
    11: "Листопад", 12: "Грудень",
}

CHILD_PLACE_KEY = "дитяче_місце"
EXTRA_PLACE_KEY = "додаткове_місце"

# Only the summer months are priced today; everything else is off-season.
PRICED_MONTHS = frozenset({6, 7, 8, 9, 10})  # June–October

# A single booking may span at most this many rooms (multi-room calculation).
MAX_ROOMS_PER_BOOKING = 8

# Combat-veteran (УБД) discount: a strict 20% off the room total.
MILITARY_DISCOUNT_RATE = 0.20

_DEFAULT_PRICING_PATH = os.path.join(os.path.dirname(__file__), "pricing.json")


class OffSeasonError(KeyError):
    """Raised when a stay falls in a month that has no price table (Sept–May)."""


class OverCapacityError(ValueError):
    """Raised when a room type physically cannot hold the requested party (owner rule
    2026-06-24). Callers filter that room type out of the quotes."""


# --- Data models -----------------------------------------------------------

@dataclass
class Guest:
    """A single guest. Adults have age=None; children carry their age in years."""
    is_adult: bool = True
    age: Optional[int] = None

    @classmethod
    def adult(cls) -> "Guest":
        return cls(is_adult=True, age=None)

    @classmethod
    def child(cls, age: int) -> "Guest":
        return cls(is_adult=False, age=age)


@dataclass
class Quote:
    total: int
    nights: int
    weekday_nights: int
    weekend_nights: int
    room_type: str
    breakdown: List[str] = field(default_factory=list)


@dataclass
class MultiQuote:
    """Total of several rooms booked together (multi-room booking, up to 8)."""
    total: int
    rooms: List[Quote] = field(default_factory=list)


# --- Helpers ---------------------------------------------------------------

def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.strptime(value, "%Y-%m-%d").date()
    raise TypeError(f"Unsupported date value: {value!r}")


def nights_between(checkin, checkout) -> int:
    """Number of paid nights = checkout - checkin. The checkout day is not paid."""
    ci, co = _as_date(checkin), _as_date(checkout)
    n = (co - ci).days
    if n <= 0:
        raise ValueError(f"Checkout {co} must be after checkin {ci}")
    return n


def is_weekend_night(night: date) -> bool:
    """A night is a weekend night if you sleep on Friday or Saturday."""
    return night.weekday() in WEEKEND_NIGHTS


def month_name_uk(d: date) -> str:
    return _UA_MONTHS[d.month]


def is_priced_month(value) -> bool:
    """True if the given date's month has a price table (summer only)."""
    return _as_date(value).month in PRICED_MONTHS


def stay_is_priced(checkin, checkout) -> bool:
    """True only if EVERY night of the stay falls in a priced month."""
    ci, co = _as_date(checkin), _as_date(checkout)
    night = ci
    while night < co:
        if night.month not in PRICED_MONTHS:
            return False
        night += timedelta(days=1)
    return True


def apply_military_discount(total: int) -> int:
    """Apply the strict 20% УБД (combat veteran) discount to a room total."""
    return round(total * (1 - MILITARY_DISCOUNT_RATE))


def night_dates(checkin, checkout) -> List[str]:
    """List of 'YYYY-MM-DD' for each night slept (checkout day excluded)."""
    ci, co = _as_date(checkin), _as_date(checkout)
    out = []
    night = ci
    while night < co:
        out.append(night.isoformat())
        night += timedelta(days=1)
    return out


def child_place_key(age: int) -> Optional[str]:
    """Which extra-place rate a child of the given age pays (None == free).

    Owner rule (2026-06-23): under 6 free; 6–11 -> дитяче_місце; 12+ -> додаткове_місце.
    """
    if age < FREE_CHILD_MAX_AGE:       # 0–5
        return None
    if age < CHILD_PLACE_MAX_AGE:      # 6–11
        return CHILD_PLACE_KEY
    return EXTRA_PLACE_KEY             # 12+


def _is_free_child(g: "Guest") -> bool:
    """A child under 6 stays free and never occupies a paid slot."""
    return (not g.is_adult) and g.age is not None and g.age < FREE_CHILD_MAX_AGE


def _is_full_rate_guest(g: "Guest") -> bool:
    """Adults and children 12+ pay the FULL extra place (додаткове_місце)."""
    return g.is_adult or (g.age is not None and g.age >= CHILD_PLACE_MAX_AGE)


def _is_napivlux(room_type: str) -> bool:
    return "".join((room_type or "").lower().split()).startswith(("напівлюкс", "напивлюкс"))


def fits_room(room_type: str, adults: int, children_ages: Optional[List[int]] = None) -> bool:
    """Physical occupancy gate (owner rule FINALISED 2026-07-11).

    Стандарт / Стандарт + (and their sub-types, e.g. the 'Сімейний В+Д' rooms with a sofa):
      * a child aged **12+** counts as an ADULT (адульт-еквівалент);
      * MAX **3 adult-equivalents** AND MAX **4 people** total.
    So 3 adults + 1 child <12 FITS (the 4th on the sofa), and "2 adults + 14yo + 9yo" fits too
    (= 3 adult-equivalents + 1 child <12). 4 adults, 3 adults + a 12+ child, or 5 people do NOT.
    Напівлюкс: MAX 5 people.
    """
    ages = children_ages or []
    total = adults + len(ages)
    if _is_napivlux(room_type):
        return total <= NAPIVLUX_MAX_TOTAL
    adult_equiv = adults + sum(1 for a in ages if a is not None and a >= CHILD_PLACE_MAX_AGE)
    return adult_equiv <= STANDARD_MAX_ADULTS and total <= STANDARD_MAX_TOTAL


# --- Pricing engine --------------------------------------------------------

class PricingEngine:
    def __init__(self, pricing_data: Optional[dict] = None,
                 pricing_path: str = _DEFAULT_PRICING_PATH):
        if pricing_data is None:
            with open(pricing_path, "r", encoding="utf-8") as f:
                pricing_data = json.load(f)
        self.data = pricing_data
        self.prices = pricing_data["ціни_по_категоріях"]

    # -- room-type resolution ----------------------------------------------

    def resolve_room_type(self, room_type: str) -> str:
        """Map a loosely-typed room name to a canonical pricing.json key.

        Handles spacing around '+' ("Стандарт+" -> "Стандарт +") and case.
        """
        if room_type in self.prices:
            return room_type

        def norm(s: str) -> str:
            return "".join(s.lower().split())

        target = norm(room_type)
        for key in self.prices:
            if norm(key) == target:
                return key
        raise KeyError(f"Unknown room type: {room_type!r}")

    def _rates_for_night(self, room_type: str, night: date) -> dict:
        if night.month not in PRICED_MONTHS:
            raise OffSeasonError(
                f"No pricing for {month_name_uk(night)} ({night.isoformat()}); "
                f"off-season (only {sorted(PRICED_MONTHS)} are priced)."
            )
        month = month_name_uk(night)
        try:
            month_table = self.prices[room_type][month]
        except KeyError as exc:
            raise KeyError(
                f"No price table for room {room_type!r} in month {month!r}"
            ) from exc
        tariff = "вихідні" if is_weekend_night(night) else "будні"
        return month_table[tariff]

    # -- extra-place categorisation ----------------------------------------

    @staticmethod
    def _extra_place_keys(guests: List[Guest]) -> List[Optional[str]]:
        """Return the per-guest extra-place rate key for guests BEYOND base
        capacity. Children under 6 are always free and never occupy a paid slot.

        The base capacity is filled by the most expensive occupants first
        (adults / children 12+, then 6-11 children), so the cheapest guests
        end up as the charged "extras" — the customer-friendly reading that
        matches project_spec.md Case 7 (2 adults + 1 child 8 => child pays).
        """
        # Free children never count toward capacity or charges.
        paying = [g for g in guests if not _is_free_child(g)]

        def cost_rank(g: Guest) -> int:
            # Higher rank = more expensive as an extra -> fills base capacity first.
            return 2 if _is_full_rate_guest(g) else 1  # додаткове vs дитяче (6-11)

        ordered = sorted(paying, key=cost_rank, reverse=True)
        extras = ordered[BASE_CAPACITY:]

        return [EXTRA_PLACE_KEY if _is_full_rate_guest(g) else CHILD_PLACE_KEY
                for g in extras]

    @staticmethod
    def _paying_guest_count(guests: List[Guest]) -> int:
        return sum(1 for g in guests if not _is_free_child(g))

    # -- public API ---------------------------------------------------------

    def quote(self, room_type, checkin, checkout, guests: List[Guest]) -> Quote:
        room = self.resolve_room_type(room_type)
        ci, co = _as_date(checkin), _as_date(checkout)
        n = nights_between(ci, co)
        if not guests:
            raise ValueError("At least one guest is required")

        # Hard physical occupancy gate (owner rule 2026-06-24): a room type that cannot hold
        # the party is rejected so callers drop it from the offered options.
        adults = sum(1 for g in guests if g.is_adult)
        child_ages = [g.age for g in guests if not g.is_adult and g.age is not None]
        if not fits_room(room, adults, child_ages):
            raise OverCapacityError(
                f"{room} cannot hold {adults} adults + {len(child_ages)} children "
                f"(standard class max {STANDARD_MAX_ADULTS} adults / {STANDARD_MAX_TOTAL} total)")

        extra_keys = self._extra_place_keys(guests)
        single_occupancy = self._paying_guest_count(guests) == 1

        total = 0
        weekday_nights = 0
        weekend_nights = 0
        breakdown: List[str] = []

        night = ci
        while night < co:
            rates = self._rates_for_night(room, night)
            if is_weekend_night(night):
                weekend_nights += 1
                tariff = "вихідні"
            else:
                weekday_nights += 1
                tariff = "будні"

            single_rate = rates.get("одномісне_поселення")
            if single_occupancy and single_rate:
                room_rate = single_rate
            else:
                room_rate = rates["вартість_кімнати"]

            night_total = room_rate
            extras_desc = []
            for key in extra_keys:
                fee = rates[key]
                night_total += fee
                extras_desc.append(f"{key}={fee}")

            extra_str = (" + " + " + ".join(extras_desc)) if extras_desc else ""
            breakdown.append(
                f"{night.isoformat()} ({tariff}): {room_rate}{extra_str} = {night_total}"
            )
            total += night_total
            night += timedelta(days=1)

        return Quote(
            total=total,
            nights=n,
            weekday_nights=weekday_nights,
            weekend_nights=weekend_nights,
            room_type=room,
            breakdown=breakdown,
        )

    def price(self, room_type, checkin, checkout, guests: List[Guest]):
        """Convenience wrapper returning only the total price in UAH, or None when the
        party physically does not fit the room type (owner capacity gate 2026-06-24)."""
        try:
            return self.quote(room_type, checkin, checkout, guests).total
        except OverCapacityError:
            return None

    def quote_multiple(self, bookings) -> MultiQuote:
        """Quote several rooms in one booking and sum them (multi-room, <=8 rooms).

        Each booking is a dict {room_type, checkin, checkout, guests} or a
        (room_type, checkin, checkout, guests) tuple. Mirrors the per-room then
        sum logic the bot performs in <THINK> for "we want 2+ rooms" requests.
        """
        bookings = list(bookings)
        if not bookings:
            raise ValueError("At least one room booking is required")
        if len(bookings) > MAX_ROOMS_PER_BOOKING:
            raise ValueError(
                f"At most {MAX_ROOMS_PER_BOOKING} rooms per booking (got {len(bookings)})"
            )
        quotes: List[Quote] = []
        for b in bookings:
            if isinstance(b, dict):
                quotes.append(self.quote(b["room_type"], b["checkin"],
                                         b["checkout"], b["guests"]))
            else:
                quotes.append(self.quote(*b))
        return MultiQuote(total=sum(q.total for q in quotes), rooms=quotes)


# --- Meal (харчування) pricing --------------------------------------------
# Owner 2026-07-10: the bot must compute the exact food cost, e.g. a 3-day stay eating full
# board for 2 days and only breakfast on the last day, for 4 people, in August:
#   (1100 * 4 * 2) + (350 * 4 * 1) = 10200 грн
# Combo ("комплексне") prices are PER PERSON PER DAY and depend on the month; single meals
# (сніданок/обід/вечеря) are flat per person per serving.

MEAL_KEYS = ("three_meals_days", "two_meals_days", "breakfast_days", "lunch_days", "dinner_days")


@dataclass
class MealQuote:
    total: int
    persons: int
    lines: List[str] = field(default_factory=list)


def meal_prices(pricing_data: dict, month_uk: str) -> dict:
    """{'3-разове','2-разове','сніданок','обід','вечеря'} unit prices for a given UA month name.

    Reads the owner's `Харчування[<month>]["будні/вихідні"]` block (meal prices don't vary by day
    of week): {"2-разове", "3-разове", "окремо": {"сніданок","обід","вечеря"}}."""
    food = pricing_data.get("Харчування") or pricing_data.get("харчування") or {}
    month = food.get(month_uk)
    if not month:
        raise OffSeasonError(f"No meal prices for month {month_uk!r}")
    block = month.get("будні/вихідні") or next(iter(month.values()))
    return {"2-разове": block["2-разове"], "3-разове": block["3-разове"], **block["окремо"]}


def meal_cost(pricing_data: dict, month_uk: str, persons: int, three_meals_days: int = 0,
              two_meals_days: int = 0, breakfast_days: int = 0, lunch_days: int = 0,
              dinner_days: int = 0) -> MealQuote:
    """Deterministic food total. Each component = unit_price * persons * days."""
    if persons <= 0:
        raise ValueError("persons must be >= 1")
    p = meal_prices(pricing_data, month_uk)
    parts = [
        ("3-разове харчування", p["3-разове"], three_meals_days),
        ("2-разове харчування", p["2-разове"], two_meals_days),
        ("Сніданок", p["сніданок"], breakfast_days),
        ("Обід", p["обід"], lunch_days),
        ("Вечеря", p["вечеря"], dinner_days),
    ]
    total, lines = 0, []
    for label, unit, days in parts:
        days = int(days or 0)
        if days <= 0:
            continue
        sub = unit * persons * days
        total += sub
        lines.append(f"• {label}: {unit} грн × {persons} ос. × {days} дн. = {sub} грн")
    return MealQuote(total=total, persons=persons, lines=lines)


# --- Convenience parsing for guest specs -----------------------------------

def make_guests(adults: int = 0, children_ages: Optional[List[int]] = None) -> List[Guest]:
    guests = [Guest.adult() for _ in range(adults)]
    for age in (children_ages or []):
        guests.append(Guest.child(age))
    return guests


if __name__ == "__main__":
    engine = PricingEngine()
    # Case 3: Standard+, 28-29 June 2026, 2 adults, 1 weekday night.
    q3 = engine.quote("Стандарт +", "2026-06-28", "2026-06-29", make_guests(adults=2))
    print("Case 3:", q3.total, q3.breakdown)
    # Case 7: Standard, 6-8 July 2026, 2 adults + child 8, 2 weekday nights.
    q7 = engine.quote("Стандарт", "2026-07-06", "2026-07-08", make_guests(adults=2, children_ages=[8]))
    print("Case 7:", q7.total, q7.breakdown)
