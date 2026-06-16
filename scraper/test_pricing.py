"""
test_pricing.py — Deterministic unit tests for pricing_engine.py.

Pure stdlib + pytest: runs anywhere, no FastAPI / google-genai / Playwright.
These lock down the hotel's money math (project_spec.md §5, skills.md §1),
including the two priced User Cases (Case 3 and Case 7).
"""
import datetime as dt

import pytest

from pricing_engine import (
    PricingEngine,
    Guest,
    make_guests,
    nights_between,
    is_weekend_night,
    child_place_key,
)


@pytest.fixture(scope="module")
def engine():
    return PricingEngine()  # loads scraper/pricing.json


# --- nights & calendar ------------------------------------------------------

def test_nights_basic():
    assert nights_between("2026-06-28", "2026-06-29") == 1   # Case 3: 1 night
    assert nights_between("2026-07-06", "2026-07-08") == 2   # Case 7: 2 nights


def test_checkout_after_checkin_required():
    with pytest.raises(ValueError):
        nights_between("2026-07-08", "2026-07-06")
    with pytest.raises(ValueError):
        nights_between("2026-07-06", "2026-07-06")


@pytest.mark.parametrize("date_str,expected_weekend", [
    ("2026-06-28", False),  # Sunday  -> будні
    ("2026-07-06", False),  # Monday  -> будні
    ("2026-07-07", False),  # Tuesday -> будні
    ("2026-07-02", False),  # Thursday-> будні
    ("2026-07-03", True),   # Friday  -> вихідні
    ("2026-07-04", True),   # Saturday-> вихідні
])
def test_weekend_classification(date_str, expected_weekend):
    d = dt.date.fromisoformat(date_str)
    assert is_weekend_night(d) is expected_weekend


# --- child / extra-place tiers ---------------------------------------------

@pytest.mark.parametrize("age,expected", [
    (0, None), (3, None), (6, None),           # 0-6 free
    (7, "дитяче_місце"), (10, "дитяче_місце"), (12, "дитяче_місце"),
    (13, "додаткове_місце"), (15, "додаткове_місце"),
])
def test_child_place_key(age, expected):
    assert child_place_key(age) == expected


# --- the two priced User Cases ---------------------------------------------

def test_case3_standard_plus_2_adults_1_weekday_night(engine):
    # Стандарт +, 28-29 June 2026 (Sun night, будні), 2 adults, no extras.
    q = engine.quote("Стандарт +", "2026-06-28", "2026-06-29", make_guests(adults=2))
    assert q.total == 2400
    assert q.nights == 1 and q.weekday_nights == 1 and q.weekend_nights == 0


def test_case7_standard_2_adults_child8_2_weekday_nights(engine):
    # Стандарт, 6-8 July 2026 (Mon+Tue, будні), 2 adults + child 8 -> дитяче_місце.
    q = engine.quote("Стандарт", "2026-07-06", "2026-07-08",
                     make_guests(adults=2, children_ages=[8]))
    assert q.total == 5000  # (2200 + 300) * 2
    assert q.nights == 2 and q.weekday_nights == 2


# --- tiered surcharges in isolation ----------------------------------------

def test_child_under_6_is_free(engine):
    base = engine.price("Стандарт", "2026-07-06", "2026-07-07", make_guests(adults=2))
    with_baby = engine.price("Стандарт", "2026-07-06", "2026-07-07",
                             make_guests(adults=2, children_ages=[4]))
    assert with_baby == base == 2200


def test_child_7_12_adds_child_place(engine):
    # 2 adults + child 10, 1 будні night July -> 2200 + 300
    assert engine.price("Стандарт", "2026-07-06", "2026-07-07",
                        make_guests(adults=2, children_ages=[10])) == 2500


def test_two_children_7_12_add_two_child_places(engine):
    assert engine.price("Стандарт", "2026-07-06", "2026-07-07",
                        make_guests(adults=2, children_ages=[8, 10])) == 2800  # 2200 + 300 + 300


def test_child_over_12_adds_extra_place(engine):
    # child 14 is charged додаткове_місце (500 in July будні Standard)
    assert engine.price("Стандарт", "2026-07-06", "2026-07-07",
                        make_guests(adults=2, children_ages=[14])) == 2700


def test_third_adult_adds_extra_place(engine):
    assert engine.price("Стандарт", "2026-07-06", "2026-07-07",
                        make_guests(adults=3)) == 2700  # 2200 + 500


def test_base_capacity_fills_with_adults_first(engine):
    # 2 adults + child 8: adults fill capacity, child 8 is the extra -> дитяче (300)
    assert engine.price("Стандарт", "2026-07-06", "2026-07-07",
                        make_guests(adults=2, children_ages=[8])) == 2500


# --- weekday/weekend rate selection ----------------------------------------

