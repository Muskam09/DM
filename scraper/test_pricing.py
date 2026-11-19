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
    fits_room,
    OverCapacityError,
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
    (0, None), (3, None), (5, None),                                   # 0-5 free
    (6, "дитяче_місце"), (10, "дитяче_місце"), (11, "дитяче_місце"),    # 6-11 child place
    (12, "додаткове_місце"), (13, "додаткове_місце"), (15, "додаткове_місце"),  # 12+ full place
])
def test_child_place_key(age, expected):
    # Owner rule (2026-06-23): under 6 free; 6-11 дитяче; 12+ додаткове.
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


# --- owner child-tier boundaries (2026-06-23): under6 free / 6-11 child / 12+ full ---

def test_child_age_5_is_free(engine):
    assert engine.price("Стандарт", "2026-07-06", "2026-07-07",
                        make_guests(adults=2, children_ages=[5])) == 2200


def test_child_age_6_now_charged_child_place(engine):
    # Age 6 is now дитяче_місце (was FREE under the old 0-6 rule).
    assert engine.price("Стандарт", "2026-07-06", "2026-07-07",
                        make_guests(adults=2, children_ages=[6])) == 2500   # 2200 + 300


def test_child_age_11_child_place(engine):
    assert engine.price("Стандарт", "2026-07-06", "2026-07-07",
                        make_guests(adults=2, children_ages=[11])) == 2500


def test_child_age_12_now_extra_place(engine):
    # Age 12 is now додаткове_місце (was дитяче under the old <=12 rule).
    assert engine.price("Стандарт", "2026-07-06", "2026-07-07",
                        make_guests(adults=2, children_ages=[12])) == 2700   # 2200 + 500


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


def test_single_occupancy_standard_plus(engine):
    # Solo ALWAYS uses одномісне_поселення (June будні Стандарт+ = 2000), not the 2400 room rate.
    assert engine.price("Стандарт +", "2026-06-28", "2026-06-29", make_guests(adults=1)) == 2000


# --- hard physical occupancy gate (owner rule FINALISED 2026-07-11) --------
# Стандарт/Стандарт+: a 12+ child counts as an ADULT; MAX 3 adult-equivalents AND MAX 4 people.
# So 3 adults + 1 child<12 fits (sofa); "2 adults + 14yo + 9yo" fits (= 3 adult-eq + 1 child<12).

@pytest.mark.parametrize("room,adults,kids,fits", [
    ("Стандарт", 3, [], True),             # 3 adults OK
    ("Стандарт", 4, [], False),            # 4 adults -> over
    ("Стандарт", 2, [11], True),           # 3 people OK
    ("Стандарт", 3, [8], True),            # 3 adults + 1 child <12 on the sofa -> OK (owner 07-11)
    ("Стандарт", 2, [14, 9], True),        # 2 ad + 14yo(adult-eq) + 9yo = 3 adult-eq + 1 child -> OK
    ("Стандарт", 3, [12], False),          # 3 adults + a 12+ child = 4 adult-eq -> over
    ("Стандарт", 3, [14], False),
    ("Стандарт", 2, [8, 12], True),        # 2 ad + 12yo(adult-eq) + 8yo = 3 adult-eq + 1 child -> OK
    ("Стандарт", 2, [12, 14], False),      # 2 ad + two 12+ = 4 adult-eq -> over
    ("Стандарт", 2, [8, 10], True),        # 2 adults + 2 children under 12 -> OK
    ("Стандарт", 3, [8, 10], False),       # 5 total -> over
    ("Стандарт", 2, [8, 10, 5], False),    # 5 total -> over
    ("Стандарт +", 4, [], False),
    ("Стандарт +", 3, [6], True),          # 3 adults + 1 child <12 -> OK
    ("Стандарт +", 2, [6, 10], True),      # 2 adults + 2 children under 12 -> OK
    ("Напівлюкс", 5, [], True),            # Напівлюкс holds 5
    ("Напівлюкс", 4, [10], True),          # 5 total OK
    ("Напівлюкс", 5, [10], False),         # 6 total -> over
])
def test_fits_room(room, adults, kids, fits):
    assert fits_room(room, adults, kids) is fits


