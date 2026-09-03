"""Message bodies.

A bare payment link looks exactly like a scam, because that is what scams are.
Every message here links to the merchant's own resume page and carries details
only the real merchant could know: the order ID, the actual item names, the
time of the attempt, the last four digits of the card.

Four rules, enforced by validate() and covered by tests:
  - never create urgency
  - never change the amount
  - never ask for information
  - always link to the merchant's own domain

Templates are the ground truth. The LLM (see write_body) only rephrases, and
anything it produces that fails validate() is thrown away.
"""

import json
import re
from dataclasses import dataclass

from app.models import Action, Case, Customer

MERCHANT_NAME = "Blue Tokai Coffee"

# Phishing markers. A real merchant does not rush you.
URGENCY_MARKERS = [
    "expire", "expiring", "expires", "hurry", "act now", "act fast",
    "last chance", "final chance", "limited time", "limited-time", "don't miss",
    "dont miss", "urgent", "immediately", "right now", "countdown",
    "will be cancelled", "will be canceled", "order will be lost", "ending soon",
    "only a few", "running out", "before it's gone", "before its gone", "!!",
    "minutes left", "hours left", "today only", "reserved for",
]

# A real merchant already knows your details and never asks for these.
INFO_REQUEST_MARKERS = [
    "otp", "cvv", "card number", "card no", "pin ", "password", "reply with",
    "send us your", "share your", "confirm your card", "verify your card",
    "enter your card details here", "3d secure code",
]

RUPEE_AMOUNT = re.compile(r"(?:₹|Rs\.?\s?|INR\s?)\s?([\d,]+(?:\.\d{1,2})?)")


@dataclass
class MessageContext:
    customer_name: str
    order_id: str
    amount_display: str
    items_display: str
    attempt_time: str
    card_last4: str
    resume_url: str
    merchant: str = MERCHANT_NAME


def format_rupees(paise: int | None) -> str:
    """Indian digit grouping: 4,00,000 not 400,000."""
    rupees = int(round((paise or 0) / 100))
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = f"{head},{tail}"
    return f"₹{s}"


def _clock_time(dt) -> str:
    """8:47 PM. Written by hand because %-I is not portable to Windows."""
    if dt is None:
        return "earlier today"
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def build_context(case: Case, customer: Customer | None, resume_url: str) -> MessageContext:
    try:
        items = json.loads(case.cart_json or "[]")
    except (ValueError, TypeError):
        items = []
    names = [str(i.get("name", "")) for i in items if isinstance(i, dict) and i.get("name")]
    items_display = ", ".join(names[:3]) if names else "your saved cart"

    first_name = (customer.name or "there").split()[0] if customer and customer.name else "there"
    return MessageContext(
        customer_name=first_name,
        order_id=case.razorpay_order_id or f"case-{case.id}",
        amount_display=format_rupees(case.amount_paise),
        items_display=items_display,
        attempt_time=_clock_time(case.failed_at),
        card_last4=case.card_last4 or "",
        resume_url=resume_url,
    )


def _card_phrase(c: MessageContext) -> str:
    return f"card ending {c.card_last4}" if c.card_last4 else "card"


