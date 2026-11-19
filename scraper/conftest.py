"""
conftest.py — shared pytest fixtures for the scraper test suite.

Freezes `date.today()` to a deterministic point inside the 2026 booking season
(the year every existing test was written against) so the rolling-year logic
introduced in 2026-11 (resolve_booking_year) does NOT make tests depend on the
wall-clock year.

The production code still reads `date.today()` at runtime; only the tests pin it.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest


# A Tuesday in late-June 2026 — chosen so BOTH:
#   * every priced summer month (Jun–Nov) is "still ahead or ongoing" this year,
#     so resolve_booking_year(6..11) returns 2026 (matches the hardcoded dates
#     in the existing test suite);
#   * January/February/March still count as "past" => resolve_booking_year(1..5)
#     rolls to 2027, which is what a mid-season booking call expects.
FROZEN_TODAY = date(2026, 6, 16)


class _FrozenDate(date):
    """`date` subclass whose `today()` classmethod is pinned to `FROZEN_TODAY`."""

    @classmethod
    def today(cls):  # type: ignore[override]
        return FROZEN_TODAY


@pytest.fixture(autouse=True)
def freeze_today(monkeypatch):
    """Pin `date.today()` in every module that reads it for year selection."""
    import bot_logic
    import dialogue_engine  # noqa: F401 — ensures the module is loaded before patching

    monkeypatch.setattr(bot_logic, "date", _FrozenDate)
    # dialogue_engine imports `date` from datetime too.
    monkeypatch.setattr(dialogue_engine, "date", _FrozenDate)
    yield
