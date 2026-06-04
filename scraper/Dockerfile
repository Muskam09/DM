# Крок 1: Використовуємо офіційний легкий образ Python
FROM python:3.11-slim
# Налаштовуємо змінні оточення для Python, щоб логі виводилися в консоль миттєво
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Встановлюємо робочу директорію всередині контейнера
WORKDIR /app
# Встановлюємо базові системні утиліти, які потрібні для завантаження залежностей
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*
# Копіюємо файл залежностей
COPY requirements.txt .
# Оновлюємо pip та встановлюємо Python-пакети
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt
# КРИТИЧНИЙ КРОК ДЛЯ DevOps: Встановлюємо Chromium та ВСІ його системні бібліотеки
# Прапорець --with-deps підтягне потрібні Linux-пакети для роботи браузера у headless режимі
RUN playwright install chromium && playwright install-deps chromium

# Копіюємо весь інший код нашого додатка в контейнер
COPY . .

# Відкриваємо порт 8000, на якому працюватиме FastAPI
EXPOSE 8000

# Запускаємо наш FastAPI сервер за допомогою uvicorn
CMD ["uvicorn", "bot_server:app", "--host", "0.0.0.0", "--port", "8000"]