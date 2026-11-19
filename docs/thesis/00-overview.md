# Розділ 3 — Інвентаризація репозиторію (оглядовий документ)

> Джерело: реальний вміст репозиторію `DM` (гілка `main`). Секрети замасковано (`***`).
> Виключено з обходу: `venv/`, `node_modules`, `__pycache__`, `.git`.

---

## 1. Структура проєкту (до 3 рівнів)

```
DM/
├── CLAUDE.md                     # операційний гайд для AI/інженера (правила поведінки бота)
├── project_spec.md               # ТЗ: архітектура, бізнес-правила, User Cases (джерело істини)
├── skills.md                     # бізнес-математика (тарифи, діти, знижки)
├── README.md                     # опис проєкту
├── LICENSE
├── pricing_backup.json           # (у etc/) резервна копія цінового конфігу
├── docker-compose-coolify.yaml   # конфіг деплою під Coolify
├── stress_scenarios.json         # сценарії для стрес-тесту
│
├── scraper/                      # ← ДЕПЛОЙОВАНИЙ ЗАСТОСУНОК
│   ├── bot_server.py             # FastAPI-ядро: webhook, LLM-екстракція, роутинг, Chatwoot I/O, кеш
│   ├── dialogue_engine.py        # детермінований конструктор відповіді: plan() + finalize_quote()
│   ├── bot_logic.py              # чисті хелпери: гейт доступності, spam/phone/large-group, [SPLIT]
│   ├── pricing_engine.py         # детермінована цінова математика (дати/будні-вихідні/тотал)
│   ├── templates.py              # база знань: усі клієнтські текстові шаблони
│   ├── hotel_scraper.py          # Playwright-скрапер календаря OtelMS → JSON доступності
│   ├── pricing.json              # конфіг цін і правил (тип кімнати / місяць / будні-вихідні)
│   ├── test_pricing.py           # unit-тести pricing_engine
│   ├── test_dialogue_engine.py   # unit-тести конструктора відповіді
│   ├── test_webhook.py           # тести хелперів + e2e живого потоку (екстракція мокнута)
│   ├── auto_qa.py                # офлайн авто-QA (швидкий)
│   ├── stress_test_live.py       # живий стрес-тест
│   ├── requirements.txt          # прод-залежності
│   ├── requirements-dev.txt      # тест-залежності (pytest)
│   ├── Dockerfile                # образ bot-brain (python:3.11-slim + Playwright Chromium)
│   └── docker-compose.yaml       # повний стек: bot-brain + Chatwoot (rails/sidekiq/postgres/redis)
│
├── etc/                          # legacy / чернетки
│   ├── analyze.py, check_booking.py, find_room.py, scraper.py   # скретч-скрипти
│   ├── calendar.html, debug.html, test.html                     # застарілі HTML
│   └── pricing_backup.json
│
├── live_auto_qa.py               # ЖИВИЙ авто-QA (без моків: реальні Chatwoot+Gemini+скрапер)
├── simulate_live_chat.py         # драйвер живого чату через Chatwoot
├── analyze_ig_export.py          # аналіз експорту Instagram-переписки
├── ig_analysis_summary.json      # результат аналізу IG
│
├── your_instagram_activity/      # [дані, не код] дамп IG-переписки (сотні message_*.json)
└── context.txt                   # [UNTRACKED] сирий лог розробки (не комітиться; можливі секрети)
```

**Примітки:**
- `venv/` присутній на диску (локальне віртуальне середовище — `.venv`-подібне) і виключений з інвентаризації.
- `scraper/.env` існує локально, **gitignored** (секрети `GEMINI_API_KEY`, Chatwoot/OtelMS — замасковано `***`).
- `your_instagram_activity/` — вивантаження даних Instagram (не частина застосунку).

---

## 2. Технологічний стек

**Мова:** Python (цільовий рантайм — **Python 3.11**, з `Dockerfile: FROM python:3.11-slim`).
Проєкт **чисто Python** — немає `pyproject.toml` і немає власного `package.json`
(єдиний `package.json` — всередині залежності Playwright у `venv/`, не рахується).

