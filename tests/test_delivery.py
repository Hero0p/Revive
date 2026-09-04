"""Delivery refuses far more often than it sends, and that is the point.

The dangerous failure here is not "the message did not arrive". It is "a
synthetic customer with a made-up address got a real message", or "a bug sent
200 emails during a comparison run". These tests pin the refusals down.
"""

import pytest

from app import delivery
from app.models import Action, Case, Customer


def make(channel="email", run_id=None, body="Your cart is saved."):
    case = Case(id=1, run_id=run_id, amount_paise=400000)
    action = Action(id=1, case_id=1, channel=channel, message_body=body, message_intent="soft_cart_reminder")
    customer = Customer(id=1, name="Aarav", email="aarav@example.com", contact="+919812345678")
    return action, case, customer


class TestItRefusesByDefault:
    def test_delivery_is_off_unless_switched_on(self, monkeypatch):
        monkeypatch.setattr(delivery, "DELIVER_FOR_REAL", False)
        status, _, detail = delivery.deliver(*make())
        assert status == "skipped"
        assert "switched off" in detail


class TestItNeverContactsSyntheticCustomers:
    def test_a_synthetic_run_is_never_delivered(self, monkeypatch):
        """A 200-case comparison must not send 200 real messages."""
        monkeypatch.setattr(delivery, "DELIVER_FOR_REAL", True)
        monkeypatch.setattr(delivery, "EMAIL_CONFIGURED", True)
        status, _, detail = delivery.deliver(*make(run_id="router-s42-n200"))
        assert status == "skipped"
        assert "synthetic" in detail

    def test_a_live_case_is_allowed_through(self, monkeypatch):
        monkeypatch.setattr(delivery, "DELIVER_FOR_REAL", True)
        monkeypatch.setattr(delivery, "EMAIL_CONFIGURED", True)
        sent = {}

        def fake_email(recipient, action, body):
            sent["to"] = recipient
            return "id-1"

        monkeypatch.setattr(delivery, "_send_email", fake_email)
        status, provider_id, _ = delivery.deliver(*make(run_id=None))
        assert status == "sent"
        assert sent["to"] == "aarav@example.com"


class TestTheAllowlist:
    def test_an_address_off_the_allowlist_is_refused(self, monkeypatch):
        monkeypatch.setattr(delivery, "DELIVER_FOR_REAL", True)
        monkeypatch.setattr(delivery, "EMAIL_CONFIGURED", True)
        monkeypatch.setattr(delivery, "DELIVERY_ALLOWLIST", ["me@example.com"])
        status, _, detail = delivery.deliver(*make())
        assert status == "skipped"
        assert "allowlist" in detail.lower()

    def test_an_allowlisted_address_goes_through(self, monkeypatch):
        monkeypatch.setattr(delivery, "DELIVER_FOR_REAL", True)
        monkeypatch.setattr(delivery, "EMAIL_CONFIGURED", True)
        monkeypatch.setattr(delivery, "DELIVERY_ALLOWLIST", ["aarav@example.com"])
        monkeypatch.setattr(
            delivery, "_send_email", lambda r, a, b: "id-1"
        )
        assert delivery.deliver(*make())[0] == "sent"

    def test_the_allowlist_ignores_case(self, monkeypatch):
        monkeypatch.setattr(delivery, "DELIVER_FOR_REAL", True)
        monkeypatch.setattr(delivery, "EMAIL_CONFIGURED", True)
        monkeypatch.setattr(delivery, "DELIVERY_ALLOWLIST", ["AARAV@example.com".lower()])
        monkeypatch.setattr(delivery, "_send_email", lambda r, a, b: "id-1")
        assert delivery.deliver(*make())[0] == "sent"


class TestMissingConfiguration:
    def test_no_smtp_credentials_means_nothing_is_sent(self, monkeypatch):
        monkeypatch.setattr(delivery, "DELIVER_FOR_REAL", True)
        monkeypatch.setattr(delivery, "EMAIL_CONFIGURED", False)
        status, _, detail = delivery.deliver(*make("email"))
        assert status == "skipped"
        assert "RESEND_API_KEY" in detail

    def test_a_customer_with_no_address_is_skipped(self, monkeypatch):
        monkeypatch.setattr(delivery, "DELIVER_FOR_REAL", True)
        monkeypatch.setattr(delivery, "EMAIL_CONFIGURED", True)
        action, case, customer = make("email")
        customer.email = ""
        status, _, detail = delivery.deliver(action, case, customer)
        assert status == "skipped"
        assert "no email address" in detail


class TestOnlyEmailIsEverDelivered:
    """Every rule in rules.py chooses email; this is the guard against a
    future rule regressing back to a channel this project cannot deliver."""

    @pytest.mark.parametrize("channel", ["sms", "whatsapp"])
    def test_a_non_email_channel_is_refused_not_rerouted(self, channel, monkeypatch):
        monkeypatch.setattr(delivery, "DELIVER_FOR_REAL", True)
        monkeypatch.setattr(delivery, "EMAIL_CONFIGURED", True)
        sent = {}
        monkeypatch.setattr(delivery, "_send_email", lambda r, a, b: sent.setdefault("hit", True))

        status, provider_id, detail = delivery.deliver(*make(channel))
        assert status == "skipped"
        assert provider_id is None
        assert channel in detail
        assert "email only" in detail
        assert "hit" not in sent, "a non-email channel must never reach the mail server"

    def test_an_email_rule_sends_normally(self, monkeypatch):
        monkeypatch.setattr(delivery, "DELIVER_FOR_REAL", True)
        monkeypatch.setattr(delivery, "EMAIL_CONFIGURED", True)
        monkeypatch.setattr(delivery, "_send_email", lambda r, a, b: "id-1")
        status, _, detail = delivery.deliver(*make("email"))
        assert status == "sent"
        assert "not supported" not in detail


