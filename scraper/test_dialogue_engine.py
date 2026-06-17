"""
test_dialogue_engine.py — the deterministic reply builder (the heart of the fix).

Covers: slot parsing, the rigid quote format, multi-room totals, the planner's
routing, off-season handling, and AVAILABILITY GATING (sold out -> Polite Close,
never a price). Pure stdlib + pytest.
"""
import pytest

import dialogue_engine as de
import templates


# --- slot parsing ----------------------------------------------------------

def test_parse_plain_json():
    s = de.parse_slots('{"topic":"faq","rooms":[],"faq_template":"POOL"}')
    assert s["topic"] == "faq" and s["faq_template"] == "POOL"


def test_parse_fenced_json():
    s = de.parse_slots('```json\n{"topic":"greeting","rooms":[]}\n```')
    assert s["topic"] == "greeting"


def test_parse_garbage_falls_back_to_greeting():
    s = de.parse_slots("the model rambled with no json")
    assert s["topic"] == "greeting" and s["rooms"] == []


# --- formatting (rigid quote format, directive 3) --------------------------

def test_quote_line_exact_format():
    line = de.quote_line("Стандарт +", 2, [], "2026-06-28", "2026-06-29", 1, 2400)
    assert line == ("Вартість номеру типу Стандарт +, для 2 дорослих, на 1 ніч "
                    "(28 - 29 червня), буде вартувати - 2400 грн")


def test_nights_phrase_ukrainian_plural():
    assert de.nights_phrase(1) == "1 ніч"
    assert de.nights_phrase(2) == "2 ночі"
    assert de.nights_phrase(5) == "5 ночей"


def test_dates_phrase_cross_month():
    assert de.dates_phrase("2026-06-30", "2026-07-02") == "30 червня - 2 липня"


def test_guests_phrase_with_child():
    assert de.guests_phrase(2, [8]) == "2 дорослих та 1 дитини (8 р.)"


# --- planning --------------------------------------------------------------

def test_plan_all_missing_asks():
    out = de.plan({"topic": "greeting", "rooms": []})
    assert out == {"action": "reply", "reply": templates.QUESTION_ALL_MISSING}


def test_plan_missing_guests():
    out = de.plan({"rooms": [{"room_type": None, "checkin": "2026-07-06",
                              "checkout": "2026-07-08", "adults": 0, "children_ages": []}]})
    assert out["reply"] == templates.QUESTION_MISSING_GUESTS


def test_plan_general_price_picks_month():
    out = de.plan({"rooms": [{"room_type": None, "checkin": "2026-07-06",
                              "checkout": "2026-07-08", "adults": 2, "children_ages": []}]})
    assert out["reply"] == templates.PRICE_JULY


def test_has_off_season_dates():
    assert de.has_off_season_dates({"rooms": [{"checkin": "2026-05-11", "checkout": "2026-05-13"}]}) is True
    assert de.has_off_season_dates({"rooms": [{"checkin": "2026-07-05", "checkout": "2026-07-07"}]}) is False
    assert de.has_off_season_dates({"rooms": []}) is False


def test_plan_off_season():
    out = de.plan({"rooms": [{"room_type": "Стандарт", "checkin": "2026-09-13",
                              "checkout": "2026-09-14", "adults": 2, "children_ages": []}]})
    assert out["reply"] == templates.OFF_SEASON


def test_plan_month_only_off_season():
    # "ціни на жовтень" -> only a month, no exact days -> still off-season.
    out = de.plan({"rooms": [{"room_type": None, "checkin": "2026-10-01",
                              "checkout": None, "adults": 0, "children_ages": []}]})
    assert out["reply"] == templates.OFF_SEASON


def test_plan_month_only_general_price():
    # "ціни на серпень на двох" -> month + guests, no exact days -> monthly price.
    out = de.plan({"rooms": [{"room_type": None, "checkin": "2026-08-01",
                              "checkout": None, "adults": 2, "children_ages": []}]})
    assert out["reply"] == templates.PRICE_AUGUST


def test_plan_complete_requests_quote():
    out = de.plan({"rooms": [{"room_type": "Стандарт", "checkin": "2026-07-06",
                              "checkout": "2026-07-08", "adults": 2, "children_ages": []}]})
    assert out["action"] == "quote" and len(out["rooms"]) == 1


