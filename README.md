# Revive

**Razorpay's Failed Payment Recovery sends every failed payment the same link
at the same time. Revive makes the timing and the content a function of the
failure cause.**

Payments fail for structurally different reasons, and they don't deserve the
same response.

- A **network timeout** customer is still holding their phone. Reach them in
  two minutes and they finish. Reach them in six hours and they are gone.
- An **insufficient funds** customer will fail again if you message them
  today. The same message, sent on their payday, works.
- A **card blocked for online payments** customer cannot succeed on retry,
  ever. "Try again" isn't a weak message for them — it's a structurally
  impossible one. Only "use UPI instead" can convert.

Revive reads the failure reason, picks the moment and the content, acts
within hard limits, and records why — for every one of those decisions.

---

## Setup

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload            # :8000
cd web && npm install && npm run dev     # :5173
```

Open http://localhost:5173 and press **Run both policies** on the Overview
screen. No API keys are needed for that — without them the system runs in
simulated mode and says so on every screen.

For a **real** failed payment, copy `.env.example` to `.env`, add your
Razorpay test keys, restart the API, and use the **Checkout** screen. See
[Running a real failed payment](#running-a-real-failed-payment).

```bash
pytest        # 224 tests
```

### Environment variables

Everything has a working default except the Razorpay credentials. Copy
`.env.example` to `.env` and fill in what you need — the system starts and
the full dashboard works with an empty `.env`.

| Variable | Needed for | Without it |
|---|---|---|
| `GROQ_API_KEY` | LLM-written message bodies. Free key from [console.groq.com/keys](https://console.groq.com/keys) | Hand-written templates, labelled as such in the outbox |
| `LLM_MODEL` | Overriding the model | `openai/gpt-oss-20b` |
| `LLM_REASONING_EFFORT` | Capping a reasoning model's thinking | `low`. Unset it entirely for a non-reasoning model; left unbounded, gpt-oss spends its whole token budget thinking and returns an empty body |
| `LLM_TIMEOUT_SECONDS` | The generation budget | `2.0`; slower replies fall back to templates |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | Real test-mode payment links and orders | Simulated links, labelled `plink_sim_…`, every screen says "simulated" |
| `RAZORPAY_WEBHOOK_SECRET` | Verifying real Razorpay webhooks. **The webhook secret from Settings → Webhooks, not the key secret** | Signature verification still runs, against the dev default, so the simulator works |
| `RESUME_TOKEN_SECRET` | Signing resume links | A dev default; set any random string before anything real |
| `PUBLIC_BASE_URL` | The domain in message links | `http://localhost:5173`; set this to your `cloudflared` URL for a phone demo |
| `DATABASE_URL` | Pointing at another SQLite file | `recovery.db` in the project root |
| `DELIVER_FOR_REAL` | Actually sending email | `false`. Messages render to the outbox only |
| `DELIVERY_ALLOWLIST` | Restricting who can ever be contacted | empty. **Set this while demoing** |
| `SMTP_USER` / `SMTP_APP_PASSWORD` | Real email through Gmail | Email is skipped and the reason recorded |

Changing `.env` needs an API restart — it is read once at import.

---

## The decision table

The whole product is `app/rules.py`, a plain dict. No model, no config file,
no inference. The `why` column isn't a comment — it's rendered in the UI on
every decision the system makes.

**Every rule uses email.** The build originally split rules across SMS,
WhatsApp, and email by which channel best fits the failure — see "Why email
only" below for what that traded away and why it was simplified.