def test_quote_over_capacity_raises(engine):
    with pytest.raises(OverCapacityError):
        engine.quote("Стандарт", "2026-07-06", "2026-07-07", make_guests(adults=4))


def test_price_over_capacity_returns_none(engine):
    assert engine.price("Стандарт", "2026-07-06", "2026-07-07", make_guests(adults=4)) is None
    assert engine.price("Стандарт +", "2026-07-06", "2026-07-07",
                        make_guests(adults=3, children_ages=[12])) is None


def test_napivlux_holds_five_adults(engine):
    # 5 adults fit Напівлюкс: base 2 + 3 extra adults (додаткове 500) July будні = 2700 + 1500
    assert engine.price("Напівлюкс", "2026-07-06", "2026-07-07", make_guests(adults=5)) == 4200


# --- room-type resolution & errors -----------------------------------------

def test_resolve_room_type_normalises_plus():
    e = PricingEngine()
    assert e.resolve_room_type("Стандарт+") == "Стандарт +"
    assert e.resolve_room_type("стандарт +") == "Стандарт +"


def test_unknown_room_raises(engine):
    with pytest.raises(KeyError):
        engine.price("Президентський люкс", "2026-07-06", "2026-07-07", make_guests(adults=2))


def test_month_without_price_table_raises(engine):
    # Only March–May are off-season now (December + Jan + Feb got winter prices
    # in the 2026-11 owner update, see pricing.json). A March stay must still
    # raise OffSeasonError / KeyError.
    with pytest.raises(KeyError):
        engine.price("Стандарт", "2026-03-20", "2026-03-21", make_guests(adults=2))


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


# --- Case 11: off-season (March–May) has no pricing ------------------------
# Owner update 2026-11: winter (Dec+Jan+Feb) + full autumn + summer are priced.
# Only March–May remain off-season.

def test_off_season_raises_offseason_error(engine):
    from pricing_engine import OffSeasonError
    # Квітень is squarely off-season.
    with pytest.raises(OffSeasonError):
        engine.price("Стандарт", "2026-04-13", "2026-04-14", make_guests(adults=2))


def test_is_priced_month_and_stay():
    from pricing_engine import is_priced_month, stay_is_priced
    assert is_priced_month("2026-07-06") is True
    assert is_priced_month("2026-09-01") is True   # autumn is now priced
    assert is_priced_month("2026-12-15") is True   # winter is now priced
    assert is_priced_month("2026-01-15") is True
    assert is_priced_month("2026-02-15") is True
    assert is_priced_month("2026-03-15") is False  # shoulder month: still off
    assert is_priced_month("2026-05-15") is False
    assert stay_is_priced("2026-07-06", "2026-07-08") is True
    # Night of Nov 30 (last night paid) is priced; Dec 1 is also priced now.
    assert stay_is_priced("2026-11-30", "2026-12-02") is True
    # But a Feb→March stay crosses into off-season on the night of March 1:
    assert stay_is_priced("2026-02-28", "2026-03-02") is False


# --- meals (харчування) — exact food math (owner 2026-07-10) ---------------

def test_meal_prices_new_structure():
    import json, os
    from pricing_engine import meal_prices
    data = json.load(open(os.path.join(os.path.dirname(__file__), "pricing.json"), encoding="utf-8"))
    p = meal_prices(data, "Серпень")
    assert p["3-разове"] == 1100 and p["2-разове"] == 850
    assert p["сніданок"] == 350 and p["обід"] == 450 and p["вечеря"] == 450


def test_meal_cost_owner_example():
    # Owner example: 3-разове for 4 people for 2 days + ONLY breakfast on the last day, August:
    #   (1100*4*2) + (350*4*1) = 8800 + 1400 = 10200 грн
    import json, os
    from pricing_engine import meal_cost
    data = json.load(open(os.path.join(os.path.dirname(__file__), "pricing.json"), encoding="utf-8"))
    q = meal_cost(data, "Серпень", 4, three_meals_days=2, breakfast_days=1)
    assert q.total == 10200
    assert q.persons == 4
    assert any("8800" in ln for ln in q.lines) and any("1400" in ln for ln in q.lines)


