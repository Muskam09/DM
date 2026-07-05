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


def test_quote_line_child_discount_note():
    # UX (owner 2026-06-24): a 6-11 y.o. -> explicit 50% child-place discount note.
    line = de.quote_line("Стандарт", 2, [8], "2026-07-06", "2026-07-08", 2, 5000)
    assert "враховано знижку 50% на дитяче місце" in line


def test_quote_line_no_child_note_when_not_applicable():
    assert "дитяче місце" not in de.quote_line("Стандарт", 2, [], "2026-07-06", "2026-07-08", 2, 4400)
    assert "дитяче місце" not in de.quote_line("Стандарт", 2, [4], "2026-07-06", "2026-07-08", 2, 4400)   # baby free
    assert "дитяче місце" not in de.quote_line("Стандарт", 2, [12], "2026-07-06", "2026-07-08", 2, 5000)  # 12 -> full place


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


def test_has_off_season_dates():
    assert de.has_off_season_dates({"rooms": [{"checkin": "2026-05-11", "checkout": "2026-05-13"}]}) is True
    assert de.has_off_season_dates({"rooms": [{"checkin": "2026-07-05", "checkout": "2026-07-07"}]}) is False
    assert de.has_off_season_dates({"rooms": []}) is False


def test_plan_off_season():
    out = de.plan({"rooms": [{"room_type": "Стандарт", "checkin": "2026-09-13",
                              "checkout": "2026-09-14", "adults": 2, "children_ages": []}]})
    assert out["reply"] == templates.OFF_SEASON


def test_plan_fuzzy_off_season():
    # "жовтень" (fuzzy) names an unpriced month -> OFF_SEASON.
    out = de.plan({"rooms": [{"room_type": None, "fuzzy_date": "жовтень",
                              "checkin": None, "checkout": None, "adults": 2, "children_ages": []}]})
    assert out["reply"] == templates.OFF_SEASON


def test_plan_fuzzy_no_guests_acknowledges_once():
    # Fuzzy + NO guests -> acknowledge the period, ask for exact dates.
    out = de.plan({"rooms": [{"room_type": None, "fuzzy_date": "початок серпня",
                              "checkin": None, "checkout": None, "adults": 0, "children_ages": []}]})
    assert "початок серпня" in out["reply"] and "точні дати" in out["reply"]


def test_plan_fuzzy_with_guests_and_nights_explores():
    # A3: fuzzy dates + guests + KNOWN nights -> proactive calendar scan (explore).
    out = de.plan({"rooms": [{"room_type": None, "fuzzy_date": "друга половина липня",
                              "nights": 4, "checkin": None, "checkout": None,
                              "adults": 2, "children_ages": []}]})
    assert out["action"] == "explore" and out["spec"]["nights"] == 4


def test_plan_fuzzy_guests_no_nights_now_explores():
    # Fix 1: fuzzy + guests, nights UNKNOWN -> STILL proactively scan (kill the fuzzy
    # loop). Unknown nights propagate as 0 -> propose_windows defaults to 2-night blocks.
    out = de.plan({"rooms": [{"room_type": None, "fuzzy_date": "початок серпня",
                              "nights": None, "checkin": None, "checkout": None,
                              "adults": 4, "children_count": 0, "children_ages": []}]})
    assert out["action"] == "explore"
    assert out["spec"]["fuzzy_date"] == "початок серпня"
    assert out["spec"]["nights"] == 0


def test_plan_exact_dates_no_room_quotes_all():
    out = de.plan({"rooms": [{"room_type": None, "checkin": "2026-07-06",
                              "checkout": "2026-07-08", "adults": 2, "children_ages": []}]})
    assert out["action"] == "quote_all"


def test_plan_exact_dates_with_room_quotes():
    out = de.plan({"rooms": [{"room_type": "Стандарт", "checkin": "2026-07-06",
                              "checkout": "2026-07-08", "adults": 2, "children_ages": []}]})
    assert out["action"] == "quote" and len(out["rooms"]) == 1


