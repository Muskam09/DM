# Підрозділ 3.5 — Тестування діалогових сценаріїв за TDD

> Технічна фактура витягнута ВИКЛЮЧНО з реального коду репозиторію `DM` (гілка
> `main`). Кожне нетривіальне твердження супроводжене посиланням `файл:рядок`.
> Секрети замасковано (`***`). Академічний текст за цією фактурою пише інша модель.

---

## 3.5.0 Головний висновок (читати першим)

* Фреймворк — **`pytest`** (єдина test-only залежність, `scraper/requirements-dev.txt`).
* Кодова база містить **три** файли тестів у каталозі `scraper/`; жодного
  `pytest.ini`, `conftest.py`, `pyproject.toml`, `tox.ini` чи `setup.cfg` немає —
  використовується типове автовиявлення pytest.
* `pytest --collect-only -q` → **486 зібраних тест-кейсів** (з 282 функцій `def test_`;
  різниця — розкриття `@pytest.mark.parametrize`).
* Категорії: **юніт-тести чистих детермінованих модулів** (математика цін, збірник
  відповідей, чисті хелпери) та **діалогові e2e-сценарії** (Layer B — драйвер
  `process_incoming_message` із замоканими межами Chatwoot/LLM/Playwright).
* **Сліди TDD-дисципліни присутні** (тест і реалізація в одному коміті у 35 з 38
  комітів, що чіпають `scraper/*.py`; правило зафіксовано в `CLAUDE.md §5`), проте
  **строгий цикл «спочатку червоний тест окремим комітом» історією не підтверджується**
  — див. 3.5.4.
* Покриття коду **[не вимірюється]** — `pytest-cov` не встановлено, конфігу coverage
  немає (див. 3.5.5).

---

## 3.5.1 Фреймворк і структура каталогу тестів

**Фреймворк.** `scraper/requirements-dev.txt` (єдиний файл dev-залежностей):

```
pytest==8.3.4
```

Коментар у цьому ж файлі фіксує, що тести навмисно виключені з прод-образу і
запускаються всередині контейнера `bot-brain`:

```
# Test-only dependencies (kept out of the production image).
# Install inside the bot-brain container to run the suite:
#   pip install -r requirements-dev.txt && python -m pytest -v
```

Прод-залежності (`scraper/requirements.txt`) тестових пакетів не містять:
`fastapi`, `uvicorn`, `playwright`, `beautifulsoup4`, `requests`, `python-dotenv`,
`google-genai`.

**Структура.** Усі тести — поряд із кодом у `scraper/` (немає окремого каталогу
`tests/`). Один тестовий файл на модуль/шар:

| Файл тестів | Тестує (модуль) | Природа |
|---|---|---|
| `scraper/test_pricing.py` | `pricing_engine.py` — математика цін | чистий, лише stdlib |
| `scraper/test_dialogue_engine.py` | `dialogue_engine.py` — детермінований збірник відповідей + планувальник `plan()` | чистий, лише stdlib |
| `scraper/test_webhook.py` | `bot_logic.py` (хелпери) + `bot_server.process_incoming_message` (жива логіка) | двошаровий: чистий + e2e |

Конфігураційних файлів pytest **не знайдено в репозиторії** (`pytest.ini`,
`conftest.py`, `pyproject.toml`, `tox.ini`, `setup.cfg`, `.coveragerc` — відсутні).

**Двошарова конструкція `test_webhook.py`** явно задокументована в docstring файлу
(`scraper/test_webhook.py:1-13`):

* **Layer A** — чисті, безза­лежнісні хелпери (фільтр blacklist, гейт доступності,
  детекція спаму/телефону, привітання/`[SPLIT]`, шаблони). Виконуються будь-де.
* **Layer B** — жива логіка потоку: драйвить `process_incoming_message` /
  обробник `/webhook` із замоканими межами Chatwoot та Playwright і **фейковим**
  екстрактором-LLM; перевіряє детерміноване маршрутизування (суми котирувань, гейт
  доступності, редиректи, спам, drip-контекст). Потребує FastAPI/`google-genai`, тому
  **пропускається** поза контейнером (`pytest.skip`, `scraper/test_webhook.py:917`).

---

## 3.5.2 Кількість тестів і категорії

### Підрахунок (`pytest --collect-only -q`)

| Файл | Функцій `def test_` (вихідний код) | Зібрано кейсів (parametrize-розкриття) |
|---|---:|---:|
| `test_dialogue_engine.py` | 99 | 99 |
| `test_pricing.py` | 38 | 68 |
| `test_webhook.py` | 145 | 319 |
| **Разом** | **282** | **486** |

