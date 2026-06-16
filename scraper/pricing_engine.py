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
* Base room price ("вартість_кімнати") covers up to BASE_CAPACITY (2) paying guests.
* Children / extra places (per night, added on top of the room price):
    - age <= 6              -> FREE (0), shares the parents' bed.
    - 7 <= age <= 12        -> charged "дитяче_місце"   (a 50% extra-bed rate).
    - age > 12 OR an adult  -> charged "додаткове_місце" (full extra-bed rate),
      but only for guests BEYOND the base capacity.
* Single occupancy: when exactly one paying guest stays in a room that has an
  "одномісне_поселення" rate, that rate replaces "вартість_кімнати".

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
PRICED_MONTHS = frozenset({6, 7, 8})

# A single booking may span at most this many rooms (multi-room calculation).
MAX_ROOMS_PER_BOOKING = 8

_DEFAULT_PRICING_PATH = os.path.join(os.path.dirname(__file__), "pricing.json")


class OffSeasonError(KeyError):
    """Raised when a stay falls in a month that has no price table (Sept–May)."""


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


def child_place_key(age: int) -> Optional[str]:
    """Which extra-place rate a child of the given age pays (None == free)."""
    if age <= 6:
        return None
    if age <= 12:
        return CHILD_PLACE_KEY
    return EXTRA_PLACE_KEY


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
        capacity. Children <=6 are always free and never occupy a paid slot.

        The base capacity is filled by the most expensive occupants first
        (adults / children >12, then 6-12 children), so the cheapest guests
        end up as the charged "extras" — the customer-friendly reading that
        matches project_spec.md Case 7 (2 adults + 1 child 8 => child pays).
        """
        # Free children never count toward capacity or charges.
        paying = [g for g in guests if not (not g.is_adult and g.age is not None and g.age <= 6)]

        def cost_rank(g: Guest) -> int:
            # Higher rank = more expensive as an extra -> fills base capacity first.
            if g.is_adult or (g.age is not None and g.age > 12):
                return 2  # додаткове_місце
            return 1      # дитяче_місце (children 7-12)

        ordered = sorted(paying, key=cost_rank, reverse=True)
        extras = ordered[BASE_CAPACITY:]

        keys: List[Optional[str]] = []
        for g in extras:
            if g.is_adult or (g.age is not None and g.age > 12):
                keys.append(EXTRA_PLACE_KEY)
            else:
                keys.append(CHILD_PLACE_KEY)
        return keys

    @staticmethod
    def _paying_guest_count(guests: List[Guest]) -> int:
        return sum(1 for g in guests
                   if not (not g.is_adult and g.age is not None and g.age <= 6))

    # -- public API ---------------------------------------------------------

    def quote(self, room_type, checkin, checkout, guests: List[Guest]) -> Quote:
        room = self.resolve_room_type(room_type)
        ci, co = _as_date(checkin), _as_date(checkout)
        n = nights_between(ci, co)
        if not guests:
            raise ValueError("At least one guest is required")

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

    def price(self, room_type, checkin, checkout, guests: List[Guest]) -> int:
        """Convenience wrapper returning only the total price in UAH."""
        return self.quote(room_type, checkin, checkout, guests).total

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
