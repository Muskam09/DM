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


def test_parse_slots_denulls_stringy_null_values():
    # LIVE-LLM quirk (caught by live_auto_qa.py, invisible to offline mocks): the extractor
    # sometimes emits the STRING "null"/"None"/"" instead of JSON null. These MUST become
    # Python None, or "двомісний номер" scans a bogus room type "null" -> empty availability
    # -> a false NEAREST_NONE ("all booked"), dropping real free windows.
    s = de.parse_slots('{"topic":"fuzzy_dates","rooms":[{"room_type":"null",'
                       '"checkin":"None","checkout":"","fuzzy_date":"друга половина липня",'
                       '"nights":"null","adults":2,"children_ages":[]}]}')
    r = s["rooms"][0]
    assert r["room_type"] is None
    assert r["checkin"] is None and r["checkout"] is None and r["nights"] is None
    assert r["fuzzy_date"] == "друга половина липня"     # a REAL value is left untouched
    assert r["adults"] == 2


def test_plan_stringy_null_room_type_not_treated_as_chosen():
    # After denulling, a stringy-null room_type with exact dates must route to quote_all
    # (price every type), NOT quote a nonexistent "null" room.
    s = de.parse_slots('{"topic":"price_quote","rooms":[{"room_type":"null",'
                       '"checkin":"2026-07-06","checkout":"2026-07-08","adults":2,"children_ages":[]}]}')
    assert de.plan(s)["action"] == "quote_all"


def test_propose_windows_stringy_null_room_defaults_and_scans():
    # The persona-9 failure end-to-end: a compound fuzzy period with a stringy-null room_type
    # must still scan the default room type and offer BOTH months (not NEAREST_NONE).
    avail = {"Стандарт": {**{f"2026-07-{d:02d}": 2 for d in range(19, 27)},
                          **{f"2026-08-{d:02d}": 2 for d in range(6, 21)}}}
    s = de.parse_slots('{"topic":"fuzzy_dates","rooms":[{"room_type":"null",'
                       '"fuzzy_date":"друга половина липня або після 6 серпня",'
                       '"adults":2,"children_ages":[]}]}')
    reply = de.propose_windows(s["rooms"][0], avail)
    assert "вільні віконця" in reply and "серпня" in reply and "липня" in reply
    assert reply != templates.NEAREST_NONE


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


def test_fuzzy_period_range_parses_normalized_yyyy_mm():
    # Bug 2026-07-05: the extractor normalised "початок серпня" -> "2026-08"; the range mapper
    # must still resolve the month (else an August period offers July windows).
    assert de.fuzzy_period_range("2026-08") == ("2026-08-01", "2026-08-31")
    assert de.fuzzy_period_range("2026-07") == ("2026-07-01", "2026-07-31")
    assert de.fuzzy_period_range("2026-01") is None          # off-season month -> unconstrained


def test_fuzzy_period_ranges_compound_keeps_all_months():
    # Date-horizon fix: a compound period naming SEVERAL months must scan EVERY named month —
    # dropping the 2nd month made an August request silently offer only July windows.
    assert de.fuzzy_period_ranges("друга половина липня або після 6 серпня") == [
        ("2026-07-16", "2026-07-31"), ("2026-08-06", "2026-08-31")]
    assert de.fuzzy_period_ranges("липень чи серпень") == [
        ("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-08-31")]
    assert de.fuzzy_period_ranges("у липні і серпні") == [
        ("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-08-31")]
    assert de.fuzzy_period_ranges("початок серпня") == [("2026-08-01", "2026-08-10")]  # single
    assert de.fuzzy_period_ranges("на вихідних") == []       # no month -> unconstrained
    # back-compat single-range view returns the FIRST named window.
    assert de.fuzzy_period_range("друга половина липня або після 6 серпня") == ("2026-07-16", "2026-07-31")


