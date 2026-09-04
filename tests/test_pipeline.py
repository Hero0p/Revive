"""The webhook path end to end: signature, raw_events, dedup, already-paid."""

import hashlib
import hmac
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.clock import clock
from app.config import RAZORPAY_WEBHOOK_SECRET
from app.db import SessionLocal, create_all, reset_database
from app.main import app
from app.models import Action, Case, Customer, DecisionRecord, RawEvent


@pytest.fixture(autouse=True)
def fresh_db():
    create_all()
    reset_database()
    # Frozen at a Tuesday noon so scheduling assertions are stable rather than
    # dependent on whenever the suite happens to run.
    clock.freeze(datetime(2026, 3, 3, 12, 0))
    yield
    clock.reset()


@pytest.fixture
def client() -> TestClient:
    # No context manager: the background worker loop stays out of the tests.
    return TestClient(app)


def failure_event(reason="payment_timed_out", order_id="order_t1", amount=400000, **extra):
    entity = {
        "id": extra.get("payment_id", "pay_t1"),
        "order_id": order_id,
        "amount": amount,
        "currency": "INR",
        "status": "failed",
        "method": "card",
        "error_code": "BAD_REQUEST_ERROR",
        "error_reason": reason,
        "card": {"last4": "4321", "issuer": "HDFC"},
        "email": extra.get("email", "aarav@example.com"),
        "contact": extra.get("contact", "+919812345678"),
        "notes": {"customer_name": "Aarav Sharma", "cart": json.dumps([{"name": "Coffee"}])},
    }
    return {"event": "payment.failed", "payload": {"payment": {"entity": entity}}}


def capture_event(order_id="order_t1", amount=400000):
    return {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {"id": "pay_ok", "order_id": order_id, "amount": amount}
            }
        },
    }


def post(client: TestClient, event: dict, signature: str | None = None):
    raw = json.dumps(event).encode()
    sig = signature or hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/webhooks/razorpay",
        content=raw,
        headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
    )


class TestSignatureVerification:
    def test_a_correctly_signed_webhook_is_accepted(self, client):
        assert post(client, failure_event()).status_code == 200

    def test_a_tampered_webhook_is_rejected(self, client):
        response = post(client, failure_event(), signature="0" * 64)
        assert response.status_code == 400
        assert response.json()["detail"] == "invalid signature"

    def test_an_unsigned_webhook_is_rejected(self, client):
        raw = json.dumps(failure_event()).encode()
        assert client.post("/webhooks/razorpay", content=raw).status_code == 400

    def test_a_rejected_webhook_is_still_logged(self, client):
        """The raw_events table is the audit foundation. Nothing is dropped."""
        post(client, failure_event(), signature="0" * 64)
        session = SessionLocal()
        events = session.scalars(select(RawEvent)).all()
        assert len(events) == 1
        assert events[0].error == "invalid signature"
        assert events[0].processed_at is None
        session.close()

    def test_the_signature_is_computed_over_raw_bytes(self, client):
        """Re-serialising the parsed JSON produces different bytes. This is the
        mistake that silently breaks ingestion."""
        event = failure_event()
        raw = json.dumps(event, indent=2).encode()  # different bytes, same object
        sig_over_compact = hmac.new(
            RAZORPAY_WEBHOOK_SECRET.encode(), json.dumps(event).encode(), hashlib.sha256
        ).hexdigest()
        response = client.post(
            "/webhooks/razorpay",
            content=raw,
            headers={"X-Razorpay-Signature": sig_over_compact},
        )
        assert response.status_code == 400


