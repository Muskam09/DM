#!/usr/bin/env python3
"""
live_auto_qa.py — END-TO-END live Auto-QA for the D&T Hotel bot (ZERO mocking).

Unlike the offline harness (scraper/auto_qa.py), this drives the FULL real stack:
  * pushes customer messages through the LIVE Chatwoot Public API (a real inbox),
  * fires the SAME `message_created` webhook Chatwoot would send to the running
    bot container (http://localhost:8000/webhook) — so the REAL extraction LLM
    (Gemini) classifies, the REAL Playwright scraper hits the REAL OtelMS calendar,
    and the bot posts its reply back into the conversation,
  * then pulls the REAL, final transcript back from the Chatwoot agent API and
  * runs an automated audit over the LIVE transcript (fix invariants + a global
    red-flag scan for hallucinations / a nonexistent tennis court / internal names).

So it catches what mocks cannot: real LLM hallucinations & routing, webhook/async
race conditions, scraper latency and actual calendar availability.

Drip timing (realistic human typing) is 20s by default (owner mandate). The ONE
exception is the batch-ordering persona: Fix 3 only engages when a burst arrives
while the bot is still processing (supersede collapses it to one reply), so that
persona is rapid-fired on purpose (a 20s gap would never form a batch).

Reads CHATWOOT_TOKEN / CHATWOOT_ACCOUNT_ID from scraper/.env at runtime (NO secret
is embedded here). Stdlib only — runs on the host: `python live_auto_qa.py`.
Select personas: `python live_auto_qa.py 2 3 5 10`. Dump only: `--read <id ...>`.
Exit 0 = every live audit passed AND no red flag; non-zero otherwise.
"""
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "scraper", ".env")
BASE_URL = os.environ.get("CHATWOOT_PUBLIC_URL", "http://localhost:3000")
BOT_WEBHOOK_URL = os.environ.get("BOT_WEBHOOK_URL", "http://localhost:8000/webhook")
SIM_INBOX_NAME = "Bot Simulator (API)"


def _delay_env(name, default):
    v = os.environ.get(name)
    return int(v) if v and v.isdigit() else default


DRIP_DEFAULT = _delay_env("SIM_DRIP", 20)     # seconds between fragments (realistic typing)
BURST_DRIP = _delay_env("SIM_BURST", 2)       # seconds between a rapid-fire burst (Fix 3)
SETTLE_SECONDS = _delay_env("SIM_SETTLE", 90)  # wait for the bot to finish the last reply
PERSONA_GAP = _delay_env("SIM_PERSONA", 5)


def load_env(path):
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


_env = load_env(ENV_PATH)
TOKEN = os.environ.get("CHATWOOT_TOKEN", _env.get("CHATWOOT_TOKEN", ""))
ACCOUNT_ID = os.environ.get("CHATWOOT_ACCOUNT_ID", _env.get("CHATWOOT_ACCOUNT_ID", "1"))


# --------------------------------------------------------------------------- #
# Chatwoot plumbing (identical code path to the real webhook flow)
# --------------------------------------------------------------------------- #