def test_meal_cost_zero_when_empty():
    import json, os
    from pricing_engine import meal_cost
    data = json.load(open(os.path.join(os.path.dirname(__file__), "pricing.json"), encoding="utf-8"))
    q = meal_cost(data, "Липень", 3)
    assert q.total == 0 and q.lines == []


# ===========================================================================
# Winter season (owner update 2026-11)
# ===========================================================================
# Three December sub-periods + two January sub-periods + a flat February key.
# The sub-period is picked PER NIGHT by `season_key_for_night` using the day
# you sleep (the check-in day of that night), not the next morning.
#
#   22→23 Dec  = "Грудень 1-22"  (you sleep on the 22nd)
#   23→24 Dec  = "Грудень 23-28"
#   28→29 Dec  = "Грудень 23-28" (you sleep on the 28th — owner rule)
#   29→30 Dec  = "Грудень 29-31"
#   31 Dec→1 Jan = "Грудень 29-31"
#   1 → 2 Jan  = "Січень 1-3"
#   3 → 4 Jan  = "Січень 1-3"
#   4 → 5 Jan  = "Січень 4-31"

from pricing_engine import season_key_for_night, OffSeasonError
import datetime as _dt


@pytest.mark.parametrize("iso,expected", [
    # December sub-periods
    ("2026-12-01", "Грудень 1-22"),
    ("2026-12-15", "Грудень 1-22"),
    ("2026-12-22", "Грудень 1-22"),    # night 22→23 goes HERE per owner rule
    ("2026-12-23", "Грудень 23-28"),   # night 23→24 goes HERE
    ("2026-12-27", "Грудень 23-28"),
    ("2026-12-28", "Грудень 23-28"),   # night 28→29 goes HERE per owner rule
    ("2026-12-29", "Грудень 29-31"),   # New Year peak begins
    ("2026-12-31", "Грудень 29-31"),   # night 31 Dec→1 Jan still peak
    # January sub-periods
    ("2027-01-01", "Січень 1-3"),
    ("2027-01-03", "Січень 1-3"),      # night 3→4 Jan still peak (owner rule)
    ("2027-01-04", "Січень 4-31"),     # night 4→5 Jan — low winter rate
    ("2027-01-15", "Січень 4-31"),
    ("2027-01-31", "Січень 4-31"),
    # February — single flat key
    ("2027-02-01", "Лютий"),
    ("2027-02-15", "Лютий"),
    ("2027-02-28", "Лютий"),
    # Summer / autumn — same name
    ("2026-07-10", "Липень"),
    ("2026-11-15", "Листопад"),
])
def test_season_key_for_night(iso, expected):
    assert season_key_for_night(_dt.date.fromisoformat(iso)) == expected


@pytest.mark.parametrize("iso", ["2027-03-10", "2027-04-01", "2027-05-31"])
def test_season_key_for_night_off_season_raises(iso):
    with pytest.raises(OffSeasonError):
        season_key_for_night(_dt.date.fromisoformat(iso))


# --- "Грудень 1-22" — owner rule: night 22→23 uses this key ---------------

def test_december_1_22_single_night_standard(engine):
    # 2026-12-15 (Tuesday, будні), Стандарт: "Грудень 1-22" будні = 2000.
    q = engine.quote("Стандарт", "2026-12-15", "2026-12-16", make_guests(adults=2))
    assert q.total == 2000


def test_december_22_to_23_still_uses_1_22_rate(engine):
    # ТЗ: "ніч з 22-23 рахується по прайсам Грудень 1-22". 2026-12-22 is Tuesday (будні),
    # so the night of the 22nd goes for "Грудень 1-22" будні = 2000, NOT 2200.
    q = engine.quote("Стандарт", "2026-12-22", "2026-12-23", make_guests(adults=2))
    assert q.total == 2000


