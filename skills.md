# skills.md — Business math & calendar parsing for the D&T Hotel bot

The exact, testable domain knowledge the bot must apply. Numbers below are
illustrative; the live values always come from `pricing.json`. The canonical,
unit-tested implementation of everything here is `scraper/pricing_engine.py`
(price math) and `scraper/bot_logic.py` (availability handling).

---

## 1. Pricing math

### 1.1 Nights
`nights = checkout_date − checkin_date` (integer days). **The checkout day is
never paid and never checked for availability.** Examples:
* `28–29 June` → 1 night (you sleep on the 28th).
* `6–8 July` → 2 nights (you sleep on the 6th and 7th).

### 1.2 Weekday vs. weekend tariff (decided **per night**)
The tariff of a night is set by **the day you sleep on**:
* **"вихідні" (weekend rate):** the night of **Friday** and the night of **Saturday**.
* **"будні" (weekday rate):** Sunday, Monday, Tuesday, Wednesday, Thursday.

A multi-night stay can mix tariffs — compute each night separately and sum.
(`pricing_engine.is_weekend_night`: `date.weekday() in {4,5}` → Fri/Sat.)

2026 anchors used in the User Cases: `2026-06-28` = Sunday (будні);
`2026-07-06` = Monday, `2026-07-07` = Tuesday (both будні).

### 1.3 Base price & capacity
`вартість_кімнати` is the room price **per night** and covers up to
**BASE_CAPACITY = 2** paying guests. If exactly **one** paying guest stays and the
room defines `одномісне_поселення`, that single-occupancy rate replaces
`вартість_кімнати`.

### 1.4 Children & extra places (TIERED — authoritative, owner rule 2026-06-23)
Charged **per night**, **only for guests beyond base capacity (2)**. Half-open
intervals (so the boundary ages are unambiguous):

| Guest | Per-night surcharge |
|---|---|
| Child **0–5** (under 6) | **0** — free, shares parents' bed, takes no paid slot |
| Child **6–11** | `дитяче_місце` (already a 50% extra-bed rate, e.g. 300 грн) |
| Child **12+** or an **extra adult** | `додаткове_місце` (full extra-bed rate) |

Exactly **6** → `дитяче_місце`; exactly **12** → `додаткове_місце`. Fill the base
capacity with the **most expensive** occupants first (adults / 12+), so the cheapest
guests become the charged "extras" — the customer-friendly reading that matches Case 7.
Free (<6) children never consume a paid slot. (Changed 2026-06-23 from the old 0–6 free /
7–12 / >12 boundaries.)

### 1.5 The formula
```
price = Σ over each night [ room_rate(night) + Σ surcharge(extra_guest, night) ]
room_rate(night) = одномісне_поселення  if exactly 1 paying guest and it exists
                   else вартість_кімнати  (at that night's будні/вихідні tariff)
```

### 1.6 Worked examples (must stay true — see `test_pricing.py`)
* **Case 3** — Стандарт +, 28–29 June 2026, 2 adults: 1 будні night, no extras →
  `2400 × 1 = 2400 грн`.
* **Case 7** — Стандарт, 6–8 July 2026, 2 adults + child 8: 2 будні nights, child 8
  → `дитяче_місце` (300) → `(2200 + 300) × 2 = 5000 грн`.
* 3 adults, Стандарт, 1 будні night July → `(2200 + 500) = 2700 грн`
  (`додаткове_місце` 500 for the 3rd adult).
* 2 adults + child 5, any room → child is free → just `вартість_кімнати × nights`.

### 1.7 УБД (combat-veteran) discount — DETERMINISTIC, WHOLE booking (owner 2026-06-23)
* Strict **−20%** off the **entire booking total**: `pricing_engine.apply_military_discount(
  total) = round(total × 0.8)`.