Розрив 282→486 дає `@pytest.mark.parametrize`: у `test_pricing.py` — 3
параметризовані функції, у `test_webhook.py` — 32; у `test_dialogue_engine.py`
параметризації немає (99=99).

### Категорії

**1. Юніт-тести чистих детермінованих модулів** (усі — лише stdlib + pytest,
працюють без FastAPI/LLM/Playwright):

* `test_pricing.py` — **68** кейсів на `pricing_engine.py`: кількість ночей,
  класифікація будні/вихідні, тарифні надбавки за дітей/додаткові місця, мультиномер,
  УБД-знижка, off-season, перерахунок. Частина названа за User Cases зі
  `project_spec.md` (`test_case3…`, `test_case7…`, `test_case8…` — `test_pricing.py:70,77,241,248`).
* `test_dialogue_engine.py` — **99** кейсів на `dialogue_engine.py`: парсинг слотів
  (`parse_slots`), жорсткий формат котирування, мультиномерні суми, маршрутизація
  планувальника `plan()`, off-season, **гейт доступності** (sold out → ніколи не
  ціна).
* `test_webhook.py` **Layer A** — **75** функцій `def test_` (рядки < 909): чисті
  хелпери `bot_logic` (фільтр blacklist, згортання внутрішніх підтипів у 3 публічні
  категорії, детекція спаму/телефону/великих груп, барь­єр `[SPLIT]`, контекст-хелпери).

**2. Інтеграційні / діалогові e2e-сценарії** (`test_webhook.py` **Layer B**, рядки
≥ 909): **70** функцій `def test_`, з них **66** названі `test_e2e_*`. Кожна драйвить
реальний `bot_server.process_incoming_message(...)` з замоканими межами
(Chatwoot-відправка, історія, Playwright-скрейп, LLM-екстракція) і перевіряє
поведінку діалогу цілком. Це шар, який відповідає підрозділу «діалогові сценарії».

> Термінологія: у роботі шар A доцільно називати **юніт-тестами**, шар B —
> **інтеграційними тестами діалогових сценаріїв** (перевіряється зчеплення
> маршрутизатора, планувальника, гейта доступності, движка цін і шаблонів на одному
> вхідному повідомленні/серії повідомлень).

---

## 3.5.3 Як діалоговий сценарій виражено у вигляді тесту

**Каркас (fixture `server`)** — `scraper/test_webhook.py:912-968`. Через
`monkeypatch` підмінюються ВСІ зовнішні межі, після чого стан бота (пам'ять слотів,
привітання, кеш доступності, локи розмов) скидається для ізоляції кожного тесту:

```python
@pytest.fixture
def server(monkeypatch):
    import bot_server                     # skip, якщо немає контейнерних залежностей
    sent = []            # повідомлення, «надіслані» в Chatwoot
    state = {"scraped": False, "labels": []}
    monkeypatch.setattr(bot_server, "send_chatwoot_message",
                        lambda conv_id, text: sent.append(text))
    ...
    def configure(slots=None, history=None, availability=None, ...):
        async def fake_llm(prompt, *a, **k):        # LLM-екстракція -> задані слоти
            return SimpleNamespace(text=json.dumps(slots))
        monkeypatch.setattr(bot_server, "generate_with_retry", fake_llm)
        async def fake_fetch():                     # Playwright-скрейп -> задана наявність
            state["scraped"] = True
            return availability
        monkeypatch.setattr(bot_server, "fetch_hotel_availability", fake_fetch)
        return bot_server
    return SimpleNamespace(configure=configure, sent=sent, state=state, ...)
```

