"""Synthetic cases, the outcome oracle, and the comparison runner.

The honest part: customer intent is scripted. Every number the oracle produces
is a modelled outcome, not a measurement. The profiles are written to
fixtures/profiles_seed42.json so anyone can read the assumptions.

The critical property: would_convert() cannot see which policy produced the
action. It is handed an OracleCase and an OracleAction, neither of which has a
policy field. That is enforced structurally, not by convention, and asserted in
tests/test_oracle.py.
"""

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from math import exp
from pathlib import Path
from random import Random

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import gate, ingest, outbox, worker
from app.clock import Clock
from app.config import ROOT
from app.models import Action, Case, RunMetric
from app.rules import BLOCKED_CAUSES

FIXTURES = ROOT / "fixtures"

# What fails, and how often. Roughly the shape of Indian card checkout traffic.
CAUSE_MIX = [
    ("insufficient_fund", 0.20),
    ("payment_timed_out", 0.18),
    ("authentication_failed", 0.15),
    ("payment_cancelled", 0.15),
    ("card_declined", 0.14),
    ("gateway_technical_error", 0.08),
    ("card_disabled_for_online_payments", 0.06),
    ("card_number_invalid", 0.04),
]

# Causes where a customer plausibly just tries again themselves and succeeds.
# Not insufficient_fund (no money) and not a blocked card (cannot work).
SELF_RECOVERY_CAUSES = {
    "payment_timed_out",
    "authentication_failed",
    "card_number_invalid",
    "gateway_technical_error",
}
SELF_RECOVERY_RATE = 0.12

CATALOGUE = [
    ("Attikan Estate Coffee 250g", 65000),
    ("Vienna Roast 500g", 89000),
    ("Ceramic Pour-Over Dripper", 145000),
    ("Cold Brew Bottle Pack", 42000),
    ("Easy Peasy Subscription Box", 210000),
    ("Hand Grinder", 320000),
    ("Filter Papers x100", 18000),
]

FIRST_NAMES = [
    "Aarav", "Diya", "Rohan", "Ishita", "Kabir", "Ananya", "Vikram", "Meera",
    "Arjun", "Sneha", "Rahul", "Priya", "Aditya", "Nisha", "Karan", "Tara",
]
LAST_NAMES = ["Sharma", "Iyer", "Reddy", "Kapoor", "Nair", "Bose", "Menon", "Gupta"]
ISSUERS = ["HDFC", "ICICI", "SBI", "Axis", "Kotak"]


@dataclass
class HiddenProfile:
    """What the customer would actually do. The policies never see this."""

    case_ref: str
    base_intent: float  # 0-1
    funds_available_on: date  # for insufficient_fund
    # How responsive this customer is to email specifically. Named for the
    # channel because every rule in rules.py chooses email -- there is
    # nothing else to model a response rate for.
    email_response: float
    intent_decay: float  # per hour
    needs_correct_advice: bool  # True for structurally blocked cards
    self_recovers_after_minutes: int | None  # pays on their own, unprompted


@dataclass(frozen=True)
class OracleCase:
    """Deliberately narrow. No policy field, no run id, nothing identifying."""

    root_cause: str
    amount_paise: int
    failed_at: datetime


@dataclass(frozen=True)
class OracleAction:
    """The features of an intervention. Also deliberately narrow."""

    scheduled_for: datetime
    delay_minutes: float
    hours_since_failure: float
    channel: str
    suggests_alt_method: bool
    message_index: int


def would_convert(
    profile: HiddenProfile, case: OracleCase, action: OracleAction, rng: Random
) -> bool:
    """Does this customer complete the payment after this message?

    (The spec's signature includes a clock; every time this needs is already on
    the action, so passing one would be dead weight.)
    """
    p = profile.base_intent

    if case.root_cause == "balance":
        p *= 0.15 if action.scheduled_for.date() < profile.funds_available_on else 1.0
    elif case.root_cause == "gateway_degraded":
        p *= 0.30 if action.delay_minutes < 20 else 1.0
    elif case.root_cause == "transient_network":
        p *= max(0.4, 1.0 - 0.05 * action.delay_minutes)

    if profile.needs_correct_advice and not action.suggests_alt_method:
        p *= 0.05  # "try again" on a card that cannot work online

    p *= exp(-profile.intent_decay * action.hours_since_failure)
    p *= profile.email_response
    p *= 0.6 ** (action.message_index - 1)

    return rng.random() < p