def test_plan_exact_dates_no_guests_asks_guests():
    out = de.plan({"rooms": [{"room_type": None, "checkin": "2026-07-06",
                              "checkout": "2026-07-08", "adults": 0, "children_ages": []}]})
    assert out["reply"] == templates.QUESTION_MISSING_GUESTS


def test_plan_guests_known_only_dates_missing_uses_only_dates():
    # A1: guests 100% known, no fuzzy, only dates missing -> QUESTION_ONLY_DATES.
    out = de.plan({"rooms": [{"room_type": None, "checkin": None, "checkout": None,
                              "adults": 4, "children_count": 0, "children_ages": []}]})
    assert out["reply"] == templates.QUESTION_ONLY_DATES


def test_plan_fuzzy_ages_missing_acknowledges_and_asks_age():
    # Bug 3: fuzzy period + guests but a child's age is missing -> acknowledge the period
    # (so the client feels heard) AND ask ONLY the age — never ignore the dates.
    out = de.plan({"rooms": [{"room_type": None, "fuzzy_date": "кінець серпня",
                              "checkin": None, "checkout": None, "nights": None,
                              "adults": 2, "children_count": 2, "children_ages": []}]})
    assert out["action"] == "reply"
    assert out["reply"] == templates.ACKNOWLEDGE_FUZZY_AGE.replace("{fuzzy_date}", "кінець серпня")
    assert "кінець серпня" in out["reply"] and "вік діток" in out["reply"]


def test_plan_child_age_missing_asks_age():
    # #2: 2 adults + 1 child (age unknown) + exact dates -> QUESTION_MISSING_AGE.
    out = de.plan({"rooms": [{"room_type": None, "checkin": "2026-07-06", "checkout": "2026-07-08",
                              "adults": 2, "children_count": 1, "children_ages": []}]})
    assert out["reply"] == templates.QUESTION_MISSING_AGE


def test_find_nearest_window_forward_scan():
    avail = {"Стандарт": {"2026-07-06": 0, "2026-07-07": 0, "2026-07-08": 2, "2026-07-09": 2}}
    win = de.find_nearest_window(avail, "Стандарт", "2026-07-05", nights=2)
    assert win == ("2026-07-08", "2026-07-10")


def test_propose_windows_lists_free_stretches():
    avail = {"Стандарт": {f"2026-07-{d:02d}": 2 for d in range(10, 16)}}  # 6-day free run
    reply = de.propose_windows({"room_type": "Стандарт", "fuzzy_date": "липень"}, avail)
    assert "10 - 15 липня" in reply and "Які дати вам підходять найбільше" in reply


def test_fuzzy_period_range_maps_named_parts():
    assert de.fuzzy_period_range("початок серпня") == ("2026-08-01", "2026-08-10")
    assert de.fuzzy_period_range("друга половина липня") == ("2026-07-16", "2026-07-31")
    assert de.fuzzy_period_range("кінець серпня") == ("2026-08-21", "2026-08-31")
    assert de.fuzzy_period_range("у серпні") == ("2026-08-01", "2026-08-31")
    assert de.fuzzy_period_range("після 6 серпня") == ("2026-08-06", "2026-08-31")
    assert de.fuzzy_period_range("на вихідних") is None      # no month -> unconstrained


def test_propose_windows_constrained_to_named_period():
    # Free blocks early AND late July, but client said "початок липня" -> only early.
    avail = {"Стандарт": {
        **{f"2026-07-0{d}": 2 for d in range(1, 6)},     # 1-5 July free
        **{f"2026-07-2{d}": 2 for d in range(0, 6)},     # 20-25 July free
    }}
    reply = de.propose_windows({"room_type": "Стандарт", "fuzzy_date": "початок липня"}, avail)
    assert "1 - 5 липня" in reply
    assert "20 - 25" not in reply       # outside the named period -> not offered


def test_propose_windows_period_beyond_visible_window_asks_dates():
    # Named period (серпень) is BEYOND the visible calendar (only July scraped). We
    # can't honestly scan it -> ask for exact dates, never propose wrong-month windows.
    avail = {"Стандарт": {f"2026-07-{d:02d}": 2 for d in range(10, 16)}}
    reply = de.propose_windows({"room_type": "Стандарт", "fuzzy_date": "серпень"}, avail)
    assert "серпень" in reply and "точні дати" in reply   # ACKNOWLEDGE_FUZZY
    assert "липня" not in reply                            # never proposes the wrong period


