# CLAUDE.md — Direct Manager ("D&T Hotel" AI assistant)

Persistent operating guide for any AI/engineer working on this repository. Read
this first, then `skills.md` (business math) and `project_spec.md` (source of
truth for behaviour). When this file and `project_spec.md` ever disagree, fix the
disagreement explicitly — do not silently pick one.

---

## 1. What this project is

An AI sales assistant for **D&T Hotel** (Carpathians), integrated with the
**Chatwoot** omnichannel platform. It greets clients, presents rooms, checks live
availability by scraping the hotel's OtelMS "Шахівниця" (chessboard) calendar, and
quotes prices by strict business rules. The bot **composes pre-written templates**;
it does not free-write marketing text.

Flow: `Chatwoot webhook -> FastAPI /webhook -> intent pre-filter (LLM) -> optional
Playwright scrape -> main LLM (State Machine) -> reply POSTed back to Chatwoot`.

## 2. Project structure

```
scraper/                     # the deployed application
  bot_server.py              # FastAPI core: webhook, the LLM EXTRACTION call, deterministic routing, Chatwoot I/O, cache
  dialogue_engine.py         # PURE deterministic reply builder: plan() + finalize_quote() + the rigid quote format
  bot_logic.py               # PURE helpers: availability filter/gate, spam/phone/large-group detection, [SPLIT], greeting
  pricing_engine.py          # PURE deterministic price math — dates/weekday/total/multi-room/off-season (tested)
  templates.py               # knowledge base: every client-facing text template (FAQ, prices, redirects, closes)
  hotel_scraper.py           # Playwright scraper of the OtelMS calendar -> availability JSON
  pricing.json               # price + rules config (per room type / month / weekday-weekend)
  test_pricing.py            # unit tests for pricing_engine (exact UAH numbers, multi-room, off-season)
  test_dialogue_engine.py    # unit tests for the deterministic reply builder (format, gating, planning)
  test_webhook.py            # Layer A helpers + Layer B live-flow e2e (extraction mocked)
  Dockerfile                 # bot-brain image (python:3.11-slim + Playwright Chromium)
  docker-compose.yaml        # full stack: bot-brain + Chatwoot (rails, sidekiq, postgres, redis)
  .env                       # secrets (GITIGNORED — never commit)
etc/                         # legacy / scratch helpers and the deprecated manual test.html
project_spec.md              # ТЗ: architecture, business rules, User Cases (behavioural source of truth)
context.txt                  # 1.1 MB raw development chat log (UNTRACKED — do NOT commit; may contain secrets)
```

## 3. Architecture: LLM extracts, Python decides (deterministic core)

The LLM is reduced to **extraction/classification only**. It NEVER computes a
price, a day-of-week, or a total, and NEVER writes the client-facing text.

```
incoming → spam? phone? (deterministic guards) → LLM EXTRACTION (returns JSON slots)
         → deterministic large-group override → route:
             simple topic  → fixed template (FAQ / redirect / thinking / close)
             booking/price → dialogue_engine.plan() → (if quote) scrape →
                             dialogue_engine.finalize_quote()  [availability gate → engine → exact format]
```

* **Extraction** (`bot_server.EXTRACTION_PROMPT`) returns `{topic, rooms[], faq_template}`.
  Slots (dates/nights/guests/room) are consolidated from the **whole** dialogue
  (drip handling). Parsed robustly by `dialogue_engine.parse_slots` (bad JSON →
  safe "greeting" fallback).
* **All money/calendar/format logic is `dialogue_engine` + `pricing_engine`** — tested,
  deterministic. This is why the bot can no longer mis-date a night or invent a price.
* `[SPLIT]` → `bot_logic.split_messages`; first turn prepends the greeting via
  `bot_logic.prepend_greeting_if_needed`. Replies are UA by construction (templates
  / Python-formatted), so no language drift.

## 4. Core algorithm rules (do not break)

1. **Extraction decides routing; Python decides everything else.** The scrape runs
   only on a deterministic scrape action (`plan()` returns one of
   `quote / quote_all / explore / nearest`): exact dates+guests, or a fuzzy period +
   guests, in a priced month. greeting / group / off-season / pure-fuzzy-no-guests /
   thinking never scrape. **FAQ:** answered from templates and normally does NOT
   scrape — BUT if the same message also carries an *actionable* booking intent
   (`plan()` → a scrape action: e.g. "липень, 4 особи, чи є трансфер?"), the bot
   answers the FAQ **and then executes the scan**, appending the real calendar result
   (windows / room options / quote). The generic `FAQ_CONTINUE_NUDGE` is appended ONLY
   when the booking action is a *question* (missing data). Availability for the FAQ+scan
   path comes from a warm cache when present (no double scrape), else a fresh scrape.
1b. **Availability gating (mandatory):** `finalize_quote` checks the calendar FIRST
   and **never quotes a sold-out room**. Sold out + other categories free →
   `ROOM_BOOKED` (Case 4); fully booked → `SOLD_OUT_NEAREST` (Case 5). A date
   **outside** the scrape window is `unknown` (not sold out) → quote proceeds.
   Large groups (20+ guests, by text or consolidated slot count) / events are
   redirected deterministically (`bot_logic.looks_like_large_group` +
   `bot_logic.slots_total_guests` ≥ `LARGE_GROUP_MIN`), not left to the LLM.
1c. **Guard order in `process_incoming_message`:** `mute (Замовлено label) → spam →
   payment → phone → extraction → large-group override → route`. The first four are
   deterministic short-circuits (no LLM).
