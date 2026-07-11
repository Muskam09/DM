"""
test_webhook.py — behavioural tests for the bot's live flow.

New architecture: the LLM does EXTRACTION ONLY (returns JSON slots); all pricing,
availability gating, formatting and routing are deterministic. So:

  Layer A (pure, runs anywhere): the deterministic helpers — blacklist filtering,
          availability gating, spam/phone detection, greeting/[SPLIT], templates.
  Layer B (needs FastAPI/google-genai -> runs in the bot-brain container): drives
          process_incoming_message / the /webhook handler with the extraction LLM,
          Chatwoot and Playwright boundaries mocked, asserting the deterministic
          routing (quote totals, availability gate, redirects, spam, drip context).
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import bot_logic
import templates


# ===========================================================================
# Layer A — deterministic, dependency-free
# ===========================================================================

DATES_28 = ["2026-06-28"]

RAW_PARTIAL_OVERBOOKING = {
    "Стандарт +": {"total_available": {"2026-06-28": 0}, "rooms": {}},
    "Напівлюкс": {"total_available": {"2026-06-28": 2}, "rooms": {}},
    "Колиба": {"total_available": {"2026-06-28": 5}, "rooms": {}},        # blacklisted
    "Overbooking": {"total_available": {"2026-06-28": 9}, "rooms": {}},   # blacklisted
    "Басейн": {"total_available": {"2026-06-28": 1}, "rooms": {}},        # blacklisted
}
RAW_SOLD_OUT = {
    "Стандарт +": {"total_available": {"2026-06-28": 0}, "rooms": {}},
    "Напівлюкс": {"total_available": {"2026-06-28": 0}, "rooms": {}},
}


def test_blacklist_filtered_and_free_rooms():
    simplified = bot_logic.build_simplified_availability(RAW_PARTIAL_OVERBOOKING)
    assert set(simplified) == {"Стандарт +", "Напівлюкс"}
    assert bot_logic.free_room_types(simplified, DATES_28) == ["Напівлюкс"]
    assert bot_logic.is_sold_out(simplified, DATES_28) is False


def test_full_sold_out_detected():
    simplified = bot_logic.build_simplified_availability(RAW_SOLD_OUT)
    assert bot_logic.is_sold_out(simplified, DATES_28) is True


def test_public_room_type_mapping():
    # Bug 2: OtelMS internal sub-types fold into the 3 public categories.
    assert bot_logic.public_room_type("Стандарт сімейний В+Д") == "Стандарт"
    assert bot_logic.public_room_type("Стандарт 4х 2Л + Д") == "Стандарт"
    assert bot_logic.public_room_type("Стандарт + Сімейний В+Д") == "Стандарт +"
    assert bot_logic.public_room_type("Стандарт +") == "Стандарт +"
    assert bot_logic.public_room_type("Напівлюкс") == "Напівлюкс"
    assert bot_logic.public_room_type("Колиба") is None


def test_build_simplified_folds_internal_names():
    # Bug 2: internal names never reach the client; Стандарт-class availability is summed.
    raw = {
        "Стандарт": {"total_available": {"2026-07-10": 1}, "rooms": {}},
        "Стандарт сімейний В+Д": {"total_available": {"2026-07-10": 2}, "rooms": {}},
        "Стандарт 4х 2Л + Д": {"total_available": {"2026-07-10": 1}, "rooms": {}},
        "Стандарт + Сімейний В+Д": {"total_available": {"2026-07-10": 3}, "rooms": {}},
        "Напівлюкс": {"total_available": {"2026-07-10": 0}, "rooms": {}},
        "Колиба": {"total_available": {"2026-07-10": 9}, "rooms": {}},   # blacklisted
    }
    s = bot_logic.build_simplified_availability(raw)
    assert set(s) == {"Стандарт", "Стандарт +", "Напівлюкс"}     # ONLY public categories
    assert s["Стандарт"]["2026-07-10"] == 4                       # 1+2+1 folded & summed
    assert s["Стандарт +"]["2026-07-10"] == 3
    assert bot_logic.free_room_types(s, ["2026-07-10"]) == ["Стандарт", "Стандарт +"]


# -- availability gating helper (the new pricing gate) ----------------------

def test_is_room_available_states():
    avail = {"Стандарт": {"2026-07-05": 3, "2026-07-06": 0}, "Напівлюкс": {"2026-07-05": 1}}
    assert bot_logic.is_room_available(avail, "Стандарт", ["2026-07-05"]) == "available"
    assert bot_logic.is_room_available(avail, "Стандарт", ["2026-07-06"]) == "sold_out"
    assert bot_logic.is_room_available(avail, "Стандарт", ["2026-07-05", "2026-07-06"]) == "sold_out"
    assert bot_logic.is_room_available(avail, "Président", ["2026-07-05"]) == "unknown"      # room category absent
    # Owner fix 2026-06-24: a date the scraper didn't return defaults to SOLD OUT, not available.
    assert bot_logic.is_room_available(avail, "Стандарт", ["2026-08-10"]) == "sold_out"      # missing date


def test_room_key_matching_handles_spacing():
    avail = {"Стандарт +": {"2026-07-05": 2}}
    assert bot_logic.match_availability_key(avail, "Стандарт+") == "Стандарт +"


# -- greeting / [SPLIT] ------------------------------------------------------

def test_greeting_prepended_and_split_first_turn():
    reply = bot_logic.prepend_greeting_if_needed(templates.QUESTION_ALL_MISSING, bot_has_spoken=False)
    parts = bot_logic.split_messages(reply)
    assert len(parts) == 2
    assert parts[0].startswith("Доброго дня! Вас вітає D&T Hotel")
    assert parts[1] == templates.QUESTION_ALL_MISSING


def test_no_double_greeting():
    assert not bot_logic.prepend_greeting_if_needed("x", bot_has_spoken=True).startswith("Доброго")


# -- B2B spam / phone detection ---------------------------------------------

@pytest.mark.parametrize("text", [
    "Доброго дня! Я з команди Stix, робимо 3D-стікери для вашого бізнесу",
    "Створюю розумних чат-ботів для готелів та ресторанів",
    "Пропоную просування сторінки в інстаграм, є пробний тариф 🤗",
    "Я таргетолог, допоможу залучення клієнтів через рекламу",
])
def test_is_spam_true(text):
    assert bot_logic.is_spam(text) is True


@pytest.mark.parametrize("text", [
    "Доброго дня, є вільні номери на 6-8 липня?",
    "Стандарт + на 2 дорослих, яка ціна?",
    "Скільки коштує сауна?",
    "Чи можна з собакою?",
])
def test_is_spam_false(text):
    assert bot_logic.is_spam(text) is False


@pytest.mark.parametrize("text,expected", [
    ("Вітаю! Я блогер, пропоную співпрацю за бартер — рілс за проживання", True),
    ("Знімаю контент за проживання, цікавить взаємопіар?", True),
    ("Готові на колаборацію — огляд за проживання 🙌", True),
    ("Доброго дня, є вільні номери на 6-8 липня?", False),
    ("Скільки коштує сауна?", False),
    ("Створюю чат-ботів, є пробний тариф", False),   # that's spam, not a wanted collab
])
def test_is_barter(text, expected):
    assert bot_logic.is_barter(text) is expected


@pytest.mark.parametrize("text,expected", [
    ("0959224876 Аліна", True),
    ("+380987521919", True),
    ("мій телефон 067 344 52 20", True),
    ("10-15 серпня, 2 дорослих", False),
    ("вартість 2400 грн", False),
    ("Стандарт +", False),
])
def test_contains_phone_number(text, expected):
    assert bot_logic.contains_phone_number(text) is expected


@pytest.mark.parametrize("text,expected", [
    ("нас 45 людей, корпоратив", True),
    ("Липень на 70-80 чол на 6 днів", True),
    ("весілля на 30 гостей", True),       # event word wins regardless of count
    ("буде 22 гостей", True),             # 20+ threshold (lowered from 40)
    ("приїде 20 осіб", True),             # boundary: 20 >= 20
    ("2 дорослих і дитина 8 років", False),
    ("група 15 дітей, школа", False),     # 15 < 20, not an event
    ("19 осіб у нас", False),             # 19 < 20, no event word
    ("Стандарт на 5-7 липня", False),
])
def test_looks_like_large_group(text, expected):
    assert bot_logic.looks_like_large_group(text) is expected


def test_merge_room_fills_missing_from_memory():
    # Guests remembered, fresh turn dropped them -> restored; new values still win.
    remembered = {"adults": 3, "children_count": 0, "children_ages": [], "room_type": "Стандарт"}
    assert bot_logic.merge_room(remembered, {})["adults"] == 3
    assert bot_logic.merge_room(remembered, {"adults": 2})["adults"] == 2     # fresh wins
    # A fresh turn that mentions dates must NOT inherit stale dates.
    remembered2 = {"checkin": "2026-07-01", "checkout": "2026-07-03", "adults": 2}
    merged = bot_logic.merge_room(remembered2, {"fuzzy_date": "серпень"})
    assert merged.get("checkin") is None and merged["fuzzy_date"] == "серпень"
    # A fresh turn silent on dates inherits the remembered dates.
    merged2 = bot_logic.merge_room(remembered2, {"room_type": "Напівлюкс"})
    assert merged2["checkin"] == "2026-07-01" and merged2["room_type"] == "Напівлюкс"


def test_slots_total_guests():
    assert bot_logic.slots_total_guests({"rooms": [
        {"adults": 10, "children_count": 12, "children_ages": []}]}) == 22
    assert bot_logic.slots_total_guests({"rooms": [
        {"adults": 2, "children_ages": [8]}, {"adults": 3, "children_count": 1}]}) == 7
    assert bot_logic.slots_total_guests({"rooms": []}) == 0


def test_merge_rooms_multi_room_preserves_unmentioned():
    # Decision 3: a 2nd room the fresh turn didn't re-mention must be preserved.
    prev = [{"room_type": "Стандарт", "adults": 2, "checkin": "2026-07-05", "checkout": "2026-07-07"},
            {"room_type": "Напівлюкс", "adults": 3, "checkin": "2026-07-05", "checkout": "2026-07-07"}]
    merged = bot_logic.merge_rooms(prev, [{"room_type": "Стандарт", "adults": 2}])
    assert len(merged) == 2
    assert merged[1]["room_type"] == "Напівлюкс" and merged[1]["checkin"] == "2026-07-05"
    # A brand-new 2nd room is appended; index-0 fields merge.
    merged2 = bot_logic.merge_rooms([{"room_type": "Стандарт", "adults": 2}],
                                    [{"room_type": "Стандарт"}, {"room_type": "Напівлюкс"}])
    assert len(merged2) == 2 and merged2[1]["room_type"] == "Напівлюкс"
    assert merged2[0]["adults"] == 2     # inherited from memory


def test_merge_rooms_backfills_dates_to_split_rooms():
    # Fix (2026-06-24): a 6+ split keeps the remembered single stay's dates on EVERY new
    # room (so the engine scrapes with those dates instead of re-asking).
    prev = [{"checkin": "2026-07-23", "checkout": "2026-07-24", "adults": 7}]
    merged = bot_logic.merge_rooms(prev, [{"adults": 4}, {"adults": 3}])
    assert len(merged) == 2
    assert all(r["checkin"] == "2026-07-23" and r["checkout"] == "2026-07-24" for r in merged)
    assert [r["adults"] for r in merged] == [4, 3]


def test_merge_rooms_split_respects_new_dates():
    # If the split turn names NEW dates, those win and propagate to the dateless split room.
    prev = [{"checkin": "2026-07-23", "checkout": "2026-07-24", "adults": 7}]
    merged = bot_logic.merge_rooms(
        prev, [{"adults": 4, "checkin": "2026-07-25", "checkout": "2026-07-26"}, {"adults": 3}])
    assert merged[0]["checkin"] == "2026-07-25"
    assert merged[1]["checkin"] == "2026-07-25"   # shared from THIS turn, not the stale 23rd


def test_merge_room_inherits_nights_even_with_new_checkin():
    # Sprint-4 Test 21: `nights` is a STAY LENGTH, inherited even when the fresh turn names a NEW
    # check-in ("15 липня" after "дві доби") — so bot_server can derive the check-out.
    remembered = {"nights": 2, "adults": 6, "children_ages": [2, 8, 11, 14]}
    merged = bot_logic.merge_room(remembered, {"checkin": "2026-07-15"})
    assert merged["checkin"] == "2026-07-15" and merged["nights"] == 2
    # A fresh explicit nights still wins over the remembered one.
    assert bot_logic.merge_room({"nights": 2}, {"nights": 3})["nights"] == 3


@pytest.mark.parametrize("text,expected_ci,expected_co", [
    ("З 15 чи 16 липня", "2026-07-15", None),          # choice -> earliest check-in, no checkout
    ("15-17 липня", "2026-07-15", "2026-07-17"),        # month-name range
    ("з 13 по 17 липня", "2026-07-13", "2026-07-17"),   # "з N по M" range
    ("з 1 го по 11 серпня", "2026-08-01", "2026-08-11"),# ordinal suffix "1 го"
    ("15 липня", "2026-07-15", None),                   # single day + month
    ("з 20 серпня", "2026-08-20", None),
    ("13.07-17.07", "2026-07-13", "2026-07-17"),        # dotted range
    ("24-26.07.26", "2026-07-24", "2026-07-26"),        # day-range + dotted month + year
    ("20.07", "2026-07-20", None),                       # single dotted
])
def test_parse_date_request_explicit_dates(text, expected_ci, expected_co):
    out = bot_logic.parse_date_request(text)
    assert out is not None and out.get("checkin") == expected_ci
    assert out.get("checkout") == expected_co


@pytest.mark.parametrize("text", [
    "На 5 ночей з 19",                 # a bare day number (no month/dot) -> NOT a date
    "як минулого разу 15000 було",     # a price -> not a date
    "стандарт за 13500",               # a price -> not a date
    "нас 6 дорослих 4 дитини 2,8,11",  # ages/counts -> not a date
    "Дякую",
    "",
])
def test_parse_date_request_ignores_bare_numbers(text):
    # The fallback must fire ONLY on an explicit month name / dotted date, never a bare number.
    assert bot_logic.parse_date_request(text) is None


@pytest.mark.parametrize("text,expected", [
    ("Нам потрібен один номер з роздільними ліжками а один на двох з одним ліжком", "BED_CONFIG"),
    ("а є номер з двоспальним ліжком?", "BED_CONFIG"),
    ("цікавить котедж на 7 чоловік", "COTTAGE"),
    ("є будиночок для великої компанії?", "COTTAGE"),
])
def test_faq_override_beds_and_cottage(text, expected):
    assert bot_logic.faq_override(text) == expected


# Owner 2026-07-11: NO air conditioners; fridge/balcony answered from GENERAL_INFORMATION.
@pytest.mark.parametrize("text,expected", [
    ("Кондиціонер входить?", "AIR_CONDITIONING"),
    ("чи є кондиціонування в номерах?", "AIR_CONDITIONING"),
    ("є клімат-контроль?", "AIR_CONDITIONING"),
    ("а в стандарт + входить холодильник, кондиціонер та басейн", "AIR_CONDITIONING"),  # AC wins
    ("Тобто не входить холодильник і балкон", "GENERAL_INFORMATION"),
    ("чи є холодильник у номері?", "GENERAL_INFORMATION"),
    ("а балкон є?", "GENERAL_INFORMATION"),
])
def test_faq_override_ac_and_amenities(text, expected):
    assert bot_logic.faq_override(text) == expected


def test_ac_and_amenity_helpers():
    assert bot_logic.is_ac_question("Кондиціонер входить?") is True
    assert bot_logic.is_ac_question("а холодильник є?") is False
    assert bot_logic.is_room_amenity_question("чи є холодильник") is True
    # "курити на балконі" is a SMOKING question, NOT an amenity list.
    assert bot_logic.is_room_amenity_question("чи можна курити на балконі?") is False
    assert bot_logic.faq_override("чи можна курити на балконі?") == "SMOKING"


def test_bed_config_and_cottage_templates_content():
    # BED_CONFIG names BOTH configs the client asked about (owner-confirmed mapping).
    assert "роздільн" in templates.BED_CONFIG and "двоспальн" in templates.BED_CONFIG
    assert "котедж" in templates.COTTAGE and "готель" in templates.COTTAGE
    # AIR_CONDITIONING states there are NO air conditioners.
    assert "кондиціонер" in templates.AIR_CONDITIONING.lower() and "немає" in templates.AIR_CONDITIONING
    # authoritative sub-type -> bed-config knowledge base is present.
    assert bot_logic.BED_CONFIG_MAP["Стандарт 4х 2Л + Д"].startswith("2 роздільні")
    assert "двоспальне" in bot_logic.BED_CONFIG_MAP["Напівлюкс"]


def test_collapse_duplicate_group_rooms_date_choice():
    # Sprint-4 Test 21: "З 15 чи 16 липня" -> the extractor duplicated one 10-person party into TWO
    # dated rooms (10+10=20 -> false large-group). Collapse to ONE group (earliest check-in).
    rooms = [
        {"adults": 6, "children_count": 4, "children_ages": [2, 8, 11, 14],
         "checkin": "2026-07-15", "checkout": "2026-07-17", "nights": 2},
        {"adults": 6, "children_count": 4, "children_ages": [2, 8, 11, 14],
         "checkin": "2026-07-16", "checkout": None, "nights": None},
    ]
    out = bot_logic.collapse_duplicate_group_rooms(rooms)
    assert len(out) == 1
    assert out[0]["checkin"] == "2026-07-15" and out[0]["nights"] == 2
    assert out[0].get("checkout") is None                 # re-derived downstream from check-in+nights
    assert bot_logic.slots_total_guests({"rooms": out}) == 10   # not the doubled 20


def test_label_kill_switch_mutes_on_pozncheno_or_zamovleno():
    # Owner Sprint 5: BOTH "Позначено" and "Замовлено" are a HARD STOP (bot halts, no LLM, no reply).
    assert bot_logic.is_muted(["Позначено"]) is True
    assert bot_logic.is_muted(["Замовлено"]) is True
    assert bot_logic.is_muted(["Instagram", "Позначено"]) is True
    assert bot_logic.is_muted(["Instagram"]) is False
    assert bot_logic.is_muted([]) is False


# --- Bug 2 (Sprint 5): vague/nights-range guards ----------------------------------------------
@pytest.mark.parametrize("text,vague", [
    ("Ще є вільні місця у липні", True),
    ("чи є вільні номери?", True),
    ("маєте щось вільне на серпень?", True),
    ("На 5 ночей з 19", False),
    ("24-26.07.26", False),
])
def test_is_vague_availability_probe(text, vague):
    assert bot_logic.is_vague_availability_probe(text) is vague


@pytest.mark.parametrize("text,rng", [
    ("Днів 4-5", True), ("на 3-5 діб", True), ("4-5 ночей", True),
    ("на 5 ночей", False), ("24-26.07", False), ("19-24 липня", False),
])
def test_mentions_nights_range(text, rng):
    assert bot_logic.mentions_nights_range(text) is rng


@pytest.mark.parametrize("text,committed", [
    ("На 5 ночей з 19", True),         # explicit single nights
    ("з 19 по 24 липня", True),        # з N по N
    ("24-26.07", True),                # date range (not adjacent to a nights word)
    ("17-19 липня", True),
    ("Днів 4-5", False),               # nights range -> NOT committed
    ("Бажано з 15.07", False),         # check-in only
    ("Ще є вільні місця у липні", False),
])
def test_has_committed_stay(text, committed):
    assert bot_logic.has_committed_stay(text) is committed


# --- Bug 1 (Sprint 5): real bed-config availability -------------------------------------------
def _raw_bed(sample):
    return {cat: {"total_available": ta, "rooms": {}} for cat, ta in sample.items()}


def test_bed_config_availability_from_raw():
    nights = ["2026-07-24", "2026-07-25"]
    # twin (2Л+Д) free both nights -> separate True; a double (Напівлюкс) free -> double True.
    raw = _raw_bed({
        "Стандарт 4х 2Л + Д": {"2026-07-24": 1, "2026-07-25": 2},
        "Стандарт": {"2026-07-24": 0, "2026-07-25": 0},
        "Стандарт +": {"2026-07-24": 0, "2026-07-25": 0},
        "Напівлюкс": {"2026-07-24": 1, "2026-07-25": 1},
        "Стандарт сімейний В+Д": {"2026-07-24": 0, "2026-07-25": 0},
        "Стандарт + Сімейний В+Д": {"2026-07-24": 0, "2026-07-25": 0},
    })
    assert bot_logic.bed_config_availability(raw, nights) == {"separate": True, "double": True}
    # a booked night in the ONLY twin room -> separate False; double still free.
    raw2 = _raw_bed({
        "Стандарт 4х 2Л + Д": {"2026-07-24": 1, "2026-07-25": 0},   # night 25 booked
        "Стандарт": {"2026-07-24": 0, "2026-07-25": 0},
        "Стандарт +": {"2026-07-24": 0, "2026-07-25": 0},
        "Стандарт сімейний В+Д": {"2026-07-24": 2, "2026-07-25": 2},
    })
    assert bot_logic.bed_config_availability(raw2, nights) == {"separate": False, "double": True}


def test_bed_availability_reply_variants():
    import dialogue_engine as de
    both = _raw_bed({"Стандарт 4х 2Л + Д": {"2026-07-24": 1, "2026-07-25": 1},
                     "Напівлюкс": {"2026-07-24": 1, "2026-07-25": 1}})
    r = de.bed_availability_reply("2026-07-24", "2026-07-26", both)
    assert "24 - 26 липня" in r and "роздільними" in r and "двоспальним" in r
    assert templates.BED_AVAIL_BOTH.split("{dates}")[0] in r     # the "both available" template
    none = _raw_bed({"Стандарт 4х 2Л + Д": {"2026-07-24": 0, "2026-07-25": 0}})
    r2 = de.bed_availability_reply("2026-07-24", "2026-07-26", none)
    assert "вже немає" in r2


def test_normalize_rooms_for_total_distributes_adults():
    # Persona 25: "2 номери для 4-х дорослих" — the extractor put 4 adults in EACH room (8 total).
    # Redistribute 4 adults across 2 rooms -> [2, 2], so no bogus 8-adult over-capacity split.
    rooms = [
        {"adults": 4, "children_ages": [], "checkin": "2026-07-24", "checkout": "2026-07-26", "nights": 2},
        {"adults": 4, "children_ages": [], "checkin": "2026-07-24", "checkout": "2026-07-26", "nights": 2},
    ]
    out = bot_logic.normalize_rooms_for_total(rooms, "Потрібно 2 номери для 4-х дорослих на 24-26.07.26")
    assert [r["adults"] for r in out] == [2, 2]
    assert all(r["checkin"] == "2026-07-24" and r["checkout"] == "2026-07-26" for r in out)


def test_normalize_rooms_for_total_noop_when_correct_or_absent():
    # Already correct (2 rooms, 2 adults each = 4) -> untouched.
    ok = [{"adults": 2, "children_ages": []}, {"adults": 2, "children_ages": []}]
    assert bot_logic.normalize_rooms_for_total(ok, "2 номери для 4 дорослих") == ok
    # No "N rooms for M people" phrase -> untouched.
    other = [{"adults": 4, "children_ages": []}]
    assert bot_logic.normalize_rooms_for_total(other, "давайте порахуйте") == other
    # A family request (children present) is never redistributed.
    fam = [{"adults": 2, "children_ages": [8]}, {"adults": 2, "children_ages": [8]}]
    assert bot_logic.normalize_rooms_for_total(fam, "2 номери для 4 дорослих") == fam


def test_normalize_rooms_for_total_generalized_separate_messages():
    # Bug 3 (Sprint 5): "2 номери Стандарт" + "Четверо дорослих" stated in SEPARATE client messages,
    # extractor drifted to 4 adults per room (8 total) -> redistribute to [2, 2] (number word "четверо").
    drift = [{"room_type": "Стандарт", "adults": 4, "children_ages": []},
             {"room_type": "Стандарт", "adults": 4, "children_ages": []}]
    out = bot_logic.normalize_rooms_for_total(drift, "Давайте 2 номери Стандарт\nЧетверо дорослих")
    assert [r["adults"] for r in out] == [2, 2]
    assert all(r["room_type"] == "Стандарт" for r in out)   # chosen type preserved
    # Also fixes a 1-room extraction of 4 adults when the client asked for 2 rooms.
    one = [{"adults": 4, "children_ages": []}]
    assert [r["adults"] for r in bot_logic.normalize_rooms_for_total(one, "два номери на чотирьох")] == [2, 2]


def test_normalize_rooms_for_total_respects_explicit_per_room_split():
    # An EXPLICIT per-room breakdown ("в одному 4, в іншому 3") must be respected, not evened out.
    split = [{"adults": 4, "children_ages": []}, {"adults": 3, "children_ages": []}]
    assert bot_logic.normalize_rooms_for_total(
        split, "Давайте 2 номери: в одному 4, в іншому 3") == split
    # A DATE "з 13 по 17 липня" must NOT be mistaken for a per-room split ("по 17"): still normalizes.
    drift = [{"adults": 4, "children_ages": []}, {"adults": 4, "children_ages": []}]
    out = bot_logic.normalize_rooms_for_total(drift, "2 номери для 4 дорослих з 13 по 17 липня")
    assert [r["adults"] for r in out] == [2, 2]


def test_collapse_leaves_real_multiroom_untouched():
    # A genuine multi-room booking must NOT be collapsed.
    same_dates_group = [   # identical over-capacity rooms on the SAME dates -> a real (big) ask
        {"adults": 6, "children_ages": [], "checkin": "2026-07-20", "checkout": "2026-07-22"},
        {"adults": 6, "children_ages": [], "checkin": "2026-07-20", "checkout": "2026-07-22"},
    ]
    assert len(bot_logic.collapse_duplicate_group_rooms(same_dates_group)) == 2
    two_families = [       # distinct compositions (Persona 23) -> untouched
        {"adults": 2, "children_ages": [8, 10], "checkin": "2026-07-20", "checkout": "2026-07-24"},
        {"adults": 2, "children_ages": [9], "checkin": "2026-07-20", "checkout": "2026-07-24"},
    ]
    assert len(bot_logic.collapse_duplicate_group_rooms(two_families)) == 2
    fits_one_room = [      # 2 rooms of 2 adults each (Persona 25) -> under capacity, untouched
        {"adults": 2, "children_ages": [], "checkin": "2026-07-24", "checkout": "2026-07-26"},
        {"adults": 2, "children_ages": [], "checkin": "2026-07-24", "checkout": "2026-07-26"},
    ]
    assert len(bot_logic.collapse_duplicate_group_rooms(fits_one_room)) == 2


@pytest.mark.parametrize("text,expected", [
    ("Так", True), ("так, давайте", True), ("Давайте", True), ("Добре, бронюємо", True),
    ("ок", True), ("Погоджуюсь", True),
    ("так, на 5-7 липня", False),    # has digits -> new info, not a bare yes
    ("Стандарт +", False),
    ("а скільки коштує?", False),
    ("", False),
])
def test_is_bare_confirmation(text, expected):
    assert bot_logic.is_bare_confirmation(text) is expected


def test_message_context_helpers():
    assert bot_logic.is_quote_message(
        "Вартість номеру типу Стандарт ... буде вартувати - 4400 грн. Бажаєте забронювати? 💙") is True
    assert bot_logic.is_window_offer_message(
        "Я перевірив календар ... маємо вільні віконця: 23 - 26 липня. Які дати?") is True
    assert bot_logic.is_quote_message("Підкажіть, будь ласка, дати") is False
    assert bot_logic.is_window_offer_message("Підкажіть, будь ласка, дати") is False


@pytest.mark.parametrize("text,expected", [
    ("Де саме знаходиться готель?", True),
    ("Де ви розташовані?", True),
    ("Яка ваша адреса?", True),
    ("Як до Вас добратися ?", False),     # directions -> HOW_TO_GET_THERE
    ("Як доїхати потягом?", False),
    ("Стандарт на 5-7 липня", False),
])
def test_is_location_question(text, expected):
    assert bot_logic.is_location_question(text) is expected


@pytest.mark.parametrize("text,expected", [
    ("а можна з собачкою?", "PETS"),
    ("+ собачка", "PETS"),
    ("а харчування у вас є?", "FOOD_PRICES"),
    ("Як до вас добратися?", "HOW_TO_GET_THERE"),
    ("Де саме знаходиться готель?", "PLACE"),
    ("чи є сауна або чани?", "SAUNA_VATS"),
    ("Стандарт на 5-7 липня для двох", None),
    ("2 дорослих", None),
])
def test_faq_override(text, expected):
    assert bot_logic.faq_override(text) == expected


@pytest.mark.parametrize("text,expected", [
    # Payment-rules questions -> BOOK_ROOM (deterministic; the LLM kept mis-routing these).
    ("Чи можна по приїзду оплатити повністю", "BOOK_ROOM"),   # the exact persona-2 failure
    ("чи можна без передоплати?", "BOOK_ROOM"),
    ("яка передоплата потрібна?", "BOOK_ROOM"),
    ("коли платити за бронювання?", "BOOK_ROOM"),
    ("можна оплатити на місці?", "BOOK_ROOM"),
    # NOT payment -> must stay out of BOOK_ROOM (check-in time / plain booking).
    ("о котрій заїзд і виїзд?", None),
    ("приїзд у п'ятницю, виїзд у неділю", None),
    ("Стандарт на 5-7 липня для двох", None),
])
def test_faq_override_payment_rules(text, expected):
    assert bot_logic.faq_override(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("У вас у кожному номері є фен?", "HAIRDRYER"),
    ("чи є фен?", "HAIRDRYER"),
    ("Чи є фото/відео номерів?", "MEDIA"),
    ("можна побачити світлини території?", "MEDIA"),
    ("Де саме знаходиться готель?", "PLACE"),
    ("Де знаходиться готель?", "PLACE"),
    ("де ваш готель розташований?", "PLACE"),
])
def test_faq_override_hairdryer_media_location(text, expected):
    assert bot_logic.faq_override(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("приїдемо з собачкою", True), ("у нас пекінес", True), ("буде кіт", False),
    ("маленький песик", True), ("наш улюбленець", True),
    ("Стандарт на 5-7 липня", False), ("двоє дорослих", False),
])
def test_mentions_pet(text, expected):
    assert bot_logic.mentions_pet(text) is expected


@pytest.mark.parametrize("text,expected", [
    ("Дякую", True), ("Дякую вам!", True), ("Спасибі велике", True),
    ("До побачення", True), ("дякую, до зустрічі", True),
    ("дякую, бронюю", False),         # confirm -> proceed to booking, not a close
    ("дякую, а яка ціна?", False),    # has a real question (4 words but contains 'ціна'? still <=4)
    ("Дякую за інформацію про номери будь ласка", False),  # too long
    ("5-7 липня", False),
])
def test_is_pure_thanks(text, expected):
    assert bot_logic.is_pure_thanks(text) is expected


@pytest.mark.parametrize("text,expected", [
    # Bug 2 (§9.11): price / swimming / day-visit WITHOUT staying -> GUEST_POOL.
    ("яка ціна покупались у басейні", "GUEST_POOL"),       # the exact live-QA failure
    ("скільки коштує басейн?", "GUEST_POOL"),
    ("можна просто поплавати в басейні?", "GUEST_POOL"),
    ("хочемо приїхати на басейн на день", "GUEST_POOL"),
    ("вартість відвідування басейну без проживання", "GUEST_POOL"),
    ("тільки басейн, не проживаємо", "GUEST_POOL"),
    # General amenity questions -> POOL.
    ("а у вас є басейн?", "POOL"),
    ("басейн з підігрівом?", "POOL"),
    ("до котрої працює басейн?", "POOL"),
    ("чи входить басейн у вартість проживання?", "POOL"),  # "входить" -> amenity, not price
])
def test_faq_override_pool_vs_guest_pool(text, expected):
    # The deterministic guard (not just the LLM) must split POOL/GUEST_POOL, because it
    # OVERRIDES the extractor's faq_template for any "басейн" message.
    assert bot_logic.faq_override(text) == expected


@pytest.mark.parametrize("text,expected", [
    # Owner fix #278: a CHILDREN'S-pool question routes to the dedicated CHILDREN_POOL answer.
    ("Чи є у вас дитячий басейн?", "CHILDREN_POOL"),
    ("а дитячий басейн є?", "CHILDREN_POOL"),
    ("розкажіть про басейн для дітей", "CHILDREN_POOL"),
    ("який розмір дитячого басейну?", "CHILDREN_POOL"),
    # a general/adult pool question stays POOL, a price question stays GUEST_POOL.
    ("а у вас є басейн?", "POOL"),
    ("скільки коштує басейн?", "GUEST_POOL"),
])
def test_faq_override_children_pool(text, expected):
    assert bot_logic.faq_override(text) == expected


def test_children_pool_template_content():
    assert "дитячий басейн" in templates.CHILDREN_POOL
    assert "3х2" in templates.CHILDREN_POOL and "30 см" in templates.CHILDREN_POOL
    assert "28" in templates.CHILDREN_POOL


@pytest.mark.parametrize("text,expected", [
    # Persona 17: own food/drinks -> NO (OUTSIDE_FOOD); sitting w/o swimming -> same price.
    ("Чи можна до вас приходити зі своїм.", "OUTSIDE_FOOD"),
    ("а можна зі своєю їжею?", "OUTSIDE_FOOD"),
    ("Яка буде вартість якщо не будем плавати а просто посидіти", "POOL_ENTRY_SAME_PRICE"),
    ("а якщо просто посидіти, не купатись?", "POOL_ENTRY_SAME_PRICE"),
    # Persona 20: distance to Bukovel.
    ("На скільки далеко Ви від Буковелю?", "DISTANCE_BUKOVEL"),
    ("скільки км до Буковеля?", "DISTANCE_BUKOVEL"),
    # own-dog must NOT be read as own-food.
    ("можна приїхати зі своїм песиком?", "PETS"),
])
def test_faq_override_persona_17_20(text, expected):
    assert bot_logic.faq_override(text) == expected


@pytest.mark.parametrize("text,expected", [
    # Owner 2026-07-10: WHICH dishes -> FOOD_MENU (not the price list).
    ("Які там страви подають?", "FOOD_MENU"),
    ("яке у вас меню?", "FOOD_MENU"),
    ("що готують на кухні?", "FOOD_MENU"),
    # price questions still go to FOOD_PRICES
    ("скільки коштує харчування?", "FOOD_PRICES"),
    ("а харчування у вас є?", "FOOD_PRICES"),
])
def test_faq_override_food_menu(text, expected):
    assert bot_logic.faq_override(text) == expected


def test_parse_meal_request_owner_phrasing():
    import dialogue_engine as de
    m = bot_logic.parse_meal_request(
        "Порахуйте харчування 3-разове на 4 особи: 2 дні повне харчування, а останній день лише сніданок")
    assert m["persons"] == 4 and m["three_meals_days"] == 2 and m["breakfast_days"] == 1
    assert m["two_meals_days"] == 0
    # end-to-end via finalize_meals in August -> 10200
    assert "10200" in de.finalize_meals(m, "Серпень", default_persons=4)
    # a plain price FAQ (no days) is NOT a calc request
    assert bot_logic.parse_meal_request("а харчування у вас є?") is None
    assert bot_logic.parse_meal_request("скільки коштує сніданок?") is None


def test_e2e_meal_cost_deterministic_fallback(server):
    # Even if the LLM leaves the meals slot EMPTY, the deterministic parser computes the cost.
    bs = server.configure(
        slots={"topic": "faq", "faq_template": "FOOD_PRICES", "rooms": [
            {"room_type": "Стандарт", "checkin": "2026-08-06", "checkout": "2026-08-09",
             "adults": 2, "children_ages": []}]},   # NOTE: no "meals" slot from the LLM
        history=_bot_spoke())
    _run(bs.process_incoming_message(
        "Порахуйте харчування 3-разове на 4 особи: 2 дні повне харчування, а останній день лише сніданок", 613))
    assert any("10200 грн" in m for m in server.sent)


def test_food_menu_template_and_accepts_split():
    assert "узгоджуються по заїзду" in templates.FOOD_MENU
    assert bot_logic.accepts_split("так, порахуйте будь ласка") is True
    assert bot_logic.accepts_split("давайте") is True
    assert bot_logic.accepts_split("Давайте 2 номери: 4 і 3") is False   # counter-proposal (digits)
    assert bot_logic.accepts_split("яка ціна?") is False


def test_past_dates_template_content():
    assert "минули" in templates.PAST_DATES and "актуальні дати" in templates.PAST_DATES


def test_persona_17_20_templates_content():
    assert "колиба" in templates.OUTSIDE_FOOD and "не можна" in templates.OUTSIDE_FOOD
    assert "такою ж" in templates.POOL_ENTRY_SAME_PRICE and "вхід" in templates.POOL_ENTRY_SAME_PRICE
    assert "Booking.com" in templates.BOOKING_COM
    assert "35 км" in templates.DISTANCE_BUKOVEL and "Буковел" in templates.DISTANCE_BUKOVEL


@pytest.mark.parametrize("text,expected", [
    ("Я забронювала номер на booking, потрібно кинути передоплату", True),
    ("на букінг мені підтвердили бронь", True),
    ("Стандарт на 5-7 липня", False),
    ("яка передоплата потрібна?", False),
])
def test_is_booking_com_question(text, expected):
    assert bot_logic.is_booking_com_question(text) is expected


@pytest.mark.parametrize("text,expected", [
    # New DISCOUNTS FAQ: a GENERAL discount question was falling through to a room description.
    ("Є якісь знижки у вас?", "DISCOUNTS"),
    ("а які у вас знижки?", "DISCOUNTS"),
    ("чи є знижки?", "DISCOUNTS"),
    ("є акції?", "DISCOUNTS"),
    ("маєте промокод?", "DISCOUNTS"),
    # military-specific discount question keeps the precise MILITARY answer.
    ("чи надаєте знижку військовим?", "MILITARY"),
    ("є знижка для УБД?", "MILITARY"),
    # a booking that merely mentions a discount must NOT be hijacked into an FAQ.
    ("порахуйте зі знижкою УБД на 5-7 липня", None),
    ("Стандарт на 5-7 липня для двох", None),
    ("2 дорослих", None),
])
def test_faq_override_discounts(text, expected):
    assert bot_logic.faq_override(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("Є якісь знижки у вас?", True), ("які у вас акції?", True), ("маєте промокод?", True),
    ("порахуйте зі знижкою УБД", False),   # passing mention in a booking, not a question
    ("Стандарт на 5-7 липня", False),
])
def test_is_discount_question(text, expected):
    assert bot_logic.is_discount_question(text) is expected


def test_pending_faq_sequence_collects_burst_faqs():
    # Fix 3: every FAQ in the unanswered burst (since the bot's last reply) + the current
    # message, in order, de-duplicated.
    hist = [
        {"id": 1, "message_type": "outgoing", "content": "вітаю"},
        {"id": 2, "message_type": "incoming", "content": "Де ви знаходитесь?"},
        {"id": 3, "message_type": "incoming", "content": "а басейн з підігрівом?"},
    ]
    assert bot_logic.pending_faq_sequence(hist, "а харчування є?") == ["PLACE", "POOL", "FOOD_PRICES"]
    # Stops at the bot's last reply -> anything before it is already answered.
    hist2 = [
        {"id": 1, "message_type": "incoming", "content": "де ви?"},
        {"id": 2, "message_type": "outgoing", "content": "ось адреса"},
        {"id": 3, "message_type": "incoming", "content": "а фен є?"},
    ]
    assert bot_logic.pending_faq_sequence(hist2, "а фен є?") == ["HAIRDRYER"]
    # De-dupes and ignores non-FAQ chatter.
    assert bot_logic.pending_faq_sequence([], "а басейн є? і ще раз про басейн?") == ["POOL"]
    assert bot_logic.pending_faq_sequence([], "Стандарт на 5-7 липня") == []


def test_pending_faq_sequence_skips_notice_messages():
    # Live-QA race: a mid-burst FAQ ("харчування є?") that landed just BEFORE the bot's
    # greeting / "Секундочку" notice (a slow scrape) must NOT be treated as answered — a
    # notice is not an answer. The scan skips notices and still collects the food FAQ.
    seku = "Секундочку, перевіряю доступність номеру та актуальні ціни на ці дати… 🗓️"
    hist = [
        {"id": 1, "message_type": "incoming", "content": "Четверо дорослих"},
        {"id": 2, "message_type": "incoming", "content": "харчування є?"},
        {"id": 3, "message_type": "outgoing", "content": bot_logic.GREETING},
        {"id": 4, "message_type": "outgoing", "content": seku},
    ]
    assert bot_logic.pending_faq_sequence(hist, "Стандарт +") == ["FOOD_PRICES"]
    # But a REAL bot answer still bounds the scan (already-answered FAQs aren't re-collected).
    hist2 = [
        {"id": 1, "message_type": "incoming", "content": "де ви?"},
        {"id": 2, "message_type": "outgoing", "content": templates.PLACE},   # a real answer
        {"id": 3, "message_type": "incoming", "content": "а фен є?"},
    ]
    assert bot_logic.pending_faq_sequence(hist2, "а фен є?") == ["HAIRDRYER"]


@pytest.mark.parametrize("text,expected", [
    (templates.QUESTION_MISSING_AGE, True),
    (templates.QUESTION_MISSING_DATES_1_CHILD, True),
    (templates.QUESTION_MISSING_DATES_CHILDREN, True),
    ("Орієнтуємось на кінець серпня! підкажіть лише вік діток 😊", True),
    (bot_logic.GREETING, False),
    ("Вартість номеру ... буде вартувати - 4400 грн", False),
])
def test_asks_for_child_ages(text, expected):
    assert bot_logic.asks_for_child_ages(text) is expected


@pytest.mark.parametrize("text,expected", [
    # Rule 4: a monthly pricing-policy question must be answered (was swallowed in Persona 9).
    ("В серпні актуальна, як і на липень, цінова політика?", "PRICE_POLICY"),
    ("ціни однакові по місяцях?", "PRICE_POLICY"),
    ("в серпні ціни такі самі як у липні?", "PRICE_POLICY"),
    # not a policy question -> stays out of PRICE_POLICY
    ("яка вартість на 5-7 липня?", None),
    ("Стандарт на 5-7 липня", None),
])
def test_faq_override_price_policy(text, expected):
    assert bot_logic.faq_override(text) == expected


@pytest.mark.parametrize("text,expected", [
    # Rule 4: a specific pet-FEE question gets the concise PET_SURCHARGE (not the verbose PETS,
    # which the anti-dedup would swallow if already shown).
    ("Треба за собаку доплачувати?", "PET_SURCHARGE"),
    ("чи є доплата за тваринку?", "PET_SURCHARGE"),
    ("скільки доплата за песика?", "PET_SURCHARGE"),
    # a general pet mention stays PETS
    ("а можна з собачкою?", "PETS"),
])
def test_faq_override_pet_surcharge(text, expected):
    assert bot_logic.faq_override(text) == expected


def test_pet_surcharge_not_triggered_by_booking_price():
    # A booking that merely mentions a pet + a ROOM price must NOT be read as a pet-FEE question
    # (no surcharge word) -> the surcharge detector stays False.
    assert bot_logic.is_pet_surcharge_question("з собакою, яка вартість Стандарт 5-7 липня?") is False
    assert bot_logic.is_pet_surcharge_question("Треба за собаку доплачувати?") is True


def test_pet_surcharge_template_content():
    assert "300 грн" in templates.PET_SURCHARGE and "тваринк" in templates.PET_SURCHARGE


def test_price_policy_template_content():
    assert "залежать від місяця" in templates.PRICE_POLICY
    assert "серпн" in templates.PRICE_POLICY.lower()


def test_e2e_price_policy_question_answered(server):
    # Rule 4: the exact Persona-9 message must be ANSWERED, not swallowed by fuzzy-date logic.
    bs = server.configure(
        slots={"topic": "fuzzy_dates", "rooms": [
            {"room_type": None, "fuzzy_date": "друга половина липня", "adults": 2, "children_ages": []}]},
        history=_bot_spoke())
    _run(bs.process_incoming_message("В серпні актуальна, як і на липень, цінова політика?", 620))
    assert any("залежать від місяця" in m for m in server.sent)   # PRICE_POLICY answered


def test_insist_child_ages_template_content():
    assert "вік діт" in templates.INSIST_CHILD_AGES.lower()
    assert "розрахувати" in templates.INSIST_CHILD_AGES


def test_discounts_template_content():
    assert "знижки для дітей" in templates.DISCOUNTS
    assert "військовослужбовців" in templates.DISCOUNTS
    assert "Instagram" in templates.DISCOUNTS


def test_new_templates_content():
    assert "0673445220" in templates.LARGE_GROUPS_EVENTS
    assert "350" in templates.FOOD_PRICES and "1100" in templates.FOOD_PRICES
    assert "instagram.com/stories/highlights" in templates.PETS
    assert "WiFi" in templates.ROOM_AMENITIES
    assert "Стандарт +" in templates.SMOKING
    assert "Ворохта" in templates.HOW_TO_GET_THERE


def test_owner_2026_06_23_templates():
    # Owner's finalized business answers reflected in the templates.
    assert "14:00" in templates.CHECK_IN_OUT and "12:00" in templates.CHECK_IN_OUT
    assert "300 грн" in templates.PETS and "котик" in templates.PETS.lower()
    assert "узгоджується" in templates.OFF_SEASON
    assert "номер телефону" not in templates.OFF_SEASON      # no longer asks for a phone
    assert "розселити" in templates.ASK_ROOM_DISTRIBUTION
    assert "20%" in templates.MILITARY and "копію" in templates.MILITARY
    assert "Парковка" in templates.TRANSFER_PARKING
    assert "грудня" in templates.SAUNA_VATS
    assert "72 годин" in templates.BOOK_ROOM
    assert "дитяче місце" in templates.CHILDREN and "Від 12" in templates.CHILDREN
    assert "ліжечок" in templates.CHILDREN_AMENITIES
    assert "фіскальний чек" in templates.DOCUMENTS


def test_owner_2026_06_24_templates():
    assert "фен" in templates.HAIRDRYER
    assert "сторіс" in templates.MEDIA or "хайлайтс" in templates.MEDIA
    assert "Дякуємо" in templates.ACKNOWLEDGE_THANKS
    assert "максимум 3" in templates.PRESENTATION_ROOMS        # explicit capacity (Fix 1)
    assert "Температура" in templates.POOL                     # typo fixed (Fix 6)
    assert "зацікавлять інші дати" not in templates.NEAREST_NONE  # no permission ask (Fix 5)
    # GENERAL_INFORMATION must NOT leak internal room names (Fix 2 — the "hallucination").
    for leak in ("Хом", "Боярин", "Гропа", "Баба Людова", "11 номер"):
        assert leak not in templates.GENERAL_INFORMATION


# -- payment hand-off & bot muting ------------------------------------------

@pytest.mark.parametrize("text,attach,expected", [
    ("Оплатив! Ось квитанція", False, True),
    ("скинув гроші на картку", False, True),
    ("готово", False, True),
    ("оплата зроблена, чек нижче", False, True),
    ("", True, True),                              # image / screenshot, no text
    ("Доброго дня, є вільні номери?", False, False),
    ("Стандарт на 5-7 липня для двох", False, False),
])
def test_is_payment_intent(text, attach, expected):
    assert bot_logic.is_payment_intent(text, attach) is expected


def test_is_muted():
    assert bot_logic.is_muted([bot_logic.ORDER_LABEL]) is True
    assert bot_logic.is_muted(["VIP", "Замовлено"]) is True
    assert bot_logic.is_muted(["VIP"]) is False
    assert bot_logic.is_muted([]) is False
    assert bot_logic.is_muted(None) is False


def test_payment_handoff_template():
    # Owner 2026-07-09: concise handoff — the bot never verifies payment itself.
    t = templates.PAYMENT_RECEIVED_HANDOFF
    assert "менеджер" in t and "перевірить оплату" in t and "підтвердження" in t


# ===========================================================================
# Layer B — live flow (skips unless FastAPI/google-genai available)
# ===========================================================================

@pytest.fixture
def server(monkeypatch):
    try:
        import bot_server
    except Exception as exc:
        pytest.skip(f"bot_server unavailable here (needs container deps): {exc}")

    sent = []           # messages sent to Chatwoot
    prompts = []        # prompts handed to the (faked) extraction LLM
    added_labels = []   # labels added to the conversation
    state = {"scraped": False, "labels": []}

    monkeypatch.setattr(bot_server, "send_chatwoot_message",
                        lambda conv_id, text: sent.append(text))
    monkeypatch.setattr(bot_server, "get_conversation_labels", lambda cid: state["labels"])
    monkeypatch.setattr(bot_server, "add_conversation_label",
                        lambda cid, label: added_labels.append(label))

    async def _fast_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(bot_server.asyncio, "sleep", _fast_sleep)
    bot_server.AVAILABILITY_CACHE.clear()
    bot_server._conv_locks.clear()   # fresh per-conversation locks per test/event-loop
    bot_server._conv_seq.clear()
    bot_server._slot_memory.clear()  # fresh slot memory per test
    bot_server._greeted.clear()      # fresh greeting state per test
    bot_server._pending_window.clear()  # fresh pending-window state per test
    bot_server._no_dates_mode.clear()   # fresh no-dates mode per test
    bot_server._cooldowns.clear()       # fresh 503-cooldown state per test
    bot_server._pending_split.clear()   # fresh pending-split state per test
    bot_server._meals_memory.clear()    # fresh meals memory per test

    def configure(slots=None, slots_text=None, history=None, availability=None,
                  labels=None, dynamic_history=False):
        state["labels"] = labels or []
        text = slots_text if slots_text is not None else json.dumps(
            slots or {"topic": "greeting", "rooms": []})

        async def fake_llm(prompt, *a, **k):
            prompts.append(prompt)
            return SimpleNamespace(text=text)
        monkeypatch.setattr(bot_server, "generate_with_retry", fake_llm)
        if dynamic_history:
            # mimic real Chatwoot: history reflects what the bot has already sent
            monkeypatch.setattr(bot_server, "get_chatwoot_history", lambda conv_id: [
                {"id": i, "message_type": "outgoing", "content": m} for i, m in enumerate(sent)])
        else:
            monkeypatch.setattr(bot_server, "get_chatwoot_history", lambda conv_id: history or [])

        async def fake_fetch():
            state["scraped"] = True
            return availability
        monkeypatch.setattr(bot_server, "fetch_hotel_availability", fake_fetch)
        return bot_server

    return SimpleNamespace(configure=configure, sent=sent, prompts=prompts,
                           added_labels=added_labels, state=state)


def _run(coro):
    return asyncio.run(coro)


def _bot_spoke():
    return [{"id": 1, "message_type": "outgoing", "content": "вітаю"}]


def _raw(avail_simplified):
    """Wrap {room_type:{date:count}} into the scraper's raw shape."""
    return {rt: {"total_available": d, "rooms": {}} for rt, d in avail_simplified.items()}