def oracle_rng(seed: int, case_ref: str, action_index: int) -> Random:
    """Same case, same action, same outcome, always."""
    return Random(f"{seed}:{case_ref}:{action_index}")


def generate_batch(
    count: int, seed: int, start: datetime
) -> tuple[list[dict], dict[str, HiddenProfile]]:
    """Synthetic failures plus the hidden profiles that decide their fate."""
    rng = Random(f"batch:{seed}")
    events: list[dict] = []
    profiles: dict[str, HiddenProfile] = {}

    causes = [c for c, _ in CAUSE_MIX]
    weights = [w for _, w in CAUSE_MIX]

    for i in range(count):
        reason = rng.choices(causes, weights=weights, k=1)[0]
        case_ref = f"case{i:04d}"
        failed_at = start + timedelta(minutes=rng.randint(0, 240))

        items = rng.sample(CATALOGUE, k=rng.randint(1, 3))
        amount = sum(price for _, price in items)

        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        payday_day = rng.choice([1, 1, 1, 2, 5, 7, 25, 28, 30])
        past_paydays = [payday_day] * rng.randint(0, 4)

        profiles[case_ref] = HiddenProfile(
            case_ref=case_ref,
            base_intent=round(rng.uniform(0.30, 0.80), 3),
            funds_available_on=_next_day_of_month(failed_at.date(), payday_day),
            # Deliberately the same weak range email always had in this model:
            # people check email less urgently than a text. Moving every rule
            # to email is a real product decision with a real modelled cost,
            # and that cost is not hidden by quietly widening this range.
            email_response=round(rng.uniform(0.14, 0.32), 3),
            # Per hour. Gentle on purpose: a customer who has not been paid yet
            # has not lost interest in the coffee, they have lost the balance.
            intent_decay=round(rng.uniform(0.002, 0.006), 5),
            needs_correct_advice=(reason == "card_disabled_for_online_payments"),
            self_recovers_after_minutes=(
                rng.randint(2, 25)
                if reason in SELF_RECOVERY_CAUSES and rng.random() < SELF_RECOVERY_RATE
                else None
            ),
        )

        events.append(
            {
                "event": "payment.failed",
                "created_at": int(failed_at.timestamp()),
                "payload": {
                    "payment": {
                        "entity": {
                            "id": f"pay_sim{i:06d}",
                            "order_id": f"order_sim{i:06d}",
                            "amount": amount,
                            "currency": "INR",
                            "status": "failed",
                            "method": "card",
                            "error_code": _error_code(reason),
                            "error_description": reason.replace("_", " "),
                            "error_source": "customer",
                            "error_step": "payment_authentication",
                            "error_reason": reason,
                            "card": {
                                "last4": f"{rng.randint(1000, 9999)}",
                                "network": rng.choice(["Visa", "MasterCard", "RuPay"]),
                                "issuer": rng.choice(ISSUERS),
                            },
                            "email": f"{name.split()[0].lower()}{i}@example.com",
                            "contact": f"+9198{rng.randint(10000000, 99999999)}",
                            "notes": {
                                "customer_name": name,
                                "case_ref": case_ref,
                                "language": rng.choice(["en", "en", "en", "hinglish"]),
                                "payday_days_json": json.dumps(past_paydays),
                                "cart": json.dumps(
                                    [{"name": n, "price_paise": p} for n, p in items]
                                ),
                            },
                        }
                    }
                },
            }
        )

    return events, profiles