def _request(method, url, body=None, token=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["api_access_token"] = token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def app_api(method, path, body=None):
    return _request(method, f"{BASE_URL}{path}", body, token=TOKEN)


def public_api(method, path, body=None):
    return _request(method, f"{BASE_URL}/public/api/v1{path}", body)


def trigger_bot(conv_id, content):
    """POST the exact `message_created` payload Chatwoot's account webhook sends."""
    body = {"event": "message_created", "message_type": "incoming",
            "content": content, "conversation": {"id": conv_id}}
    try:
        _request("POST", BOT_WEBHOOK_URL, body)
    except Exception as e:
        print(f"      [!] bot trigger failed: {e}")


def find_or_create_sim_inbox():
    inboxes = app_api("GET", f"/api/v1/accounts/{ACCOUNT_ID}/inboxes").get("payload", []) or []
    inbox_id = None
    for ib in inboxes:
        if ib.get("name") == SIM_INBOX_NAME and ib.get("channel_type") == "Channel::Api":
            inbox_id = ib["id"]
            break
    if inbox_id is None:
        created = app_api("POST", f"/api/v1/accounts/{ACCOUNT_ID}/inboxes",
                          {"name": SIM_INBOX_NAME, "channel": {"type": "api", "webhook_url": ""}})
        inbox_id = created["id"]
    detail = app_api("GET", f"/api/v1/accounts/{ACCOUNT_ID}/inboxes/{inbox_id}")
    return inbox_id, detail.get("inbox_identifier")


def inject_persona(identifier, name, messages, drip):
    contact = public_api("POST", f"/inboxes/{identifier}/contacts", {"name": name})
    source_id = contact.get("source_id")
    conv = public_api("POST", f"/inboxes/{identifier}/contacts/{source_id}/conversations", {})
    conv_id = conv.get("id")
    msg_path = f"/inboxes/{identifier}/contacts/{source_id}/conversations/{conv_id}/messages"
    for i, msg in enumerate(messages):
        public_api("POST", msg_path, {"content": msg})
        trigger_bot(conv_id, msg)
        tag = "  ⚡BURST" if drip <= BURST_DRIP else ""
        print(f"      → msg {i + 1}/{len(messages)}{tag}: \"{msg[:60]}{'…' if len(msg) > 60 else ''}\"")
        if i < len(messages) - 1:
            time.sleep(drip)
    return conv_id


def fetch_rows(conv_id):
    # Chatwoot returns only the LAST ~20 messages per page; a long persona (Persona 26 = 24 msgs +
    # bot replies) overflows that, truncating the EARLY FAQ answers (e.g. the address) from the audit.
    # Paginate backward via ?before=<id> so the audit sees the WHOLE conversation.
    seen = {}
    before = None
    for _ in range(30):                        # safety cap on pages
        path = f"/api/v1/accounts/{ACCOUNT_ID}/conversations/{conv_id}/messages"
        if before is not None:
            path += f"?before={before}"
        data = app_api("GET", path)
        msgs = data if isinstance(data, list) else (
            data.get("payload") or data.get("messages") or data.get("data") or [])
        ids = [m.get("id") for m in msgs if m.get("id") is not None]
        if not ids:
            break
        for m in msgs:
            if m.get("id") is not None:
                seen[m["id"]] = m
        page_min = min(ids)
        if before is not None and page_min >= before:   # no older messages returned -> done
            break
        before = page_min
    rows = []
    for m in sorted(seen.values(), key=lambda x: x.get("id", 0)):
        content = (m.get("content") or "").strip()
        if not content or m.get("private"):
            continue
        mt = m.get("message_type")
        if mt in (0, "incoming"):
            rows.append(("client", content))
        elif mt in (1, "outgoing"):
            rows.append(("bot", content))
    return rows


def print_transcript(name, conv_id, rows=None):
    rows = rows if rows is not None else fetch_rows(conv_id)
    print(f"\n┌─ {name}  (conv #{conv_id}) " + "─" * 24)
    for role, content in rows:
        who = "👤 Клієнт" if role == "client" else "🤖 Бот   "
        lines = content.split("\n")
        print(f"│ {who}: {lines[0]}")
        for extra in lines[1:]:
            print(f"│           {extra}")
    print("└" + "─" * 46)


# --------------------------------------------------------------------------- #
# LIVE audits — fix invariants over the REAL transcript (tolerant to LLM phrasing)
# --------------------------------------------------------------------------- #

HALLUCINATION_TOKENS = ["хом", "боярин", "гропа", "баба людова", "11 номер"]
TENNIS_TOKENS = ["тенісн", "тенісний корт", "корт"]   # "настільний теніс" is fine (no match)


def bot_text(rows):
    return "\n".join(c for role, c in rows if role == "bot").lower()


def red_flags(rows):
    t = bot_text(rows)
    flags = []
    for tok in HALLUCINATION_TOKENS:
        if tok in t:
            flags.append(f"hallucinated/internal room name «{tok}»")
    for tok in TENNIS_TOKENS:
        if tok in t:
            flags.append(f"nonexistent tennis court «{tok}»")
    return flags


# --- LIVE WINDOW VALIDATION (owner mandate): every OFFERED date window must have EVERY night
#     free. We scrape ONE availability snapshot, parse "A - B місяць" ranges from the bot text,
#     and flag any offered window whose interior nights are booked (a bridge / hallucination). ---
_UA_GEN_MONTH = {"січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
                 "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12}
_MON = "|".join(_UA_GEN_MONTH)
_WIN_RE = re.compile(r"(\d{1,2})\s*[-–—]\s*(\d{1,2})\s+(" + _MON + r")")
# cross-month: "29 липня - 8 серпня"
_WIN_XMON_RE = re.compile(r"(\d{1,2})\s+(" + _MON + r")\s*[-–—]\s*(\d{1,2})\s+(" + _MON + r")")


def scrape_snapshot():
    """Best-effort {date_iso: max free count across the 3 public types} via the container.
    Taken once, right after the personas run, so it closely matches what the bot scraped."""
    code = (
        "import asyncio,json\n"
        "from hotel_scraper import fetch_hotel_availability\n"
        "import bot_logic\n"
        "s=bot_logic.build_simplified_availability(asyncio.run(fetch_hotel_availability()))\n"
        "m={}\n"
        "for v in s.values():\n"
        " for d,c in (v or {}).items():\n"
        "  m[d]=max(m.get(d,0), c or 0)\n"
        "print('SNAP'+json.dumps(m))\n")
    try:
        out = subprocess.run(["docker", "exec", "scraper-bot-brain-1", "python", "-c", code],
                             capture_output=True, text=True, timeout=150)
        for line in out.stdout.splitlines():
            if line.startswith("SNAP"):
                return json.loads(line[4:])
    except Exception as e:
        print(f"   [window-validation snapshot unavailable: {e}]")
    return None


def window_violations(rows, snapshot, year=2026):
    """Offered same-month windows whose interior nights [A, B) are booked in the snapshot.
    A booked night INSIDE an offered range == the exact bridging/hallucination the owner flagged."""
    if not snapshot:
        return []
    import datetime
    def _nights_booked(start_iso, end_iso, label):
        out = []
        d = datetime.date.fromisoformat(start_iso)
        end = datetime.date.fromisoformat(end_iso)
        while d < end:                          # nights of the stay (checkout day excluded)
            if snapshot.get(d.isoformat(), 1) <= 0:
                out.append(f"offered «{label}» spans BOOKED night {d.isoformat()}")
            d += datetime.timedelta(days=1)
        return out

    viol = []
    for role, content in rows:
        if role != "bot":
            continue
        for m in _WIN_XMON_RE.finditer(content):        # cross-month first (superset pattern)
            a, m1, b, m2 = int(m.group(1)), _UA_GEN_MONTH[m.group(2)], int(m.group(3)), _UA_GEN_MONTH[m.group(4)]
            y2 = year + 1 if m2 < m1 else year          # month wrap (Dec->Jan)
            viol += _nights_booked(f"{year}-{m1:02d}-{a:02d}", f"{y2}-{m2:02d}-{b:02d}",
                                   f"{a} {m.group(2)} - {b} {m.group(4)}")
        for m in _WIN_RE.finditer(content):             # same-month
            a, b, mon = int(m.group(1)), int(m.group(2)), _UA_GEN_MONTH[m.group(3)]
            if b <= a:
                continue
            viol += _nights_booked(f"{year}-{mon:02d}-{a:02d}", f"{year}-{mon:02d}-{b:02d}",
                                   f"{a} - {b} {m.group(3)}")
    return sorted(set(viol))


def has(*subs):
    return lambda t: any(s in t for s in subs)


def lacks(*subs):
    return lambda t: all(s not in t for s in subs)


# (index, name, [messages], drip_seconds, [ (desc, predicate_on_lowercased_bot_text) ])
#
# Persona numbering matches simulate_live_chat.py. The owner's manual QA (2026-07-08) marked
# personas 1,3,4,6,9,10,12,13,14,16 as PASSING → COMMENTED OUT below (kept, not deleted, so
# they can be re-enabled). Personas 2,5,7,8,11,15 carried owner notes (the six Phase-2 fixes)
# and 17-20 are NEW → all ACTIVE. See DEFAULT_SET.
PERSONAS = {
    # ----- PASSED without owner notes (2026-07-08) — commented out, kept for re-runs -----
    # 1: ("1️⃣ Зміна дат на льоту",
    #     ["Доброго дня!", "Хочу забронювати номер!", "17-19 липня",
    #      "двоє дорослих та дитина 8 років", "Стандарт +", "що по харчуванню?",
    #      "що входить у вартість?", "наші дати змінились, хочемо заїхати 24 липня на 3 дні",
    #      "Хочу забрювати, треба платити передоплату?"],
    #     DRIP_DEFAULT,
    #     [("coherent quote/window", has("грн", "віконц", "заброньован"))]),

    2: ("2️⃣ УБД + початок серпня + 2 Стандарти + харчування + меню (fix #266, food)",
        ["Четверо дорослих",
         "початок серпня",
         "харчування є?",
         "Давайте 2 номери Стандарт",
         "А є знижка по УБД на один номер?",
         "Порахуйте харчування 3-разове на 4 особи: 2 дні повне харчування, а останній день лише сніданок",
         "Які там страви подають?"],
        DRIP_DEFAULT,
        [("August period acknowledged (Fix 2 month kept)", has("серпн")),
         ("food-prices FAQ answered", has("350", "сніданок", "обід")),
         ("military/УБД discount handled", has("20%", "убд", "військов")),
         ("no removed «Стандартів більше» aside (#266)", lacks("стандартів у нас більше")),
         ("EXACT food cost computed: (1100*4*2)+(350*4*1)=10200", has("10200")),
         ("menu (dishes) question answered", has("узгоджуються по заїзду")),
         # Bug 3 (Sprint 5): the food/menu turn must NOT leak a ROOM_TOO_SMALL capacity error.
         ("no ROOM_TOO_SMALL flip-flop leak (Bug 3)", lacks("обраний розподіл не вміщується"))]),

    # 3: ("3️⃣ Двоє дітей, нечіткі дати (кінець серпня)",  # PASSED
    #     ["Доброго дня.", "Двоє дорослих і двоє дітей.", "Яка вартість, і вільні дати?",
    #      "Орієнтовно кінець серпня", "3-5 діб, не остаточно вирішено",
    #      "Супер. Дякую. Визначимося з датами, відпишу",
    #      "Зорієнтуйте ще будь ласка по харчуванню.", "Дякую."],
    #     DRIP_DEFAULT,
    #     [("August acknowledged", has("серпн")),
    #      ("asks child ages", has("вік діт", "діток", "вік дит")),
    #      ("food FAQ answered", has("350", "сніданок", "обід")),
    #      ("warm close", has("гарного дня", "будемо раді", "раді допомогти"))]),

    # 4: ("4️⃣ Трьох + собака",  # PASSED
    #     ["Добрий день", "Яка ціна за трьох?", "Дорослі всі )", "Орієнтовно на вихідні",
    #      "Також ми хочем приїхати з собачкою", "Пекінес", "Собака має все",
    #      "Треба за собаку доплачувати?"],
    #     DRIP_DEFAULT,
    #     [("pet surcharge 300", has("300 грн")), ("coherent", has("грн", "віконц", "дати"))]),

    # 5: PASSED (owner Sprint 4) — commented out, kept for re-runs. (past-dates: 22.06–02.07)
    # 5: ("5️⃣ Одна особа, тиждень (22.06–02.07) — nights alignment (#274)",
    #     ["Які вільні дати для бронювання?",
    #      "Доброго дня. На 1 особу з 22.06 по 02.07. на 7 днів",
    #      "Дякую",
    #      "так"],
    #     DRIP_DEFAULT,
    #     [("coherent date/price response (solo)", has("грн", "віконц", "вільн", "заброньован", "дати")),
    #      ("if a nearest window is offered, it STATES nights (#274)",
    #       lambda t: "найближче вільне віконце" not in t or "ноч" in t),
    #      ("warm close on thanks", has("гарного дня", "будемо раді", "раді допомогти"))]),

    # 6: ("6️⃣ 2 дорослих + 3 дітей",  # PASSED
    #     ["Де саме знаходиться готель?", "Яка ціна кімнати 2 дорослих і 3 дітей",
    #      "Саме головне цікавить ціна проживання за добу?"],
    #     DRIP_DEFAULT,
    #     [("location answered", has("серці карпат", "стаїще", "верховин")),
    #      ("asks child ages", has("вік діт", "діток"))]),

    # 7: PASSED (owner Sprint 4) — commented out, kept for re-runs. (exact dates 13.07-17.07)
    # 7: ("7️⃣ Бронь 13.07-17.07 (4 ночі) — exact dates priced/handled (#274/#299)",
    #     ["Хочу забронювати",
    #      "13.07-17.07",
    #      "яка вартість відпочинку?",
    #      "Що входить у вартість",
    #      "На двох",
    #      "Стандарт +"],
    #     DRIP_DEFAULT,
    #     [("INCLUDED-in-price FAQ answered", has("паркув", "входить", "басейн")),
    #      ("coherent 4-night stay: exact price OR nearest-window/booked, never a false error",
    #       has("грн", "віконц", "заброньован", "вартувати")),
    #      ("if a nearest window is offered it states 4 nights (#274)",
    #       lambda t: "найближче вільне віконце" not in t or "4 ноч" in t),
    #      ("never claims the (future, valid) dates already passed", lacks("вже минули"))]),

    # 8: PASSED 2026-07-09 (owner) — commented out, kept for re-runs.
    # 8: ("8️⃣ Як добратися (2-7 липня, 1 дор + діти 6 і 13) — only-available (#275)",
    #     ["Де саме знаходиться готель?", "Орієнтовно 2-7 липня",
    #      "1 дорослий, 2 дітей 6 і 13 років", "Як до Вас добратися ?", "Бердичів, Житомирська область"],
    #     DRIP_DEFAULT,
    #     [("location answered", has("серці карпат", "стаїще", "верховин", "maps")),
    #      ("directions answered", has("ворохт", "залізниц", "трансфер", "автобус")),
    #      ("coherent booking response", has("віконц", "грн", "заброньован", "дати"))]),

    # 9: ("9️⃣ Compound period липень/серпень",  # PASSED
    #     ["Добрий вечір! Цікавить двомісний номер? На двох)",
    #      "Ще точно не знаю, поки лише вирішуємо. Або друга половина липня, або після 6 серпня",
    #      "Але відпочинок вже просто необхідний, як повітря!", "Вартість доби)",
    #      "В серпні актуальна, як і на липень, цінова політика?"],
    #     DRIP_DEFAULT,
    #     [("August not dropped", has("серпн")),
    #      ("coherent", has("віконц", "вільн", "точні дати", "дати", "грн"))]),

    # 10: ("🔟 BATCH booking + фото/відео + знижки",  # PASSED
    #      ["Доброго дня! Цікавить номер на 2 дорослих і 2 дітей (7 і 10 років) з 1 по 5 серпня",
    #       "Яка вартість проживання?", "Чи є фото/відео номерів?", "Є якісь знижки у вас?"],
    #      BURST_DRIP,
    #      [("greeting", has("d&t hotel")), ("discounts children", has("знижки для дітей")),
    #       ("discounts military", has("військовослужбовц")), ("media", has("сторіс", "хайлайтс", "фото")),
    #       ("booking not ignored", has("грн", "віконц", "заброньован", "вартіст"))]),

    # 11: PASSED 2026-07-09 (owner) — commented out, kept for re-runs.
    # 11: ("1️⃣1️⃣ Троє діти + дитячий басейн (fix #278)",
    #      ["Доброго дня! Цікавить номер на 2 дорослих і 3 дітей (5 і 10 і півроку) з 1 по 5 серпня",
    #       "Найменша буде спати з нами)", "Чи є у вас дитячі ліжечка/манежі?",
    #       "Чи є у вас дитячий басейн?", "Яка темперптура бесейну?"],
    #      DRIP_DEFAULT,
    #      [("children's pool answered with size (#278)", has("3х2", "дитячий басейн")),
    #       ("children's pool temperature", has("28")),
    #       ("cribs/playpens answered (none)", has("ліжечок", "манеж")),
    #       ("August booking coherent", has("серпн", "грн", "віконц", "заброньован"))]),

    # 12: ("1️⃣2️⃣ Фен", ["Добрий день", "У вас у кожному номері є фен?", "Дякую"],  # PASSED
    #      DRIP_DEFAULT, [("hairdryer answered", has("фен")), ("warm close", has("гарного дня"))]),

    # 13: ("1️⃣3️⃣ Басейн",  # PASSED
    #      ["Добрий день, у вас басейн працює?", "А можна просто прийти завтра на день і поплавати в басейні?"],
    #      DRIP_DEFAULT, [("pool answered", has("басейн")), ("day-visit price", has("400 грн", "600 грн"))]),

    # 14: ("🧪 No-dates → proactive scan",  # PASSED
    #      ["Доброго дня! Цікавить ціна, але дати ще не можу сказати", "Для двох дорослих"],
    #      DRIP_DEFAULT, [("proactive scan", has("вільні віконця", "перевірив календар"))]),

    # 15: PASSED (owner Sprint 5) — commented out, kept for re-runs. (6+ split accept -> quote)
    # 15: ("🧪 6+ split: 7 дорослих → 4 і 3 → valid re-split → ACCEPT → quote (fix #282-284)",
    #      ["Доброго дня! На 23-24 липня, нас 7 дорослих",
    #       "Давайте 2 номери: в одному 4, в іншому 3",
    #       "Добре, порахуйте, будь ласка, вартість"],
    #      DRIP_DEFAULT,
    #      [("explains max 3 adults per room (#282-284)", has("максимум 3 дорослих")),
    #       ("suggests enough rooms (3) with a valid distribution", has("3 номери")),
    #       ("shows the 2 + 2 + 3 distribution", has("2 + 2 + 3")),
    #       ("after ACCEPT -> quotes the split rooms (owner #15)", has("буде вартувати", "загальна вартість"))]),

    # 16: ("🧪 УБД on a sold-out window → note kept",  # PASSED
    #      ["Доброго дня! Стандарт на 6-8 липня, двоє дорослих, я УБД"],
    #      DRIP_DEFAULT, [("nearest window", has("віконц", "заброньован")),
    #                     ("military note kept", has("убд", "посвідчення"))]),

    # ===== NEW personas (owner 2026-07-08) =====
    # 17: PASSED 2026-07-10 (owner) — commented out, kept for re-runs.
    # 17: ("1️⃣7️⃣ Басейн: свій харч + вхід без купання",
    #      ["А можна просто прийти завтра на день і поплавати в басейні?",
    #       "Чи можна до вас приходити зі своїм.",
    #       "Яка буде вартість якщо не будем плавати а просто посидіти"],
    #      DRIP_DEFAULT,
    #      [("day-visit pool price (GUEST_POOL)", has("400 грн", "600 грн", "відвідувач", "на день")),
    #       ("no own food — kolyba/bar instead", has("колиба", "бар")),
    #       ("own food refused", has("не можна")),
    #       ("same price if just sitting — entrance fee", has("такою ж", "вхід", "зону відпочинку"))]),

    # 18: PASSED 2026-07-09 (owner) — commented out, kept for re-runs.
    # 18: ("1️⃣8️⃣ Booking.com передоплата → людині",
    #      ["Доброго дня! Я забронювала вже номер на booking, мені підтвердили. Потрібно кинути передоплату. Скажіть будь ласка 50% передоплати"],
    #      DRIP_DEFAULT,
    #      [("cannot answer Booking.com questions", has("booking.com")),
    #       ("does NOT send our IBAN/prepayment rules", lacks("iban", "26005064100017")),
    #       ("hands off to a human", has("менеджер"))]),

    # 19: PASSED (owner Sprint 4) — commented out, kept for re-runs. (NLP family 5 pax)
    # 19: ("1️⃣9️⃣ NLP родини (3 дор + 11,16) + 23-27 → ТІЛЬКИ доступні типи (#19)",
    #      ["Добрий день. Яка ціна?",
    #       "Мої батьки, я та мої 2 сина",
    #       "11 і 16 років",
    #       "23-27 липня"],
    #      DRIP_DEFAULT,
    #      [("parsed 3 adults + 2 children; asked the sons' ages", has("вік діт", "вік дит", "діток")),
    #       # party of 5 fits only Напівлюкс, which is BOOKED on 23-27 -> offer a Стандарт-class split,
    #       # naming ONLY the AVAILABLE types (owner #19).
    #       ("offers Стандарт-class rooms", has("стандарт")),
    #       ("does NOT offer the BOOKED Напівлюкс on these dates (#19)", lacks("напівлюкс"))]),

    # 20: PASSED 2026-07-09 (owner) — commented out, kept for re-runs.
    # 20: ("2️⃣0️⃣ Відстань до Буковелю",
    #      ["Дізнатися вартість", "Добрий день. 2 дорослих 1 дитина (8 років) 3 ночі 20-23 липня",
    #       "На скільки далеко Ви від Буковелю?"],
    #      DRIP_DEFAULT,
    #      [("distance to Bukovel answered (~35 km)", has("35 км", "буковел")),
    #       ("booking coherent (20-23 July, 3 nights)", has("грн", "віконц", "заброньован", "дата", "3 ноч"))]),

    # ===== NEW personas (owner 2026-07-09) =====
    21: ("2️⃣1️⃣ Велика змішана компанія + гнучкий старт (6 дор + 4 діти) — date-after-split (Sprint-4)",
         ["Цікавить дві доби для компанії 6 дорослих 4 дитини 2,8,11,14р",
          "З 15 чи 16 липня"],
         DRIP_DEFAULT,
         [("shows capacities (max 3 adults/room)", has("максимум 3 дорослих")),
          ("PROPOSES a valid multi-room split (owner #21)", has("розподілити")),
          # Sprint-4 Test 21 FIX: the follow-up "З 15 чи 16 липня" must be CAPTURED, not ignored —
          # the split is re-proposed with the July dates attached (never silent).
          ("captures the dates sent right after the split (Test 21 fix)", has("липня")),
          ("does NOT cram everyone into one room / no bogus quote",
           lambda t: "буде вартувати" not in t or "загальна вартість" in t)]),

    # 22: PASSED 2026-07-10 (owner) — commented out, kept for re-runs.
    # 22: ("2️⃣2️⃣ Оплата → хендоф + мут",
    #      ["Я оплатив, ось квитанція", "а коли буде підтвердження броні?"],
    #      BURST_DRIP,
    #      [("hands off — manager will verify payment", has("менеджер", "перевірить оплату")),
    #       ("does NOT auto-confirm a booking / no IBAN", lacks("буде вартувати", "iban")),
    #       ("stays a SINGLE handoff message (muted after)", lambda t: t.count("перевірить оплату") <= 1)]),

    # NOTE: the original IG dialogue had a 14yo in family 2. Under the owner's STRICT capacity
    # correction (2 adults + 2 children UNDER 12 only), that family no longer fits a Standard —
    # which CONTRADICTS this round's bug-1 note. Pending the owner's decision, this persona tests
    # the code fix actually requested (bug 2: AGGREGATE BOTH families) with a fitting composition.
    23: ("2️⃣3️⃣ Дві сім'ї → рахуємо ОБИДВІ (multi-room aggregate, owner bug 2)",
         ["Доброго дня! Можна дізнатись ціни?",
          "Дві сім'ї на 20-24 липня. 1 сім'я - 2 дорослих і 2 дітей 8 і 10 років. 2 сім'я - 2 дорослих і дитина 9 років",
          "Так, порахуйте, будь ласка, обидві сім'ї"],
         DRIP_DEFAULT,
         [("quotes a Стандарт-class room", has("стандарт")),
          # bug 2: MULTIPLE rooms aggregated — either a "Загальна вартість" grand total (distinct
          # rooms) or the collapsed "за N номери типу X" combined price (identical rooms).
          ("AGGREGATES multiple rooms (owner bug 2)", has("загальна вартість", "2 номери", "за 2 номери")),
          ("real prices, not stuck", has("грн")),
          ("no capacity error leak", lacks("error", "traceback"))]),

    # 24: PASSED (owner Sprint 4) — commented out, kept for re-runs. (off-season 28.09–01.10)
    # 24: ("2️⃣4️⃣ Міжсезоння (28.09–01.10) + УБД + фото (IG real)",
    #      ["Доброго дня! Заїзд 28 вересня, виїзд 01 жовтня. Двоє дорослих і двоє дітей 4 і 6 років",
    #       "Стандарт",
    #       "Чи є знижки для військових/УБД?",
    #       "Можливо маєте фото/відео номерів?"],
    #      DRIP_DEFAULT,
    #      [("off-season handled — price being agreed, not a made-up quote",
    #        lambda t: "узгоджується" in t or "буде вартувати" not in t),
    #       ("military/УБД discount answered", has("20%", "убд", "військов")),
    #       ("photo/video (MEDIA) answered", has("сторіс", "хайлайтс", "фото"))]),

    # ===== NEW personas (owner Sprint 4, 2026-07-11) =====
    25: ("2️⃣5️⃣ Beds check: 2 номери 4 дор 24-26.07 + роздільні vs двоспальне ліжко",
         ["Дізнатися вартість",
          "Потрібно 2 номери для 4-х дорослих на 24-26.07.26",
          "Нам потрібен один номер з роздільними ліжками а один номер на двох з одним ліжком, такі 2 є?"],
         DRIP_DEFAULT,
         [("quotes a Стандарт-class room for the 2-room, 4-adult booking", has("стандарт")),
          ("gives a real price / aggregate (2 rooms)",
           has("грн", "загальна вартість", "2 номери", "за 2 номери")),
          # Bug 1 (Sprint 5): the bed answer is based on REAL availability ON THE DATES, not the
          # generic "підберемо при бронюванні" template.
          ("answers beds from REAL availability on the dates", has("на дати")),
          ("names both bed configs (separate + double)",
           lambda t: "роздільн" in t and "двоспальн" in t),
          ("NOT the generic lazy bed template (Bug 1)",
           lacks("підтвердимо доступність на ваші дати при бронюванні"))]),

    26: ("2️⃣6️⃣ Scattered NLP stress client (3 дор, pool/address/food, 15-24.07)",
         ["Добрий день яка вартість номеру на трьох дорослих",
          "Басейн фходить у вартість номеру",
          "Можна точну адресу",
          "Що з харчуванням",
          "Яка вартість",
          "Днів 4-5",
          "Вибачте що не відповіла на попередні повідомлення",
          "Підкажіть спілкувалися щодо 3 місного номеру для дорослих",
          "Ще є вільні місця у липні",
          "Бажано з 15.07",
          "На 5 ночей з 19",
          "Уточніть будь ласка",
          "Вартість",
          "Як минулого разу спілкувалися 15000 було з басейном",
          "А зараз як?",
          "Трішки не зрозуміла якщо брати стандарт за 13500 що в ньог входить",
          "Тобто не входить холодильник і балкон",
          "Кондиціонер входить?",
          "Кімната одно чи дах кімната",
          "І чи входить басейн",
          "?",
          "А в стандарт + входить холодильник, кондиціонер, міні холодильник та басейн",
          "І це також однокімнатний номер з диваном чи двокімнатний?",
          "Дякую"],
         DRIP_DEFAULT,
         [("pool 'included in price' answered", has("басейн", "входить у вартість")),
          ("address answered", has("стаїще", "верховин", "карпат", "maps")),
          ("food answered", has("сніданок", "350", "харчуванн")),
          # Bug 2 (Sprint 5): NEVER claim "booked" before exact dates — push the client to clarify.
          ("pushes for exact dates before quoting (Bug 2)",
           has("які дати вас цікавлять", "точні дати", "на який термін")),
          # owner 2026-07-11: A/C answered as NO air conditioners (never hallucinated).
          ("A/C answered — no air conditioners", lambda t: "кондиціонер" in t and "немає" in t),
          ("gives a coherent price once dates are committed", has("грн")),
          ("warm close on 'Дякую'", has("гарного дня", "будемо раді", "раді допомогти", "дякуємо"))]),

    # 27: PASSED (owner Sprint 5) — commented out, kept for re-runs. (cottage -> rooms)
    # 27: ("2️⃣7️⃣ Cottage → no cottage, pivot to room split (5 дор + 2 діти, 1-11 серпня)",
    #      ["Добрий день цікавить котедж на 7 чоловік , 5 дорослих і 2 дітей",
    #       "які є варіанти з 1 го по 11 серпня, дякую"],
    #      DRIP_DEFAULT,
    #      [("acknowledges there is NO cottage (we are a hotel)", has("котедж", "готель")),
    #       ("offers the real room types instead", has("стандарт", "напівлюкс")),
    #       ("handles the 7-person group (asks distribution / proposes split)",
    #        has("розсел", "розподіл", "як вас краще", "оптимальні номери")),
    #       ("no bogus single-room quote for 7 people",
    #        lambda t: "буде вартувати" not in t or "загальна вартість" in t)]),
}

DEFAULT_SET = [2, 21, 23, 25, 26]


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def audit(rows, checks):
    t = bot_text(rows)
    results = [(desc, bool(pred(t))) for desc, pred in checks]
    flags = red_flags(rows)
    return results, flags


def run(selection):
    if not TOKEN:
        print("[FATAL] CHATWOOT_TOKEN not found (scraper/.env). Aborting.")
        return 2
    inbox_id, identifier = find_or_create_sim_inbox()
    if not identifier:
        print("[FATAL] could not resolve the Api inbox identifier.")
        return 2

    print("=" * 74)
    print(f"LIVE AUTO-QA — real Chatwoot + real Gemini + real OtelMS scraper (ZERO mocks)")
    print(f"  Chatwoot: {BASE_URL} (account {ACCOUNT_ID})  Bot: {BOT_WEBHOOK_URL}")
    print(f"  Personas: {selection}   drip={DRIP_DEFAULT}s  burst={BURST_DRIP}s  settle={SETTLE_SECONDS}s")
    print("=" * 74)

    injected = []
    for n, idx in enumerate(selection, start=1):
        name, messages, drip, _checks = PERSONAS[idx]
        print(f"\n[{idx}] {name}   (drip {drip}s)")
        try:
            conv_id = inject_persona(identifier, name, messages, drip)
            print(f"      ✓ conversation #{conv_id}")
            injected.append((idx, name, conv_id))
        except urllib.error.HTTPError as e:
            print(f"      ✗ HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
        except Exception as e:
            print(f"      ✗ error: {e}")
        if n < len(selection):
            time.sleep(PERSONA_GAP)

    if not injected:
        return 2
    print(f"\n… settling {SETTLE_SECONDS}s for the bot to finish its real replies …")
    time.sleep(SETTLE_SECONDS)

    print("\n… scraping ONE availability snapshot for live window-validation …")
    snapshot = scrape_snapshot()
    print(f"   snapshot: {'ok (' + str(len(snapshot)) + ' dates)' if snapshot else 'UNAVAILABLE (window-validation skipped)'}")

    print("\n" + "=" * 74 + "\nLIVE TRANSCRIPTS + AUDIT\n" + "=" * 74)
    total = passed = 0
    all_flags = []
    failures = []
    for idx, name, conv_id in injected:
        rows = fetch_rows(conv_id)
        print_transcript(name, conv_id, rows)
        _n, _m, _d, checks = PERSONAS[idx]
        results, flags = audit(rows, checks)
        # Owner mandate: every offered date window must have every night free (no bridging).
        flags = flags + window_violations(rows, snapshot)
        bot_msgs = sum(1 for r, _ in rows if r == "bot")
        print(f"   AUDIT (persona {idx}, {bot_msgs} bot messages):")
        for desc, ok in results:
            total += 1
            passed += 1 if ok else 0
            print(f"     {'✅' if ok else '❌'} {desc}")
            if not ok:
                failures.append((idx, desc))
        if flags:
            for f in flags:
                print(f"     🚩 RED FLAG: {f}")
                all_flags.append((idx, f))
        else:
            print("     ✅ no red flags (no hallucinated names / tennis / bridged windows)")

    print("\n" + "=" * 74 + "\nLIVE AUTO-QA SUMMARY\n" + "-" * 74)
    print(f"  audits: {passed}/{total} passed | red flags: {len(all_flags)}")
    green = (passed == total) and not all_flags
    if not green:
        print("  FAILURES:")
        for idx, desc in failures:
            print(f"   - persona {idx}: {desc}")
        for idx, f in all_flags:
            print(f"   - persona {idx}: RED FLAG {f}")
    print("\n  RESULT:", "🟢 ALL GREEN" if green else "🔴 NOT GREEN")
    print("  Conversations viewable at", f"{BASE_URL}/app/accounts/{ACCOUNT_ID}/")
    return 0 if green else 1


def main():
    if "--read" in sys.argv:
        ids = [int(a) for a in sys.argv[sys.argv.index("--read") + 1:] if a.isdigit()]
        for cid in ids:
            try:
                print_transcript(f"conversation {cid}", cid)
            except Exception as e:
                print(f"  ✗ transcript #{cid} failed: {e}")
        return 0
    wanted = [int(a) for a in sys.argv[1:] if a.isdigit() and int(a) in PERSONAS]
    return run(wanted or DEFAULT_SET)


if __name__ == "__main__":
    sys.exit(main())
