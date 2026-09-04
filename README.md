# Revive

**Razorpay's Failed Payment Recovery sends every failed payment the same link
at the same time. Revive makes the timing and the content a function of why
the payment failed.**

A failed payment is not one problem. It is eight or ten different problems
wearing the same error screen, and they do not deserve the same response:

- A **network timeout** customer is still holding their phone. Reach them in
  two minutes and they finish the purchase. Reach them in six hours and they
  are gone.
- An **insufficient funds** customer will fail again if you message them
  today. The identical message, sent on their payday, works.
- A **card blocked for online payments** customer cannot succeed on a retry,
  ever. "Try again" is not a weak message for them — it is a structurally
  impossible one. Only "use UPI instead" can convert.

Revive reads the failure reason, picks the moment and the words, checks itself
against seven stopping rules before it acts, and writes down why — for every
single decision.

---

## The shape of the whole system

```
   Razorpay webhook                    (real, signature-verified)
          |
          v
   ingest.py ........... one case per checkout, retries folded in
          |
          v
   rules.py ............ WHY did it fail?  ->  root cause
          |               no match? -> escalate to a human, never guess
          v
   policy.py ........... WHEN to send, WHAT to say  ->  a scheduled Action
          |
          v
   outbox.py ........... the row is written BEFORE anything is sent
          |
          v
   worker.py ........... the scheduler: what is due right now?
          |
          v
   gate.py ............. seven stopping rules -> allow | defer | block
          |                                              | halt  | escalate
          v
   messages.py ......... template, or LLM prose validated against it
          |
          v
   delivery.py ......... real email, behind two safety switches
          |
          v
   the outcome comes back as another webhook -> the loop closes
```

Every arrow writes a row. Nothing happens to a payment outside that trail.

---

## Quick start

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload            # :8000
cd web && npm install && npm run dev     # :5173
```

Open http://localhost:5173 and press **Run both policies**. No API keys are
needed — without them everything runs in simulated mode and says so on every
screen.

> The comparison runs 3,000 synthetic failures through the real pipeline twice,
> once per policy, and takes about **two minutes**. It is a genuine execution of
> every rule, gate check, and outbox write — not a lookup table. Results are
> stored, so the dashboard loads instantly afterwards.

For a **real** failed payment, copy `.env.example` to `.env`, add Razorpay test
keys, restart the API, and use the **Checkout** screen. See
[Running a real failed payment](#running-a-real-failed-payment).

```bash
pytest        # 235 tests, ~5 seconds
```

### Environment variables

Everything has a working default except the Razorpay credentials. The system
starts and the full dashboard works with an empty `.env`.

| Variable | Needed for | Without it |
|---|---|---|
| `GROQ_API_KEY` | LLM-written message bodies. Free key from [console.groq.com/keys](https://console.groq.com/keys) | Hand-written templates, labelled as such in the outbox |
| `LLM_MODEL` | Overriding the model | `openai/gpt-oss-20b` |
| `LLM_REASONING_EFFORT` | Capping a reasoning model's thinking | `low`. Unset for a non-reasoning model — left unbounded, gpt-oss spends its whole token budget thinking and returns an empty body |
| `LLM_TIMEOUT_SECONDS` | The generation budget | `2.0`; anything slower falls back to templates |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | Real test-mode orders and payment links | Simulated links labelled `plink_sim_…`; every screen says "simulated" |
| `RAZORPAY_WEBHOOK_SECRET` | Verifying real webhooks. **The webhook secret from Settings → Webhooks, not the key secret** | Signature verification still runs against a dev default, so the simulator works |
| `RESUME_TOKEN_SECRET` | Signing resume links | A dev default; set any random string before anything real |
| `PUBLIC_BASE_URL` | The domain in message links | `http://localhost:5173`; point at your tunnel for a phone demo |
| `DATABASE_URL` | Another SQLite file | `recovery.db` in the project root |
| `DELIVER_FOR_REAL` | Actually sending email | `false`. Messages render to the outbox only |
| `DELIVERY_ALLOWLIST` | Restricting who can ever be contacted | empty. **Set this while demoing** |
| `MIN_HOURS_BETWEEN_CONTACTS` | Loosening the gate's per-customer contact cap for a demo | `24`, the product default every published result uses. Live test checkouts all share one phone number, so they are one customer and each one after the first is deferred a day; set `0` to demo back-to-back checkouts |
| `SMTP_USER` / `SMTP_APP_PASSWORD` | Real email through Gmail | Email is skipped and the reason recorded |

