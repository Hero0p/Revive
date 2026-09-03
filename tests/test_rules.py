"""The decision table is the product. These tests are the spec for it."""

from datetime import datetime
from pathlib import Path

import pytest

from app.rules import (
    BLOCKED_CAUSES,
    ERROR_CODE_REASONS,
    OBSERVED_REASONS,
    RULES,
    payday_window,
    rule_for,
)

ALL_REASONS = [r for reasons in ERROR_CODE_REASONS.values() for r in reasons]


def test_every_documented_error_reason_maps_to_a_rule():
    unmapped = [reason for reason in ALL_REASONS if reason not in RULES]
    assert not unmapped, f"no rule for {unmapped}"


def test_the_table_holds_the_documented_reasons_plus_the_observed_ones():
    """The eight documented reasons were written from Razorpay's docs. The
    extras came from real test-mode captures, which emit a different set."""
    assert set(RULES) == set(ALL_REASONS) | set(OBSERVED_REASONS)


def test_every_observed_reason_has_a_captured_payload_behind_it():
    captured = list((Path(__file__).resolve().parents[1] / "fixtures" / "captured").glob("*.json"))
    names = " ".join(f.name for f in captured)
    for reason in OBSERVED_REASONS:
        assert reason in names, f"{reason} claims to be observed but has no fixture"


def test_structurally_blocked_cards_always_suggest_an_alternative():
    """A blocked card cannot succeed on retry. If the message says 'try again'
    the customer fails a second time, guaranteed."""
    for reason, rule in RULES.items():
        if rule.root_cause in BLOCKED_CAUSES:
            assert rule.suggests_alt_method, f"{rule.rule_id} must offer an alternative"


def test_insufficient_fund_never_states_the_reason():
    rule = RULES["insufficient_fund"]
    assert rule.mention_reason is False
    assert rule.delay_strategy == "payday_window"


def test_every_rule_has_a_delay_and_explains_itself():
    for reason, rule in RULES.items():
        assert rule.delay_minutes is not None or rule.delay_strategy, reason
        assert len(rule.why) > 40, f"{rule.rule_id} why is too thin to render"
        assert rule.rule_id.startswith("R")
        assert 0 < rule.base_conversion <= 1
        assert rule.channel == "email", f"{rule.rule_id} must use email, no other channel exists"
        assert rule.max_messages in {1, 2}


def test_every_rule_uses_email_and_nothing_else():
    """Email is the only channel this project can actually deliver on. A rule
    that chose sms or whatsapp would render fine but never reach anyone."""
    for reason, rule in RULES.items():
        assert rule.channel == "email", f"{reason} -> {rule.rule_id} chose {rule.channel!r}"


def test_low_conversion_causes_spend_fewer_messages():
    """Cheap-to-recover buckets get two shots, unlikely ones get exactly one."""
    for rule in RULES.values():
        if rule.base_conversion < 0.30:
            assert rule.max_messages == 1, rule.rule_id


def test_timeout_is_the_fastest_rule():
    """The whole product argument: a network timeout customer is still holding
    their phone, an out-of-funds customer is not."""
    assert RULES["payment_timed_out"].delay_minutes == 2
    assert RULES["payment_cancelled"].delay_minutes == 360
    assert RULES["insufficient_fund"].delay_strategy == "payday_window"


def test_unmapped_reason_returns_none_rather_than_guessing():
    assert rule_for("some_new_reason_razorpay_added") is None
    assert rule_for(None) is None
    assert rule_for("") is None


class TestRazorpaysCatchAllReason:
    """`payment_failed` covers unrelated situations, so it is only acted on
    when the payload says where the failure came from."""

    def test_a_bank_decline_at_authorisation_is_treated_as_a_decline(self):
        rule = rule_for("payment_failed", "bank", "payment_authorization")
        assert rule is not None
        assert rule.rule_id == "R9_BANK_DECLINED"
        assert rule.root_cause == "issuer_decline"
        assert rule.suggests_alt_method is True
        assert rule.max_messages == 1

    def test_the_bare_reason_escalates(self):
        """No corroborating fields means we do not know what happened."""
        assert rule_for("payment_failed") is None

    @pytest.mark.parametrize(
        "source,step",
        [
            ("customer", "payment_authorization"),
            ("business", "payment_initiation"),
            ("gateway", "payment_initiation"),
            ("bank", "payment_initiation"),
            (None, None),
        ],
    )
    def test_any_other_combination_escalates(self, source, step):
        assert rule_for("payment_failed", source, step) is None


class TestObservedCardBlock:
    def test_an_international_block_is_structurally_blocked(self):
        rule = rule_for("international_transaction_not_allowed")
        assert rule.root_cause in BLOCKED_CAUSES
        assert rule.suggests_alt_method is True
        assert rule.max_messages == 1


def test_gateway_outage_waits_long_enough_for_the_outage_to_clear():
    assert RULES["gateway_technical_error"].delay_minutes >= 20


class TestPaydayWindow:
    def test_uses_the_modal_day_of_past_successful_payments(self):
        now = datetime(2026, 3, 5, 14, 0)
        assert payday_window(now, [1, 1, 28, 1]) == datetime(2026, 4, 1, 10, 30)

    def test_schedules_this_month_when_the_day_is_still_ahead(self):
        now = datetime(2026, 3, 5, 14, 0)
        assert payday_window(now, [20, 20]) == datetime(2026, 3, 20, 10, 30)

    def test_defaults_to_the_2nd_of_next_month_without_history(self):
        now = datetime(2026, 3, 5, 14, 0)
        assert payday_window(now, []) == datetime(2026, 4, 2, 10, 30)

    def test_skips_months_that_do_not_have_the_day(self):
        now = datetime(2026, 1, 31, 12, 0)
        assert payday_window(now, [31]) == datetime(2026, 3, 31, 10, 30)

    def test_rolls_over_the_year(self):
        now = datetime(2026, 12, 15, 9, 0)
        assert payday_window(now, [5]) == datetime(2027, 1, 5, 10, 30)

    def test_never_schedules_in_the_past(self):
        now = datetime(2026, 6, 10, 11, 0)
        for days in ([], [1], [10], [15], [31]):
            assert payday_window(now, days) > now
