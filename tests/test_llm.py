"""The LLM writes prose and nothing else, and it is never trusted.

Every path that could put an unvalidated sentence in front of a customer is
covered here, including the one where the model returns something plausible
that quietly breaks a trust rule.
"""

import json
from datetime import datetime

import pytest

from app import llm, messages
from app.models import Action, Case, Customer

RESUME_URL = "https://shop.example.com/orders/order_test123/resume?token=abc"


@pytest.fixture
def case() -> Case:
    return Case(
        id=1,
        razorpay_order_id="order_test123",
        amount_paise=400000,
        card_last4="4321",
        error_reason="card_disabled_for_online_payments",
        root_cause="card_config",
        cart_json=json.dumps([{"name": "Attikan Estate Coffee 250g", "price_paise": 65000}]),
        failed_at=datetime(2026, 3, 3, 20, 47),
    )


@pytest.fixture
def action() -> Action:
    return Action(
        id=1,
        case_id=1,
        channel="email",
        message_intent="must_use_alternate_method",
        suggests_alt_method=True,
        message_index=1,
    )


@pytest.fixture
def customer() -> Customer:
    return Customer(id=1, name="Aarav Sharma", language="en")


class TestSchemaValidation:
    def test_accepts_a_well_formed_reply(self):
        parsed = llm._parse(
            json.dumps(
                {
                    "body": "Your cart is saved.",
                    "channel_fit": "email",
                    "mentions_urgency": False,
                    "rationale": "Calm and factual.",
                }
            )
        )
        assert parsed["body"] == "Your cart is saved."

    def test_tolerates_a_markdown_fence(self):
        raw = '```json\n{"body": "Saved.", "channel_fit": "email", "mentions_urgency": false}\n```'
        assert llm._parse(raw) is not None

    @pytest.mark.parametrize(
        "raw",
        [
            "not json at all",
            "",
            "[]",
            '{"body": "Hi"}',  # missing keys
            '{"body": "", "channel_fit": "email", "mentions_urgency": false}',  # empty body
            '{"body": "Hi", "channel_fit": "carrier pigeon", "mentions_urgency": false}',
            '{"body": "Hi", "channel_fit": "email", "mentions_urgency": true}',  # it admits it
        ],
    )
    def test_rejects_anything_unexpected(self, raw):
        assert llm._parse(raw) is None

    def test_rejects_a_body_that_runs_long(self):
        raw = json.dumps(
            {"body": "x" * 600, "channel_fit": "email", "mentions_urgency": False}
        )
        assert llm._parse(raw) is None

    def test_rejects_a_channel_fit_other_than_email(self):
        raw = json.dumps({"body": "Hi", "channel_fit": "sms", "mentions_urgency": False})
        assert llm._parse(raw) is None


class TestFallback:
    def test_no_api_key_means_templates(self, case, action, customer, monkeypatch):
        monkeypatch.setattr(llm, "GROQ_API_KEY", "")
        body, source, _, model = messages.write_body(
            case, action, customer, RESUME_URL, use_llm=True
        )
        assert source == "template"
        assert model is None
        assert "blocked for online payments" in body

    def test_the_chaos_toggle_forces_templates(self, case, action, customer, monkeypatch):
        monkeypatch.setattr(llm, "chaos_llm_down", True)
        _, source, _, _ = messages.write_body(case, action, customer, RESUME_URL, use_llm=True)
        assert source == "template"

    def test_a_transport_error_falls_back_rather_than_raising(
        self, case, action, customer, monkeypatch
    ):
        def explode(*args, **kwargs):
            raise ConnectionError("groq unreachable")

        monkeypatch.setattr(llm, "write_message_llm", explode, raising=True)
        # write_body imports the function at call time, so patch the module it
        # reaches for and confirm a failure never escapes to the executor.
        try:
            messages.write_body(case, action, customer, RESUME_URL, use_llm=True)
        except ConnectionError:
            pytest.fail("an LLM transport error must never reach the executor")


class TestGeneratedTextIsNeverTrusted:
    """The model can return something fluent that still breaks a trust rule.
    When it does, the template goes out and the reason is recorded."""

    def _with_llm_saying(self, monkeypatch, body):
        monkeypatch.setattr(
            llm,
            "write_message_llm",
            lambda *a, **k: (body, "a rationale", "llama-3.1-8b-instant"),
        )

    def test_a_valid_message_is_used(self, case, action, customer, monkeypatch):
        good = (
            f"Hi Aarav, your card ending 4321 is blocked for online payments, so retrying it "
            f"will not work. You can pay ₹4,000 by UPI here: {RESUME_URL}"
        )
        self._with_llm_saying(monkeypatch, good)
        body, source, rationale, model = messages.write_body(
            case, action, customer, RESUME_URL, use_llm=True
        )
        assert source == "llm"
        assert model == "llama-3.1-8b-instant"
        assert body == good

    def test_urgency_is_rejected_even_when_the_model_denies_it(
        self, case, action, customer, monkeypatch
    ):
        self._with_llm_saying(
            monkeypatch,
            f"Hurry! Your order order_test123 for ₹4,000 expires soon: {RESUME_URL}",
        )
        body, source, rationale, _ = messages.write_body(
            case, action, customer, RESUME_URL, use_llm=True
        )
        assert source == "template"
        assert "rejected" in rationale
        assert "hurry" in rationale.lower() or "expire" in rationale.lower()

    def test_a_changed_amount_is_rejected(self, case, action, customer, monkeypatch):
        self._with_llm_saying(
            monkeypatch, f"Hi Aarav, order order_test123 is saved at ₹3,200: {RESUME_URL}"
        )
        _, source, rationale, _ = messages.write_body(
            case, action, customer, RESUME_URL, use_llm=True
        )
        assert source == "template"
        assert "not the order amount" in rationale

    def test_asking_for_an_otp_is_rejected(self, case, action, customer, monkeypatch):
        self._with_llm_saying(
            monkeypatch,
            f"Hi Aarav, order order_test123, ₹4,000. Reply with the OTP: {RESUME_URL}",
        )
        _, source, _, _ = messages.write_body(case, action, customer, RESUME_URL, use_llm=True)
        assert source == "template"

    def test_a_message_that_drops_the_merchant_link_is_rejected(
        self, case, action, customer, monkeypatch
    ):
        self._with_llm_saying(
            monkeypatch, "Hi Aarav, pay ₹4,000 for order_test123 at http://bit.ly/x9f"
        )
        _, source, _, _ = messages.write_body(case, action, customer, RESUME_URL, use_llm=True)
        assert source == "template"