def test_propose_windows_compound_period_offers_second_month():
    # The exact reported miss: "друга половина липня або після 6 серпня" with late July booked
    # must still offer the AUGUST window (never drop the 2nd month).
    avail = {"Стандарт": {
        **{f"2026-07-{d:02d}": 0 for d in range(16, 32)},     # late July fully booked
        **{f"2026-08-{d:02d}": 2 for d in range(6, 14)},      # early-mid August free
    }}
    reply = de.propose_windows(
        {"room_type": "Стандарт", "fuzzy_date": "друга половина липня або після 6 серпня"}, avail)
    assert "вільні віконця" in reply                          # a real window was found
    assert "6 - 13 серпня" in reply                           # the AUGUST window is offered
    # the only DATE window offered is August (late July was booked); the echoed period text
    # naturally still contains "липня", so we check the offered window portion explicitly.
    offered = reply.split("вільні віконця:", 1)[1]
    assert "липня" not in offered


def test_propose_windows_compound_period_prefers_earliest_across_months():
    # Both months free -> windows are offered in chronological order (July first).
    avail = {"Стандарт": {
        **{f"2026-07-{d:02d}": 2 for d in range(16, 22)},     # late July free
        **{f"2026-08-{d:02d}": 2 for d in range(6, 12)},      # August free
    }}
    reply = de.propose_windows(
        {"room_type": "Стандарт", "fuzzy_date": "друга половина липня або після 6 серпня"}, avail)
    assert "липня" in reply                                   # earliest (July) window shown


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

def test_plan_six_plus_guests_in_one_room_proposes_split():
    # Owner #21: 6+ guests can't share one room -> show capacities THEN PROACTIVELY propose a
    # valid split (composition known) rather than only asking how to split.
    out = de.plan({"rooms": [{"room_type": None, "checkin": "2026-07-06", "checkout": "2026-07-08",
                              "adults": 6, "children_count": 0, "children_ages": []}]})
    assert out["action"] == "reply"
    assert templates.PRESENTATION_ROOMS in out["reply"]       # capacities shown first
    assert "[SPLIT]" in out["reply"]
    assert "розподілити" in out["reply"]                       # SUGGEST_GROUP_SPLIT
    assert "2 номери" in out["reply"] and "3 + 3" in out["reply"]   # 6 adults -> 2 rooms of 3


def test_plan_six_guests_counts_children_too_proposes_split():
    # 2 adults + 4 children still = 6 bodies -> a proposed split (composition known).
    out = de.plan({"rooms": [{"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-08",
                              "adults": 2, "children_count": 4, "children_ages": [5, 7, 9, 11]}]})
    assert "розподілити" in out["reply"] and "2 номери" in out["reply"]


def test_plan_six_plus_unknown_ages_still_asks_distribution():
    # If a child's age is UNKNOWN we can't compute a precise valid split -> ask how to split.
    out = de.plan({"rooms": [{"room_type": None, "checkin": "2026-07-06", "checkout": "2026-07-08",
                              "adults": 2, "children_count": 4, "children_ages": []}]})
    assert templates.ASK_ROOM_DISTRIBUTION in out["reply"]


def test_suggest_group_distribution():
    # STANDARD-priority (owner 2026-07-10): split into Стандарт-sized rooms (<=3 people), biggest
    # first. 6 adults + 4 kids -> FOUR Standards 3+3+2+2, never 3+3+4 (which needs a Напівлюкс).
    assert de.suggest_group_distribution(6, []) == [3, 3]                    # 6 adults -> 2×3
    assert de.suggest_group_distribution(7, []) == [3, 2, 2]                 # 7 adults -> 3 rooms
    assert de.suggest_group_distribution(6, [2, 8, 11, 14]) == [3, 3, 2, 2]  # owner #21: 4 Standards
    assert de.suggest_group_distribution(2, [5, 7, 9, 11]) == [3, 3]         # 2 ad + 4 kids -> 2×3
    # every room respects <=3 people (Standard priority) AND <=3 adults; sums to the party size.
    for a, ages in [(6, []), (7, []), (6, [2, 8, 11, 14]), (2, [5, 7, 9, 11]), (10, [3, 4])]:
        counts = de.suggest_group_distribution(a, ages)
        assert all(c <= 3 for c in counts)
        assert sum(counts) == a + len(ages)
        # the accepted split materialises into valid rooms (no kids-only room; each fits a Стандарт)
        for r in de.rooms_from_split(counts, a, ages, "2026-07-06", "2026-07-08"):
            assert r["adults"] >= 1 or not r["children_ages"]


