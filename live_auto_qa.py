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
    data = app_api("GET", f"/api/v1/accounts/{ACCOUNT_ID}/conversations/{conv_id}/messages")
    msgs = data if isinstance(data, list) else (
        data.get("payload") or data.get("messages") or data.get("data") or [])
    rows = []
    for m in sorted(msgs, key=lambda x: x.get("id", 0)):
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


def has(*subs):
    return lambda t: any(s in t for s in subs)


# (index, name, [messages], drip_seconds, [ (desc, predicate_on_lowercased_bot_text) ])
PERSONAS = {
    2: ("2️⃣ УБД + початок серпня + оплата по приїзду",
        ["Четверо дорослих",
         "початок серпня",
         "харчування є?",
         "Cтандарт +, якщо немає тоді стандарт",
         "А є знижка по УБД на один номер?",
         "Хочу забронювати",
         "Чи можна по приїзду оплатити повністю"],
        DRIP_DEFAULT,
        [("August period acknowledged (Fix 2 month kept)", has("серпн")),
         ("food FAQ answered", has("350", "сніданок", "обід")),
         ("military/УБД discount handled", has("20%", "убд", "військов")),
         ("pay-on-arrival → prepayment rules, not check-in time", has("аванс", "передоплат"))]),

    3: ("3️⃣ Двоє дітей, нечіткі дати (кінець серпня)",
        ["Доброго дня.",
         "Двоє дорослих і двоє дітей.",
         "Яка вартість, і вільні дати?",
         "Орієнтовно кінець серпня",
         "3-5 діб, не остаточно вирішено",
         "Супер. Дякую. Визначимося з датами, відпишу",
         "Зорієнтуйте ще будь ласка по харчуванню.",
         "Дякую."],
        DRIP_DEFAULT,
        [("August period acknowledged (Fix 2)", has("серпн")),
         ("asks child ages (were unknown)", has("вік діт", "діток", "вік дит")),
         ("food FAQ answered", has("350", "сніданок", "обід")),
         ("warm close on final thanks", has("гарного дня", "будемо раді", "раді допомогти"))]),

    5: ("5️⃣ Одна особа, тиждень (22.06–02.07)",
        ["Які вільні дати для бронювання?",
         "Доброго дня. На 1 особу з 22.06 по 02.07. на 7 днів",
         "Дякую"],
        DRIP_DEFAULT,
        [("coherent date/price response (solo)", has("грн", "віконц", "вільн", "заброньован", "дати")),
         ("warm close on thanks", has("гарного дня", "будемо раді", "раді допомогти"))]),

    9: ("9️⃣ Compound period: друга половина липня АБО після 6 серпня (Fix 2)",
        ["Добрий вечір! Цікавить двомісний номер? На двох)",
         "Ще точно не знаю, поки лише вирішуємо. Або друга половина липня, або після 6 серпня",
         "Але відпочинок вже просто необхідний, як повітря!",
         "Вартість доби)",
         "В серпні актуальна, як і на липень, цінова політика?"],
        DRIP_DEFAULT,
        [("compound period → August NOT silently dropped", has("серпн")),
         ("coherent scan/date response (windows or exact-date ask)",
          has("віконц", "вільн", "точні дати", "дати", "грн"))]),

    10: ("🔟 BATCH: booking(1-5 серпня) + фото/відео + знижки (Fix 1 + Fix 3)",
         ["Доброго дня! Цікавить номер на 2 дорослих і 2 дітей (7 і 10 років) з 1 по 5 серпня",
          "Яка вартість проживання?",
          "Чи є фото/відео номерів?",
          "Є якісь знижки у вас?"],
         BURST_DRIP,      # rapid-fire so the burst collapses -> Fix 3 batch ordering engages
         [("greeting present", has("d&t hotel")),
          ("DISCOUNTS FAQ: children discount listed (Fix 1)", has("знижки для дітей")),
          ("DISCOUNTS FAQ: military discount listed (Fix 1)", has("військовослужбовц")),
          ("MEDIA FAQ answered", has("сторіс", "хайлайтс", "фото")),
          ("booking priced or sold-out/nearest (not ignored)",
           has("грн", "віконц", "заброньован", "вартіст"))]),
}

DEFAULT_SET = [2, 3, 5, 9, 10]


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

    print("\n" + "=" * 74 + "\nLIVE TRANSCRIPTS + AUDIT\n" + "=" * 74)
    total = passed = 0
    all_flags = []
    failures = []
    for idx, name, conv_id in injected:
        rows = fetch_rows(conv_id)
        print_transcript(name, conv_id, rows)
        _n, _m, _d, checks = PERSONAS[idx]
        results, flags = audit(rows, checks)
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
            print("     ✅ no red flags (no hallucinated names / tennis court)")

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
