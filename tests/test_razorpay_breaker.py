"""The circuit breaker must survive the demo clock being manipulated.

This is a regression test for a real incident: the breaker used clock.now()
for its own timing. An operator advanced the live clock forward (via "jump to
next action") while the breaker was open, then reset it back with
/api/clock/reset. The offset vanished but _opened_at kept the stale value, and
the breaker computed a "reopens in" of the entire vanished offset -- about
nineteen real hours, for a breaker meant to reopen in sixty seconds. It stayed
open until someone noticed and force-reset it by hand.

The fix: the breaker times itself off real wall clock
(app.clock.wall_clock_now), never the demo clock. These tests pin that down by
manipulating app.clock.clock in every way the dashboard exposes and asserting
the breaker does not notice.
"""

from datetime import datetime, timedelta

import pytest

from app.clock import clock
from app.razorpay_client import FAILURE_THRESHOLD, OPEN_DURATION, RazorpayClient, RazorpayDown


@pytest.fixture(autouse=True)
def real_clock():
    """These tests only care about the demo clock, not frozen test time."""
    clock.reset()
    yield
    clock.reset()


def trip(breaker: RazorpayClient) -> None:
    """Open the breaker with FAILURE_THRESHOLD consecutive real failures."""
    breaker.chaos_down = True
    for _ in range(FAILURE_THRESHOLD):
        with pytest.raises(RazorpayDown):
            breaker.create_order(amount_paise=1000, receipt="r", notes={})
    breaker.chaos_down = False


class TestBreakerIgnoresTheDemoClock:
    def test_advancing_the_demo_clock_does_not_close_the_breaker_early(self):
        """The whole point of the 60-second window is that it takes 60 real
        seconds. If advancing the demo clock could close it, a merchant could
        "jump to next action" straight through a real outage."""
        breaker = RazorpayClient()
        trip(breaker)
        assert breaker.state("order") == "open"

        clock.advance(days=3)  # the demo clock now thinks it is 3 days later
        assert breaker.state("order") == "open", "a demo-clock jump must not reopen the breaker early"

    def test_the_incident_exactly_as_it_happened(self):
        """Advance the live clock forward while the breaker is open (as
        'jump to next action' does), then reset the clock (as the dashboard's
        Reset button does) without touching the breaker. Before the fix, this
        left reopens_in_seconds equal to the entire vanished offset."""
        breaker = RazorpayClient()
        trip(breaker)
        assert breaker.status()["breaker"] == "open"

        clock.advance(hours=20)  # "jump to next action" onto a far-future case
        clock.reset()  # the operator presses Reset on the clock bar

        status = breaker.status()
        assert status["breaker"] == "open", "still within the real 60s window"
        assert 0 <= status["reopens_in_seconds"] <= OPEN_DURATION.total_seconds(), (
            f"reopens_in_seconds must never exceed the real open duration, got "
            f"{status['reopens_in_seconds']}"
        )

    def test_freezing_the_demo_clock_does_not_freeze_the_breaker(self):
        breaker = RazorpayClient()
        trip(breaker)
        clock.freeze(datetime(2026, 1, 1, 0, 0))  # demo clock now stands still

        # Real time keeps moving regardless. The breaker must eventually
        # half-open on its own, which a frozen demo clock must not prevent.
        breaker._opened_at["order"] -= OPEN_DURATION + timedelta(seconds=1)
        assert breaker.state("order") == "half_open"

    def test_reopens_in_seconds_is_never_negative(self):
        """The clamp. Even a corrupted _opened_at must not surface as a
        negative-implied absurd number on the dashboard."""
        breaker = RazorpayClient()
        trip(breaker)
        breaker._opened_at["order"] += timedelta(days=10)  # simulate corruption
        assert breaker.status()["reopens_in_seconds"] >= 0

    def test_a_fresh_breaker_closes_on_real_elapsed_time_alone(self):
        """No demo-clock interaction at all: sixty real seconds, half-open."""
        breaker = RazorpayClient()
        trip(breaker)
        assert breaker.state("order") == "open"
        breaker._opened_at["order"] -= OPEN_DURATION + timedelta(seconds=1)
        assert breaker.state("order") == "half_open"