class TestCaseCreation:
    def test_a_failure_creates_a_case_with_a_root_cause_and_a_plan(self, client):
        post(client, failure_event(reason="card_disabled_for_online_payments"))
        session = SessionLocal()
        case = session.scalars(select(Case)).one()
        assert case.root_cause == "card_config"
        assert case.status == "planned"

        action = session.scalars(select(Action).where(Action.case_id == case.id)).one()
        assert action.suggests_alt_method is True
        assert action.status == "pending"

        record = session.scalars(select(DecisionRecord)).one()
        assert record.rule_id == "R6_CARD_BLOCKED_ONLINE"
        assert "disabled for e-commerce" in record.why
        session.close()

    def test_an_unmapped_reason_is_escalated_not_guessed(self, client):
        post(client, failure_event(reason="some_reason_razorpay_added_last_week"))
        session = SessionLocal()
        case = session.scalars(select(Case)).one()
        assert case.status == "escalated"
        assert case.root_cause == "unknown"
        assert session.scalars(select(Action)).all() == []

        record = session.scalars(select(DecisionRecord)).one()
        assert record.rule_id == "UNMAPPED"
        session.close()

    def test_the_review_queue_surfaces_escalations(self, client):
        post(client, failure_event(reason="brand_new_reason"))
        queue = client.get("/api/review-queue").json()
        assert len(queue["cases"]) == 1


class TestDeduplication:
    def test_three_failures_in_one_checkout_are_one_case(self, client):
        """Group by (customer, order) inside 30 minutes."""
        for i, reason in enumerate(
            ["payment_timed_out", "card_number_invalid", "insufficient_fund"]
        ):
            post(client, failure_event(reason=reason, payment_id=f"pay_{i}"))

        session = SessionLocal()
        cases = session.scalars(select(Case)).all()
        assert len(cases) == 1
        assert cases[0].attempt_count == 3
        # The last failure is the one that explains the drop-off.
        assert cases[0].error_reason == "insufficient_fund"
        assert cases[0].root_cause == "balance"
        session.close()

    def test_the_superseded_plan_is_kept_for_the_audit_trail(self, client):
        post(client, failure_event(reason="payment_timed_out"))
        post(client, failure_event(reason="insufficient_fund", payment_id="pay_2"))

        session = SessionLocal()
        actions = session.scalars(select(Action).order_by(Action.id)).all()
        assert actions[0].status == "blocked"
        assert "superseded" in actions[0].blocked_reason
        assert actions[-1].status == "pending"
        session.close()

    def test_the_same_failure_twice_does_not_collide(self, client):
        """The most ordinary customer behaviour there is: try the same card
        again, fail the same way again. Both plans have the same rule and the
        same message index, so the idempotency key has to separate them."""
        first = post(client, failure_event(reason="payment_timed_out"))
        second = post(client, failure_event(reason="payment_timed_out", payment_id="pay_2"))
        assert first.status_code == 200
        assert second.status_code == 200

        session = SessionLocal()
        case = session.scalars(select(Case)).one()
        assert case.attempt_count == 2

        actions = session.scalars(select(Action).order_by(Action.id)).all()
        keys = [a.idempotency_key for a in actions]
        assert len(keys) == len(set(keys)), f"duplicate idempotency keys: {keys}"
        assert actions[0].status == "blocked"  # the superseded plan
        assert actions[-1].status == "pending"  # the live one
        session.close()

    def test_three_identical_failures_still_produce_one_case(self, client):
        for i in range(3):
            assert (
                post(client, failure_event(reason="card_declined", payment_id=f"pay_{i}")).status_code
                == 200
            )
        session = SessionLocal()
        assert len(session.scalars(select(Case)).all()) == 1
        assert session.scalars(select(Case)).one().attempt_count == 3
        session.close()

    def test_a_different_order_is_a_different_case(self, client):
        post(client, failure_event(order_id="order_a"))
        post(client, failure_event(order_id="order_b", payment_id="pay_2"))
        session = SessionLocal()
        assert len(session.scalars(select(Case)).all()) == 2
        session.close()

    def test_a_failure_outside_the_window_is_a_new_case(self, client):
        post(client, failure_event())
        clock.advance(minutes=45)
        post(client, failure_event(payment_id="pay_2"))
        session = SessionLocal()
        assert len(session.scalars(select(Case)).all()) == 2
        session.close()