def test_plan_five_guests_one_room_still_quotes():
    # 5 fits Напівлюкс -> NOT a distribution ask; no room chosen -> quote_all.
    out = de.plan({"rooms": [{"room_type": None, "checkin": "2026-07-06", "checkout": "2026-07-08",
                              "adults": 5, "children_count": 0, "children_ages": []}]})
    assert out["action"] == "quote_all"


def test_plan_multiroom_no_type_assigns_fitting_and_quotes_all():
    # A VALID split ("2 номери: 3 і 2", every room <= 3 adults, no types) must quote BOTH rooms
    # with fitting types, never just the first. 3 adults -> Стандарт; 2 adults -> Стандарт.
    out = de.plan({"rooms": [
        {"room_type": None, "checkin": "2026-07-23", "checkout": "2026-07-24", "adults": 3, "children_ages": []},
        {"room_type": None, "checkin": "2026-07-23", "checkout": "2026-07-24", "adults": 2, "children_ages": []}]})
    assert out["action"] == "quote"
    assert len(out["rooms"]) == 2                                   # BOTH rooms kept
    assert [r["room_type"] for r in out["rooms"]] == ["Стандарт", "Стандарт"]


def test_plan_multiroom_over_adult_cap_suggests_valid_split():
    # Owner fix #282-284: "2 номери: 4 і 3" packs 4 adults into ONE room (> max 3 adults). The
    # bot must NOT quote it — it suggests a VALID split into enough rooms (7 adults -> 3 номери,
    # distribution 2 + 2 + 3), never a bare rejection.
    out = de.plan({"rooms": [
        {"room_type": None, "checkin": "2026-07-23", "checkout": "2026-07-24", "adults": 4, "children_ages": []},
        {"room_type": None, "checkin": "2026-07-23", "checkout": "2026-07-24", "adults": 3, "children_ages": []}]})
    assert out["action"] == "reply"
    assert "максимум 3 дорослих" in out["reply"]
    assert "3 номери" in out["reply"]           # 7 adults -> 3 rooms
    assert "2 + 2 + 3" in out["reply"]          # even, ascending distribution


def test_assign_fitting_type():
    assert de._assign_fitting_type({"adults": 2, "children_ages": []})["room_type"] == "Стандарт"
    assert de._assign_fitting_type({"adults": 3, "children_ages": []})["room_type"] == "Стандарт"
    assert de._assign_fitting_type({"adults": 4, "children_ages": []})["room_type"] == "Напівлюкс"
    assert de._assign_fitting_type({"adults": 2, "children_ages": [8, 10]})["room_type"] == "Стандарт"
    assert de._assign_fitting_type({"adults": 2, "children_ages": [8, 10, 5]})["room_type"] == "Напівлюкс"


def test_plan_six_plus_explicit_two_rooms_proceeds():
    # Already split across 2 rooms -> the client said how -> proceed to quote.
    out = de.plan({"rooms": [
        {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-08",
         "adults": 3, "children_ages": []},
        {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-08",
         "adults": 3, "children_ages": []}]})
    assert out["action"] == "quote"


def test_finalize_quote_all_group_offers_standard_split_first():
    # Owner rule 2026-07-06: 4 adults -> PRIORITISE several Стандарт/Стандарт+ rooms (more
    # inventory); Напівлюкс only as a secondary fallback. (Reverses the old Напівлюкс-only.)
    avail = {"Стандарт": {"2026-07-06": 3, "2026-07-07": 3},
             "Стандарт +": {"2026-07-06": 3, "2026-07-07": 3},
             "Напівлюкс": {"2026-07-06": 2, "2026-07-07": 2}}
    reply = de.finalize_quote_all(
        {"checkin": "2026-07-06", "checkout": "2026-07-07", "adults": 4, "children_ages": []}, avail)
    assert "2 номери Стандарт" in reply             # split offered (2+2)
    assert "рекомендуємо кілька" in reply           # standard rooms prioritised
    assert "Напівлюкс" in reply                      # secondary fallback still shown
    assert reply.index("Стандарт") < reply.index("Напівлюкс")   # standard BEFORE Напівлюкс