def test_december_1_22_weekend_night_standard(engine):
    # 2026-12-18 is Friday (вихідні), Стандарт: "Грудень 1-22" вихідні = 2300.
    q = engine.quote("Стандарт", "2026-12-18", "2026-12-19", make_guests(adults=2))
    assert q.total == 2300


# --- "Грудень 23-28" — owner rule: night 28→29 uses this key --------------

def test_december_23_to_24_uses_23_28_rate(engine):
    # 2026-12-23 is Wednesday (будні). "Грудень 23-28" будні = 2200 for Standard.
    q = engine.quote("Стандарт", "2026-12-23", "2026-12-24", make_guests(adults=2))
    assert q.total == 2200


def test_december_28_to_29_uses_23_28_rate(engine):
    # ТЗ: "ніч 28-29 включено по тому прайсу [Грудень 23-28]".
    # 2026-12-28 is Monday (будні) → 2200, NOT 2700.
    q = engine.quote("Стандарт", "2026-12-28", "2026-12-29", make_guests(adults=2))
    assert q.total == 2200


# --- "Грудень 29-31" / "Січень 1-3" — New Year peak -----------------------

def test_december_29_30_peak_rate(engine):
    # "Грудень 29-31" is a flat 2700 for Standard (будні == вихідні).
    # 2026-12-29 is Tuesday — would be будні, but the New Year key is flat.
    q = engine.quote("Стандарт", "2026-12-29", "2026-12-30", make_guests(adults=2))
    assert q.total == 2700


def test_new_year_night_31_to_1_uses_december_peak(engine):
    # The night of 31 Dec → 1 Jan: you sleep on the 31st, so it's "Грудень 29-31" (2700).
    q = engine.quote("Стандарт", "2026-12-31", "2027-01-01", make_guests(adults=2))
    assert q.total == 2700


def test_january_1_to_2_still_peak(engine):
    # Night 1→2 Jan: sleep on the 1st, "Січень 1-3" = 2700 flat.
    q = engine.quote("Стандарт", "2027-01-01", "2027-01-02", make_guests(adults=2))
    assert q.total == 2700


def test_january_3_to_4_still_peak(engine):
    # Owner rule: "Січень 1-3" covers through night 3→4.
    q = engine.quote("Стандарт", "2027-01-03", "2027-01-04", make_guests(adults=2))
    assert q.total == 2700


def test_january_4_to_5_drops_to_low_winter(engine):
    # Night 4→5 Jan: "Січень 4-31" будні (Mon) = 2200.
    q = engine.quote("Стандарт", "2027-01-04", "2027-01-05", make_guests(adults=2))
    assert q.total == 2200


# --- Лютий — single flat key ----------------------------------------------

def test_february_weekday_standard(engine):
    # 2027-02-10 is Wednesday (будні), "Лютий" будні = 2200 for Standard.
    q = engine.quote("Стандарт", "2027-02-10", "2027-02-11", make_guests(adults=2))
    assert q.total == 2200


def test_february_weekend_standard(engine):
    # 2027-02-12 is Friday (вихідні), "Лютий" вихідні = 2500 for Standard.
    q = engine.quote("Стандарт", "2027-02-12", "2027-02-13", make_guests(adults=2))
    assert q.total == 2500


# --- period boundary crossings --------------------------------------------

def test_stay_straddles_1_22_and_23_28_boundary(engine):
    # 21-24 Dec 2026, Стандарт, 2 adults. Nights:
    #   21 Dec (Mon, будні, "Грудень 1-22")   = 2000
    #   22 Dec (Tue, будні, "Грудень 1-22")   = 2000   (owner rule — night 22→23 stays in 1-22)
    #   23 Dec (Wed, будні, "Грудень 23-28")  = 2200
    # Total = 6200
    q = engine.quote("Стандарт", "2026-12-21", "2026-12-24", make_guests(adults=2))
    assert q.total == 6200
    assert q.nights == 3


