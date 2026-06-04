from bs4 import BeautifulSoup

with open("calendar.html", "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f, 'html.parser')

# Знаходимо першу бронь
booking = soup.find('div', class_='calendar_item')

if booking:
    print("[+] Знайдено блок броні! Ось усі його атрибути:\n")
    for attr, value in booking.attrs.items():
        print(f"{attr}: {value}")
else:
    print("[-] Бронь не знайдено.")
