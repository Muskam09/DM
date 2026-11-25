import os
import asyncio
import re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import json
from dotenv import load_dotenv

load_dotenv()

HMS_ID = os.getenv("OTELMS_HMS_ID", "18472")
USERNAME = os.getenv("OTELMS_LOGIN")
PASSWORD = os.getenv("OTELMS_PASSWORD")

LOGIN_URL = f"https://desktop.otelms.com/login_c2/single_login?hmsid={HMS_ID}"
CALENDAR_URL = "https://desktop.otelms.com/reservation_c2/calendar"

# OtelMS "Шахівниця" (chessboard) only renders a ~46-day window around the browser-side
# "today". Without explicit navigation it is IMPOSSIBLE to see Jan/Feb 2027 from Nov 2026
# — the grid simply doesn't contain those columns, so every parse of a far-future date
# returned `count=0` and the bot wrongly said "all rooms booked" (2026-11-19 prod bug).
#
# The calendar form (`#frmdata`) exposes a date picker `#datein100` whose `change` event
# copies the full ISO date into the hidden `#date_shift` and submits the form, re-rendering
# the grid centred on the chosen day. We drive exactly that handler via Playwright so the
# scrape returns a grid that COVERS the requested check-in instead of the browser default.


def _target_iso(target_date) -> "str | None":
    """Normalise target_date into 'YYYY-MM-DD' (OtelMS format) or None if unusable."""
    if not target_date:
        return None
    if isinstance(target_date, str):
        s = target_date.strip()
        # Accept full ISO and best-effort 'YYYY-MM' (first day of month).
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return s
        if re.fullmatch(r"\d{4}-\d{2}", s):
            return s + "-01"
        return None
    # datetime / date
    try:
        return target_date.strftime("%Y-%m-%d")
    except Exception:
        return None


async def fetch_hotel_availability(target_date=None):
    """Scrape the "Шахівниця" availability grid.

    Args:
        target_date: optional ISO 'YYYY-MM-DD' (or `datetime`/`date`) that the client
            wants to book. When given, we navigate the OtelMS calendar to that date so
            the grid contains the requested nights; otherwise we scrape the default
            window (today ± ~23 days). Driven via the real `#datein100` picker, which
            submits `#frmdata` → the grid re-renders centred on the chosen date.
    """
    target_iso = _target_iso(target_date)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        page = await context.new_page()

        print("[*] Відкриваємо сторінку авторизації...")
        await page.goto(LOGIN_URL)
        await page.wait_for_selector('#userLogin', state='visible', timeout=15000)

        print("[*] Вводимо логін та пароль...")
        await page.fill('#userLogin', USERNAME)
        await page.fill('#password', PASSWORD)
        await page.press('#password', 'Enter')

        await page.wait_for_timeout(4000)

        print("[*] Перехід на Шахівницю...")
        await page.goto(CALENDAR_URL)

        print("[*] Очікуємо рендерингу сітки з даними...")
        try:
            await page.wait_for_selector('td[category_id]', timeout=20000)
            print("[+] Сітка успішно завантажена!")
        except Exception:
            print("[-] Помилка: елементи таблиці не з'явилися.")
            await browser.close()
            return {"error": "Не вдалося завантажити шахівницю."}

        # Owner fix 2026-11-19: navigate to the requested month BEFORE parsing. OtelMS
        # only shows ~46 days around browser-today, so a client asking about Jan 2027
        # from Nov 2026 gets an empty grid for Jan — which the parser then (wrongly)
        # treats as "all booked". The navigation uses the real jQuery handler so the
        # server-side re-render path matches what the UI does.
        if target_iso:
            print(f"[*] Перемикаємо календар на цільову дату {target_iso}...")
            try:
                # (1) First try the server-authoritative path: write ISO into the hidden
                #     `#date_shift` input and submit `#frmdata` directly. This is EXACTLY
                #     what the `#datein100.change` handler does and bypasses the jQuery
                #     datepicker UI entirely (datepicker libraries sometimes reject
                #     `fill()`/`type()` writes made before their "ready" event fires).
                shifted = await page.evaluate(
                    """(iso) => {
                        const ds = document.getElementById('date_shift');
                        const frm = document.getElementById('frmdata');
                        if (!ds || !frm) return false;
                        ds.value = iso;
                        // Mirror the datepicker's behaviour: also sync #datein100 so the
                        // rendered header matches, but don't fire its change (would double-submit).
                        const picker = document.getElementById('datein100');
                        if (picker) picker.value = iso;
                        frm.submit();
                        return true;
                    }""",
                    target_iso,
                )
                if not shifted:
                    print("[!] Форма #frmdata не знайдена — ймовірно, OtelMS змінив розмітку.")
                else:
                    # Wait for the grid to be rebuilt on the new date window.
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await page.wait_for_selector('td[category_id]', timeout=20000)
                    # Sanity check: the grid now MUST include the requested day_id.
                    # If OtelMS silently refused the shift (e.g. unauthorised date), we
                    # degrade gracefully — the stay-outside-calendar fallback in
                    # `bot_logic.is_room_available` turns it into "unknown", not "sold_out".
                    try:
                        want_day_id = str((datetime.strptime(target_iso, "%Y-%m-%d")
                                           - datetime(1970, 1, 1)).days)
                        found = await page.query_selector(f'td[day_id="{want_day_id}"]')
                        if found:
                            print(f"[+] Календар перемкнуто: день {target_iso} видно на сітці.")
                        else:
                            print(f"[!] Календар не містить {target_iso} після перемикання — "
                                  f"можливо OtelMS обмежив горизонт бронювання.")
                    except Exception as e:
                        print(f"[i] Пропускаю sanity-check видимості дня ({e}).")
            except Exception as e:
                # Navigation failure MUST NOT crash the scrape — fall back to the default
                # window. `bot_logic.is_room_available` will return "unknown" for nights
                # that end up outside the window, which is the correct lenient behaviour
                # (never claim "sold out" for dates we literally couldn't see).
                print(f"[-] Помилка перемикання календаря: {e}. Парсимо дефолтне вікно.")

        print("[*] Збір HTML-даних...")
        await page.wait_for_timeout(2000)
        html_content = await page.content()
        await browser.close()

        return parse_detailed_availability(html_content)

