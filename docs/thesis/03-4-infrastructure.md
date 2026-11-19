# Підрозділ 3.4 — Інфраструктура та контейнеризація (Docker · Coolify · Traefik)

> Технічна фактура витягнута ВИКЛЮЧНО з реального вмісту репозиторію `DM` (гілка
> `main`). Секрети замасковано (`***`). Кожне нетривіальне твердження супроводжене
> посиланням `файл:рядок`. Академічний текст за цією фактурою пише інша модель.

Джерела розділу:
`scraper/Dockerfile`, `scraper/docker-compose.yaml` (dev), `docker-compose-coolify.yaml`
(prod), `project_spec.md` §16, `scraper/requirements.txt`. Розв'язану конфігурацію
prod-стека отримано командою `docker compose -f docker-compose-coolify.yaml config`
з тимчасовими dummy-секретами (справжній `.env` не залучався; усі значення нижче —
замасковані/фіктивні).

---

## 3.4.0 Головний висновок (читати першим)

* **Власний образ у репозиторії — рівно ОДИН:** `bot-brain` (`scraper/Dockerfile`,
  однорівнева збірка на `python:3.11-slim` + Playwright Chromium). Решта чотири
  сервіси стека (`rails`, `sidekiq`, `postgres`, `redis`) — це **готові публічні
  образи** платформи Chatwoot, які лише завантажуються, а не збираються.
* **Два варіанти Compose:** локальний dev (`scraper/docker-compose.yaml`, публікує
  порти на хост) і продакшн під Coolify (`docker-compose-coolify.yaml`, лише
  `expose` у внутрішній bridge-мережі `dt-net`, без host-біндингів).
* **Traefik керується Coolify зовні:** у самому репозиторії **немає** явних
  Traefik-міток, `entrypoints`, `middlewares` чи `certresolver`/TLS-конфігу —
  `[не знайдено в репозиторії]`. Проксі та маршрутизацію за доменом додає
  платформа Coolify під час деплою (підтверджено коментарями у compose-файлі та
  `project_spec.md:730-735`).
* **У прод-топології назовні відкрито лише `rails`** (веб Chatwoot, через проксі
  Coolify). БД, Redis і навіть сам `bot-brain` — **internal-only**; вебхук
  Chatwoot → бот іде по внутрішній мережі `http://bot-brain:8000/webhook`.

---

## 3.4.1 Docker-образ застосунку (`scraper/Dockerfile`)

Однорівнева (single-stage) збірка; multi-stage **немає**. Порядок шарів
оптимізовано під кеш (залежності ставляться до копіювання коду).

| Крок | Директива | Рядок | Призначення |
|---|---|---|---|
| Базовий образ | `FROM python:3.11-slim` | `:2` | цільовий рантайм Python 3.11 (slim-варіант Debian) |
| ENV | `PYTHONDONTWRITEBYTECODE=1` | `:4` | не писати `.pyc` |
| ENV | `PYTHONUNBUFFERED=1` | `:5` | миттєвий вивід логів у консоль |
| Робоча тека | `WORKDIR /app` | `:7` | усі подальші кроки — відносно `/app` |
| Системні пакети | `apt-get install --no-install-recommends wget gnupg` | `:9-12` | утиліти для завантаження; після — `rm -rf /var/lib/apt/lists/*` |
| Залежності (шар кешу) | `COPY requirements.txt .` | `:14` | копіюється окремо, щоб не інвалідовувати кеш при зміні коду |
| Python-пакети | `pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt` | `:16-17` | встановлення прод-залежностей |
| Браузер | `playwright install chromium && playwright install-deps chromium` | `:20` | headless-Chromium + його Linux-бібліотеки для скрапера |
| Код застосунку | `COPY . .` | `:23` | увесь вміст `scraper/` у контейнер |
| Порт | `EXPOSE 8000` | `:26` | декларація порту FastAPI |
| Запуск | `CMD ["uvicorn", "bot_server:app", "--host", "0.0.0.0", "--port", "8000"]` | `:29` | ASGI-сервер |

