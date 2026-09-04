"""Cases, the audit timeline, and the outbox panel."""

import json
import random as _random

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import delivery, ingest
from app.clock import clock, iso
from app.db import get_session
from app.models import Action, Case, Customer, DecisionRecord, RawEvent
from app.rules import RULES, rule_for

router = APIRouter(prefix="/api")

# Excludes "payment_failed": it is Razorpay's ambiguous catch-all and only
# ever resolves to a rule with error_source/error_step corroboration a manual
# reclassification does not have. See app.ingest.reclassify_case.
RECLASSIFIABLE_REASONS = [r for r in RULES if r != "payment_failed"]


class ReclassifyRequest(BaseModel):
    error_reason: str | None = None
    random: bool = False


@router.get("/cases")
def list_cases(
    session: Session = Depends(get_session),
    root_cause: str | None = None,
    status: str | None = None,
    run_id: str | None = None,
    policy: str | None = None,
    limit: int = Query(200, le=1000),
):
    stmt = select(Case).order_by(Case.failed_at.desc(), Case.id.desc())
    if root_cause:
        stmt = stmt.where(Case.root_cause == root_cause)
    if status:
        stmt = stmt.where(Case.status == status)
    if policy:
        stmt = stmt.where(Case.policy == policy)
    if run_id == "live":
        stmt = stmt.where(Case.run_id.is_(None))
    elif run_id:
        stmt = stmt.where(Case.run_id == run_id)

    cases = list(session.scalars(stmt.limit(limit)).all())
    return {"cases": [case_summary(session, c) for c in cases]}


@router.get("/cases/{case_id}")
def case_detail(case_id: int, session: Session = Depends(get_session)):
    case = session.get(Case, case_id)
    if case is None:
        raise HTTPException(404, "case not found")

    customer = session.get(Customer, case.customer_id) if case.customer_id else None
    actions = list(
        session.scalars(select(Action).where(Action.case_id == case_id).order_by(Action.id)).all()
    )
    records = list(
        session.scalars(
            select(DecisionRecord).where(DecisionRecord.case_id == case_id).order_by(DecisionRecord.id)
        ).all()
    )
    rule = rule_for(case.error_reason, case.error_source, case.error_step)

    return {
        **case_summary(session, case),
        "customer": {
            "id": customer.id,
            "name": customer.name,
            "contact": customer.contact,
            "email": customer.email,
            "language": customer.language,
            "opted_out": customer.opted_out,
            "last_contacted_at": iso(customer.last_contacted_at),
        }
        if customer
        else None,
        "rule": {
            "rule_id": rule.rule_id,
            "why": rule.why,
            "channel": rule.channel,
            "max_messages": rule.max_messages,
            "base_conversion": rule.base_conversion,
            "delay_minutes": rule.delay_minutes,
            "delay_strategy": rule.delay_strategy,
            "suggests_alt_method": rule.suggests_alt_method,
            "mention_reason": rule.mention_reason,
        }
        if rule
        else None,
        "actions": [action_dict(a) for a in actions],
        "decision_records": [record_dict(r) for r in records],
    }


@router.post("/cases/{case_id}/reclassify")
def reclassify(
    case_id: int, body: ReclassifyRequest, session: Session = Depends(get_session)
):
    """A human assigns a cause the pipeline could not determine on its own.

    Only for escalated live cases -- see app.ingest.reclassify_case for why
    this exists and why nothing in the pipeline ever calls it automatically.
    """
    case = session.get(Case, case_id)
    if case is None:
        raise HTTPException(404, "case not found")
    if case.run_id is not None:
        raise HTTPException(400, "cannot reclassify a synthetic case")
    if case.status != "escalated":
        raise HTTPException(400, f"case is {case.status}, not escalated -- nothing to reclassify")

    if body.random:
        new_reason = _random.choice(RECLASSIFIABLE_REASONS)
    elif body.error_reason in RECLASSIFIABLE_REASONS:
        new_reason = body.error_reason
    else:
        raise HTTPException(
            400, f"error_reason must be one of {RECLASSIFIABLE_REASONS}, or set random=true"
        )

    customer = session.get(Customer, case.customer_id) if case.customer_id else None
    ingest.reclassify_case(session, case, customer, new_reason, clock.now())
    session.commit()
    return case_summary(session, case)