def test_finalize_quote_chosen_too_small_redirects_to_standard_split():
    # User chose Стандарт for 4 adults -> redirect offers the Стандарт SPLIT (owner priority),
    # with Напівлюкс as the secondary option.
    avail = {"Стандарт": {"2026-07-06": 3, "2026-07-07": 3},
             "Напівлюкс": {"2026-07-06": 2, "2026-07-07": 2}}
    reply = de.finalize_quote(
        [{"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-07",
          "adults": 4, "children_ages": []}], avail)
    assert "2 номери Стандарт" in reply and "Напівлюкс" in reply
    assert "Який варіант обираєте" in reply


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


def test_finalize_quote_all_group_uses_standard_split_when_napivlux_soldout():
    # 4 adults on 06-07: Напівлюкс sold out, but Стандарт has 2+ free -> the split now fits on
    # the REQUESTED dates (owner priority), so we offer it rather than a nearest window.
    avail = {"Стандарт": {"2026-07-06": 3, "2026-07-07": 3},
             "Напівлюкс": {"2026-07-06": 0, "2026-07-07": 0,
                           "2026-07-10": 2, "2026-07-11": 2}}
    reply = de.finalize_quote_all(
        {"checkin": "2026-07-06", "checkout": "2026-07-07", "adults": 4, "children_ages": []}, avail)
    assert "2 номери Стандарт" in reply
    assert "10 - 11" not in reply          # no nearest window needed — the split fits the dates


def test_finalize_quote_all_group_no_split_available_offers_fitting_nearest():
    # 4 adults, Стандарт has only ONE free room (can't split into 2) and Напівлюкс sold out on
    # the dates -> fall back to the nearest FITTING window (Напівлюкс 10-11).
    avail = {"Стандарт": {"2026-07-06": 1, "2026-07-07": 1},
             "Напівлюкс": {"2026-07-06": 0, "2026-07-07": 0,
                           "2026-07-10": 2, "2026-07-11": 2}}
    reply = de.finalize_quote_all(
        {"checkin": "2026-07-06", "checkout": "2026-07-07", "adults": 4, "children_ages": []}, avail)
    assert "10 - 11 липня" in reply


def test_finalize_soldout_crosssell_is_capacity_aware():
    # Persona 15 live: a 4-adult room's chosen type (Напівлюкс) is sold out; the free Стандарт/
    # Стандарт+ DON'T fit 4 -> never offer them. Fall through to the nearest FITTING window.
    avail = {"Напівлюкс": {"2026-07-23": 0, "2026-07-24": 0, "2026-07-30": 2, "2026-07-31": 2},
             "Стандарт": {"2026-07-23": 3, "2026-07-24": 3},
             "Стандарт +": {"2026-07-23": 3, "2026-07-24": 3}}
    reply = de.finalize_quote(
        [{"room_type": "Напівлюкс", "checkin": "2026-07-23", "checkout": "2026-07-24",
          "adults": 4, "children_ages": []}], avail)
    assert "Стандарт" not in reply          # non-fitting types NOT offered for a 4-adult room
    assert "30 - 31 липня" in reply         # nearest FITTING (Напівлюкс) window instead


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


def test_finalize_quote_all_family_recommends_standard_first():
    # Owner rule 2026-07-06 (REVERSED): a family of 4 (2 adults + 2 kids) -> prioritise
    # Стандарт/Стандарт+ (double bed + sofa); Напівлюкс is the LAST resort, not the pick.
    avail = {"Стандарт": {"2026-07-06": 3, "2026-07-07": 3},
             "Стандарт +": {"2026-07-06": 3, "2026-07-07": 3},
             "Напівлюкс": {"2026-07-06": 1, "2026-07-07": 1}}
    reply = de.finalize_quote_all(
        {"checkin": "2026-07-06", "checkout": "2026-07-08", "adults": 2, "children_ages": [8, 10]}, avail)
    assert "добре підійде Стандарт" in reply          # standard prioritised for the family
    assert "два окремі номери" not in reply            # no longer the old Напівлюкс/split pitch
    assert reply.index("Стандарт") < reply.index("Напівлюкс")   # standard listed BEFORE Напівлюкс


# --- OWNER LIVE-QA FIXES 2026-07-08 (dialogues #266/#271/#274/#275/#278/#282-284) ----

