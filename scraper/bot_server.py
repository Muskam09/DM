import os
import time
import asyncio
import requests
import uvicorn
from datetime import date
from fastapi import FastAPI, Request, BackgroundTasks
from google import genai
from dotenv import load_dotenv

import templates
import bot_logic
import dialogue_engine
from hotel_scraper import fetch_hotel_availability

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CHATWOOT_URL = os.getenv("CHATWOOT_URL", "http://rails:3000")
CHATWOOT_TOKEN = os.getenv("CHATWOOT_TOKEN")
ACCOUNT_ID = os.getenv("CHATWOOT_ACCOUNT_ID", "1")

ai_client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_NAME = 'gemini-2.5-flash-lite'

app = FastAPI()

AVAILABILITY_CACHE = {}
CACHE_TTL = 900

def send_chatwoot_message(conversation_id: int, message_text: str):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id}/messages"
    headers = {
        "api_access_token": CHATWOOT_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {
        "content": message_text.strip(),
        "message_type": "outgoing",
        "private": False
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=5)
        if response.status_code == 200:
            print(f"[->] Успішно відправлено: {message_text[:40]}...")
        else:
            print(f"[-] Chatwoot ВІДХИЛИВ! Статус: {response.status_code}")
    except Exception as e:
        print(f"[-] Помилка мережі: {e}")

def get_chatwoot_history(conversation_id: int):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id}/messages"
    headers = {
        "api_access_token": CHATWOOT_TOKEN,
        "Content-Type": "application/json"
    }
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                return sorted(data, key=lambda x: x.get("id", 0))
            elif isinstance(data, dict):
                messages = data.get("payload", []) or data.get("messages", []) or data.get("data", [])
                return sorted(messages, key=lambda x: x.get("id", 0))
    except Exception as e:
        print(f"[-] Помилка історії: {e}")
    return []

def get_conversation_labels(conversation_id: int):
    """Return the list of Chatwoot labels on a conversation (empty on error)."""
    url = f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id}/labels"
    headers = {"api_access_token": CHATWOOT_TOKEN, "Content-Type": "application/json"}
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            return response.json().get("payload", []) or []
    except Exception as e:
        print(f"[-] Помилка читання міток: {e}")
    return []

def add_conversation_label(conversation_id: int, label: str):
    """Add a label to a conversation (Chatwoot replaces the full set, so we union)."""
    url = f"{CHATWOOT_URL}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id}/labels"
    headers = {"api_access_token": CHATWOOT_TOKEN, "Content-Type": "application/json"}
    labels = get_conversation_labels(conversation_id)
    if label not in labels:
        labels = labels + [label]
    try:
        response = requests.post(url, json={"labels": labels}, headers=headers, timeout=5)
        if response.status_code in (200, 201):
            print(f"[->] Мітку '{label}' додано до конверсації {conversation_id}")
        else:
            print(f"[-] Chatwoot відхилив мітку! Статус: {response.status_code}")
    except Exception as e:
        print(f"[-] Помилка додавання мітки: {e}")

async def get_hotel_data_cached(conversation_id: int):
    now = time.time()
    if conversation_id in AVAILABILITY_CACHE:
        cached_data, timestamp = AVAILABILITY_CACHE[conversation_id]
        if now - timestamp < CACHE_TTL:
            return cached_data
            
    print("[*] Запускаємо Playwright...")
    await asyncio.to_thread(send_chatwoot_message, conversation_id, "Секундочку, перевіряю доступність номеру та актуальні ціни на ці дати… 🗓️")
    
    try:
        data = await fetch_hotel_availability()
        AVAILABILITY_CACHE[conversation_id] = (data, now)
        return data
    except Exception as e:
        print(f"[-] Помилка скрапера: {e}")
        return None


def peek_cached_availability(conversation_id: int):
    """Return fresh cached availability WITHOUT scraping (no Playwright, no
    "Секундочку"). Lets an FAQ reply be combined with a pending booking answer when
    a scrape already ran this conversation (Bug 1: an FAQ during a scrape must not
    drop the calendar result)."""
    entry = AVAILABILITY_CACHE.get(conversation_id)
    if not entry:
        return None
    data, ts = entry
    if time.time() - ts < CACHE_TTL:
        return data
    return None

def _is_retryable_llm_error(exc) -> bool:
    """Transient Gemini errors worth retrying (overload / rate limit / timeout)."""
    s = str(exc).lower()
    return any(k in s for k in (
        "503", "429", "unavailable", "resource_exhausted", "overloaded",
        "high demand", "timeout", "timed out", "deadline", "internal error", "500"))


async def generate_with_retry(prompt: str, retries: int = 3, base_delay: int = 2):
    """Call the extractor LLM with exponential backoff on TRANSIENT failures (503/429/…):
    wait 2s, then 4s, then give up. Non-transient errors raise immediately so the caller
    can degrade gracefully (ERROR_LLM_DOWN) without wasting time."""
    for attempt in range(retries):
        try:
            return await ai_client.aio.models.generate_content(
                model=MODEL_NAME, contents=prompt)
        except Exception as e:
            if attempt < retries - 1 and _is_retryable_llm_error(e):
                wait = base_delay * (2 ** attempt)        # 2s, 4s
                print(f"[i] LLM transient error ({str(e)[:60]}) -> retry {attempt + 1}/{retries - 1} in {wait}s")
                await asyncio.sleep(wait)
            else:
                raise