| Reason | Cause | Wait | Msgs | Why |
|---|---|---|---|---|
| `payment_timed_out` | transient network | **2 min** | 2 | Nothing was wrong with the customer or the card. They're still in the buying moment. Speed matters more than anything else. |
| `card_number_invalid` | data entry | **2 min** | 2 | A typo. High intent, trivially fixable, contact immediately. |
| `authentication_failed` | OTP failure | **15 min** | 2 | OTP was wrong, expired, or never arrived. The customer tried to pay. Give the OTP state 15 minutes to settle, then offer UPI as an escape hatch. |
| `gateway_technical_error` | gateway degraded | **45 min** | 2 | The bank or gateway was down. Contacting them now sends them straight back into the outage. Wait for it to clear. |
| `insufficient_fund` | balance | **payday** | 2 | The money isn't in the account. Contacting them today guarantees a second failure. Wait for their observed payday pattern. Never state the reason — it's embarrassing. |
| `card_disabled_for_online_payments` | card config | **60 min** | 1 | The card is disabled for e-commerce by the issuing bank. "Try again" is structurally impossible — the same card fails every time. Only a message pointing at UPI or a different card can convert. |
| `card_declined` | issuer decline | **120 min** | 1 | The bank refused and didn't say why. Could be risk rules or limits. Low odds, so spend exactly one message and suggest an alternative rather than repeat. |
| `payment_cancelled` | deliberate abandon | **6 h** | 1 | They closed the modal on purpose — the lowest-intent bucket. One quiet nudge, no urgency, no discount. Chasing someone who deliberately left is how merchants get marked as spam. |

### Two more rules, from real captures

The eight rules above were written from Razorpay's **documented** reason
list. The test-mode API sends a different set. These two came out of an
actual capture session against a live test account, kept visibly separate
because the difference matters:

| Reason | Cause | Wait | Msgs | Why |
|---|---|---|---|---|
| `payment_failed` *(only when `error_source=bank` at authorisation)* | issuer decline | **120 min** | 1 | Razorpay's catch-all. Treated as a decline only when the payload says a bank rejected it at the authorisation step — what a netbanking failure looks like. Anything else carrying this reason is escalated rather than guessed at. |
| `international_transaction_not_allowed` | card config | **60 min** | 1 | The card isn't permitted for this transaction. Like a disabled card, retrying is structurally impossible — only naming a different method can convert. |

**An unmapped reason is never guessed at.** The case is created with
`root_cause="unknown"`, marked `escalated`, and put in a human review queue.
A bare `payment_failed` with no corroborating `error_source` takes that path
too.

**The payday window**, for `insufficient_fund` only: take the modal
day-of-month of the customer's past successful payments, schedule for the
next occurrence at 10:30 local. With no history, default to the 2nd of next
month.

---

## Results

Seed 42, 200 synthetic failures, both policies over the identical batch.

### What the system did

Counts of actions actually taken by the pipeline — no behavioural
assumptions, which is why these four numbers come first.

| Metric | Baseline | Router | |
|---|---:|---:|---|
| Messages sent | 200 | 308 | the router follows up on causes worth two messages |
| **Wrong advice** | **12** | **0** | "try again" sent to a card blocked for online payments |
| Already-paid contacts | 4 | 0 | messages to customers who had already paid |
| Messages suppressed | 0 | 22 | stopped by the gate, each with a recorded reason |
| Escalated to a human | 0 | 0 | no unmapped reasons in this batch |

The 12 → 0 on wrong advice is the result we'd defend hardest. It doesn't
depend on any model of customer behaviour: those twelve cards are disabled
for online payments, and "try again" cannot work on them, ever.

### Modelled outcome

This table depends on the outcome oracle, so it's presented second and
labelled as modelled, not measured.

| Metric | Baseline | Router |
|---|---:|---:|
| Amount at risk | ₹5,54,590 | ₹5,54,590 |
| Amount recovered | ₹54,140 | ₹59,270 |
| Recovery rate | 9.8% | **10.7%** |
| Cases recovered | 17 | 22 |

**+0.9 percentage points, a 9.5% relative improvement.** Every message here
goes out on email — the channel the oracle scores weakest for a "come back
and pay" nudge (`email_response` draws from 0.14–0.32, versus 0.32–0.55 for
SMS and 0.45–0.78 for WhatsApp in the same model; see `app/simulator.py`).
That's a real, disclosed cost of an email-only product decision, not a
hidden one: recalibrating `base_conversion` in `rules.py` upward would move
both numbers back toward the 15–20% band usually cited for one-shot retry
campaigns. That hasn't been done, because the honest number here is worth
more than a re-tuned one that happens to look familiar.

### Sensitivity

45 parameter settings — intent decay ±50%, channel response ±20%, payday
penalty from 0.10 to 0.25. **The router wins 45 of 45.** The claim is
directional robustness, not the specific number: all 45 settings perturb the
same oracle, so this shows the result isn't knife-edge, not that the oracle
is right.

---

## What's real, what's modelled

