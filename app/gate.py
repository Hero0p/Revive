"""The stopping rules.

Runs before every action. Every check is recorded in the decision record
whether it passes or fails, so the audit trail shows what the agent considered,
not just what it did.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from app.models import Action, Case, Customer

QUIET_START_HOUR = 21  # 21:00 IST
QUIET_END_HOUR = 9  # 09:00 IST
MIN_HOURS_BETWEEN_CONTACTS = 24
MAX_DISCOUNT_PAISE = 50_000  # INR 500
MAX_DISCOUNT_FRACTION = 0.05  # 5% of the order

ALLOW, BLOCK, DEFER, HALT, ESCALATE = "allow", "block", "defer", "halt", "escalate"


@dataclass
class Budget:
    """A run's message budget. Exhausting it halts the run -- it does not
    quietly degrade into sending fewer messages."""

    max_messages: int | None = None
    messages_used: int = 0

    @property
    def exhausted(self) -> bool:
        return self.max_messages is not None and self.messages_used >= self.max_messages


@dataclass
class GateCheck:
    name: str
    passed: bool
    outcome: str  # allow | block | defer | halt | escalate
    detail: str
    enforced: bool = True


# The baseline policy is the status quo: a blast to everyone who failed. It
# still respects opt-outs and the run budget, because every merchant does, but
# it has none of the router's restraint. The checks it ignores are still run
# and still recorded -- that is how the comparison stays honest and visible.
BASELINE_ENFORCED = {"customer_opted_out", "run_budget"}


@dataclass
class GateResult:
    allowed: bool
    outcome: str
    checks: list[GateCheck] = field(default_factory=list)
    reason: str | None = None
    defer_until: datetime | None = None

    def as_json(self) -> list[dict]:
        return [asdict(c) for c in self.checks]


def check(
    case: Case,
    action: Action,
    now: datetime,
    customer: Customer | None = None,
    messages_sent: int = 0,
    max_messages: int = 1,
    budget: Budget | None = None,
    enforced_checks: set[str] | None = None,
) -> GateResult:
    checks: list[GateCheck] = []
    defer_until: datetime | None = None

    # 1. Customer opted out -- permanent, no override.
    opted_out = bool(customer and customer.opted_out)
    checks.append(
        GateCheck(
            "customer_opted_out",
            not opted_out,
            BLOCK if opted_out else ALLOW,
            "Customer has opted out of messages" if opted_out else "Customer has not opted out",
        )
    )

    # 2. Already paid. A customer who fails at 8:47 and succeeds on their own
    #    at 8:49 must never be messaged.
    already_paid = case.status == "recovered"
    checks.append(
        GateCheck(
            "case_already_recovered",
            not already_paid,
            BLOCK if already_paid else ALLOW,
            "Payment already succeeded" if already_paid else "Payment still outstanding",
        )
    )

    # 3. Message cap for this cause.
    over_cap = messages_sent >= max_messages
    checks.append(
        GateCheck(
            "message_cap",
            not over_cap,
            BLOCK if over_cap else ALLOW,
            f"{messages_sent} of {max_messages} messages already sent",
        )
    )

    # 4. Quiet hours 21:00-09:00 IST.
    in_quiet_hours = now.hour >= QUIET_START_HOUR or now.hour < QUIET_END_HOUR
    if in_quiet_hours:
        defer_until = _next_9am(now)
    checks.append(
        GateCheck(
            "quiet_hours",
            not in_quiet_hours,
            DEFER if in_quiet_hours else ALLOW,
            f"{now:%H:%M} is inside quiet hours, deferring to {defer_until:%d %b %H:%M}"
            if in_quiet_hours
            else f"{now:%H:%M} is inside contact hours",
        )
    )

    # 5. One message per day, per customer, across all their cases.
    too_soon = False
    if customer and customer.last_contacted_at:
        elapsed = now - customer.last_contacted_at
        too_soon = elapsed < timedelta(hours=MIN_HOURS_BETWEEN_CONTACTS)
        if too_soon:
            candidate = customer.last_contacted_at + timedelta(hours=MIN_HOURS_BETWEEN_CONTACTS)
            defer_until = max(defer_until, candidate) if defer_until else candidate
        detail = f"Last contacted {elapsed.total_seconds() / 3600:.1f}h ago"
    else:
        detail = "Never contacted before"
    checks.append(
        GateCheck("contact_frequency", not too_soon, DEFER if too_soon else ALLOW, detail)
    )

    # 6. Run budget. This halts the run and waits for a human.
    out_of_budget = bool(budget and budget.exhausted)
    checks.append(
        GateCheck(
            "run_budget",
            not out_of_budget,
            HALT if out_of_budget else ALLOW,
            f"{budget.messages_used}/{budget.max_messages} messages used"
            if budget and budget.max_messages is not None
            else "No budget cap set for this run",
        )
    )

    # 7. Discount authority. Anything meaningful goes to a human.
    discount = action.discount_paise or 0
    cap = min(MAX_DISCOUNT_PAISE, int((case.amount_paise or 0) * MAX_DISCOUNT_FRACTION))
    over_authority = discount > cap
    checks.append(
        GateCheck(
            "discount_authority",
            not over_authority,
            ESCALATE if over_authority else ALLOW,
            f"Discount {discount / 100:.0f} exceeds the {cap / 100:.0f} limit"
            if over_authority
            else f"No discount requested (limit {cap / 100:.0f})",
        )
    )

    if enforced_checks is not None:
        for c in checks:
            c.enforced = c.name in enforced_checks

    # Priority order matters: a halt outranks a block outranks a defer.
    for outcome in (HALT, ESCALATE, BLOCK, DEFER):
        failed = next(
            (c for c in checks if c.outcome == outcome and not c.passed and c.enforced), None
        )
        if failed:
            return GateResult(
                allowed=False,
                outcome=outcome,
                checks=checks,
                reason=f"{failed.name}: {failed.detail}",
                defer_until=defer_until if outcome == DEFER else None,
            )

    return GateResult(allowed=True, outcome=ALLOW, checks=checks)


def _next_9am(now: datetime) -> datetime:
    target = now.replace(hour=QUIET_END_HOUR, minute=0, second=0, microsecond=0)
    if now.hour >= QUIET_START_HOUR:
        target += timedelta(days=1)
    return target
