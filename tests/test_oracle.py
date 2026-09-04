"""The oracle must not be able to favour the router.

It is handed a customer profile, a case, and an action. None of those carry the
policy name, so it cannot know which policy produced the action. That is
enforced by the shape of the data, not by a promise in a comment.
"""

import dataclasses
import inspect
from datetime import date, datetime, timedelta

import pytest

from app.simulator import (
    HiddenProfile,
    OracleAction,
    OracleCase,
    generate_batch,
    oracle_rng,
    would_convert,
)

FAILED_AT = datetime(2026, 3, 3, 14, 0)


def profile(**overrides) -> HiddenProfile:
    base = dict(
        case_ref="case0001",
        base_intent=0.6,
        funds_available_on=date(2026, 3, 25),
        email_response=0.22,
        intent_decay=0.004,
        needs_correct_advice=False,
        self_recovers_after_minutes=None,
    )
    base.update(overrides)
    return HiddenProfile(**base)


def action(**overrides) -> OracleAction:
    base = dict(
        scheduled_for=FAILED_AT + timedelta(minutes=2),
        delay_minutes=2,
        hours_since_failure=2 / 60,
        channel="email",
        suggests_alt_method=False,
        message_index=1,
    )
    base.update(overrides)
    return OracleAction(**base)


class TestPolicyBlindness:
    """The property the whole comparison rests on."""

    def test_oracle_inputs_carry_no_policy_field(self):
        for cls in (OracleCase, OracleAction, HiddenProfile):
            names = {f.name for f in dataclasses.fields(cls)}
            leaks = {n for n in names if "policy" in n or "router" in n or "baseline" in n}
            assert not leaks, f"{cls.__name__} leaks the policy: {leaks}"

    def test_oracle_takes_nothing_but_profile_case_action_and_rng(self):
        params = set(inspect.signature(would_convert).parameters)
        assert params == {"profile", "case", "action", "rng"}

    def test_identical_actions_score_identically_whatever_produced_them(self):
        """Two policies that happen to choose the same action get the same
        outcome. There is no branch the router could benefit from."""
        case = OracleCase("issuer_decline", 210000, FAILED_AT)
        a = action()
        p = profile()
        first = would_convert(p, case, a, oracle_rng(42, "case0001", 1))
        second = would_convert(p, case, a, oracle_rng(42, "case0001", 1))
        assert first == second


class TestDeterminism:
    def test_same_case_same_action_same_outcome_always(self):
        case = OracleCase("transient_network", 150000, FAILED_AT)
        results = [
            would_convert(profile(), case, action(), oracle_rng(42, "case0007", 1))
            for _ in range(20)
        ]
        assert len(set(results)) == 1

    def test_different_cases_get_independent_draws(self):
        case = OracleCase("transient_network", 150000, FAILED_AT)
        outcomes = [
            would_convert(profile(), case, action(), oracle_rng(42, f"case{i:04d}", 1))
            for i in range(60)
        ]
        assert 0 < sum(outcomes) < 60  # not all the same draw

    def test_the_batch_is_reproducible_from_the_seed(self):
        start = datetime(2026, 3, 3, 11, 0)
        events_a, profiles_a = generate_batch(30, 42, start)
        events_b, profiles_b = generate_batch(30, 42, start)
        assert events_a == events_b
        assert profiles_a == profiles_b


class TestModelledBehaviour:
    def test_contacting_before_payday_is_heavily_penalised(self):
        case = OracleCase("balance", 400000, FAILED_AT)
        early = action(scheduled_for=datetime(2026, 3, 4, 10, 30))
        on_time = action(scheduled_for=datetime(2026, 3, 26, 10, 30))
        p = profile(base_intent=1.0, intent_decay=0.0)

        early_hits = sum(
            would_convert(p, case, early, oracle_rng(1, f"c{i}", 1)) for i in range(400)
        )
        on_time_hits = sum(
            would_convert(p, case, on_time, oracle_rng(1, f"c{i}", 1)) for i in range(400)
        )
        assert on_time_hits > early_hits * 3

    def test_try_again_on_a_blocked_card_almost_never_works(self):
        case = OracleCase("card_config", 400000, FAILED_AT)
        p = profile(needs_correct_advice=True, base_intent=1.0, intent_decay=0.0)

        retry = sum(
            would_convert(p, case, action(suggests_alt_method=False), oracle_rng(2, f"c{i}", 1))
            for i in range(400)
        )
        alternative = sum(
            would_convert(p, case, action(suggests_alt_method=True), oracle_rng(2, f"c{i}", 1))
            for i in range(400)
        )
        assert alternative > retry * 5

    def test_a_slow_response_to_a_network_timeout_costs_conversions(self):
        case = OracleCase("transient_network", 400000, FAILED_AT)
        p = profile(base_intent=1.0, intent_decay=0.004)
        fast = action(delay_minutes=2, hours_since_failure=2 / 60)
        slow = action(delay_minutes=360, hours_since_failure=6)

        fast_hits = sum(would_convert(p, case, fast, oracle_rng(3, f"c{i}", 1)) for i in range(400))
        slow_hits = sum(would_convert(p, case, slow, oracle_rng(3, f"c{i}", 1)) for i in range(400))
        assert fast_hits > slow_hits

    def test_the_second_message_is_worth_less_than_the_first(self):
        case = OracleCase("otp_failure", 400000, FAILED_AT)
        p = profile(base_intent=1.0, intent_decay=0.0)
        first = sum(
            would_convert(p, case, action(message_index=1), oracle_rng(4, f"c{i}", 1))
            for i in range(400)
        )
        second = sum(
            would_convert(p, case, action(message_index=2), oracle_rng(4, f"c{i}", 1))
            for i in range(400)
        )
        assert second < first


class TestTheSweepCannotSilentlyScoreZero:
    """The sweep re-scores a stored run against the profiles that produced it.

    When the profiles file was renamed, the reader was left looking for the old
    name and returned 0 for every setting -- reporting "Revive wins 0 of 45",
    which reads as a devastating finding rather than a missing file. A sweep
    that cannot read its inputs has to fail loudly.
    """

    def test_missing_profiles_raise_instead_of_scoring_zero(self, tmp_path, monkeypatch):
        from app import simulator
        from app.db import SessionLocal

        monkeypatch.setattr(simulator, "FIXTURES", tmp_path)
        session = SessionLocal()
        try:
            with pytest.raises(FileNotFoundError, match="nothing to re-score"):
                simulator._rescore(session, "router-s42-n3000", 42, 3000, 1.0, 1.0, 0.15)
        finally:
            session.close()

    def test_the_profiles_filename_matches_what_the_run_writes(self):
        """Writer and reader have to agree, which is the coupling that broke."""
        import inspect

        from app import simulator

        writer = inspect.getsource(simulator.write_profiles_fixture)
        reader = inspect.getsource(simulator._rescore)
        assert 'profiles_seed{seed}_n{count}.json' in writer
        assert 'profiles_seed{seed}_n{count}.json' in reader