# The LLM does EXTRACTION ONLY — it never computes prices, dates or weekdays and
# never writes the client-facing text. It returns structured slots as JSON; all
# pricing/availability/formatting is handled deterministically in dialogue_engine.
EXTRACTION_PROMPT = """Ти — аналізатор повідомлень готелю D&T Hotel. Сьогодні: %%TODAY%%. Рік бронювань: 2026.
Прочитай ВСЮ історію діалогу і нове повідомлення та ОБ'ЄДНАЙ дані з УСІХ повідомлень
(клієнт часто пише по частинах: дати в одному, людей у другому, номер у третьому).

Поверни ВИКЛЮЧНО JSON (без markdown, без пояснень) такої структури:
{
  "topic": "<один з: price_quote | general_price | faq | presentation | group_event | thinking | reject_dates | booking_confirm | fuzzy_dates | nearest_dates | greeting | unknown>",
  "rooms": [ {"room_type": "<Стандарт|Стандарт +|Напівлюкс|null>", "checkin": "YYYY-MM-DD|null", "checkout": "YYYY-MM-DD|null", "fuzzy_date": "<текст нечіткого періоду|null>", "nights": <ціле|null>, "adults": <ціле>, "children_count": <ціле>, "children_ages": [<вік>, ...], "ubd": <true|false>} ],
  "faq_template": "<POOL|PETS|SAUNA_VATS|FOOD_PRICES|TRANSFER_PARKING|HOW_TO_GET_THERE|ROOM_AMENITIES|SMOKING|PLACE|BOOK_ROOM|MILITARY|CHILDREN|BAR|GUEST_POOL|KITCHEN|INCLUDED_IN_THE_PRICE|BREAKFAST_IN_THE_PRICE|GENERAL_INFORMATION|null>"
}

Дати/гостей/номери збирай з УСІЄЇ історії. Якщо обрано topic=faq — faq_template обирай за ОСТАННІМ (поточним) питанням клієнта.
ВАЖЛИВО (анти-амнезія): на БУДЬ-ЯКОМУ ході (не лише FAQ) ОБОВ'ЯЗКОВО заповнюй rooms[] усіма вже відомими даними бронювання (дати, гості, ночі, номер, нечіткий період) з УСІЄЇ історії. Відповідь на FAQ НЕ повинна стирати раніше надані клієнтом дані. Якщо клієнт раніше назвав дати — ЗБЕРІГАЙ їх у checkin/checkout, навіть якщо ці дати виявились зайняті або бот запропонував інші; скидай/змінюй дату ЛИШЕ коли клієнт сам назве нову.

Значення topic:
- group_event (НАЙВИЩИЙ ПРІОРИТЕТ): якщо у БУДЬ-ЯКОМУ повідомленні (навіть ранньому) згадано 20+ осіб (сумарно дорослі+діти) / велику групу / табір / тур / спортивні збори / весілля / банкет / корпоратив / захід — ЗАВЖДИ став topic=group_event, незалежно від того, про що останнє повідомлення.
- price_quote: клієнт обрав КОНКРЕТНИЙ номер і є дати+гості, АБО змінює дати/ночі для вже обраного номеру. ВАЖЛИВО: будь-яка ЗМІНА або НАЗИВАННЯ дат — це ОНОВЛЕННЯ бронювання (price_quote), а НЕ відмова. Фрази "давайте змінимо дати", "наші дати змінились", "змінюємо на...", "краще...", "а якщо...", "перенесемо на...", дні тижня — це price_quote. Так само ВИБІР типу номеру ("Стандарт", "обидва Стандарт", "беремо Напівлюкс") — price_quote. НІКОЛИ не став reject_dates лише тому, що попередні дати були зайняті чи клієнт хоче інші.
- general_price: є дати і гості, але конкретний номер НЕ обрано.
- faq: загальне питання (тоді заповни faq_template). "розваги / активності / що робити / що входить у вартість" -> INCLUDED_IN_THE_PRICE.
- presentation: просить розказати про номери / які є номери.
- thinking: каже, що подумає / порадиться / відпише пізніше.
- reject_dates: ТІЛЬКИ коли клієнт ЯВНО прощається/відмовляється, бо дати не підходять і він НЕ може приїхати ("тоді не вийде", "шкода, не підходить", "тоді іншим разом", "до побачення"). Якщо клієнт називає/змінює дати або обирає номер — це price_quote, НЕ reject_dates.
- booking_confirm: ТІЛЬКИ якщо клієнт погоджується бронювати ПІСЛЯ озвученої ціни ("так, бронюю", "давайте оформлюємо"). "Хочу забронювати" / "вільні дати для бронювання" / "забронювати номер" БЕЗ попередньої ціни — це НЕ booking_confirm (це початок збору даних: greeting/price_quote).
- fuzzy_dates: згадує період БЕЗ конкретних дат ("на літо", "десь у серпні").
- nearest_dates: погоджується пошукати найближчі вільні дати після відмови.
- greeting: привітання / клієнт хоче бронювати чи питати про готель, але ще не дав даних.
- unknown: повідомлення ВЗАГАЛІ не стосується готелю, бронювання чи FAQ і незрозуміле (НЕ привітання, НЕ бронювання, НЕ питання про готель). Став РІДКО — лише коли жоден інший topic не підходить.

Правила заповнення rooms:
- Для general_price (є дати+гості, без номеру) додай ОДИН об'єкт з room_type=null.
- КІЛЬКА НОМЕРІВ — окремий об'єкт у rooms[] на КОЖЕН номер ТІЛЬКИ якщо клієнт ЯВНО просить кілька номерів ("2 номери", "два номери", "дві сім'ї", "1 Стандарт і 1 Напівлюкс"). Якщо клієнт називає лише КІЛЬКІСТЬ ОСІБ ("4 дорослих", "нас 5", "на шістьох") — це ОДИН об'єкт rooms[] з відповідними adults/children. НЕ розбивай гостей на кілька номерів і НЕ присвоюй різні типи номерів сам — покажемо варіанти і клієнт обере.
- checkout = дата ВИЇЗДУ (остання ніч НЕ включається). "5-7 липня" => checkin 2026-07-05, checkout 2026-07-07.
- Якщо дано дату заїзду + кількість ночей — обчисли checkout = заїзд + ночі.
- Відносні дати ("завтра", "післязавтра", "на вихідних") рахуй від %%TODAY%%.
- ВІДНОСНІ ДНІ ТИЖНЯ → ТОЧНІ ДАТИ (ОБОВ'ЯЗКОВО): якщо клієнт називає день/дні тижня
  ("п'ятниця", "субота", "неділя", "понеділок", "із суботи на неділю", "виїзд у неділю")
  і є будь-який орієнтир часу (місяць, діапазон чисел типу "23-27", "після N", "наступного
  тижня", або %%TODAY%%) — РОЗРАХУЙ конкретні дати YYYY-MM-DD у 2026 році і заповни
  checkin/checkout. НЕ лишай рядком, НЕ став fuzzy_date, НЕ перепитуй. Місяць бери з
  КОНТЕКСТУ попередніх повідомлень, якщо в цьому повідомленні його не названо.
  ⚠ ПРАВИЛО ДІАПАЗОНУ: якщо названо діапазон чисел (напр. "23-27") РАЗОМ із днями тижня
  (п'ятниця→неділя), знайди саме ту П'ЯТНИЦЮ і ту НЕДІЛЮ, що ПОПАДАЮТЬ УСЕРЕДИНУ цього
  діапазону чисел. НЕ бери межі діапазону (23 і 27) як дати!
  • Контекст — липень. "п'ятниця, виїзд в неділю (після 23-27)" => у липні 2026: 24 —
    п'ятниця, 26 — неділя => checkin 2026-07-24, checkout 2026-07-26 (А НЕ 23-25!).
  • "п'ятниця, виїзд у неділю (після 23-27 червня)" => 26 червня — п'ятниця, 28 — неділя
    => checkin 2026-06-26, checkout 2026-06-28.
  • "із суботи на неділю в липні" => найближча Сб→Нд у липні 2026 (заїзд Сб, виїзд Нд).
  • "заїзд у п'ятницю на 2 ночі в серпні" => обери п'ятницю серпня і додай 2 ночі.
- "ДАТИ ЩЕ НЕ ЗНАЮ": фрази "дати ще не можу сказати", "поки не знаю дати", "ще не визначились" (БЕЗ названого періоду) — це НЕ reject_dates і НЕ нечіткий період. Залиш checkin=null, checkout=null, fuzzy_date=null; topic лишається price_quote/general_price (триває збір даних, клієнт просто ще не має дат).
- ТОЧНІ vs НЕЧІТКІ дати (ВАЖЛИВО):
  • ТОЧНИЙ діапазон (конкретні числа заїзду+виїзду АБО дата+кількість ночей, напр. "з 22.06 по 02.07", "17-19 липня", "20.07 на 3 ночі") => заповни checkin/checkout, fuzzy_date=null, і topic="price_quote" (НІКОЛИ не "general_price"). Слова "орієнтовно"/"приблизно"/"десь" ПЕРЕД конкретними числами НЕ роблять дати нечіткими ("орієнтовно 2-7 липня" = ТОЧНІ дати 2026-07-02..2026-07-07).
  • НЕЧІТКИЙ період ("початок серпня", "друга половина липня", "у серпні", "влітку", "кінець місяця", "після 6 серпня") => fuzzy_date="<текст періоду клієнта>", checkin=null, checkout=null.
  • НЕ підставляй "перше число місяця" як checkin для нечітких періодів — став fuzzy_date.
- nights — кількість ночей, ЛИШЕ якщо названо ОДНЕ чітке число ("на 3 ночі"=3, "тиждень"=7, "на 5 діб"=5, "2 ночі"=2). ДІАПАЗОН ("3-5 діб", "на 3-4 ночі") або невідомо => nights=null. Для точних дат nights можна лишити null (порахується з checkin/checkout).
- adults — кількість дорослих (ціле). "двоє дорослих" / "2 дорослих" / "на двох" / "вдвох" => adults=2; "троє" / "за трьох" / "на трьох" / "для трьох" / "трьох" => adults=3; "четверо" / "за чотирьох" => 4; одна особа => 1. Якщо клієнт лише УТОЧНЮЄ "дорослі всі" / "всі дорослі" / "дорослі" — кількість дорослих БЕРИ з попередніх повідомлень (напр., раніше "за трьох" => adults=3) і НЕ скидай у 0.
- children_count — ЗАГАЛЬНА кількість дітей (навіть якщо вік невідомий). children_ages — лише ВІДОМІ віки (цілі), вік не вигадуй.
- ЗАКРИВАЙ слот дітей: "лише дорослі" / "X дорослих" / "всі дорослі" / "дорослі всі" / "на двох/трьох" БЕЗ згадки дітей => children_count=0, children_ages=[]. НІКОЛИ не перепитуй про дітей, якщо кількість дорослих відома, а дітей не згадано.
- Якщо згадано N дітей без віку => children_count=N, children_ages=[]. Якщо вказані віки => children_count=кількість, children_ages=[віки].
- Якщо взагалі не вказано ні дорослих, ні дітей — adults=0, children_count=0, children_ages=[].
- ubd — true ЛИШЕ якщо клієнт згадує УБД / посвідчення УБД / військовослужбовця / ветерана / знижку для військових для ЦЬОГО номеру; інакше false. Якщо просять знижку УБД "на один номер" — постав ubd=true тільки для одного (першого) номеру.

FAQ-підказки (faq_template за останнім питанням): як добратися / як доїхати / потягом / залізницею / автобусом / звідки їхати -> HOW_TO_GET_THERE; вартість трансферу / парковка -> TRANSFER_PARKING; знижка військовим / УБД (як окреме питання без розрахунку) -> MILITARY.
БАСЕЙН — РОЗРІЗНЯЙ ДВА ШАБЛОНИ:
- GUEST_POOL: будь-яке питання про ЦІНУ/ВАРТІСТЬ басейну, про КУПАННЯ чи ВІДВІДУВАННЯ басейну БЕЗ проживання, "покупатись/поплавати в басейні", "скільки коштує басейн", "приїхати на басейн на день", "тільки/лише басейн", "можна просто скупатись".
- POOL: ЗАГАЛЬНЕ питання про басейн як зручність для гостей ("чи є басейн", "він з підігрівом?", "графік/години роботи", "розмір/глибина", "чи входить басейн у вартість проживання").

ІСТОРІЯ ДІАЛОГУ:
%%HISTORY%%

НОВЕ ПОВІДОМЛЕННЯ КЛІЄНТА: "%%MESSAGE%%"

JSON:"""

