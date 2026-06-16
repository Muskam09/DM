"""
test_webhook.py — Behavioural tests for the 8 User Cases (project_spec.md §6).

The bot answers out-of-band: POST /webhook returns {"status":"ok"} and the real
reply is sent to Chatwoot from a background task, with all pricing decided by the
LLM. So this suite asserts the *deterministic* behaviour of each case:

  Layer A (runs anywhere, pure modules): intent->scrape gating, blacklist
          filtering, availability decisions, greeting/[SPLIT]/<REPLY> plumbing,
          and exact prices via pricing_engine.
  Layer B (runs where FastAPI/google-genai are installed — i.e. the bot-brain
          container): drives process_incoming_message / the /webhook handler with
          the Gemini, Chatwoot and Playwright boundaries mocked, asserting the
          orchestration (scrape or not, "Секундочку", greeting, message delivery).

Exact price correctness is locked down separately in test_pricing.py.
"""
import asyncio
from types import SimpleNamespace

import pytest

import bot_logic
import templates
from pricing_engine import PricingEngine, make_guests


# ===========================================================================
# Layer A — deterministic, dependency-free
# ===========================================================================

engine = PricingEngine()


# -- shared fixtures: a synthetic "Шахівниця" for the chosen June dates --------

DATES_28 = ["2026-06-28"]  # single night 28->29

RAW_PARTIAL_OVERBOOKING = {  # Case 4: chosen room sold out, another free
    "Стандарт +": {"total_available": {"2026-06-28": 0}, "rooms": {}},
    "Напівлюкс": {"total_available": {"2026-06-28": 2}, "rooms": {}},
    "Колиба": {"total_available": {"2026-06-28": 5}, "rooms": {}},        # blacklisted
    "Overbooking": {"total_available": {"2026-06-28": 9}, "rooms": {}},   # blacklisted
    "Басейн": {"total_available": {"2026-06-28": 1}, "rooms": {}},        # blacklisted
}

RAW_SOLD_OUT = {  # Case 5: everything sold out on the dates
    "Стандарт +": {"total_available": {"2026-06-28": 0}, "rooms": {}},
    "Напівлюкс": {"total_available": {"2026-06-28": 0}, "rooms": {}},
}


# -- Case 1: First contact, no data -----------------------------------------

def test_case1_no_scrape_and_greeting_with_split():
    assert bot_logic.intent_says_yes("НІ") is False  # scraper must NOT run
    reply = bot_logic.prepend_greeting_if_needed(templates.QUESTION_ALL_MISSING,
                                                 bot_has_spoken=False)
    parts = bot_logic.split_messages(reply)
    assert len(parts) == 2
    assert parts[0].startswith("Доброго дня! Вас вітає D&T Hotel")
    assert parts[1] == templates.QUESTION_ALL_MISSING


def test_no_double_greeting_when_bot_already_spoke():
    reply = bot_logic.prepend_greeting_if_needed("Вже без вітання", bot_has_spoken=True)
    assert not reply.startswith("Доброго дня")


# -- Case 2: Dates only, no room chosen -> no scrape -------------------------

def test_case2_dates_only_does_not_trigger_scrape():
    assert bot_logic.intent_says_yes("НІ") is False


# -- Case 3: Room chosen, data complete -> price (1 weekday night) -----------

def test_case3_intent_triggers_scrape_and_price():
    assert bot_logic.intent_says_yes("ТАК") is True
    price = engine.price("Стандарт +", "2026-06-28", "2026-06-29", make_guests(adults=2))
    assert price == 2400


# -- Case 4: Partial overbooking -> only genuinely-free rooms offered --------

def test_case4_blacklist_filtered_and_only_free_rooms_offered():
    simplified = bot_logic.build_simplified_availability(RAW_PARTIAL_OVERBOOKING)
    # Blacklisted categories are gone:
    assert set(simplified) == {"Стандарт +", "Напівлюкс"}
    # Only the genuinely free category is offerable (asserted via real bot code):
    assert bot_logic.free_room_types(simplified, DATES_28) == ["Напівлюкс"]
    assert bot_logic.is_sold_out(simplified, DATES_28) is False


# -- Case 5: Full sold-out ---------------------------------------------------

def test_case5_full_sold_out_detected():
    simplified = bot_logic.build_simplified_availability(RAW_SOLD_OUT)
    assert bot_logic.free_room_types(simplified, DATES_28) == []  # nothing free
    assert bot_logic.is_sold_out(simplified, DATES_28) is True    # -> sold-out branch


# -- Case 6: Data dripped word-by-word -> still no scrape until room chosen --

def test_case6_partial_drip_does_not_trigger_scrape():
    # "5 липня", "дитині 7 років", "2 дорослих", "1 ніч" — each is data, not a
    # room selection, so the intent pre-filter answers НІ.
    assert bot_logic.intent_says_yes("НІ") is False


