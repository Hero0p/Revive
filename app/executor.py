"""Runs one due action: gate, message, payment link, outbox row.

Nothing here decides anything. The rule chose the moment and the intent, the
gate decides whether it is still allowed, and this file carries it out.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app import delivery, gate, messages, outbox
from app.config import PUBLIC_BASE_URL
from app.models import Action, Case, Customer
from app.policy import router_policy
from app.razorpay_client import client
from app.rules import rule_for
from app.tokens import make_token


def execute(
    session: Session,
    action: Action,
    now: datetime,
    *,
    budget: gate.Budget | None = None,
    gate_mode: str = "full",
    use_llm: bool = True,
    send_real_notifications: bool = False,
    real_links: bool = True,
) -> str:
    """Returns one of: sent, deferred, blocked, escalated, halted."""
    case = session.get(Case, action.case_id)
    customer = session.get(Customer, case.customer_id) if case.customer_id else None
    rule = rule_for(case.error_reason, case.error_source, case.error_step)

    # The baseline does not read the failure reason, so it does not get the
    # rule's message allowance either. One link, once, like today.
    is_baseline = case.policy == "baseline"
    max_messages = 1 if is_baseline else (rule.max_messages if rule else 1)

    result = gate.check(
        case,
        action,
        now,
        customer=customer,
        messages_sent=outbox.sent_count(session, case.id),
        max_messages=max_messages,
        budget=budget,
        enforced_checks=gate.BASELINE_ENFORCED if gate_mode == "baseline" else None,
    )
    outbox.attach_gate_checks(session, action, result)

    if not result.allowed:
        return _handle_blocked(session, action, case, result, now)

    resume_url = build_resume_url(case, now)

    # The payment link is a record for Razorpay's own dashboard and (unused
    # here) notification options -- it is not how the customer actually pays.
    # The resume page opens Razorpay's checkout live from the browser using
    # the order itself, so a failure here does not need to block the message.
    # Real Razorpay outages/quotas fail this call constantly in test mode
    # (Payment Links has its own daily cap, separate from order creation), and
    # blocking every message on it meant a quota limit silently stopped all
    # customer communication -- something the resume page never needed.
    link: dict = {}
    payment_link_error: str | None = None
    try:
        link = client.create_payment_link(
            amount_paise=case.amount_paise or 0,
            description=f"Order {case.razorpay_order_id}",
            customer_name=(customer.name if customer else "Customer"),
            contact=(customer.contact if customer else ""),
            email=(customer.email if customer else ""),
            resume_url=resume_url,
            idempotency_key=action.idempotency_key,
            # Razorpay's own notification on the link itself. Email only,
            # same as everything else in this project.
            notify_sms=False,
            notify_email=send_real_notifications,
            force_simulated=not real_links,
        )
    except Exception as exc:  # noqa: BLE001 -- non-fatal, see the note above
        payment_link_error = str(exc)

    written = messages.write_body(
        case,
        action,
        customer,
        resume_url,
        mention_reason=(rule.mention_reason if rule else True),
        use_llm=use_llm,
        rule_id=rule.rule_id if rule else None,
    )
    outbox.mark_sent(
        session,
        action,
        written.body,
        written.source,
        now,
        link_id=link.get("id"),
        resume_url=resume_url,
        payment_link_error=payment_link_error,
        customer_name=customer.name if customer else None,
        copy_tier=written.tier,
        copy_variant=written.variant,
    )
    outbox.attach_message_meta(
        session, action, written.detail, None, written.tier, written.variant
    )

    # The outbox row exists whether or not this sends anything, so the audit
    # trail is the same either way. Only the delivery fields differ.
    delivery_status, delivery_id, delivery_detail = delivery.deliver(action, case, customer)
    action.delivery_status = delivery_status
    action.delivery_detail = delivery_detail
    action.delivery_id = delivery_id
    action.delivered_at = now if delivery_status == "sent" else None

    if customer is not None:
        customer.last_contacted_at = now
    if case.status not in ("recovered",):
        case.status = "acting"
    if budget is not None:
        budget.messages_used += 1

    if not is_baseline:
        _schedule_follow_up(session, case, customer, action, now, rule, max_messages)

    if (action.message_index or 1) >= max_messages and case.status == "acting":
        # Every message this cause allows has now been spent.
        case.status = "exhausted"

    session.flush()
    return "sent"


def _handle_blocked(
    session: Session, action: Action, case: Case, result: gate.GateResult, now: datetime
) -> str:
    if result.outcome == gate.DEFER and result.defer_until:
        outbox.defer(session, action, result.defer_until, result.reason)
        return "deferred"

    outbox.mark_blocked(session, action, result.reason or "blocked by the gate", now)

    if result.outcome == gate.HALT:
        # A halt stops the whole run and waits for a human. It does not
        # degrade into sending fewer messages.
        return "halted"
    if result.outcome == gate.ESCALATE:
        case.status = "escalated"
        return "escalated"

    if case.status not in ("recovered", "escalated"):
        # "Suppressed" means we correctly did nothing at all. If messages have
        # already gone out, the case is exhausted, not suppressed.
        already_sent = outbox.sent_count(session, case.id)
        case.status = "suppressed" if already_sent == 0 else "exhausted"
    return "blocked"


def _schedule_follow_up(
    session: Session,
    case: Case,
    customer: Customer | None,
    action: Action,
    now: datetime,
    rule,
    max_messages: int,
) -> None:
    """Causes worth two messages get the second one queued now. The gate's
    message cap is what actually stops it, so this cannot run away."""
    if rule is None or max_messages <= (action.message_index or 1):
        return
    if case.status == "recovered":
        return
    plan = router_policy(case, now, customer, message_index=(action.message_index or 1) + 1)
    if plan is not None:
        outbox.schedule(session, case, plan)


def build_resume_url(case: Case, now: datetime) -> str:
    """The message links to the merchant's own domain, never a bare payment
    link. See section 7 -- this is the whole trust design."""
    order_id = case.razorpay_order_id or f"case-{case.id}"
    token = make_token(order_id, now)
    return f"{PUBLIC_BASE_URL}/orders/{order_id}/resume?token={token}"
