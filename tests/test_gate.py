"""The stopping rules. Every check is recorded whether it passes or fails."""

from datetime import datetime, timedelta

import pytest

from app.gate import BASELINE_ENFORCED, BLOCK, DEFER, ESCALATE, HALT, Budget, check
from app.models import Action, Case, Customer

NOON = datetime(2026, 3, 3, 12, 0)


def make_case(**overrides) -> Case:
    base = dict(id=1, amount_paise=400000, status="planned", root_cause="transient_network")
    base.update(overrides)
    return Case(**base)


def make_action(**overrides) -> Action:
    base = dict(id=1, case_id=1, channel="email", discount_paise=0, message_index=1)
    base.update(overrides)
    return Action(**base)


def names(result) -> set:
    return {c.name for c in result.checks}


class TestEveryCheckIsRecorded:
    def test_all_six_checks_run_even_when_the_first_one_blocks(self):
        customer = Customer(id=1, opted_out=True)
        result = check(make_case(), make_action(), NOON, customer=customer)
        assert len(result.checks) == 6
        assert names(result) == {
            "customer_opted_out",
            "case_already_recovered",
            "message_cap",
            "contact_frequency",
            "run_budget",
            "discount_authority",
        }

    def test_a_clean_case_is_allowed(self):
        result = check(make_case(), make_action(), NOON, customer=Customer(id=1))
        assert result.allowed
        assert all(c.passed for c in result.checks)


class TestBlocking:
    def test_opted_out_customers_are_never_contacted(self):
        customer = Customer(id=1, opted_out=True)
        result = check(make_case(), make_action(), NOON, customer=customer)
        assert not result.allowed and result.outcome == BLOCK
        assert "opted out" in result.reason

    def test_a_customer_who_already_paid_is_never_messaged(self):
        """Fails at 8:47, pays on their own at 8:49. No message at 8:52."""
        case = make_case(status="recovered")
        result = check(case, make_action(), NOON, customer=Customer(id=1))
        assert not result.allowed and result.outcome == BLOCK
        assert "already succeeded" in result.reason

    def test_the_message_cap_stops_a_third_message(self):
        result = check(
            make_case(), make_action(), NOON, customer=Customer(id=1),
            messages_sent=2, max_messages=2,
        )
        assert not result.allowed and result.outcome == BLOCK


class TestDeferring:
    @pytest.mark.parametrize("hour", [0, 3, 8, 9, 12, 17, 20, 21, 23])
    def test_there_are_no_quiet_hours_for_email(self, hour):
        """Deliberate. A quiet-hours rule protects someone from being woken by
        a phone at 3am; email waits in an inbox instead. Keeping it would defer
        a two-minute timeout message by twelve hours and buy nothing."""
        result = check(make_case(), make_action(), NOON.replace(hour=hour), customer=Customer(id=1))
        assert result.allowed, f"{hour}:00 should be sendable on an email-only channel"

    def test_one_message_per_customer_per_day(self):
        customer = Customer(id=1, last_contacted_at=NOON - timedelta(hours=3))
        result = check(make_case(), make_action(), NOON, customer=customer)
        assert result.outcome == DEFER
        assert result.defer_until == customer.last_contacted_at + timedelta(hours=24)

    def test_a_day_later_is_fine(self):
        customer = Customer(id=1, last_contacted_at=NOON - timedelta(hours=25))
        assert check(make_case(), make_action(), NOON, customer=customer).allowed


class TestHaltAndEscalate:
    def test_an_exhausted_budget_halts_the_run(self):
        """It halts and waits for a human. It does not quietly send less."""
        budget = Budget(max_messages=50, messages_used=50)
        result = check(make_case(), make_action(), NOON, customer=Customer(id=1), budget=budget)
        assert not result.allowed and result.outcome == HALT

    def test_budget_with_room_left_is_fine(self):
        budget = Budget(max_messages=50, messages_used=49)
        assert check(
            make_case(), make_action(), NOON, customer=Customer(id=1), budget=budget
        ).allowed

    def test_a_large_discount_goes_to_a_human(self):
        action = make_action(discount_paise=60000)  # INR 600 on a INR 4,000 order
        result = check(make_case(), action, NOON, customer=Customer(id=1))
        assert not result.allowed and result.outcome == ESCALATE

    def test_a_discount_inside_the_limit_is_allowed(self):
        action = make_action(discount_paise=15000)  # under both 5% and INR 500
        assert check(make_case(), action, NOON, customer=Customer(id=1)).allowed

    def test_the_five_percent_rule_binds_on_small_orders(self):
        case = make_case(amount_paise=100000)  # INR 1,000, so the cap is INR 50
        action = make_action(discount_paise=20000)  # INR 200
        assert check(case, action, NOON, customer=Customer(id=1)).outcome == ESCALATE