def test_propose_windows_partial_overlap_visible_full_asks_dates():
    # "початок серпня" = Aug 1-10, but the calendar only reaches Aug 3 and those are
    # booked. Don't propose July; ask for exact dates (most of the period is unseeable).
    avail = {"Стандарт": {**{f"2026-07-{d:02d}": 2 for d in range(10, 16)},   # July free
                          "2026-08-01": 0, "2026-08-02": 0, "2026-08-03": 0}}  # window ends Aug 3, booked
    reply = de.propose_windows({"room_type": "Стандарт", "fuzzy_date": "початок серпня"}, avail)
    assert "серпн" in reply and "точні дати" in reply
    assert "липня" not in reply       # never proposes the wrong month


def test_offered_window_covers_explore_and_soldout():
    # Bug 1: the bot must remember the window it offered, for ANY path.
    avail = {"Стандарт": {f"2026-07-{d:02d}": 2 for d in range(20, 28)}}
    assert de.offered_window(
        {"action": "explore", "spec": {"room_type": "Стандарт",
         "fuzzy_date": "друга половина липня", "nights": 3}}, avail) == ("2026-07-20", "2026-07-23")
    # chosen room sold out, nothing else free -> the SOLD_OUT_FOUND_NEAREST window.
    avail2 = {"Стандарт": {"2026-07-05": 0, "2026-07-06": 0,
                           "2026-07-08": 2, "2026-07-09": 2}}
    decision = {"action": "quote", "rooms": [
        {"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
         "adults": 2, "children_ages": []}]}
    assert de.offered_window(decision, avail2) == ("2026-07-08", "2026-07-10")


def test_first_offered_window_honors_nights():
    # Decision 2A helper: the (checkin, checkout) a bare "Так" accepts after windows.
    avail = {"Стандарт": {f"2026-07-{d:02d}": 2 for d in range(20, 28)}}   # 20-27 free
    assert de.first_offered_window(
        {"room_type": "Стандарт", "fuzzy_date": "друга половина липня", "nights": 3},
        avail) == ("2026-07-20", "2026-07-23")          # checkin 20 + 3 nights -> checkout 23
    win2 = de.first_offered_window(
        {"room_type": "Стандарт", "fuzzy_date": "друга половина липня"}, avail)
    assert win2[0] == "2026-07-20"                       # unknown nights -> run end is checkout
    # Period beyond the visible window -> propose_windows asks for dates -> nothing to accept.
    assert de.first_offered_window(
        {"room_type": "Стандарт", "fuzzy_date": "серпень"}, avail) is None


def test_propose_windows_period_busy_offers_nearest():
    # Period overlaps the window but is booked there -> offer the nearest real window.
    avail = {"Стандарт": {"2026-07-10": 0, "2026-07-11": 0,        # early July booked
                          "2026-07-20": 2, "2026-07-21": 2, "2026-07-22": 2}}
    reply = de.propose_windows({"room_type": "Стандарт", "fuzzy_date": "початок липня"}, avail)
    assert "20 - 22 липня" in reply


# --- Fix 3: FAQ mid-booking must not wipe the gathered state -----------------

def test_faq_followup_partial_state_asks_only_missing():
    # guests known, dates missing -> ask ONLY dates (never the all-missing monolith).
    slots = {"rooms": [{"room_type": None, "checkin": None, "checkout": None,
                        "adults": 2, "children_count": 0, "children_ages": []}]}
    out = de.faq_followup(slots)
    assert templates.QUESTION_ONLY_DATES in out
    assert templates.QUESTION_ALL_MISSING not in out


def test_faq_followup_complete_booking_offers_to_continue():
    slots = {"rooms": [{"room_type": "Стандарт", "checkin": "2026-07-06",
                        "checkout": "2026-07-08", "adults": 2, "children_ages": []}]}
    assert de.faq_followup(slots) == templates.FAQ_CONTINUE_NUDGE