def write_profiles_fixture(profiles: dict[str, HiddenProfile], seed: int) -> Path:
    """Judges can read exactly what the customers were scripted to do."""
    FIXTURES.mkdir(exist_ok=True)
    path = FIXTURES / f"profiles_seed{seed}.json"
    path.write_text(
        json.dumps(
            {
                "seed": seed,
                "note": "Hidden customer profiles used by the outcome oracle. "
                "These are assumptions, not measurements. The oracle never sees "
                "which policy produced an action.",
                "intent_decay_units": "per hour",
                "profiles": {k: asdict(v) for k, v in profiles.items()},
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return path


def run_policy(
    session: Session,
    policy_name: str,
    count: int = 200,
    seed: int = 42,
    horizon_days: int = 40,
    message_budget: int | None = None,
    use_llm: bool = False,
) -> str:
    """Run one policy over a fresh synthetic batch. Returns the run id."""
    start = datetime(2026, 3, 3, 11, 0)  # a Tuesday, inside contact hours
    run_id = f"{policy_name}-s{seed}-n{count}"
    horizon = start + timedelta(days=horizon_days)

    # A previous run with this id is replaced, so repeated runs stay comparable.
    _clear_run(session, run_id)

    events, profiles = generate_batch(count, seed, start)
    write_profiles_fixture(profiles, seed)

    sim = Clock()
    sim.freeze(start)

    for event in events:
        entity = event["payload"]["payment"]["entity"]
        failed_at = datetime.fromtimestamp(event["created_at"])
        ingest.handle_event(session, event, failed_at, run_id=run_id, policy_name=policy_name)
    session.commit()

    case_refs = _case_refs(session, run_id)
    budget = gate.Budget(max_messages=message_budget) if message_budget else None
    gate_mode = "baseline" if policy_name == "baseline" else "full"

    # Customers who were going to pay on their own anyway, and when.
    self_recoveries = sorted(
        (
            (case.failed_at + timedelta(minutes=profiles[ref].self_recovers_after_minutes), case.id)
            for case, ref in case_refs.items()
            if profiles[ref].self_recovers_after_minutes is not None
        ),
        key=lambda pair: pair[0],
    )
    self_index = 0
    halted = False

    for _ in range(10_000):  # a hard stop; a stuck run is a bug, not a demo
        next_action = _next_due(session, run_id)
        next_self = self_recoveries[self_index][0] if self_index < len(self_recoveries) else None

        upcoming = [t for t in (next_action, next_self) if t is not None]
        if not upcoming:
            break
        now = min(upcoming)
        if now > horizon:
            break

        if next_self is not None and now == next_self:
            _inject_self_recovery(session, self_recoveries[self_index][1], now, run_id)
            self_index += 1
            continue

        before = _sent_action_ids(session, run_id)
        outcomes = worker.tick(
            session,
            now,
            run_id=run_id,
            budget=budget,
            gate_mode=gate_mode,
            use_llm=use_llm,
            # Synthetic customers do not get real payment links. Only the live
            # webhook path talks to the Razorpay API.
            real_links=False,
        )
        if outcomes.get("halted_run"):
            halted = True

        # Ask the oracle about each message that actually went out.
        for action_id in _sent_action_ids(session, run_id) - before:
            _resolve_outcome(session, action_id, profiles, seed, now, run_id)
        session.commit()

        if halted:
            break

    session.commit()
    return _write_metrics(session, run_id, policy_name, seed, halted)


def _resolve_outcome(
    session: Session,
    action_id: int,
    profiles: dict[str, HiddenProfile],
    seed: int,
    now: datetime,
    run_id: str,
) -> None:
    action = session.get(Action, action_id)
    case = session.get(Case, action.case_id)
    ref = _ref_for(session, case)
    profile = profiles.get(ref)
    if profile is None or case.status == "recovered":
        return

    hours_since_failure = (now - case.failed_at).total_seconds() / 3600
    oracle_case = OracleCase(
        root_cause=case.root_cause or "unknown",
        amount_paise=case.amount_paise or 0,
        failed_at=case.failed_at,
    )
    oracle_action = OracleAction(
        scheduled_for=now,
        delay_minutes=(now - case.failed_at).total_seconds() / 60,
        hours_since_failure=hours_since_failure,
        channel=action.channel,
        suggests_alt_method=bool(action.suggests_alt_method),
        message_index=action.message_index or 1,
    )

    rng = oracle_rng(seed, ref, action.message_index or 1)
    if would_convert(profile, oracle_case, oracle_action, rng):
        # A conversion is a real capture event through the real pipeline.
        ingest.handle_event(
            session,
            _capture_event(case, now),
            now,
            run_id=run_id,
        )


def _inject_self_recovery(session: Session, case_id: int, now: datetime, run_id: str) -> None:
    """The customer paid on their own. Nothing to do with us -- but everything
    to do with whether we then message them anyway."""
    case = session.get(Case, case_id)
    if case is None or case.status == "recovered":
        return
    ingest.handle_event(session, _capture_event(case, now), now, run_id=run_id)
    # Marked so no policy can claim credit for it. Both policies see the same
    # self-recoveries at the same times, from the same seed.
    case.recovery_mode = "self_recovered"
    session.commit()


def _capture_event(case: Case, now: datetime) -> dict:
    return {
        "event": "payment.captured",
        "created_at": int(now.timestamp()),
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_ok_{case.id}",
                    "order_id": case.razorpay_order_id,
                    "amount": case.amount_paise,
                    "currency": "INR",
                    "status": "captured",
                }
            }
        },
    }