class TestFailuresAreContained:
    def test_a_provider_error_never_raises(self, monkeypatch):
        """A dead mail server must not take down the run."""
        monkeypatch.setattr(delivery, "DELIVER_FOR_REAL", True)
        monkeypatch.setattr(delivery, "EMAIL_CONFIGURED", True)

        def explode(*args):
            raise ConnectionRefusedError("smtp is down")

        monkeypatch.setattr(delivery, "_send_email", explode)
        status, _, detail = delivery.deliver(*make())
        assert status == "failed"
        assert "ConnectionRefusedError" in detail

    def test_an_empty_body_is_never_sent(self, monkeypatch):
        monkeypatch.setattr(delivery, "DELIVER_FOR_REAL", True)
        monkeypatch.setattr(delivery, "EMAIL_CONFIGURED", True)
        status, _, detail = delivery.deliver(*make(body=""))
        assert status == "skipped"
        assert "no message body" in detail


class TestEverySubjectExists:
    def test_every_message_intent_has_an_email_subject(self):
        from app.messages import TEMPLATES

        missing = [intent for intent in TEMPLATES if intent not in delivery.SUBJECTS]
        assert not missing, f"no email subject for {missing}"

    def test_no_subject_creates_urgency(self):
        from app.messages import URGENCY_MARKERS

        for intent, subject in delivery.SUBJECTS.items():
            low = subject.lower()
            for marker in URGENCY_MARKERS:
                assert marker not in low, f"{intent} subject uses '{marker}'"


class TestTheHttpsTransport:
    """SMTP is unusable on hosting platforms that block outbound mail ports --
    Render failed every send with "[Errno 101] Network is unreachable". The
    whole transport is an HTTPS API for that reason."""

    def _configured(self, monkeypatch):
        monkeypatch.setattr(delivery, "DELIVER_FOR_REAL", True)
        monkeypatch.setattr(delivery, "EMAIL_CONFIGURED", True)
        monkeypatch.setattr(delivery, "RESEND_API_KEY", "secret-key-value")
        monkeypatch.setattr(delivery, "EMAIL_FROM_ADDRESS", "revive@calmcat.in")
        monkeypatch.setattr(delivery, "DELIVERY_ALLOWLIST", [])

    def test_it_posts_the_message_to_the_email_api(self, monkeypatch):
        self._configured(monkeypatch)
        sent = {}

        class Response:
            status_code = 201

            @staticmethod
            def json():
                return {"id": "resend-123"}

        def fake_post(url, **kwargs):
            sent.update(url=url, json=kwargs["json"], headers=kwargs["headers"])
            return Response()

        monkeypatch.setattr(delivery.httpx, "post", fake_post)

        action, case, customer = make()
        customer.email = "someone@example.com"
        status, provider_id, detail = delivery.deliver(action, case, customer)

        assert status == "sent"
        assert provider_id == "resend-123"
        assert sent["url"] == "https://api.resend.com/emails"
        assert sent["json"]["to"] == ["someone@example.com"]
        assert "revive@calmcat.in" in sent["json"]["from"]
        # The subject and body are the ones the pipeline produced, unchanged.
        assert sent["json"]["subject"] == delivery.SUBJECTS["soft_cart_reminder"]
        assert sent["json"]["text"] == "Your cart is saved."
        assert sent["headers"]["Authorization"] == "Bearer secret-key-value"

    def test_an_api_error_is_recorded_rather_than_raised(self, monkeypatch):
        self._configured(monkeypatch)

        class Response:
            status_code = 400
            text = '{"message":"The calmcat.in domain is not verified"}'

        monkeypatch.setattr(delivery.httpx, "post", lambda url, **kw: Response())

        action, case, customer = make()
        customer.email = "someone@example.com"
        status, _, detail = delivery.deliver(action, case, customer)

        assert status == "failed"
        assert "400" in detail and "not verified" in detail

    def test_the_api_key_never_reaches_the_recorded_detail(self, monkeypatch):
        """The detail is stored on the action, rendered in the dashboard and
        printed in logs. A provider echoing the key back must not leak it."""
        self._configured(monkeypatch)

        class Response:
            status_code = 401
            text = 'unauthorised for key secret-key-value'

        monkeypatch.setattr(delivery.httpx, "post", lambda url, **kw: Response())

        action, case, customer = make()
        customer.email = "someone@example.com"
        status, _, detail = delivery.deliver(action, case, customer)

        assert status == "failed"
        assert "secret-key-value" not in detail
        assert "***" in detail

    def test_a_transport_failure_is_recorded_without_the_key(self, monkeypatch):
        self._configured(monkeypatch)

        def explode(url, **kwargs):
            raise ConnectionError("connect failed using secret-key-value")

        monkeypatch.setattr(delivery.httpx, "post", explode)

        action, case, customer = make()
        customer.email = "someone@example.com"
        status, _, detail = delivery.deliver(action, case, customer)

        assert status == "failed"
        assert "secret-key-value" not in detail


class TestTheDeliveryStatusExposesMisconfiguration:
    def test_it_flags_a_localhost_link(self, monkeypatch):
        """A message linking to localhost is useless to whoever gets it, and
        that is invisible from the outbox alone."""
        status = delivery.status()
        assert "public_base_url" in status
        assert "public_base_url_is_local" in status
        assert status["transport"] == "resend"