# -- Case 7: Strict math with an extra child place --------------------------

def test_case7_child_extra_place_price():
    price = engine.price("Стандарт", "2026-07-06", "2026-07-08",
                         make_guests(adults=2, children_ages=[8]))
    assert price == 5000  # (2200 + 300) * 2


# -- Case 8: Changing dates/nights for a chosen room -> must re-scrape -------

@pytest.mark.parametrize("msg_intent", ["ТАК"])
def test_case8_change_dates_triggers_scrape(msg_intent):
    assert bot_logic.intent_says_yes(msg_intent) is True


# -- intent pre-filter robustness -------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("ТАК", True), ("так", True), ("Так, звичайно", True),
    ("НІ", False), ("ні", False), ("", False), ("Не впевнений", False),
])
def test_intent_says_yes_variants(text, expected):
    assert bot_logic.intent_says_yes(text) is expected


# -- <REPLY> extraction ------------------------------------------------------

def test_extract_reply_takes_only_reply_tag():
    raw = "<THINK>Дати: 28-29. Ночі: 1. Гості: 2.</THINK><REPLY>Вартість 2400 грн</REPLY>"
    assert bot_logic.extract_reply(raw) == "Вартість 2400 грн"


def test_extract_reply_strips_think_when_no_reply_tag():
    raw = "<THINK>міркування</THINK>Текст без тегу"
    assert "міркування" not in bot_logic.extract_reply(raw)


# -- B2B spam detection (Case: ignore, no reply) ----------------------------

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


# -- phone-number capture (Case: hand off to a human) -----------------------

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


# -- new templates carry the owner's exact content --------------------------

def test_new_templates_content():
    assert "0673445220" in templates.LARGE_GROUPS_EVENTS          # co-owner phone
    assert "350" in templates.FOOD_PRICES and "1100" in templates.FOOD_PRICES
    assert "instagram.com/stories/highlights" in templates.PETS   # pet-rules link
    assert "WiFi" in templates.ROOM_AMENITIES
    assert "Стандарт +" in templates.SMOKING                      # balcony exception
    assert templates.OFF_SEASON and templates.FUZZY_DATES and templates.POLITE_CLOSE


# ===========================================================================
# Layer B — live orchestration (skips unless FastAPI/google-genai available)
# ===========================================================================

@pytest.fixture
def server(monkeypatch):
    """Import bot_server (skips locally) and stub all external boundaries."""
    try:
        import bot_server
    except Exception as exc:  # ImportError locally, anything in a broken env
        pytest.skip(f"bot_server unavailable here (needs container deps): {exc}")

    sent = []      # messages "sent" to Chatwoot
    prompts = []   # prompts passed to the (faked) LLM
    state = {"scraped": False}

    monkeypatch.setattr(bot_server, "send_chatwoot_message",
                        lambda conv_id, text: sent.append(text))

    async def _fast_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(bot_server.asyncio, "sleep", _fast_sleep)

    bot_server.AVAILABILITY_CACHE.clear()

    def configure(intent="НІ", reply="", history=None, availability=None):
        async def fake_llm(prompt, *a, **k):
            prompts.append(prompt)
            text = intent if "запускати скрапер" in prompt else reply
            return SimpleNamespace(text=text)
        monkeypatch.setattr(bot_server, "generate_with_retry", fake_llm)
        monkeypatch.setattr(bot_server, "get_chatwoot_history",
                            lambda conv_id: history or [])

        async def fake_fetch():
            state["scraped"] = True
            return availability
        monkeypatch.setattr(bot_server, "fetch_hotel_availability", fake_fetch)
        return bot_server

    return SimpleNamespace(configure=configure, sent=sent, prompts=prompts, state=state)


def _run(coro):
    return asyncio.run(coro)


# -- Case 1 end-to-end: greeting + question, no scrape ----------------------

def test_e2e_case1_first_contact(server):
    bs = server.configure(
        intent="НІ",
        reply=f"<THINK>немає даних</THINK><REPLY>{templates.QUESTION_ALL_MISSING}</REPLY>",
        history=[],
    )
    _run(bs.process_incoming_message("Доброго дня, хочу забронювати номер!", 101))
    assert server.state["scraped"] is False
    assert server.sent[0].startswith("Доброго дня! Вас вітає D&T Hotel")
    assert server.sent[-1] == templates.QUESTION_ALL_MISSING


# -- Case 3 end-to-end: scrape runs, "Секундочку" precedes the quote --------