**Що встановлюється у Python-шар** (`scraper/requirements.txt`): `fastapi==0.110.0`,
`uvicorn==0.28.0`, `playwright==1.60.0`, `beautifulsoup4==4.14.3`, `requests==2.31.0`,
`python-dotenv==1.2.2`, `google-genai>=0.1.0`.

**Явно відсутнє в Dockerfile** (факти для повноти):

* **`ENTRYPOINT` немає** — використовується лише `CMD` (uvicorn). `[не знайдено в репозиторії]`
* **`HEALTHCHECK` немає** — контейнер не має вбудованої перевірки здоров'я. `[не знайдено в репозиторії]`
* **`USER` немає** — процес виконується від `root` (образ не понижує привілеї). `[не знайдено в репозиторії]`
* **multi-stage build немає** — фінальний образ містить і toolchain, і Chromium.

---

## 3.4.2 Оркестрація: два варіанти Compose

Обидва файли описують **той самий набір сервісів**; відрізняються публікацією
портів, наявністю явної мережі та джерелом контексту збірки `bot-brain`.

### Спільна структура

* `base: &base` — службовий **YAML-якір** (`image: chatwoot/chatwoot:latest`,
  `env_file: .env`, том `storage_data:/app/storage`), який через `<<: *base`
  вмерджується у `rails` та `sidekiq` (dev: `:2-6, 9, 24`; prod: `:20-26, 29, 45`).