def test_faq_followup_no_context_uses_date_nudge():
    assert de.faq_followup({"rooms": []}) == templates.FAQ_DATE_NUDGE


def test_finalize_quote_all_lists_available_types():
    avail = {"Стандарт": {"2026-07-06": 3, "2026-07-07": 3},
             "Стандарт +": {"2026-07-06": 3, "2026-07-07": 3},
             "Напівлюкс": {"2026-07-06": 0, "2026-07-07": 0}}  # Напівлюкс sold out
    reply = de.finalize_quote_all(
        {"checkin": "2026-07-06", "checkout": "2026-07-07", "adults": 2, "children_ages": []}, avail)
    assert "Стандарт" in reply and "грн" in reply
    assert "Напівлюкс" not in reply          # sold-out type skipped
    assert "Який тип номеру обираєте" in reply


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

def test_finalize_full_sold_out_forward_scans_to_real_dates():
    # Case 5 / A2 Step 2: fully booked -> forward-scan THIS room to real nearest dates.
    avail = {"Стандарт": {"2026-07-05": 0, "2026-07-06": 0,
                          "2026-07-08": 2, "2026-07-09": 2, "2026-07-10": 2}}
    reply = de.finalize_quote(
        [{"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
          "adults": 2, "children_ages": []}], avail)
    assert "всі номери заброньовані" in reply and "8 - 10 липня" in reply   # SOLD_OUT_FOUND_NEAREST
    assert reply != templates.POLITE_CLOSE


def test_finalize_full_sold_out_no_window_keeps_dialogue_open():
    # Fix 2: nothing free even after the infinite scan -> NEAREST_NONE, but NO manager
    # hand-off / phone ask; the dialogue stays open for other dates.
    avail = {"Стандарт": {"2026-07-05": 0, "2026-07-06": 0}}
    reply = de.finalize_quote(
        [{"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
          "adults": 2, "children_ages": []}], avail)
    assert reply == templates.NEAREST_NONE
    assert "менеджер" not in reply.lower()


def test_finalize_quote_all_sold_out_auto_proposes_nearest():
    # Bug 2: exact dates sold out, no room chosen -> AUTO-propose the nearest free window
    # (do NOT ask permission). SOLD_OUT_NEAREST is no longer used here.
    avail = {"Стандарт": {"2026-07-06": 0, "2026-07-07": 0, "2026-07-09": 2, "2026-07-10": 2},
             "Стандарт +": {"2026-07-06": 0, "2026-07-07": 0},
             "Напівлюкс": {"2026-07-06": 0, "2026-07-07": 0}}
    reply = de.finalize_quote_all(
        {"checkin": "2026-07-06", "checkout": "2026-07-07", "adults": 2, "children_ages": []}, avail)
    assert "вказані вами дати всі номери заброньовані" in reply   # SOLD_OUT_FOUND_NEAREST
    assert "9 - 10 липня" in reply
    assert reply != templates.SOLD_OUT_NEAREST


def test_finalize_quote_all_sold_out_no_window_nearest_none():
    # Nothing free anywhere in the visible window -> NEAREST_NONE (still no permission ask).
    avail = {"Стандарт": {"2026-07-06": 0, "2026-07-07": 0},
             "Стандарт +": {"2026-07-06": 0, "2026-07-07": 0},
             "Напівлюкс": {"2026-07-06": 0, "2026-07-07": 0}}
    reply = de.finalize_quote_all(
        {"checkin": "2026-07-06", "checkout": "2026-07-07", "adults": 2, "children_ages": []}, avail)
    assert reply == templates.NEAREST_NONE


def test_build_quote_reply_groups_identical_rooms():
    # Bug 3: two identical rooms collapse into ONE "за 2 номери типу X" line (combined
    # price), not two summed lines.
    room = {"room_type": "Стандарт", "adults": 2, "children_ages": [], "checkin": "2026-07-06",
            "checkout": "2026-07-08", "nights": 2, "price": 5000, "ubd": False}
    reply = de.build_quote_reply([dict(room), dict(room)])
    assert "за 2 номери типу Стандарт" in reply
    assert "10000 грн" in reply
    assert reply.count("Вартість") == 1            # one collapsed line, no grand-total block


