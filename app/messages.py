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

The eight templates here are the floor, not the ceiling. What normally goes
out is pre-written copy from copy_cache -- the whole space of messages, written
and reviewed ahead of time -- filled with this case's details. These templates
are what a missing or unusable cell falls back to, which is why they stay.
"""

import json
import re
import string
from dataclasses import dataclass
from typing import NamedTuple

from app import copy_cache
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


def _greeting_name(customer: Customer | None) -> str:
    """The name the message opens with.

    Whatever someone typed into a checkout form is what arrives here: "nish",
    "NISHANT", "  aarav sharma ". A merchant writing to a customer capitalises
    the name and uses the first one only. Echoing the raw string back is what
    makes a real message read like an unfinished mail merge, and "Hi nish," is
    exactly that.

    Mixed case is left alone, because it is usually deliberate -- McDonald and
    O'Neill are not improved by capitalize().
    """
    raw = ((customer.name if customer else None) or "").strip()
    first = raw.split(" ")[0] if raw else ""

    # An address or a number is not a name, and "Hi nishant03115@gmail.com,"
    # is worse than no name at all.
    if not first or "@" in first or not any(char.isalpha() for char in first):
        return "there"

    return first.capitalize() if first.isupper() or first.islower() else first


def build_context(case: Case, customer: Customer | None, resume_url: str) -> MessageContext:
    try:
        items = json.loads(case.cart_json or "[]")
    except (ValueError, TypeError):
        items = []
    names = [str(i.get("name", "")) for i in items if isinstance(i, dict) and i.get("name")]
    items_display = ", ".join(names[:3]) if names else "your saved cart"

    return MessageContext(
        customer_name=_greeting_name(customer),
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


def validate_template(template: str, allowed: frozenset[str]) -> list[str]:
    """The trust rules as they can be checked *before* the slots are filled.

    Two of the four survive unchanged here: a template cannot contain urgency
    language or ask for information whatever gets substituted in. The other two
    are about concrete values that do not exist yet, so they become structural
    checks -- the link has to be present as a placeholder, and a literal rupee
    figure must not appear at all, because the only correct way to state the
    amount is {amount}.

    They are checked again after slot filling, since a filled value can
    introduce a second amount that no template inspection would catch.
    """
    problems = []
    low = template.lower()

    for marker in URGENCY_MARKERS:
        if marker in low:
            problems.append(f"urgency language: '{marker.strip()}'")

    for marker in INFO_REQUEST_MARKERS:
        if marker in low:
            problems.append(f"asks for information: '{marker.strip()}'")

    for found in RUPEE_AMOUNT.findall(template):
        problems.append(f"literal amount {found!r} -- use the {{amount}} slot")

    try:
        fields = {name for _, name, _, _ in string.Formatter().parse(template) if name}
    except ValueError as exc:  # unbalanced braces
        return problems + [f"malformed placeholders: {exc}"]

    unknown = fields - allowed
    if unknown:
        problems.append(f"unknown placeholder(s): {', '.join(sorted(unknown))}")
    if "resume_url" not in fields:
        problems.append("does not link to the merchant's resume page")
    if not ({"order_id", "item_names"} & fields):
        problems.append("no merchant-only detail (order id or item names)")

    return problems


def fill(template: str, ctx: MessageContext) -> str:
    """Slot-fill a stored template. Raises on a bad placeholder so the caller
    can fall through to the next tier rather than send a broken message."""
    return template.format(
        customer_name=ctx.customer_name,
        merchant_name=ctx.merchant,
        order_id=ctx.order_id,
        item_names=ctx.items_display,
        amount=ctx.amount_display,
        last4=ctx.card_last4,
        attempt_time=ctx.attempt_time,
        resume_url=ctx.resume_url,
        alt_method="UPI, net banking or a different card",
    )


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


class Written(NamedTuple):
    """What went out, and where the words came from."""

    body: str
    source: str  # "copy" (pre-written table) | "template" (hand-written)
    tier: int  # 1 exact cell, 2 generic vertical, 3 en/generic, 4 template
    variant: str | None
    detail: str | None  # why a tier was skipped, when one was


def write_body(
    case: Case,
    action: Action,
    customer: Customer | None,
    resume_url: str,
    mention_reason: bool = True,
    use_llm: bool = True,
    rule_id: str | None = None,
    locale: str | None = None,
    vertical: str | None = None,
) -> Written:
    """Pick the wording for one message. Makes no network call.

    The copy is written ahead of time and looked up here (see copy_cache);
    the eight hand-written templates are tier 4, the floor that a missing
    cell, a malformed placeholder, or a stored string that no longer passes
    the trust rules all land on.

    `use_llm` is kept as the parameter name because it is part of the run API
    and every caller passes it. It now means "consult the pre-written copy" --
    false goes straight to the hand-written template, which is what the
    synthetic comparison runs want.
    """
    ctx = build_context(case, customer, resume_url)
    template_body = render_template(action.message_intent, ctx)

    if not use_llm or rule_id is None:
        return Written(template_body, "template", 4, None, None)

    locale = locale or (customer.language if customer else None) or copy_cache.FALLBACK_LOCALE
    vertical = vertical or copy_cache.DEFAULT_VERTICAL
    # Deterministic per case, so re-running a demo picks the same wording and
    # a case detail page never disagrees with the message that went out.
    seed_key = f"{case.run_id or 'live'}:{case.id}"

    entry, tier = copy_cache.lookup(rule_id, locale, vertical, seed_key)
    if entry is None:
        return Written(template_body, "template", 4, None, None)

    try:
        body = fill(entry["body_template"], ctx)
    except (KeyError, IndexError) as exc:
        # A bad placeholder in stored copy must never raise mid-send.
        return Written(
            template_body, "template", 4, None, f"copy {entry['key']} has a bad slot: {exc}"
        )

    # The second pass. A filled value can introduce something the template
    # could not: most obviously a second rupee amount from the item names.
    problems = validate(body, case, ctx)
    if problems:
        return Written(
            template_body,
            "template",
            4,
            None,
            f"copy {entry['key']} rejected after filling: {'; '.join(problems)}",
        )

    return Written(body, "copy", tier, entry["variant"], None)