# Topics that map straight to a fixed template (no calculation needed).
_SIMPLE_TOPIC_TEMPLATE = {
    "group_event": "LARGE_GROUPS_EVENTS",
    "thinking": "THINKING_ABOUT_IT",
    "reject_dates": "POLITE_CLOSE",
    "booking_confirm": "BOOK_ROOM",
    "presentation": "PRESENTATION_ROOMS",
}


def route_simple_topic(slots: dict):
    """Return a fixed template reply for non-pricing topics, else None (the caller
    runs the deterministic booking/pricing path)."""
    topic = slots.get("topic", "greeting")
    # Don't take a booking for dates we cannot even price (off-season).
    if topic == "booking_confirm" and dialogue_engine.has_off_season_dates(slots):
        return templates.OFF_SEASON
    if topic in _SIMPLE_TOPIC_TEMPLATE:
        return getattr(templates, _SIMPLE_TOPIC_TEMPLATE[topic])
    if topic == "faq":
        name = slots.get("faq_template")
        reply = (getattr(templates, name)
                 if isinstance(name, str) and hasattr(templates, name)
                 else templates.GENERAL_INFORMATION)
        # Fix 3: an FAQ answered mid-booking must NOT wipe the gathered state. Append a
        # state-aware follow-up that asks ONLY what's still missing (never the all-missing
        # monolith, never info the client already gave).
        if slots.get("_faq_override") or bot_logic.has_booking_context(slots):
            reply = reply + dialogue_engine.faq_followup(slots)
        return reply
    return None  # price_quote / general_price / greeting -> booking path