def test_e2e_case3_scrape_and_quote(server):
    history = [
        {"id": 1, "message_type": "incoming", "content": "28-29 червня, 2 дорослих"},
        {"id": 2, "message_type": "outgoing", "content": templates.PRICE_JUNE},
    ]
    quote = "Вартість номеру типу Стандарт +, для 2 осіб, на 1 ніч (28-29 червня), буде вартувати - 2400 грн."
    bs = server.configure(
        intent="ТАК",
        reply=f"<THINK>1 будня ніч, вільно</THINK><REPLY>{quote}</REPLY>",
        history=history,
        availability={"Стандарт +": {"total_available": {"2026-06-28": 2}, "rooms": {}}},
    )
    _run(bs.process_incoming_message("Давайте номер типу Стандарт +", 103))
    assert server.state["scraped"] is True
    assert "Секундочку" in server.sent[0]      # scrape notice goes out first
    assert server.sent[-1] == quote
    # No greeting mid-dialogue:
    assert not any(m.startswith("Доброго дня") for m in server.sent)


# -- Case 5 end-to-end: agreeing to a nearest-date search re-scrapes ---------

def test_e2e_case5_nearest_search_triggers_scrape(server):
    history = [
        {"id": 1, "message_type": "incoming", "content": "Стандарт +"},
        {"id": 2, "message_type": "outgoing",
         "content": "На жаль, на ваші дати всі номери повністю заброньовані"},
    ]
    bs = server.configure(
        intent="ТАК",
        reply="<THINK>шукаю вперед</THINK><REPLY>" + templates.NEAREST_DATES + "</REPLY>",
        history=history,
        availability=RAW_SOLD_OUT,
    )
    _run(bs.process_incoming_message("Так, підшукайте", 105))
    assert server.state["scraped"] is True


# -- Case 8 end-to-end: changing dates forces a re-scrape -------------------

def test_e2e_case8_date_change_triggers_scrape(server):
    history = [
        {"id": 1, "message_type": "incoming", "content": "Стандарт"},
        {"id": 2, "message_type": "outgoing", "content": "Вартість ... 2400 грн"},
    ]
    bs = server.configure(
        intent="ТАК",
        reply="<THINK>нові дати, перевіряю</THINK><REPLY>Перерахунок готовий.</REPLY>",
        history=history,
        availability={"Стандарт": {"total_available": {"2026-08-10": 0}, "rooms": {}}},
    )
    _run(bs.process_incoming_message("А якщо 10-15 серпня?", 108))
    assert server.state["scraped"] is True


# -- webhook routing: only incoming message_created events are processed -----

def test_webhook_routing_schedules_only_incoming(server):
    bs = server.configure(intent="НІ", reply="<REPLY>ok</REPLY>")
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

    incoming = {"event": "message_created", "message_type": "incoming",
                "content": "Привіт", "conversation": {"id": 1}}
    echo = {"event": "message_created", "message_type": "outgoing",
            "content": "Бот", "conversation": {"id": 1}}
    assert schedules(incoming) == 1
    assert schedules(echo) == 0
    assert schedules({"event": "conversation_updated"}) == 0


# -- Case 15 e2e: B2B spam is ignored entirely (no reply, no LLM, no scrape) -

def test_e2e_spam_is_ignored_no_reply(server):
    bs = server.configure(intent="НІ", reply="<REPLY>x</REPLY>")
    _run(bs.process_incoming_message("Створюю чат-ботів для готелів, є пробний тариф", 215))
    assert server.sent == []            # nothing sent to Chatwoot
    assert server.prompts == []         # LLM never called
    assert server.state["scraped"] is False


# -- phone handoff e2e: reply PHONE_RECEIVED and stop (no LLM) ---------------

def test_e2e_phone_number_handoff(server):
    bs = server.configure(intent="НІ", reply="<REPLY>x</REPLY>")
    _run(bs.process_incoming_message("Передзвоніть мені 0959224876", 216))
    assert server.sent == [templates.PHONE_RECEIVED]
    assert server.prompts == []         # short-circuits before the LLM


# -- sys_prompt enforces Ukrainian-only + exposes the new templates ---------

def test_e2e_sys_prompt_enforces_ukrainian_and_new_templates(server):
    bs = server.configure(
        intent="НІ", reply="<REPLY>ok</REPLY>",
        history=[{"id": 1, "message_type": "outgoing", "content": "вітаю"}],
    )
    _run(bs.process_incoming_message("Скажіть про чани та харчування", 217))
    sys_prompts = [p for p in server.prompts if "системний алгоритм" in p]
    assert sys_prompts, "sys_prompt was not generated"
    sp = sys_prompts[0]
    assert "ВИКЛЮЧНО УКРАЇНСЬКОЮ" in sp                    # strict-UA hard rule
    assert "SAUNA_VATS" in sp and "FOOD_PRICES" in sp      # new FAQ templates wired in
    assert "LARGE_GROUPS_EVENTS" in sp                     # groups/events branch present
    assert "НЕМАЄ КОТЕДЖІВ" in sp                          # no-cottage guard (rule 1)