class TestCustomerDetailsStayCurrent:
    """Matching an existing customer by phone number proves it is the same
    person, not that nothing about them has changed. A regression: the first
    email ever seen for a number used to stick forever, so a real customer
    who typed a corrected email on a later checkout kept being contacted at
    the old one."""

    def test_a_later_checkout_updates_the_stored_email(self, client):
        post(client, failure_event(contact="+919812345678", email="old@example.com"))
        post(
            client,
            failure_event(
                order_id="order_2", payment_id="pay_2",
                contact="+919812345678", email="new@example.com",
            ),
        )
        session = SessionLocal()
        customer = session.scalars(select(Customer)).one()
        assert customer.email == "new@example.com"
        session.close()

    def test_a_later_checkout_updates_the_stored_name_and_language(self, client):
        first = failure_event(contact="+919812345678")
        first["payload"]["payment"]["entity"]["notes"]["customer_name"] = "Old Name"
        first["payload"]["payment"]["entity"]["notes"]["language"] = "en"
        post(client, first)

        second = failure_event(order_id="order_2", payment_id="pay_2", contact="+919812345678")
        second["payload"]["payment"]["entity"]["notes"]["customer_name"] = "New Name"
        second["payload"]["payment"]["entity"]["notes"]["language"] = "hinglish"
        post(client, second)

        session = SessionLocal()
        customer = session.scalars(select(Customer)).one()
        assert customer.name == "New Name"
        assert customer.language == "hinglish"
        session.close()

    def test_a_webhook_with_no_email_does_not_erase_the_stored_one(self, client):
        """Not every webhook carries every field. A blank must never overwrite
        a known-good value."""
        post(client, failure_event(contact="+919812345678", email="keep@example.com"))
        blank = failure_event(order_id="order_2", payment_id="pay_2", contact="+919812345678")
        blank["payload"]["payment"]["entity"]["email"] = ""
        post(client, blank)

        session = SessionLocal()
        customer = session.scalars(select(Customer)).one()
        assert customer.email == "keep@example.com"
        session.close()

    def test_matching_by_email_can_backfill_a_missing_contact(self, client):
        first = failure_event(email="shared@example.com")
        first["payload"]["payment"]["entity"]["contact"] = ""
        post(client, first)

        post(
            client,
            failure_event(
                order_id="order_2", payment_id="pay_2",
                email="shared@example.com", contact="+919812345678",
            ),
        )
        session = SessionLocal()
        customer = session.scalars(select(Customer)).one()
        assert customer.contact == "+919812345678"
        session.close()

    def test_updating_details_does_not_create_a_second_customer(self, client):
        post(client, failure_event(contact="+919812345678", email="old@example.com"))
        post(
            client,
            failure_event(
                order_id="order_2", payment_id="pay_2",
                contact="+919812345678", email="new@example.com",
            ),
        )
        session = SessionLocal()
        assert len(session.scalars(select(Customer)).all()) == 1
        session.close()

    def test_payday_history_survives_an_unrelated_detail_update(self, client):
        """payday_days_json is learned from successful payments, not from
        notes on a failure webhook. A later failure updating the email must
        not reset it to the default empty list."""
        post(client, failure_event(contact="+919812345678", email="old@example.com"))
        post(client, capture_event())  # teaches a payday day

        session = SessionLocal()
        customer = session.scalars(select(Customer)).one()
        learned = customer.payday_days_json
        assert learned != "[]"
        session.close()

        post(
            client,
            failure_event(
                order_id="order_2", payment_id="pay_2",
                contact="+919812345678", email="new@example.com",
            ),
        )
        session = SessionLocal()
        customer = session.scalars(select(Customer)).one()
        assert customer.payday_days_json == learned
        session.close()