def test_plan_guests_known_does_not_fall_back_to_all_missing():
    # Fix 4: adults given (no children) -> ask only for dates, never QUESTION_ALL_MISSING.
    out = de.plan({"rooms": [{"room_type": None, "checkin": None, "checkout": None,
                              "adults": 2, "children_ages": []}]})
    assert out["reply"] != templates.QUESTION_ALL_MISSING
    assert out["reply"] == templates.QUESTION_MISSING_DATES


# --- finalize_quote: pricing is deterministic (the July-5 bug fix) ----------

AVAIL_OPEN = {  # everything free on the relevant July nights
    "Стандарт": {f"2026-07-0{d}": 3 for d in range(1, 9)},
    "Стандарт +": {f"2026-07-0{d}": 3 for d in range(1, 9)},
    "Напівлюкс": {f"2026-07-0{d}": 1 for d in range(1, 9)},
}
AVAIL_JUNE = {"Стандарт +": {"2026-06-28": 2}}


def test_finalize_single_room_case3():
    reply = de.finalize_quote(
        [{"room_type": "Стандарт +", "checkin": "2026-06-28", "checkout": "2026-06-29",
          "adults": 2, "children_ages": []}], AVAIL_JUNE)
    assert "2400 грн" in reply
    assert reply.startswith("Вартість номеру типу Стандарт +, для 2 дорослих, на 1 ніч")


def test_finalize_july5_is_weekday_not_friday():
    # The old LLM called 5 July a Friday (weekend 2500). Python knows it's Sunday.
    reply = de.finalize_quote(
        [{"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
          "adults": 2, "children_ages": []}], AVAIL_OPEN)
    assert "4400 грн" in reply  # 2200 * 2 будні nights, NOT 2500+2200


def test_finalize_multi_room_with_total():
    rooms = [
        {"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
         "adults": 2, "children_ages": []},
        {"room_type": "Напівлюкс", "checkin": "2026-07-05", "checkout": "2026-07-07",
         "adults": 2, "children_ages": [8]},
    ]
    reply = de.finalize_quote(rooms, AVAIL_OPEN)
    assert "4400 грн" in reply and "6000 грн" in reply
    assert "Загальна вартість: 10400 грн" in reply


# --- AVAILABILITY GATING (directive 2) -------------------------------------

def test_finalize_full_sold_out_offers_nearest_dates():
    # Case 5: only room is fully booked on a night -> offer nearest dates (NOT close).
    avail = {"Стандарт": {"2026-07-05": 3, "2026-07-06": 0}}
    reply = de.finalize_quote(
        [{"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
          "adults": 2, "children_ages": []}], avail)
    assert reply == templates.SOLD_OUT_NEAREST
    assert reply != templates.POLITE_CLOSE


def test_finalize_partial_overbooking_offers_other_rooms():
    # Case 4: chosen room is full but another category is free -> ROOM_BOOKED.
    avail = {"Стандарт": {"2026-07-05": 0, "2026-07-06": 0},
             "Напівлюкс": {"2026-07-05": 2, "2026-07-06": 2}}
    reply = de.finalize_quote(
        [{"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
          "adults": 2, "children_ages": []}], avail)
    assert "Напівлюкс" in reply and "заброньовані" in reply
    assert reply != templates.SOLD_OUT_NEAREST


def test_finalize_unknown_room_in_availability_still_quotes():
    # Room not present in scraped data -> 'unknown' -> we proceed (lenient), not block.
    reply = de.finalize_quote(
        [{"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-07",
          "adults": 2, "children_ages": []}], {})
    assert "2200 грн" in reply


def test_finalize_out_of_window_dates_quote_not_blocked():
    # Дати поза вікном "Шахівниці" (тільки липень у даних) -> unknown -> все одно ціна.
    avail = {"Стандарт": {"2026-07-05": 3}}
    reply = de.finalize_quote(
        [{"room_type": "Стандарт", "checkin": "2026-08-10", "checkout": "2026-08-11",
          "adults": 2, "children_ages": []}], avail)
    assert "грн" in reply and reply != templates.POLITE_CLOSE
