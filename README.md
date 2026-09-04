# Revive

**Razorpay's Failed Payment Recovery sends every failed payment the same link
at the same time. Revive makes the timing and the content a function of why
the payment failed.**

A failed payment is not one problem. It is ten different problems wearing the
same error screen, and they do not deserve the same response:

- A **network timeout** customer is still holding their phone. Reach them in
  two minutes and they finish the purchase. Reach them in six hours and they
  are gone.
- An **insufficient funds** customer will fail again if you message them
  today. The identical message, sent on their payday, works.
- A **card blocked for online payments** customer cannot succeed on a retry,
  ever. "Try again" is not a weak message for them — it is a structurally
  impossible one. Only "use UPI instead" can convert.

Revive reads the failure reason, picks the moment and the words, checks itself
against six stopping rules before acting, and records why for every decision.

---

## The shape of the system

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
   policy.py ........... WHEN to send, WHAT to say  ->  a scheduled action
          |
          v
   outbox.py ........... the row is written BEFORE anything is sent
          |
          v
   worker.py ........... the scheduler: what is due right now?
          |
          v
   gate.py ............. six stopping rules  -> allow | defer | block
          |                                            | halt  | escalate
          v
   messages.py ......... template, or LLM prose validated against it
          |
          v
   delivery.py ......... real email, behind two safety switches
          |
          v
   the outcome returns as another webhook -> the loop closes
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
needed; without them everything runs in simulated mode and says so on every
screen.

> The comparison drives **3,000 synthetic failures** through the real pipeline
> twice, once per policy, and takes about **two minutes**. It is a genuine
> execution of every rule, gate check, and outbox write, not a lookup table.
> Results are stored, so the dashboard loads instantly afterwards.

For a **real** failed payment, copy `.env.example` to `.env`, add Razorpay test
keys, restart the API, and use the **Checkout** screen.

```bash
pytest        # 263 tests, ~8 seconds
```

### Environment variables

Everything has a working default except the Razorpay credentials. The system
starts and the full dashboard works with an empty `.env`.

