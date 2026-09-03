"""The trust rules from section 7, enforced on every template.

A bare payment link looks exactly like a scam. These tests are what stops a
well-meaning edit from turning a recovery message into one.
"""

import json
from datetime import datetime

import pytest

from app.messages import (
    TEMPLATES,
    MessageContext,
    build_context,
    format_rupees,
    render_template,
    validate,
)
from app.models import Case, Customer
from app.rules import RULES

AMOUNT_PAISE = 400000  # INR 4,000
RESUME_URL = "https://shop.example.com/orders/order_test123/resume?token=abc"


@pytest.fixture
def case() -> Case:
    return Case(
        id=1,
        razorpay_order_id="order_test123",
        amount_paise=AMOUNT_PAISE,
        currency="INR",
        method="card",
        card_last4="4321",
        error_code="BAD_REQUEST_ERROR",
        error_reason="payment_timed_out",
        root_cause="transient_network",
        cart_json=json.dumps(
            [
                {"name": "Attikan Estate Coffee 250g", "price_paise": 65000},
                {"name": "Ceramic Pour-Over Dripper", "price_paise": 145000},
            ]
        ),
        failed_at=datetime(2026, 3, 3, 20, 47),
    )


@pytest.fixture
def customer() -> Customer:
    return Customer(id=1, name="Aarav Sharma", contact="+919812345678", language="en")


@pytest.fixture
def ctx(case, customer) -> MessageContext:
    return build_context(case, customer, RESUME_URL)


ALL_INTENTS = sorted(TEMPLATES)


class TestEveryTemplateIsSafe:
    @pytest.mark.parametrize("intent", ALL_INTENTS)
    def test_passes_every_trust_rule(self, intent, case, ctx):
        body = render_template(intent, ctx)
        assert validate(body, case, ctx) == [], f"{intent}: {validate(body, case, ctx)}"

    @pytest.mark.parametrize("intent", ALL_INTENTS)
    def test_never_states_an_amount_other_than_the_order_amount(self, intent, case, ctx):
        body = render_template(intent, ctx)
        for other in ("₹4,500", "₹3,999", "₹400"):
            assert other not in body

    @pytest.mark.parametrize("intent", ALL_INTENTS)
    def test_never_creates_urgency(self, intent, case, ctx):
        body = render_template(intent, ctx).lower()
        for phrase in ("expires", "hurry", "last chance", "act now", "will be cancelled"):
            assert phrase not in body

    @pytest.mark.parametrize("intent", ALL_INTENTS)
    def test_never_asks_for_information(self, intent, case, ctx):
        body = render_template(intent, ctx).lower()
        for phrase in ("otp", "cvv", "card number", "reply with", "password"):
            assert phrase not in body

    @pytest.mark.parametrize("intent", ALL_INTENTS)
    def test_links_to_the_merchants_own_page(self, intent, case, ctx):
        assert RESUME_URL in render_template(intent, ctx)

    @pytest.mark.parametrize("intent", ALL_INTENTS)
    def test_carries_a_detail_only_the_real_merchant_knows(self, intent, case, ctx):
        body = render_template(intent, ctx)
        assert ctx.order_id in body or "Attikan" in body


class TestEveryRuleHasATemplate:
    def test_every_message_intent_in_the_decision_table_can_be_rendered(self):
        missing = [r.message_intent for r in RULES.values() if r.message_intent not in TEMPLATES]
        assert not missing, f"no template for {missing}"

    def test_the_baseline_has_one_too(self):
        assert "generic_retry" in TEMPLATES


class TestInsufficientFunds:
    """Never state the reason. It is embarrassing and it costs the sale."""

    def test_the_soft_reminder_does_not_reveal_why_the_payment_failed(self, case, ctx):
        body = render_template("soft_cart_reminder", ctx).lower()
        for word in ("insufficient", "funds", "balance", "declined", "failed", "bank"):
            assert word not in body


class TestValidatorCatchesBadMessages:
    def test_rejects_urgency(self, case, ctx):
        bad = f"Your cart expires in 10 minutes! Pay ₹4,000 now: {RESUME_URL}"
        assert any("urgency" in p for p in validate(bad, case, ctx))

    def test_rejects_a_changed_amount(self, case, ctx):
        bad = f"Order order_test123 is saved at ₹3,600: {RESUME_URL}"
        assert any("not the order amount" in p for p in validate(bad, case, ctx))

    def test_rejects_asking_for_the_otp(self, case, ctx):
        bad = f"Order order_test123, ₹4,000. Reply with the OTP to confirm: {RESUME_URL}"
        problems = validate(bad, case, ctx)
        assert any("asks for information" in p for p in problems)

    def test_rejects_a_message_with_no_merchant_link(self, case, ctx):
        bad = "Order order_test123 is saved at ₹4,000. Pay at http://bit.ly/x9f"
        assert any("resume page" in p for p in validate(bad, case, ctx))


class TestRupeeFormatting:
    @pytest.mark.parametrize(
        "paise,expected",
        [
            (400000, "₹4,000"),
            (65000, "₹650"),
            (100000000, "₹10,00,000"),  # Indian grouping, not 1,000,000
            (0, "₹0"),
        ],
    )
    def test_indian_digit_grouping(self, paise, expected):
        assert format_rupees(paise) == expected