def test_build_quote_reply_distinct_rooms_sum():
    # Genuinely different rooms still get per-room lines + a grand total.
    rooms = [
        {"room_type": "Стандарт", "adults": 2, "children_ages": [], "checkin": "2026-07-06",
         "checkout": "2026-07-08", "nights": 2, "price": 5000, "ubd": False},
        {"room_type": "Напівлюкс", "adults": 2, "children_ages": [8], "checkin": "2026-07-06",
         "checkout": "2026-07-08", "nights": 2, "price": 7000, "ubd": False},
    ]
    reply = de.build_quote_reply(rooms)
    assert "Загальна вартість: 12000 грн" in reply
    assert reply.count("Вартість номеру типу") == 2


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


def test_finalize_missing_dates_default_sold_out():
    # Owner fix 2026-06-24: dates the scraper didn't return default to SOLD OUT, never quoted.
    # Here only 5 July is known; an August request has no forward window in the data.
    avail = {"Стандарт": {"2026-07-05": 3}}
    reply = de.finalize_quote(
        [{"room_type": "Стандарт", "checkin": "2026-08-10", "checkout": "2026-08-11",
          "adults": 2, "children_ages": []}], avail)
    assert "буде вартувати" not in reply         # NOT quoted
    assert reply == templates.NEAREST_NONE


def test_finalize_soldout_specific_room_falls_back_to_any_fitting():
    # Fix 5: chosen Стандарт has no forward window, but Напівлюкс does -> offer it (always
    # offer SOMETHING; never the permission-asking NEAREST_NONE when a fitting window exists).
    avail = {"Стандарт": {"2026-07-05": 0, "2026-07-06": 0},
             "Напівлюкс": {"2026-07-05": 0, "2026-07-06": 0, "2026-07-08": 2, "2026-07-09": 2}}
    reply = de.finalize_quote(
        [{"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
          "adults": 2, "children_ages": []}], avail)
    assert "найближче вільне віконце" in reply and "8 - 10 липня" in reply
    assert reply != templates.NEAREST_NONE


def test_nearest_none_does_not_ask_permission():
    # Fix 5: the fallback message must NOT ask "можливо вас зацікавлять інші дати?".
    assert "зацікавлять інші дати" not in templates.NEAREST_NONE
    assert "підшукати" not in templates.NEAREST_NONE


# --- УБД (combat-veteran) 20% discount, deterministic ----------------------

def test_apply_military_discount():
    import pricing_engine
    assert pricing_engine.apply_military_discount(4400) == 3520
    assert pricing_engine.apply_military_discount(5000) == 4000


def test_finalize_ubd_single_room_discount_and_text():
    reply = de.finalize_quote(
        [{"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-08",
          "adults": 2, "children_ages": [], "ubd": True}], {})
    assert "3520 грн" in reply            # 4400 * 0.8
    assert "УБД" in reply
    assert templates.MILITARY in reply    # UBD template appended


def test_finalize_ubd_whole_booking_discounts_all_rooms():
    # Owner rule 2026-06-23: УБД -20% applies to the ENTIRE booking (a veteran's family),
    # even if only one room carries the flag. Per-room lines stay at full price; the GRAND
    # TOTAL is discounted -20%.
    rooms = [
        {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-08",
         "adults": 2, "children_ages": [], "ubd": True},
        {"room_type": "Напівлюкс", "checkin": "2026-07-06", "checkout": "2026-07-08",
         "adults": 2, "children_ages": [], "ubd": False},
    ]
    reply = de.finalize_quote(rooms, {})
    assert "4400 грн" in reply and "5400 грн" in reply        # per-room lines at FULL price
    assert "Загальна вартість: 7840 грн" in reply             # (4400+5400)=9800 * 0.8
    assert "7920" not in reply                                # NOT the old per-room math
    assert "УБД" in reply and templates.MILITARY in reply


# --- 6+ guest distribution (owner rule 2026-06-23) -------------------------