def test_stay_straddles_23_28_and_29_31_boundary(engine):
    # 27-30 Dec 2026, Стандарт, 2 adults. Nights:
    #   27 Dec (Sun, будні, "Грудень 23-28")  = 2200
    #   28 Dec (Mon, будні, "Грудень 23-28")  = 2200   (owner rule — night 28→29 stays in 23-28)
    #   29 Dec (Tue, flat peak, "Грудень 29-31") = 2700
    # Total = 7100
    q = engine.quote("Стандарт", "2026-12-27", "2026-12-30", make_guests(adults=2))
    assert q.total == 7100


def test_stay_crosses_dec_to_jan_new_year(engine):
    # 30 Dec 2026 → 2 Jan 2027, Стандарт, 2 adults. Three peak nights:
    #   30 Dec = 2700 (Грудень 29-31)
    #   31 Dec = 2700 (Грудень 29-31, night 31→1 Jan)
    #   1  Jan = 2700 (Січень 1-3, flat)
    q = engine.quote("Стандарт", "2026-12-30", "2027-01-02", make_guests(adults=2))
    assert q.total == 8100
    assert q.nights == 3


def test_stay_crosses_jan_peak_to_low_winter(engine):
    # 2-5 Jan 2027, Стандарт, 2 adults. Nights:
    #   2 Jan (Sat, "Січень 1-3", flat)   = 2700
    #   3 Jan (Sun, "Січень 1-3", flat)   = 2700   (owner rule — night 3→4 stays in 1-3)
    #   4 Jan (Mon, "Січень 4-31", будні) = 2200
    q = engine.quote("Стандарт", "2027-01-02", "2027-01-05", make_guests(adults=2))
    assert q.total == 7600


# --- other categories: verify the key naming was unified (NO "Грудень 22-28") ---

def test_napivlux_december_1_22_weekday(engine):
    # "Напівлюкс" has no одномісне (null), so 2 adults = base. "Грудень 1-22" будні = 2500.
    q = engine.quote("Напівлюкс", "2026-12-15", "2026-12-16", make_guests(adults=2))
    assert q.total == 2500


def test_napivlux_december_23_28_weekday(engine):
    # Previously the JSON had a buggy "Грудень 22-28" key for Напівлюкс — the rename to
    # "Грудень 23-28" must still resolve. 2026-12-23 is Wednesday (будні) = 2700.
    q = engine.quote("Напівлюкс", "2026-12-23", "2026-12-24", make_guests(adults=2))
    assert q.total == 2700


def test_standard_plus_december_29_31_peak(engine):
    # Стандарт +, "Грудень 29-31" flat = 3000.
    q = engine.quote("Стандарт +", "2026-12-30", "2026-12-31", make_guests(adults=2))
    assert q.total == 3000


# ===========================================================================
# Rolling-year logic (bot_logic.resolve_booking_year)
# ===========================================================================
# A bare month name ("на червень") must resolve to THIS year if the month is still
# ahead or ongoing, and to NEXT year once the month has already passed. Prevents
# the January-2027 client from being quoted stale 2026 prices (= wrong calendar
# days-of-week, hence wrong weekday/weekend split).

from bot_logic import resolve_booking_year, current_booking_year, parse_date_request


def test_resolve_booking_year_future_month_this_year():
    # Today = 2026-06-01. "На серпень" → 2026 (still ahead).
    today = _dt.date(2026, 6, 1)
    assert resolve_booking_year(8, today) == 2026
    assert resolve_booking_year(6, today) == 2026   # same month = this year


def test_resolve_booking_year_past_month_rolls_forward():
    # Today = 2026-11-30. "На лютий" → 2027 (Feb already passed this year).
    today = _dt.date(2026, 11, 30)
    assert resolve_booking_year(2, today) == 2027
    assert resolve_booking_year(1, today) == 2027


def test_resolve_booking_year_default_today_uses_system_clock():
    # When `today` is omitted, `date.today()` is used. In the test suite conftest.py
    # freezes it to 2026-06-16, so August must resolve to 2026, not the wall-clock year.
    assert resolve_booking_year(8) == 2026
    assert current_booking_year() == 2026