Сценарій виражається так: `configure(...)` задає **вхідні слоти** (те, що «повернув»
би LLM), **історію** діалогу та **календарну наявність**; далі
`_run(process_incoming_message(текст, conv_id))` проганяє один хід; **асерти**
перевіряють `server.sent` (що бот сказав), `server.state["scraped"]` (чи був скрейп)
та додані мітки. **Багатоходовий** діалог — це кілька послідовних викликів
`process_incoming_message` з тим самим `conv_id` (спільна пам'ять слотів між ходами).

### Приклад 1 — одноходовий сценарій: детермінована сума котирування

`scraper/test_webhook.py:996-1013` (`test_e2e_price_quote_deterministic_total`):

```python
def test_e2e_price_quote_deterministic_total(server):
    bs = server.configure(
        slots={"topic": "price_quote", "rooms": [
            {"room_type": "Стандарт", "checkin": "2026-07-05", "checkout": "2026-07-07",
             "adults": 2, "children_ages": []}]},
        history=_bot_spoke(),
        availability=_raw({"Стандарт": {"2026-07-05": 3, "2026-07-06": 3}}),
    )
    _run(bs.process_incoming_message("Давайте Стандарт на 5-7 липня", 302))
    assert server.state["scraped"] is True
    assert any("Секундочку" in m for m in server.sent)   # спершу перевірено наявність
    assert any("4400 грн" in m for m in server.sent)     # 2200*2 будні ночі, НЕ 2500
    assert not any("2500" in m for m in server.sent)
```

**Що перевіряється:** запит «Стандарт на 5–7 липня для двох» → (1) виконано скрейп
календаря (`scraped is True`); (2) наявність перевірено **перед** ціною (репліка
«Секундочку»); (3) підсумок **детермінований і точний** — 4400 грн (дві будні ночі
×2200), а типова помилка LLM-арифметики (2500) відсутня. Це фіксує ключову
архітектурну властивість: ціну рахує Python, а не модель.

### Приклад 2 — багатоходовий сценарій: розподіл 6 гостей і пам'ять дат

`scraper/test_webhook.py:1491-1513` (`test_e2e_six_plus_split_uses_stored_dates`):

```python
def test_e2e_six_plus_split_uses_stored_dates(server):
    avail = _raw({"Стандарт": {"2026-07-23": 5, "2026-07-24": 5}})
    # ХІД 1: 6 дорослих на 23-24 липня -> бот пропонує розподілити, ще НЕ скрейпить
    bs = server.configure(slots={"topic": "price_quote", "rooms": [
            {"room_type": None, "checkin": "2026-07-23", "checkout": "2026-07-24",
             "adults": 6, "children_ages": []}]},
        history=_bot_spoke(), availability=avail)
    _run(bs.process_incoming_message("На 23-24 липня, нас 6 дорослих", 470))
    assert any("розподілити" in m for m in server.sent)
    assert server.state["scraped"] is False        # спершу пропозиція розподілу

    server.sent.clear()
    # ХІД 2: валідний спліт на 2 номери по 3, дати екстрактор ВТРАТИВ (None)
    server.configure(slots={"topic": "price_quote", "rooms": [
            {"room_type": None, "checkin": None, "checkout": None, "adults": 3, "children_ages": []},
            {"room_type": None, "checkin": None, "checkout": None, "adults": 3, "children_ages": []}]},
        history=_bot_spoke(), availability=avail)
    _run(bs.process_incoming_message("Зробіть 2 номери по 3 дорослих", 470))
    assert server.state["scraped"] is True                        # взяв ЗБЕРЕЖЕНІ дати
    assert templates.QUESTION_ONLY_DATES not in "\n".join(server.sent)   # не перепитав дати
```

**Що перевіряється:** діалог із **двох ходів** у межах однієї розмови (`conv_id=470`).
Хід 1: одна кімната на 6 дорослих перевищує місткість → бот проактивно **пропонує
розподіл** і НЕ скрейпить. Хід 2: клієнт погоджується («2 номери по 3»), але LLM у
слотах **втратив дати** (`None`) — тест перевіряє, що бот **дістає дати з пам'яті**
попереднього ходу (скрейп відбувся) і **не перепитує** їх. Так сценарій кодує вимогу
довготривалої пам'яті слотів (`CLAUDE.md §4, правило 7`).

Обидва приклади ілюструють спосіб вираження сценарію: `Given` (слоти + історія +
наявність через `configure`) → `When` (`process_incoming_message`) → `Then` (асерти
на `server.sent` / `server.state`).

---

## 3.5.4 Ознаки TDD у git-історії

**А. Реалізація і тести з'являються в одному коміті** (перевірено `git log --follow`
за першою появою файлів):

| Файл реалізації | Файл тесту | Коміт першої появи |
|---|---|---|
| `pricing_engine.py` | `test_pricing.py` (+`bot_logic.py`, `test_webhook.py`) | `d7cf318` «Initialize automated tests, deterministic pricing engine…» (2026-06-16) |
| `dialogue_engine.py` | `test_dialogue_engine.py` | `9634600` «Make pricing/availability deterministic…» (2026-06-16) |

Статистика коміту `d7cf318` (одночасне введення движка й тестів):

```
scraper/pricing_engine.py    | 277 ++++...     (нова реалізація)
scraper/test_pricing.py      | 177 ++++...     (нові тести)
scraper/test_webhook.py      | 287 ++++...     (нові тести)
scraper/bot_logic.py         |  96 ++++...
```