**Прод-залежності** (`scraper/requirements.txt`):

| Пакет | Версія | Призначення |
|---|---|---|
| `fastapi` | `0.110.0` | вебсервер / webhook API |
| `uvicorn` | `0.28.0` | ASGI-сервер запуску |
| `playwright` | `1.60.0` | headless-браузер для скрапінгу календаря OtelMS |
| `beautifulsoup4` | `4.14.3` | парсинг HTML «Шахівниці» |
| `requests` | `2.31.0` | HTTP-виклики до Chatwoot API |
| `python-dotenv` | `1.2.2` | завантаження `.env` |
| `google-genai` | `>=0.1.0` | LLM-екстракція слотів (Gemini) |

**Тест-залежності** (`scraper/requirements-dev.txt`):

| Пакет | Версія | Призначення |
|---|---|---|
| `pytest` | `8.3.4` | запуск unit/e2e-тестів (тримається поза прод-образом) |

**Інфраструктурний стек** (з `scraper/docker-compose.yaml`):

| Сервіс | Образ | Роль |
|---|---|---|
| `rails` | `chatwoot/chatwoot:latest` | Chatwoot вебдодаток |
| `sidekiq` | `chatwoot/chatwoot:latest` | фонові задачі Chatwoot |
| `postgres` | `pgvector/pgvector:pg16` | БД Chatwoot |
| `redis` | `redis:alpine` | черга / кеш Chatwoot |
| `bot-brain` | локальний `build` (`scraper/Dockerfile`) | застосунок бота (FastAPI) |

Тома: `storage_data`, `postgres_data`, `redis_data`.

---

## 3. Точки входу

| Що запускається | Як | Файл:рядок |
|---|---|---|
| **Основний застосунок (прод)** | `uvicorn bot_server:app --host 0.0.0.0 --port 8000` (CMD у Dockerfile) | `scraper/Dockerfile:29`; `scraper/bot_server.py:26` |
| **ASGI-об'єкт** | `app = FastAPI()` | `scraper/bot_server.py:26` |
| **Головний ендпойнт** | `@app.post("/webhook")` — приймає webhook Chatwoot | `scraper/bot_server.py:894` |
| **Скрапер (standalone)** | `if __name__ == "__main__":` → `fetch_hotel_availability()` | `scraper/hotel_scraper.py:134`, `:19` |
| **Живий авто-QA** | `python live_auto_qa.py` | `live_auto_qa.py` (корінь) |
| **Симулятор чату** | `python simulate_live_chat.py` | `simulate_live_chat.py` (корінь) |
| **Офлайн авто-QA** | `python scraper/auto_qa.py` | `scraper/auto_qa.py` |
| **Тести** | `pytest scraper/` | `scraper/test_*.py` |

Воркерів власної розробки (Celery/RQ тощо) немає; єдиний «воркер» — це `sidekiq`
всередині стеку Chatwoot. Порт застосунку — `8000` (`EXPOSE 8000`).

---

## 4. Компоненти верхнього рівня (по одному рядку)

**Ядро застосунку (`scraper/`):**

| Компонент | Призначення |
|---|---|
| `bot_server.py` | FastAPI-ядро: webhook, LLM-екстракція слотів, детермінований роутинг, Chatwoot I/O, кеш доступності |
| `dialogue_engine.py` | Чистий детермінований конструктор відповіді: `plan()` + `finalize_quote()` + жорсткий формат котирування |
| `bot_logic.py` | Чисті хелпери: фільтр/гейт доступності, детекція spam/phone/large-group, `[SPLIT]`, greeting |
| `pricing_engine.py` | Детермінована цінова математика: дати/будні-вихідні/тотал/мульти-кімната/off-season |
| `templates.py` | База знань: усі клієнтські текстові шаблони (FAQ, ціни, редиректи, closes) |
| `hotel_scraper.py` | Playwright-скрапер календаря OtelMS «Шахівниця» → JSON доступності |
| `pricing.json` | Конфіг цін і правил (тип кімнати / місяць / будні-вихідні) |

