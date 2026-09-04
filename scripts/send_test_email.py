"""Send one real email through app/delivery.py and report exactly what happened.

    python scripts/send_test_email.py [address]

Bypasses Razorpay entirely -- this exercises only the delivery path, so it
still works when the Razorpay circuit breaker is open (e.g. the test-mode
payment link daily limit). Address defaults to the first entry in
DELIVERY_ALLOWLIST, or EMAIL_FROM_ADDRESS if the allowlist is empty.

This is the ground truth for "am I actually receiving these": a printed
message id means Resend accepted the message for delivery. It does not
guarantee the inbox over spam, so check both.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import delivery, messages
from app.config import (
    DELIVER_FOR_REAL,
    DELIVERY_ALLOWLIST,
    EMAIL_CONFIGURED,
    EMAIL_FROM_ADDRESS,
)
from app.models import Action, Case, Customer

print(f"DELIVER_FOR_REAL : {DELIVER_FOR_REAL}")
print(f"EMAIL_CONFIGURED : {EMAIL_CONFIGURED} (from {EMAIL_FROM_ADDRESS or 'no EMAIL_FROM_ADDRESS set'})")
print(f"DELIVERY_ALLOWLIST: {DELIVERY_ALLOWLIST or '(empty -- everyone is allowed)'}")

if not EMAIL_CONFIGURED:
    print("\nRESEND_API_KEY is not set in .env. Nothing to test.")
    raise SystemExit(1)

recipient = sys.argv[1] if len(sys.argv) > 1 else (DELIVERY_ALLOWLIST[0] if DELIVERY_ALLOWLIST else EMAIL_FROM_ADDRESS)
if DELIVERY_ALLOWLIST and recipient.lower() not in DELIVERY_ALLOWLIST:
    print(f"\n{recipient} is not on DELIVERY_ALLOWLIST -- delivery.deliver() will refuse it.")
    print(f"Either add it to DELIVERY_ALLOWLIST in .env, or run with no argument to use "
          f"{DELIVERY_ALLOWLIST[0]}.")
    raise SystemExit(1)

case = Case(
    id=0,
    run_id=None,  # a synthetic run_id would make delivery.deliver() refuse on purpose
    razorpay_order_id="order_test_email_probe",
    amount_paise=400000,
    card_last4="1111",
    error_reason="card_disabled_for_online_payments",
    root_cause="card_config",
    cart_json='[{"name": "Attikan Estate Coffee 250g", "price_paise": 65000}]',
    failed_at=datetime.now(),
)
customer = Customer(id=0, name="Test Recipient", email=recipient, language="en")
action = Action(
    id=0,
    case_id=0,
    channel="email",
    message_intent="must_use_alternate_method",
    suggests_alt_method=True,
    message_index=1,
)

resume_url = "http://localhost:5173/orders/order_test_email_probe/resume?token=probe"
body, source, rationale, model = messages.write_body(
    case, action, customer, resume_url, use_llm=True
)
action.message_body = body
print(f"\nMessage written by: {source}" + (f" ({model})" if model else ""))
print("-" * 60)
print(body)
print("-" * 60)

status, provider_id, detail = delivery.deliver(action, case, customer)

print(f"\nstatus: {status}")
print(f"detail: {detail}")
if provider_id:
    print(f"id    : {provider_id}")

if status == "sent":
    print(f"\nCheck the inbox (and spam folder) for {recipient}.")
elif status == "skipped":
    print("\nNothing was sent. The 'detail' line above says exactly why.")
else:
    print(
        "\nResend rejected it. The 'detail' line above carries the real reason -- "
        "most often an unverified sending domain or a spent quota."
    )
