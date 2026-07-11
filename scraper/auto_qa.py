#!/usr/bin/env python3
"""
auto_qa.py — automated self-audit harness for the D&T Hotel bot.

The user has stepped back from manual QA. This script lets an AI/engineer act as
both developer AND lead QA: it drives the REAL deterministic bot flow
(`bot_server.process_incoming_message`) through scripted multi-turn personas with
the Chatwoot API, the extraction LLM and the Playwright scraper all MOCKED, then:

  1. captures the exact transcript the client would see (client + bot messages),
  2. runs per-turn behavioural checks (the expected deterministic reply), and
  3. runs a GLOBAL red-flag audit over every bot message (hallucinated room names,
     a tennis court, a price on sold-out dates, internal sub-type leaks, …).

Because the extractor is mocked with the slots each turn, this exercises the part
we actually own — the deterministic routing / pricing / formatting core — with NO
API key, NO Docker Chatwoot and NO live OtelMS calendar. It is fully offline and
deterministic, so it can run in a tight edit -> audit -> fix loop.

Run (inside the bot-brain container, which carries fastapi/google-genai/playwright):
    docker exec scraper-bot-brain-1 python /app/auto_qa.py
Exit code 0 = 100% green; non-zero = at least one check or red flag failed.
"""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import bot_logic
import templates

try:
    import bot_server
except Exception as exc:                      # pragma: no cover - env guard
    print(f"[FATAL] cannot import bot_server (needs container deps): {exc}")
    sys.exit(2)


# --------------------------------------------------------------------------- #
# Harness: mock the Chatwoot / LLM / scraper boundaries, capture the transcript
# --------------------------------------------------------------------------- #

def _raw(simplified):
    """Wrap {room_type: {date: count}} into the scraper's raw shape."""
    return {rt: {"total_available": d, "rooms": {}} for rt, d in (simplified or {}).items()}


class Harness:
    """Drives one persona (a list of turns) through the real bot flow with mocked I/O."""

    def __init__(self, loop):
        self.loop = loop
        self.sent = []                 # every message the bot sent this persona
        self.added_labels = []
        self.history = []              # accumulated Chatwoot history (client + bot)
        self._labels = []
        self._avail = None
        self._slots_text = "{}"
        self._scraped = False
        self._install_mocks()

    # -- boundary mocks -----------------------------------------------------
    def _install_mocks(self):
        bs = bot_server
        bs.send_chatwoot_message = lambda cid, text: self.sent.append(text)
        bs.get_conversation_labels = lambda cid: list(self._labels)
        bs.add_conversation_label = lambda cid, label: self.added_labels.append(label)
        bs.get_chatwoot_history = lambda cid: list(self.history)

        async def _no_sleep(*_a, **_k):
            return None
        bs.asyncio.sleep = _no_sleep

        async def _fake_llm(prompt, *a, **k):
            return SimpleNamespace(text=self._slots_text)
        bs.generate_with_retry = _fake_llm

        async def _fake_fetch():
            self._scraped = True
            return self._avail
        bs.fetch_hotel_availability = _fake_fetch

    def reset_state(self):
        """Fresh per-conversation state so personas never bleed into each other."""
        bs = bot_server
        for d in (bs.AVAILABILITY_CACHE, bs._conv_seq, bs._slot_memory,
                  bs._pending_window, bs._cooldowns, bs._conv_locks,
                  bs._pending_split, bs._meals_memory):
            d.clear()
        bs._greeted.clear()
        bs._no_dates_mode.clear()
        self.sent.clear()
        self.added_labels.clear()
        self.history.clear()
        self._labels = []
        self._scraped = False

    # -- run one turn -------------------------------------------------------
    def run_turn(self, conv_id, turn):
        """Process one client message; return the NEW bot messages it produced."""
        self._labels = list(turn.get("labels", []))
        self._avail = _raw(turn["avail"]) if turn.get("avail") is not None else None
        self._slots_text = json.dumps(turn.get("slots", {"topic": "greeting", "rooms": []}))
        self._scraped = False

        # A drip burst: earlier client messages already sat in Chatwoot (unanswered) when
        # the final one is processed. They must be in history BUT not the just-arrived one.
        for pre in turn.get("burst", []):
            self.history.append({"id": len(self.history) + 1,
                                 "message_type": "incoming", "content": pre})

        before = len(self.sent)
        client = turn["client"]
        self.loop.run_until_complete(
            bot_server.process_incoming_message(client, conv_id, turn.get("attach", False)))
        new_msgs = self.sent[before:]

        # Reflect this turn into history for the next turn (client THEN bot replies).
        self.history.append({"id": len(self.history) + 1,
                             "message_type": "incoming", "content": client})
        for m in new_msgs:
            self.history.append({"id": len(self.history) + 1,
                                 "message_type": "outgoing", "content": m})
        return new_msgs, self._scraped