**Б. Тест і зміна поведінки — разом.** З **38** комітів, що змінюють `scraper/*.py`,
**35** одночасно чіпають файл `test_*.py`. Три винятки — ранній `bot_server.py`
(«Project init», 2026-06-04, **до** введення тестового фреймворку 2026-06-16) та
кілька інфра/prompt-правок. Приклади парних комітів «фікс + тест» (subject → impl/test):

```
067fe09  impl=1 test=1  Live-QA fix: bare "Так" after a window offer always QUOTES the offered dates
a727eed  impl=2 test=1  Fix anti-spam deadlock when client has no dates yet
ae078d0  impl=1 test=1  Autofix (live sim): fuzzy period normalized to YYYY-MM offered wrong-month windows
c5cff78  impl=2 test=1  Pin "де знаходиться" to PLACE (deterministic location-question override)
d33b5e7  impl=2 test=2  Fix availability gating for out-of-window dates and month-only off-season/price
```

**В. Дисципліна зафіксована як правило проєкту.** `CLAUDE.md §5` («Coding
guidelines»):

> «Determinism over LLM arithmetic … Any new price rule must land in
> `pricing_engine.py` **with a test** *and* be mirrored in the `sys_prompt` wording.»
> «Never break the 8 User Cases in `project_spec.md §6`. **Add a test before changing
> behaviour they depend on**.»

Це підтверджується прямим маппінгом тестів на User Cases зі специфікації
(`test_case3_…`, `test_case7_…`, `test_case8_…` у `test_pricing.py`) та ~118 згадками
номерів кейсів/правил/персон/багів у коментарях тестів.

**Г. Чесне застереження щодо строгого red-green-refactor.**
`[припущення — потребує перевірки]` За історією комітів **не можна довести**, що
кожен тест був написаний і «падав» *окремим комітом перед* реалізацією: тест і код
здебільшого лягають **одним** комітом (патерн «test-alongside»). Формальний слід
циклу red→green (окремий коміт з червоним тестом, потім коміт із зеленою реалізацією)
у репозиторії **відсутній**. Фактично підтверджується сильніше слабке твердження:
розробка **тесто-супроводжувана** — жодна зміна детермінованої поведінки не
злита без відповідного тесту в тому ж коміті, а самі назви/коментарі тестів явно
описують сценарій-баг, який вони закривають (наприклад `test_e2e_*` із приміткою
«Fix N», «Bug N», «§9.11», «owner #21»), що відповідає формулюванню «пишемо тест на
дефект, потім усуваємо дефект».

---

## 3.5.5 Покриття коду

**Покриття коду `[не вимірюється]`.** Підстави:

* `pytest-cov` **не встановлено** — `pip show pytest-cov` → `WARNING: Package(s) not
  found`; у `scraper/requirements-dev.txt` лише `pytest==8.3.4`.
* Немає конфігурації coverage (`.coveragerc`, секції `[tool.coverage.*]` у
  `pyproject.toml`, `--cov` у якомусь ini) — **не знайдено в репозиторії**.
* Немає збережених звітів (`.coverage`, `htmlcov/`, `coverage.xml`) — **не знайдено в
  репозиторії**.

Кількісні орієнтири, які РЕАЛЬНО фіксуються замість coverage (їх коректно навести як
проксі-метрику обсягу тестування, не називаючи «покриттям рядків»):

* **486** зібраних тест-кейсів на застосунок, чиї модулі — `pricing_engine.py`,
  `dialogue_engine.py`, `bot_logic.py`, `bot_server.py` (перелік — [00-overview.md](00-overview.md) §2);
* усі **три** чисті детерміновані модулі (`pricing_engine`, `dialogue_engine`,
  `bot_logic`) мають власні набори тестів; жива логіка `bot_server.process_incoming_message`
  покрита 66 e2e-сценаріями Layer B;
* прямий маппінг на 8 User Cases специфікації (`test_caseN_*`).

> Якщо в роботі потрібні відсотки покриття — їх слід виміряти окремо
> (`pip install pytest-cov && python -m pytest --cov=. --cov-report=term`), оскільки
> станом на гілку `main` вони в репозиторії відсутні.

---

## 3.5.6 Команди відтворення (для додатків роботи)

```bash
# кількість і перелік тестів
python -m pytest scraper/ --collect-only -q          # -> 486 tests collected

# повний прогін (Layer B пропускається поза контейнером bot-brain)
python -m pytest scraper/ -v

# лише чисті модулі (працюють будь-де, без FastAPI/LLM/Playwright)
python -m pytest scraper/test_pricing.py scraper/test_dialogue_engine.py -v

# повний прогін усередині контейнера (Layer B активний)
pip install -r scraper/requirements-dev.txt && python -m pytest scraper/ -v
```