* Функціональних сервісів застосунку — **п'ять**: `rails`, `sidekiq`, `postgres`,
  `redis`, `bot-brain`. (`base` — шаблон-якір, не окремий бізнес-сервіс; його
  матеріалізацію у розв'язаній конфігурації див. §3.4.3.)
* Томи: `storage_data`, `postgres_data`, `redis_data` (dev `:66-69`; prod `:104-107`).

### Ключові відмінності dev ↔ prod

| Аспект | dev (`scraper/docker-compose.yaml`) | prod (`docker-compose-coolify.yaml`) |
|---|---|---|
| Публікація портів | host-біндинги: `rails 127.0.0.1:3000`, `postgres 127.0.0.1:5433`, `redis 127.0.0.1:6379`, `bot-brain 8000:8000` (`:13-14, 38-39, 54-55, 61-62`) | **лише `expose`** (без host): `3000/5432/6379/8000` (`:34-35, 60-61, 78-79, 93-94`) |
| Мережа | неявна дефолтна bridge | **явна** `dt-net` (`driver: bridge`, `:100-102`), приєднана до кожного сервісу |
| Контекст збірки `bot-brain` | `context: .` (запуск із теки `scraper/`) (`:57-59`) | `context: ./scraper` (запуск із кореня репо) (`:86-88`) |
| Джерело секретів | локальний `scraper/.env` (`env_file: .env`) | `.env`, матеріалізований з env-UI Coolify (репо клонується без `.env`) (`:9-17, 90-91`) |
| Пароль Postgres | літерал у файлі (замасковано `***`) (`:45`) | `${POSTGRES_PASSWORD:-***}` — з env-UI, з fallback на старий літерал (`:68`) |
| Видимість `bot-brain` назовні | **так** (`8000:8000`, єдиний без `127.0.0.1`) | **ні** (internal-only; лише вебхук по `dt-net`) |

### Зведена таблиця сервісів (обидва варіанти)

| Сервіс | Образ | Порт (dev → host) | Порт (prod) | Томи | `depends_on` | Мережа (prod) | Роль |
|---|---|---|---|---|---|---|---|
| `bot-brain` | локальний `build` (`scraper/Dockerfile`) | `8000:8000` (**назовні**) | `expose 8000` (internal) | — | `rails` | `dt-net` | застосунок бота: FastAPI `/webhook`, роутинг, Chatwoot I/O, скрапер |
| `rails` | `chatwoot/chatwoot:latest` | `127.0.0.1:3000:3000` | `expose 3000` (за проксі Coolify) | `storage_data` | `postgres`, `redis` | `dt-net` | веб Chatwoot (`rails s -p 3000 -b 0.0.0.0`) |
| `sidekiq` | `chatwoot/chatwoot:latest` | — (немає) | — (немає) | `storage_data` | `postgres`, `redis` | `dt-net` | фонові задачі Chatwoot (`sidekiq -C config/sidekiq.yml`) |
| `postgres` | `pgvector/pgvector:pg16` | `127.0.0.1:5433:5432` | `expose 5432` (internal) | `postgres_data` | — | `dt-net` | БД Chatwoot |
| `redis` | `redis:alpine` | `127.0.0.1:6379:6379` | `expose 6379` (internal) | `redis_data` | — | `dt-net` | черга/кеш Chatwoot (`redis-server --requirepass ***`) |

`entrypoint`/`command` (dev `:19-20, 32, 50`; prod `:40-41, 53, 75`):
`rails` → `docker/entrypoints/rails.sh` + `bundle exec rails s -p 3000 -b 0.0.0.0`;
`sidekiq` → `bundle exec sidekiq -C config/sidekiq.yml`;
`redis` → `sh -c 'redis-server --requirepass "$REDIS_PASSWORD"'`.
`restart: always` виставлено на всіх сервісах, крім `base`-якоря.

**`healthcheck` — немає в жодному сервісі обох файлів** → `depends_on` спрацьовує за
умовою `service_started` (старт контейнера), а не `service_healthy`. `[не знайдено в
репозиторії]`

---

## 3.4.3 Розв'язана конфігурація prod-стека (`docker compose config`)

Команда `docker compose -f docker-compose-coolify.yaml config` (з фіктивним `.env`)
відпрацювала з `EXIT=0` без помилок. Витяг ключових фактів із розв'язаного YAML
(усі значення секретів — dummy/замасковані):

* **Ім'я проєкту:** `name: dm` (звідси префікс ресурсів).
* **Розв'язані імена мережі й томів:** `dm_dt-net` (`driver: bridge`),
  `dm_postgres_data`, `dm_redis_data`, `dm_storage_data`.
* **Розкриття якоря:** `<<: *base` розгорнулось у повні секції `rails`/`sidekiq`
  (успадкували `image`, `env_file`-змінні, том `storage_data`, мережу `dt-net`).
* **`base` матеріалізується як окремий запис сервісу** у розв'язаній конфігурації
  (image `chatwoot/chatwoot:latest`, без власного `command`/`depends_on`). Тобто
  файл формально описує 6 сервісів (5 функціональних + `base`). Чи стартує `base`
  окремим контейнером при деплої на Coolify — залежить від способу виклику
  `up`/вибору сервісів платформою: `[припущення — потребує перевірки]`.
* **`depends_on` розв'язано як `condition: service_started`** для всіх зв'язків
  (`bot-brain→rails`, `rails/sidekiq→postgres,redis`) — підтверджує відсутність
  healthcheck-гейтингу.
* **`build.context`** для `bot-brain` розв'язався в абсолютний шлях теки `scraper/`
  (у моєму середовищі — `…\DM\scraper`), `dockerfile: Dockerfile`.
* **`env_file` розкрився в `environment`:** усі змінні з `.env` (Chatwoot/Gemini/
  Postgres/Redis тощо) потрапляють у контейнери `rails`, `sidekiq`, `redis`,
  `bot-brain` (у `postgres` — лише його власні `POSTGRES_DB/USER/PASSWORD`, бо він
  без `env_file`). Значення `$REDIS_PASSWORD` підставилось у команду
  `redis-server --requirepass "***"`.

Фрагмент розв'язаної мережі/томів (замасковано):

```yaml
networks:
  dt-net:
    name: dm_dt-net
    driver: bridge
volumes:
  postgres_data: { name: dm_postgres_data }
  redis_data:    { name: dm_redis_data }
  storage_data:  { name: dm_storage_data }
```

---

## 3.4.4 Специфіка розгортання на Coolify

**Що є в репозиторії** (фактично):

* Окремий файл `docker-compose-coolify.yaml` у корені — це і є артефакт деплою під
  Coolify. Його заголовний коментар (`:1-17`) фіксує три відмінності від dev:
  1. **немає host-біндингів** — ingress до веб-сервісу робить проксі Coolify
     (Traefik), БД ніколи не публікуються на хост (`:4-8`);
  2. усі сервіси в **одній bridge-мережі `dt-net`** заради DNS за іменами сервісів:
     `bot-brain → rails:3000`, `rails/sidekiq → postgres:5432 / redis:6379`,
     Chatwoot-вебхуки → `http://bot-brain:8000/webhook` (`:6-8`);
  3. `bot-brain` збирається з `./scraper` і читає `env_file: .env`, який на Coolify
     **не з репозиторію** (він gitignored), а матеріалізується з env-var UI Coolify
     (`:9-11, 90-91`).
* Перелік змінних, які треба задати в **«Environment Variables» UI Coolify**,
  явно виписано в коментарі (`:13-17`): `GEMINI_API_KEY`, `CHATWOOT_TOKEN`,
  `CHATWOOT_ACCOUNT_ID`, `OTELMS_HMS_ID`, `OTELMS_LOGIN`, `OTELMS_PASSWORD`,
  `REDIS_PASSWORD`, `POSTGRES_PASSWORD` + змінні Chatwoot (`SECRET_KEY_BASE`,
  `FRONTEND_URL`, `POSTGRES_HOST=postgres`, `POSTGRES_USERNAME=postgres`,
  `POSTGRES_DATABASE=chatwoot`, `REDIS_URL=redis://:$REDIS_PASSWORD@redis:6379`,
  `RAILS_ENV=production` тощо).
* Процедура деплою з `project_spec.md` §16 (`:730-743`): ініціалізація свіжої БД —
  `bundle exec rails db:chatwoot_prepare` **усередині контейнера `rails`** через
  термінал Coolify, потім рестарт `rails`/`sidekiq` (ідемпотентно); далі — задати
  прод-`CHATWOOT_TOKEN` (+`CHATWOOT_ACCOUNT_ID`) в env-UI і **передеплоїти
  `bot-brain`**; також рекомендовано свіжий `POSTGRES_PASSWORD` (у compose лишається
  старий літерал як `${POSTGRES_PASSWORD:-***}`-fallback). Поточна фаза —
  «Test-Drive / Beta» (`:740-747`).

**Чого в репозиторії немає** (явно):

* Окремих Coolify-конфігів/маніфестів (`.coolify`, YAML платформи), Coolify-міток
  чи анотацій у самому compose — **`[не знайдено в репозиторії]`**. Уся інтеграція з
  Coolify виражена через (а) структуру `docker-compose-coolify.yaml` і (б)
  інструкції у `project_spec.md`; решту (проксі, домен, TLS, env-матеріалізація)
  робить платформа поза межами репо.

---

## 3.4.5 Traefik (reverse proxy)

**Прямий пошук по репозиторію** (`traefik|labels|entrypoints|certresolver|middleware`,
без `venv/`) дає збіги **лише в коментарях і документації**, не в конфігурації:
згадки в `docker-compose-coolify.yaml:1-8, 33`, `project_spec.md:730-735` та у
thesis-документах.

Отже, факти:

| Елемент Traefik | Стан у репозиторії |
|---|---|
| `labels:` (docker-мітки маршрутизації) | **немає в жодному сервісі** обох compose-файлів → `[не знайдено в репозиторії]` |
| `entrypoints` (web/websecure, порти 80/443) | `[не знайдено в репозиторії]` |
| `middlewares` (redirect, headers, auth) | `[не знайдено в репозиторії]` |
| `certresolver` / TLS / ACME (Let's Encrypt) | `[не знайдено в репозиторії]` |
| Статичний/динамічний конфіг Traefik (`traefik.yml`) | `[не знайдено в репозиторії]` |

**Як маршрутизація забезпечується фактично:** Traefik розгортає та конфігурує
**Coolify** автоматично під час деплою (для веб-сервісу `rails` за призначеним
доменом). У коді репозиторію проксі свідомо не описано — сервіс `rails` лише
`expose: 3000` у мережі `dt-net`, а проксі Coolify підключається до цього порту
(коментар `docker-compose-coolify.yaml:33`, «Coolify's proxy routes the assigned
domain to this port on dt-net»). Конкретні правила Host/Path, `entrypoints` та
cert-resolver формуються платформою і **у репозиторії відсутні**.

---

## 3.4.6 Мережева топологія та маршрут запиту

### Що відкрито назовні, що internal-only

**Prod (Coolify):**

| Сервіс | Зовнішня доступність |
|---|---|
| `rails` (Chatwoot web) | **назовні** — через Traefik/Coolify за доменом (єдина публічна точка входу) |
| `bot-brain` | **internal-only** — лише `expose 8000` у `dt-net`; вебхук приходить від `rails` по внутрішній мережі |
| `postgres`, `redis` | **internal-only** — `expose` без host-біндингу; на хост не публікуються ніколи (`:59-61, 77-79`) |
| `sidekiq` | без портів взагалі (фоновий воркер) |

**Dev (локально):** назовні по-справжньому відкрито лише `bot-brain` (`8000:8000`);
`rails`/`postgres`/`redis` прив'язані до `127.0.0.1` (доступні тільки з хоста, не з
мережі).

### Маршрут запиту: інтернет → Traefik → сервіс (prod)

```
[Клієнт / месенджер]
      │  HTTPS (домен)
      ▼
[Traefik — проксі Coolify]           ← єдиний публічний вхід
      │  proxy → rails:3000 (dt-net)
      ▼
[rails — Chatwoot web]  ──webhook──►  http://bot-brain:8000/webhook   (внутрішньо, dt-net)
      │  ▲                                   │
      │  │ REST http://rails:3000            │  обробка (роутинг, LLM-екстракція, ціни)
      │  └───────────────────────────────────┘
      ▼
[postgres:5432] [redis:6379]         ← internal-only (rails/sidekiq)

[bot-brain] ──HTTPS──► Gemini API (зовні)      ← вихідні з'єднання бота
[bot-brain] ──headless Chromium──► OtelMS (зовні)
```

**Внутрішні зв'язки за DNS-іменами сервісів** (працюють завдяки спільній `dt-net`):

* `rails` → `bot-brain:8000/webhook` (вебхук нового повідомлення);
* `bot-brain` → `rails:3000` — Chatwoot REST (відправка відповіді, історія, мітки;
  дефолт `CHATWOOT_URL=http://rails:3000`, підтверджено в `bot_server.py`);
* `rails`/`sidekiq` → `postgres:5432`, `redis:6379`;
* `bot-brain` → зовнішні **Gemini API** (HTTPS) та **OtelMS** (headless Chromium) —
  єдині вихідні з'єднання в інтернет із контейнера бота.

> Порівняння з dev: у локальному `scraper/docker-compose.yaml` немає `dt-net` і
> проксі; замість цього `rails`/`postgres`/`redis` слухають на `127.0.0.1`, а
> `bot-brain` опубліковано на `8000:8000`. Прод-діаграму розгортання (Mermaid) див.
> також у [03-1-architecture.md](03-1-architecture.md) §3.1.7 (в).

---

## 3.4.7 Зауваги про повноту / прогалини (для чесності фактури)

* **Traefik/TLS/домен** — конфіг не в репозиторії; генерується Coolify. Усі
  конкретні правила, сертифікати й entrypoints — `[не знайдено в репозиторії]`.
* **Healthcheck'ів немає** ні в Dockerfile, ні в compose — залежності стартують за
  `service_started`, не за станом здоров'я.
* **`base`-сервіс** формально присутній у розв'язаній конфігурації як 6-й запис;
  його фактичний старт на Coolify — `[припущення — потребує перевірки]`.
* **Схема БД Chatwoot** (Rails-міграції) — усередині образу `chatwoot/chatwoot:latest`,
  не в цьому репозиторії (див. [03-3-database.md](03-3-database.md)).
* **Секрети** у продакшні надаються через env-UI Coolify; у репозиторії реальний
  `.env` gitignored. Літеральний `POSTGRES_PASSWORD` у compose лишається лише як
  fallback і в роботі перекривається значенням з UI.

---

*Документ згенеровано з фактичного вмісту репозиторію; усі шляхи, образи, порти й
директиви наведено дослівно з файлів, секрети замасковано (`***`).*
