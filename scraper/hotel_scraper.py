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

async def fetch_hotel_availability():
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
        except Exception as e:
            print("[-] Помилка: елементи таблиці не з'явилися.")
            await browser.close()
            return {"error": "Не вдалося завантажити шахівницю."}
        
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
    print("=== Старт тестування розумного скрапера ===")
    result = asyncio.run(fetch_hotel_availability())
    
    print("\n[+] РЕЗУЛЬТАТ (JSON):")
    print(json.dumps(result, ensure_ascii=False, indent=4))