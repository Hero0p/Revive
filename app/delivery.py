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

It sends over Resend's HTTPS API. SMTP is not an option: Render, like most
hosting platforms, blocks outbound SMTP ports, so every send from a deployed
instance failed with "[Errno 101] Network is unreachable" however correct the
credentials were. Port 443 is never blocked.
"""

from email.utils import formataddr

import httpx

from app.config import (
    DELIVER_FOR_REAL,
    DELIVERY_ALLOWLIST,
    EMAIL_CONFIGURED,
    EMAIL_FROM_ADDRESS,
    EMAIL_FROM_NAME,
    RESEND_API_KEY,
)
from app.models import Action, Case, Customer

TIMEOUT = 15.0
API_URL = "https://api.resend.com/emails"

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
        return "skipped", None, "RESEND_API_KEY / EMAIL_FROM_ADDRESS not set"

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
        # Redacted: this string is written to the action, shown in the
        # dashboard, and printed in logs. None of those should ever be able to
        # carry the API key.
        return "failed", None, _redact(f"{type(exc).__name__}: {exc}")

    return "sent", provider_id, f"emailed {recipient}"


def _send_email(recipient: str, action: Action, body: str) -> str:
    """One HTTPS call. Returns Resend's message id, raises on anything else."""
    response = httpx.post(
        API_URL,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": formataddr((EMAIL_FROM_NAME, EMAIL_FROM_ADDRESS)),
            "to": [recipient],
            "subject": SUBJECTS.get(action.message_intent, "About your recent order"),
            "text": body,
        },
        timeout=TIMEOUT,
    )
    if response.status_code >= 300:
        # The body carries the real reason -- an unverified sending domain, a
        # spent quota, a bad key -- and it is the only useful thing to record.
        raise ConnectionError(f"resend {response.status_code}: {_redact(response.text)}")

    return response.json().get("id", "")


def _redact(text: str) -> str:
    """Strip the API key out of anything that might be shown or logged."""
    if RESEND_API_KEY and RESEND_API_KEY in text:
        text = text.replace(RESEND_API_KEY, "***")
    return text


def status() -> dict:
    """What the dashboard shows about delivery configuration."""
    from app.config import PUBLIC_BASE_URL, PUBLIC_BASE_URL_IS_LOCAL

    return {
        "deliver_for_real": DELIVER_FOR_REAL,
        "email_configured": EMAIL_CONFIGURED,
        "transport": "resend",
        "allowlist": DELIVERY_ALLOWLIST,
        "from_email": EMAIL_FROM_ADDRESS or None,
        "public_base_url": PUBLIC_BASE_URL,
        # Surfaced because a link pointing at localhost is useless to whoever
        # receives the message, and that is invisible from the outbox alone.
        "public_base_url_is_local": PUBLIC_BASE_URL_IS_LOCAL,
    }