A judge who discovers a hidden simulation stops believing the whole demo, so
here's the line, drawn plainly.

### Real

- The webhook receiver, HMAC-SHA256 signature verification over raw request
  bytes, and rejection + logging of tampered payloads.
- `raw_events`: every webhook stored whole before processing.
- Case creation, deduplication (one checkout = one case), the already-paid
  guard, the decision table, the gate, decision records, the outbox, the
  circuit breaker, idempotency keys, and the resume page.
- The Razorpay SDK integration for orders, payment links, and the Payments
  API. **With API keys in `.env` these are real test-mode API calls** — a
  sent message carries a real link id like `plink_TWi3ogKr1HBRpm`, and a real
  failed payment can be driven through the whole pipeline from the Checkout
  screen. Without keys the client returns clearly-labelled `plink_sim_…`
  links and every screen says "simulated".
- Checkout-callback signature verification, with its own algorithm and
  secret.
- The message templates and the trust rules enforced on them.

### Scripted

- **Customer intent.** Every hidden profile — base intent, payday date,
  channel responsiveness, intent decay, whether a customer would have paid
  unprompted — is generated from a seed. Read them at
  [`fixtures/profiles_seed42.json`](fixtures/profiles_seed42.json).
- **Whether a message converts.** Decided by `would_convert()` in
  `app/simulator.py`, not observed. The recovery-rate table above is the
  output of that function.
- **The synthetic batch.** 200 generated failures with a plausible cause mix,
  not real traffic.

### Partially captured

`fixtures/captured/` holds **9 real `payment.failed` payloads** from a live
test account, delivered over a real webhook. They immediately proved the
risk this section exists to warn about: **the documented reason list isn't
what the API actually sends.**