def test_free_windows_never_bridges_noncontiguous_zero_middle():
    # Fix #271: 12-16 free, 17-18 BOOKED (count 0), 19-27 free -> TWO windows, never one
    # bridged "12-27" span. The bot must never merge over unavailable days.
    avail = {**{f"2026-07-{d:02d}": 2 for d in range(12, 17)},
             "2026-07-17": 0, "2026-07-18": 0,
             **{f"2026-07-{d:02d}": 2 for d in range(19, 28)}}
    dates = sorted(avail)
    wins = de._free_windows(avail, dates, min_nights=2, room_count=1)
    assert wins == [("2026-07-12", "2026-07-16"), ("2026-07-19", "2026-07-27")]


def test_free_windows_never_bridges_missing_middle_dates():
    # Same, but 17-18 are entirely ABSENT from the scrape (not present as 0). Missing days must
    # STILL break the run — a gap in the calendar is unavailable, never bridged.
    avail = {**{f"2026-07-{d:02d}": 2 for d in range(12, 17)},
             **{f"2026-07-{d:02d}": 2 for d in range(19, 28)}}
    dates = sorted(avail)      # 17,18 not present
    wins = de._free_windows(avail, dates, min_nights=2, room_count=1)
    assert wins == [("2026-07-12", "2026-07-16"), ("2026-07-19", "2026-07-27")]


def test_propose_windows_offers_both_noncontiguous_windows_not_bridged():
    # End-to-end #271: a fuzzy July scan over a calendar free 12-16 and 19-27 must offer BOTH
    # discrete windows, never the illegal merged "12 - 22"/"12 - 27".
    avail = {"Стандарт": {**{f"2026-07-{d:02d}": 2 for d in range(12, 17)},
                          "2026-07-17": 0, "2026-07-18": 0,
                          **{f"2026-07-{d:02d}": 2 for d in range(19, 28)}}}
    reply = de.propose_windows({"room_type": "Стандарт", "fuzzy_date": "липень"}, avail)
    assert "12 - 16 липня" in reply and "19 - 27 липня" in reply
    assert "12 - 22" not in reply and "12 - 27" not in reply


def test_sold_out_found_nearest_states_matching_nights():
    # Fix #274: SOLD_OUT_FOUND_NEAREST states the nights, and they ALIGN with the offered dates.
    # Requested 4-night stay (8-12 July) is booked; the nearest 4-night window is offered WITH
    # "(4 ночі)". (Dates are WITHIN the visible calendar, so this is a real sold-out, not "past".)
    avail = {"Стандарт +": {**{f"2026-07-{d:02d}": 0 for d in range(1, 12)},
                            **{f"2026-07-{d:02d}": 2 for d in range(12, 20)}}}
    reply = de.finalize_quote(
        [{"room_type": "Стандарт +", "checkin": "2026-07-08", "checkout": "2026-07-12",
          "adults": 2, "children_ages": []}], avail)
    assert "найближче вільне віконце" in reply
    assert "4 ночі" in reply                       # nights stated
    assert "12 - 16 липня" in reply                # exactly 4 nights (12,13,14,15) -> checkout 16


def test_finalize_past_dates_asks_for_real_dates():
    # Owner #299 (2026-07-10): a stay EARLIER than the whole visible calendar has passed -> say so,
    # never "all rooms booked", never invent an offset window.
    avail = {"Стандарт +": {f"2026-07-{d:02d}": 2 for d in range(10, 20)}}   # calendar starts 10 Jul
    reply = de.finalize_quote(
        [{"room_type": "Стандарт +", "checkin": "2026-06-13", "checkout": "2026-06-17",
          "adults": 2, "children_ages": []}], avail)
    assert reply == templates.PAST_DATES
    assert "минули" in reply and "найближче вільне віконце" not in reply


def test_finalize_quote_all_skips_type_absent_in_covered_window():
    # Fix #275: only Стандарт is in the scrape (window covered) — Стандарт+/Напівлюкс are ABSENT.
    # The bot must recommend ONLY Стандарт, never a type OtelMS didn't confirm free.
    avail = {"Стандарт": {"2026-07-06": 3, "2026-07-07": 3}}   # the ONLY type present
    reply = de.finalize_quote_all(
        {"checkin": "2026-07-06", "checkout": "2026-07-07", "adults": 2, "children_ages": []}, avail)
    assert "Стандарт" in reply and "грн" in reply
    assert "Напівлюкс" not in reply and "Стандарт +" not in reply   # absent types NOT recommended