# -- first contact: greeting + ask, no scrape -------------------------------

def test_e2e_first_contact(server):
    bs = server.configure(slots={"topic": "greeting", "rooms": []}, history=[])
    _run(bs.process_incoming_message("Доброго дня, хочу до вас!", 301))
    assert server.state["scraped"] is False
    assert server.sent[0].startswith("Доброго дня! Вас вітає D&T Hotel")
    assert server.sent[-1] == templates.QUESTION_ALL_MISSING


# -- price quote: scrape, then a DETERMINISTIC total (the July-5 fix) --------

def test_e2e_price_quote_deterministic_total(server):
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
             "adults": 2, "children_ages": []}]},
        history=_bot_spoke(),
        availability=_raw({"Стандарт": {"2026-07-05": 3, "2026-07-06": 3}}),
    )
    _run(bs.process_incoming_message("Давайте Стандарт на 5-7 липня", 302))
    assert server.state["scraped"] is True
    assert any("Секундочку" in m for m in server.sent)        # availability checked first
    assert any("4400 грн" in m for m in server.sent)          # 2200*2 будні, NOT 2500
    assert not any("2500" in m for m in server.sent)


# -- availability gating: sold out -> Polite Close, no price ----------------

def test_e2e_sold_out_offers_nearest_dates(server):
    # Fully booked -> offer nearest dates (Case 5), NOT instant close, NOT a price.
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
             "adults": 2, "children_ages": []}]},
        history=_bot_spoke(),
        availability=_raw({"Стандарт": {"2026-07-05": 0, "2026-07-06": 0,
                                         "2026-07-08": 2, "2026-07-09": 2}}),
    )
    _run(bs.process_incoming_message("Стандарт на 5-7 липня", 303))
    assert any("всі номери заброньовані" in m and "8 - 10 липня" in m for m in server.sent)  # SOLD_OUT_FOUND_NEAREST
    assert not any(templates.POLITE_CLOSE == m for m in server.sent)
    assert not any("грн" in m for m in server.sent)           # never quoted a price


