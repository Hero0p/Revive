"""Actually sending the message.

Everything upstream of this file decides *what* to send and *when*. This file
is the only place that sends anything, and it refuses to send in six
situations:

  - delivery is switched off (the default)
  - the case belongs to a synthetic run, so the customer does not exist
  - the action's channel is not email -- every rule in rules.py chooses
    email, so this should never fire; it is a guard against a future rule
    regressing, not a feature
  - the recipient is not on the allowlist, when one is set
  - there is no address, or no body
  - no email transport is configured

A refusal is recorded on the action, not swallowed. The outbox row is written
before this runs either way, so the audit trail is identical whether or not a
real message left the building.

Email is the only channel, by design. Real SMS to Indian numbers needs DLT
registration with a telecom operator, which is a commercial process rather
than an API key, so it was never implemented.

There are two transports for it. SMTP is the default and needs no third-party
account, which is what makes a local demo easy. It is also unusable on most
hosting platforms: Render and friends block outbound SMTP ports outright, so
every send from a deployed instance fails with "Network is unreachable" no
matter how correct the credentials are. Setting RESEND_API_KEY switches to an
email API over HTTPS, which nothing blocks.
"""

import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import httpx

from app.config import (
    DELIVER_FOR_REAL,
    DELIVERY_ALLOWLIST,
    EMAIL_CONFIGURED,
    EMAIL_TRANSPORT,
    RESEND_API_KEY,
    RESEND_FROM,
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
        return "skipped", None, "no email transport configured (RESEND_API_KEY, or SMTP_USER / SMTP_APP_PASSWORD)"

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
    subject = SUBJECTS.get(action.message_intent, "About your recent order")
    if EMAIL_TRANSPORT == "resend":
        return _send_via_resend(recipient, subject, body)
    return _send_via_smtp(recipient, subject, body)


def _send_via_resend(recipient: str, subject: str, body: str) -> str:
    """One HTTPS call. Raises on any non-2xx so deliver() records the reason."""
    response = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={
            "from": formataddr((SMTP_FROM_NAME, RESEND_FROM)),
            "to": [recipient],
            "subject": subject,
            "text": body,
        },
        timeout=TIMEOUT,
    )
    if response.status_code >= 300:
        raise ConnectionError(f"resend {response.status_code}: {response.text}")
    return response.json().get("id", "")


def _send_via_smtp(recipient: str, subject: str, body: str) -> str:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((SMTP_FROM_NAME, SMTP_USER))
    message["To"] = recipient
    # Set explicitly: without it the audit trail records no usable id, and
    # this is the only handle on the message once it has left.
    message["Message-ID"] = make_msgid(domain="revive.local")
    message.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_APP_PASSWORD)
        server.send_message(message)

    return message["Message-ID"]


def status() -> dict:
    """What the dashboard shows about delivery configuration."""
    from app.config import PUBLIC_BASE_URL, PUBLIC_BASE_URL_IS_LOCAL

    return {
        "deliver_for_real": DELIVER_FOR_REAL,
        "email_configured": EMAIL_CONFIGURED,
        "transport": EMAIL_TRANSPORT,
        "allowlist": DELIVERY_ALLOWLIST,
        "from_email": (RESEND_FROM if EMAIL_TRANSPORT == "resend" else SMTP_USER) or None,
        "public_base_url": PUBLIC_BASE_URL,
        # Surfaced because a link pointing at localhost is useless to whoever
        # receives the message, and that is invisible from the outbox alone.
        "public_base_url_is_local": PUBLIC_BASE_URL_IS_LOCAL,
    }