def test_parse_date_request_rolls_forward_past_months():
    # Today pinned to 2027-10-15: a bare "15 лютого" must resolve to 2028, not 2027.
    today = _dt.date(2027, 10, 15)
    out = parse_date_request("заїзд 15 лютого", today=today)
    assert out == {"checkin": "2028-02-15"}


def test_parse_date_request_keeps_current_year_for_future_months():
    # Today pinned to 2027-04-10: "на червень" must stay in 2027.
    today = _dt.date(2027, 4, 10)
    out = parse_date_request("з 5 по 10 червня", today=today)
    assert out == {"checkin": "2027-06-05", "checkout": "2027-06-10"}


def test_parse_date_request_respects_explicit_year_in_dotted():
    # Explicit year in dotted format is honoured (overrides rolling-year logic).
    # Today pinned to 2027-10-15 — rolling would roll "02.26" to the next-year
    # February, but the dotted ".26" is an explicit 2026 and must be kept.
    today = _dt.date(2027, 10, 15)
    out = parse_date_request("10.07.26", today=today)
    assert out == {"checkin": "2026-07-10"}


# ===========================================================================
# Fuzzy-period year resolution (dialogue_engine.fuzzy_period_ranges)
# ===========================================================================

import dialogue_engine as _de


def test_fuzzy_period_rolls_forward_past_month():
    # Today = 2027-10-15 → "у лютому" resolves to 2028-02.
    today = _dt.date(2027, 10, 15)
    assert _de.fuzzy_period_range("у лютому", today=today) == ("2028-02-01", "2028-02-29")


def test_fuzzy_period_stays_in_same_year_for_future_month():
    # Today = 2027-01-15 → "на липень" resolves to 2027-07 (still ahead).
    today = _dt.date(2027, 1, 15)
    assert _de.fuzzy_period_range("на липень", today=today) == ("2027-07-01", "2027-07-31")


def test_fuzzy_period_honours_explicit_yyyy_mm():
    # Even when today has moved past Jan 2026, "2026-01" names an explicit past year —
    # honour it verbatim (do NOT roll forward to 2027). This path matches the extractor
    # output format "YYYY-MM" (e.g. "2026-01" for "у січні").
    today = _dt.date(2027, 10, 15)
    assert _de.fuzzy_period_range("2026-01", today=today) == ("2026-01-01", "2026-01-31")


def test_fuzzy_period_compound_each_segment_picks_own_year():
    # Compound period "лютий або липень" today = 2027-08-01:
    #   Feb has already passed (2027-02) → rolls to 2028-02
    #   July has already passed (2027-07) → rolls to 2028-07
    today = _dt.date(2027, 8, 1)
    ranges = _de.fuzzy_period_ranges("лютий або липень", today=today)
    assert ranges == [("2028-02-01", "2028-02-29"), ("2028-07-01", "2028-07-31")]


# ===========================================================================
# PRICED_MONTHS surface — Jan/Feb/Dec are now priced
# ===========================================================================

def test_priced_months_include_winter():
    from pricing_engine import PRICED_MONTHS
    for m in (1, 2, 6, 7, 8, 9, 10, 11, 12):
        assert m in PRICED_MONTHS, f"month {m} must be priced"
    # Only March–May remain off-season.
    for m in (3, 4, 5):
        assert m not in PRICED_MONTHS, f"month {m} must be off-season"


def test_stay_entirely_in_winter_is_priced():
    from pricing_engine import stay_is_priced
    assert stay_is_priced("2026-12-10", "2026-12-15") is True
    assert stay_is_priced("2027-01-10", "2027-01-15") is True
    assert stay_is_priced("2027-02-10", "2027-02-15") is True


def test_stay_crossing_from_feb_into_march_is_offseason():
    from pricing_engine import stay_is_priced
    # Last paid night on Feb 28 (priced), but the night of March 1 is OFF — so
    # `stay_is_priced` is False for the whole stay.
    assert stay_is_priced("2027-02-28", "2027-03-02") is False
