"""Razorpay SDK wrapper.

Two things live here that the SDK does not give you: a circuit breaker, so a
Razorpay outage queues actions instead of burning them, and a chaos toggle so
the outage can be demonstrated on demand.

The breaker's own clock is real wall time (clock.wall_clock_now()), not the
demo clock. This is deliberate and was a real bug once: the demo clock can be
advanced, jumped, or reset by the dashboard's clock controls, and the breaker
briefly used clock.now() for its open/half-open timing. An operator jumping
the live clock forward while the breaker was open, then resetting it, left
_opened_at holding a stale offset with no way to correct itself -- the breaker
computed "reopens in" as the entire vanished offset, once nineteen real hours
for a breaker meant to reopen in sixty seconds. A circuit breaker protects
against a real API outage happening in real time; it must not care what the
demo is currently pretending the time is.

Without API keys the client runs in simulated mode and says so in every
response. It never pretends a simulated link is a real one.

The breaker is scoped **per operation** (order / payment_link / fetch), not
one breaker for the whole client. Razorpay's Payment Links product has its own
daily cap in test mode, separate from Standard Checkout order creation; a
single shared breaker meant hitting that cap tripped the breaker for
*everything*, including creating a brand new order for the next checkout,
which has nothing to do with Payment Links at all. The chaos toggle still
takes down every operation at once, because that is meant to simulate a real
full outage.
"""

from datetime import datetime, timedelta

from app.clock import wall_clock_now
from app.config import LIVE_RAZORPAY, RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET

FAILURE_THRESHOLD = 5
OPEN_DURATION = timedelta(seconds=60)
OPERATIONS = ("order", "payment_link", "fetch")


class RazorpayDown(Exception):
    """Raised when the call failed or the breaker is open. The caller leaves
    the action pending and the worker retries it."""