`.env` is read once at import, so changes need an API restart.

---

## The decision table

The whole product is `app/rules.py`: a plain dict. No model, no inference, no
config file. The `why` column is not a comment — it is rendered in the UI on
every decision the system makes, so an operator always sees the reasoning.

**Every rule sends email.** See [Why email only](#why-email-only).

| Reason | Root cause | Wait | Msgs | Why this timing |
|---|---|---|---|---|
| `payment_timed_out` | transient network | **2 min** | 2 | Nothing was wrong with the customer or the card. They are still in the buying moment; speed matters more than anything else. |
| `card_number_invalid` | data entry | **2 min** | 2 | A typo. High intent, trivially fixable, contact immediately. |
| `authentication_failed` | OTP failure | **15 min** | 2 | The customer tried to pay and the bank step failed. Give the OTP state 15 minutes to settle, then offer UPI as an escape hatch. |
| `gateway_technical_error` | gateway degraded | **45 min** | 2 | The bank or gateway was down. Messaging now sends them straight back into the outage. Wait for it to clear. |
| `insufficient_fund` | balance | **payday** | 2 | The money is not in the account. Contacting them today guarantees a second failure. Wait for their observed payday. Never state the reason — it is embarrassing. |
| `card_disabled_for_online_payments` | card config | **60 min** | 1 | The bank has disabled the card for e-commerce. The same card will fail every time, so the only message that can convert names UPI or a different card. |
| `card_declined` | issuer decline | **120 min** | 1 | The bank refused and did not say why. Low odds, so spend exactly one message and suggest an alternative rather than a repeat. |
| `payment_cancelled` | deliberate abandon | **6 h** | 1 | They closed the modal on purpose — the lowest-intent bucket. One quiet nudge, no urgency, no discount. Chasing someone who deliberately left is how merchants get marked as spam. |

### Two more rules, written from real captures

The eight rules above come from Razorpay's **documented** reason list. The
test-mode API sends a different set. These two came out of an actual capture
session against a live account and are kept visibly separate, because the
difference between documentation and reality is the point:

| Reason | Root cause | Wait | Msgs | Why |
|---|---|---|---|---|
| `payment_failed` *(only when `error_source=bank` at authorisation)* | issuer decline | **120 min** | 1 | Razorpay's catch-all. Treated as a decline **only** when the payload also says a bank rejected it at the authorisation step — which is what a netbanking failure looks like. Anything else carrying this reason is escalated, not guessed at. |
| `international_transaction_not_allowed` | card config | **60 min** | 1 | The card is not permitted for this transaction. Like a disabled card, retrying is structurally impossible; only naming a different method can convert. |

**An unmapped reason is never guessed at.** The case is created with
`root_cause="unknown"`, marked `escalated`, and put in a review queue for a
human. A bare `payment_failed` with no corroborating `error_source` takes that
same path.

---

## Results

Seed 42, **3,000 synthetic failures**, both policies over the identical batch.

### What the system did

Counts of actions actually taken by the pipeline. No behavioural assumptions
are involved in these numbers, which is why they come first.

| Metric | Baseline | Revive | |
|---|---:|---:|---|
| Messages sent | 3,000 | 4,582 | Revive follows up on causes worth a second message |
| **Wrong advice** | **167** | **0** | "try again" sent to a card blocked for online payments |
| Already-paid contacts | 23 | 0 | messages to customers who had already paid |
| Messages suppressed | 0 | 323 | stopped by the gate, each with a recorded reason |
| Escalated to a human | 0 | 0 | no unmapped reasons in this batch |

**167 → 0 on wrong advice is the result we would defend hardest.** It depends
on no model of customer behaviour whatsoever: those 167 cards are disabled for
online payments, and a message saying "try again" cannot work on them. The
baseline sends it anyway, because it never reads the reason.

### Modelled outcome

This table depends on the outcome oracle, so it comes second and is labelled
as modelled, not measured.

| Metric | Baseline | Revive |
|---|---:|---:|
| Amount at risk | ₹75,51,530 | ₹75,51,530 |
| Amount recovered | ₹6,86,020 | ₹9,81,050 |
| Recovery rate | 9.1% | **13.0%** |
| Cases recovered | 280 | 398 |

**+3.9 percentage points — a 43% relative improvement**, and 118 more customers
who completed their purchase.

The gap is not spread evenly, and where it concentrates is the argument for
reading the failure reason at all:

| Root cause | Baseline | Revive | |
|---|---:|---:|---|
| `card_config` (blocked for online payments) | **0.0%** | **15.8%** | the baseline says "try again" to a card that cannot work online, so it recovers nothing at all |
| `gateway_degraded` | 3.2% | 12.4% | the baseline messages during the outage it is reacting to |
| `balance` (insufficient funds) | 1.6% | 4.5% | the baseline messages before payday, guaranteeing a second failure |
| `issuer_decline` | 13.3% | 13.3% | identical — one message either way, so there is nothing to win |

The `card_config` row is the whole thesis in one line: 0% is not the baseline
being unlucky, it is the baseline giving structurally impossible advice.

Two honest notes on that number. First, every message goes out on email, the
channel this oracle scores weakest for a "come back and pay" nudge
(`email_response` draws from 0.14–0.32, against 0.32–0.55 for SMS and
0.45–0.78 for WhatsApp in the same model). Recalibrating `base_conversion` in
`rules.py` upward would lift both policies toward the 15–20% band usually
quoted for one-shot retry campaigns; that has deliberately not been done, so
these are uncalibrated numbers rather than ones re-tuned to look familiar.
Second, the *absolute* rate is a property of the oracle's assumptions; the
*gap between two policies over one identical batch* is the claim being made.

### Sensitivity

45 parameter settings — intent decay ±50%, channel response ±20%, payday
penalty from 0.10 to 0.25. **Revive wins 45 of 45.** All 45 perturb the same
oracle, so this shows the result is not knife-edge, not that the oracle is
right.

---

## How it actually works

This is the part with the engineering in it.

### 1. The clock, and why everything depends on it

A recovery agent is mostly a statement about *time*: wait two minutes, wait
until payday, do not message at 3am. Demonstrating that in a five-minute
review means time has to be something you can move.

So `app/clock.py` owns every time reference in the system. `clock.now()`
returns wall time plus an offset the dashboard can change — advance five
minutes, jump a day, jump directly to whenever the next action is due. The
whole three-day recovery sequence plays out in ten seconds.

This only works if the rule has no exceptions, so it is enforced rather than
documented: **`tests/test_clock_lint.py` greps the codebase and fails the build
if anything outside `clock.py` calls `datetime.now()`.**

There is exactly one deliberate exception, `wall_clock_now()`, used only by the
circuit breaker — and it exists because of a real bug. The breaker's 60-second
backoff originally used `clock.now()`, so advancing the demo clock while the
breaker was open and then resetting it left the breaker computing a "reopens
in" of the entire vanished offset: nineteen hours, for a sixty-second timer. A
timer protecting a real API against real outages must not care what the demo is
pretending the time is.

### 2. The scheduler

Nothing sends immediately. `policy.py` turns a matched rule into a
`scheduled_for` timestamp, and `outbox.py` writes it as a **pending Action row
before anything is sent**. That single ordering rule is what makes the audit
trail, the dashboard, and crash recovery all fall out for free.

Four different things decide *when*:

| Strategy | Used by | How the time is computed |
|---|---|---|
| Fixed delay | most rules | `now + rule.delay_minutes` (2 min … 6 h) |
| **Payday window** | `insufficient_fund` | The modal day-of-month of the customer's past successful payments, next occurrence, 10:30 local. No history → the 2nd of next month. |
| Follow-up | any rule with `max_messages: 2` | first message **+24 h** — deliberately *not* a re-run of the timing strategy, because re-running the payday window would push the second nudge a month out onto a dead cart |
| Deferral | the gate | pushed to the next allowed moment (see below) |

`worker.py` is the loop that runs them. It asks for actions that are `pending`
and `scheduled_for <= now`, and executes each one. It runs two ways, and this
matters:

- **Live**: an async background task polls every 2 seconds, scoped to real
  webhook cases.
- **Demo and simulation**: the clock jump *is* the tick. `POST
  /api/clock/advance` moves time and immediately runs everything that became
  due, so a 3-day sequence resolves in one request.

The simulation runner is event-driven rather than a fixed step: it repeatedly
jumps to whichever comes first — the next due action, or the moment a customer
was going to pay on their own — ticks, resolves the outcome, and repeats. No
time is spent simulating the empty hours in between, which is how 3,000 cases
across a 40-day horizon resolve in about a minute per policy.

### 3. The gate: seven ways to not send

Rules decide what is worth doing. The gate decides whether it is still allowed,
and it runs before **every** action. It has five possible verdicts, and the
distinction between them is most of the design:

| # | Check | Verdict if it fails |
|---|---|---|
| 1 | Customer opted out | **block** — permanent, no override |
| 2 | Case already recovered | **block** — they paid at 8:49; never message them at 8:52 |
| 3 | Message cap for this cause | **block** |
| 4 | Quiet hours (21:00–09:00 IST) | **defer** to 09:00 |
| 5 | Contact frequency (24 h per customer, across all their cases) | **defer** |
| 6 | Run budget exhausted | **halt the entire run** |
| 7 | Discount authority (₹500 / 5% cap) | **escalate to a human** |

Priority order is halt → escalate → block → defer, so a run that is out of
budget stops rather than quietly sending something smaller. A **defer** is not
a cancellation: the action stays pending with a new `scheduled_for`, which is
how a message that comes due at 2am goes out at 9am instead of being lost.

Two properties worth calling out:

- **Every check is recorded whether it passes or fails.** The decision record
  shows what the agent considered, not just what it did.
- **The baseline runs all seven and enforces only two** (opt-out, run budget) —
  the two every merchant already has. So on a baseline decision record you can
  read `case_already_recovered: FAILED — not enforced by this policy`. The
  comparison's asymmetry is visible in the audit trail rather than buried in a
  constant.

### 4. The LLM, and why the agent runs without one

**Groq, `openai/gpt-oss-20b` at low reasoning effort, writes the message body.
That is the entire job.** All eight intents come back in 0.4–0.9s, inside a
2-second budget.

It does **not** decide who to contact, when, on which channel, or whether to
contact at all. Rules decide all of that. The model is handed a fully-decided
action and asked to phrase it.

What comes back is never trusted:

1. It must be strict JSON matching a fixed schema, or it is discarded.
2. It must self-report `mentions_urgency: false` — if it admits urgency, discarded.
3. The body is then run through the same four trust rules as every template:
   no urgency language, no amount other than the order amount, no request for
   information, and it must link to the merchant's own resume page.
4. Anything that fails **any** of those falls back to the hand-written template,
   and the rejection reason is written to the decision record and shown in the UI.

This is not theoretical. During development the model wrote a card-expiry
message mentioning an OTP; the validator caught it, the template went out, and
the reason was visible in the outbox. The safety net is the design, not a
comment about the design.

**Why this still counts as an agent.** The autonomy is in the loop, not in the
language model. The system perceives (webhooks), decides (the decision table),
checks itself (the gate), acts (payment links, messages), observes the result
(capture webhooks), and records its reasoning (decision records). That loop is
the agent. The LLM is one replaceable component inside the *act* step, and it
only ever writes prose.

That is deliberate, because **every action taken on money is traceable to a
rule with the inputs that triggered it recorded.** There is no "the model
decided" anywhere in the audit trail — a merchant cannot accept that, and
neither can a reviewer. Delete `app/llm.py` and the system still runs: without
a key it uses the eight hand-written templates and says so on every screen and
on every message. With a key you get better prose plus per-customer language
(English, Hindi, Hinglish), and the decision record gains the model's one-line
rationale. Nothing else changes.

### 5. Trust design

A bare payment link looks exactly like a scam, because that is what scams are.
Every message links to `/orders/{id}/resume?token=…` on the merchant's own
domain, showing the real cart, the exact original amount, and a standard
Razorpay checkout.

Four rules, enforced in `app/messages.py`, covered by `tests/test_messages.py`,
and applied to **both** templates and LLM output:

1. **Never create urgency.** No countdowns, no expiry, no "your order will be
   cancelled". Urgency is the single most reliable phishing marker.
2. **Never change the amount.** If the original was ₹4,000, the message says
   ₹4,000.
3. **Never ask for information.** No card details, no OTP, no "reply with your
   order number" — the words themselves are banned, because a real merchant
   does not put them in a message.
4. **Always include what only the real merchant knows.** Order ID, real item
   names, the time of the attempt, the last four digits of the card.

### 6. Money safety

- **Idempotency keys** on every money action (`live:1:R1_TIMEOUT:1:a1`),
  including the attempt number — because a customer failing twice in one
  checkout is the most ordinary thing in the world, and without it the second
  plan collides with the first on a unique constraint.
- **A circuit breaker** around Razorpay, scoped **per operation**
  (order / payment_link / fetch). Five consecutive failures open it for 60
  seconds. Per-operation scoping came from a real incident: Payment Links has
  its own daily cap in test mode, and a single shared breaker meant hitting
  that cap blocked new order creation too — a Payment Links problem taking down
  something unrelated to Payment Links.
- **The outbox row is always written first**, so the audit trail is identical
  whether or not a real message left the building. Only the delivery fields
  differ.

---

## What is real, and what is modelled

A reviewer who discovers a hidden simulation stops believing the whole demo, so
here is the line, drawn plainly.

### Real

- The webhook receiver, HMAC-SHA256 verification over the **raw request bytes**,
  and rejection + logging of tampered payloads.
- `raw_events`: every webhook stored whole, before processing.
- Case creation, deduplication, the already-paid guard, the decision table, the
  gate, decision records, the outbox, the scheduler, the circuit breaker,
  idempotency keys, and the resume page.
- The Razorpay integration for orders, payment links, and the Payments API.
  **With keys in `.env` these are real test-mode API calls** — a sent message
  carries a real link id like `plink_TWi3ogKr1HBRpm`, and a real failed payment
  can be driven through the entire pipeline from the Checkout screen.
- Checkout-callback signature verification, which uses a different algorithm and
  a different secret from the webhook (see below).
- Real email delivery over SMTP, behind two safety switches.

### Modelled

- **Customer intent.** Every hidden profile — base intent, payday, channel
  responsiveness, intent decay, whether they would have paid unprompted — is
  generated from a seed. Read them in
  [`fixtures/profiles_seed42.json`](fixtures/profiles_seed42.json).
- **Whether a message converts.** Decided by `would_convert()` in
  `app/simulator.py`. The recovery-rate table is the output of that function.
- **The batch itself.** 3,000 generated failures with a plausible cause mix, not
  real traffic.

### Partially captured

`fixtures/captured/` holds **9 real `payment.failed` payloads** from a live test
account, delivered over a real webhook. They immediately proved the risk this
section exists to flag: **the documented reason list is not what the API sends.**

Real test mode produced `payment_failed` (netbanking, and separately from
Razorpay's own "Insufficient Funds" test card) and
`international_transaction_not_allowed` — none of which were in the original
table. All three escalated on first contact. Two are now handled by the
real-capture rules. The third — the card that was supposed to mean insufficient
funds — **still escalates, deliberately**: its `error_source` is `gateway`, not
`bank`, and nothing in the payload actually says why the card was declined.
Guessing "insufficient funds" from that is exactly the unfounded action this
project refuses to take.

Still unverified: seven of the eight documented reasons have not been reproduced
against a live account, `insufficient_fund` among them — so the payday rule has
never fired from a real payment. Until each is captured, those rules rest on
documentation rather than observation.

### How the comparison is kept honest

- **The oracle cannot see the policy.** `would_convert()` takes a profile, an
  `OracleCase`, and an `OracleAction`. None of those has a policy field, and
  `tests/test_oracle.py` asserts that *structurally* rather than trusting a
  comment. Two policies choosing the same action get the same outcome.
- **Common random numbers.** `Random(f"{seed}:{case_ref}:{action_index}")`, so
  the two runs differ only by their decisions.
- **Self-recoveries are excluded from both policies' totals.** ~147 customers in
  this batch would have paid unprompted. Without excluding them the baseline
  gets credit for every one, purely because it blasts everyone at +5 minutes.
- **Runs are isolated.** Each run gets its own customer rows, so one run's
  contact history cannot defer the other's messages.

### Where the model disagrees with our own policy

`payment_cancelled` recovers **17.1% under the baseline against 16.6% under
Revive**. Both use email, so the whole gap is the delay: the baseline messages at +5 minutes,
the rule deliberately waits six hours because chasing someone who *deliberately*
closed the modal is how merchants get marked as spam. The oracle scores that
wait as pure loss — it models conversion only, and has no concept of spam
complaints or unsubscribes, which is the exact thing the delay protects. The
rule stayed as written and the disagreement is reported rather than tuned away.

---

## Real delivery

Off by default. With `DELIVER_FOR_REAL=true` and Gmail configured, a message
that passes the gate is genuinely sent — the LLM-written body, at the moment
the rule chose.

### Why email only

Every rule sends email. It is not a fallback, it is the only channel this
project delivers on.

The original design split channel by cause: SMS for urgency-driven nudges,
WhatsApp for the softer insufficient-funds reminder, email for the lowest-intent
bucket — on the theory that a text converts better than an email for "come back
and pay". That theory is still visible in the oracle, which scores email well
below SMS and WhatsApp, and it is why the headline recovery rate is lower than a
multi-channel design would model.

Real SMS delivery needs DLT registration with an Indian telecom operator — a
commercial process, not an API key. Rather than keep rules that *claimed* SMS
and then silently rerouted to email at delivery time, the channel was removed
from the rules. **Every rule now genuinely sends what it says it sends**, and
`app/delivery.py` refuses any non-email channel outright rather than quietly
substituting one.

`delivery.py` is the only file that talks to a mail server, and it **refuses**
in six situations, each recorded on the action rather than swallowed:

1. delivery is switched off
2. the case belongs to a synthetic run — a 3,000-case comparison must never
   email 3,000 people
3. the channel is not email
4. the recipient is not on `DELIVERY_ALLOWLIST`, when one is set
5. email is not configured
6. there is no body, or no address

Gmail setup: enable 2-step verification, create an
[App Password](https://myaccount.google.com/apppasswords), set `SMTP_USER` and
`SMTP_APP_PASSWORD`.

---

## Failure handling

| Failure | Behaviour | How to demo it |
|---|---|---|
| Razorpay API down | 5 consecutive failures open the circuit breaker for 60s, scoped per operation. Every money action carries an idempotency key, so replay cannot double-charge. | Simulate → Razorpay API down |
| Payment Links quota hit | Link creation is best-effort and non-blocking: the failure is recorded on the action (`payment_link_error`) and the message sends anyway. | Happens for real; check any action's `payment_link_error` |
| Budget cap hit | The gate **halts the run**, writes a decision record, and waits for a human. It does not degrade into sending fewer messages. | `POST /api/runs {"message_budget": 5}` |
| LLM down, slow, or malformed | Validation fails, the template goes out, the degradation is logged and visible in the outbox. The run completes normally. | Simulate → LLM down |
| Tampered webhook | Rejected with 400 and logged to `raw_events` with `error="invalid signature"`. | Simulate → Send one |
| Reason we cannot act on | Escalates to the review queue rather than guessing. | Checkout → fail with netbanking |

### Why the payment link does not gate the message

`executor.py` still creates a real Payment Link per action as a record, but it
is not how a customer here actually pays — the resume page opens Razorpay
checkout against the order directly. So when link creation fails, the message
still goes out with the resume URL and the failure is recorded rather than
blocking customer communication.

### When Razorpay gives us nothing to act on

Real test traffic routinely returns the generic `payment_failed` catch-all with
nothing the rules can read, and the pipeline correctly escalates instead of
guessing — which means a live demo can dead-end with nothing to show.

`POST /api/cases/{id}/reclassify` lets a **human** assign a cause to an
already-escalated live case (a specific one, or `{"random": true}`), from the
Checkout screen. This is not the pipeline guessing: it is scoped to escalated
live cases only, a person triggers it explicitly, and it writes a
`DEMO_RECLASSIFIED` decision record stating plainly that a human assigned it,
*before* the normal rule-based record that follows.

---

## Architecture

```
app/
  clock.py            every time reference in the system
  rules.py            the decision table
  policy.py           baseline vs Revive: when and what
  gate.py             the seven stopping rules
  ingest.py           webhooks -> cases, dedup, the already-paid guard
  worker.py           the scheduler loop
  executor.py         gate -> message -> payment link -> outbox -> delivery
  outbox.py           every message is a row before it is sent
  messages.py         8 templates + the four trust rules
  llm.py              writes the body, nothing else
  delivery.py         the only file that talks to a mail server
  simulator.py        synthetic cases, the outcome oracle, the runner
  razorpay_client.py  SDK wrapper, per-operation circuit breaker, chaos toggle
```

### API

```
POST /webhooks/razorpay          signature-verified, the real entry point
POST /api/orders                 creates a real test-mode order for checkout
POST /api/verify-payment         checkout callback signature (order_id|payment_id)
POST /api/razorpay/sync          polls the Payments API, feeds failures to the webhook
POST /api/cases/{id}/reclassify  human-only: assign a cause to an escalated live case
POST /api/sim/inject             builds a payload and sends it through that same route
POST /api/clock/advance          {"days": 3} | {"minutes": 30} | {"to_next_action": true}
POST /api/runs                   {"policy": "both", "count": 3000, "seed": 42}
POST /api/runs/sweep             the sensitivity sweep
POST /api/chaos                  {"razorpay_down": true} | {"llm_down": true}
POST /api/demo/reset             wipe everything and put the clock back
GET  /api/cases, /api/cases/{id}, /api/cases/{id}/timeline
GET  /api/outbox, /api/events, /api/review-queue, /api/rules
GET  /orders/{id}/resume         the trust page
```

### Two signatures, deliberately kept apart

| | Signed payload | Key |
|---|---|---|
| Webhook (`/webhooks/razorpay`) | the **raw request bytes** | `RAZORPAY_WEBHOOK_SECRET` |
| Checkout callback (`/api/verify-payment`) | `order_id + "\|" + payment_id` | `RAZORPAY_KEY_SECRET` |

Using one where the other belongs fails silently forever, so `test_pipeline.py`
asserts that a webhook-secret signature is rejected by the checkout endpoint,
and that re-serialised JSON fails webhook verification.

### Running a real failed payment

With keys in `.env`, the **Checkout** screen creates a real test-mode order and
opens the real Razorpay modal. Nothing is charged in test mode: pick netbanking
or UPI and choose **Failure** on the simulated bank page, or use a card from
Razorpay's [test card list](https://razorpay.com/docs/payments/payments/test-card-details/).

Razorpay only delivers webhooks to a public URL, so there are two ways in:

- **With a tunnel** — `cloudflared tunnel --url http://localhost:8000`, then add
  `https://<tunnel>/webhooks/razorpay` in Dashboard → Settings → Webhooks,
  subscribed to `payment.failed` and `payment.captured`, using the same secret
  as `RAZORPAY_WEBHOOK_SECRET`.
- **Without one** — press **Pull recent payments** on the Checkout screen. It
  polls the Payments API and pushes each real failed payment through the same
  signed webhook route, skipping ones already ingested.

`/api/sim/inject` builds a payload, signs it with the webhook secret, and posts
it to `/webhooks/razorpay`. There is no parallel code path: the payload is
synthetic, the pipeline is identical.

---

## Tests

235 tests, ~5 seconds. The suite is hermetic: it forces simulated mode, so a
populated `.env` can never make the tests bill a real account.

| File | Covers |
|---|---|
| `test_rules.py` | every documented reason maps to a rule; every rule uses email; structurally blocked causes always set `suggests_alt_method`; the payday window |
| `test_oracle.py` | the oracle cannot see the policy; determinism; the modelled behaviours |
| `test_messages.py` | every template against all four trust rules |
| `test_llm.py` | schema validation, fallback on every failure mode, and generated text that breaks a trust rule being thrown away |
| `test_gate.py` | all seven checks, priority order, baseline enforcement |
| `test_pipeline.py` | signature verification over raw bytes, tampered webhooks logged, dedup, the already-paid guard, the resume page, the review queue, reclassification scoped to escalated live cases, a payment-link failure never blocking a message |
| `test_delivery.py` | every delivery refusal, including a non-email channel refused outright rather than rerouted |
| `test_razorpay_breaker.py` | the breaker survives the demo clock being advanced, jumped, or reset while open; a payment_link failure never opens the order breaker |
| `test_clock_lint.py` | no `datetime.now()` outside `clock.py` |

---

## Known limitations

- `fixtures/captured/` covers 3 of the 10 reasons. The other 7 — including
  `insufficient_fund`, and therefore the payday window — are unverified against
  a live account.
- Email is the only channel; real SMS needs DLT registration this project does
  not have.
- The oracle models conversion only — not annoyance, unsubscribes, or brand
  damage. The `payment_cancelled` result is where that bites.
- The payday window uses the modal day of past payments, so a customer paid
  weekly or on a shifting date is modelled poorly.
- Single merchant, no auth, no multi-tenancy.
- The 3,000-case comparison takes about two minutes and runs synchronously.
  Results are stored, so it only needs running once — but a fresh clone starts
  with an empty database and must run it before the dashboard shows anything.
