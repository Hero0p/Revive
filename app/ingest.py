"""Webhook events become cases.

Three failures from one customer in one checkout session is one case, not
three. The already-paid guard lives here too: a success on the same order
closes the case before any message goes out.
"""

import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import outbox
from app.models import Action, Case, Customer
from app.policy import Plan, baseline_policy, router_policy
from app.rules import rule_for

DEDUP_WINDOW = timedelta(minutes=30)
OPEN_STATUSES = ("detected", "planned", "acting", "escalated")
SUCCESS_EVENTS = {"payment.captured", "order.paid", "payment_link.paid"}


def handle_event(
    session: Session,
    event: dict,
    now: datetime,
    run_id: str | None = None,
    policy_name: str = "router",
) -> Case | None:
    kind = event.get("event")
    entity = _entity(event)
    if entity is None:
        return None
    if kind == "payment.failed":
        return handle_failure(session, entity, now, run_id, policy_name)
    if kind in SUCCESS_EVENTS:
        return handle_success(session, entity, now, run_id)
    return None


def handle_failure(
    session: Session,
    entity: dict,
    now: datetime,
    run_id: str | None = None,
    policy_name: str = "router",
) -> Case:
    customer = find_or_create_customer(session, entity, run_id)
    order_id = entity.get("order_id") or f"order_{entity.get('id')}"
    notes = entity.get("notes") or {}

    case = _open_case_for(session, customer.id, order_id, now, run_id)
    if case is not None:
        # Same checkout session. Fold this failure in and re-plan on the
        # latest reason -- the last failure is the one that explains the drop.
        case.attempt_count = (case.attempt_count or 1) + 1
        case.razorpay_payment_id = entity.get("id")
        case.error_code = entity.get("error_code")
        case.error_reason = entity.get("error_reason")
        case.error_source = entity.get("error_source")
        case.error_step = entity.get("error_step")
        case.error_description = entity.get("error_description")
        case.method = entity.get("method")
        # Consistent with folding in the latest error_reason: the latest
        # attempt is also the one whose name we display for the case.
        case.customer_name_snapshot = customer.name
        _supersede_pending(session, case, now)
    else:
        card = entity.get("card") or {}
        case = Case(
            razorpay_payment_id=entity.get("id"),
            razorpay_order_id=order_id,
            customer_id=customer.id,
            amount_paise=entity.get("amount"),
            currency=entity.get("currency", "INR"),
            method=entity.get("method"),
            issuer=card.get("issuer") or entity.get("bank"),
            card_last4=card.get("last4"),
            error_code=entity.get("error_code"),
            error_reason=entity.get("error_reason"),
            error_source=entity.get("error_source"),
            error_step=entity.get("error_step"),
            error_description=entity.get("error_description"),
            status="detected",
            cart_json=_cart_json(notes),
            amount_recovered_paise=0,
            failed_at=now,
            attempt_count=1,
            policy=policy_name,
            run_id=run_id,
            customer_name_snapshot=customer.name,
        )
        session.add(case)
        session.flush()

    plan_case(session, case, customer, now, policy_name)
    session.flush()
    return case


def handle_success(
    session: Session, entity: dict, now: datetime, run_id: str | None = None
) -> Case | None:
    """The already-paid guard. A customer who fails at 8:47 and succeeds on
    their own at 8:49 must never be messaged."""
    order_id = entity.get("order_id") or entity.get("id")
    stmt = select(Case).where(Case.razorpay_order_id == order_id)
    stmt = stmt.where(Case.run_id == run_id) if run_id else stmt.where(Case.run_id.is_(None))
    case = session.scalars(stmt.order_by(Case.id.desc())).first()
    if case is None:
        return None

    case.status = "recovered"
    case.resolved_at = now
    case.amount_recovered_paise = entity.get("amount") or case.amount_paise

    # Learn the payday pattern from real successes, for R5.
    customer = session.get(Customer, case.customer_id) if case.customer_id else None
    if customer is not None:
        days = json.loads(customer.payday_days_json or "[]")
        days.append(now.day)
        customer.payday_days_json = json.dumps(days[-12:])

    session.flush()
    return case


def plan_case(
    session: Session,
    case: Case,
    customer: Customer | None,
    now: datetime,
    policy_name: str = "router",
    message_index: int | None = None,
) -> Action | None:
    """Choose the action, or escalate when there is no rule for this failure."""
    index = message_index or outbox.sent_count(session, case.id) + 1

    # The root cause is a classification of the failure, not a policy decision.
    # Both policies record it; only the router acts on it.
    rule = rule_for(case.error_reason, case.error_source, case.error_step)
    case.root_cause = rule.root_cause if rule else "unknown"

    if policy_name == "baseline":
        plan: Plan | None = baseline_policy(case, now, message_index=index)
    else:
        plan = router_policy(case, now, customer, message_index=index)

    if plan is None:
        # An error reason we have never seen. Do not guess -- a human looks at
        # it. See section 5.4 of the spec.
        case.status = "escalated"
        outbox.record_suppression(
            session,
            case,
            rule_id="UNMAPPED",
            decision=f"No rule for '{case.error_reason}'. Escalated for human review.",
            why="An unrecognised failure reason. Guessing a bucket would mean "
            "acting on money without a rule behind it, so the agent stops and "
            "asks instead.",
            now=now,
        )
        return None

    case.status = "planned"
    return outbox.schedule(session, case, plan)