_SCRAPE_ACTIONS = ("quote", "quote_all", "explore", "nearest")


def build_booking_reply(decision: dict, simplified: dict):
    """Turn a deterministic scrape-path decision + availability into the client text.
    Pure dispatch over dialogue_engine; shared by the live scrape path and the
    FAQ-combines-with-cached-calendar path (Bug 1)."""
    act = decision.get("action")
    if act == "quote":
        return dialogue_engine.finalize_quote(decision["rooms"], simplified)
    if act == "quote_all":
        return dialogue_engine.finalize_quote_all(decision["spec"], simplified)
    if act == "explore":
        return dialogue_engine.propose_windows(decision["spec"], simplified)
    if act == "nearest":
        return dialogue_engine.nearest_reply(decision["spec"], simplified)
    return None


async def _deliver(conversation_id: int, text: str):
    """Send `text` to Chatwoot, split on [SPLIT], with a human-like 1.5s pause."""
    for msg in bot_logic.split_messages(text):
        await asyncio.to_thread(send_chatwoot_message, conversation_id, msg)
        await asyncio.sleep(1.5)


_conv_locks: dict = {}
_conv_seq: dict = {}      # conv_id -> latest incoming sequence (drip-burst dedup)
_slot_memory: dict = {}   # conv_id -> list of last-known booking rooms (robust to extractor drops)
_greeted: set = set()     # conv_ids already greeted (idempotent vs Chatwoot read-after-write lag)
_pending_window: dict = {}  # conv_id -> (checkin, checkout) of the first proposed free window
_no_dates_mode: set = set() # conv_ids where the client said they don't know dates yet