def test_e2e_greeting_then_wait_then_result_order(server):
    # Fix 1: first turn order must be Greeting -> "Секундочку…" -> result.
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-07",
             "adults": 2, "children_ages": []}]},
        history=[],  # first turn
        availability=_raw({"Стандарт": {"2026-07-06": 3}}),
    )
    _run(bs.process_incoming_message("Стандарт на 6-7 липня для двох", 312))
    assert server.sent[0].startswith("Доброго дня! Вас вітає D&T Hotel")
    assert "Секундочку" in server.sent[1]
    assert "грн" in server.sent[2]


# -- multi-room: per-room lines + Загальна вартість -------------------------

def test_e2e_multiroom_total(server):
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
             "adults": 2, "children_ages": []},
            {"room_type": "Напівлюкс", "checkin": "2026-07-05", "checkout": "2026-07-07",
             "adults": 2, "children_ages": [8]}]},
        history=_bot_spoke(),
        availability=_raw({"Стандарт": {"2026-07-05": 3, "2026-07-06": 3},
                           "Напівлюкс": {"2026-07-05": 1, "2026-07-06": 1}}),
    )
    _run(bs.process_incoming_message("Хочемо два номери", 304))
    full = "\n".join(server.sent)
    assert "4400 грн" in full and "6000 грн" in full and "Загальна вартість: 10400 грн" in full


