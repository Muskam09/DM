import asyncio
from playwright.async_api import async_playwright

LOGIN_URL = "https://desktop.otelms.com/login_c2/single_login?hmsid=18472"
USERNAME = "yaryna.danyliv17@gmail.com"       # Не забудь вставити
PASSWORD = "Qwe123"      # Не забудь вставити

async def run_scraper():
    async with async_playwright() as p:
        # Запускаємо браузер
        browser = await p.chromium.launch(headless=False) 
        context = await browser.new_context()
        page = await context.new_page()

        print("[*] Перехід на сторінку логіну...")
        await page.goto(LOGIN_URL)

        print("[*] Вводимо логін та пароль...")
        await page.locator('input[type="text"], input[name="login"], input[name="username"]').first.fill(USERNAME)
        await page.locator('input[type="password"], input[name="password"]').first.fill(PASSWORD)
        await page.keyboard.press("Enter")
        
        print("[*] Очікуємо завантаження Шахівниці (10 секунд)...")
        # Чекаємо 10 секунд, щоб сторінка 100% завантажила таблицю
        await page.wait_for_timeout(60000)
        
        print("[*] Зберігаємо HTML-код...")
        html_content = await page.content()
        
        with open("calendar.html", "w", encoding="utf-8") as f:
            f.write(html_content)
            
        print("[+] Успіх! Файл calendar.html збережено у поточній папці.")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run_scraper())