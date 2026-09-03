"""The decision table.

This is the most important file in the project. Payments fail for eight
structurally different reasons and they do not deserve the same response. No
AI, no ML, no config file -- a plain dict a judge can read in 60 seconds.

The `why` field is not a comment. It is rendered in the UI on every decision
record. Explainability is satisfied by construction.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass(frozen=True)
class Rule:
    rule_id: str
    root_cause: str
    base_conversion: float
    channel: str
    message_intent: str
    suggests_alt_method: bool
    max_messages: int
    why: str
    delay_minutes: int | None = None
    delay_strategy: str | None = None  # "payday_window" overrides delay_minutes
    mention_reason: bool = True


RULES: dict[str, Rule] = {
    "payment_timed_out": Rule(
        rule_id="R1_TIMEOUT",
        root_cause="transient_network",
        delay_minutes=2,
        base_conversion=0.55,
        channel="email",
        message_intent="reassure_and_resume",
        suggests_alt_method=False,
        max_messages=2,
        why="Nothing was wrong with the customer or the card. They are still "
        "in the buying moment. Speed matters more than anything else.",
    ),
    "card_number_invalid": Rule(
        rule_id="R2_BAD_CARD_NUMBER",
        root_cause="data_entry",
        delay_minutes=2,
        base_conversion=0.50,
        channel="email",
        message_intent="reenter_details",
        suggests_alt_method=False,
        max_messages=2,
        why="A typo. High intent, trivially fixable, contact immediately.",
    ),
    "authentication_failed": Rule(
        rule_id="R3_AUTH_FAILED",
        root_cause="otp_failure",
        delay_minutes=15,
        base_conversion=0.40,
        channel="email",
        message_intent="retry_or_switch_to_upi",
        suggests_alt_method=True,
        max_messages=2,
        why="OTP was wrong, expired, or never arrived. The customer tried to "
        "pay. Give the OTP state 15 minutes to settle, then offer UPI as "
        "an escape hatch.",
    ),
    "gateway_technical_error": Rule(
        rule_id="R4_GATEWAY_DOWN",
        root_cause="gateway_degraded",
        delay_minutes=45,
        base_conversion=0.45,
        channel="email",
        message_intent="bank_was_down_try_now",
        suggests_alt_method=False,
        max_messages=2,
        why="The bank or gateway was down. Contacting them now sends them "
        "straight back into the outage. Wait for it to clear.",
    ),
    "insufficient_fund": Rule(
        rule_id="R5_INSUFFICIENT_FUND",
        root_cause="balance",
        delay_strategy="payday_window",
        base_conversion=0.35,
        channel="email",
        message_intent="soft_cart_reminder",
        suggests_alt_method=False,
        max_messages=2,
        mention_reason=False,
        why="The money is not in the account. Contacting them today guarantees "
        "a second failure. Wait for their observed payday pattern. Never "
        "state the reason -- it is embarrassing.",
    ),
    "card_disabled_for_online_payments": Rule(
        rule_id="R6_CARD_BLOCKED_ONLINE",
        root_cause="card_config",
        delay_minutes=60,
        base_conversion=0.30,
        channel="email",
        message_intent="must_use_alternate_method",
        suggests_alt_method=True,  # THE critical flag
        max_messages=1,
        why="The card is disabled for e-commerce by the issuing bank. 'Try "
        "again' is structurally impossible -- the same card will fail every "
        "time. The only message that can convert is one that tells them to "
        "use UPI or a different card.",
    ),
    "card_declined": Rule(
        rule_id="R7_DECLINED",
        root_cause="issuer_decline",
        delay_minutes=120,
        base_conversion=0.20,
        channel="email",
        message_intent="try_different_method",
        suggests_alt_method=True,
        max_messages=1,
        why="The bank refused and did not say why. Could be risk rules or "
        "limits. Low odds, so spend exactly one message and suggest an "
        "alternative rather than a repeat.",
    ),
    "payment_cancelled": Rule(
        rule_id="R8_USER_CANCELLED",
        root_cause="deliberate_abandon",
        delay_minutes=360,
        base_conversion=0.12,
        channel="email",
        message_intent="gentle_cart_reminder",
        suggests_alt_method=False,
        max_messages=1,
        why="They closed the modal on purpose. This is the lowest-intent "
        "bucket. One quiet nudge, no urgency, no discount. Chasing "
        "someone who deliberately left is how merchants get marked spam.",
    ),
    # --- Added from real test-mode captures, not from the docs. -------------
    # See fixtures/captured/. The eight rules above were written from
    # Razorpay's documented reason list; these two are what the API actually
    # sends. Keeping them separate is the honest record of that difference.
    "payment_failed": Rule(
        rule_id="R9_BANK_DECLINED",
        root_cause="issuer_decline",
        delay_minutes=120,
        base_conversion=0.20,
        channel="email",
        message_intent="try_different_method",
        suggests_alt_method=True,
        max_messages=1,
        why="Razorpay's catch-all reason. Only treated as a decline when the "
        "payload says the bank rejected it at the authorisation step, which "
        "is what a netbanking failure looks like. The bank refused and did "
        "not say why, so spend one message and suggest another method "
        "rather than a repeat. Anything else carrying this reason is "
        "escalated instead of guessed at.",
    ),
    "international_transaction_not_allowed": Rule(
        rule_id="R10_INTERNATIONAL_BLOCKED",
        root_cause="card_config",
        delay_minutes=60,
        base_conversion=0.30,
        channel="email",
        message_intent="must_use_alternate_method",
        suggests_alt_method=True,  # structurally blocked, same as R6
        max_messages=1,
        why="The card is not permitted for this transaction by the issuer or "
        "the merchant's configuration. Like a card disabled for online "
        "payments, retrying it is structurally impossible -- the only "
        "message that can convert names a different payment method.",
    ),
}

# Razorpay reuses this reason for many unrelated failures, so it is never
# matched on its own. rule_for() only accepts it with corroborating fields.
CATCH_ALL_REASONS = {"payment_failed"}
CATCH_ALL_BANK_STEPS = {"payment_authorization", "payment_authentication"}


# Reasons taken from Razorpay's documented error list. The simulator draws
# from these, and test_rules asserts every one maps to a rule.
ERROR_CODE_REASONS: dict[str, list[str]] = {
    "BAD_REQUEST_ERROR": [
        "payment_timed_out",
        "insufficient_fund",
        "payment_cancelled",
        "card_declined",
        "card_disabled_for_online_payments",
        "card_number_invalid",
    ],
    "GATEWAY_ERROR": [
        "gateway_technical_error",
        "authentication_failed",
    ],
}

# Reasons actually observed coming out of the test-mode API, which is not the
# same set. Raw payloads for each are in fixtures/captured/.
OBSERVED_REASONS: dict[str, str] = {
    "payment_failed": "netbanking failure, error_source=bank",
    "international_transaction_not_allowed": "card, error_source=business",
}

# Structurally blocked: no amount of "try again" can succeed on this card.
BLOCKED_CAUSES = {"card_config"}


def rule_for(
    error_reason: str | None,
    error_source: str | None = None,
    error_step: str | None = None,
) -> Rule | None:
    """None means unmapped. Do not guess -- the caller escalates to a human.

    `payment_failed` is Razorpay's catch-all and covers unrelated situations,
    so it is only honoured when the payload also says a bank rejected it at
    the authorisation step. Without that corroboration it escalates, exactly
    like a reason we have never seen.
    """
    if not error_reason:
        return None

    if error_reason in CATCH_ALL_REASONS:
        if error_source == "bank" and error_step in CATCH_ALL_BANK_STEPS:
            return RULES[error_reason]
        return None

    return RULES.get(error_reason)


def error_code_for(error_reason: str) -> str:
    for code, reasons in ERROR_CODE_REASONS.items():
        if error_reason in reasons:
            return code
    return "BAD_REQUEST_ERROR"


def payday_window(now: datetime, payday_days: list[int]) -> datetime:
    """Next occurrence of the customer's modal payday, at 10:30 local.

    payday_days is the day-of-month of each past successful payment. With no
    history we default to the 2nd of next month.
    """
    target_day = Counter(payday_days).most_common(1)[0][0] if payday_days else 2

    candidate = _at_1030(now.year, now.month, target_day)
    if candidate is None or candidate <= now:
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        candidate = _at_1030(year, month, target_day)
        while candidate is None:  # e.g. the 31st in a 30-day month
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            candidate = _at_1030(year, month, target_day)
    return candidate


def _at_1030(year: int, month: int, day: int) -> datetime | None:
    try:
        return datetime(year, month, day, 10, 30)
    except ValueError:
        return None


def observed_payday_days(successful_payment_dates: list[date]) -> list[int]:
    return [d.day for d in successful_payment_dates]