def compute_metrics(session: Session, run_id: str) -> dict:
    cases = list(session.scalars(select(Case).where(Case.run_id == run_id)).all())
    case_ids = {c.id for c in cases}
    actions = [
        a
        for a in session.scalars(select(Action)).all()
        if a.case_id in case_ids
    ]
    by_case = {c.id: c for c in cases}
    sent = [a for a in actions if a.status == "sent"]

    wrong_advice = [
        a
        for a in sent
        if (by_case[a.case_id].root_cause in BLOCKED_CAUSES) and not a.suggests_alt_method
    ]
    already_paid = [
        a
        for a in sent
        if by_case[a.case_id].resolved_at is not None
        and a.executed_at is not None
        and a.executed_at > by_case[a.case_id].resolved_at
    ]

    recovered = [c for c in cases if c.status == "recovered"]
    # Money we can claim: a message went out before the payment succeeded, and
    # the customer was not one who was going to pay unprompted anyway. The
    # second condition matters -- without it, a policy that blasts everyone
    # immediately gets credited for every customer who self-recovers later.
    contacted_before_recovery = {
        a.case_id
        for a in sent
        if by_case[a.case_id].resolved_at is not None
        and a.executed_at is not None
        and a.executed_at <= by_case[a.case_id].resolved_at
    }
    attributed = [
        c
        for c in recovered
        if c.id in contacted_before_recovery and c.recovery_mode != "self_recovered"
    ]
    self_recovered = [c for c in recovered if c not in attributed]

    at_risk = sum(c.amount_paise or 0 for c in cases)
    attributed_amount = sum(c.amount_recovered_paise or 0 for c in attributed)

    return {
        "run_id": run_id,
        "case_count": len(cases),
        # Facts about what the system did.
        "messages_sent": len(sent),
        "wrong_advice_count": len(wrong_advice),
        "already_paid_contacts": len(already_paid),
        # Messages the gate stopped before they went out: doing nothing,
        # on purpose, with a recorded reason.
        "suppressed_count": len([a for a in actions if a.status == "blocked"]),
        "suppressed_cases": len([c for c in cases if c.status == "suppressed"]),
        "escalated_count": len([c for c in cases if c.status == "escalated"]),
        "suppressed_reasons": _reason_counts(actions),
        # Modelled outcome. Depends on the oracle.
        "amount_at_risk_paise": at_risk,
        "amount_recovered_paise": attributed_amount,
        "recovery_rate": round(attributed_amount / at_risk, 4) if at_risk else 0.0,
        "cases_recovered": len(attributed),
        "self_recovered_count": len(self_recovered),
        "self_recovered_paise": sum(c.amount_recovered_paise or 0 for c in self_recovered),
    }


def _reason_counts(actions: list[Action]) -> dict:
    counts: dict[str, int] = {}
    for action in actions:
        if action.status != "blocked":
            continue
        key = (action.blocked_reason or "blocked").split(":")[0]
        counts[key] = counts.get(key, 0) + 1
    return counts