# -- topic redirects (group/event, faq) -------------------------------------

def test_e2e_group_event_redirect(server):
    bs = server.configure(slots={"topic": "group_event", "rooms": []}, history=_bot_spoke())
    _run(bs.process_incoming_message("нас 45, корпоратив", 305))
    assert server.state["scraped"] is False
    assert server.sent == [templates.LARGE_GROUPS_EVENTS]


def test_e2e_large_group_by_slot_count_redirects(server):
    # 20+ total guests split across fields ("10 + 12") -> the text regex wouldn't catch
    # it, but the consolidated slot count does -> redirect to the co-owner, no scrape.
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": None, "checkin": "2026-07-10", "checkout": "2026-07-12",
             "adults": 10, "children_count": 12, "children_ages": []}]},
        history=_bot_spoke())
    _run(bs.process_incoming_message("нас 10 дорослих і 12 дітей на 10-12 липня", 360))
    assert server.sent == [templates.LARGE_GROUPS_EVENTS]
    assert server.state["scraped"] is False


def test_e2e_large_group_override_beats_llm(server):
    # Even when the extractor mislabels the last message, a 70-80 person inquiry
    # earlier in the thread is deterministically redirected.
    bs = server.configure(
        slots={"topic": "fuzzy_dates", "rooms": []},
        history=[{"id": 1, "message_type": "incoming", "content": "Липень на 70-80 чол на 6 днів"},
                 {"id": 2, "message_type": "outgoing", "content": "вітаю"}])
    _run(bs.process_incoming_message("Ще актуальні ці дати?", 311))
    assert server.sent == [templates.LARGE_GROUPS_EVENTS]