def test_weekend_night_uses_weekend_rate(engine):
    # Night of Fri 3 July -> вихідні вартість_кімнати = 2500 for Standard
    assert engine.price("Стандарт", "2026-07-03", "2026-07-04", make_guests(adults=2)) == 2500


def test_mixed_weekday_and_weekend_stay(engine):
    # Thu 2 July (будні 2200) + Fri 3 July (вихідні 2500) = 4700
    assert engine.price("Стандарт", "2026-07-02", "2026-07-04", make_guests(adults=2)) == 4700


# --- single occupancy -------------------------------------------------------

def test_single_occupancy_rate(engine):
    # 1 adult, Standard, 1 будні July night -> одномісне_поселення = 1800
    assert engine.price("Стандарт", "2026-07-06", "2026-07-07", make_guests(adults=1)) == 1800


# --- room-type resolution & errors -----------------------------------------

def test_resolve_room_type_normalises_plus():
    e = PricingEngine()
    assert e.resolve_room_type("Стандарт+") == "Стандарт +"
    assert e.resolve_room_type("стандарт +") == "Стандарт +"


def test_unknown_room_raises(engine):
    with pytest.raises(KeyError):
        engine.price("Президентський люкс", "2026-07-06", "2026-07-07", make_guests(adults=2))


def test_month_without_price_table_raises(engine):
    # Hotel only prices summer (June/July/August).
    with pytest.raises(KeyError):
        engine.price("Стандарт", "2026-12-20", "2026-12-21", make_guests(adults=2))


def test_napivlyuks_capacity_extra_adults(engine):
    # Напівлюкс has no одномісне rate (null) and base capacity still 2.
    # 3 adults July будні: 2700 + 500 (додаткове) = 3200
    assert engine.price("Напівлюкс", "2026-07-06", "2026-07-07", make_guests(adults=3)) == 3200


# --- Case 8: recalculation on changed dates / nights -----------------------

def test_case8_recalc_more_nights(engine):
    # "А на 2 ночі?" — Стандарт +, 28-30 June (Sun+Mon, both будні), 2 adults.
    # Recalculation must scale from 1 night (2400) to 2 nights = 4800.
    q = engine.quote("Стандарт +", "2026-06-28", "2026-06-30", make_guests(adults=2))
    assert q.total == 4800 and q.nights == 2 and q.weekend_nights == 0


def test_case8_recalc_changed_dates_mixed_tariff(engine):
    # "А якщо 10-15 серпня?" — Стандарт, 5 nights, Mon-Thu будні + Fri вихідні.
    # 4 * 2400 + 1 * 2700 = 12300.
    q = engine.quote("Стандарт", "2026-08-10", "2026-08-15", make_guests(adults=2))
    assert q.total == 12300
    assert q.nights == 5 and q.weekday_nights == 4 and q.weekend_nights == 1


# --- Case 10: multi-room booking (up to 8 rooms) ---------------------------

def test_multi_room_sums_each_room(engine):
    # A family wants a Standard + a Standard+ for 6-8 July (2 будні nights), 2 adults each.
    mq = engine.quote_multiple([
        {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-08",
         "guests": make_guests(adults=2)},
        {"room_type": "Стандарт +", "checkin": "2026-07-06", "checkout": "2026-07-08",
         "guests": make_guests(adults=2)},
    ])
    assert [q.total for q in mq.rooms] == [4400, 5200]   # 2200*2, 2600*2
    assert mq.total == 9600


def test_multi_room_accepts_tuples(engine):
    mq = engine.quote_multiple([
        ("Стандарт", "2026-07-06", "2026-07-07", make_guests(adults=2)),
        ("Стандарт", "2026-07-06", "2026-07-07", make_guests(adults=3)),
    ])
    assert mq.total == 2200 + 2700  # base + (base+додаткове)


def test_multi_room_rejects_over_8(engine):
    one = ("Стандарт", "2026-07-06", "2026-07-07", make_guests(adults=2))
    with pytest.raises(ValueError):
        engine.quote_multiple([one] * 9)


# --- Case 11: off-season (Sept–May) has no pricing -------------------------

def test_off_season_raises_offseason_error(engine):
    from pricing_engine import OffSeasonError
    with pytest.raises(OffSeasonError):
        engine.price("Стандарт", "2026-09-13", "2026-09-14", make_guests(adults=2))


def test_is_priced_month_and_stay():
    from pricing_engine import is_priced_month, stay_is_priced
    assert is_priced_month("2026-07-06") is True
    assert is_priced_month("2026-09-01") is False
    assert stay_is_priced("2026-07-06", "2026-07-08") is True
    # checkin Aug 31 -> night of Sep 1 is off-season:
    assert stay_is_priced("2026-08-31", "2026-09-02") is False
