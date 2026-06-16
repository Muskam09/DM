import os
import json
import time
import asyncio
import requests
import uvicorn
from datetime import datetime
from fastapi import FastAPI, Request, BackgroundTasks
from google import genai
from dotenv import load_dotenv

import templates
import bot_logic
from hotel_scraper import fetch_hotel_availability

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CHATWOOT_URL = os.getenv("CHATWOOT_URL", "http://rails:3000")
CHATWOOT_TOKEN = os.getenv("CHATWOOT_TOKEN")
ACCOUNT_ID = os.getenv("CHATWOOT_ACCOUNT_ID", "1")

ai_client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_NAME = 'gemini-2.5-flash-lite' 

with open("pricing.json", "r", encoding="utf-8") as f:
    PRICING_DATA = json.load(f)

app = FastAPI()

AVAILABILITY_CACHE = {}
CACHE_TTL = 900

# IGNORE_CATEGORIES is imported from bot_logic (single source of truth).

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

async def process_incoming_message(user_message: str, conversation_id: int):
    raw_history = get_chatwoot_history(conversation_id)
    bot_has_spoken = any(msg.get("message_type") in ["outgoing", 1] for msg in raw_history)

    dialogue_history = ""
    for msg in raw_history:
        content = msg.get("content")
        if content:
            author = "Клієнт" if msg.get("message_type") in ["incoming", 0] else "Бот"
            dialogue_history += f"{author}: {content}\n"

    print(f"\n[+] Обробка повідомлення: {user_message}")

    # ЖОРСТКИЙ ІНТЕНТ АНАЛІЗАТОР
    intent_prompt = f"""
    Історія діалогу:
    {dialogue_history}
    Нове повідомлення клієнта: "{user_message}"
    
    ЗАВДАННЯ: Чи потрібно запускати скрапер бази номерів?
    Відповідай "ТАК", ТІЛЬКИ якщо клієнт:
    1. Обирає або питає ціну КОНКРЕТНОГО номеру (наприклад: "Стандарт+", "А для Стандарту?").
    2. Змінює дати або кількість ночей для вже обраного раніше номеру (наприклад: "А на 2 ночі?", "А якщо 6-8 липня?").
    3. Погоджується на пошук найближчих вільних дат ("Так, підшукайте").
    
    Відповідай "НІ" в усіх інших випадках (якщо клієнт просто пише дати, кількість людей, або загальні питання).
    Відповідь (ТІЛЬКИ ТАК або НІ):
    """
    
    try:
        intent_response = await generate_with_retry(intent_prompt)
        needs_calendar = bot_logic.intent_says_yes(intent_response.text)
    except Exception as e:
        print(f"[-] Помилка інтенту: {e}")
        return

    context = f"Прайс та правила готелю:\n{json.dumps(PRICING_DATA, ensure_ascii=False)}\n"

    if needs_calendar:
        availability_data = await get_hotel_data_cached(conversation_id)
        if availability_data:
            simplified_data = bot_logic.build_simplified_availability(availability_data)
            context += f"\nРЕАЛЬНИЙ СТАН КІМНАТ (Шахівниця):\n{json.dumps(simplified_data, ensure_ascii=False)}\n"
        else:
            await asyncio.to_thread(send_chatwoot_message, conversation_id, "Вибачте, технічна затримка бази. Менеджер вже підключається.")
            return

    kb_templates = f"""
--- БАЗА ЗНАНЬ ТА ШАБЛОНИ ---
[GENERAL_INFORMATION]: {templates.GENERAL_INFORMATION}
[PRESENTATION_ROOMS]: {templates.PRESENTATION_ROOMS}
[QUESTION_ALL_MISSING]: {templates.QUESTION_ALL_MISSING}
[QUESTION_MISSING_DATES]: {templates.QUESTION_MISSING_DATES}
[QUESTION_MISSING_GUESTS]: {templates.QUESTION_MISSING_GUESTS}
[QUESTION_MISSING_AGE]: {templates.QUESTION_MISSING_AGE}
[QUESTION_MISSING_DATES_1_CHILD]: {templates.QUESTION_MISSING_DATES_1_CHILD}
[QUESTION_MISSING_DATES_CHILDREN]: {templates.QUESTION_MISSING_DATES_CHILDREN}
[CHILDREN]: {templates.CHILDREN}
[PLACE]: {templates.PLACE}
[BOOK_ROOM]: {templates.BOOK_ROOM}
[POOL]: {templates.POOL}
[EAT]: {templates.EAT}
[PRICE_JUNE]: {templates.PRICE_JUNE}
[PRICE_JULY]: {templates.PRICE_JULY}
[PRICE_AUGUST]: {templates.PRICE_AUGUST}
[PETS]: {templates.PETS}
[BAR]: {templates.BAR}
[GUEST_POOL]: {templates.GUEST_POOL}
[MILITARY]: {templates.MILITARY}
[PRICE_CALLCULATION]: {templates.PRICE_CALLCULATION}
[ROOM_BOOKED]: {templates.ROOM_BOOKED}
[INCLUDED_IN_THE_PRICE]: {templates.INCLUDED_IN_THE_PRICE}
[BREAKFAST_IN_THE_PRICE]: {templates.BREAKFAST_IN_THE_PRICE}
[KITCHEN]: {templates.KITCHEN}
[NEAREST_DATES]: {templates.NEAREST_DATES}
-----------------------------
    """

    greeting_instruction = ""
    if not bot_has_spoken:
        greeting_instruction = "ОСКІЛЬКИ ЦЕ ТВОЄ ПЕРШЕ ПОВІДОМЛЕННЯ В ДІАЛОЗІ, ОБОВ'ЯЗКОВО встав у <REPLY> на самий початок: 'Доброго дня! Вас вітає D&T Hotel ⛰\\nРаді, що зацікавились нашим готелем!\\n[SPLIT]\\n'"
    else:
        greeting_instruction = "КАТЕГОРИЧНО ЗАБОРОНЕНО вітатися ('Доброго дня', 'Вітаю' тощо), бо ти вже вітався раніше."

    sys_prompt = f"""
    Ти - системний алгоритм готелю 'D&T Hotel ⛰'. РІК: 2026.
    
    ІСТОРІЯ ДІАЛОГУ:
    {dialogue_history}
    
    БАЗА ДАНИХ (Прайс та Наявність):
    {context}
    
    {kb_templates}
    
    СУВОРІ ПРАВИЛА ТА МАТЕМАТИКА (КРИТИЧНО!):
    1. ПАМ'ЯТЬ (УВАГА!): Уважно читай ВСЮ історію діалогу! Клієнт міг написати дати в одному повідомленні, дорослих у другому, а ночі в третьому. ОБ'ЄДНУЙ ці дані. Не перепитуй те, що клієнт вже вказував вище! У <THINK> обов'язково випиши зібрані дані: Дати, Ночі, Гості.
    2. КАЛЕНДАР 2026: Вихідні ночі (тариф "вихідні") — це П'ЯТНИЦЯ та СУБОТА (наприклад, 3 і 4 липня). Будні — Нд, Пн, Вт, Ср, Чт.
    3. МАТЕМАТИКА НОЧЕЙ: Дата виїзду МІНУС Дата заїзду (6-8 липня = 2 ночі). День виїзду НІКОЛИ не перевіряється в базі на зайнятість.
    4. ДІТИ ТА ДОДАТКОВІ МІСЦЯ (СУВОРА ТАРИФІКАЦІЯ!): Базова `вартість_кімнати` покриває до 2 гостей (базова місткість).
       Рахуй доплати ЗА КОЖНУ НІЧ за кожного гостя, що ПЕРЕВИЩУЄ базову місткість (2):
         • Дитина 0–6 років (включно) — БЕЗКОШТОВНО (спить з батьками, доплати 0). Не займає платного місця.
         • Дитина 7–12 років — додай `дитяче_місце` (це вже 50% тариф).
         • Гість старше 12 років АБО дорослий — додай `додаткове_місце` (повний тариф).
       Базову місткість заповнюй найдорожчими гостями (дорослі/діти 12+), тому "зайвими" (платними) стають найдешевші.
       ФОРМУЛА за ніч = `вартість_кімнати` (або `одномісне_поселення` якщо платний гість лише 1) + сума доплат за зайвих гостей.
       Приклад (Кейс 7): Стандарт, 2 дорослих + дитина 8 р., 2 будні ночі = (вартість_кімнати + дитяче_місце) * 2.
       Підсумовуй ПО НОЧАХ окремо (будні/вихідні тариф може відрізнятись для різних ночей).
    5. У тег <REPLY> ти ЗОБОВ'ЯЗАНИЙ скопіювати ПОВНИЙ ТЕКСТ із шаблону БАЗИ ЗНАНЬ.
    
    АЛГОРИТМ ДІЙ (Обери СУВОРО ОДИН варіант):
    {greeting_instruction}
    
    IF (Клієнт задає загальне питання (FAQ)):
        <REPLY>КОПІЮЙ_СЮДИ_ТЕКСТ_З_БАЗИ_ЗНАНЬ (наприклад: POOL, PETS, KITCHEN)</REPLY>
        
    ELSE IF (У ВСІЙ зібраній історії діалогу НЕ ВИСТАЧАЄ Дат, АБО Кількості ночей, АБО Гостей):
        <THINK>Перевіряємо ВСЮ історію. Чого бракує? Обираємо відповідний шаблон питання.</THINK>
        <REPLY>КОПІЮЙ_СЮДИ_ТЕКСТ_З_БАЗИ_ЗНАНЬ (QUESTION_ALL_MISSING, QUESTION_MISSING_DATES_1_CHILD тощо)</REPLY>
        
    ELSE IF (Клієнт явно просить розказати про номери (детальніше)):
        <REPLY>КОПІЮЙ_СЮДИ_ТЕКСТ_З_БАЗИ_ЗНАНЬ (PRESENTATION_ROOMS)</REPLY>

    ELSE IF (Клієнт погоджується на пошук найближчих дат ("Так", "Шукайте") ПІСЛЯ відмови):
        <THINK>Шукаємо найближчі дати ПІСЛЯ дат клієнта, де обраний номер має значення > 0 на ВСІ потрібні ночі.</THINK>
        <REPLY>КОПІЮЙ_СЮДИ_ТЕКСТ_З_NEAREST_DATES (заміни змінні [тип номеру] та [найближчі_дати])</REPLY>

    ELSE IF (Дати, Ночі та Гості ВЖЕ Є у загальній історії, АЛЕ клієнт ЩЕ НЕ обрав номер і НЕ питає ціну):
        <THINK>Клієнт надав усі дані, але номер не обрав. Видаємо загальні ціни на вказаний місяць.</THINK>
        <REPLY>КОПІЮЙ_СЮДИ_ТЕКСТ_З_БАЗИ_ЗНАНЬ (PRICE_JUNE, PRICE_JULY або PRICE_AUGUST)</REPLY>
        
    ELSE IF (Клієнт ОБРАВ номер АБО ЗМІНИВ дати/ночі для обраного номеру ("А на 2 ночі?")):
        <THINK>
        1. Визначаємо точні ночі проживання.
        2. Перевіряємо Шахівницю. Якщо ХОЧА Б НА ОДНУ ніч проживання є '0' -> ЗАЙНЯТО.
        3. Рахуємо ціну СУВОРО за Правилами 3 і 4 (доплати за дітей/додаткові місця рахуй за тарифною таблицею Правила 4).
        </THINK>
        IF (Номер ВІЛЬНИЙ на всі ночі):
            <REPLY>КОПІЮЙ_СЮДИ_ТЕКСТ_З_PRICE_CALLCULATION (заміни змінні на реальні дані з Прайсу)</REPLY>
        ELSE IF (Номер ЗАЙНЯТИЙ і Є ІНШІ ДІЙСНО ВІЛЬНІ номери в базі):
            <REPLY>КОПІЮЙ_СЮДИ_ТЕКСТ_З_ROOM_BOOKED (замість змінної [вільні_номери] впиши ТІЛЬКИ ті, де > 0 на всі ночі)</REPLY>
        ELSE IF (Номер ЗАЙНЯТИЙ і ІНШИХ ВІЛЬНИХ НОМЕРІВ НЕМАЄ ВЗАГАЛІ):
            <REPLY>На жаль, на ваші дати всі номери повністю заброньовані 😔. Можемо запропонувати інші найближчі вільні дати. Підкажіть, чи підшукати вам варіанти? 💙</REPLY>
            
    ELSE IF (Клієнт погоджується бронювати ("Так", "Давайте") ПІСЛЯ розрахунку ціни):
        <REPLY>КОПІЮЙ_СЮДИ_ТЕКСТ_З_BOOK_ROOM</REPLY>
        
    Нове повідомлення: {user_message}
    Твоя відповідь (ТІЛЬКИ ОДИН СТАН, обов'язково використовуй <THINK> і <REPLY>):
    """
    
    try:
        final_response = await generate_with_retry(sys_prompt)
        response_text = final_response.text

        clean_message = bot_logic.extract_reply(response_text)
        clean_message = bot_logic.prepend_greeting_if_needed(clean_message, bot_has_spoken)

        for msg in bot_logic.split_messages(clean_message):
            await asyncio.to_thread(send_chatwoot_message, conversation_id, msg)
            await asyncio.sleep(1.5)
                
    except Exception as e:
        print(f"[-] Помилка генерації відповіді: {e}")

@app.post("/webhook")
async def chatwoot_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        return {"status": "ignored"}
    
    if payload.get("event") == "message_created" and payload.get("message_type") == "incoming":
        content = payload.get("content")
        conversation_id = payload.get("conversation", {}).get("id")
        
        if content and conversation_id:
            background_tasks.add_task(process_incoming_message, content, conversation_id)
            
    return {"status": "ok"}

# Запусти код я не тестив ще)