class TestAlreadyPaidGuard:
    def test_a_capture_closes_the_case(self, client):
        post(client, failure_event())
        post(client, capture_event())
        session = SessionLocal()
        case = session.scalars(select(Case)).one()
        assert case.status == "recovered"
        assert case.amount_recovered_paise == 400000
        assert case.resolved_at is not None
        session.close()

    def test_the_pending_message_is_blocked_not_sent(self, client):
        """Fails at 8:47, pays at 8:49, and the 8:49 message never goes out."""
        post(client, failure_event())
        post(client, capture_event())

        clock.advance(minutes=10)
        client.post("/api/clock/advance", json={"minutes": 0})

        session = SessionLocal()
        action = session.scalars(select(Action)).one()
        assert action.status == "blocked"
        assert "already succeeded" in action.blocked_reason
        assert action.message_body is None
        session.close()

    def test_a_capture_teaches_the_payday_pattern(self, client):
        post(client, failure_event())
        post(client, capture_event())
        session = SessionLocal()
        from app.models import Customer

        customer = session.scalars(select(Customer)).one()
        assert json.loads(customer.payday_days_json) == [clock.now().day]
        session.close()


class TestCustomerNameIsASnapshotNotALiveJoin:
    """The same phone number gets reused across unrelated test checkouts with
    different names, and TestCustomerDetailsStayCurrent above means the
    shared customer row updates to whichever name was used most recently.
    Without a snapshot, an old case or outbox row would silently start
    displaying that new name instead of who actually failed the payment."""

    def test_the_outbox_shows_the_name_from_when_the_message_was_sent(self, client):
        first = failure_event(contact="+919812345678")
        first["payload"]["payment"]["entity"]["notes"]["customer_name"] = "loki"
        post(client, first)
        client.post("/api/clock/advance", json={"minutes": 5})

        second = failure_event(order_id="order_2", payment_id="pay_2", contact="+919812345678")
        second["payload"]["payment"]["entity"]["notes"]["customer_name"] = "Aarav Sharma"
        post(client, second)

        outbox = client.get("/api/outbox?run_id=live").json()["messages"]
        sent = next(m for m in outbox if m["order_id"] == "order_t1")
        assert sent["customer_name"] == "loki", (
            f"the outbox must show who this message actually greeted, not the "
            f"customer's current name, got {sent['customer_name']!r}"
        )

    def test_the_case_list_shows_the_name_at_the_time_of_that_failure(self, client):
        first = failure_event(contact="+919812345678")
        first["payload"]["payment"]["entity"]["notes"]["customer_name"] = "loki"
        post(client, first)

        second = failure_event(order_id="order_2", payment_id="pay_2", contact="+919812345678")
        second["payload"]["payment"]["entity"]["notes"]["customer_name"] = "Aarav Sharma"
        post(client, second)

        cases = client.get("/api/cases?run_id=live").json()["cases"]
        first_case = next(c for c in cases if c["order_id"] == "order_t1")
        second_case = next(c for c in cases if c["order_id"] == "order_2")
        assert first_case["customer_name"] == "loki"
        assert second_case["customer_name"] == "Aarav Sharma"

    def test_a_dedup_fold_updates_the_snapshot_to_the_latest_attempt(self, client):
        """Consistent with folding in the latest error_reason: the case shows
        the name from the failure that actually explains the current state."""
        first = failure_event(contact="+919812345678")
        first["payload"]["payment"]["entity"]["notes"]["customer_name"] = "loki"
        post(client, first)

        second = failure_event(
            order_id="order_t1", payment_id="pay_2", contact="+919812345678",
            reason="insufficient_fund",
        )
        second["payload"]["payment"]["entity"]["notes"]["customer_name"] = "tst"
        post(client, second)

        session = SessionLocal()
        case = session.scalars(select(Case)).one()
        assert case.attempt_count == 2
        assert case.customer_name_snapshot == "tst"
        session.close()

    def test_the_message_body_and_the_outbox_heading_agree(self, client):
        """The screenshot bug: the body correctly greeted 'Hi loki,' while the
        heading above it read 'Aarav Sharma' -- two views of the same send
        disagreeing about who it was sent to."""
        event = failure_event(reason="payment_timed_out", contact="+919812345678")
        event["payload"]["payment"]["entity"]["notes"]["customer_name"] = "loki"
        post(client, event)
        client.post("/api/clock/advance", json={"minutes": 5})

        # Someone else's checkout reuses the same phone number afterwards.
        other = failure_event(order_id="order_x", payment_id="pay_x", contact="+919812345678")
        other["payload"]["payment"]["entity"]["notes"]["customer_name"] = "Aarav Sharma"
        post(client, other)

        outbox = client.get("/api/outbox?run_id=live").json()["messages"]
        sent = next(m for m in outbox if m["order_id"] == "order_t1")
        assert "Hi loki" in sent["message_body"]
        assert sent["customer_name"] == "loki"


