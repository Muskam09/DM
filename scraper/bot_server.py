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

async def generate_with_retry(prompt: str, retries: int = 3, delay: int = 2):
    for attempt in range(retries):
        try:
            response = await ai_client.aio.models.generate_content(
                model=MODEL_NAME,
                contents=prompt
            )
            return response
        except Exception as e:
            if attempt < retries - 1:
                await asyncio.sleep(delay)
            else:
                raise e

# The LLM does EXTRACTION ONLY — it never computes prices, dates or weekdays and
# never writes the client-facing text. It returns structured slots as JSON; all
# pricing/availability/formatting is handled deterministically in dialogue_engine.
EXTRACTION_PROMPT = """Ти — аналізатор повідомлень готелю D&T Hotel. Сьогодні: %%TODAY%%. Рік бронювань: 2026.
Прочитай ВСЮ історію діалогу і нове повідомлення та ОБ'ЄДНАЙ дані з УСІХ повідомлень
(клієнт часто пише по частинах: дати в одному, людей у другому, номер у третьому).

Поверни ВИКЛЮЧНО JSON (без markdown, без пояснень) такої структури:
{
  "topic": "<один з: price_quote | general_price | faq | presentation | group_event | thinking | reject_dates | booking_confirm | fuzzy_dates | nearest_dates | greeting>",
  "rooms": [ {"room_type": "<Стандарт|Стандарт +|Напівлюкс|null>", "checkin": "YYYY-MM-DD|null", "checkout": "YYYY-MM-DD|null", "adults": <ціле>, "children_ages": [<вік>, ...], "ubd": <true|false>} ],
  "faq_template": "<POOL|PETS|SAUNA_VATS|FOOD_PRICES|TRANSFER_PARKING|HOW_TO_GET_THERE|ROOM_AMENITIES|SMOKING|PLACE|BOOK_ROOM|MILITARY|CHILDREN|BAR|GUEST_POOL|KITCHEN|INCLUDED_IN_THE_PRICE|BREAKFAST_IN_THE_PRICE|GENERAL_INFORMATION|null>"
}

Дати/гостей/номери збирай з УСІЄЇ історії. Якщо обрано topic=faq — faq_template обирай за ОСТАННІМ (поточним) питанням клієнта.

Значення topic:
- group_event (НАЙВИЩИЙ ПРІОРИТЕТ): якщо у БУДЬ-ЯКОМУ повідомленні (навіть ранньому) згадано 40+ осіб / велику групу / табір / тур / спортивні збори / весілля / банкет / корпоратив / захід — ЗАВЖДИ став topic=group_event, незалежно від того, про що останнє повідомлення.
- price_quote: клієнт обрав КОНКРЕТНИЙ номер і є дати+гості, АБО змінює дати/ночі для вже обраного номеру.
- general_price: є дати і гості, але конкретний номер НЕ обрано.
- faq: загальне питання (тоді заповни faq_template). "розваги / активності / що робити / що входить у вартість" -> INCLUDED_IN_THE_PRICE.
- presentation: просить розказати про номери / які є номери.
- thinking: каже, що подумає / порадиться / відпише пізніше.
- reject_dates: не може змінити дати, а все зайнято.
- booking_confirm: погоджується бронювати ("так, бронюю", "давайте").
- fuzzy_dates: згадує період БЕЗ конкретних дат ("на літо", "десь у серпні").
- nearest_dates: погоджується пошукати найближчі вільні дати після відмови.
- greeting: привітання / немає корисних даних / незрозуміло.

Правила заповнення rooms:
- Для general_price (є дати+гості, без номеру) додай ОДИН об'єкт з room_type=null.
- Для кількох номерів — окремий об'єкт на КОЖЕН номер.
- checkout = дата ВИЇЗДУ (остання ніч НЕ включається). "5-7 липня" => checkin 2026-07-05, checkout 2026-07-07.
- Якщо дано дату заїзду + кількість ночей — обчисли checkout = заїзд + ночі.
- Відносні дати ("завтра", "післязавтра", "на вихідних") рахуй від %%TODAY%%.
- adults — кількість дорослих (ціле). "двоє дорослих" / "2 дорослих" / "на двох" / "вдвох" => adults=2; "троє" => 3; одна особа => 1.
- children_ages — лише ВІДОМІ віки дітей (цілі числа). Якщо вік невідомий — НЕ вигадуй.
- ВАЖЛИВО: якщо названо лише дорослих і про дітей НЕ згадано — це означає, що дітей НЕМАЄ: постав children_ages=[]. НЕ повертайся до питання про дітей, якщо кількість дорослих уже відома.
- Якщо взагалі не вказано ні дорослих, ні дітей — постав adults=0, children_ages=[].
- ubd — true ЛИШЕ якщо клієнт згадує УБД / посвідчення УБД / військовослужбовця / ветерана / знижку для військових для ЦЬОГО номеру; інакше false. Якщо просять знижку УБД "на один номер" — постав ubd=true тільки для одного (першого) номеру.

FAQ-підказки (faq_template за останнім питанням): як добратися / як доїхати / потягом / залізницею / автобусом / звідки їхати -> HOW_TO_GET_THERE; вартість трансферу / парковка -> TRANSFER_PARKING; знижка військовим / УБД (як окреме питання без розрахунку) -> MILITARY.

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
    "fuzzy_dates": "FUZZY_DATES",
    "nearest_dates": "NEAREST_DATES",
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
        if isinstance(name, str) and hasattr(templates, name):
            return getattr(templates, name)
        return templates.GENERAL_INFORMATION
    return None  # price_quote / general_price / greeting -> booking path


async def _deliver(conversation_id: int, text: str):
    """Send `text` to Chatwoot, split on [SPLIT], with a human-like 1.5s pause."""
    for msg in bot_logic.split_messages(text):
        await asyncio.to_thread(send_chatwoot_message, conversation_id, msg)
        await asyncio.sleep(1.5)


async def process_incoming_message(user_message: str, conversation_id: int,
                                   has_attachment: bool = False):
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
        print(f"[-] Помилка екстракції: {e}")
        return

    # DETERMINISTIC override: a 40+ group / event must always be redirected,
    # regardless of what the LLM classified (the rule is too important to guess).
    if bot_logic.looks_like_large_group(f"{dialogue_history}\n{user_message}"):
        slots["topic"] = "group_event"
    print(f"[i] Slots: {slots}")

    # 2) DETERMINISTIC ROUTING — Python decides everything below.
    reply = route_simple_topic(slots)
    if reply is None:
        decision = dialogue_engine.plan(slots)
        if decision["action"] == "quote":
            # Fix 1 — correct ORDER on the first turn: greet FIRST, then the scrape
            # notice, then the result. So greet BEFORE calling the scraper.
            if not bot_has_spoken:
                await _deliver(conversation_id, bot_logic.GREETING)
                bot_has_spoken = True
            # AVAILABILITY GATING: check the calendar BEFORE pricing (sends "Секундочку…").
            availability_data = await get_hotel_data_cached(conversation_id)
            if not availability_data:
                await asyncio.to_thread(send_chatwoot_message, conversation_id,
                                        "Вибачте, технічна затримка бази. Менеджер вже підключається.")
                return
            simplified = bot_logic.build_simplified_availability(availability_data)
            reply = dialogue_engine.finalize_quote(decision["rooms"], simplified)
        else:
            reply = decision["reply"]

    # 3) SEND — replies are UA templates / Python-formatted text by construction.
    try:
        reply = bot_logic.prepend_greeting_if_needed(reply, bot_has_spoken)
        await _deliver(conversation_id, reply)
    except Exception as e:
        print(f"[-] Помилка надсилання: {e}")

@app.post("/webhook")
async def chatwoot_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        return {"status": "ignored"}
    
    if payload.get("event") == "message_created" and payload.get("message_type") == "incoming":
        content = payload.get("content")
        conversation_id = payload.get("conversation", {}).get("id")
        # A payment screenshot may arrive as an image with NO text -> still process.
        has_attachment = bool(payload.get("attachments"))

        if conversation_id and (content or has_attachment):
            background_tasks.add_task(process_incoming_message, content or "",
                                      conversation_id, has_attachment)

    return {"status": "ok"}

# Запусти код я не тестив ще)