def parse_detailed_availability(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    availability = {}

    print("[*] Парсинг детальних даних по кожному номеру...")
    
    for div in soup.find_all('div', class_='calendar_rooms', attrs={'catid': True}):
        cat_id = div['catid']
        name_div = div.find('div', class_='calendar_rooms_dott')
        if not name_div: continue
            
        cat_name = name_div.text.strip()
        availability[cat_name] = {"total_available": {}, "rooms": {}}
        
        # 1. Агреговані дані
        for td in soup.find_all('td', attrs={'category_id': cat_id, 'day_id': True, 'room_id': "0"}):
            day_id_str = td['day_id']
            sum_div = td.find('div', class_='calendar_sum')
            if sum_div:
                count = int(sum_div.text.strip()) if sum_div.text.strip().isdigit() else 0
                date_str = (datetime(1970, 1, 1) + timedelta(days=int(day_id_str))).strftime('%Y-%m-%d')
                availability[cat_name]["total_available"][date_str] = count
                
        # 2. Детальні дані по фізичних кімнатах
        room_name_divs = soup.find_all('div', class_=f'btn_close_box{cat_id}')
        room_names = []
        for rd in room_name_divs:
            if 'calendar_num_room' in rd.get('class', []):
                inner_div = rd.find('div', class_='calendar_number_room')
                if inner_div:
                    room_names.append(inner_div.find('div').text.strip())
                    
        room_rows = soup.find_all('tr', class_=f'btn_close_box{cat_id}')
        
        if len(room_names) == len(room_rows):
            for i in range(len(room_names)):
                room_name = room_names[i]
                row = room_rows[i]
                availability[cat_name]["rooms"][room_name] = {}
                
                # Крок А: Спочатку заповнюємо всі видимі дати як "Available"
                for td in row.find_all('td', attrs={'day_id': True, 'room_id': True}):
                    date_str = (datetime(1970, 1, 1) + timedelta(days=int(td['day_id']))).strftime('%Y-%m-%d')
                    availability[cat_name]["rooms"][room_name][date_str] = "Available"

                # Крок Б: Шукаємо броні і перекриваємо статуси на весь діапазон
                for td in row.find_all('td', attrs={'day_id': True, 'room_id': True}):
                    booking_div = td.find('div', class_='calendar_item')
                    
                    if booking_div and booking_div.has_attr('data-title'):
                        title_text = booking_div['data-title']
                        
                        # Витягуємо дати через Regex
                        checkin_match = re.search(r'Заїзд:\s*(\d{4}-\d{2}-\d{2})', title_text)
                        checkout_match = re.search(r'Виїзд:\s*(\d{4}-\d{2}-\d{2})', title_text)
                        
                        if checkin_match and checkout_match:
                            checkin_date = datetime.strptime(checkin_match.group(1), '%Y-%m-%d')
                            checkout_date = datetime.strptime(checkout_match.group(1), '%Y-%m-%d')
                            
                            # Позначаємо всі ночі як Booked
                            current_date = checkin_date
                            while current_date < checkout_date:
                                curr_str = current_date.strftime('%Y-%m-%d')
                                # Оновлюємо статус лише якщо дата є на екрані (у нашому словнику)
                                if curr_str in availability[cat_name]["rooms"][room_name]:
                                    availability[cat_name]["rooms"][room_name][curr_str] = "Booked"
                                current_date += timedelta(days=1)
                        else:
                            # Fallback: якщо дати не знайшлися, бронюємо хоча б поточну клітинку
                            date_str = (datetime(1970, 1, 1) + timedelta(days=int(td['day_id']))).strftime('%Y-%m-%d')
                            availability[cat_name]["rooms"][room_name][date_str] = "Booked"

    return availability

if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"=== Старт тестування розумного скрапера (target_date={target!r}) ===")
    result = asyncio.run(fetch_hotel_availability(target_date=target))
    
    print("\n[+] РЕЗУЛЬТАТ (JSON):")
    print(json.dumps(result, ensure_ascii=False, indent=4))