Real test mode produced `payment_failed` (netbanking, and separately from
Razorpay's own "Insufficient Funds" test card) and
`international_transaction_not_allowed` — none in the original table. All
three escalated to the review queue on first contact. The netbanking case
and the international block are now handled by the two real-capture rules
above. The card case that was supposed to mean insufficient funds is **still
escalating, on purpose** — its `error_source` is `gateway`, not `bank`, so it
doesn't meet the corroboration bar, and nothing in the payload actually says
why the card was declined. Guessing "insufficient funds" from that would be
exactly the kind of unfounded action this project refuses to take. Details
in `fixtures/captured/README.md`.

What's **still** unverified: seven of the eight documented reasons haven't
been reproduced against a live account, `insufficient_fund` among them —
meaning the payday-window rule has never fired from a real payment.
Netbanking and UPI failures always return the generic `payment_failed`; only
specific test cards produce specific reasons, and even a card documented for
one may not produce it. Until each is captured, those rules rest on
documentation rather than observed behaviour.

### How the comparison is kept honest

- **The oracle cannot see the policy.** `would_convert()` takes a profile, an
  `OracleCase`, and an `OracleAction`. None of those dataclasses has a policy
  field, and `tests/test_oracle.py` asserts it structurally rather than
  trusting a comment. Two policies that choose the same action get the same
  outcome.
- **Common random numbers.** `Random(f"{seed}:{case_ref}:{action_index}")`,
  so the same case and action always produce the same result and the two
  runs differ only by their decisions.
- **Self-recoveries are excluded from both policies' recovered totals.** 13
  customers in this batch would have paid unprompted. Without this
  exclusion, the baseline gets credit for every one of them simply because
  it blasts everyone at +5 minutes.
- **Runs are isolated.** Each run gets its own customer rows, so one run's
  contact history can't defer the other's messages.

### Where the model disagrees with our own policy

`payment_cancelled` recovers 27.1% under the baseline and 20.7% under the
router. Both message on email, so the entire gap is the delay: baseline
messages at +5 minutes, the rule deliberately waits six hours because
chasing someone who *deliberately* closed the modal is how merchants get
marked as spam. The oracle's intent-decay penalty scores that wait as pure
loss — it has no model of spam risk or unsubscribes, which is exactly what
the delay guards against. The rule stayed as written, and the disagreement
is reported rather than tuned away to make the simulator agree with us.

---

## The baseline, precisely

"What merchants do today": one email, five minutes after the failure, same
content for everyone, no reading of the failure reason.

The gate's checks **all run for both policies and are all recorded**, but
the baseline only *enforces* opt-out and the run budget — the two every
merchant already has. It doesn't enforce the already-paid guard, the message
cap, or quiet hours, because a naive blast doesn't have them. On a baseline
decision record you can see `case_already_recovered: FAILED — not enforced
by this policy`. The difference is visible in the audit trail, not hidden
in a constant.

---

## Trust design

A bare payment link looks exactly like a scam, because that's what scams
are. Every message links to `/orders/{id}/resume?token=…` on the merchant's
own domain, showing the real cart, the exact original amount, and the
standard Razorpay checkout.

Four rules, enforced in `app/messages.py` and covered by
`tests/test_messages.py`:

1. **Never create urgency.** No countdowns, no expiry, no "your order will
   be cancelled". Urgency is the single most reliable phishing marker.
2. **Never change the amount.** If the original was ₹4,000, the message
   says ₹4,000.
3. **Never ask for information.** No card details, no OTP, no "reply with
   your order number".
4. **Always include what only the real merchant knows.** Order ID, actual
   item names, the time of the attempt, the last four digits of the card.

Every generated message — template or LLM — is validated against all four
before it can be sent.

---

## The agent

**Groq, `openai/gpt-oss-20b` at low reasoning effort**, writes the message
body. That is its entire job. All eight intents come back in 0.4–0.9s, well
inside the 2-second budget.

It doesn't decide who to contact, when, on what channel, or whether to
contact them at all — the decision table does. It returns strict JSON,
schema-validated and then run through the same four trust rules as every
template. It falls back to the hand-written template when the JSON fails to
parse, when it reports its own urgency, when the body carries a rupee amount
other than the case amount, when it drops the merchant link, or when the
call exceeds its budget.

**The autonomy is in the loop, not in the language model.** The agent
perceives (webhooks), decides (the decision table), checks itself (the
gate's seven stopping rules), acts (payment links, messages), observes the
result (capture webhooks), and records its reasoning (decision records).
That loop is the agent; the LLM is one replaceable component inside the
*act* step, and it only ever writes prose. Every action taken on money is
traceable to a rule, with the inputs that triggered it recorded — there is
no "the model decided" anywhere in the audit trail.

Delete `app/llm.py` and the system still runs, fully: without a key it uses
the eight hand-written templates and says so on every screen and every
outbox message. With `GROQ_API_KEY` set, the messages get better prose and
per-customer language (English, Hindi, Hinglish), and the decision record
gains the model's one-line rationale. Nothing else about the system's
behaviour changes.

---

## Real delivery

Off by default. With `DELIVER_FOR_REAL=true` and Gmail configured, a message
that passes the gate is actually sent — the LLM-written body, at the moment
the rule chose.

### Why email only

Every rule sends on email — not a fallback, the only channel this project
delivers on. The original design split channel by cause (SMS for
urgency-driven nudges, WhatsApp for the softer insufficient-funds reminder,
email for the lowest-intent bucket), on the theory that a text converts
better than an email for a "come back and pay" message — which is why the
oracle still scores email lower than SMS and WhatsApp in the Results above.
Real SMS delivery needs DLT registration with a telecom operator, a
commercial process rather than an API key, so that path was cut rather than
faked with a silent reroute at delivery time. Every rule now genuinely sends
what it says it sends.

`app/delivery.py` is the only file that talks to a mail server, and it
**refuses** in six situations, each recorded on the action rather than
swallowed:

1. delivery is switched off
2. the case belongs to a synthetic run — a 200-case comparison must never
   email 200 people
3. the action's channel isn't email — a guard against a future rule
   regressing, since none should ever choose anything else
4. the recipient isn't on `DELIVERY_ALLOWLIST`, when one is set
5. email isn't configured
6. there's no body, or no address

The outbox row is written before any of this, so the audit trail is
identical whether or not a real message left the building — only the
delivery fields differ. `tests/test_delivery.py` covers every refusal.

Gmail setup: enable 2-step verification, create an
[App Password](https://myaccount.google.com/apppasswords), set `SMTP_USER`
and `SMTP_APP_PASSWORD`.

---

## Failure handling

| Failure | Behaviour | Demo |
|---|---|---|
| Razorpay API down | 5 consecutive failures open the circuit breaker for 60s, **scoped per operation** (order / payment_link / fetch). Every money action carries an idempotency key (`live:1:R1_TIMEOUT:1`), so replay can't double-charge. | Simulate → Razorpay API down |
| Payment Links quota hit | Test mode caps Payment Links creation at 30/day, separate from order creation. Creating the link is best-effort and non-blocking (below): the failure is recorded on the action (`payment_link_error`), `razorpay_link_id` stays null, and the message sends anyway. | Genuinely happens; check any action's `payment_link_error` field |
| Budget cap hit | The gate **halts the whole run**, writes a decision record, and waits for a human — it doesn't degrade into sending fewer messages. | `POST /api/runs {"message_budget": 5}` |
| LLM down or malformed | Schema validation fails, the template goes out, the degradation is logged and visible in the outbox. The run completes normally. | Simulate → LLM down |
| Tampered webhook | Rejected with 400 and logged to `raw_events` with `error="invalid signature"`. | Simulate → Send one |
| Real response gives no usable reason | Escalates to the review queue, same as any unmapped reason — see below. | Checkout → fail with netbanking or most test cards |

### Why the payment link doesn't gate the message

`executor.py` still creates a real Razorpay Payment Link for every action as
a record, but it isn't how a customer here actually pays — the resume page
(`/orders/{id}/resume`) opens a live Razorpay checkout against the order
directly, independent of any pre-created link. So when link creation fails,
the message still sends using the resume URL, and the failure is recorded
rather than swallowed or allowed to block real communication.

Razorpay's Payment Links product has its own daily quota in test mode,
separate from order creation. A single shared circuit breaker used to mean
hitting that quota opened the breaker for *every* Razorpay operation,
including creating the next order — a Payment Links problem blocking
something that has nothing to do with Payment Links. The breaker is now
scoped per operation (`app/razorpay_client.py`,
`OPERATIONS = ("order", "payment_link", "fetch")`), and message sending no
longer depends on the payment link at all.

### When the reason Razorpay gives us isn't enough to act on

Real test-mode traffic routinely returns the generic `payment_failed`
catch-all with no `error_source`/`error_step` this project's rules can act
on (see "Two more rules" above, and `fixtures/captured/README.md`). The
pipeline escalates rather than guesses — but it means a live demo checkout
can dead-end on an unclassified case with nothing to show.

`POST /api/cases/{id}/reclassify` lets a **human**, watching the demo,
assign one of the decision table's reasons to an escalated case — a
specific one, or `{"random": true}`. This isn't the pipeline guessing: it's
scoped to already-escalated live cases only, it's a request a person makes
explicitly (the Checkout screen surfaces it right under a case that came
back unclassified), and it writes a `DEMO_RECLASSIFIED` decision record that
says plainly a human assigned this, before the normal rule-based decision
record that follows. This is the human-review authority the build always
allowed for — "ask the LLM to suggest a bucket, but never act on it
automatically" — exercised on demand instead of never exercised at all.

---

## Architecture

```
app/
  clock.py            every time reference in the system, so the demo can move time
  rules.py            the decision table
  policy.py           baseline vs router
  gate.py             seven stopping rules: opt-out, already-paid, cap,
                      quiet hours, frequency, budget halt, discount authority
  ingest.py           webhooks -> cases, dedup, the already-paid guard
  executor.py         gate -> message -> payment link -> outbox
  outbox.py           every message is a row before it is sent
  messages.py         8 templates + the four trust rules
  llm.py              writes the body, nothing else
  simulator.py        synthetic cases, the outcome oracle, the runner
  razorpay_client.py  SDK wrapper, per-operation circuit breaker, chaos toggle
```

**Nothing calls `datetime.now()` except `clock.py`.** `tests/test_clock_lint.py`
greps the codebase and fails the build if anything does — without that, the
demo can't show three days passing in ten seconds.

### API

```
POST /webhooks/razorpay          signature-verified, the real entry point
POST /api/orders                 creates a real test-mode order for checkout
POST /api/verify-payment         checkout callback signature (order_id|payment_id)
POST /api/razorpay/sync          polls the Payments API, feeds failures to the webhook
POST /api/cases/{id}/reclassify  human-only: assign a cause to an escalated live case
POST /api/sim/inject             builds a payload and sends it through that same route
POST /api/clock/advance          {"days": 3} | {"minutes": 30} | {"to_next_action": true}
POST /api/runs                   {"policy": "both", "count": 200, "seed": 42}
POST /api/runs/sweep             the sensitivity sweep
POST /api/chaos                  {"razorpay_down": true} | {"llm_down": true}
GET  /api/cases, /api/cases/{id}, /api/cases/{id}/timeline
GET  /api/outbox, /api/events, /api/review-queue, /api/rules
GET  /orders/{id}/resume         the trust page
```

### Two different signatures, deliberately kept apart

| | Signed payload | Key |
|---|---|---|
| Webhook (`/webhooks/razorpay`) | the **raw request bytes** | `RAZORPAY_WEBHOOK_SECRET` |
| Checkout callback (`/api/verify-payment`) | `order_id + "\|" + payment_id` | `RAZORPAY_KEY_SECRET` |

Using one where the other belongs fails silently forever. `test_pipeline.py`
asserts that a webhook-secret signature is rejected by the checkout endpoint
and that re-serialised JSON fails webhook verification.

### Running a real failed payment

With keys in `.env`, the **Checkout** screen creates a real test-mode order
and opens the real Razorpay modal. In test mode nothing is charged; pick
netbanking or UPI and choose **Failure** on the simulated bank page, or use a
card from Razorpay's
[test card list](https://razorpay.com/docs/payments/payments/test-card-details/).

Razorpay only delivers webhooks to a public URL, so there are two ways to
get that failure into the pipeline:

- **With a tunnel** — `cloudflared tunnel --url http://localhost:8000`, then
  add `https://<tunnel>/webhooks/razorpay` in Dashboard → Settings →
  Webhooks, subscribed to `payment.failed` and `payment.captured`, using the
  same secret as `RAZORPAY_WEBHOOK_SECRET`.
- **Without one** — press **Pull recent payments** on the Checkout screen
  (`POST /api/razorpay/sync`). It polls the Payments API and pushes each
  real failed payment through the same signed webhook route, skipping
  payments already ingested.

`/api/sim/inject` constructs a webhook payload, signs it with the webhook
secret, and posts it to `/webhooks/razorpay`. There is no parallel code
path: the payload is synthetic, the pipeline is identical.

---

## Tests

224 tests, ~5 seconds. The suite is hermetic: it forces simulated mode so a
populated `.env` can never make the tests bill a real account.

- `test_rules.py` — every documented reason maps to a rule; every rule uses
  email; structurally blocked causes always set `suggests_alt_method`; the
  payday window.
- `test_oracle.py` — the oracle cannot see the policy; determinism; the
  modelled behaviours.
- `test_messages.py` — every template against all four trust rules.
- `test_llm.py` — schema validation, fallback on every failure mode, and
  generated text that breaks a trust rule being thrown away for the
  template.
- `test_gate.py` — all seven checks, priority order, baseline enforcement.
- `test_pipeline.py` — signature verification over raw bytes, tampered
  webhooks logged, dedup, the already-paid guard, the resume page,
  reclassification scoped to escalated live cases only, a payment-link
  failure never blocking the message.
- `test_delivery.py` — every delivery refusal, including a non-email
  channel being refused outright rather than rerouted.
- `test_razorpay_breaker.py` — the circuit breaker survives the demo clock
  being advanced, jumped, or reset while it is open; a payment_link failure
  never opens the order breaker; the chaos toggle still takes down every
  operation at once.
- `test_clock_lint.py` — no `datetime.now()` outside `clock.py`.

---

## Known limitations

- `fixtures/captured/` has 9 real payloads covering 3 of the 10 reasons, one
  of which (Razorpay's own "Insufficient Funds" test card) turned out to
  carry no usable signal and correctly still escalates. The other 7 reasons,
  including `insufficient_fund` itself, are unverified against a live test
  account.
- Email is the only channel. WhatsApp and SMS were part of the original
  design (see "Why email only" above) but real SMS delivery needs DLT
  registration this project doesn't have, so the decision table uses email
  throughout.
- The oracle models conversion only — not annoyance, unsubscribes, or brand
  damage. The `payment_cancelled` result above is where that bites.
- The payday window uses the modal day of past payments. A customer paid
  weekly, or on a shifting date, is modelled poorly.
- Single merchant, no auth, no multi-tenancy.
