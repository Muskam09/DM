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


# -- availability gating helper (the new pricing gate) ----------------------

def test_is_room_available_states():
    avail = {"Стандарт": {"2026-07-05": 3, "2026-07-06": 0}, "Напівлюкс": {"2026-07-05": 1}}
    assert bot_logic.is_room_available(avail, "Стандарт", ["2026-07-05"]) == "available"
    assert bot_logic.is_room_available(avail, "Стандарт", ["2026-07-06"]) == "sold_out"
    assert bot_logic.is_room_available(avail, "Стандарт", ["2026-07-05", "2026-07-06"]) == "sold_out"
    assert bot_logic.is_room_available(avail, "Président", ["2026-07-05"]) == "unknown"      # unknown room
    assert bot_logic.is_room_available(avail, "Стандарт", ["2026-08-10"]) == "unknown"       # out of window


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
    ("2 дорослих і дитина 8 років", False),
    ("група 15 дітей, школа", False),     # 15 < 40, not an event
    ("Стандарт на 5-7 липня", False),
])
def test_looks_like_large_group(text, expected):
    assert bot_logic.looks_like_large_group(text) is expected


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


def test_new_templates_content():
    assert "0673445220" in templates.LARGE_GROUPS_EVENTS
    assert "350" in templates.FOOD_PRICES and "1100" in templates.FOOD_PRICES
    assert "instagram.com/stories/highlights" in templates.PETS
    assert "WiFi" in templates.ROOM_AMENITIES
    assert "Стандарт +" in templates.SMOKING
    assert "Ворохта" in templates.HOW_TO_GET_THERE


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
    t = templates.PAYMENT_RECEIVED_HANDOFF
    assert "адміністратор" in t and "ПІБ" in t and "Instagram" in t


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
        availability=_raw({"Стандарт": {"2026-07-05": 3, "2026-07-06": 0}}),  # 6th booked
    )
    _run(bs.process_incoming_message("Стандарт на 5-7 липня", 303))
    assert any(templates.SOLD_OUT_NEAREST == m for m in server.sent)
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


# -- payment hand-off: reply + tag "Замовлено" + never confirm via LLM ------

def test_e2e_payment_keyword_handoff_and_label(server):
    bs = server.configure(slots={"topic": "greeting", "rooms": []})
    _run(bs.process_incoming_message("Оплатив! Ось квитанція 🙂", 401))
    assert server.sent == [templates.PAYMENT_RECEIVED_HANDOFF]
    assert server.added_labels == [bot_logic.ORDER_LABEL]   # tagged "Замовлено"
    assert server.prompts == []                             # LLM never triggered


def test_e2e_payment_attachment_handoff(server):
    # An image-only payment screenshot (empty text + attachment) hands off too.
    bs = server.configure(slots={"topic": "greeting", "rooms": []})
    _run(bs.process_incoming_message("", 402, True))
    assert server.sent == [templates.PAYMENT_RECEIVED_HANDOFF]
    assert server.added_labels == [bot_logic.ORDER_LABEL]


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