def sensitivity_sweep(seed: int = 42, count: int = 120) -> dict:
    """Does the router still win when the assumptions move?

    Re-scores both policies' actions under perturbed profiles. The headline
    claim is directional robustness, not one number.
    """
    from app.db import SessionLocal

    settings = []
    for decay_scale in (0.5, 0.75, 1.0, 1.25, 1.5):
        for channel_scale in (0.8, 1.0, 1.2):
            for payday_penalty in (0.10, 0.15, 0.25):
                settings.append((decay_scale, channel_scale, payday_penalty))

    session = SessionLocal()
    try:
        # The sweep re-scores real runs, so make sure both exist first.
        for policy_name in ("baseline", "router"):
            run_id = f"{policy_name}-s{seed}-n{count}"
            if session.scalar(select(RunMetric).where(RunMetric.run_id == run_id)) is None:
                run_policy(session, policy_name, count=count, seed=seed)

        results = []
        for decay_scale, channel_scale, payday_penalty in settings:
            scores = {}
            for policy_name in ("baseline", "router"):
                run_id = f"{policy_name}-s{seed}-n{count}"
                scores[policy_name] = _rescore(
                    session, run_id, seed, decay_scale, channel_scale, payday_penalty
                )
            results.append(
                {
                    "intent_decay_scale": decay_scale,
                    "channel_response_scale": channel_scale,
                    "payday_penalty": payday_penalty,
                    "baseline_paise": scores["baseline"],
                    "router_paise": scores["router"],
                    "router_wins": scores["router"] > scores["baseline"],
                }
            )
    finally:
        session.close()

    wins = sum(1 for r in results if r["router_wins"])
    return {
        "settings_tested": len(results),
        "router_wins": wins,
        "baseline_wins": len(results) - wins,
        "results": results,
    }


def _rescore(
    session: Session,
    run_id: str,
    seed: int,
    decay_scale: float,
    channel_scale: float,
    payday_penalty: float,
) -> int:
    """Replay the run's sent messages under perturbed assumptions."""
    profiles_path = FIXTURES / f"profiles_seed{seed}.json"
    if not profiles_path.exists():
        return 0
    raw = json.loads(profiles_path.read_text(encoding="utf-8"))["profiles"]

    cases = {c.id: c for c in session.scalars(select(Case).where(Case.run_id == run_id)).all()}
    if not cases:
        return 0
    actions = [
        a
        for a in session.scalars(select(Action).where(Action.status == "sent")).all()
        if a.case_id in cases
    ]

    recovered_total = 0
    already: set[int] = set()
    for action in sorted(actions, key=lambda a: a.executed_at or datetime.min):
        case = cases[action.case_id]
        if case.id in already:
            continue
        ref = _ref_for(session, case)
        row = raw.get(ref)
        if row is None:
            continue

        profile = HiddenProfile(
            case_ref=ref,
            base_intent=row["base_intent"],
            funds_available_on=date.fromisoformat(row["funds_available_on"]),
            email_response=min(1.0, row["email_response"] * channel_scale),
            intent_decay=row["intent_decay"] * decay_scale,
            needs_correct_advice=row["needs_correct_advice"],
            self_recovers_after_minutes=row["self_recovers_after_minutes"],
        )
        executed_at = action.executed_at or case.failed_at
        oracle_case = OracleCase(
            root_cause=case.root_cause or "unknown",
            amount_paise=case.amount_paise or 0,
            failed_at=case.failed_at,
        )
        oracle_action = OracleAction(
            scheduled_for=executed_at,
            delay_minutes=(executed_at - case.failed_at).total_seconds() / 60,
            hours_since_failure=(executed_at - case.failed_at).total_seconds() / 3600,
            channel=action.channel,
            suggests_alt_method=bool(action.suggests_alt_method),
            message_index=action.message_index or 1,
        )
        rng = oracle_rng(seed, ref, action.message_index or 1)
        if _would_convert_with_penalty(profile, oracle_case, oracle_action, rng, payday_penalty):
            recovered_total += case.amount_paise or 0
            already.add(case.id)

    return recovered_total