def test_finalize_quote_all_far_future_all_unknown_stays_lenient():
    # A window entirely OUTSIDE the visible calendar (empty scrape -> every type 'unknown') stays
    # lenient: still list the priced types (far-future exact dates aren't sold-out).
    reply = de.finalize_quote_all(
        {"checkin": "2026-08-20", "checkout": "2026-08-21", "adults": 2, "children_ages": []}, {})
    assert "Стандарт" in reply and "Напівлюкс" in reply and "грн" in reply


def test_finalize_quote_all_ubd_note_on_price_and_no_aside():
    # Fix #266: the UBD note "(з урахуванням знижки УБД -20%)" is on the priced message, and the
    # "(Стандартів у нас більше, ніж Напівлюксів)" aside is gone (4 adults -> standard split).
    avail = {"Стандарт": {"2026-08-01": 3, "2026-08-02": 3},
             "Стандарт +": {"2026-08-01": 3, "2026-08-02": 3},
             "Напівлюкс": {"2026-08-01": 2, "2026-08-02": 2}}
    reply = de.finalize_quote_all(
        {"checkin": "2026-08-01", "checkout": "2026-08-02", "adults": 4,
         "children_ages": [], "ubd": True}, avail)
    assert "(з урахуванням знижки УБД -20%)" in reply
    assert "Стандартів у нас більше" not in reply
    assert templates.MILITARY in reply


def test_finalize_quote_all_ubd_note_single_listing():
    # The UBD note also lands on a single-room-listing (2 adults) priced message.
    avail = {"Стандарт": {"2026-08-01": 3, "2026-08-02": 3}}
    reply = de.finalize_quote_all(
        {"checkin": "2026-08-01", "checkout": "2026-08-02", "adults": 2,
         "children_ages": [], "ubd": True}, avail)
    assert "(з урахуванням знижки УБД -20%)" in reply
    assert templates.MILITARY in reply


# --- OWNER CALENDAR-MATH ROUND 2 (2026-07-09): bridging / hallucination (deterministic) ----
# These replicate the EXACT owner scenarios (#287, #296, #288) with a FIXED availability map,
# so they PROVE no-bridging immune to the live OtelMS calendar mutating between runs.

def test_stay_all_free_validator():
    m = {"2026-07-12": 2, "2026-07-13": 2, "2026-07-14": 0, "2026-07-15": 2}
    assert de._stay_all_free(m, "2026-07-12", "2026-07-14") is True    # nights 12,13 free
    assert de._stay_all_free(m, "2026-07-12", "2026-07-15") is False   # night 14 booked
    assert de._stay_all_free(m, "2026-07-13", "2026-07-16") is False   # night 14 booked
    assert de._stay_all_free(m, "2026-07-15", "2026-07-16") is True
    assert de._stay_all_free(m, "2026-07-20", "2026-07-21") is False   # missing date == booked
    assert de._stay_all_free(m, "2026-07-12", "2026-07-12") is False   # zero-night range


def _avail_287():
    # free 10-16, BOOKED 17-18, free 19-27 — the exact owner #287 calendar.
    a = {}
    for d in range(10, 17): a[f"2026-07-{d:02d}"] = 5
    a["2026-07-17"] = 0; a["2026-07-18"] = 0
    for d in range(19, 28): a[f"2026-07-{d:02d}"] = 5
    return {"Стандарт": a}


def test_find_nearest_window_never_bridges_booked_gap_287():
    av = _avail_287()
    # A 4- or 7-night block fits BEFORE the booked 17-18; a 10-night block CANNOT exist
    # contiguously (17-18 break it) -> None, never a bridged "12-22".
    assert de.find_nearest_window(av, "Стандарт", "2026-06-22", 4) == ("2026-07-10", "2026-07-14")
    assert de.find_nearest_window(av, "Стандарт", "2026-06-22", 7) == ("2026-07-10", "2026-07-17")
    assert de.find_nearest_window(av, "Стандарт", "2026-06-22", 10) is None
    # nearest_window_any (used by finalize_quote_all) must also refuse a 10-night bridge.
    assert de.nearest_window_any(av, "2026-06-22", 10, fit_adults=1) is None