class TestHaltOutranksEverything:
    def test_a_halt_wins_over_a_block_and_a_defer(self):
        customer = Customer(id=1, opted_out=True, last_contacted_at=NOON)
        budget = Budget(max_messages=10, messages_used=10)
        result = check(
            make_case(status="recovered"), make_action(), NOON.replace(hour=22),
            customer=customer, budget=budget,
        )
        assert result.outcome == HALT


class TestBaselineEnforcement:
    """The baseline still runs every check -- it just does not act on most of
    them. That is what makes the comparison visible instead of hidden."""

    def test_baseline_records_the_already_paid_check_but_sends_anyway(self):
        case = make_case(status="recovered")
        result = check(
            case, make_action(), NOON, customer=Customer(id=1),
            enforced_checks=BASELINE_ENFORCED,
        )
        assert result.allowed  # it sends
        recorded = next(c for c in result.checks if c.name == "case_already_recovered")
        assert not recorded.passed  # and the record shows it should not have
        assert not recorded.enforced

    def test_baseline_still_respects_opt_out(self):
        customer = Customer(id=1, opted_out=True)
        result = check(
            make_case(), make_action(), NOON, customer=customer,
            enforced_checks=BASELINE_ENFORCED,
        )
        assert not result.allowed and result.outcome == BLOCK

    def test_baseline_still_halts_on_budget(self):
        budget = Budget(max_messages=5, messages_used=5)
        result = check(
            make_case(), make_action(), NOON, customer=Customer(id=1),
            budget=budget, enforced_checks=BASELINE_ENFORCED,
        )
        assert result.outcome == HALT


class TestTheClockCanBeWoundBack:
    """The demo clock can be reset, and `uvicorn --reload` resets it on every
    code change. Either leaves customer.last_contacted_at stamped in the
    future, and the frequency cap has to survive that without exiling the
    action days out."""

    def test_a_future_last_contact_never_defers_beyond_the_cap(self):
        customer = Customer(id=1, last_contacted_at=NOON + timedelta(days=2))
        result = check(make_case(), make_action(), NOON, customer=customer)

        assert result.outcome == DEFER
        # Not NOON + 3 days, which is what last_contacted_at + 24h would give.
        assert result.defer_until <= NOON + timedelta(hours=24)

    def test_it_says_so_rather_than_reporting_negative_hours(self):
        customer = Customer(id=1, last_contacted_at=NOON + timedelta(days=2))
        result = check(make_case(), make_action(), NOON, customer=customer)

        detail = next(c for c in result.checks if c.name == "contact_frequency").detail
        assert "-" not in detail
        assert "future" in detail

    def test_an_ordinary_recent_contact_is_unaffected(self):
        customer = Customer(id=1, last_contacted_at=NOON - timedelta(hours=1))
        result = check(make_case(), make_action(), NOON, customer=customer)

        assert result.outcome == DEFER
        assert result.defer_until == NOON + timedelta(hours=23)


class TestTheContactCapIsConfigurable:
    """24h is the product default and what every published result uses. It is
    settable only because a live demo runs every test checkout through one
    phone number, so they are all one customer."""

    def test_the_shipped_default_is_still_twenty_four_hours(self):
        """Read from .env.example, not from the loaded config: the config is
        pinned by conftest, and what actually matters is that the value a new
        clone gets -- the one every published result assumes -- has not been
        quietly loosened."""
        from pathlib import Path

        shipped = Path(__file__).resolve().parents[1] / ".env.example"
        line = next(
            l for l in shipped.read_text(encoding="utf-8").splitlines()
            if l.startswith("MIN_HOURS_BETWEEN_CONTACTS=")
        )
        assert line.split("=", 1)[1].strip() == "24"

    def test_zero_lets_back_to_back_messages_through(self, monkeypatch):
        monkeypatch.setattr("app.gate.MIN_HOURS_BETWEEN_CONTACTS", 0)
        customer = Customer(id=1, last_contacted_at=NOON - timedelta(minutes=1))

        result = check(make_case(), make_action(), NOON, customer=customer)

        assert result.allowed