def _lock_for(conversation_id):
    lock = _conv_locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _conv_locks[conversation_id] = lock
    return lock


def _next_seq(conversation_id):
    _conv_seq[conversation_id] = _conv_seq.get(conversation_id, 0) + 1
    return _conv_seq[conversation_id]


def _superseded(conversation_id, seq):
    """True if a NEWER message has arrived for this conversation, so this one must NOT
    reply — the newest task will, using the full consolidated history."""
    return seq != _conv_seq.get(conversation_id, seq)


async def process_incoming_message(user_message: str, conversation_id: int,
                                   has_attachment: bool = False, seq: int = None):
    # Drip-burst dedup: number each incoming message; the per-conversation lock
    # serializes processing, and ONLY the latest message in a burst emits a reply.
    if seq is None:
        seq = _next_seq(conversation_id)
    async with _lock_for(conversation_id):
        if _superseded(conversation_id, seq):
            print(f"[i] {conversation_id}: msg #{seq} superseded -> skip (newer drip pending).")
            return
        await _handle_incoming(user_message, conversation_id, has_attachment, seq)


async def _handle_incoming(user_message: str, conversation_id: int,
                           has_attachment: bool = False, seq: int = 0):
    # 0) MUTE SWITCH: if a human admin has taken over the conversation (the
    #    "Замовлено" label, or any mute label), the bot stays COMPLETELY silent —
    #    no labels query beyond this, no LLM, no reply.
    labels = await asyncio.to_thread(get_conversation_labels, conversation_id)
    if bot_logic.is_muted(labels):
        print(f"[i] Конверсація {conversation_id} під керуванням людини ({labels}); бот мовчить.")
        return

    # B2B / реклама / спам -> повністю ігноруємо (НЕ відправляємо жодної відповіді).
    if bot_logic.is_spam(user_message):
        print(f"[!] Спам проігноровано: {user_message[:50]}")
        return

    # ОПЛАТА (скрін / квитанція / ключові слова) -> НЕ підтверджуємо бронь
    # автоматично: передаємо людині-адміністратору, тегуємо конверсацію, замовкаємо.
    if bot_logic.is_payment_intent(user_message, has_attachment):
        print(f"[i] Виявлено оплату -> хендоф адміністратору, тег '{bot_logic.ORDER_LABEL}'")
        await asyncio.to_thread(send_chatwoot_message, conversation_id, templates.PAYMENT_RECEIVED_HANDOFF)
        await asyncio.to_thread(add_conversation_label, conversation_id, bot_logic.ORDER_LABEL)
        return

    # Клієнт залишив номер телефону -> передаємо менеджеру і зупиняємо діалог.
    if bot_logic.contains_phone_number(user_message):
        print(f"[i] Отримано контакт, передаю менеджеру: {user_message[:50]}")
        await asyncio.to_thread(send_chatwoot_message, conversation_id, templates.PHONE_RECEIVED)
        return

    raw_history = get_chatwoot_history(conversation_id)
    bot_has_spoken = any(msg.get("message_type") in ["outgoing", 1] for msg in raw_history)
    # Idempotent greeting: if we already greeted this conversation, trust that over the
    # history (Chatwoot read-after-write lag can hide a just-sent greeting and cause a
    # double-greet when an FAQ interrupts the first scrape).
    if conversation_id in _greeted:
        bot_has_spoken = True

    dialogue_history = ""
    for msg in raw_history:
        content = msg.get("content")
        if content:
            author = "Клієнт" if msg.get("message_type") in ["incoming", 0] else "Бот"
            dialogue_history += f"{author}: {content}\n"

    print(f"\n[+] Обробка повідомлення: {user_message}")

    # 1) LLM EXTRACTION ONLY — structured slots, no math, no prose.
    prompt = (EXTRACTION_PROMPT
              .replace("%%TODAY%%", date.today().isoformat())
              .replace("%%HISTORY%%", dialogue_history or "(порожня)")
              .replace("%%MESSAGE%%", user_message))
    try:
        extraction = await generate_with_retry(prompt)
        slots = dialogue_engine.parse_slots(extraction.text)
    except Exception as e:
        # GRACEFUL DEGRADATION: the LLM provider is down (503/429 after retries). NEVER
        # leave the client hanging — send a holding message once and hand off to a manager.
        print(f"[-] Помилка екстракції (LLM недоступний): {e}")
        if _superseded(conversation_id, seq):
            return
        last_bot = next((m.get("content", "").strip() for m in reversed(raw_history)
                         if m.get("message_type") in ("outgoing", 1) and m.get("content")), "")
        if last_bot != templates.ERROR_LLM_DOWN.strip():   # don't repeat the holding message
            await _deliver(conversation_id, templates.ERROR_LLM_DOWN)
            await asyncio.to_thread(add_conversation_label, conversation_id, bot_logic.INSTAGRAM_LABEL)
        return

    # SLOT MEMORY (multi-room): the extractor sometimes drops a slot the client already
    # gave (LLM variance), causing the bot to re-ask / forget a 2nd room. Python remembers
    # the booking rooms per conversation and refills anything the fresh extraction left
    # empty, BY INDEX (new values win; un-mentioned rooms are preserved — never forget).
    prev_mem = _slot_memory.get(conversation_id)
    merged_rooms = bot_logic.merge_rooms(prev_mem, slots.get("rooms") or [])
    slots["rooms"] = merged_rooms
    new_mem = bot_logic.remember_rooms(merged_rooms)
    if any(any(r.get(f) for f in bot_logic.MERGE_FIELDS) for r in new_mem):
        _slot_memory[conversation_id] = new_mem

    # Bug 2: did THIS turn actually CHANGE the booking state? Compared against the prior
    # memory AFTER the merge — robust even when the extractor (per the anti-amnesia rule)
    # re-emits the same slots on a chit-chat turn. If nothing changed, the message added
    # no new booking info -> it must NOT launch a redundant calendar search.
    slots_changed = (new_mem != (prev_mem or []))

    # Bug 1: track "client doesn't know dates yet" so a later guests-only turn proactively
    # SCANS the calendar instead of looping the date question. Exit the mode once any dates
    # (exact OR fuzzy) appear.
    if bot_logic.says_no_dates(user_message):
        _no_dates_mode.add(conversation_id)
    if any((r.get("checkin") or r.get("checkout") or r.get("fuzzy_date")) for r in merged_rooms):
        _no_dates_mode.discard(conversation_id)

    # DETERMINISTIC overrides (too important / too often mislabelled to leave to the LLM):
    # Large group = 20+ guests (by text OR consolidated slot count) or any event.
    if (bot_logic.looks_like_large_group(f"{dialogue_history}\n{user_message}")
            or bot_logic.slots_total_guests(slots) >= bot_logic.LARGE_GROUP_MIN):
        slots["topic"] = "group_event"
    else:
        # FAQ ABSOLUTE PRIORITY: a clear FAQ (location/pets/food/transport/…) is
        # answered immediately, overriding slot collection.
        faq_tmpl = bot_logic.faq_override(user_message)
        if faq_tmpl:
            slots["topic"] = "faq"
            slots["faq_template"] = faq_tmpl
            slots["_faq_override"] = True
    # The bot's most recent message — context for a bare "Так" and the payment guard.
    last_bot_msg = next((m.get("content", "") for m in reversed(raw_history)
                         if m.get("message_type") in ("outgoing", 1) and m.get("content")), "")

    # Decision 2: a bare confirmation ("Так" / "Давайте") means different things by the
    # bot's PREVIOUS message — accept the first proposed window, or proceed to payment.
    if bot_logic.is_bare_confirmation(user_message):
        if bot_logic.is_quote_message(last_bot_msg):
            slots["topic"] = "booking_confirm"            # Context B: ready to pay -> BOOK_ROOM
            print(f"[i] {conversation_id}: bare 'Так' after a quote -> booking_confirm")
        elif bot_logic.is_window_offer_message(last_bot_msg):
            # Context A: accept the offered window. Prefer the stored window; else use the
            # dates the extractor parsed from the bot's offer. Force a QUOTE (never let it
            # become nearest_dates -> re-search the dates we just offered).
            base = (slots.get("rooms") or [{}])[0]
            ci, co = _pending_window.get(conversation_id, (base.get("checkin"), base.get("checkout")))
            if ci and co:
                room0 = dict(base); room0["checkin"] = ci; room0["checkout"] = co; room0["fuzzy_date"] = None
                slots["rooms"] = [room0] + (slots.get("rooms") or [])[1:]
                slots["topic"] = "price_quote"
                _slot_memory[conversation_id] = bot_logic.remember_rooms(slots["rooms"])
                _pending_window.pop(conversation_id, None)
                slots_changed = True                       # dates changed -> the quote scrape may run
                print(f"[i] {conversation_id}: bare 'Так' accepts window {ci}..{co}")

    # Bug 3: NEVER drop payment details (BOOK_ROOM) unless the bot ACTUALLY quoted an
    # exact price last turn. A "Так, бронюємо" after a cross-sell / info / window offer
    # must continue the booking (price_quote), not jump straight to the IBAN.
    if slots.get("topic") == "booking_confirm" and not bot_logic.is_quote_message(last_bot_msg):
        slots["topic"] = "price_quote"
        print(f"[i] {conversation_id}: booking_confirm without a prior quote -> price_quote (no premature IBAN)")
    print(f"[i] Slots: {slots}")

    # Fix 2: a COMPLETELY unrecognized intent is the only manager hand-off (date
    # searches never hand off). Reply once, tag the conversation Instagram, stop.
    if slots.get("topic") == "unknown":
        print(f"[i] Невпізнаний намір -> менеджер, тег '{bot_logic.INSTAGRAM_LABEL}'")
        if not _superseded(conversation_id, seq):
            await _deliver(conversation_id, templates.MANAGER_HANDOFF)
            await asyncio.to_thread(add_conversation_label, conversation_id, bot_logic.INSTAGRAM_LABEL)
        return

    # 2) DETERMINISTIC ROUTING — Python decides everything below.
    is_faq_reply = (slots.get("topic") == "faq")
    reply = route_simple_topic(slots)
    if reply is None:
        decision = dialogue_engine.plan(slots)
        # Bug 1: guests are known and the client already said they don't know dates ->
        # PROACTIVELY scan the open calendar and propose windows, instead of looping
        # QUESTION_ONLY_DATES (which the anti-spam guard would then suppress -> silence).
        if (decision.get("action") == "reply" and decision.get("reply") == templates.QUESTION_ONLY_DATES
                and conversation_id in _no_dates_mode):
            spec = next((r for r in (slots.get("rooms") or [])
                         if (r.get("adults") or 0) >= 1 or r.get("children_ages") or r.get("children_count")),
                        (slots.get("rooms") or [{}])[0])
            decision = {"action": "explore", "spec": {**spec, "fuzzy_date": ""}}
            slots_changed = True   # a deliberate proactive scan -> bypass the filler guard
            print(f"[i] {conversation_id}: no-dates mode + guests known -> proactive open-calendar scan")
        if decision["action"] in _SCRAPE_ACTIONS:
            if not slots_changed and slots.get("topic") != "nearest_dates":
                # Bug 2: this turn added NO new booking info -> never re-scan (chit-chat
                # just repeats — or flips — a prior result). Robust to the extractor
                # re-emitting known slots. "Search nearest dates" is exempt above.
                if slots.get("topic") in ("price_quote", "general_price"):
                    # Decision 1: a price re-ask must NOT go silent.
                    cached = peek_cached_availability(conversation_id)
                    if cached is None:
                        reply = templates.PRICE_NEED_DETAILS
                    elif decision["action"] == "explore":
                        # No exact dates yet -> explain we need details + RE-SHOW the windows.
                        simplified = bot_logic.build_simplified_availability(cached)
                        reply = (templates.PRICE_NEED_DETAILS + "\n\n"
                                 + dialogue_engine.propose_windows(decision["spec"], simplified))
                        win = dialogue_engine.offered_window(decision, simplified)
                        if win:
                            _pending_window[conversation_id] = win
                    else:
                        # Exact dates ARE known -> just re-quote from cache (we have details).
                        reply = build_booking_reply(
                            decision, bot_logic.build_simplified_availability(cached))
                else:
                    print(f"[i] {conversation_id}: booking unchanged by '{user_message[:40]}' -> no re-scan (silent).")
                    return
            else:
                # Greet FIRST on the first turn, then the scrape notice, then the result.
                if not bot_has_spoken:
                    await _deliver(conversation_id, bot_logic.GREETING)
                    _greeted.add(conversation_id)
                    bot_has_spoken = True
                # Check the calendar BEFORE answering (sends "Секундочку…").
                availability_data = await get_hotel_data_cached(conversation_id)
                if not availability_data:
                    if not _superseded(conversation_id, seq):
                        await asyncio.to_thread(send_chatwoot_message, conversation_id,
                                                "Вибачте, технічна затримка бази. Менеджер вже підключається.")
                    return
                simplified = bot_logic.build_simplified_availability(availability_data)
                reply = build_booking_reply(decision, simplified)
                # Bug 1: remember WHATEVER window we offered (explore windows / a sold-out
                # room's nearest / nearest) so a later bare "Так" quotes EXACTLY it and
                # never re-searches the dates we just proposed.
                if bot_logic.is_window_offer_message(reply or ""):
                    win = dialogue_engine.offered_window(decision, simplified)
                    if win:
                        _pending_window[conversation_id] = win
        else:
            reply = decision["reply"]

    # Anti-spam DEADLOCK breaker: the client wants a price but gave no dates/guests (e.g.
    # "дати ще не можу сказати"), so we'd repeat QUESTION_ALL_MISSING — which the identical
    # -reply guard would then suppress, leaving the client ignored. Pivot ONCE to asking
    # guests; if we've already pivoted, fall back to ACKNOWLEDGE so anti-spam settles us
    # into silence rather than ping-ponging the two prompts.
    if reply == templates.QUESTION_ALL_MISSING:
        prev = last_bot_msg.strip()
        if prev == templates.QUESTION_ALL_MISSING.strip():
            reply = templates.ACKNOWLEDGE_NO_DATES_ASK_GUESTS
            _no_dates_mode.add(conversation_id)   # asking guests because dates are unknown
            print(f"[i] {conversation_id}: QUESTION_ALL_MISSING would repeat -> pivot to ASK_GUESTS")
        elif prev == templates.ACKNOWLEDGE_NO_DATES_ASK_GUESTS.strip():
            reply = templates.ACKNOWLEDGE_NO_DATES_ASK_GUESTS  # already pivoted -> anti-spam silences

    # 3) SEND. A newer drip that arrived while we were processing supersedes this reply,
    #    so a burst collapses to ONE consolidated reply AND a mid-scrape correction wins
    #    (no stale/double quote). A scrape still populated the cache for the next turn.
    if _superseded(conversation_id, seq):
        print(f"[i] {conversation_id}: msg #{seq} superseded mid-processing -> suppress reply.")
        return

    # Anti-spam: never send the SAME message twice in a row (across both emits this turn).
    last_bot = next((m.get("content", "").strip() for m in reversed(raw_history)
                     if m.get("message_type") in ("outgoing", 1) and m.get("content")), "")

    async def _emit(text):
        nonlocal last_bot, bot_has_spoken
        if not text or not text.strip():
            return
        if last_bot and text.strip() == last_bot:
            print(f"[i] {conversation_id}: reply identical to previous -> suppress (anti-spam).")
            return
        out = bot_logic.prepend_greeting_if_needed(text, bot_has_spoken)
        if out != text:                       # greeting was prepended this emit
            _greeted.add(conversation_id)
        await _deliver(conversation_id, out)
        bot_has_spoken = True
        last_bot = text.strip()

    # Bug 1 (FAQ hijacks the scan): an FAQ asked ALONGSIDE an actionable booking intent
    #    must ANSWER the FAQ and then RUN the real scan, appending the ACTUAL calendar
    #    result (free віконця / room options / quote) — NEVER the generic "shall I check?"
    #    nudge. So: answer the FAQ first (greeting prepended on turn 1), take availability
    #    from a warm cache if present (no double scrape — e.g. an FAQ that interrupted an
    #    in-flight scrape) else scrape now ("Секундочку…"), then send the booking result as
    #    a 2nd message. The nudge survives ONLY when the booking action is a QUESTION
    #    (missing data) — that path keeps faq_followup's nudge and never reaches here.
    booking_extra = None
    if is_faq_reply:
        d2 = dialogue_engine.plan(slots)
        if d2.get("action") in _SCRAPE_ACTIONS:
            reply = reply.replace(templates.FAQ_CONTINUE_NUDGE, "")  # real result, not the nudge
            try:
                await _emit(reply)                  # 1) answer the FAQ first
            except Exception as e:
                print(f"[-] Помилка надсилання (FAQ): {e}")
            reply = None                            # already sent above
            cached = peek_cached_availability(conversation_id)
            availability_data = (cached if cached is not None
                                 else await get_hotel_data_cached(conversation_id))
            if availability_data and not _superseded(conversation_id, seq):
                simplified = bot_logic.build_simplified_availability(availability_data)
                booking_extra = build_booking_reply(d2, simplified)
                if bot_logic.is_window_offer_message(booking_extra or ""):
                    win = dialogue_engine.offered_window(d2, simplified)
                    if win:
                        _pending_window[conversation_id] = win

    try:
        if reply is not None:
            await _emit(reply)
        if booking_extra:
            await _emit(booking_extra)
    except Exception as e:
        print(f"[-] Помилка надсилання: {e}")