class RazorpayClient:
    def __init__(self) -> None:
        self._sdk = None
        self._consecutive_failures: dict[str, int] = dict.fromkeys(OPERATIONS, 0)
        self._opened_at: dict[str, datetime | None] = dict.fromkeys(OPERATIONS, None)
        # The real underlying error, kept separate from the "breaker open"
        # wrapper message. Without this the true cause of the first failure
        # was unrecoverable -- every retry attempt overwrote it with
        # "circuit breaker open...", including on the row an operator would
        # actually go look at to find out what happened.
        self._last_error: dict[str, str | None] = dict.fromkeys(OPERATIONS, None)
        self.chaos_down = False
        self.call_count = 0

        if LIVE_RAZORPAY:
            import razorpay

            self._sdk = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

    @property
    def live(self) -> bool:
        return self._sdk is not None

    def state(self, op: str, now: datetime | None = None) -> str:
        now = now or wall_clock_now()
        opened_at = self._opened_at[op]
        if opened_at is None:
            return "closed"
        if now - opened_at >= OPEN_DURATION:
            return "half_open"
        return "open"

    def status(self) -> dict:
        now = wall_clock_now()
        by_op = {}
        for op in OPERATIONS:
            state = self.state(op, now)
            by_op[op] = {
                "breaker": state,
                "consecutive_failures": self._consecutive_failures[op],
                "reopens_in_seconds": self._reopen_in_seconds(op, now) if state == "open" else 0,
                "last_error": self._last_error[op],
            }
        overall = "closed"
        if any(v["breaker"] == "open" for v in by_op.values()):
            overall = "open"
        elif any(v["breaker"] == "half_open" for v in by_op.values()):
            overall = "half_open"
        return {
            "mode": "live" if self.live else "simulated",
            "breaker": overall,
            "by_operation": by_op,
            "chaos_razorpay_down": self.chaos_down,
            "calls": self.call_count,
            # Kept for any old caller reading the flat shape: the worst-case
            # operation's numbers, so a single open breaker is never hidden.
            "consecutive_failures": max(by_op[op]["consecutive_failures"] for op in OPERATIONS),
            "reopens_in_seconds": max(by_op[op]["reopens_in_seconds"] for op in OPERATIONS),
            "last_error": next(
                (by_op[op]["last_error"] for op in OPERATIONS if by_op[op]["breaker"] != "closed"),
                None,
            ),
        }

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        description: str,
        customer_name: str,
        contact: str,
        email: str,
        resume_url: str,
        idempotency_key: str,
        notify_sms: bool = False,
        notify_email: bool = False,
        force_simulated: bool = False,
    ) -> dict:
        """One payment link. The idempotency key means a replay after a crash
        cannot create a second link or a second charge.

        force_simulated is for the synthetic comparison runs. A 200-case batch
        would otherwise create ~300 real payment links on the merchant's
        account, take minutes, and risk rate limits -- for money that was never
        real in the first place.
        """

        def call():
            if not self.live or force_simulated:
                # Simulated mode: deterministic id derived from the idempotency
                # key, and labelled so nothing downstream mistakes it for real.
                return {
                    "id": f"plink_sim_{abs(hash(idempotency_key)) % 10**12:012d}",
                    "short_url": resume_url,
                    "amount": amount_paise,
                    "simulated": True,
                }
            payload = {
                "amount": amount_paise,
                "currency": "INR",
                "description": description[:255],
                "customer": {"name": customer_name, "contact": contact, "email": email},
                "notify": {"sms": notify_sms, "email": notify_email},
                "reminder_enable": False,
                "callback_url": resume_url,
                "callback_method": "get",
                "notes": {"idempotency_key": idempotency_key},
            }
            return self._sdk.payment_link.create(payload)

        return self._guarded("payment_link", call)

    def create_order(self, *, amount_paise: int, receipt: str, notes: dict) -> dict:
        def call():
            if not self.live:
                return {
                    "id": f"order_sim_{abs(hash(receipt)) % 10**12:012d}",
                    "amount": amount_paise,
                    "currency": "INR",
                    "simulated": True,
                }
            return self._sdk.order.create(
                {"amount": amount_paise, "currency": "INR", "receipt": receipt, "notes": notes}
            )

        return self._guarded("order", call)

    def fetch_payments(self, count: int = 25, skip: int = 0) -> list[dict]:
        """Recent payments, newest first.

        Razorpay only delivers webhooks to a public URL. Polling this endpoint
        is the documented fallback and lets a real failed payment reach the
        pipeline from localhost, with no tunnel. The entity shape is identical
        to payload.payment.entity in a payment.failed webhook.
        """

        def call():
            if not self.live:
                return {"items": []}
            return self._sdk.payment.all({"count": count, "skip": skip})

        return (self._guarded("fetch", call) or {}).get("items", [])

    def fetch_payment(self, payment_id: str) -> dict:
        def call():
            if not self.live:
                return {"id": payment_id, "simulated": True}
            return self._sdk.payment.fetch(payment_id)

        return self._guarded("fetch", call)

    def _guarded(self, op: str, call):
        now = wall_clock_now()
        state = self.state(op, now)

        if state == "open":
            raise RazorpayDown(
                f"{op}: circuit breaker open after {self._consecutive_failures[op]} failures "
                f"({self._last_error[op]}), retrying in {self._reopen_in_seconds(op, now)}s"
            )

        try:
            if self.chaos_down:
                raise ConnectionError("chaos: Razorpay API unreachable")
            result = call()
        except Exception as exc:  # noqa: BLE001 -- any SDK error trips the breaker
            self._last_error[op] = str(exc)
            self._record_failure(op, now)
            raise RazorpayDown(f"{op}: {exc}") from exc

        self.call_count += 1
        self._consecutive_failures[op] = 0
        self._opened_at[op] = None
        return result

    def _record_failure(self, op: str, now: datetime) -> None:
        self._consecutive_failures[op] += 1
        if self._consecutive_failures[op] >= FAILURE_THRESHOLD:
            self._opened_at[op] = now

    def _reopen_in_seconds(self, op: str, now: datetime) -> int:
        """Never negative. A clamp, not just documentation: it is the only
        thing standing between a corrupted _opened_at and a demo screen
        reading '71999s' instead of the 60-second reality."""
        opened_at = self._opened_at[op]
        if opened_at is None:
            return 0
        return max(0, int((OPEN_DURATION - (now - opened_at)).total_seconds()))

    def reset_breaker(self) -> None:
        for op in OPERATIONS:
            self._consecutive_failures[op] = 0
            self._opened_at[op] = None
            self._last_error[op] = None


client = RazorpayClient()