class TestReclassifyIsHumanOnly:
    """The pipeline never guesses a cause for a real, unmapped failure -- but
    a human watching a demo can, explicitly and once, so the demo does not
    dead-end on Razorpay's generic catch-all. This is that lever, and these
    tests are about how narrowly it is scoped."""

    def escalated_case(self, client):
        post(client, failure_event(reason="a_reason_nobody_has_ever_seen"))
        session = SessionLocal()
        case = session.scalars(select(Case)).one()
        session.close()
        return case.id

    def test_assigning_a_specific_reason_schedules_the_right_rule(self, client):
        case_id = self.escalated_case(client)
        r = client.post(f"/api/cases/{case_id}/reclassify", json={"error_reason": "insufficient_fund"})
        assert r.status_code == 200
        assert r.json()["status"] == "planned"
        assert r.json()["root_cause"] == "balance"

        session = SessionLocal()
        case = session.get(Case, case_id)
        assert case.error_reason == "insufficient_fund"
        record = session.scalars(
            select(DecisionRecord).where(DecisionRecord.rule_id == "DEMO_RECLASSIFIED")
        ).one()
        assert "human operator" in record.why
        session.close()

    def test_random_picks_something_that_is_not_the_ambiguous_catch_all(self, client):
        case_id = self.escalated_case(client)
        r = client.post(f"/api/cases/{case_id}/reclassify", json={"random": True})
        assert r.status_code == 200
        session = SessionLocal()
        case = session.get(Case, case_id)
        assert case.error_reason != "payment_failed"
        assert case.status == "planned"
        session.close()

    def test_the_ambiguous_catch_all_cannot_be_chosen(self, client):
        case_id = self.escalated_case(client)
        r = client.post(f"/api/cases/{case_id}/reclassify", json={"error_reason": "payment_failed"})
        assert r.status_code == 400

    def test_a_reason_outside_the_decision_table_is_refused(self, client):
        case_id = self.escalated_case(client)
        r = client.post(f"/api/cases/{case_id}/reclassify", json={"error_reason": "made_up"})
        assert r.status_code == 400

    def test_a_case_that_already_has_a_rule_cannot_be_reclassified(self, client):
        post(client, failure_event(reason="payment_timed_out"))
        session = SessionLocal()
        case = session.scalars(select(Case)).one()
        session.close()
        r = client.post(f"/api/cases/{case.id}/reclassify", json={"random": True})
        assert r.status_code == 400

    def test_a_missing_case_is_a_404(self, client):
        r = client.post("/api/cases/999999/reclassify", json={"random": True})
        assert r.status_code == 404


