from bs4 import BeautifulSoup
import re

print("[*] Шукаємо структуру фізичної кімнати...")
with open("calendar.html", "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f, 'html.parser')

# Шукаємо будь-яку згадку конкретної кімнати
element = soup.find(string=re.compile("Ротило"))

if element:
    # Беремо батьківський контейнер 4-го рівня, щоб точно захопити всі атрибути рядка
    container = element.parent.parent.parent.parent
    print("[+] Знайдено! Ось структура:")
    print("-" * 50)
    print(container.prettify()[:800])
    print("-" * 50)
else:
    print("[-] Кімнату не знайдено.")