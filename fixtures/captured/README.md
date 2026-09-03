# Captured webhook payloads

Real `payment.failed` bodies from the Razorpay test account, delivered to this
project over a public tunnel and stored whole in `raw_events` before anything
processed them. These are the exports.

## What the capture session found

The decision table was originally written from Razorpay's **documented** error
reason list. The test-mode API does not send that list. Three reasons showed
up that the table had never heard of. Two escalated on first contact and were
given rules; the third also escalated and was **left escalated on purpose** —
see below.

| Observed reason | Method | `error_source` | `error_step` | Outcome |
|---|---|---|---|---|
| `payment_failed` | netbanking | `bank` | `payment_authorization` | Routed to `R9_BANK_DECLINED` |
| `international_transaction_not_allowed` | card | `business` | `payment_initiation` | Routed to `R10_INTERNATIONAL_BLOCKED` |
| `payment_failed` | card (`4100 2800 0008 0001`, documented as "Insufficient Funds") | `gateway` | `payment_authorization` | **Still escalates.** See below. |

`payment_failed` is Razorpay's catch-all and carries no meaning on its own. It
is matched **only** when `error_source` is `bank` and the failure happened at
the authorisation step, which is what the netbanking payloads show and what
their `error_description` says in words: "Your payment didn't go through as it
was declined by the bank." That is a bank decline, so it routes to the decline
rule. The same reason with any other source or step still escalates rather
than being guessed at — `tests/test_rules.py::TestRazorpaysCatchAllReason` pins
that down.

### The insufficient-funds test card does not say "insufficient funds"

Razorpay documents `4100 2800 0008 0001` as the "Insufficient Funds" test
card. In a real test-mode checkout it produced `error_reason: payment_failed`,
`error_source: gateway`, `error_description: "Payment failed"` —
`payment_failed_card_gateway_1.json`. No field says insufficient funds, or
anything else actionable. `error_source: gateway` is not `bank`, so it does
not match R9's corroboration, and it correctly escalates to the human review
queue instead of guessing that a generic gateway failure means low balance.

This means `insufficient_fund` — and by extension `R5_INSUFFICIENT_FUND`, the
payday-window rule — **has never been observed from the real API.** It may
only ever arrive through a different integration path (S2S, a specific bank),
or Razorpay's test mode may simply never emit it for a card decline. Until it
is captured, that rule is documentation, not observed behaviour, and the
router will correctly refuse to act on this test card rather than assume.

To see `R5_INSUFFICIENT_FUND` actually fire, use the **Simulate** screen with
"Insufficient funds" selected — it sends the literal `error_reason:
insufficient_fund` through the same webhook route the real API would use if
it ever sent that string.

## Still missing

Seven of the eight documented reasons — `payment_timed_out`,
`card_number_invalid`, `authentication_failed`, `gateway_technical_error`,
`insufficient_fund`, `card_disabled_for_online_payments`, `card_declined` —
have **not** been reproduced against the live account. Every case seen with
those rules so far came from `/api/sim/inject`, not a real card. Until each is
captured here, those rules are written against documentation rather than
observed behaviour, and any of them could turn out to arrive under a different
string, the way `payment_failed` did three separate times.

## How to capture more

1. Start the API and a tunnel (`ngrok http 8000` or
   `cloudflared tunnel --url http://localhost:8000`).
2. Point Dashboard → Settings → Webhooks at `https://<tunnel>/webhooks/razorpay`
   with the same secret as `RAZORPAY_WEBHOOK_SECRET`, subscribed to
   `payment.failed` and `payment.captured`.
3. Run a payment from the **Checkout** screen using a card from Razorpay's
   [test card list](https://razorpay.com/docs/payments/payments/test-card-details/)
   — one card per error scenario.
4. Export what arrived:

   ```bash
   sqlite3 recovery.db "select payload_json from raw_events where error is null"
   ```

   Save one file per reason as `<error_reason>_<method>_<n>.json`.

Without a tunnel, **Pull recent payments** on the Checkout screen
(`POST /api/razorpay/sync`) polls the Payments API and pushes each real failure
through the same signed webhook route.

## Replaying one

```bash
python -c "
import json, hmac, hashlib, httpx
from app.config import RAZORPAY_WEBHOOK_SECRET
raw = open('fixtures/captured/payment_failed_netbanking_1.json','rb').read()
sig = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
print(httpx.post('http://localhost:8000/webhooks/razorpay', content=raw,
                 headers={'X-Razorpay-Signature': sig}).json())
"
```