| Variable | Needed for | Without it |
|---|---|---|
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | Real test-mode orders and payment links | Simulated links labelled `plink_sim_…`; every screen says "simulated" |
| `RAZORPAY_WEBHOOK_SECRET` | Verifying real webhooks. **The webhook secret from Settings → Webhooks, not the key secret** | Verification still runs against a dev default, so the simulator works |
| `GROQ_API_KEY` | LLM-written message bodies. Free key from [console.groq.com/keys](https://console.groq.com/keys) | Hand-written templates, labelled as such in the outbox |
| `RESUME_TOKEN_SECRET` | Signing resume links | A dev default; set any random string before anything real |
| `PUBLIC_BASE_URL` | The domain in message links | Falls back to `RENDER_EXTERNAL_URL` when deployed, then `http://localhost:5173`. **A message linking to localhost is useless to whoever gets it** |
| `BREVO_API_KEY` / `EMAIL_FROM_ADDRESS` | Sending email at all. Free tier at [brevo.com](https://www.brevo.com), 300/day | Email is skipped and the reason recorded |
| `EMAIL_FROM_NAME` | The display name on the message | `Blue Tokai Coffee` |
| `CORS_ORIGINS` | A dashboard served from another origin | localhost only |
| `DATABASE_URL` | Another SQLite file | `recovery.db` in the project root |
| `DELIVER_FOR_REAL` | Actually sending email | `false`. Messages render to the outbox only |
| `DELIVERY_ALLOWLIST` | Restricting who can ever be contacted | empty. **Set this while demoing** |
| `DEMO_SEED_COUNT` | Cases generated at startup when the database is empty | `200`. A deployed instance boots empty, so without this the Cases, Outbox and Audit screens are blank until someone runs a comparison. `0` disables it |
| `MIN_HOURS_BETWEEN_CONTACTS` | Loosening the per-customer contact cap for a demo | `24`, the product default every published result assumes. Live test checkouts share one phone number, so they are one customer and each after the first is deferred a day; set `0` to demo back-to-back checkouts |
| `LLM_MODEL` / `LLM_REASONING_EFFORT` / `LLM_TIMEOUT_SECONDS` | Overriding the model, its thinking budget, its deadline | `openai/gpt-oss-20b`, `low`, `2.0` |

`.env` is read once at import, so changes need an API restart.

---

## The decision table

The whole product is `app/rules.py`: a plain dict. No model, no inference, no
config file. The `why` column is not a comment — it is rendered in the UI on
every decision the system makes.

**Every rule sends email.** The build originally split channels by cause, but
real SMS in India needs DLT registration with a telecom operator, a commercial
process rather than an API key. Rather than keep rules that claimed SMS and
silently rerouted to email, the channel was removed. Every rule now sends what
it says it sends.

| Reason | Root cause | Wait | Msgs | Why this timing |
|---|---|---|---|---|
| `payment_timed_out` | transient network | **2 min** | 2 | Nothing was wrong with the customer or the card. They are still in the buying moment; speed matters more than anything else. |
| `card_number_invalid` | data entry | **2 min** | 2 | A typo. High intent, trivially fixable, contact immediately. |
| `authentication_failed` | OTP failure | **15 min** | 2 | The customer tried to pay and the bank step failed. Give the OTP state 15 minutes to settle, then offer UPI as an escape hatch. |
| `gateway_technical_error` | gateway degraded | **45 min** | 2 | The bank or gateway was down. Messaging now sends them straight back into the outage. |
| `insufficient_fund` | balance | **payday** | 2 | The money is not in the account. Contacting them today guarantees a second failure. Wait for their observed payday. Never state the reason — it is embarrassing. |
| `card_disabled_for_online_payments` | card config | **60 min** | 1 | The bank has disabled the card for e-commerce. The same card fails every time, so the only message that can convert names UPI or a different card. |
| `card_declined` | issuer decline | **120 min** | 1 | The bank refused and did not say why. Low odds, so spend one message and suggest an alternative rather than a repeat. |
| `payment_cancelled` | deliberate abandon | **6 h** | 1 | They closed the modal on purpose, the lowest-intent bucket. One quiet nudge. Chasing someone who deliberately left is how merchants get marked as spam. |

### Two more rules, written from real captures

The eight rules above come from Razorpay's **documented** reason list. The
test-mode API sends a different set. These two came out of an actual capture
session against a live account, and are kept visibly separate because the gap
between documentation and reality is the point:

| Reason | Root cause | Wait | Msgs | Why |
|---|---|---|---|---|
| `payment_failed` *(only when `error_source=bank` at authorisation)* | issuer decline | **120 min** | 1 | Razorpay's catch-all. Treated as a decline **only** when the payload also says a bank rejected it at the authorisation step. Anything else carrying this reason is escalated, not guessed at. |
| `international_transaction_not_allowed` | card config | **60 min** | 1 | The card is not permitted for this transaction. Like a disabled card, retrying is structurally impossible. |

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

**167 → 0 on wrong advice is the result we would defend hardest.** It depends
on no model of customer behaviour: those 167 cards are disabled for online
payments, and a message saying "try again" cannot work on them. The baseline
sends it anyway, because it never reads the reason.

### Modelled outcome

This depends on the outcome oracle, so it comes second and is labelled as
modelled, not measured.

| Metric | Baseline | Revive |
|---|---:|---:|
| Amount at risk | ₹75,51,530 | ₹75,51,530 |
| Amount recovered | ₹6,86,020 | ₹9,81,050 |
| Recovery rate | 9.1% | **13.0%** |
| Cases recovered | 280 | 398 |

**+3.9 percentage points, a 43% relative improvement**, and 118 more customers
who completed their purchase.

The gap is not spread evenly, and where it concentrates is the argument for
reading the failure reason at all:

| Root cause | Baseline | Revive | |
|---|---:|---:|---|
| `card_config` (blocked online) | **0.0%** | **15.8%** | the baseline gives structurally impossible advice, so it recovers nothing |
| `gateway_degraded` | 3.2% | 12.4% | the baseline messages during the outage it is reacting to |
| `balance` (insufficient funds) | 1.6% | 4.5% | the baseline messages before payday, guaranteeing a second failure |
| `deliberate_abandon` | **17.1%** | 16.6% | the one cause where the baseline wins — see below |
| `issuer_decline` | 13.3% | 13.3% | identical: one message either way, nothing to win |

Two honest notes. First, the absolute rate is a property of the oracle's
assumptions. Every message goes out on email, the channel it scores weakest,
and `base_conversion` has deliberately not been recalibrated upward to reach
the 15–20% band usually quoted for retry campaigns. The claim being made is the
*gap between two policies over one identical batch*, not the absolute number.

Second, `deliberate_abandon` is a case where the model disagrees with our own
rule. Both policies use email, so the whole gap is the delay: the baseline
messages at +5 minutes, the rule deliberately waits six hours because chasing
someone who *deliberately* closed the modal is how merchants get marked as
spam. The oracle scores that wait as pure loss, because it models conversion
only and has no concept of spam complaints. The rule stayed as written and the
disagreement is reported rather than tuned away.

### Sensitivity

45 parameter settings: intent decay ±50%, channel response ±20%, payday penalty
from 0.10 to 0.25. **Revive wins 45 of 45.** All 45 perturb the same oracle, so
this shows the result is not knife-edge, not that the oracle is right.

---

## How it works

### The clock

A recovery agent is mostly a statement about *time*: wait two minutes, wait
until payday, never message at 3am. Demonstrating that in a five-minute review
means time has to be movable.

`app/clock.py` owns every time reference in the system. `clock.now()` returns
wall time plus an offset the dashboard can change: advance five minutes, jump a
day, or jump straight to whenever the next action is due, so a three-day
sequence plays out in ten seconds. The rule has no exceptions, and it is
enforced rather than documented — **`tests/test_clock_lint.py` greps the
codebase and fails the build if anything outside `clock.py` calls
`datetime.now()`.**

The one deliberate exception is `wall_clock_now()`, used only by the circuit
breaker: a 60-second backoff protecting a real API must not care what the demo
is pretending the time is.

### The scheduler

Nothing sends immediately. `policy.py` turns a matched rule into a
`scheduled_for` timestamp, and `outbox.py` writes it as a **pending row before
anything is sent**, which is what makes the audit trail, the dashboard, and
crash recovery fall out for free.

Four things decide *when*:

| Strategy | Used by | How the time is computed |
|---|---|---|
| Fixed delay | most rules | `now + rule.delay_minutes` (2 min to 6 h) |
| **Payday window** | `insufficient_fund` | the modal day-of-month of the customer's past successful payments, next occurrence, 10:30 local. No history means the 2nd of next month |
| Follow-up | rules allowing 2 messages | first message **+24 h**, deliberately not a re-run of the timing strategy, because re-running the payday window would push the second nudge a month out onto a dead cart |
| Deferral | the gate | pushed to the next allowed moment |

`worker.py` runs them, asking for actions that are `pending` and
`scheduled_for <= now`. It works two ways: live, an async task polls every 2
seconds; in the demo, the clock jump *is* the tick, so `POST
/api/clock/advance` moves time and immediately runs whatever became due.

The simulation runner is event-driven rather than stepped. It jumps to
whichever comes first — the next due action, or the moment a customer would
have paid unprompted — ticks, resolves the outcome, and repeats. No time is
spent on the empty hours between, which is how 3,000 cases across a 40-day
horizon resolve in about a minute per policy.

### The gate: six ways to not send

Rules decide what is worth doing; the gate decides whether it is still allowed,
and runs before **every** action.

| # | Check | Verdict if it fails |
|---|---|---|
| 1 | Customer opted out | **block** — permanent, no override |
| 2 | Case already recovered | **block** — they paid at 8:49; never message at 8:52 |
| 3 | Message cap for this cause | **block** |
| 4 | Contact frequency (24 h per customer, across all their cases) | **defer** |
| 5 | Run budget exhausted | **halt the entire run** |
| 6 | Discount authority (₹500 / 5% cap) | **escalate to a human** |

There is deliberately **no quiet-hours rule**. One was here while the decision
table still chose SMS and WhatsApp, where a message at 3am wakes somebody up.
Email does not ring — it waits in an inbox until it is read — so the same
restriction buys nothing and costs a lot: it could defer a `payment_timed_out`
message, whose entire value is arriving within two minutes, by twelve hours.
The frequency cap is what protects a customer from being pestered. (It barely
showed up in the published run either way: 2 of 4,582 messages fell in the old
window, which is why removing it moved none of the numbers above.)

Priority is halt → escalate → block → defer, so a run that is out of budget
stops rather than quietly sending something smaller. A **defer** is not a
cancellation: the action stays pending with a new time, which is how a message
that comes due at 2am goes out at 9am instead of being lost.

Two properties worth calling out. **Every check is recorded whether it passes
or fails**, so the decision record shows what the agent considered, not just
what it did. And **the baseline runs all six but enforces only two**
(opt-out, run budget) — the two every merchant already has — so a baseline
record reads `case_already_recovered: FAILED — not enforced by this policy`.
The comparison's asymmetry is visible in the audit trail rather than buried in
a constant.

### The LLM

**Groq, `openai/gpt-oss-20b`, writes the message body.** That is its entire
job. It does not decide who to contact, when, or whether to contact at all —
rules decide that, and the model is handed a fully-decided action and asked to
phrase it.

What comes back is never trusted. It must be strict JSON matching a fixed
schema, it must self-report that it used no urgency, and the body is then run
through the same four trust rules as every template. Anything failing any check
falls back to the hand-written template, with the rejection reason recorded on
the decision record and shown in the UI.

**The system runs fully without an API key**, using the eight hand-written
templates and saying so on every screen. Every action taken on money is
traceable to a rule with its inputs recorded; there is no "the model decided"
anywhere in the audit trail.

### Trust design

A bare payment link looks exactly like a scam, because that is what scams are.
Every message links to `/orders/{id}/resume?token=…` on the merchant's own
domain, showing the real cart, the exact original amount, and a standard
Razorpay checkout.

Four rules, enforced in `app/messages.py`, applied to **both** templates and
LLM output:

1. **Never create urgency.** No countdowns, no expiry, no "your order will be
   cancelled". Urgency is the single most reliable phishing marker.
2. **Never change the amount.** If the original was ₹4,000, the message says
   ₹4,000.
3. **Never ask for information.** No card details, no OTP — the words
   themselves are banned, because a real merchant does not put them in a
   message.
4. **Always include what only the real merchant knows.** Order ID, real item
   names, the time of the attempt, the last four of the card.

### Money safety

- **Idempotency keys** on every money action (`live:1:R1_TIMEOUT:1:a1`),
  including the attempt number. A customer failing twice in one checkout is the
  most ordinary thing there is, and without it the second plan collides with
  the first.
- **A circuit breaker** around Razorpay, scoped **per operation** (order /
  payment_link / fetch). Five consecutive failures open it for 60 seconds.
  Per-operation scoping came from a real incident: Payment Links has its own
  daily cap in test mode, and one shared breaker meant hitting that cap blocked
  new order creation too.
- **The outbox row is always written first**, so the audit trail is identical
  whether or not a message left the building. Only the delivery fields differ.

---

## What is real, and what is modelled

A reviewer who discovers a hidden simulation stops believing the whole demo, so
here is the line, drawn plainly.

**Real.** The webhook receiver, HMAC-SHA256 verification over the **raw request
bytes**, and rejection plus logging of tampered payloads. Every webhook stored
whole before processing. Case creation, deduplication, the already-paid guard,
the decision table, the gate, decision records, the outbox, the scheduler, the
circuit breaker, idempotency keys, and the resume page. The Razorpay
integration for orders, payment links, and the Payments API: with keys in
`.env` these are real test-mode API calls, and a real failed payment can be
driven through the entire pipeline from the Checkout screen. Checkout-callback
signature verification, which uses a different algorithm and secret from the
webhook. Real email over SMTP, behind two safety switches.

**Modelled.** Customer intent: every hidden profile (base intent, payday,
responsiveness, decay, whether they would have paid unprompted) is generated
from a seed and readable in
[`fixtures/profiles_seed42_n3000.json`](fixtures/profiles_seed42_n3000.json). Whether a
message converts, decided by `would_convert()` in `app/simulator.py`. And the
batch itself: 3,000 generated failures with a plausible cause mix, not real
traffic.

**Partially captured.** `fixtures/captured/` holds **9 real `payment.failed`
payloads** from a live test account. They immediately proved the risk this
section exists to flag: the documented reason list is not what the API sends.
Real test mode produced `payment_failed` and
`international_transaction_not_allowed`, neither of which was in the original
table, and all escalated on first contact. Two are now handled by the
real-capture rules. The third — the card that was supposed to mean insufficient
funds — **still escalates, deliberately**: its `error_source` is `gateway`, not
`bank`, and nothing in the payload says why the card was declined.

Still unverified: seven of the eight documented reasons have not been
reproduced against a live account, `insufficient_fund` among them, so the
payday rule has never fired from a real payment.

### How the comparison is kept honest

- **The oracle cannot see the policy.** `would_convert()` takes a profile, an
  `OracleCase`, and an `OracleAction`. None has a policy field, and
  `tests/test_oracle.py` asserts that *structurally* rather than trusting a
  comment.
- **Common random numbers.** `Random(f"{seed}:{case_ref}:{action_index}")`, so
  the two runs differ only by their decisions.
- **Self-recoveries are excluded from both totals.** About 147 customers would
  have paid unprompted; without excluding them the baseline gets credit for
  every one, purely because it blasts everyone at +5 minutes.
- **Runs are isolated.** Each gets its own customer rows, so one run's contact
  history cannot defer the other's messages.

---

## Failure handling

| Failure | Behaviour | How to demo it |
|---|---|---|
| Razorpay API down | 5 consecutive failures open the breaker for 60s, per operation. Idempotency keys mean replay cannot double-charge. | Simulate → Razorpay API down |
| Payment Links quota hit | Link creation is best-effort: the failure is recorded on the action (`payment_link_error`) and the message sends anyway, using the resume URL it never depended on the link for. | Happens for real; check `payment_link_error` |
| Budget cap hit | The gate **halts the run**, writes a decision record, and waits for a human. It does not degrade into sending fewer messages. | `POST /api/runs {"message_budget": 5}` |
| LLM down, slow, or malformed | Validation fails, the template goes out, the degradation is logged and visible in the outbox. | Simulate → LLM down |
| Tampered webhook | Rejected with 400 and logged with `error="invalid signature"`. | Simulate → Send one |
| Reason we cannot act on | Escalates to the review queue rather than guessing. | Checkout → fail with netbanking |

Real test traffic routinely returns the generic `payment_failed` with nothing
the rules can read, which means a live demo can dead-end with nothing to show.
`POST /api/cases/{id}/reclassify` lets a **human** assign a cause to an
already-escalated live case from the Checkout screen. This is not the pipeline
guessing: it is scoped to escalated live cases, a person triggers it, and it
writes a `DEMO_RECLASSIFIED` record stating plainly that a human assigned it,
before the normal rule-based record.

---

## Architecture

```
app/
  clock.py            every time reference in the system
  rules.py            the decision table
  policy.py           baseline vs Revive: when and what
  gate.py             the six stopping rules
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
POST /api/runs                   {"policy": "both", "count": 3000, "seed": 42}. Returns 202
                                 immediately; the run happens on a background thread
GET  /api/runs/status            progress of the running comparison or sweep
POST /api/runs/sweep             the sensitivity sweep, backgrounded the same way
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
asserts a webhook-secret signature is rejected by the checkout endpoint, and
that re-serialised JSON fails webhook verification.

### Running a real failed payment locally

With keys in `.env`, the **Checkout** screen creates a real test-mode order and
opens the real Razorpay modal. Nothing is charged in test mode: pick netbanking
or UPI and choose **Failure** on the simulated bank page, or use a card from
Razorpay's [test card list](https://razorpay.com/docs/payments/payments/test-card-details/).

Razorpay only delivers webhooks to a public URL, so locally there are two ways
in: a tunnel (`cloudflared tunnel --url http://localhost:8000`, then point
Dashboard → Settings → Webhooks at `https://<tunnel>/webhooks/razorpay`), or
press **Pull recent payments** on the Checkout screen, which polls the Payments
API and pushes each real failure through the same signed webhook route.

---

## Deploying

Repository: [github.com/Hero0p/Revive](https://github.com/Hero0p/Revive).
The API goes to Render, the dashboard to Vercel, and Vercel proxies `/api` and
`/orders` back to Render so the browser sees a single origin — which keeps the
resume link on the same domain the customer was shopping on.

**Read this first.** The app runs on SQLite, and Render's filesystem is
ephemeral unless you attach a paid persistent disk. **Every deploy and every
restart starts with an empty database**, so the 3,000-case comparison has to be
re-run from the dashboard afterwards, and live checkout cases do not survive a
restart. On the free instance the run is also slower than the roughly two
minutes it takes locally, and the service spins down after about 15 minutes of
inactivity — while it is asleep the scheduler is not running, so nothing is
sent until the next request wakes it. That is fine for a demo and not fine for
anything real; the fix is a persistent disk or Postgres.

### 1. The API on Render

The repo ships a [`render.yaml`](render.yaml) blueprint, so **New → Blueprint →
select the repo** picks up the build and start commands. To do it by hand
instead, create a **Web Service** from the repo with:

| Setting | Value |
|---|---|
| Runtime | Python 3 |
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health check path | `/api/health` |

Then set the environment variables: at minimum `PYTHON_VERSION=3.13.5`,
`RESUME_TOKEN_SECRET` (any long random string), and your `RAZORPAY_KEY_ID`,
`RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`. Add `GROQ_API_KEY` for
LLM-written copy. Leave `DELIVER_FOR_REAL=false` until the rest works.

Note the service URL, `https://<name>.onrender.com`.

### 2. The dashboard on Vercel

Edit [`web/vercel.json`](web/vercel.json) and replace both
`REPLACE-WITH-YOUR-RENDER-SERVICE` placeholders with your Render host, then
commit and push. Then **New Project → import the repo** with:

| Setting | Value |
|---|---|
| Root directory | `web` |
| Framework preset | Vite (auto-detected) |
| Build command | `npm run build` (default) |
| Output directory | `dist` (default) |

No environment variables are needed on Vercel; the rewrites in `vercel.json` do
the wiring.

### 3. Connect the two

1. On Render, set `PUBLIC_BASE_URL` to your **Vercel** URL
   (`https://<project>.vercel.app`). Every recovery message links there, and
   Vercel rewrites `/orders/…` back to the API. Redeploy so it takes effect.
2. In the Razorpay dashboard, add a webhook pointing at
   `https://<name>.onrender.com/webhooks/razorpay` — **the Render URL directly,
   not through Vercel**, so nothing sits between Razorpay and the signature
   check, which is computed over the exact request bytes. Subscribe to
   `payment.failed` and `payment.captured`, and use the same secret as
   `RAZORPAY_WEBHOOK_SECRET`.
3. Open the Vercel URL. The Overview already shows results — see below.

**Nothing is empty on a cold instance.** The Overview and the case-level
screens need different treatment.

*The Overview* never shows an empty page. It opens with a committed run
(`web/src/data/comparison.json`, exported by
[`scripts/export_comparison.py`](scripts/export_comparison.py)) that ships
inside the frontend bundle, so a first visitor sees the numbers immediately
even though a fresh instance has an empty database. Those are the simulator's
own figures read back out of a real run, not hand-written ones, and the run is
seeded — pressing **Re-run both policies** recomputes exactly them on the
instance, at which point the screen switches to live results and says so. The
Compare screen is always live and never uses the committed copy.

A run takes minutes, which is far longer than Vercel's proxy or the browser
will hold a request open — so `POST /api/runs` returns `202` straight away, the
work happens on a background thread, and the dashboard polls
`/api/runs/status`. You can navigate away and come back; it keeps going. On a
free instance 3,000 cases is slow, so if you would rather the deployed demo
compare fewer, set `VITE_COMPARISON_COUNT` (e.g. `500`) in Vercel's environment
variables and redeploy — the Overview and Compare screens both read it, so they
stay in step. The numbers in this README are the 3,000-case run.

If you would rather point the dashboard straight at the API instead of using
the rewrites, set `CORS_ORIGINS` on Render to your Vercel URL. That is what it
is for.

### Before turning on real email

Set `DELIVERY_ALLOWLIST` to your own address **first**, then
`DELIVER_FOR_REAL=true`. The allowlist is the difference between a bug costing
nothing and a bug emailing a stranger.

Mail goes out over **Brevo's transactional HTTPS API**. SMTP is deliberately
not supported: Render, like most hosting platforms, blocks outbound SMTP ports
to keep spammers off its address space, so a deployed instance failed every
send with `[Errno 101] Network is unreachable` however correct the credentials
were. Port 443 is never blocked.

Brevo rather than the better-known options because its free tier verifies a
single sender **address** rather than a whole domain — so mail reaches real
recipients without owning one. Sign up, add your address on the Senders page,
click the confirmation link, create an API key, then set `BREVO_API_KEY` and
`EMAIL_FROM_ADDRESS`. Delivery to an unverified sender is rejected by the API,
and the rejection is recorded on the action like any other refusal.

Nothing else about a message changes with the transport: the same triggers, the
same recipients, the same subjects and the same bodies. Only the delivery
fields on the outbox row differ.

---

## Tests

263 tests, ~8 seconds. The suite is hermetic: it forces simulated mode and
overrides delivery, credentials and the contact cap, so a populated `.env` can
never make the tests bill a real account or change their outcome.

| File | Covers |
|---|---|
| `test_rules.py` | every documented reason maps to a rule; every rule uses email; blocked causes always set `suggests_alt_method`; the payday window |
| `test_oracle.py` | the oracle cannot see the policy; determinism; the modelled behaviours; the sweep failing loudly rather than scoring zero when it cannot read its inputs |
| `test_messages.py` | every template against all four trust rules |
| `test_llm.py` | schema validation, fallback on every failure mode, generated text breaking a trust rule being thrown away |
| `test_gate.py` | all six checks, priority order, baseline enforcement, email being sendable at any hour, the contact cap surviving a wound-back clock |
| `test_pipeline.py` | signature verification over raw bytes, tampered webhooks logged, dedup, the already-paid guard, the resume page, the review queue, reclassification, jump-to-next-action only moving forward, the checkout email winning over Razorpay's |
| `test_delivery.py` | every delivery refusal, the message reaching the HTTPS API unchanged, an API error being recorded rather than raised, and the API key never reaching a recorded detail |
| `test_razorpay_breaker.py` | the breaker surviving the demo clock being advanced, jumped, or reset while open; a payment_link failure never opening the order breaker |
| `test_runs_api.py` | a comparison is backgrounded, answers in under a second, keeps the API answerable, refuses a concurrent run, reports failures instead of hanging, and an empty database seeds itself while one with data is left alone |
| `test_public_base_url.py` | an explicit setting wins, a Render deployment falls back to its own URL, and local development still points locally |
| `test_published_comparison.py` | the committed run the Overview opens with still matches the figures this README quotes |
| `test_clock_lint.py` | no `datetime.now()` outside `clock.py` |

---

## Known limitations

- `fixtures/captured/` covers 3 of the 10 reasons. The other 7, including
  `insufficient_fund` and therefore the payday window, are unverified against a
  live account.
- Email is the only channel; real SMS needs DLT registration this project does
  not have.
- The oracle models conversion only, not annoyance, unsubscribes, or brand
  damage. The `deliberate_abandon` result is where that bites.
- The payday window uses the modal day of past payments, so a customer paid
  weekly or on a shifting date is modelled poorly.
- Single merchant, no auth, no multi-tenancy.
- SQLite with no migrations: schema changes need a fresh database
  (`POST /api/demo/reset`), and a deployment on ephemeral storage loses its
  data on every restart.