def test_plan_six_plus_guests_in_one_room_asks_distribution():
    # 6+ guests can't share one room (max ~5) -> show capacities THEN ask HOW to split.
    out = de.plan({"rooms": [{"room_type": None, "checkin": "2026-07-06", "checkout": "2026-07-08",
                              "adults": 6, "children_count": 0, "children_ages": []}]})
    assert out["action"] == "reply"
    assert templates.PRESENTATION_ROOMS in out["reply"]       # capacities shown first
    assert templates.ASK_ROOM_DISTRIBUTION in out["reply"]    # then the split question
    assert "[SPLIT]" in out["reply"]


def test_plan_six_guests_counts_children_too():
    # 2 adults + 4 children still = 6 bodies -> distribution ask.
    out = de.plan({"rooms": [{"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-08",
                              "adults": 2, "children_count": 4, "children_ages": [5, 7, 9, 11]}]})
    assert templates.ASK_ROOM_DISTRIBUTION in out["reply"]


def test_plan_five_guests_one_room_still_quotes():
    # 5 fits Напівлюкс -> NOT a distribution ask; no room chosen -> quote_all.
    out = de.plan({"rooms": [{"room_type": None, "checkin": "2026-07-06", "checkout": "2026-07-08",
                              "adults": 5, "children_count": 0, "children_ages": []}]})
    assert out["action"] == "quote_all"


def test_plan_six_plus_explicit_two_rooms_proceeds():
    # Already split across 2 rooms -> the client said how -> proceed to quote.
    out = de.plan({"rooms": [
        {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-08",
         "adults": 3, "children_ages": []},
        {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-08",
         "adults": 3, "children_ages": []}]})
    assert out["action"] == "quote"


def test_finalize_quote_all_filters_over_capacity_standard():
    # Owner capacity gate: 4 adults can't fit Стандарт/Стандарт+ (max 3 adults) -> only Напівлюкс.
    avail = {"Стандарт": {"2026-07-06": 3, "2026-07-07": 3},
             "Стандарт +": {"2026-07-06": 3, "2026-07-07": 3},
             "Напівлюкс": {"2026-07-06": 2, "2026-07-07": 2}}
    reply = de.finalize_quote_all(
        {"checkin": "2026-07-06", "checkout": "2026-07-07", "adults": 4, "children_ages": []}, avail)
    assert "Напівлюкс" in reply
    assert "Стандарт" not in reply        # both standard-class lines filtered out (impossible)


def test_finalize_quote_chosen_too_small_redirects_to_fitting():
    # User chose Стандарт for 4 adults -> redirect to the room types that DO fit (Напівлюкс).
    avail = {"Стандарт": {"2026-07-06": 3, "2026-07-07": 3},
             "Напівлюкс": {"2026-07-06": 2, "2026-07-07": 2}}
    reply = de.finalize_quote(
        [{"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-07",
          "adults": 4, "children_ages": []}], avail)
    assert "Напівлюкс" in reply and "Який тип номеру обираєте" in reply


def test_finalize_multiroom_one_over_capacity_keeps_others():
    # Review fix 2026-06-24: in a MULTI-room booking, an over-capacity room must NOT silently
    # drop the others (early-return bug). Room 0 (4 adults in Стандарт) is too small; the bot
    # explains the bad split instead of quoting only one room's type-picker.
    avail = {"Стандарт": {"2026-07-06": 3, "2026-07-07": 3}}
    rooms = [
        {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-08",
         "adults": 4, "children_ages": []},
        {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-08",
         "adults": 2, "children_ages": []},
    ]
    reply = de.finalize_quote(rooms, avail)
    assert templates.ROOM_TOO_SMALL.replace("{деталі}", "«Стандарт» — 4 дорослих") in reply
    assert templates.PRESENTATION_ROOMS in reply     # capacities shown first
    assert "Який тип номеру обираєте" not in reply   # NOT the single-room picker that dropped room 1