def _would_convert_with_penalty(
    profile: HiddenProfile,
    case: OracleCase,
    action: OracleAction,
    rng: Random,
    payday_penalty: float,
) -> bool:
    p = profile.base_intent
    if case.root_cause == "balance":
        p *= payday_penalty if action.scheduled_for.date() < profile.funds_available_on else 1.0
    elif case.root_cause == "gateway_degraded":
        p *= 0.30 if action.delay_minutes < 20 else 1.0
    elif case.root_cause == "transient_network":
        p *= max(0.4, 1.0 - 0.05 * action.delay_minutes)
    if profile.needs_correct_advice and not action.suggests_alt_method:
        p *= 0.05
    p *= exp(-profile.intent_decay * action.hours_since_failure)
    p *= profile.email_response
    p *= 0.6 ** (action.message_index - 1)
    return rng.random() < p


def _write_metrics(
    session: Session, run_id: str, policy_name: str, seed: int, halted: bool
) -> str:
    metrics = compute_metrics(session, run_id)
    existing = session.scalar(select(RunMetric).where(RunMetric.run_id == run_id))
    if existing is not None:
        session.delete(existing)
        session.flush()

    session.add(
        RunMetric(
            run_id=run_id,
            policy=policy_name,
            case_count=metrics["case_count"],
            messages_sent=metrics["messages_sent"],
            wrong_advice_count=metrics["wrong_advice_count"],
            already_paid_contacts=metrics["already_paid_contacts"],
            suppressed_count=metrics["suppressed_count"],
            amount_at_risk_paise=metrics["amount_at_risk_paise"],
            amount_recovered_paise=metrics["amount_recovered_paise"],
            seed=seed,
            sweep_json=json.dumps({"halted": halted}),
            created_at=datetime(2026, 3, 3, 11, 0),
        )
    )
    session.commit()
    return run_id


def _clear_run(session: Session, run_id: str) -> None:
    from app.models import Customer, DecisionRecord

    case_ids = [
        c.id for c in session.scalars(select(Case).where(Case.run_id == run_id)).all()
    ]
    if case_ids:
        for model in (DecisionRecord, Action):
            for row in session.scalars(select(model)).all():
                if row.case_id in case_ids:
                    session.delete(row)
        for case in session.scalars(select(Case).where(Case.run_id == run_id)).all():
            session.delete(case)
    # Customers too: re-running a run id must start with a clean contact
    # history, not the previous attempt's.
    for customer in session.scalars(select(Customer).where(Customer.run_id == run_id)).all():
        session.delete(customer)
    for metric in session.scalars(select(RunMetric).where(RunMetric.run_id == run_id)).all():
        session.delete(metric)
    session.commit()


def _case_refs(session: Session, run_id: str) -> dict:
    cases = session.scalars(select(Case).where(Case.run_id == run_id)).all()
    return {case: _ref_for(session, case) for case in cases}


def _ref_for(session: Session, case: Case) -> str:
    """order_sim000123 -> case0123. The link between a case and its profile."""
    order_id = case.razorpay_order_id or ""
    digits = "".join(ch for ch in order_id if ch.isdigit())
    return f"case{int(digits):04d}" if digits else f"case-{case.id}"


def _next_due(session: Session, run_id: str) -> datetime | None:
    stmt = (
        select(Action.scheduled_for)
        .join(Case, Case.id == Action.case_id)
        .where(Case.run_id == run_id, Action.status == "pending")
        .order_by(Action.scheduled_for)
        .limit(1)
    )
    return session.scalar(stmt)


def _sent_action_ids(session: Session, run_id: str) -> set[int]:
    stmt = (
        select(Action.id)
        .join(Case, Case.id == Action.case_id)
        .where(Case.run_id == run_id, Action.status == "sent")
    )
    return set(session.scalars(stmt).all())


def _next_day_of_month(from_date: date, day: int) -> date:
    year, month = from_date.year, from_date.month
    for _ in range(4):
        try:
            candidate = date(year, month, day)
            if candidate >= from_date:
                return candidate
        except ValueError:
            pass
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return from_date


def _error_code(reason: str) -> str:
    from app.rules import error_code_for

    return error_code_for(reason)