@router.get("/cases/{case_id}/timeline")
def case_timeline(case_id: int, session: Session = Depends(get_session)):
    """Two lanes for the same failed payment: what the baseline did, and what
    the router did. The counterpart case is the same synthetic order in the
    other policy's run, so both lanes describe one customer."""
    case = session.get(Case, case_id)
    if case is None:
        raise HTTPException(404, "case not found")

    lanes = {}
    for candidate in _counterparts(session, case):
        lanes[candidate.policy or "router"] = _lane(session, candidate)

    origin = min(
        (lane["failed_at"] for lane in lanes.values() if lane["failed_at"]), default=None
    )
    return {"case_id": case_id, "order_id": case.razorpay_order_id, "origin": origin, "lanes": lanes}


def _counterparts(session: Session, case: Case) -> list[Case]:
    if case.run_id is None:
        return [case]
    stmt = select(Case).where(Case.razorpay_order_id == case.razorpay_order_id)
    return list(session.scalars(stmt).all())


def _lane(session: Session, case: Case) -> dict:
    actions = list(
        session.scalars(select(Action).where(Action.case_id == case.id).order_by(Action.id)).all()
    )
    records = {
        r.action_id: r
        for r in session.scalars(
            select(DecisionRecord).where(DecisionRecord.case_id == case.id)
        ).all()
    }

    events = [
        {
            "kind": "failed",
            "at": iso(case.failed_at),
            "label": "Payment failed",
            "detail": case.error_reason,
        }
    ]
    for action in actions:
        record = records.get(action.id)
        # "failed" already means the payment failed, so a failed send is
        # named distinctly rather than colliding with it.
        kind = {"sent": "message", "failed": "send_failed"}.get(action.status, action.status)
        events.append(
            {
                "kind": kind,
                "at": iso(action.executed_at or action.scheduled_for),
                "label": _action_label(action),
                "detail": (record.decision if record else None),
                "note": action.blocked_reason,
                "channel": action.channel,
                "status": action.status,
                "message_body": action.message_body,
                "suggests_alt_method": action.suggests_alt_method,
            }
        )
    if case.resolved_at:
        events.append(
            {
                "kind": "recovered",
                "at": iso(case.resolved_at),
                "label": "Paid",
                "detail": (
                    "Recovered without a message"
                    if case.recovery_mode == "self_recovered"
                    else "Recovered after the message"
                ),
            }
        )

    return {
        "case_id": case.id,
        "policy": case.policy,
        "status": case.status,
        "root_cause": case.root_cause,
        "failed_at": iso(case.failed_at),
        "resolved_at": iso(case.resolved_at),
        "amount_paise": case.amount_paise,
        "events": sorted(events, key=lambda e: e["at"] or ""),
    }


def _action_label(action: Action) -> str:
    if action.status == "sent":
        return f"Sent {action.channel}"
    if action.status == "blocked":
        return "Blocked"
    if action.status == "failed":
        return "Send failed"
    return f"Scheduled {action.channel}"


@router.get("/outbox")
def outbox_panel(
    session: Session = Depends(get_session),
    run_id: str | None = None,
    limit: int = Query(200, le=1000),
):
    stmt = select(Action).order_by(Action.executed_at.desc().nulls_last(), Action.id.desc())
    actions = list(session.scalars(stmt.limit(limit * 3)).all())

    rows = []
    for action in actions:
        case = session.get(Case, action.case_id)
        if case is None:
            continue
        if run_id == "live" and case.run_id is not None:
            continue
        if run_id and run_id != "live" and case.run_id != run_id:
            continue
        customer = session.get(Customer, case.customer_id) if case.customer_id else None
        rows.append(
            {
                **action_dict(action),
                "case_id": case.id,
                "order_id": case.razorpay_order_id,
                "root_cause": case.root_cause,
                "amount_paise": case.amount_paise,
                "policy": case.policy,
                # Who this was actually sent to, not whoever that contact
                # number is named today. Falls back to the live name only for
                # rows sent before this was tracked.
                "customer_name": action.customer_name_snapshot
                or (customer.name if customer else None),
            }
        )
        if len(rows) >= limit:
            break
    return {"messages": rows}


