"""The Clock.

Every time reference in this project goes through this module. Nothing else
calls datetime.now() -- tests/test_clock_lint.py fails the build if it does --
because the demo needs to show three days passing in ten seconds.

All times in this project are naive datetimes in IST. One timezone, no
conversions, no tz-aware/naive comparison bugs. The database stores exactly
what clock.now() returns.
"""

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")


class Clock:
    def __init__(self) -> None:
        self._offset = timedelta(0)
        self._frozen_at: datetime | None = None

    def now(self) -> datetime:
        """Current simulated time, naive IST."""
        base = self._frozen_at if self._frozen_at is not None else self._wall()
        return base + self._offset

    def advance(self, **kwargs) -> datetime:
        """clock.advance(days=3), clock.advance(minutes=30)."""
        self._offset += timedelta(**kwargs)
        return self.now()

    def jump_to(self, dt: datetime) -> datetime:
        """Move the clock so that now() == dt."""
        base = self._frozen_at if self._frozen_at is not None else self._wall()
        self._offset = dt - base
        return self.now()

    def freeze(self, at: datetime | None = None) -> datetime:
        """Stop the clock so a run is reproducible. Advancing still works."""
        self._frozen_at = at if at is not None else self.now()
        self._offset = timedelta(0)
        return self.now()

    def unfreeze(self) -> datetime:
        """Resume tracking wall time, keeping the current simulated instant."""
        if self._frozen_at is not None:
            current = self.now()
            self._frozen_at = None
            self.jump_to(current)
        return self.now()

    def reset(self) -> datetime:
        self._offset = timedelta(0)
        self._frozen_at = None
        return self.now()

    @property
    def is_frozen(self) -> bool:
        return self._frozen_at is not None

    @property
    def offset(self) -> timedelta:
        return self._offset

    def _wall(self) -> datetime:
        # The only real clock reading in the codebase.
        return datetime.now(IST).replace(tzinfo=None)


# One instance, imported everywhere.
clock = Clock()


def iso(dt: datetime | None) -> str | None:
    """Serialise a stored time for the API."""
    return dt.isoformat() if dt is not None else None


def wall_clock_now() -> datetime:
    """Real wall-clock time, always -- never advanced, frozen, or reset.

    For infrastructure timers only, currently just the Razorpay circuit
    breaker. Its open/half-open window is a real-seconds backoff against a
    real API and must survive `clock` being advanced, jumped, or reset while
    it is open. Using clock.now() there was a real bug: an operator advancing
    the demo clock while the breaker was open, then resetting it, left
    _opened_at holding a stale offset with nothing to correct it, and the
    breaker computed a "reopens in" of the entire vanished offset -- once
    nineteen real hours, for a breaker meant to reopen in sixty seconds.
    Everything else in the app (scheduling, quiet hours, decision timestamps,
    payday calculations) correctly keeps using clock.now().
    """
    return Clock()._wall()