TEMPLATES = {
    # R1 payment_timed_out -- they are still holding their phone.
    "reassure_and_resume": lambda c: (
        f"Hi {c.customer_name}, your payment for order {c.order_id} ({c.items_display}) "
        f"did not complete at {c.attempt_time} because the connection dropped. "
        f"Nothing was charged. Your cart is saved at the same total, {c.amount_display}. "
        f"You can finish it here: {c.resume_url} - {c.merchant}"
    ),
    # R2 card_number_invalid -- a typo, trivially fixable.
    "reenter_details": lambda c: (
        f"Hi {c.customer_name}, the card details entered for order {c.order_id} "
        f"({c.items_display}) at {c.attempt_time} did not match your bank's records, "
        f"so the payment did not go through. Nothing was charged. "
        f"Your cart is saved at {c.amount_display}: {c.resume_url} - {c.merchant}"
    ),
    # R3 authentication_failed -- OTP never landed. Offer an escape hatch.
    "retry_or_switch_to_upi": lambda c: (
        f"Hi {c.customer_name}, the bank verification step for order {c.order_id} "
        f"({c.items_display}) did not complete at {c.attempt_time}, so the payment "
        f"was not taken. Your cart is saved at {c.amount_display}. You can try the "
        f"{_card_phrase(c)} again or pay by UPI instead: {c.resume_url} - {c.merchant}"
    ),
    # R4 gateway_technical_error -- the outage has cleared by the time this sends.
    "bank_was_down_try_now": lambda c: (
        f"Hi {c.customer_name}, your payment for order {c.order_id} ({c.items_display}) "
        f"could not be processed at {c.attempt_time} because the bank's payment system "
        f"was temporarily unavailable. That has been resolved. Nothing was charged and "
        f"your cart is saved at {c.amount_display}: {c.resume_url} - {c.merchant}"
    ),
    # R5 insufficient_fund -- never state the reason. It is embarrassing.
    "soft_cart_reminder": lambda c: (
        f"Hi {c.customer_name}, your cart at {c.merchant} is still saved: "
        f"{c.items_display}, {c.amount_display} in total, the same price as when you "
        f"left it. Whenever you are ready: {c.resume_url}"
    ),
    # R6 card_disabled_for_online_payments -- "try again" cannot work.
    "must_use_alternate_method": lambda c: (
        f"Hi {c.customer_name}, your {_card_phrase(c)} is currently blocked for online "
        f"payments by your bank, which is why order {c.order_id} ({c.items_display}) "
        f"did not go through at {c.attempt_time}. Retrying the same card will not work. "
        f"You can pay the same total, {c.amount_display}, by UPI, net banking or a "
        f"different card here: {c.resume_url} - {c.merchant}"
    ),
    # R7 card_declined -- one message, suggest something different.
    "try_different_method": lambda c: (
        f"Hi {c.customer_name}, your bank declined the payment for order {c.order_id} "
        f"({c.items_display}) at {c.attempt_time} and nothing was charged. "
        f"Your cart is saved at {c.amount_display}. Another card or UPI usually works: "
        f"{c.resume_url} - {c.merchant}"
    ),
    # R8 payment_cancelled -- one quiet nudge, no urgency, no discount.
    "gentle_cart_reminder": lambda c: (
        f"Hi {c.customer_name}, you left {c.items_display} in your cart at {c.merchant}. "
        f"Order {c.order_id} is saved at {c.amount_display} if you would like to come "
        f"back to it: {c.resume_url}"
    ),
    # The baseline policy's one message, for comparison.
    "generic_retry": lambda c: (
        f"Hi {c.customer_name}, your payment for order {c.order_id} failed. "
        f"Please try again here: {c.resume_url} - {c.merchant}"
    ),
}


def render_template(intent: str, ctx: MessageContext) -> str:
    template = TEMPLATES.get(intent) or TEMPLATES["generic_retry"]
    return template(ctx)


def _amount_value(text: str) -> str:
    """'4,000' and '4000.00' are the same amount. Compare the number, not the
    formatting."""
    value = text.replace(",", "").strip()
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value or "0"


def validate(body: str, case: Case, ctx: MessageContext | None = None) -> list[str]:
    """Returns the trust rules this message breaks. Empty list means it is safe
    to send."""
    problems = []
    low = body.lower()

    for marker in URGENCY_MARKERS:
        if marker in low:
            problems.append(f"urgency language: '{marker.strip()}'")

    for marker in INFO_REQUEST_MARKERS:
        if marker in low:
            problems.append(f"asks for information: '{marker.strip()}'")

    expected = format_rupees(case.amount_paise).lstrip("₹")
    for found in RUPEE_AMOUNT.findall(body):
        if _amount_value(found) != _amount_value(expected):
            problems.append(f"amount {found} is not the order amount {expected}")

    if ctx is not None:
        if ctx.resume_url not in body:
            problems.append("does not link to the merchant's resume page")
        if ctx.order_id not in body and "cart" not in low:
            problems.append("no merchant-only detail (order ID or item names)")

    return problems


def write_body(
    case: Case,
    action: Action,
    customer: Customer | None,
    resume_url: str,
    mention_reason: bool = True,
    use_llm: bool = True,
) -> tuple[str, str, str | None, str | None]:
    """Returns (body, source, llm_rationale, llm_model).

    Falls back to the template whenever the LLM is unavailable, slow, or writes
    something that breaks a trust rule. The template path needs no network at
    all, which is why it was built first."""
    ctx = build_context(case, customer, resume_url)
    template_body = render_template(action.message_intent, ctx)

    if use_llm:
        # Nothing that happens in the LLM layer may stop a message going out.
        # It is the most replaceable part of the system, so the boundary is
        # guarded here rather than trusting it to handle its own failures.
        try:
            from app.llm import write_message_llm

            candidate = write_message_llm(case, action, customer, ctx, mention_reason)
        except Exception as exc:  # noqa: BLE001
            return template_body, "template", f"LLM layer failed: {exc}", None

        if candidate is not None:
            body, rationale, model = candidate
            problems = validate(body, case, ctx)
            if not problems:
                return body, "llm", rationale, model
            # Rejected. The template goes out instead and the reason is logged.
            return (
                template_body,
                "template",
                f"LLM output rejected: {'; '.join(problems)}",
                None,
            )

    return template_body, "template", None, None
