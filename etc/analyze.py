from bs4 import BeautifulSoup

print("[*] Читаємо calendar.html...\n")
with open("calendar.html", "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f, 'html.parser')

cat_id = "13" # Шукаємо дані для "Стандарт"
print(f"[*] Шукаємо праву частину таблиці для категорії {cat_id}...\n")

# Варіант 1: Шукаємо по ID, який містить число 13 (ігноруючи ліве меню)
for tag in soup.find_all(True, id=True):
    tag_id = tag['id']
    if cat_id in tag_id and 'btn_close' not in tag_id:
        print(f"--- Знайдено зв'язок по ID: {tag_id} ---")
        print(f"Тег: <{tag.name}>, Класи: {tag.get('class')}")
        print("Вміст (перші 500 символів):")
        print(tag.prettify()[:500])
        print("-" * 50 + "\n")

# Варіант 2: Шукаємо по інших атрибутах (наприклад, row-id="13")
for tag in soup.find_all(True):
    for attr, value in tag.attrs.items():
        if attr not in ['catid', 'id'] and str(value) == cat_id:
            print(f"--- Знайдено зв'язок по атрибуту {attr}='{value}' ---")
            print(f"Тег: <{tag.name}>, Класи: {tag.get('class')}")
            print("Вміст (перші 500 символів):")
            print(tag.prettify()[:500])
            print("-" * 50 + "\n")