def reclassify_case(
    session: Session, case: Case, customer: Customer | None, new_reason: str, now: datetime
) -> Action | None:
    """A human assigns a cause the real pipeline could not determine on its
    own. This exists because Razorpay's test mode routinely returns the
    generic `payment_failed` catch-all with no corroborating detail (see
    fixtures/captured/README.md) -- the router correctly escalates those
    rather than guessing, which is honest but makes a live demo dead-end on a
    reason-less case.

    This is not the pipeline guessing. It is the explicit, logged, one-off
    action of a person watching the demo, choosing among the causes the
    decision table already knows how to handle -- the same authority the
    review queue always implied a human has (section 5.4: "Optionally ask the
    LLM to suggest a bucket... but never act on it automatically"). The
    pipeline itself never calls this on its own.
    """
    old_reason = case.error_reason
    case.error_reason = new_reason
    # The real error_source/error_step described the ORIGINAL response, not
    # this one. Left in place they would sit next to an unrelated reason and
    # misdescribe what actually happened.
    case.error_source = None
    case.error_step = None

    outbox.record_suppression(
        session,
        case,
        rule_id="DEMO_RECLASSIFIED",
        decision=f"Manually reclassified from '{old_reason}' to '{new_reason}' for this demo.",
        why="Razorpay's real response carried no reason the decision table "
        "recognises -- test mode routinely returns a generic catch-all with "
        "no usable detail. A human operator assigned a cause here so the "
        "demo could continue; the pipeline never does this by itself.",
        now=now,
    )
    return plan_case(session, case, customer, now, policy_name=case.policy)


def find_or_create_customer(
    session: Session, entity: dict, run_id: str | None = None
) -> Customer:
    contact = entity.get("contact") or ""
    notes = entity.get("notes") or {}
    name = notes.get("customer_name") or ""
    language = notes.get("language") or ""

    # The address the customer typed on the merchant's own checkout form wins
    # over the one Razorpay reports on the payment. Razorpay's modal auto-fills
    # saved details for a returning phone number, so entity.email is routinely
    # some earlier address tied to that number rather than the one this
    # customer just gave this merchant -- and the recovery message has to go to
    # the address they actually used here. Falls back to the payment entity
    # whenever the order carried no email of its own.
    email = notes.get("email") or entity.get("email") or ""

    # Scoped to the run. Two policy runs over the same synthetic batch must not
    # share contact history, or the second run inherits the first run's
    # last_contacted_at and the gate defers everything.
    stmt = select(Customer)
    stmt = stmt.where(Customer.run_id == run_id) if run_id else stmt.where(Customer.run_id.is_(None))

    # Try contact first, then email -- not either/or. A customer who checked
    # out without a phone number the first time and supplied one later must
    # still be recognised by the email that ties the two together, rather
    # than getting a second row.
    customer = None
    if contact:
        customer = session.scalars(stmt.where(Customer.contact == contact)).first()
    if customer is None and email:
        customer = session.scalars(stmt.where(Customer.email == email)).first()

    if customer is None:
        customer = Customer(
            name=name or "Customer",
            contact=contact,
            email=email,
            language=language or "en",
            opted_out=False,
            payday_days_json=notes.get("payday_days_json", "[]"),
            run_id=run_id,
        )
        session.add(customer)
    else:
        # Matching by contact only proves it is the same person, not that
        # their details are unchanged. A real customer who checks out again
        # with a corrected email must not keep going to the first one ever
        # seen for their number. Only overwrite with a value this event
        # actually supplies, and never touch payday_days_json here -- that is
        # learned from successful payments (see handle_success), and a
        # failure webhook defaulting it to "[]" would erase that history.
        if email and customer.email != email:
            customer.email = email
        if contact and customer.contact != contact:
            customer.contact = contact
        if name and customer.name != name:
            customer.name = name
        if language and customer.language != language:
            customer.language = language

    session.flush()
    return customer


def _open_case_for(
    session: Session, customer_id: int, order_id: str, now: datetime, run_id: str | None
) -> Case | None:
    """Group by (customer, order) inside a 30-minute window."""
    stmt = select(Case).where(
        Case.customer_id == customer_id,
        Case.razorpay_order_id == order_id,
        Case.status.in_(OPEN_STATUSES),
        Case.failed_at >= now - DEDUP_WINDOW,
    )
    stmt = stmt.where(Case.run_id == run_id) if run_id else stmt.where(Case.run_id.is_(None))
    return session.scalars(stmt.order_by(Case.id.desc())).first()


def _supersede_pending(session: Session, case: Case, now: datetime) -> None:
    """Nothing was sent, so these are replaced rather than blocked on merit.
    They stay in the table -- the audit trail shows the plan that was replaced."""
    for action in session.scalars(
        select(Action).where(Action.case_id == case.id, Action.status == "pending")
    ).all():
        outbox.mark_blocked(session, action, "superseded by a later failure in the same session", now)


def _cart_json(notes: dict) -> str:
    cart = notes.get("cart")
    if isinstance(cart, str):
        return cart
    if isinstance(cart, list):
        return json.dumps(cart)
    return "[]"


def _entity(event: dict) -> dict | None:
    payload = event.get("payload") or {}
    for key in ("payment", "order", "payment_link"):
        section = payload.get(key)
        if isinstance(section, dict) and isinstance(section.get("entity"), dict):
            return section["entity"]
    return None