1d. **Payment = human hand-off, never auto-confirm.** A screenshot attachment OR a
   completed-payment keyword (`bot_logic.is_payment_intent`) → send
   `PAYMENT_RECEIVED_HANDOFF`, add the `Замовлено` label, go silent. If a
   conversation already has `Замовлено` (`bot_logic.is_muted`) the bot ignores ALL
   messages (a human owns it).
1e. **УБД −20% (deterministic, WHOLE booking):** the extractor sets `ubd:true` for ALL
   rooms when a veteran is mentioned; `finalize_quote` applies
   `pricing_engine.apply_military_discount` (round(total×0.8)) to the **entire booking
   total** (a veteran's family — every room), shows the discounted grand total
   "(з урахуванням знижки УБД -20%)", and appends the `MILITARY` template (which asks for
   at least a copy of the УБД certificate at check-in). The bot offers NO other discount —
   the 10% loyalty / length-of-stay discount is human-only and never quoted by the bot.
2. **Nights = checkout − checkin.** The checkout day is never charged and never
   checked for availability.
3. **Weekend nights = Friday & Saturday** (тариф "вихідні"); Sun–Thu = "будні".
   Decided **per night**.
4. **Children / extra places** are tiered by age — see `skills.md` §1 and
   `project_spec.md` §5. **Owner rule 2026-06-23:** 0–5 free, 6–11 `дитяче_місце`,
   12+/extra adult `додаткове_місце`, per night, only beyond base capacity 2 (for EVERY
   room type, Напівлюкс included). The boundaries are half-open: exactly **6** is
   `дитяче_місце`, exactly **12** is `додаткове_місце`.
5. **Blacklist filter.** `bot_logic.IGNORE_CATEGORIES = ["Колиба","Басейн","Overbooking"]`
   are force-removed from scraper output — never offered as rooms.
6. **Templates only.** Replies are copied verbatim from `templates.py`; the model
   fills `{placeholders}`, it does not invent prose.
7. **Long-term memory.** Consolidate slots from the entire history; never re-ask
   something the client already provided, even if given one word at a time.
8. **Owner decisions 2026-06-23 (deterministic):**
   * **6+ guests in one room → split first.** `dialogue_engine.plan` returns
     `ASK_ROOM_DISTRIBUTION` before any quote when a single room object holds ≥6 guests
     (can't fit one room; max ~5). An explicit multi-room request (>1 room) proceeds.
   * **Family of 4–5 (with children) → recommend.** `finalize_quote_all` prioritises
     Напівлюкс and offers a two-room split as the roomier alternative (still only the 3
     public names).
   * **Off-season ≠ rejection.** `OFF_SEASON` now says the price is "ще узгоджується" and
     `bot_server` tags the conversation `Instagram` for a human follow-up (no phone ask).
   * **Barter/PR collab is wanted, not spam.** `bot_logic.is_barter` (checked BEFORE spam)
     and the extractor `topic=barter` → bot stays SILENT but adds the `Instagram` label so
     an operator handles the deal.
   * **Solo → `одномісне_поселення` always.** **Pets** = 300 грн/night (cats too).
     **Check-in 14:00 / out 12:00**, early/late free if the room is free (`CHECK_IN_OUT`).
     **NEVER** mention a tennis court (it doesn't exist; "настільний теніс" = ping-pong, OK).

## 5. Coding guidelines

* **Keep math in `pricing_engine.py`, decisions in `bot_logic.py`.** Both are pure
  (stdlib only) and unit-tested. `bot_server.py` should import and call them rather
  than re-implement logic inline — this keeps the live server and the tests on the
  same code path.
* **No heavy deps in the pure modules.** `pricing_engine.py` and `bot_logic.py`
  must import only the standard library so the test suite runs without FastAPI /
  google-genai / Playwright.
* **Determinism over LLM arithmetic.** History shows the LLM repeatedly mis-added
  child beds and counted the checkout day. Any new price rule must land in
  `pricing_engine.py` with a test *and* be mirrored in the `sys_prompt` wording.
* **Never break the 8 User Cases** in `project_spec.md` §6. Add a test before
  changing behaviour they depend on.
* **Secrets stay in `.env`** (gitignored). Never hard-code keys/tokens. Never
  commit `.env` or `context.txt`.
* **Language:** templates and prompts are Ukrainian; code identifiers/comments may
  mix Ukrainian (domain terms like `вартість_кімнати`) and English.
* **Run the tests** (`pytest scraper/`) before committing. Pricing tests must be
  green; webhook tests that need the heavy deps run inside the `bot-brain`
  container.

## 6. Auto-approval / autonomy policy

This repo is configured for autonomous development. An agent may, **without asking**:

* Create / modify / delete files under this repository.
* Run terminal commands, including `docker compose up/down/restart/build`.
* Run the test suite and iterate (fix → restart `bot-brain` → re-test) until green.
* Stage, commit, and push to the configured git remote (`origin`,
  `github.com/Muskam09/DM.git`) using the **`git` CLI** (there is no GitHub MCP /
  `gh` in this environment).

**Pause and ask the human only when:**

* A required secret / API key is missing or invalid (e.g. `GEMINI_API_KEY`,
  OtelMS / Chatwoot creds) and cannot be recovered.
* An **unrecoverable** system/tooling error blocks progress.
* The spec, the config, and the live code give **contradictory business rules**
  (e.g. the children-pricing conflict resolved on 2026-06-16) — surface it, do not
  guess, because it changes money math and persistent artifacts.

**Always, even in autonomous mode:** never commit secrets or `context.txt`; never
push code whose tests are red; report failures honestly (paste real output).