class TestPaymentLinkFailureDoesNotBlockTheMessage:
    """The resume page opens Razorpay's checkout live from the browser using
    the order itself -- it never needed the payment link record to exist.
    Regression: Payment Links has its own daily quota in test mode, separate
    from order creation, and a real account hit it mid-demo. Before this fix,
    every message silently stopped going out the moment that quota was hit,
    with no connection between the two beyond both touching Razorpay."""

    def test_the_message_still_sends_when_the_link_cannot_be_created(self, client, monkeypatch):
        from app import razorpay_client

        def broken(*args, **kwargs):
            raise razorpay_client.RazorpayDown("test mode limit of 30 reached for payment_link")

        monkeypatch.setattr(razorpay_client.client, "create_payment_link", broken)

        post(client, failure_event(reason="payment_timed_out"))
        client.post("/api/clock/advance", json={"minutes": 5})

        session = SessionLocal()
        action = session.scalars(select(Action).where(Action.status == "sent")).one()
        assert action.message_body  # the LLM/template body still wrote
        assert action.resume_url  # still points at our own domain
        assert action.razorpay_link_id is None
        assert "test mode limit" in action.payment_link_error
        session.close()

    def test_a_working_link_leaves_no_error_recorded(self, client):
        """The common case: no monkeypatch, simulated link succeeds."""
        post(client, failure_event(reason="payment_timed_out"))
        client.post("/api/clock/advance", json={"minutes": 5})

        session = SessionLocal()
        action = session.scalars(select(Action).where(Action.status == "sent")).one()
        assert action.payment_link_error is None
        assert action.razorpay_link_id is not None
        session.close()

    def test_delivery_is_still_attempted_when_the_link_fails(self, client, monkeypatch):
        from app import delivery, razorpay_client

        monkeypatch.setattr(
            razorpay_client.client,
            "create_payment_link",
            lambda *a, **k: (_ for _ in ()).throw(razorpay_client.RazorpayDown("quota")),
        )
        monkeypatch.setattr(delivery, "DELIVER_FOR_REAL", True)
        monkeypatch.setattr(delivery, "EMAIL_CONFIGURED", True)
        sent = {}
        monkeypatch.setattr(
            delivery, "_send_email", lambda r, a, b: sent.setdefault("to", r) or "id-1"
        )

        post(client, failure_event(reason="payment_timed_out"))
        client.post("/api/clock/advance", json={"minutes": 5})

        session = SessionLocal()
        action = session.scalars(select(Action).where(Action.status == "sent")).one()
        assert action.delivery_status == "sent"
        assert sent.get("to") == "aarav@example.com"
        session.close()


class TestTheClockDrivesEverything:
    def test_a_message_is_sent_once_its_moment_arrives(self, client):
        post(client, failure_event(reason="payment_timed_out"))  # 2 minute rule

        session = SessionLocal()
        assert session.scalars(select(Action)).one().status == "pending"
        session.close()

        client.post("/api/clock/advance", json={"minutes": 5})

        session = SessionLocal()
        action = session.scalars(select(Action).order_by(Action.id)).first()
        assert action.status == "sent"
        assert action.message_body
        assert action.resume_url
        assert "/orders/order_t1/resume?token=" in action.resume_url
        session.close()

    def test_jumping_to_the_next_action_runs_it(self, client):
        post(client, failure_event(reason="card_declined"))  # 120 minute rule
        response = client.post("/api/clock/advance", json={"to_next_action": True})
        assert response.json()["ran"].get("sent") == 1


class TestSimulationConsoleUsesTheSamePipeline:
    def test_inject_goes_through_the_webhook_route(self, client):
        response = client.post(
            "/api/sim/inject",
            json={"error_reason": "insufficient_fund", "amount_paise": 250000},
        )
        body = response.json()
        assert body["sent_through"] == "/webhooks/razorpay"
        assert body["case_id"] is not None

        session = SessionLocal()
        raw = session.scalars(select(RawEvent)).one()
        assert raw.processed_at is not None  # it was signed, verified, and processed
        session.close()

    def test_the_tamper_demo_is_rejected_and_logged(self, client):
        response = client.post("/api/sim/tamper")
        assert response.json()["status_code"] == 400
        events = client.get("/api/events").json()["events"]
        assert events[0]["error"] == "invalid signature"