def test_nearest_window_any_respects_capacity():
    # 4 adults: only Напівлюкс fits -> the nearest window must come from Напівлюкс, never an
    # earlier Стандарт window (which physically can't hold 4).
    avail = {"Стандарт": {f"2026-07-{d:02d}": 2 for d in range(6, 12)},   # Стандарт free early
             "Напівлюкс": {"2026-07-20": 2, "2026-07-21": 2}}             # Напівлюкс only later
    assert de.nearest_window_any(avail, "2026-07-05", 1, fit_adults=4) == ("2026-07-20", "2026-07-21")
    # No party constraint -> earliest across any type (legacy behaviour).
    assert de.nearest_window_any(avail, "2026-07-05", 1)[0] == "2026-07-06"


def test_finalize_quote_all_over_capacity_offers_fitting_nearest():
    # 4 adults on 06-07: Стандарт free (too small) + Напівлюкс sold out -> offer the nearest
    # NAPIVLUX window, never the free-but-too-small Стандарт dates.
    avail = {"Стандарт": {"2026-07-06": 3, "2026-07-07": 3},
             "Напівлюкс": {"2026-07-06": 0, "2026-07-07": 0,
                           "2026-07-10": 2, "2026-07-11": 2}}
    reply = de.finalize_quote_all(
        {"checkin": "2026-07-06", "checkout": "2026-07-07", "adults": 4, "children_ages": []}, avail)
    assert "10 - 11 липня" in reply


def test_finalize_ubd_soldout_nearest_appends_military():
    # Fix (2026-06-24): sold out + UBD -> SOLD_OUT_FOUND_NEAREST WITH the УБД note appended,
    # so the veteran knows -20% still applies to the offered window.
    avail = {"Стандарт": {"2026-07-05": 0, "2026-07-06": 0,
                          "2026-07-08": 2, "2026-07-09": 2, "2026-07-10": 2}}
    reply = de.finalize_quote(
        [{"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
          "adults": 2, "children_ages": [], "ubd": True}], avail)
    assert "найближче вільне віконце" in reply and "8 - 10 липня" in reply
    assert templates.MILITARY in reply


def test_finalize_ubd_room_booked_appends_military():
    # Chosen room sold out, another category free, UBD flagged -> ROOM_BOOKED + УБД note.
    avail = {"Стандарт": {"2026-07-05": 0, "2026-07-06": 0},
             "Напівлюкс": {"2026-07-05": 2, "2026-07-06": 2}}
    reply = de.finalize_quote(
        [{"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
          "adults": 2, "children_ages": [], "ubd": True}], avail)
    assert "Напівлюкс" in reply and templates.MILITARY in reply


def test_finalize_soldout_without_ubd_has_no_military():
    # Without УБД, the sold-out alternative must NOT carry the military note.
    avail = {"Стандарт": {"2026-07-05": 0, "2026-07-06": 0,
                          "2026-07-08": 2, "2026-07-09": 2, "2026-07-10": 2}}
    reply = de.finalize_quote(
        [{"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
          "adults": 2, "children_ages": []}], avail)
    assert templates.MILITARY not in reply


def test_finalize_quote_all_ubd_soldout_appends_military():
    avail = {"Стандарт": {"2026-07-06": 0, "2026-07-07": 0, "2026-07-09": 2, "2026-07-10": 2},
             "Стандарт +": {"2026-07-06": 0, "2026-07-07": 0},
             "Напівлюкс": {"2026-07-06": 0, "2026-07-07": 0}}
    reply = de.finalize_quote_all(
        {"checkin": "2026-07-06", "checkout": "2026-07-07", "adults": 2,
         "children_ages": [], "ubd": True}, avail)
    assert "найближче вільне віконце" in reply and templates.MILITARY in reply


def test_finalize_quote_all_family_recommends_napivlux():
    # Family of 4 (2 adults + 2 kids) -> prioritise Напівлюкс + offer the two-room split.
    avail = {"Стандарт": {"2026-07-06": 3, "2026-07-07": 3},
             "Стандарт +": {"2026-07-06": 3, "2026-07-07": 3},
             "Напівлюкс": {"2026-07-06": 1, "2026-07-07": 1}}
    reply = de.finalize_quote_all(
        {"checkin": "2026-07-06", "checkout": "2026-07-08", "adults": 2, "children_ages": [8, 10]}, avail)
    assert "Напівлюкс" in reply
    assert "два окремі номери" in reply      # offers the roomier split alternative
