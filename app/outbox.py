"""Every outbound message writes a row before it is sent.

That single rule buys the dashboard panel, the audit trail, and replay after a
failure. Nothing in this codebase calls a send API without an outbox row.
"""

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.gate import GateResult
from app.models import Action, Case, DecisionRecord
from app.policy import Plan, expected_value_paise


def schedule(session: Session, case: Case, plan: Plan) -> Action:
    """Write the pending row and the decision record that explains it."""
    action = Action(
        case_id=case.id,
        action_type=plan.action_type,
        channel=plan.channel,
        message_intent=plan.message_intent,
        suggests_alt_method=plan.suggests_alt_method,
        message_index=plan.message_index,
        scheduled_for=plan.scheduled_for,
        status="pending",
        idempotency_key=idempotency_key(case, plan),
        discount_paise=0,
    )
    session.add(action)
    session.flush()

    session.add(
        DecisionRecord(
            case_id=case.id,
            action_id=action.id,
            rule_id=plan.rule_id,
            inputs_json=json.dumps(plan.inputs, default=str),
            decision=plan.decision,
            why=plan.why,
            expected_value_paise=expected_value_paise(plan, case),
            gate_checks_json="[]",  # filled when the action runs
            created_at=plan.scheduled_for,
        )
    )
    session.flush()
    return action


def record_suppression(
    session: Session, case: Case, rule_id: str, decision: str, why: str, now: datetime
) -> DecisionRecord:
    """A decision to do nothing is still a decision, and gets a record."""
    record = DecisionRecord(
        case_id=case.id,
        action_id=None,
        rule_id=rule_id,
        inputs_json=json.dumps(
            {"error_reason": case.error_reason, "error_code": case.error_code}
        ),
        decision=decision,
        why=why,
        expected_value_paise=0,
        gate_checks_json="[]",
        created_at=now,
    )
    session.add(record)
    session.flush()
    return record


def mark_sent(
    session: Session,
    action: Action,
    body: str,
    source: str,
    now: datetime,
    link_id: str | None = None,
    resume_url: str | None = None,
    payment_link_error: str | None = None,
    customer_name: str | None = None,
) -> None:
    action.message_body = body
    action.message_source = source
    action.razorpay_link_id = link_id
    action.payment_link_error = payment_link_error
    action.resume_url = resume_url
    action.customer_name_snapshot = customer_name
    action.executed_at = now
    action.status = "sent"
    # A retry that finally succeeds is not a blocked action. Clearing this
    # stops the UI reading "not sent" under a message that went out.
    action.blocked_reason = None
    session.flush()


def mark_blocked(session: Session, action: Action, reason: str, now: datetime) -> None:
    action.status = "blocked"
    action.blocked_reason = reason
    action.executed_at = now
    session.flush()


def mark_failed(session: Session, action: Action, reason: str, now: datetime) -> None:
    """Left pending-with-an-error so the worker retries when the breaker closes."""
    action.status = "failed"
    action.blocked_reason = reason
    action.executed_at = now
    session.flush()


def defer(session: Session, action: Action, until: datetime, reason: str) -> None:
    action.scheduled_for = until
    action.blocked_reason = reason
    action.status = "pending"
    session.flush()


def attach_gate_checks(session: Session, action: Action, result: GateResult) -> None:
    record = session.scalar(
        select(DecisionRecord).where(DecisionRecord.action_id == action.id)
    )
    if record is not None:
        record.gate_checks_json = json.dumps(result.as_json())
        session.flush()


def attach_message_meta(
    session: Session, action: Action, rationale: str | None, model: str | None
) -> None:
    record = session.scalar(
        select(DecisionRecord).where(DecisionRecord.action_id == action.id)
    )
    if record is not None:
        record.llm_rationale = rationale
        record.llm_model = model
        session.flush()


def sent_count(session: Session, case_id: int) -> int:
    return len(
        session.scalars(
            select(Action).where(Action.case_id == case_id, Action.status == "sent")
        ).all()
    )


def due_actions(session: Session, now: datetime, run_id: str | None = None) -> list[Action]:
    stmt = (
        select(Action)
        .join(Case, Case.id == Action.case_id)
        .where(Action.status == "pending", Action.scheduled_for <= now)
        .order_by(Action.scheduled_for)
    )
    if run_id is not None:
        stmt = stmt.where(Case.run_id == run_id)
    else:
        stmt = stmt.where(Case.run_id.is_(None))
    return list(session.scalars(stmt).all())


def idempotency_key(case: Case, plan: Plan) -> str:
    """Deterministic, so a replay after a crash cannot double-send or
    double-charge.

    The attempt number is part of the key because a second failure in the same
    checkout supersedes the first plan and schedules a fresh one. Without it,
    a customer who fails twice for the same reason produces two actions with
    the same key and the insert dies on the unique constraint -- which is the
    single most ordinary thing a real customer does.
    """
    attempt = case.attempt_count or 1
    return f"{case.run_id or 'live'}:{case.id}:{plan.rule_id}:{plan.message_index}:a{attempt}"