class TestSimulationRunsStayOffTheRealApi:
    """A 200-case comparison would otherwise create ~300 real payment links on
    the merchant's account for money that was never real."""

    def test_synthetic_runs_use_simulated_links(self):
        from app.db import SessionLocal
        from app.simulator import run_policy

        session = SessionLocal()
        try:
            run_id = run_policy(session, "router", count=6, seed=99)
            case_ids = {c.id for c in session.scalars(select(Case).where(Case.run_id == run_id)).all()}
            sent = [
                a
                for a in session.scalars(select(Action).where(Action.status == "sent")).all()
                if a.case_id in case_ids
            ]
            assert sent, "the run should have sent something"
            for action in sent:
                assert action.razorpay_link_id.startswith("plink_sim_"), (
                    f"synthetic run created a non-simulated link: {action.razorpay_link_id}"
                )
        finally:
            session.close()


class TestCheckoutSignatureVerification:
    """The checkout callback signs "order_id|payment_id" with the API key
    secret. The webhook signs the raw body with the webhook secret. Confusing
    the two is the mistake that breaks ingestion silently, so both are tested
    against each other."""

    def checkout_signature(self, order_id: str, payment_id: str) -> str:
        from app.config import RAZORPAY_KEY_SECRET

        return hmac.new(
            RAZORPAY_KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
        ).hexdigest()

    def test_a_valid_signature_closes_the_case(self, client):
        post(client, failure_event())
        response = client.post(
            "/api/verify-payment",
            json={
                "razorpay_order_id": "order_t1",
                "razorpay_payment_id": "pay_ok_1",
                "razorpay_signature": self.checkout_signature("order_t1", "pay_ok_1"),
            },
        )
        assert response.status_code == 200
        assert response.json()["verified"] is True

        session = SessionLocal()
        assert session.scalars(select(Case)).one().status == "recovered"
        session.close()

    def test_a_mismatched_signature_marks_nothing_as_paid(self, client):
        post(client, failure_event())
        response = client.post(
            "/api/verify-payment",
            json={
                "razorpay_order_id": "order_t1",
                "razorpay_payment_id": "pay_ok_1",
                "razorpay_signature": "f" * 64,
            },
        )
        assert response.status_code == 400

        session = SessionLocal()
        case = session.scalars(select(Case)).one()
        assert case.status != "recovered"
        assert case.resolved_at is None
        session.close()

    def test_missing_fields_are_rejected(self, client):
        response = client.post("/api/verify-payment", json={"razorpay_order_id": "order_t1"})
        assert response.status_code == 422

    def test_the_webhook_secret_does_not_work_for_checkout(self, client):
        """Different secret, different payload, different result."""
        from app.config import RAZORPAY_WEBHOOK_SECRET

        wrong = hmac.new(
            RAZORPAY_WEBHOOK_SECRET.encode(),
            b"order_t1|pay_ok_1",
            hashlib.sha256,
        ).hexdigest()
        response = client.post(
            "/api/verify-payment",
            json={
                "razorpay_order_id": "order_t1",
                "razorpay_payment_id": "pay_ok_1",
                "razorpay_signature": wrong,
            },
        )
        assert response.status_code == 400


class TestOrderCreation:
    def test_an_amount_below_one_rupee_is_refused(self, client):
        response = client.post("/api/orders", json={"amount_paise": 50})
        assert response.status_code == 400
        assert "at least 100 paise" in response.json()["detail"]

    def test_the_key_secret_never_reaches_the_response(self, client):
        from app.config import RAZORPAY_KEY_SECRET

        response = client.post("/api/orders", json={"amount_paise": 210000})
        assert RAZORPAY_KEY_SECRET not in response.text