@router.get("/audit")
def audit_log(
    session: Session = Depends(get_session),
    case_id: int | None = None,
    live_only: bool = True,
    limit: int = Query(300, le=2000),
):
    """Everything that happened, in the order it happened.

    Merges the four tables that make up the audit trail: the webhook as it
    arrived, the case it became, every decision with its rule, and every
    message with whether it was actually delivered.
    """
    entries: list[dict] = []

    if case_id is None:
        for event in session.scalars(
            select(RawEvent).order_by(RawEvent.id.desc()).limit(limit)
        ).all():
            entries.append(
                {
                    "at": iso(event.received_at),
                    "kind": "webhook_rejected" if event.error else "webhook",
                    "title": f"Webhook {event.event_type}",
                    "detail": event.error or "Signature verified, payload stored",
                    "case_id": None,
                    "ref": f"raw_event #{event.id}",
                    "ok": not event.error,
                }
            )

    case_stmt = select(Case)
    if case_id is not None:
        case_stmt = case_stmt.where(Case.id == case_id)
    elif live_only:
        case_stmt = case_stmt.where(Case.run_id.is_(None))
    cases = list(session.scalars(case_stmt.order_by(Case.id.desc()).limit(limit)).all())
    case_ids = {c.id for c in cases}

    for case in cases:
        entries.append(
            {
                "at": iso(case.failed_at),
                "kind": "case",
                "title": f"Payment failed, {case.error_reason or 'no reason given'}",
                "detail": (
                    f"Classified as {case.root_cause}"
                    + (f" after {case.attempt_count} attempts" if (case.attempt_count or 1) > 1 else "")
                ),
                "case_id": case.id,
                "ref": case.razorpay_payment_id,
                "amount_paise": case.amount_paise,
                "ok": True,
            }
        )
        if case.resolved_at:
            entries.append(
                {
                    "at": iso(case.resolved_at),
                    "kind": "recovered",
                    "title": "Paid",
                    "detail": (
                        "Recovered without a message"
                        if case.recovery_mode == "self_recovered"
                        else "Recovered after the message"
                    ),
                    "case_id": case.id,
                    "ref": None,
                    "amount_paise": case.amount_recovered_paise,
                    "ok": True,
                }
            )

    for record in session.scalars(select(DecisionRecord).order_by(DecisionRecord.id.desc())).all():
        if record.case_id not in case_ids:
            continue
        entries.append(
            {
                "at": iso(record.created_at),
                "kind": "decision",
                "title": f"Decision {record.rule_id}",
                "detail": record.decision,
                "why": record.why,
                "case_id": record.case_id,
                "ref": f"EV {(record.expected_value_paise or 0) / 100:.0f}",
                "gate_checks": _json_or_empty(record.gate_checks_json),
                "ok": True,
            }
        )

    for action in session.scalars(select(Action).order_by(Action.id.desc())).all():
        if action.case_id not in case_ids:
            continue
        if action.status == "sent":
            entries.append(
                {
                    "at": iso(action.executed_at),
                    "kind": "message",
                    "title": f"Message sent on {action.channel}",
                    "detail": action.message_body,
                    "case_id": action.case_id,
                    "ref": f"written by {action.message_source}",
                    "delivery_status": action.delivery_status,
                    "delivery_detail": action.delivery_detail,
                    "delivery_id": action.delivery_id,
                    "ok": True,
                }
            )
        elif action.status == "blocked":
            entries.append(
                {
                    "at": iso(action.executed_at or action.scheduled_for),
                    "kind": "blocked",
                    "title": "Message stopped by the gate",
                    "detail": action.blocked_reason,
                    "case_id": action.case_id,
                    "ref": None,
                    "ok": False,
                }
            )
        else:
            entries.append(
                {
                    "at": iso(action.scheduled_for),
                    "kind": "scheduled",
                    "title": f"Scheduled {action.channel} message",
                    "detail": f"Waiting until {iso(action.scheduled_for)}",
                    "case_id": action.case_id,
                    "ref": action.idempotency_key,
                    "ok": True,
                }
            )

    entries.sort(key=lambda e: (e["at"] or ""), reverse=True)
    return {"entries": entries[:limit], "delivery": delivery.status()}


@router.get("/events")
def raw_events(session: Session = Depends(get_session), limit: int = Query(50, le=500)):
    """The raw_events table, including rejected webhooks."""
    events = list(
        session.scalars(select(RawEvent).order_by(RawEvent.id.desc()).limit(limit)).all()
    )
    return {
        "events": [
            {
                "id": e.id,
                "event_type": e.event_type,
                "received_at": iso(e.received_at),
                "processed_at": iso(e.processed_at),
                "error": e.error,
                "signature": (e.signature or "")[:16],
                "bytes": len(e.payload_json or ""),
            }
            for e in events
        ]
    }


