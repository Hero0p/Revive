"""Actually sending the message.

Everything upstream of this file decides *what* to send and *when*. This file
is the only place that talks to a mail server, and it refuses to send in six
situations:

  - delivery is switched off (the default)
  - the case belongs to a synthetic run, so the customer does not exist
  - the action's channel is not email -- every rule in rules.py chooses
    email, so this should never fire; it is a guard against a future rule
    regressing, not a feature
  - the recipient is not on the allowlist, when one is set
  - there is no address, or no body
  - email is not configured

A refusal is recorded on the action, not swallowed. The outbox row is written
before this runs either way, so the audit trail is identical whether or not a
real message left the building.

Email is the only channel and the only transport, by design. Real SMS to
Indian numbers needs DLT registration with a telecom operator, which is a
commercial process rather than an API key, so it was never implemented.
"""

import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app.config import (
    DELIVER_FOR_REAL,
    DELIVERY_ALLOWLIST,
    EMAIL_CONFIGURED,
    SMTP_APP_PASSWORD,
    SMTP_FROM_NAME,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USER,
)
from app.models import Action, Case, Customer

TIMEOUT = 15.0

# One line, no marketing. The body carries the detail.
SUBJECTS = {
    "reassure_and_resume": "Your order is saved - the payment did not go through",
    "reenter_details": "Your order is saved - the card details need a second look",
    "retry_or_switch_to_upi": "Your order is saved - the bank check did not complete",
    "bank_was_down_try_now": "Your order is saved - the bank was briefly unavailable",
    "soft_cart_reminder": "Your cart is still saved",
    "must_use_alternate_method": "Your order is saved - please use a different payment method",
    "try_different_method": "Your order is saved - the bank declined the payment",
    "gentle_cart_reminder": "You left something in your cart",
    "generic_retry": "Your payment did not go through",
}


def deliver(
    action: Action, case: Case, customer: Customer | None
) -> tuple[str, str | None, str | None]:
    """Returns (status, provider_id, detail).

    status is one of: sent, skipped, failed. "skipped" is a normal outcome and
    means the system deliberately did not send.
    """
    if not DELIVER_FOR_REAL:
        return "skipped", None, "real delivery is switched off (DELIVER_FOR_REAL)"

    if case.run_id is not None:
        # A synthetic customer has a made-up address. Never contact them.
        return "skipped", None, "synthetic run, no real recipient"

    if customer is None:
        return "skipped", None, "no customer on the case"

    if action.channel != "email":
        # Should never happen -- every rule chooses email. Recorded plainly
        # rather than silently dropped, in case a future rule regresses.
        return "skipped", None, f"channel {action.channel!r} is not supported, email only"

    if not EMAIL_CONFIGURED:
        return "skipped", None, "SMTP_USER / SMTP_APP_PASSWORD not set"

    body = action.message_body or ""
    if not body:
        return "skipped", None, "no message body"

    recipient = customer.email or ""
    if not recipient:
        return "skipped", None, "customer has no email address"

    if DELIVERY_ALLOWLIST and recipient.lower() not in DELIVERY_ALLOWLIST:
        return "skipped", None, f"{recipient} is not on DELIVERY_ALLOWLIST"

    try:
        provider_id = _send_email(recipient, action, body)
    except Exception as exc:  # noqa: BLE001 -- delivery must never break the run
        return "failed", None, f"{type(exc).__name__}: {exc}"

    return "sent", provider_id, f"emailed {recipient}"


def _send_email(recipient: str, action: Action, body: str) -> str:
    message = EmailMessage()
    message["Subject"] = SUBJECTS.get(action.message_intent, "About your recent order")
    message["From"] = formataddr((SMTP_FROM_NAME, SMTP_USER))
    message["To"] = recipient
    # Set explicitly: without it the audit trail records no usable id, and
    # this is the only handle on the message once it has left.
    message["Message-ID"] = make_msgid(domain="recovery-router.local")
    message.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_APP_PASSWORD)
        server.send_message(message)

    return message["Message-ID"]


def status() -> dict:
    """What the dashboard shows about delivery configuration."""
    return {
        "deliver_for_real": DELIVER_FOR_REAL,
        "email_configured": EMAIL_CONFIGURED,
        "allowlist": DELIVERY_ALLOWLIST,
        "from_email": SMTP_USER or None,
    }