class TestBreakerIsScopedPerOperation:
    """The whole point of the change: Razorpay's Payment Links product has its
    own daily cap in test mode, separate from order creation. One shared
    breaker meant hitting that cap blocked brand new checkouts too."""

    def test_a_payment_link_failure_does_not_open_the_order_breaker(self):
        breaker = RazorpayClient()
        for _ in range(FAILURE_THRESHOLD):
            with pytest.raises(RazorpayDown):
                breaker._guarded("payment_link", lambda: (_ for _ in ()).throw(RuntimeError("quota")))

        assert breaker.state("payment_link") == "open"
        assert breaker.state("order") == "closed", (
            "a payment_link quota failure must not block creating a new order"
        )
        # And a real order call still goes through.
        assert breaker.create_order(amount_paise=1000, receipt="r", notes={})

    def test_each_operation_tracks_its_own_failure_count_and_last_error(self):
        breaker = RazorpayClient()
        for _ in range(3):
            with pytest.raises(RazorpayDown):
                breaker._guarded("fetch", lambda: (_ for _ in ()).throw(RuntimeError("fetch broke")))

        status = breaker.status()
        assert status["by_operation"]["fetch"]["consecutive_failures"] == 3
        assert "fetch broke" in status["by_operation"]["fetch"]["last_error"]
        assert status["by_operation"]["order"]["consecutive_failures"] == 0
        assert status["by_operation"]["payment_link"]["consecutive_failures"] == 0

    def test_the_chaos_toggle_still_takes_down_every_operation_at_once(self):
        """Unlike a real, narrow failure, the deliberate 'Razorpay is down'
        demo toggle should simulate a genuine full outage -- every operation
        fails, not just the one that happened to be called first."""
        breaker = RazorpayClient()
        breaker.chaos_down = True
        for _ in range(FAILURE_THRESHOLD):
            with pytest.raises(RazorpayDown):
                breaker.create_order(amount_paise=1000, receipt="r", notes={})
        for _ in range(FAILURE_THRESHOLD):
            with pytest.raises(RazorpayDown):
                breaker.fetch_payments()
        breaker.chaos_down = False

        assert breaker.state("order") == "open"
        assert breaker.state("fetch") == "open"

    def test_reset_breaker_clears_every_operation(self):
        breaker = RazorpayClient()
        for op in ("order", "payment_link", "fetch"):
            for _ in range(FAILURE_THRESHOLD):
                with pytest.raises(RazorpayDown):
                    breaker._guarded(op, lambda: (_ for _ in ()).throw(RuntimeError("x")))
            assert breaker.state(op) == "open"

        breaker.reset_breaker()
        for op in ("order", "payment_link", "fetch"):
            assert breaker.state(op) == "closed"
        assert breaker.status()["last_error"] is None


class TestTheOriginalErrorSurvivesTheWrapper:
    def test_last_error_is_the_real_cause_not_the_breaker_wrapper_text(self):
        """Every retry while open used to overwrite the stored reason with
        'circuit breaker open...', erasing what actually went wrong first."""
        breaker = RazorpayClient()
        trip(breaker)
        assert "chaos" in breaker.status()["last_error"]

        with pytest.raises(RazorpayDown) as excinfo:
            breaker.create_order(amount_paise=1000, receipt="r", notes={})
        assert "chaos" in str(excinfo.value)
        assert breaker.status()["last_error"] is not None
        assert "circuit breaker open" not in breaker.status()["last_error"]

    def test_reset_breaker_clears_the_last_error_too(self):
        breaker = RazorpayClient()
        trip(breaker)
        breaker.reset_breaker()
        assert breaker.status()["last_error"] is None
        assert breaker.status()["breaker"] == "closed"
