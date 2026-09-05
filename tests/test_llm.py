"""The model client, which no longer runs while sending.

Copy is written ahead of time into copy/approved.json, so a failed payment
costs no API call. app/llm.py remains as the client for producing that table
offline, and its parsing stays strict -- anything unexpected from a model is
discarded rather than written into the table.
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


class TestNothingCallsItWhileSending:
    """app/llm.py is the client for writing the copy table offline. It is not
    on the send path any more, and these assert that rather than trusting it:
    the point of pre-writing the copy is that sending costs no model call."""

    def test_messages_does_not_import_the_llm(self):
        import inspect

        from app import messages

        assert "write_message_llm" not in inspect.getsource(messages)

    def test_the_executor_does_not_import_the_llm(self):
        import inspect

        from app import executor

        source = inspect.getsource(executor)
        assert "llm" not in source.replace("use_llm", "")

    def test_writing_a_body_makes_no_call_even_with_a_key_configured(
        self, case, action, customer, monkeypatch
    ):
        monkeypatch.setattr(llm, "GROQ_API_KEY", "gsk_pretend_this_is_real")
        monkeypatch.setattr(
            llm, "write_message_llm",
            lambda *a, **k: pytest.fail("sending must not reach the model"),
        )
        written = messages.write_body(
            case, action, customer, RESUME_URL, rule_id="R6_CARD_BLOCKED_ONLINE"
        )
        assert written.body
        assert written.source in ("copy", "template")