def test_e2e_faq_routes_to_template(server):
    bs = server.configure(slots={"topic": "faq", "faq_template": "PETS", "rooms": []},
                          history=_bot_spoke())
    _run(bs.process_incoming_message("а з песиком можна?", 306))
    assert len(server.sent) == 1
    assert server.sent[0].startswith(templates.PETS)
    assert templates.FAQ_DATE_NUDGE in server.sent[0]   # FAQ answered + gentle date nudge


def test_e2e_location_question_pinned_to_place(server):
    # Even if the extractor mislabels it GENERAL_INFORMATION, "де знаходиться" -> PLACE.
    bs = server.configure(
        slots={"topic": "faq", "faq_template": "GENERAL_INFORMATION", "rooms": []},
        history=_bot_spoke())
    _run(bs.process_incoming_message("Де саме знаходиться готель?", 501))
    assert len(server.sent) == 1 and server.sent[0].startswith(templates.PLACE)


def test_e2e_faq_midbooking_preserves_state(server):
    # §9.11 Bug 1: user is mid-booking (room+dates+guests known) and asks an FAQ. The bot
    # answers WITHOUT wiping state AND — because the booking intent is ACTIONABLE (plan ->
    # quote) — runs the scan and appends the REAL quote, never the generic nudge.
    bs = server.configure(
        slots={"topic": "faq", "faq_template": "HOW_TO_GET_THERE", "rooms": [
            {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-08",
             "adults": 2, "children_ages": []}]},
        history=_bot_spoke(),
        availability=_raw({"Стандарт": {"2026-07-06": 3, "2026-07-07": 3}}))
    _run(bs.process_incoming_message("а як до вас добратися?", 351))
    full = "\n".join(server.sent)
    assert templates.HOW_TO_GET_THERE in full                 # FAQ answered
    assert "буде вартувати" in full                           # + the REAL quote (scan ran)
    assert templates.FAQ_CONTINUE_NUDGE.strip() not in full   # nudge replaced by real result
    assert templates.QUESTION_ALL_MISSING not in full         # never re-asks from scratch
    assert server.state["scraped"] is True                    # actionable FAQ executes the scan


def test_e2e_faq_with_fuzzy_intent_runs_proactive_scan(server):
    # §9.11 Bug 1 (mission Dialogue 1): one message carries BOTH an FAQ and an actionable
    # fuzzy booking intent ("липень, 4 особи, чи є трансфер?"). The bot answers the FAQ AND
    # runs the proactive scan, proposing REAL windows — never the generic nudge, so a later
    # "Так" has windows to accept (the scan actually happened).
    bs = server.configure(
        slots={"topic": "faq", "faq_template": "TRANSFER_PARKING", "rooms": [
            {"room_type": None, "fuzzy_date": "липень", "nights": None,
             "checkin": None, "checkout": None, "adults": 4, "children_ages": []}]},
        history=[],   # first turn -> cold cache, must scrape
        availability=_raw({"Стандарт": {f"2026-07-{d:02d}": 2 for d in range(10, 20)}}))
    _run(bs.process_incoming_message("липень, 4 особи, чи є трансфер?", 355))
    assert server.sent[0].startswith("Доброго дня! Вас вітає D&T Hotel")  # greeting first
    full = "\n".join(server.sent)
    assert templates.TRANSFER_PARKING in full                 # FAQ answered
    assert "вільні віконця" in full                           # proactive scan ran -> real windows
    assert templates.FAQ_CONTINUE_NUDGE.strip() not in full   # NOT the generic nudge
    assert server.state["scraped"] is True
    assert 355 in bs._pending_window                          # offered window stored for a later "Так"


def test_e2e_discounts_faq_answered_not_room_description(server):
    # PHASE-2 fix: "Є якісь знижки?" was answered with a room description. Now the
    # deterministic guard pins it to DISCOUNTS even if the extractor mislabels it.
    bs = server.configure(
        slots={"topic": "faq", "faq_template": "GENERAL_INFORMATION", "rooms": []},
        history=_bot_spoke())
    _run(bs.process_incoming_message("Є якісь знижки у вас?", 520))
    joined = "\n".join(server.sent)
    assert "знижки для дітей" in joined and "військовослужбовців" in joined   # DISCOUNTS
    assert "У нас 3 типи номерів" not in joined                                # NOT the room description


def test_e2e_burst_multiple_faqs_then_quote_ordered(server):
    # PHASE-2 Fix 3: a drip burst asked TWO FAQs (location, pool) then a full booking. The
    # bot must answer BOTH FAQs (in order) AND the quote — Greeting -> FAQs -> Booking -> CTA.
    hist = [{"id": 1, "message_type": "incoming", "content": "Де ви знаходитесь?"},
            {"id": 2, "message_type": "incoming", "content": "а басейн з підігрівом?"}]
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-07",
             "adults": 2, "children_ages": []}]},
        history=hist, availability=_raw({"Стандарт": {"2026-07-06": 3}}))
    _run(bs.process_incoming_message("Стандарт на 6-7 липня для двох", 530))
    assert server.sent[0].startswith("Доброго дня! Вас вітає")     # greeting first
    i_place = next(i for i, m in enumerate(server.sent) if "серці Карпат" in m)
    i_pool = next(i for i, m in enumerate(server.sent) if "працює щодня" in m)
    i_quote = next(i for i, m in enumerate(server.sent) if "буде вартувати" in m)
    assert i_place < i_quote and i_pool < i_quote                  # both FAQs answered BEFORE the quote
    assert "буде вартувати" in "\n".join(server.sent)              # the quote (CTA) is produced


def test_e2e_compound_fuzzy_period_includes_second_month(server):
    # PHASE-2 Fix 2 (date horizon): "друга половина липня або після 6 серпня" with late July
    # booked must still scan AUGUST and offer that window (never drop the 2nd month).
    avail = _raw({"Стандарт": {
        **{f"2026-07-{d:02d}": 0 for d in range(16, 32)},     # late July booked
        **{f"2026-08-{d:02d}": 2 for d in range(6, 14)}}})    # early August free
    bs = server.configure(
        slots={"topic": "fuzzy_dates", "rooms": [
            {"room_type": None, "fuzzy_date": "друга половина липня або після 6 серпня",
             "nights": None, "checkin": None, "checkout": None, "adults": 2, "children_ages": []}]},
        history=_bot_spoke(), availability=avail)
    _run(bs.process_incoming_message("друга половина липня або після 6 серпня, на двох", 540))
    full = "\n".join(server.sent)
    assert "вільні віконця" in full and "серпня" in full          # August window offered


def test_extraction_prompt_carries_guest_pool_rule(server):
    # Bug 2: the extractor prompt must explicitly teach GUEST_POOL vs POOL.
    import bot_server
    assert "GUEST_POOL:" in bot_server.EXTRACTION_PROMPT
    assert "POOL: ЗАГАЛЬНЕ" in bot_server.EXTRACTION_PROMPT


def test_e2e_slot_memory_restores_dropped_guests(server):
    # Turn 1 the extractor captures 3 adults. Turn 2 (a pet FAQ) the extractor DROPS the
    # guests (LLM variance). Slot memory must restore them so the bot asks only for the
    # missing dates — never the all-missing monolith, never re-asking guests.
    bs = server.configure(
        slots={"topic": "general_price", "rooms": [
            {"room_type": None, "checkin": None, "checkout": None,
             "adults": 3, "children_count": 0, "children_ages": []}]},
        history=_bot_spoke())
    _run(bs.process_incoming_message("Яка ціна за трьох?", 370))
    server.sent.clear()
    server.configure(slots={"topic": "faq", "faq_template": "PETS", "rooms": []},
                     history=_bot_spoke())
    _run(bs.process_incoming_message("+ собачка", 370))
    joined = "\n".join(server.sent)
    assert templates.PETS.split("\n")[0] in joined          # answered the pet FAQ
    assert templates.QUESTION_ALL_MISSING not in joined     # never re-asks from scratch
    assert templates.QUESTION_ONLY_DATES in joined          # asks ONLY the missing dates


def test_e2e_superseded_scrape_suppressed_but_cached(server):
    # Bug 1 (revised): a scrape superseded mid-flight is SUPPRESSED (so a newer message
    # — a date correction — wins; no stale/double quote), BUT it still populated the
    # cache so the next/latest turn can deliver the result without a re-scrape.
    bs = server.configure(
        slots={"topic": "general_price", "rooms": [
            {"room_type": None, "fuzzy_date": "друга половина липня", "nights": 3,
             "adults": 2, "children_ages": []}]},
        history=_bot_spoke(),
        availability=_raw({"Стандарт": {f"2026-07-{d:02d}": 2 for d in range(20, 28)}}))
    bs._conv_seq[902] = 5   # a newer message arrived while this one scraped
    _run(bs._handle_incoming("друга половина липня", 902, seq=1))
    assert server.state["scraped"] is True
    assert not any("вільні віконця" in m for m in server.sent)   # superseded -> suppressed
    assert bs.peek_cached_availability(902) is not None          # cache populated for next turn


def test_e2e_cheap_reply_suppressed_when_superseded(server):
    # A CHEAP reply IS superseded by a newer drip, so a burst collapses to one reply.
    bs = server.configure(slots={"topic": "greeting", "rooms": []}, history=_bot_spoke())
    bs._conv_seq[903] = 5
    _run(bs._handle_incoming("привіт", 903, seq=1))
    assert server.sent == []


