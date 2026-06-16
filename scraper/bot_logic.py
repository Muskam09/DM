"""
bot_logic.py — Pure, dependency-free helpers extracted from bot_server.py.

These functions hold the deterministic, side-effect-free decision logic of the
bot (intent gating, availability filtering, message splitting, reply extraction,
greeting injection). Keeping them here lets the automated test suite assert the
bot's behaviour without importing FastAPI / google-genai / Playwright, and
guarantees the live server and the tests exercise the very same code.
"""

from __future__ import annotations

import re
from typing import Dict, List

# Categories that must NEVER be offered to a client as a bookable room.
IGNORE_CATEGORIES = ["Колиба", "Басейн", "Overbooking"]

GREETING = (
    "Доброго дня! Вас вітає D&T Hotel ⛰\n"
    "Раді, що зацікавились нашим готелем!\n"
    "[SPLIT]\n"
)
# Stable marker used to detect that the greeting is already present in a reply.
GREETING_MARKER = "Доброго дня! Вас вітає D&T Hotel"


def intent_says_yes(intent_text: str) -> bool:
    """The intent pre-filter triggers the scraper only on an explicit 'ТАК'."""
    return "ТАК" in (intent_text or "").strip().upper()


def build_simplified_availability(
    availability_data: Dict, ignore_categories: List[str] = IGNORE_CATEGORIES
) -> Dict:
    """Reduce raw scraper output to ``{room_type: {date: free_count}}``, dropping
    any blacklisted category (Колиба / Басейн / Overbooking) so the bot can never
    propose them as a room. ``free_count`` is the per-date "total_available" map
    produced by the scraper.
    """
    simplified: Dict = {}
    for room_type, r_data in (availability_data or {}).items():
        if any(ignore.lower() in room_type.lower() for ignore in ignore_categories):
            continue
        if isinstance(r_data, dict) and "total_available" in r_data:
            simplified[room_type] = r_data["total_available"]
    return simplified


def free_room_types(simplified: Dict, night_dates: List[str]) -> List[str]:
    """Room types that have a free room (count > 0) on EVERY requested night.

    `simplified` is the output of build_simplified_availability; `night_dates` are
    the nights to stay (the checkout day is excluded by the caller). A room with a
    `0` on any night is treated as booked (Cases 4/5/8).
    """
    if not night_dates:
        return []
    return [
        room_type
        for room_type, avail in (simplified or {}).items()
        if all(avail.get(d, 0) > 0 for d in night_dates)
    ]


def is_sold_out(simplified: Dict, night_dates: List[str]) -> bool:
    """True when NO room type is free on all requested nights (full sold-out, Case 5)."""
    return len(free_room_types(simplified, night_dates)) == 0


def extract_reply(response_text: str) -> str:
    """Return only the text inside <REPLY>...</REPLY>. If the tag is missing,
    strip out any <THINK> machine-reasoning and return what remains.
    """
    response_text = response_text or ""
    reply_match = re.search(
        r"<REPLY>(.*?)</REPLY>", response_text, re.DOTALL | re.IGNORECASE
    )
    if reply_match:
        return reply_match.group(1).strip()
    return re.sub(
        r"<THINK>.*?(</THINK>|THINK>|>|$)", "", response_text,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()


def prepend_greeting_if_needed(clean_message: str, bot_has_spoken: bool) -> str:
    """On the bot's very first message, guarantee the greeting + [SPLIT] prefix."""
    if not bot_has_spoken and GREETING_MARKER not in clean_message:
        return GREETING + clean_message
    return clean_message


def split_messages(clean_message: str) -> List[str]:
    """Split on the [SPLIT] marker into the non-empty chunks to send in sequence."""
    return [part.strip() for part in (clean_message or "").split("[SPLIT]") if part.strip()]
