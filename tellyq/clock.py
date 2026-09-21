"""System time at the application boundary, independent of any playback adapter."""

from datetime import UTC, datetime
from time import monotonic


class SystemClock:
    """UTC for records and monotonic time for intervals within one process lifetime."""

    def utcnow(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return monotonic()