def test_e2e_faq_during_scrape_combines_with_cached_booking(server):
    # Bug 1: an FAQ asked while a scrape was in flight must not drop the booking answer.
    # The (prior/superseded) scrape populated the cache -> the FAQ turn answers the FAQ
    # AND delivers the pending calendar result FROM CACHE (no new scrape).
    import time as _t
    bs = server.configure(
        slots={"topic": "faq", "faq_template": "FOOD_PRICES", "rooms": [
            {"room_type": None, "fuzzy_date": "друга половина липня", "nights": 3,
             "adults": 2, "children_ages": []}]},
        history=_bot_spoke())
    bs.AVAILABILITY_CACHE[905] = (
        _raw({"Стандарт": {f"2026-07-{d:02d}": 2 for d in range(20, 28)}}), _t.time())
    _run(bs.process_incoming_message("а харчування є?", 905))
    joined = "\n".join(server.sent)
    assert templates.FOOD_PRICES.split("\n")[0] in joined       # FAQ answered
    assert "вільні віконця" in joined                           # + calendar result from cache
    assert server.state["scraped"] is False                     # used cache, no new scrape


def test_e2e_greeting_idempotent_across_history_lag(server):
    # An FAQ interrupting the first scrape must not double-greet even if Chatwoot's
    # history hasn't yet recorded the just-sent greeting (read-after-write lag).
    avail = _raw({"Стандарт": {f"2026-07-{d:02d}": 2 for d in range(20, 28)}})
    bs = server.configure(
        slots={"topic": "general_price", "rooms": [
            {"room_type": None, "fuzzy_date": "друга половина липня", "nights": 3,
             "adults": 2, "children_ages": []}]},
        history=[], availability=avail)               # first turn -> greets
    _run(bs.process_incoming_message("друга половина липня, на двох", 907))
    assert any(m.startswith("Доброго дня! Вас вітає") for m in server.sent)
    server.sent.clear()
    server.configure(slots={"topic": "faq", "faq_template": "PETS", "rooms": []},
                     history=[], availability=avail)  # lag: history STILL hides the greeting
    _run(bs.process_incoming_message("а з собакою можна?", 907))
    assert not any(m.startswith("Доброго дня! Вас вітає") for m in server.sent)   # no double greeting


def test_e2e_filler_with_refilled_slots_no_rescan(server):
    # Bug 2 (robust): even when the well-behaved extractor RE-EMITS the known slots on a
    # chit-chat turn (per the anti-amnesia rule), an UNCHANGED booking must NOT re-scan.
    avail = _raw({"Стандарт": {f"2026-07-{d:02d}": 2 for d in range(20, 28)}})
    room = {"room_type": None, "fuzzy_date": "друга половина липня", "nights": 3,
            "adults": 2, "children_ages": []}
    bs = server.configure(slots={"topic": "fuzzy_dates", "rooms": [dict(room)]},
                          history=_bot_spoke(), availability=avail)
    _run(bs.process_incoming_message("друга половина липня, на двох", 906))   # establishes memory + scrapes
    assert server.state["scraped"] is True
    server.state["scraped"] = False
    server.sent.clear()
    # filler turn: extractor RE-EMITS the SAME slots (not empty) -> slots_changed must be False
    server.configure(slots={"topic": "fuzzy_dates", "rooms": [dict(room)]},
                     history=_bot_spoke(), availability=avail)
    _run(bs.process_incoming_message("Але відпочинок просто необхідний!", 906))
    assert server.state["scraped"] is False    # unchanged booking -> no re-scan
    assert server.sent == []                    # silent
    assert 906 in bs._slot_memory               # slot memory preserved


def test_e2e_bare_yes_after_quote_triggers_booking(server):
    # Decision 2B: the bot's last message was a price quote -> a bare "Так" means the
    # client is ready to pay -> BOOK_ROOM (IBAN/payment) flow.
    bs = server.configure(
        slots={"topic": "greeting", "rooms": []},
        history=[{"id": 1, "message_type": "outgoing",
                  "content": "Вартість номеру типу Стандарт, для 2 дорослих, на 2 ночі "
                             "(5 - 7 липня), буде вартувати - 4400 грн\nБажаєте забронювати? 💙"}])
    _run(bs.process_incoming_message("Так", 410))
    assert any("IBAN" in m for m in server.sent)        # BOOK_ROOM payment details


def test_e2e_bare_yes_after_windows_accepts_first(server):
    # Decision 2A: the bot proposed windows (stored _pending_window) -> a bare "Так"
    # applies the first window's dates and proceeds to a quote.
    avail = _raw({"Стандарт": {f"2026-07-{d:02d}": 2 for d in range(20, 28)}})
    bs = server.configure(
        slots={"topic": "fuzzy_dates", "rooms": [
            {"room_type": None, "fuzzy_date": "друга половина липня", "nights": 3,
             "adults": 2, "children_ages": []}]},
        history=_bot_spoke(), availability=avail)
    _run(bs.process_incoming_message("друга половина липня, на двох", 411))   # proposes + stores window
    assert 411 in bs._pending_window
    server.sent.clear()
    server.state["scraped"] = False
    server.configure(slots={"topic": "greeting", "rooms": []},
                     history=[{"id": 9, "message_type": "outgoing",
                               "content": "маємо вільні віконця: 20 - 23 липня. Які дати вам підходять?"}],
                     availability=avail)
    _run(bs.process_incoming_message("Так", 411))
    assert any("грн" in m for m in server.sent)         # window accepted -> quote produced
    assert 411 not in bs._pending_window                # pending window consumed


def test_e2e_no_dates_then_guests_triggers_explore(server):
    # Bug 1: client said they don't know dates (mode set), then gives guests -> proactively
    # SCAN the open calendar and propose windows, NOT loop QUESTION_ONLY_DATES.
    avail = _raw({"Стандарт": {f"2026-07-{d:02d}": 2 for d in range(20, 28)}})
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": None, "checkin": None, "checkout": None, "fuzzy_date": None,
             "adults": 0, "children_count": 0, "children_ages": []}]},
        history=_bot_spoke(), availability=avail)
    _run(bs.process_incoming_message("Цікавить ціна, дати ще не можу сказати", 180))  # sets no-dates mode
    server.sent.clear()
    server.state["scraped"] = False
    server.configure(slots={"topic": "price_quote", "rooms": [
        {"room_type": None, "checkin": None, "checkout": None, "fuzzy_date": None,
         "adults": 2, "children_count": 0, "children_ages": []}]},
        history=_bot_spoke(), availability=avail)
    _run(bs.process_incoming_message("Для 2 осіб", 180))
    assert server.state["scraped"] is True
    full = "\n".join(server.sent)
    assert "вільні віконця" in full and "Оскільки ви ще не визначились" in full   # PROPOSE_WINDOWS_OPEN
    assert templates.QUESTION_ONLY_DATES not in full        # did NOT loop the date question


def test_e2e_no_dates_breaks_question_loop(server):
    # Anti-spam deadlock: client wants a price but has no dates/guests ("дати ще не можу
    # сказати") and we already asked QUESTION_ALL_MISSING -> must pivot to asking guests,
    # not repeat it (which the identical-reply guard would suppress, ignoring the client).
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": None, "checkin": None, "checkout": None, "fuzzy_date": None,
             "adults": 0, "children_count": 0, "children_ages": []}]},
        history=[{"id": 1, "message_type": "outgoing", "content": templates.QUESTION_ALL_MISSING}])
    _run(bs.process_incoming_message("Цікавить ціна, дати ще не можу сказати", 162))
    assert any(m == templates.ACKNOWLEDGE_NO_DATES_ASK_GUESTS for m in server.sent)
    assert not any(m == templates.QUESTION_ALL_MISSING for m in server.sent)


def test_e2e_book_confirm_without_quote_no_iban(server):
    # Bug 3: "Так, бронюємо" after a cross-sell (NOT an exact quote) must NOT drop IBAN.
    bs = server.configure(
        slots={"topic": "booking_confirm", "rooms": [
            {"room_type": "Напівлюкс", "checkin": "2026-07-06", "checkout": "2026-07-08",
             "adults": 2, "children_ages": []}]},
        history=[{"id": 1, "message_type": "outgoing",
                  "content": "На жаль, номери категорії Напівлюкс на ваші дати вже повністю "
                             "заброньовані. Проте маємо вільні: Стандарт. Який варіант вам підходить?"}],
        availability=_raw({"Стандарт": {"2026-07-06": 2, "2026-07-07": 2},
                           "Напівлюкс": {"2026-07-06": 0, "2026-07-07": 0}}))
    _run(bs.process_incoming_message("Так, бронюємо", 430))
    assert not any("IBAN" in m for m in server.sent)        # no premature payment details


def test_e2e_bare_yes_after_soldout_nearest_quotes_offered(server):
    # Live-QA bug: "Так" after a SOLD_OUT_FOUND_NEAREST offer must QUOTE the offered
    # window (using the dates the extractor parsed from the offer), not re-search.
    avail = _raw({"Стандарт +": {"2026-07-12": 2, "2026-07-13": 2}})
    bs = server.configure(
        slots={"topic": "nearest_dates", "rooms": [
            {"room_type": "Стандарт +", "checkin": "2026-07-12", "checkout": "2026-07-13",
             "adults": 2, "children_ages": []}]},
        history=[{"id": 1, "message_type": "outgoing",
                  "content": "На обрані вами дати всі номери зайняті 😔. Проте я підшукав "
                             "найближче вільне віконце: 12 - 13 липня. Бажаєте, розрахую вартість?"}],
        availability=avail)
    _run(bs.process_incoming_message("Так", 420))
    assert any("буде вартувати" in m for m in server.sent)        # quoted the offered window
    assert not any("підшукав найближче" in m for m in server.sent)  # did NOT re-search


def test_e2e_price_reask_without_dates_reshows_windows(server):
    # Decision 1: a price re-ask with no new info must NOT go silent -> explain + re-show
    # the proposed windows (from cache, no new scrape).
    avail = _raw({"Стандарт": {f"2026-07-{d:02d}": 2 for d in range(20, 28)}})
    room = {"room_type": None, "fuzzy_date": "друга половина липня", "nights": 3,
            "adults": 2, "children_ages": []}
    bs = server.configure(slots={"topic": "fuzzy_dates", "rooms": [dict(room)]},
                          history=_bot_spoke(), availability=avail)
    _run(bs.process_incoming_message("друга половина липня, на двох", 412))   # proposes windows + caches
    server.sent.clear()
    server.state["scraped"] = False
    server.configure(slots={"topic": "general_price", "rooms": [dict(room)]},
                     history=_bot_spoke(), availability=avail)
    _run(bs.process_incoming_message("а скільки вартість доби?", 412))
    full = "\n".join(server.sent)
    assert "Для точного розрахунку вартості доби" in full   # PRICE_NEED_DETAILS
    assert "вільні віконця" in full                         # + re-shown windows
    assert server.state["scraped"] is False                 # used cache, no new scrape


def test_e2e_fuzzy_with_guests_proactively_scans(server):
    # Fix 1: fuzzy period + guests (no exact dates) -> scrape and propose REAL windows,
    # never bounce back asking for exact dates.
    bs = server.configure(
        slots={"topic": "fuzzy_dates", "rooms": [
            {"room_type": None, "fuzzy_date": "початок липня", "nights": None,
             "checkin": None, "checkout": None, "adults": 2, "children_ages": []}]},
        history=_bot_spoke(),
        availability=_raw({"Стандарт": {f"2026-07-0{d}": 2 for d in range(1, 7)}}),
    )
    _run(bs.process_incoming_message("плануємо на початок липня, нас двоє", 352))
    assert server.state["scraped"] is True
    full = "\n".join(server.sent)
    assert "вільні віконця" in full and "1 - 6 липня" in full
    assert "напишіть, будь ласка, точні дати" not in full      # killed the fuzzy loop


def test_e2e_unknown_intent_hands_off_with_instagram_label(server):
    # Fix 2: a COMPLETELY unrecognized intent is the only manager hand-off, tagged Instagram.
    bs = server.configure(slots={"topic": "unknown", "rooms": []}, history=_bot_spoke())
    _run(bs.process_incoming_message("асдфгхй ???", 353))
    assert server.sent == [templates.MANAGER_HANDOFF]
    assert server.added_labels == [bot_logic.INSTAGRAM_LABEL]
    assert server.state["scraped"] is False


def test_e2e_barter_silent_and_labeled(server):
    # Blogger/barter pitch -> bot stays SILENT but tags the conversation Instagram (the
    # hotel WANTS the collab; a human negotiates). Detected deterministically before the LLM.
    bs = server.configure(slots={"topic": "greeting", "rooms": []})
    _run(bs.process_incoming_message("Вітаю! Я блогер, пропоную бартер — рілс за проживання", 440))
    assert server.sent == []                                    # silent
    assert server.added_labels == [bot_logic.INSTAGRAM_LABEL]   # tagged for the operator
    assert server.prompts == []                                 # caught before extraction


def test_e2e_barter_via_extractor_silent_and_labeled(server):
    # When keywords miss it, the extractor's topic=barter still -> silent + Instagram tag.
    bs = server.configure(slots={"topic": "barter", "rooms": []}, history=_bot_spoke())
    _run(bs.process_incoming_message("пропоную взаємну рекламу нашим аудиторіям", 441))
    assert server.sent == []
    assert server.added_labels == [bot_logic.INSTAGRAM_LABEL]


def test_e2e_six_plus_guests_asks_distribution(server):
    # Owner #21: 6+ guests packed into one room -> proactively PROPOSE a valid split; no scrape,
    # no premature quote.
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": None, "checkin": "2026-07-06", "checkout": "2026-07-08",
             "adults": 6, "children_count": 0, "children_ages": []}]},
        history=_bot_spoke())
    _run(bs.process_incoming_message("нас 6, хочемо до вас на 6-8 липня", 450))
    assert server.state["scraped"] is False
    assert any("розподілити" in m for m in server.sent)       # proactive split proposal
    assert not any("буде вартувати" in m for m in server.sent)