def test_propose_windows_owner_287_offers_two_separate_windows():
    reply = de.propose_windows({"room_type": "Стандарт", "fuzzy_date": "липень"}, _avail_287())
    assert "10 - 16 липня" in reply and "19 - 27 липня" in reply     # two DISTINCT windows
    assert "12 - 22" not in reply and "10 - 27" not in reply and "10 - 22" not in reply  # never bridged


def test_finalize_quote_all_owner_296_no_soldout_hallucination():
    # Aug 1-5 BOOKED, 6-15 free. A request for 1-5 Aug must NOT be quoted; the nearest window is
    # 6-10, never a hallucinated "1-10 серпня" that spans the booked 1-5.
    a = {}
    for d in range(1, 6): a[f"2026-08-{d:02d}"] = 0
    for d in range(6, 16): a[f"2026-08-{d:02d}"] = 5
    av = {"Стандарт": a}
    reply = de.finalize_quote_all(
        {"checkin": "2026-08-01", "checkout": "2026-08-05", "adults": 2, "children_ages": []}, av)
    assert "буде вартувати" not in reply                # never priced the booked dates
    assert "6 - 10 серпня" in reply                      # the genuine nearest window
    assert "1 - 10 серпня" not in reply and "1 - 5 серпня" not in reply
    # and propose over the same booked-prefix period offers only the free tail.
    p = de.propose_windows({"room_type": "Стандарт", "fuzzy_date": "початок серпня"}, av)
    assert "6 - 10 серпня" in reply and "1 - 10" not in p


def test_find_nearest_window_exact_inclusive_when_free():
    # Owner #288/#274: when the requested check-in itself starts a fully-free block, return it
    # unchanged (do not skip to a later "weird offset").
    av = {"Стандарт": {f"2026-07-{d:02d}": 3 for d in range(13, 20)}}
    assert de.find_nearest_window(av, "Стандарт", "2026-07-13", 4) == ("2026-07-13", "2026-07-17")


def test_finalize_multiroom_partial_soldout_quotes_available_flags_booked():
    # Owner #23 (2026-07-09, IG two-family case): a MULTI-room booking where one room type is sold
    # out on the dates must QUOTE the available room and flag the booked one — never drop the valid
    # room by short-circuiting on the sold-out sibling.
    avail = {"Стандарт": {"2026-07-20": 5, "2026-07-21": 5, "2026-07-22": 5, "2026-07-23": 5},
             "Напівлюкс": {"2026-07-20": 1, "2026-07-21": 1, "2026-07-22": 1, "2026-07-23": 0}}  # 23 booked
    rooms = [
        {"room_type": "Стандарт", "checkin": "2026-07-20", "checkout": "2026-07-24",
         "adults": 2, "children_ages": [8, 9]},     # 2 adults + 2 children <12 -> fits Стандарт
        {"room_type": "Напівлюкс", "checkin": "2026-07-20", "checkout": "2026-07-24",
         "adults": 3, "children_ages": [13]},
    ]
    reply = de.finalize_quote(rooms, avail)
    assert "Вартість номеру типу Стандарт" in reply        # the AVAILABLE room IS quoted
    assert "грн" in reply
    assert "Напівлюкс" in reply and "зайняті" in reply       # the booked room flagged separately
    assert reply != templates.NEAREST_NONE


def test_finalize_multiroom_all_soldout_offers_nearest():
    # If EVERY room in a multi-room booking is sold out, fall back to the first room's sold-out
    # reply (nearest window), not a partial quote.
    avail = {"Стандарт": {"2026-07-20": 0, "2026-07-21": 0, "2026-07-22": 0, "2026-07-23": 0,
                          "2026-07-25": 5, "2026-07-26": 5, "2026-07-27": 5, "2026-07-28": 5}}
    rooms = [
        {"room_type": "Стандарт", "checkin": "2026-07-20", "checkout": "2026-07-22",
         "adults": 2, "children_ages": []},
        {"room_type": "Стандарт", "checkin": "2026-07-20", "checkout": "2026-07-22",
         "adults": 2, "children_ages": []},
    ]
    reply = de.finalize_quote(rooms, avail)
    assert "буде вартувати" not in reply
    assert "найближче вільне віконце" in reply