# --------------------------------------------------------------------------- #
# Check helpers (predicates over a turn's joined bot output)
# --------------------------------------------------------------------------- #

def has(sub):
    return (lambda out, sub=sub: sub in out)


def lacks(sub):
    return (lambda out, sub=sub: sub not in out)


def order(a, b):
    """a appears before b in the transcript (both must appear)."""
    def _pred(out, a=a, b=b):
        return a in out and b in out and out.index(a) < out.index(b)
    return _pred


# --------------------------------------------------------------------------- #
# GLOBAL red-flag audit — runs over EVERY bot message across all personas
# --------------------------------------------------------------------------- #

_HALLUCINATION_TOKENS = ["Хом", "Боярин", "Гропа", "Баба Людова", "11 номер"]
_TENNIS_COURT_TOKENS = ["тенісн", "тенісний корт", "корт"]


def red_flags(message: str):
    """Return a list of red-flag descriptions for a single bot message (empty = clean)."""
    flags = []
    for tok in _HALLUCINATION_TOKENS:
        if tok in message:
            flags.append(f"internal/hallucinated room name «{tok}»")
    for tok in _TENNIS_COURT_TOKENS:
        if tok in message:                       # "настільний теніс" is fine (no "тенісн"/"корт")
            flags.append(f"nonexistent tennis court «{tok}»")
    return flags


# --------------------------------------------------------------------------- #
# PERSONAS — scripted multi-turn conversations + expected deterministic replies
# --------------------------------------------------------------------------- #

AVAIL_JULY = {"Стандарт": {f"2026-07-{d:02d}": 3 for d in range(1, 15)},
              "Стандарт +": {f"2026-07-{d:02d}": 3 for d in range(1, 15)},
              "Напівлюкс": {f"2026-07-{d:02d}": 1 for d in range(1, 15)}}