def test_e2e_six_plus_split_uses_stored_dates(server):
    # Fix (2026-06-24): after ASK_ROOM_DISTRIBUTION, a VALID split (<=3 adults per room) must
    # SCRAPE using the STORED dates (not re-ask for dates). Turn 1 sets memory (dates + 6
    # guests); turn 2 splits into 2 dateless rooms of 3 -> the engine backfills the dates & scrapes.
    avail = _raw({"Стандарт": {"2026-07-23": 5, "2026-07-24": 5}})
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": None, "checkin": "2026-07-23", "checkout": "2026-07-24",
             "adults": 6, "children_count": 0, "children_ages": []}]},
        history=_bot_spoke(), availability=avail)
    _run(bs.process_incoming_message("На 23-24 липня, нас 6 дорослих", 470))
    assert any("розподілити" in m for m in server.sent)   # proactive split proposal (owner #21)
    assert server.state["scraped"] is False        # distribution proposed first, no scrape yet

    server.sent.clear()
    server.configure(                               # turn 2: valid split, dates DROPPED by extractor
        slots={"topic": "price_quote", "rooms": [
            {"room_type": None, "checkin": None, "checkout": None, "adults": 3, "children_ages": []},
            {"room_type": None, "checkin": None, "checkout": None, "adults": 3, "children_ages": []}]},
        history=_bot_spoke(), availability=avail)
    _run(bs.process_incoming_message("Зробіть 2 номери по 3 дорослих", 470))
    assert server.state["scraped"] is True          # used STORED dates -> scraped, didn't re-ask
    full = "\n".join(server.sent)
    assert templates.QUESTION_ONLY_DATES not in full and templates.ASK_DATES_ONLY not in full
    assert templates.ASK_ROOM_DISTRIBUTION not in full


def test_e2e_six_plus_split_over_adult_cap_suggests_valid_split(server):
    # Owner fix #282-284: after ASK_ROOM_DISTRIBUTION, "2 номери: 4 і 3" packs 4 adults into
    # ONE room (> max 3). The bot must SUGGEST a valid re-split (3 rooms 2+2+3), never quote or
    # bare-reject. No scrape (nothing quotable yet).
    avail = _raw({"Стандарт": {"2026-07-23": 5, "2026-07-24": 5}})
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": None, "checkin": "2026-07-23", "checkout": "2026-07-24",
             "adults": 7, "children_count": 0, "children_ages": []}]},
        history=_bot_spoke(), availability=avail)
    _run(bs.process_incoming_message("На 23-24 липня, нас 7 дорослих", 471))
    server.sent.clear()
    server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": None, "checkin": None, "checkout": None, "adults": 4, "children_ages": []},
            {"room_type": None, "checkin": None, "checkout": None, "adults": 3, "children_ages": []}]},
        history=_bot_spoke(), availability=avail)
    _run(bs.process_incoming_message("Зробіть 2 номери: 4 і 3 дорослих", 471))
    full = "\n".join(server.sent)
    assert "максимум 3 дорослих" in full and "3 номери" in full and "2 + 2 + 3" in full
    assert "буде вартувати" not in full             # never quoted the invalid 4-adult room


def test_e2e_meal_cost_computed(server):
    # Owner 2026-07-10: an explicit meal request is priced deterministically — 3-разове for 4
    # people 2 days + only breakfast the last day, August: (1100*4*2)+(350*4*1)=10200 грн.
    bs = server.configure(
        slots={"topic": "faq", "faq_template": "FOOD_PRICES",
                "meals": {"persons": 4, "three_meals_days": 2, "two_meals_days": 0,
                          "breakfast_days": 1, "lunch_days": 0, "dinner_days": 0},
                "rooms": [{"room_type": "Стандарт", "checkin": "2026-08-06", "checkout": "2026-08-09",
                           "adults": 2, "children_ages": []}]},
        history=_bot_spoke())
    _run(bs.process_incoming_message("порахуйте 3-разове на 4 особи: 2 дні повне, останній лише сніданок", 610))
    full = "\n".join(server.sent)
    assert "10200 грн" in full                  # the exact food total
    assert "8800" in full and "1400" in full     # the breakdown lines


def test_e2e_menu_question_answered(server):
    bs = server.configure(slots={"topic": "faq", "faq_template": "FOOD_PRICES", "rooms": []},
                          history=_bot_spoke())
    _run(bs.process_incoming_message("а які там страви подають?", 611))
    assert any("узгоджуються по заїзду" in m for m in server.sent)


def test_e2e_split_accepted_then_quoted(server):
    # Owner #15/#21: the bot proposed a split last turn; a bare "так, порахуйте" must MATERIALISE
    # that distribution and QUOTE it (not re-propose forever).
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": None, "checkin": "2026-07-06", "checkout": "2026-07-07",
             "adults": 6, "children_count": 0, "children_ages": []}]},
        history=_bot_spoke(),
        availability=_raw({"Стандарт": {"2026-07-06": 5, "2026-07-07": 5}}))
    _run(bs.process_incoming_message("нас 6 на 6-7 липня", 612))
    assert 612 in bs._pending_split and any("розподілити" in m for m in server.sent)
    server.sent.clear()
    # accept — the bot's last message was the split offer; slots re-emit the group
    server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": None, "checkin": "2026-07-06", "checkout": "2026-07-07",
             "adults": 6, "children_count": 0, "children_ages": []}]},
        history=[{"id": 1, "message_type": "outgoing",
                  "content": "Ваша компанія завелика — пропонуємо розподілити її на 2 номери (наприклад: 3 + 3 осіб)"}],
        availability=_raw({"Стандарт": {"2026-07-06": 5, "2026-07-07": 5}}))
    _run(bs.process_incoming_message("так, порахуйте будь ласка", 612))
    full = "\n".join(server.sent)
    assert "буде вартувати" in full and "2 номери" in full   # quoted the two Standard rooms


def test_e2e_booking_com_hands_off_and_tags_instagram(server):
    # Persona 18: a Booking.com prepayment question must NOT get our IBAN (BOOK_ROOM) — the bot
    # replies it can't help with Booking.com and tags the conversation Instagram for a human.
    bs = server.configure(slots={"topic": "greeting", "rooms": []}, history=_bot_spoke())
    _run(bs.process_incoming_message(
        "Доброго дня! Я забронювала номер на booking, потрібно кинути 50% передоплати", 490))
    full = "\n".join(server.sent)
    assert "Booking.com" in full                     # the can't-help handoff
    assert "IBAN" not in full                         # NOT the prepayment/IBAN rules
    assert bot_logic.INSTAGRAM_LABEL in server.added_labels


def test_e2e_payment_handoff_tags_and_mutes(server):
    # Owner #22 (2026-07-09): a payment submission -> concise handoff, tag BOTH Замовлено (mute)
    # and Instagram, never verify/confirm. The Замовлено label then mutes all future messages.
    bs = server.configure(slots={"topic": "greeting", "rooms": []}, history=_bot_spoke())
    _run(bs.process_incoming_message("Я оплатив, ось квитанція", 495))
    full = "\n".join(server.sent)
    assert "перевірить оплату" in full and "менеджер" in full   # concise handoff text
    assert "буде вартувати" not in full and "IBAN" not in full  # never auto-confirms
    assert bot_logic.ORDER_LABEL in server.added_labels         # mute label
    assert bot_logic.INSTAGRAM_LABEL in server.added_labels     # owner's Instagram tag
    # Now the conversation is muted -> the bot ignores follow-ups.
    server.sent.clear()
    server.configure(slots={"topic": "greeting", "rooms": []},
                     history=_bot_spoke(), labels=[bot_logic.ORDER_LABEL])
    _run(bs.process_incoming_message("а коли підтвердження броні?", 495))
    assert server.sent == []                                    # muted -> silent


def test_e2e_ubd_soldout_includes_military_note(server):
    # Fix (2026-06-24): sold-out dates + UBD -> nearest-window offer WITH the military note.
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
             "adults": 2, "children_ages": [], "ubd": True}]},
        history=_bot_spoke(),
        availability=_raw({"Стандарт": {"2026-07-05": 0, "2026-07-06": 0,
                                        "2026-07-08": 2, "2026-07-09": 2, "2026-07-10": 2}}))
    _run(bs.process_incoming_message("Стандарт 5-7 липня, 2 дорослих, я УБД", 480))
    full = "\n".join(server.sent)
    assert "найближче вільне віконце" in full and templates.MILITARY in full


def test_e2e_503_cooldown_silences_followups(server):
    # Fix 7: after an LLM outage the bot goes silent for 5 min so a human can take over,
    # instead of re-failing on every new message.
    bs = server.configure(slots={"topic": "greeting", "rooms": []}, history=[])
    async def boom(prompt, *a, **k):
        raise RuntimeError("503 UNAVAILABLE high demand")
    bs.generate_with_retry = boom
    _run(bs.process_incoming_message("Дізнатися вартість", 175))
    assert server.sent == [templates.ERROR_LLM_DOWN]
    assert 175 in bs._cooldowns
    server.sent.clear()
    # a follow-up within the cooldown -> ignored entirely (even though the LLM is "back")
    server.configure(slots={"topic": "price_quote", "rooms": [
        {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-07",
         "adults": 2, "children_ages": []}]}, history=_bot_spoke())
    _run(bs.process_incoming_message("Стандарт на 6-7 липня", 175))
    assert server.sent == []                       # silent during cooldown
    assert server.state["scraped"] is False
    assert server.prompts == []                    # no LLM call attempted


def test_e2e_pure_thanks_gets_a_close(server):
    # Fix 8: "Дякую" must NEVER be met with silence -> a warm close (deterministic, no LLM).
    bs = server.configure(slots={"topic": "greeting", "rooms": []}, history=_bot_spoke())
    _run(bs.process_incoming_message("Дякую!", 490))
    assert server.sent == [templates.ACKNOWLEDGE_THANKS]
    assert server.prompts == []                    # no LLM call


def test_e2e_insist_on_child_ages_when_ignored(server):
    # Rule 3 (owner 2026-07-06): the bot already asked the child ages and the client replied
    # WITHOUT them -> insist firmly (a DIFFERENT message, so anti-spam doesn't silence it), and
    # NEVER quote a price until the ages are known.
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": None, "checkin": "2026-07-06", "checkout": "2026-07-08",
             "adults": 2, "children_count": 1, "children_ages": []}]},
        history=[{"id": 1, "message_type": "outgoing", "content": templates.QUESTION_MISSING_AGE}])
    _run(bs.process_incoming_message("та просто цікавить ціна", 610))
    assert any(m == templates.INSIST_CHILD_AGES for m in server.sent)
    assert not any("буде вартувати" in m for m in server.sent)   # never quotes without ages


def test_e2e_no_insist_for_adult_only_booking(server):
    # Rule 3 guard (Persona 7 live bug): the generic prompts also say "вік діток", but an
    # ADULT-ONLY booking (no children) must NEVER trigger the child-age insist.
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": None, "checkin": "2026-07-13", "checkout": "2026-07-17",
             "adults": 0, "children_count": 0, "children_ages": []}]},
        history=[{"id": 1, "message_type": "outgoing", "content": templates.QUESTION_ALL_MISSING}])
    _run(bs.process_incoming_message("13-17 липня", 612))
    assert not any(m == templates.INSIST_CHILD_AGES for m in server.sent)
    assert any(m == templates.QUESTION_MISSING_GUESTS for m in server.sent)   # asks guests, not insist


@pytest.mark.parametrize("slots,expected", [
    ({"rooms": [{"children_count": 1, "children_ages": []}]}, True),
    ({"rooms": [{"children_count": 2, "children_ages": [8]}]}, True),
    ({"rooms": [{"children_count": 2, "children_ages": [8, 10]}]}, False),   # ages known
    ({"rooms": [{"adults": 2, "children_count": 0, "children_ages": []}]}, False),  # no children
    ({"rooms": []}, False),
])
def test_has_child_of_unknown_age(slots, expected):
    assert bot_logic.has_child_of_unknown_age(slots) is expected


@pytest.mark.parametrize("text,expected", [
    ("що входить у вартість?", "INCLUDED_IN_THE_PRICE"),
    ("що включено у проживання?", "INCLUDED_IN_THE_PRICE"),
    ("чи входить басейн у вартість?", "POOL"),           # pool amenity check wins
    ("що входить у сніданок?", "FOOD_PRICES"),            # food keyword wins (checked earlier)
])
def test_faq_override_included_in_price(text, expected):
    assert bot_logic.faq_override(text) == expected


def test_e2e_first_age_ask_is_not_insist(server):
    # Regression: the FIRST age ask is the normal question, not the firm insist.
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": None, "checkin": "2026-07-06", "checkout": "2026-07-08",
             "adults": 2, "children_count": 1, "children_ages": []}]},
        history=_bot_spoke())
    _run(bs.process_incoming_message("2 дорослих і дитина", 611))
    assert any(m == templates.QUESTION_MISSING_AGE for m in server.sent)
    assert not any(m == templates.INSIST_CHILD_AGES for m in server.sent)