class TestResumePage:
    def test_the_page_shows_the_cart_and_the_original_amount(self, client):
        post(client, failure_event(amount=400000))
        client.post("/api/clock/advance", json={"minutes": 5})

        session = SessionLocal()
        action = session.scalars(select(Action).where(Action.status == "sent")).first()
        url = action.resume_url
        session.close()

        page = client.get(url.replace("http://localhost:5173", ""))
        assert page.status_code == 200
        assert "₹4,000" in page.text
        assert "order_t1" in page.text

    def test_a_forged_token_is_refused(self, client):
        post(client, failure_event())
        page = client.get("/orders/order_t1/resume?token=not-a-real-token")
        assert page.status_code == 400
        assert "not valid" in page.text

    def test_an_already_paid_order_says_so(self, client):
        post(client, failure_event())
        client.post("/api/clock/advance", json={"minutes": 5})
        session = SessionLocal()
        url = session.scalars(select(Action).where(Action.status == "sent")).first().resume_url
        session.close()

        post(client, capture_event())
        page = client.get(url.replace("http://localhost:5173", ""))
        assert "already paid" in page.text


class TestJumpToNextAction:
    """'Jump to next action' is the control the whole demo is driven with. It
    must always move time forward."""

    def _pending_action_at(self, when, order_id):
        session = SessionLocal()
        case = Case(
            razorpay_order_id=order_id,
            amount_paise=210000,
            status="planned",
            error_reason="payment_timed_out",
            root_cause="transient_network",
            failed_at=when,
        )
        session.add(case)
        session.flush()
        session.add(
            Action(
                case_id=case.id,
                action_type="send_link",
                channel="email",
                message_intent="reassure_and_resume",
                message_index=1,
                scheduled_for=when,
                status="pending",
                idempotency_key=f"{order_id}:1",
            )
        )
        session.commit()
        session.close()

    def test_an_action_left_in_the_past_never_drags_the_clock_backwards(self, client):
        """A stale pending action -- an old case, or one scheduled before the
        clock was last reset -- is already due. Jumping back to it used to
        rewind the clock days, which pushed everything else into the far
        future and made the demo look frozen."""
        self._pending_action_at(clock.now() - timedelta(days=3), "order_stale")
        before = clock.now()

        client.post("/api/clock/advance", json={"to_next_action": True})

        assert clock.now() >= before

    def test_the_overdue_action_still_runs(self, client):
        self._pending_action_at(clock.now() - timedelta(days=3), "order_overdue")

        ran = client.post("/api/clock/advance", json={"to_next_action": True}).json()["ran"]

        assert ran.get("sent") == 1

    def test_a_future_action_is_still_jumped_to(self, client):
        target = clock.now() + timedelta(days=2)
        self._pending_action_at(target, "order_future")

        client.post("/api/clock/advance", json={"to_next_action": True})

        assert clock.now() >= target

    def test_nothing_scheduled_says_so(self, client):
        result = client.post("/api/clock/advance", json={"to_next_action": True}).json()
        assert result["note"] == "nothing scheduled"


class TestTheCheckoutEmailWins:
    """Razorpay's modal auto-fills saved details for a returning phone number,
    so the payment entity's email is often an older address tied to that
    number. The recovery message has to reach the address the customer typed
    on the merchant's own form."""

    def test_the_order_note_email_beats_the_payment_entity_email(self, client):
        event = failure_event(reason="payment_timed_out")
        entity = event["payload"]["payment"]["entity"]
        entity["email"] = "autofilled-by-razorpay@example.com"
        entity["notes"]["email"] = "typed-at-checkout@example.com"

        post(client, event)

        session = SessionLocal()
        customer = session.scalars(select(Customer)).one()
        assert customer.email == "typed-at-checkout@example.com"
        session.close()

    def test_it_falls_back_to_the_entity_when_the_order_carried_no_email(self, client):
        event = failure_event(reason="payment_timed_out")
        entity = event["payload"]["payment"]["entity"]
        entity["email"] = "only-from-razorpay@example.com"
        entity["notes"].pop("email", None)

        post(client, event)

        session = SessionLocal()
        customer = session.scalars(select(Customer)).one()
        assert customer.email == "only-from-razorpay@example.com"
        session.close()
