"""Two policies, same interface, same input batch.

baseline_policy is what merchants do today: same link, same time, everyone.
router_policy reads the failure cause and picks the moment and the content.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.models import Case, Customer
from app.rules import Rule, payday_window, rule_for

SECOND_MESSAGE_DECAY = 0.6


@dataclass
class Plan:
    """What a policy decided to do. Becomes an Action row plus a decision record."""

    action_type: str  # send_link | suppress | escalate
    channel: str
    scheduled_for: datetime
    message_intent: str
    suggests_alt_method: bool
    rule_id: str
    root_cause: str
    why: str
    max_messages: int
    base_conversion: float
    mention_reason: bool = True
    message_index: int = 1
    inputs: dict = field(default_factory=dict)
    decision: str = ""

    @property
    def delay_minutes(self) -> float:
        return self.inputs.get("delay_minutes", 0)


def baseline_policy(case: Case, now: datetime, message_index: int = 1) -> Plan:
    """What merchants do today: same link, same time, everyone."""
    return Plan(
        action_type="send_link",
        channel="email",
        scheduled_for=now + timedelta(minutes=5),
        message_intent="generic_retry",
        suggests_alt_method=False,
        rule_id="BASELINE",
        root_cause=case.root_cause or "unknown",
        why="Baseline sends every failed payment the same retry link five "
        "minutes later, regardless of why the payment failed.",
        max_messages=1,
        base_conversion=0.18,
        message_index=message_index,
        inputs={"delay_minutes": 5, "reads_error_reason": False},
        decision="Sent the standard retry link 5 minutes after the failure.",
    )


def router_policy(
    case: Case,
    now: datetime,
    customer: Customer | None = None,
    message_index: int = 1,
) -> Plan | None:
    """Cause-aware. Returns None when there is no rule for this failure --
    the caller escalates to a human rather than guessing."""
    rule = rule_for(case.error_reason, case.error_source, case.error_step)
    if rule is None:
        return None

    if message_index > 1:
        # A follow-up is a follow-up: a day after the first message. It does
        # not re-run the timing strategy -- re-running the payday window would
        # push the second nudge a full month out, onto a cart that is dead.
        scheduled_for = now + timedelta(hours=24)
        timing_note = "24 hours after the first message"
        inputs = _base_inputs(rule, case)
        inputs["delay_minutes"] = 24 * 60
        inputs["follow_up_to_message"] = message_index - 1
    else:
        scheduled_for, timing_note, inputs = _schedule(rule, case, now, customer)

    return Plan(
        action_type="send_link",
        channel=rule.channel,
        scheduled_for=scheduled_for,
        message_intent=rule.message_intent,
        suggests_alt_method=rule.suggests_alt_method,
        rule_id=rule.rule_id,
        root_cause=rule.root_cause,
        why=rule.why,
        max_messages=rule.max_messages,
        base_conversion=rule.base_conversion,
        mention_reason=rule.mention_reason,
        message_index=message_index,
        inputs=inputs,
        decision=(
            f"{case.error_reason} is {rule.root_cause}. "
            f"Send {rule.channel} {timing_note}"
            + (", offering an alternative payment method." if rule.suggests_alt_method else ".")
        ),
    )


def _base_inputs(rule: Rule, case: Case) -> dict:
    """Exactly what the rule looked at. Recorded on every decision."""
    return {
        "error_reason": case.error_reason,
        "error_code": case.error_code,
        "root_cause": rule.root_cause,
        "amount_paise": case.amount_paise,
        "method": case.method,
        "issuer": case.issuer,
        "channel": rule.channel,
        "max_messages": rule.max_messages,
        "base_conversion": rule.base_conversion,
    }


def _schedule(
    rule: Rule, case: Case, now: datetime, customer: Customer | None
) -> tuple[datetime, str, dict]:
    inputs = _base_inputs(rule, case)

    if rule.delay_strategy == "payday_window":
        payday_days = _payday_days(customer)
        scheduled_for = payday_window(now, payday_days)
        inputs["delay_strategy"] = "payday_window"
        inputs["observed_payday_days"] = payday_days
        inputs["delay_minutes"] = round((scheduled_for - now).total_seconds() / 60)
        note = (
            f"on {scheduled_for:%d %b} at 10:30, the customer's observed payday"
            if payday_days
            else f"on {scheduled_for:%d %b} at 10:30 (no payment history, using the 2nd)"
        )
        return scheduled_for, note, inputs

    inputs["delay_minutes"] = rule.delay_minutes
    return now + timedelta(minutes=rule.delay_minutes), _human_delay(rule.delay_minutes), inputs


def _payday_days(customer: Customer | None) -> list[int]:
    if customer is None or not customer.payday_days_json:
        return []
    try:
        return list(json.loads(customer.payday_days_json))
    except (ValueError, TypeError):
        return []


def _human_delay(minutes: int) -> str:
    if minutes < 60:
        return f"in {minutes} minutes"
    if minutes < 60 * 24:
        hours = minutes / 60
        return f"in {hours:.0f} hour{'s' if hours != 1 else ''}"
    return f"in {minutes / 1440:.0f} days"


def expected_value_paise(plan: Plan, case: Case) -> int:
    """Explainable arithmetic, not a model: conversion x amount, discounted for
    each message already spent."""
    p = plan.base_conversion * (SECOND_MESSAGE_DECAY ** (plan.message_index - 1))
    return int(round(p * (case.amount_paise or 0)))