def test_e2e_llm_down_always_tags_instagram_even_on_repeat(server):
    # Rule 2 (owner 2026-07-06): a repeat 503 (holding message suppressed) STILL tags Instagram.
    bs = server.configure(slots={"topic": "greeting", "rooms": []},
                          history=[{"id": 1, "message_type": "outgoing", "content": templates.ERROR_LLM_DOWN}])
    async def boom(prompt, *a, **k):
        raise RuntimeError("503 UNAVAILABLE")
    bs.generate_with_retry = boom
    _run(bs.process_incoming_message("ще раз", 172))
    assert server.sent == []                                    # holding message not repeated
    assert bot_logic.INSTAGRAM_LABEL in server.added_labels     # but Instagram tag applied


def test_e2e_pure_thanks_still_answers_pending_burst_faq(server):
    # Live-QA finding: an FAQ ("харчування?") the bot was still processing when a bare "Дякую"
    # arrived (superseding it) must STILL be answered before the warm close — never dropped.
    bs = server.configure(
        slots={"topic": "greeting", "rooms": []},
        history=[{"id": 1, "message_type": "outgoing", "content": templates.THINKING_ABOUT_IT},
                 {"id": 2, "message_type": "incoming", "content": "Зорієнтуйте по харчуванню"}])
    _run(bs.process_incoming_message("Дякую", 495))
    joined = "\n".join(server.sent)
    assert "350" in joined or "Сніданок" in joined            # FOOD_PRICES was answered
    assert templates.ACKNOWLEDGE_THANKS in server.sent        # and the warm close still sent
    assert server.prompts == []                               # still no LLM call on the fast path


def test_e2e_pure_thanks_no_pending_faq_just_closes(server):
    # Regression: a plain "Дякую" with nothing pending still just closes warmly (unchanged).
    bs = server.configure(slots={"topic": "greeting", "rooms": []}, history=_bot_spoke())
    _run(bs.process_incoming_message("Дякую!", 494))
    assert server.sent == [templates.ACKNOWLEDGE_THANKS]


def test_e2e_pet_note_appended_to_quote(server):
    # Fix 4: a client who mentioned a pet gets the +300 грн/доба note on the price quote.
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-07",
             "adults": 2, "children_ages": []}]},
        history=[{"id": 1, "message_type": "incoming", "content": "приїдемо з собачкою"},
                 {"id": 2, "message_type": "outgoing", "content": "вітаю"}],
        availability=_raw({"Стандарт": {"2026-07-06": 3}}))
    _run(bs.process_incoming_message("Стандарт на 6-7 липня", 492))
    full = "\n".join(server.sent)
    assert "буде вартувати" in full and "доплати за тваринку 300 грн" in full


def test_e2e_dedup_suppresses_message_two_back(server):
    # Fix 4: a reply matching a bot message TWO turns back is suppressed (drip FAQ spam).
    bs = server.configure(
        slots={"topic": "faq", "faq_template": "SMOKING", "rooms": []},
        history=[{"id": 1, "message_type": "outgoing",
                  "content": templates.SMOKING + templates.FAQ_DATE_NUDGE},
                 {"id": 2, "message_type": "outgoing", "content": "щось інше від бота"}])
    _run(bs.process_incoming_message("а курити можна?", 496))
    assert not any(templates.SMOKING in m for m in server.sent)   # suppressed (2 back)


def test_e2e_off_season_tags_instagram_no_phone_ask(server):
    # Off-season (May) -> holding reply + Instagram tag for a human follow-up (owner rule):
    # we DON'T reject and DON'T ask for a phone number.
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": "Стандарт", "checkin": "2026-05-11", "checkout": "2026-05-13",
             "adults": 2, "children_ages": []}]},
        history=_bot_spoke())
    _run(bs.process_incoming_message("Стандарт 11-13 травня для двох", 460))
    assert server.sent == [templates.OFF_SEASON]
    assert bot_logic.INSTAGRAM_LABEL in server.added_labels
    assert server.state["scraped"] is False


def test_is_retryable_llm_error(server):
    import bot_server
    assert bot_server._is_retryable_llm_error(RuntimeError("503 UNAVAILABLE high demand")) is True
    assert bot_server._is_retryable_llm_error(RuntimeError("429 RESOURCE_EXHAUSTED")) is True
    assert bot_server._is_retryable_llm_error(RuntimeError("400 INVALID_ARGUMENT")) is False


def test_generate_with_retry_backoff_then_succeeds(server):
    # Transient 503s are retried (asyncio.sleep is patched no-op by the fixture) and the
    # eventual success is returned. Use bot_server directly so the REAL generate_with_retry
    # runs (configure() would replace it with the fake LLM).
    import bot_server
    from types import SimpleNamespace
    calls = {"n": 0}
    async def gen(model, contents):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("503 UNAVAILABLE — high demand")
        return SimpleNamespace(text="{}")
    bot_server.ai_client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=gen)))
    res = _run(bot_server.generate_with_retry("p"))
    assert calls["n"] == 3 and res.text == "{}"


def test_e2e_llm_down_sends_fallback_and_tags(server):
    # LLM provider down (503 after retries) -> bot must NOT stay silent: send the holding
    # message ONCE and hand off to a manager (Instagram label).
    bs = server.configure(slots={"topic": "greeting", "rooms": []}, history=[])
    async def boom(prompt, *a, **k):
        raise RuntimeError("503 UNAVAILABLE high demand")
    bs.generate_with_retry = boom
    _run(bs.process_incoming_message("Дізнатися вартість", 170))
    assert server.sent == [templates.ERROR_LLM_DOWN]
    assert server.added_labels == [bot_logic.INSTAGRAM_LABEL]


def test_e2e_llm_down_not_repeated(server):
    # On a follow-up while still down, don't repeat the holding message (history shows it).
    bs = server.configure(slots={"topic": "greeting", "rooms": []},
                          history=[{"id": 1, "message_type": "outgoing", "content": templates.ERROR_LLM_DOWN}])
    async def boom(prompt, *a, **k):
        raise RuntimeError("503 UNAVAILABLE")
    bs.generate_with_retry = boom
    _run(bs.process_incoming_message("ще раз", 171))
    assert server.sent == []        # already told them -> stay quiet


def test_e2e_concurrent_drip_no_double_greeting(server):
    # Two messages arriving together for the SAME conversation must be serialized
    # by the per-conversation lock -> exactly one greeting (no drip race).
    bs = server.configure(slots={"topic": "greeting", "rooms": []}, dynamic_history=True)

    async def two_at_once():
        await asyncio.gather(
            bs.process_incoming_message("Привіт", 601),
            bs.process_incoming_message("Ще раз", 601),
        )
    asyncio.run(two_at_once())
    greetings = [m for m in server.sent if m.startswith("Доброго дня! Вас вітає")]
    assert len(greetings) == 1


def test_e2e_drip_burst_emits_exactly_one_reply(server):
    # Fix 4: a 3-message drip-burst must yield ONE reply (superseded ones suppressed),
    # never the same message 3x.
    bs = server.configure(slots={"topic": "greeting", "rooms": []}, dynamic_history=True)

    async def burst():
        await asyncio.gather(*[bs.process_incoming_message(f"msg{i}", 701) for i in range(3)])
    asyncio.run(burst())
    greetings = [m for m in server.sent if m.startswith("Доброго дня! Вас вітає")]
    questions = [m for m in server.sent if m == templates.QUESTION_ALL_MISSING]
    assert len(greetings) == 1
    assert len(questions) == 1   # one reply for the whole burst, not three


def test_e2e_no_repeated_identical_reply(server):
    # Vague messages in a row -> ask ONCE, then suppress the identical repeat (no spam).
    bs = server.configure(slots={"topic": "greeting", "rooms": []}, dynamic_history=True)
    _run(bs.process_incoming_message("Привіт", 801))
    _run(bs.process_incoming_message("Ну то що?", 802))    # still vague -> same question
    _run(bs.process_incoming_message("Агов", 803))         # and again
    assert len([m for m in server.sent if m == templates.QUESTION_ALL_MISSING]) == 1


# -- payment hand-off: reply + tag "Замовлено" + never confirm via LLM ------

def test_e2e_payment_keyword_handoff_and_label(server):
    bs = server.configure(slots={"topic": "greeting", "rooms": []})
    _run(bs.process_incoming_message("Оплатив! Ось квитанція 🙂", 401))
    assert server.sent == [templates.PAYMENT_RECEIVED_HANDOFF]
    # Owner #22: mute label (Замовлено) AND the Instagram manager tag.
    assert server.added_labels == [bot_logic.ORDER_LABEL, bot_logic.INSTAGRAM_LABEL]
    assert server.prompts == []                             # LLM never triggered


def test_e2e_payment_attachment_handoff(server):
    # An image-only payment screenshot (empty text + attachment) hands off too.
    bs = server.configure(slots={"topic": "greeting", "rooms": []})
    _run(bs.process_incoming_message("", 402, True))
    assert server.sent == [templates.PAYMENT_RECEIVED_HANDOFF]
    assert server.added_labels == [bot_logic.ORDER_LABEL, bot_logic.INSTAGRAM_LABEL]


# -- mute switch: a human-owned ("Замовлено") conversation is ignored -------

def test_e2e_muted_conversation_is_ignored(server):
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-07",
             "adults": 2, "children_ages": []}]},
        labels=["Замовлено"])
    _run(bs.process_incoming_message("Стандарт на 6-7 липня для двох", 403))
    assert server.sent == []          # bot stays completely silent
    assert server.prompts == []       # no LLM
    assert server.added_labels == []  # no changes
    assert server.state["scraped"] is False


def test_e2e_booking_confirm_off_season_blocked(server):
    # Confirming a May (off-season) stay must NOT yield payment details.
    bs = server.configure(
        slots={"topic": "booking_confirm", "rooms": [
            {"room_type": None, "checkin": "2026-05-11", "checkout": "2026-05-13",
             "adults": 2, "children_ages": []}]},
        history=_bot_spoke())
    _run(bs.process_incoming_message("узгоджуємо 11-13 травня", 310))
    assert server.sent == [templates.OFF_SEASON]
    assert not any("IBAN" in m for m in server.sent)


# -- spam ignored entirely; phone -> handoff (both before any LLM) ----------

def test_e2e_spam_silent(server):
    bs = server.configure(slots={"topic": "greeting", "rooms": []})
    _run(bs.process_incoming_message("Створюю чат-ботів, є пробний тариф", 307))
    assert server.sent == [] and server.prompts == []


def test_e2e_phone_handoff(server):
    bs = server.configure(slots={"topic": "greeting", "rooms": []})
    _run(bs.process_incoming_message("Передзвоніть 0991234567", 308))
    assert server.sent == [templates.PHONE_RECEIVED] and server.prompts == []


# -- DRIP handling (directive 4): full fragmented history reaches the extractor

def test_e2e_drip_history_is_passed_to_extractor(server):
    history = [
        {"id": 1, "message_type": "incoming", "content": "Привіт"},
        {"id": 2, "message_type": "outgoing", "content": "Вітаємо! Які дати цікавлять?"},
        {"id": 3, "message_type": "incoming", "content": "А є вільний стандарт?"},
        {"id": 4, "message_type": "incoming", "content": "на 12 липня"},
    ]
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": "Стандарт", "checkin": "2026-07-12", "checkout": "2026-07-13",
             "adults": 2, "children_ages": []}]},
        history=history,
        availability=_raw({"Стандарт": {"2026-07-12": 2}}),
    )
    _run(bs.process_incoming_message("2 дорослих", 309))
    prompt = server.prompts[-1]
    # all fragments are present so the extractor can consolidate them into one intent
    for fragment in ["Привіт", "вільний стандарт", "12 липня", "2 дорослих"]:
        assert fragment in prompt


# -- webhook routing: only incoming message_created is processed ------------

def test_webhook_routing_schedules_only_incoming(server):
    bs = server.configure(slots={"topic": "greeting", "rooms": []})
    from fastapi import BackgroundTasks

    class FakeRequest:
        def __init__(self, payload):
            self._payload = payload
        async def json(self):
            return self._payload

    def schedules(payload):
        bg = BackgroundTasks()
        _run(bs.chatwoot_webhook(FakeRequest(payload), bg))
        return len(bg.tasks)

    assert schedules({"event": "message_created", "message_type": "incoming",
                      "content": "Привіт", "conversation": {"id": 1}}) == 1
    assert schedules({"event": "message_created", "message_type": "outgoing",
                      "content": "Бот", "conversation": {"id": 1}}) == 0
    assert schedules({"event": "conversation_updated"}) == 0
    # image-only payment screenshot (no text, but an attachment) is still processed
    assert schedules({"event": "message_created", "message_type": "incoming",
                      "content": None, "attachments": [{"id": 1}],
                      "conversation": {"id": 1}}) == 1
    # truly empty incoming (no text, no attachment) is ignored
    assert schedules({"event": "message_created", "message_type": "incoming",
                      "content": None, "conversation": {"id": 1}}) == 0
    # Bug 5: WebWidget/other channels may send message_type as INT 0 -> still processed
    assert schedules({"event": "message_created", "message_type": 0,
                      "content": "Привіт з віджета", "conversation": {"id": 2}}) == 1
    # private agent notes are ignored
    assert schedules({"event": "message_created", "message_type": "incoming", "private": True,
                      "content": "внутрішня нотатка", "conversation": {"id": 1}}) == 0
