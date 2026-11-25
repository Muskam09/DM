"""
test_hotel_scraper.py — Unit tests for the pure helpers in hotel_scraper.py.

These cover the date-normalisation surface added on 2026-11-19 to fix the
production incident where the bot wrongly said "all rooms booked" for January
and February 2027: the scraper now accepts a `target_date` so Playwright can
navigate OtelMS "Шахівниця" to the requested month before parsing. Only the
PURE helper is unit-tested here — the Playwright navigation itself is covered
by the e2e tests in test_webhook.py via a fake fetcher that records the
`target_date` kwarg it was called with.
"""
from __future__ import annotations

from datetime import date, datetime

import hotel_scraper as hs


def test_target_iso_full_iso_passes_through():
    # A fully-qualified ISO date is forwarded unchanged — this is what the bot
    # server passes: the earliest `checkin` ISO from the slots payload.
    assert hs._target_iso("2027-01-11") == "2027-01-11"
    assert hs._target_iso("2027-02-04") == "2027-02-04"


def test_target_iso_accepts_year_month_and_pads_to_first():
    # A YYYY-MM form (useful for fuzzy months) is padded to the 1st — OtelMS
    # accepts any day in the target month and recentres the 46-day window on it.
    assert hs._target_iso("2027-01") == "2027-01-01"
    assert hs._target_iso("2027-02") == "2027-02-01"


def test_target_iso_accepts_date_and_datetime_objects():
    # Both plain date and datetime should format via strftime, letting callers
    # pass either without an ISO conversion first.
    assert hs._target_iso(date(2027, 1, 11)) == "2027-01-11"
    assert hs._target_iso(datetime(2027, 2, 4, 15, 30)) == "2027-02-04"


def test_target_iso_returns_none_on_junk_or_empty():
    # None / "" / junk strings must NOT crash the scrape — the caller falls
    # back to the default OtelMS view (today ± ~23 days) and the stay-outside-
    # calendar branch of `bot_logic.is_room_available` turns any far-future
    # night into "unknown", not "sold_out".
    assert hs._target_iso(None) is None
    assert hs._target_iso("") is None
    assert hs._target_iso("   ") is None
    assert hs._target_iso("not-a-date") is None
    assert hs._target_iso("01-01-2027") is None   # wrong order, not ISO


def test_target_iso_strips_surrounding_whitespace():
    # Chatwoot occasionally forwards strings with trailing whitespace; the
    # normaliser should tolerate that instead of returning None (which would
    # silently skip navigation and reintroduce the production bug).
    assert hs._target_iso("  2027-01-11  ") == "2027-01-11"
