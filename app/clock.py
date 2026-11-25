# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.

from datetime import datetime
from zoneinfo import ZoneInfo


class Clock:
    """Wall clock. Date-of-service rules use the practice's timezone, not UTC."""

    def now(self, timezone_name: str) -> datetime:
        return datetime.now(ZoneInfo(timezone_name))


class FrozenClock(Clock):
    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware instant")
        self.instant = instant

    def now(self, timezone_name: str) -> datetime:
        return self.instant.astimezone(ZoneInfo(timezone_name))