**Тести (`scraper/`):**

| Компонент | Призначення |
|---|---|
| `test_pricing.py` | Unit-тести цінового рушія (точні суми UAH, мульти-кімната, off-season) |
| `test_dialogue_engine.py` | Unit-тести конструктора відповіді (формат, гейтинг, планування) |
| `test_webhook.py` | Хелпери + e2e живого потоку (екстракція мокнута) |

**QA / утиліти:**

| Компонент | Призначення |
|---|---|
| `live_auto_qa.py` | Живий self-audit проти реального Chatwoot+Gemini+скрапера (без моків) |
| `simulate_live_chat.py` | Драйвер сценаріїв живого чату через Chatwoot API |
| `scraper/auto_qa.py` | Швидкий офлайн авто-QA |
| `scraper/stress_test_live.py` | Живий стрес-тест |
| `analyze_ig_export.py` | Аналіз експорту Instagram-переписки → `ig_analysis_summary.json` |

**Документація / конфіг:**
`CLAUDE.md`, `project_spec.md` (ТЗ), `skills.md` (бізнес-математика), `README.md`,
`docker-compose-coolify.yaml`, `scraper/docker-compose.yaml`, `scraper/Dockerfile`.

---

## 5. Обсяг коду (для розділу 4 — трудомісткість)

> Метод: `wc -l` по `*.py` (немає tokei/cloc). Тести й дані рахуються окремо.

**Ядро застосунку (прод-логіка, `scraper/`):**

| Файл | Рядків |
|---|---|
| `dialogue_engine.py` | 1157 |
| `bot_logic.py` | 1193 |
| `bot_server.py` | 926 |
| `pricing_engine.py` | 474 |
| `templates.py` | 380 |
| `hotel_scraper.py` | 138 |
| **Разом ядро** | **4268** |

**Тести (`scraper/`):**

| Файл | Рядків |
|---|---|
| `test_webhook.py` | 1996 |
| `test_dialogue_engine.py` | 1019 |
| `test_pricing.py` | 329 |
| **Разом тести** | **3344** |

**QA / утиліти:**

| Файл | Рядків |
|---|---|
| `live_auto_qa.py` (корінь) | 701 |
| `simulate_live_chat.py` (корінь) | 445 |
| `scraper/auto_qa.py` | 478 |
| `analyze_ig_export.py` (корінь) | 197 |
| `scraper/stress_test_live.py` | 94 |
| **Разом QA/утиліти** | **1915** |

**Legacy (`etc/`):** ~95 рядків (`analyze.py`, `scraper.py`, `find_room.py`, `check_booking.py`).

**Підсумок Python-коду:**

| Категорія | Рядків | Частка |
|---|---|---|
| Ядро застосунку | 4268 | ~43 % |
| Тести | 3344 | ~34 % |
| QA / утиліти | 1915 | ~19 % |
| Legacy (etc/) | 95 | ~1 % |
| **Усього `.py`** | **~9622** | 100 % |

> Співвідношення тести:ядро ≈ **0.78 : 1** (майже рядок тесту на рядок прод-коду).
> Конфіг/база знань поза підрахунком рядків коду: `pricing.json`, шаблони у `templates.py`.

---

## 6. Активність розробки

| Показник | Значення |
|---|---|
| Усього комітів | **46** |
| Перший коміт | `be33203` — «Initial commit», **2026-06-04** |
| Останній коміт | `b85671d` — «Docs: Save state before terminal clear», **2026-07-13** |
| Період активності | **2026-06-04 → 2026-07-13** (≈ 39 днів) |
| Середній темп | ≈ 1.2 коміта/день |

Робота велася спринтами (за повідомленнями комітів простежуються Sprint 4–5:
kill-switch міток, bed-availability, flip-flop-фікси, персони/User Cases).

---

*Документ згенеровано інвентаризацією фактичного вмісту репозиторію; усі шляхи та
версії наведено дослівно з файлів.*