* The extractor sets `ubd:true` for ALL rooms when a veteran is mentioned (the discount
  covers a veteran's whole family). `dialogue_engine.finalize_quote` applies −20% to the
  **grand total across all rooms** (per-room lines stay at full price), shows the
  discounted total + "(з урахуванням знижки УБД -20%)", and appends `MILITARY` (asks for
  at least a copy of the УБД certificate at check-in). Single room: Стандарт July 6–8,
  2 adults = 4400 → **3520** грн. Two rooms 4400 + 5400 = 9800 → **7840** грн.
* The bot offers **no other discount** — the 10% loyalty / length-of-stay discount is
  human-only and is never quoted or computed by the bot.

## 1b. Deterministic guards & flow (NOT left to the LLM)

`process_incoming_message` order: **mute → spam → payment → phone → extraction →
large-group override → route**.
* **Mute (`is_muted`):** conversation has the `Замовлено` label → bot stays silent
  (a human admin owns it).
* **Spam (`is_spam`):** B2B/ads → silent.
* **Payment (`is_payment_intent`):** an attachment (screenshot) OR a completed-payment
  keyword (`оплатив/скинув/квитанція/чек/готово/переказ…`; NOT the bare noun "оплата")
  → send `PAYMENT_RECEIVED_HANDOFF`, add the `Замовлено` label, go silent. The bot
  **never auto-confirms a booking**.
* **Phone (`contains_phone_number`):** ≥9 digits → `PHONE_RECEIVED`, hand to manager.
* **Large group (`looks_like_large_group` / `slots_total_guests` ≥ `LARGE_GROUP_MIN`):**
  20+ people (by text OR consolidated adults+children) / event keyword anywhere →
  force `group_event` → `LARGE_GROUPS_EVENTS` redirect.

### Availability gating (Cases 4 & 5)
`finalize_quote` checks each requested room with `bot_logic.is_room_available`
(`available` / `sold_out` / `unknown`). Sold out + other categories free →
`ROOM_BOOKED`; fully booked → `SOLD_OUT_NEAREST`; a date outside the scrape window →
`unknown` → quote proceeds (never block on missing data).

### Drip consolidation & UX routing
Slots are merged across the WHOLE history; `topic`/`faq_template` follow the LAST
question (except the large-group override). "двоє дорослих" → `adults=2,
children_ages=[]` (no re-asking about kids). Routing (`dialogue_engine.plan`):
* Ask ONLY what's missing — guests known/dates missing → `ASK_DATES_ONLY`; dates
  known/guests missing → `ASK_GUESTS_ONLY`; `QUESTION_ALL_MISSING` only on first contact.
* **FAQ priority** (`bot_logic.faq_override`): a clear FAQ in the current message is
  answered immediately (+ `FAQ_DATE_NUDGE` if booking incomplete).
* **Fuzzy date** (`rooms[].fuzzy_date`, e.g. "початок серпня") → `ACKNOWLEDGE_FUZZY`
  (echo + ask exact dates); fuzzy off-season month → `OFF_SEASON`.
* **Exact dates + guests** = always a calendar quote: chosen room → `finalize_quote`;
  no room → `finalize_quote_all` (prices every available type). The monthly `PRICE_*`
  range templates are no longer used.

## 2. Parsing the hotel calendar JSON ("Шахівниця")

`hotel_scraper.fetch_hotel_availability()` returns, per room category:
```jsonc
{
  "Стандарт +": {
    "total_available": { "2026-06-28": 2, "2026-06-29": 0, ... },  // free physical rooms per DATE
    "rooms": { "28 - Гропа": { "2026-06-28": "Available", "2026-06-29": "Booked", ... }, ... }
  },
  ...
}
```

* **Dates** come from OtelMS `day_id` = **days since 1970-01-01**:
  `date = datetime(1970,1,1) + timedelta(days=int(day_id))`.
* **`total_available[date]`** = how many physical rooms of that category are free
  that night. `0` = sold out that night.
* **`rooms[name][date]`** = `"Available"` / `"Booked"` for each physical room. A
  booking marks every night from `Заїзд` (check-in) up to but **excluding**
  `Виїзд` (check-out).

### 2.1 What the bot consumes
`bot_logic.build_simplified_availability(raw)` reduces the above to
`{room_type: {date: count}}` and **drops blacklisted categories**
(`IGNORE_CATEGORIES = ["Колиба","Басейн","Overbooking"]`). Only this simplified
view is put into the prompt — the bot can never offer a blacklisted entity.

### 2.2 Availability decisions
* **Room free for a stay?** It is bookable only if `total_available[night] > 0`
  for **every** night in `[checkin, checkout)`. If **any** night is `0` → treated
  as **booked** (Case 4/5).
* **Partial overbooking (Case 4):** chosen room is `0`, but other categories are
  `> 0` → offer **only** the genuinely free categories via `ROOM_BOOKED`.
* **Full sold-out (Case 5):** every category `0` on the dates → say everything is
  booked and offer to search nearest dates; on agreement, scan forward from the
  client's dates for the first window where the chosen room is `> 0` on all needed
  nights → `NEAREST_DATES`.

## 3. The 8 User Cases (behavioural contract — `project_spec.md` §6)

| # | Trigger | Scraper? | Expected reply |
|---|---|---|---|
| 1 | First contact, no data | No | Greeting + `[SPLIT]` + `QUESTION_ALL_MISSING` |
| 2 | Dates only, no room | No | Detect month → `PRICE_JUNE/JULY/AUGUST` |
| 3 | Picks a specific room (data complete) | **Yes** | "Секундочку…" then filled `PRICE_CALLCULATION` |
| 4 | Room = 0 but others free | Yes | `ROOM_BOOKED` listing only the free categories |
| 5 | All categories = 0; then "Так" | Yes | Sold-out msg; then forward-scan → `NEAREST_DATES` |
| 6 | Data dripped word-by-word | No | Consolidate in `<THINK>`, never re-ask |
| 7 | Full data + child (extra place) | Yes | Tiered price (Case 7 = 5000) in `PRICE_CALLCULATION` |
| 8 | Changes dates/nights for chosen room | **Yes** | Re-check availability, recompute |

Tests in `test_webhook.py` assert the deterministic parts of each case (intent →
scrape gating, blacklist filtering, sold-out branch, template/greeting/split
plumbing) and exact prices via `pricing_engine`.