@router.get("/review-queue")
def review_queue(session: Session = Depends(get_session)):
    """Unmapped failure reasons and gate escalations. Humans only."""
    cases = list(
        session.scalars(select(Case).where(Case.status == "escalated").order_by(Case.id.desc())).all()
    )
    return {"cases": [case_summary(session, c) for c in cases]}


def case_summary(session: Session, case: Case) -> dict:
    customer = session.get(Customer, case.customer_id) if case.customer_id else None
    next_action = session.scalars(
        select(Action)
        .where(Action.case_id == case.id, Action.status == "pending")
        .order_by(Action.scheduled_for)
        .limit(1)
    ).first()
    rule = rule_for(case.error_reason, case.error_source, case.error_step)

    return {
        "id": case.id,
        "order_id": case.razorpay_order_id,
        "payment_id": case.razorpay_payment_id,
        # Who failed this specific payment, not whoever that phone number is
        # named today -- the same contact is often reused across unrelated
        # test checkouts. Falls back to the live name for cases created
        # before this was tracked.
        "customer_name": case.customer_name_snapshot or (customer.name if customer else None),
        "amount_paise": case.amount_paise,
        "method": case.method,
        "issuer": case.issuer,
        "card_last4": case.card_last4,
        "error_code": case.error_code,
        "error_reason": case.error_reason,
        "root_cause": case.root_cause,
        "rule_id": rule.rule_id if rule else None,
        "status": case.status,
        "policy": case.policy,
        "run_id": case.run_id,
        "attempt_count": case.attempt_count,
        "cart": _json_or_empty(case.cart_json),
        "amount_recovered_paise": case.amount_recovered_paise,
        "failed_at": iso(case.failed_at),
        "resolved_at": iso(case.resolved_at),
        "next_action_at": iso(next_action.scheduled_for) if next_action else None,
        "next_action_channel": next_action.channel if next_action else None,
        # Which message this is, and whether the gate has pushed it back. A
        # second message is always a day out by design, and a deferred one is
        # not on the rule's schedule at all -- without these, both look like
        # the rule's delay being ignored.
        "next_action_index": next_action.message_index if next_action else None,
        "next_action_deferred": (
            next_action.blocked_reason if next_action and next_action.blocked_reason else None
        ),
    }


def action_dict(action: Action) -> dict:
    return {
        "id": action.id,
        "action_type": action.action_type,
        "channel": action.channel,
        "message_intent": action.message_intent,
        "suggests_alt_method": action.suggests_alt_method,
        "message_index": action.message_index,
        "scheduled_for": iso(action.scheduled_for),
        "executed_at": iso(action.executed_at),
        "status": action.status,
        "idempotency_key": action.idempotency_key,
        "razorpay_link_id": action.razorpay_link_id,
        "payment_link_error": action.payment_link_error,
        "resume_url": action.resume_url,
        "message_body": action.message_body,
        "message_source": action.message_source,
        "blocked_reason": action.blocked_reason,
        "delivery_status": action.delivery_status,
        "delivery_detail": action.delivery_detail,
        "delivery_id": action.delivery_id,
        "delivered_at": iso(action.delivered_at),
    }


def record_dict(record: DecisionRecord) -> dict:
    return {
        "id": record.id,
        "action_id": record.action_id,
        "rule_id": record.rule_id,
        "inputs": _json_or_empty(record.inputs_json, {}),
        "decision": record.decision,
        "why": record.why,
        "expected_value_paise": record.expected_value_paise,
        "gate_checks": _json_or_empty(record.gate_checks_json),
        "llm_rationale": record.llm_rationale,
        "llm_model": record.llm_model,
        "created_at": iso(record.created_at),
    }


@router.get("/rules")
def decision_table():
    """The decision table, rendered in the UI exactly as it exists in code."""
    return {
        "rules": [
            {
                "error_reason": reason,
                "rule_id": rule.rule_id,
                "root_cause": rule.root_cause,
                "delay_minutes": rule.delay_minutes,
                "delay_strategy": rule.delay_strategy,
                "base_conversion": rule.base_conversion,
                "channel": rule.channel,
                "message_intent": rule.message_intent,
                "suggests_alt_method": rule.suggests_alt_method,
                "max_messages": rule.max_messages,
                "mention_reason": rule.mention_reason,
                "why": rule.why,
            }
            for reason, rule in RULES.items()
        ]
    }


def _json_or_empty(raw: str | None, default=None):
    if not raw:
        return [] if default is None else default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return [] if default is None else default