@app.post("/webhook")
async def chatwoot_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        return {"status": "ignored"}
    
    event = payload.get("event")
    mtype = payload.get("message_type")
    # Bug 5: message_type may be a STRING ("incoming"/"outgoing") OR an INT (0/1)
    # depending on the channel/Chatwoot version (the Website Widget sends incoming too).
    # Accept BOTH incoming forms. We do NOT filter by inbox/channel, so every configured
    # inbox (WebWidget, API, Instagram, …) is served the same way.
    if event == "message_created" and mtype in ("incoming", 0):
        if payload.get("private"):
            return {"status": "ok"}                    # ignore private agent notes
        content = payload.get("content")
        conv = payload.get("conversation") or {}
        conversation_id = conv.get("id") or payload.get("conversation_id")
        # A payment screenshot may arrive as an image with NO text -> still process.
        has_attachment = bool(payload.get("attachments"))

        if conversation_id and (content or has_attachment):
            seq = _next_seq(conversation_id)   # assign in ARRIVAL order (drip-burst dedup)
            background_tasks.add_task(process_incoming_message, content or "",
                                      conversation_id, has_attachment, seq)
        else:
            print(f"[i] webhook incoming but skipped: conv={conversation_id} has_content={bool(content)} attach={has_attachment}")
    elif event == "message_created":
        print(f"[i] webhook ignored message_created: message_type={mtype!r}")

    return {"status": "ok"}

# Запусти код я не тестив ще)