PERSONAS = [
    # ---- PHASE-2 FIX 1: DISCOUNTS FAQ -----------------------------------
    {"name": "Знижки — загальне питання (Fix 1)", "conv": 9001, "turns": [
        {"client": "Доброго дня! Є якісь знижки у вас?",
         "slots": {"topic": "faq", "faq_template": "GENERAL_INFORMATION", "rooms": []},
         "expect": [("lists child discount", has("знижки для дітей")),
                    ("lists military discount", has("військовослужбовців")),
                    ("does NOT describe rooms instead", lacks("У нас 3 типи номерів"))]},
    ]},

    # ---- PHASE-2 FIX 2: compound fuzzy period must not drop the 2nd month ----
    {"name": "Дві половини (липень+серпень) — date horizon (Fix 2)", "conv": 9002, "turns": [
        {"client": "друга половина липня або після 6 серпня, на двох",
         "slots": {"topic": "fuzzy_dates", "rooms": [
             {"room_type": None, "fuzzy_date": "друга половина липня або після 6 серпня",
              "nights": None, "checkin": None, "checkout": None, "adults": 2, "children_ages": []}]},
         "avail": {"Стандарт": {**{f"2026-07-{d:02d}": 0 for d in range(16, 32)},
                                **{f"2026-08-{d:02d}": 2 for d in range(6, 14)}}},
         "expect": [("scanned & proposed a window", has("вільні віконця")),
                    ("August NOT dropped", has("серпня"))]},
    ]},

    # ---- PHASE-2 FIX 3: multiple FAQs + a quote, in logical order ---------
    {"name": "Драбина FAQ + бронювання (Fix 3 ordering)", "conv": 9003, "turns": [
        {"client": "Стандарт на 6-7 липня для двох",
         "burst": ["Де ви знаходитесь?", "а басейн з підігрівом?"],
         "slots": {"topic": "price_quote", "rooms": [
             {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-07",
              "adults": 2, "children_ages": []}]},
         "avail": {"Стандарт": {"2026-07-06": 3}},
         "expect": [("greeting first", has("Доброго дня! Вас вітає")),
                    ("answered location FAQ", has("серці Карпат")),
                    ("answered pool FAQ", has("працює щодня")),
                    ("produced the quote", has("буде вартувати")),
                    ("location before quote", order("серці Карпат", "буде вартувати")),
                    ("pool before quote", order("працює щодня", "буде вартувати"))]},
    ]},

    # ---- Case 3: deterministic price (the July-5 weekday fix) -------------
    {"name": "Точний розрахунок Стандарт 5-7 липня", "conv": 9004, "turns": [
        {"client": "вітаю", "slots": {"topic": "greeting", "rooms": []},
         "expect": [("asks for details", has("Підкажіть"))]},
        {"client": "Стандарт, 5-7 липня, 2 дорослих",
         "slots": {"topic": "price_quote", "rooms": [
             {"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
              "adults": 2, "children_ages": []}]},
         "avail": AVAIL_JULY,
         "expect": [("2 будні ночі = 4400", has("4400 грн")),
                    ("NOT the weekend rate", lacks("2500")),
                    ("checked availability", has("Секундочку"))]},
    ]},

    # ---- Case 7: strict child math (8 y.o. -> дитяче_місце) ---------------
    {"name": "Дитина 8 років — дитяче місце", "conv": 9005, "turns": [
        {"client": "Стандарт, 6-8 липня, 2 дорослих і дитина 8 років",
         "slots": {"topic": "price_quote", "rooms": [
             {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-08",
              "adults": 2, "children_count": 1, "children_ages": [8]}]},
         "avail": AVAIL_JULY,
         "expect": [("(2200+300)*2 = 5000", has("5000 грн")),
                    ("child-place discount noted", has("знижку 50% на дитяче місце"))]},
    ]},

    # ---- Children boundary: age 12 -> full extra place --------------------
    {"name": "Дитина 12 років — додаткове місце", "conv": 9006, "turns": [
        {"client": "Стандарт, 6-8 липня, 2 дорослих і дитина 12 років",
         "slots": {"topic": "price_quote", "rooms": [
             {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-08",
              "adults": 2, "children_count": 1, "children_ages": [12]}]},
         "avail": AVAIL_JULY,
         "expect": [("12 y.o. is a full place, not 50%", lacks("знижку 50%")),
                    ("quoted", has("буде вартувати"))]},
    ]},

    # ---- УБД -20% on the WHOLE booking -----------------------------------
    {"name": "УБД -20% на все бронювання", "conv": 9007, "turns": [
        {"client": "Стандарт, 6-8 липня, 2 дорослих, я УБД",
         "slots": {"topic": "price_quote", "rooms": [
             {"room_type": "Стандарт", "checkin": "2026-07-06", "checkout": "2026-07-08",
              "adults": 2, "children_ages": [], "ubd": True}]},
         "avail": AVAIL_JULY,
         "expect": [("shows the discounted total", has("УБД -20%")),
                    ("appends the certificate note", has("посвідчення УБД"))]},
    ]},

    # ---- Case 5: full sold-out -> offer the nearest window (never a price) ----
    {"name": "Sold-out -> найближче вікно", "conv": 9008, "turns": [
        {"client": "Стандарт 5-7 липня, 2 дорослих",
         "slots": {"topic": "price_quote", "rooms": [
             {"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
              "adults": 2, "children_ages": []}]},
         "avail": {"Стандарт": {"2026-07-05": 0, "2026-07-06": 0,
                                "2026-07-08": 2, "2026-07-09": 2}},
         "expect": [("offers the nearest window", has("найближче вільне віконце")),
                    ("never quotes a price", lacks("буде вартувати"))]},
    ]},

    # ---- Case 4: partial overbooking -> cross-sell the free category ------
    {"name": "Частковий overbooking -> інша категорія", "conv": 9009, "turns": [
        {"client": "Напівлюкс 5-7 липня, 2 дорослих",
         "slots": {"topic": "price_quote", "rooms": [
             {"room_type": "Напівлюкс", "checkin": "2026-07-05", "checkout": "2026-07-07",
              "adults": 2, "children_ages": []}]},
         "avail": {"Стандарт": {"2026-07-05": 2, "2026-07-06": 2},
                   "Напівлюкс": {"2026-07-05": 0, "2026-07-06": 0}},
         "expect": [("offers the free Стандарт", has("Стандарт")),
                    ("says the chosen one is booked", has("заброньовані")),
                    ("no price for the sold-out room", lacks("буде вартувати"))]},
    ]},

    # ---- Case 10: multi-room total ---------------------------------------
    {"name": "Два номери — сума", "conv": 9010, "turns": [
        {"client": "Хочемо два номери на 5-7 липня: Стандарт (2 дор.) і Напівлюкс (2 дор. + дитина 8)",
         "slots": {"topic": "price_quote", "rooms": [
             {"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
              "adults": 2, "children_ages": []},
             {"room_type": "Напівлюкс", "checkin": "2026-07-05", "checkout": "2026-07-07",
              "adults": 2, "children_ages": [8]}]},
         "avail": AVAIL_JULY,
         "expect": [("Стандарт line", has("4400 грн")),
                    ("Напівлюкс line", has("6000 грн")),
                    ("grand total", has("Загальна вартість: 10400 грн"))]},
    ]},

    # ---- Case 9: large group (20+) redirected, no scrape ------------------
    {"name": "Велика група 20+ -> перенаправлення", "conv": 9011, "turns": [
        {"client": "нас 25, корпоратив",
         "slots": {"topic": "group_event", "rooms": []},
         "expect": [("redirected to co-owner", has("0673445220")),
                    ("no room math", lacks("буде вартувати"))]},
    ]},

    # ---- 6+ guests in one room -> proactively propose a valid split (owner #21) -----
    {"name": "6+ в одному номері -> пропозиція розподілу", "conv": 9012, "turns": [
        {"client": "нас 6, на 6-8 липня",
         "slots": {"topic": "price_quote", "rooms": [
             {"room_type": None, "checkin": "2026-07-06", "checkout": "2026-07-08",
              "adults": 6, "children_count": 0, "children_ages": []}]},
         "expect": [("shows capacities first", has("максимум 3 дорослих")),
                    ("proposes a valid split", has("розподілити")),
                    ("2 rooms of 3", order("2 номери", "3 + 3")),
                    ("no premature quote", lacks("буде вартувати"))]},
    ]},

    # ---- Case 11: off-season -> holding message + no price ----------------
    {"name": "Міжсезоння (травень) -> узгоджується", "conv": 9013, "turns": [
        {"client": "Стандарт 11-13 травня, 2 дорослих",
         "slots": {"topic": "price_quote", "rooms": [
             {"room_type": "Стандарт", "checkin": "2026-05-11", "checkout": "2026-05-13",
              "adults": 2, "children_ages": []}]},
         "expect": [("says price is being agreed", has("узгоджується")),
                    ("no price", lacks("буде вартувати"))]},
    ]},

    # ---- Payment hand-off: never auto-confirm (owner #22 2026-07-09) ------
    {"name": "Оплата -> хендоф менеджеру + мут", "conv": 9014, "turns": [
        {"client": "Оплатив, ось квитанція",
         "slots": {"topic": "greeting", "rooms": []},
         "expect": [("hands off to manager", has("менеджер")),
                    ("says manager will verify payment", has("перевірить оплату")),
                    ("does NOT auto-confirm the booking", lacks("буде вартувати"))]},
        {"client": "а коли буде підтвердження?",   # bot is now MUTED (Замовлено) -> silent
         "slots": {"topic": "greeting", "rooms": []},
         "labels": ["Замовлено"],
         "expect": [("stays silent while muted", lambda out: out == "")]},
    ]},

    # ---- Barter/blogger: silent + tag ------------------------------------
    {"name": "Блогер/бартер -> тиша + тег", "conv": 9015, "turns": [
        {"client": "Вітаю! Я блогер, пропоную бартер — рілс за проживання",
         "slots": {"topic": "greeting", "rooms": []},
         "expect": [("stays silent", lambda out: out == "")]},
    ]},

    # ---- Spam: silent -----------------------------------------------------
    {"name": "Спам (SMM) -> повна тиша", "conv": 9016, "turns": [
        {"client": "Пропоную просування сторінки, є пробний тариф",
         "slots": {"topic": "greeting", "rooms": []},
         "expect": [("stays silent", lambda out: out == "")]},
    ]},

    # ---- Pure thanks: always the last word -------------------------------
    {"name": "Подяка -> тепле закриття", "conv": 9017, "turns": [
        {"client": "Дякую!", "slots": {"topic": "greeting", "rooms": []},
         "expect": [("warm close", has("Гарного дня")),
                    ("not silent", lambda out: out != "")]},
    ]},

    # ---- Payment-rules question -> BOOK_ROOM (deterministic) --------------
    {"name": "«Оплата по приїзду?» -> правила передоплати", "conv": 9018, "turns": [
        {"client": "Чи можна по приїзду оплатити повністю?",
         "slots": {"topic": "faq", "faq_template": "CHECK_IN_OUT", "rooms": []},
         "expect": [("prepayment rules (IBAN)", has("аванс")),
                    ("not the check-in time answer", lacks("Заїзд у нас з 14:00"))]},
    ]},

    # ---- Owner fix #278: children's pool question -> dedicated CHILDREN_POOL answer ----
    {"name": "Дитячий басейн -> окрема відповідь (fix #278)", "conv": 9019, "turns": [
        {"client": "Чи є у вас дитячий басейн?",
         "slots": {"topic": "faq", "faq_template": "POOL", "rooms": []},
         "expect": [("answers about the children's pool", has("дитячий басейн")),
                    ("gives size & depth", has("3х2")),
                    ("gives temperature", has("28"))]},
    ]},

    # ---- Owner fix #282-284: split with >3 adults in a room -> suggest a valid split ----
    {"name": "7 дорослих: 4 і 3 -> валідний розподіл (fix #282-284)", "conv": 9020, "turns": [
        {"client": "На 23-24 липня, нас 7 дорослих",
         "slots": {"topic": "price_quote", "rooms": [
             {"room_type": None, "checkin": "2026-07-23", "checkout": "2026-07-24",
              "adults": 7, "children_count": 0, "children_ages": []}]},
         "avail": {"Стандарт": {"2026-07-23": 5, "2026-07-24": 5}},
         "expect": [("proposes a valid split first", has("розподілити")),
                    ("shows capacities", has("максимум 3 дорослих"))]},
        {"client": "Давайте 2 номери: в одному 4, в іншому 3",
         "slots": {"topic": "price_quote", "rooms": [
             {"room_type": None, "checkin": None, "checkout": None, "adults": 4, "children_ages": []},
             {"room_type": None, "checkin": None, "checkout": None, "adults": 3, "children_ages": []}]},
         "avail": {"Стандарт": {"2026-07-23": 5, "2026-07-24": 5}},
         "expect": [("max-3-adults explained", has("максимум 3 дорослих")),
                    ("suggests 3 rooms", has("3 номери")),
                    ("shows an even distribution", has("2 + 2 + 3")),
                    ("never quotes the invalid room", lacks("буде вартувати"))]},
    ]},
]


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def run() -> int:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    h = Harness(loop)

    total_checks = passed_checks = 0
    persona_fails = []
    all_bot_messages = []       # (persona, message) for the global red-flag audit

    for persona in PERSONAS:
        h.reset_state()
        print("\n" + "=" * 78)
        print(f"PERSONA: {persona['name']}  (conv {persona['conv']})")
        print("-" * 78)
        for turn in persona["turns"]:
            for pre in turn.get("burst", []):
                print(f"  👤 (burst) {pre}")
            new_msgs, scraped = h.run_turn(persona["conv"], turn)
            print(f"  👤 {turn['client']}")
            if not new_msgs:
                print("  🤖 (мовчить)")
            for m in new_msgs:
                for line in m.splitlines() or [""]:
                    print(f"  🤖 {line}")
                all_bot_messages.append((persona["name"], m))
            out = "\n".join(new_msgs)
            for desc, pred in turn.get("expect", []):
                total_checks += 1
                ok = False
                try:
                    ok = bool(pred(out))
                except Exception as e:       # a check that errors is a failure
                    ok = False
                    desc = f"{desc} [predicate error: {e}]"
                if ok:
                    passed_checks += 1
                else:
                    persona_fails.append((persona["name"], turn["client"], desc, out))
                print(f"     {'✅' if ok else '❌'} {desc}")

    # Global red-flag audit over every bot message emitted anywhere.
    print("\n" + "=" * 78)
    print("GLOBAL RED-FLAG AUDIT (hallucinations / tennis court / internal names)")
    print("-" * 78)
    flag_hits = []
    for persona_name, msg in all_bot_messages:
        for flag in red_flags(msg):
            flag_hits.append((persona_name, flag, msg))
    if flag_hits:
        for persona_name, flag, msg in flag_hits:
            print(f"  ❌ [{persona_name}] {flag}\n       in: {msg[:120]}")
    else:
        print(f"  ✅ scanned {len(all_bot_messages)} bot messages — no red flags")

    # Summary
    print("\n" + "=" * 78)
    print("AUTO-QA SUMMARY")
    print("-" * 78)
    print(f"  behavioural checks: {passed_checks}/{total_checks} passed")
    print(f"  red flags:          {len(flag_hits)}")
    green = (passed_checks == total_checks) and not flag_hits
    if not green:
        print("\n  FAILURES:")
        for name, client, desc, out in persona_fails:
            print(f"   - [{name}] on «{client}»: {desc}")
            print(f"       got: {out[:160]!r}")
    print("\n  RESULT:", "🟢 ALL GREEN" if green else "🔴 NOT GREEN")
    return 0 if green else 1


if __name__ == "__main__":
    sys.exit(run())
