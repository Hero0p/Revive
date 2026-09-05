"""Sending looks copy up. It does not generate it.

The send path used to call an LLM per failed payment, with the customer's
name, order id, amount and card digits in the request. Now the whole space of
messages is written ahead of time, reviewed, committed, and looked up.

Two properties matter most here and are asserted rather than assumed: sending
makes no model call at all, and a missing or broken cell still produces a
message, because the eight hand-written templates are the floor.
"""

import json
from datetime import datetime

import pytest

from app import copy_cache, messages
from app.models import Action, Case, Customer

RESUME_URL = "https://shop.example.com/orders/order_t1/resume?token=abc"


@pytest.fixture(autouse=True)
def loaded():
    copy_cache.chaos_copy_down = False
    copy_cache.load()
    yield
    copy_cache.chaos_copy_down = False
    copy_cache.load()


@pytest.fixture
def case() -> Case:
    return Case(
        id=1,
        razorpay_order_id="order_t1",
        amount_paise=400000,
        card_last4="4321",
        error_reason="card_disabled_for_online_payments",
        root_cause="card_config",
        cart_json=json.dumps([{"name": "Attikan Estate Coffee 250g"}]),
        failed_at=datetime(2026, 3, 3, 20, 47),
    )


@pytest.fixture
def action() -> Action:
    return Action(
        id=1, case_id=1, channel="email",
        message_intent="must_use_alternate_method",
        suggests_alt_method=True, message_index=1,
    )


def write(case, action, customer, **kwargs):
    return messages.write_body(
        case, action, customer, RESUME_URL, rule_id="R6_CARD_BLOCKED_ONLINE", **kwargs
    )


class TestTheTableIsComplete:
    def test_every_cell_the_lookup_can_ask_for_exists(self):
        cov = copy_cache.coverage()
        assert cov["error"] is None
        assert cov["complete"], (
            f"only {cov['cells_loaded']}/{cov['cells_expected']} cells -- "
            "run python -m scripts.build_copy_table"
        )

    def test_it_covers_every_rule_locale_and_vertical(self):
        cov = copy_cache.coverage()
        from app.rules import RULES

        assert cov["cells_expected"] == (
            len(RULES) * len(copy_cache.LOCALES) * len(copy_cache.VERTICALS)
        )
        assert cov["entries"] == cov["cells_expected"] * len(copy_cache.VARIANTS)


class TestSendingMakesNoModelCall:
    def test_writing_a_message_never_touches_the_llm(self, case, action, monkeypatch):
        """The headline claim: one failure used to be one API call."""
        import app.llm as llm

        monkeypatch.setattr(
            llm, "write_message_llm",
            lambda *a, **k: pytest.fail("the send path must not call a model"),
        )
        monkeypatch.setattr(
            "groq.Groq",
            lambda *a, **k: pytest.fail("no Groq client may be constructed while sending"),
        )
        written = write(case, action, Customer(id=1, name="Aarav", language="en"))
        assert written.source == "copy"
        assert written.body

    def test_the_body_is_stored_copy_with_the_slots_filled(self, case, action):
        written = write(case, action, Customer(id=1, name="Aarav", language="en"))
        assert "order_t1" in written.body
        assert "₹4,000" in written.body
        assert RESUME_URL in written.body
        assert "4321" in written.body
        assert "{" not in written.body  # every slot got filled


class TestTheFourTiers:
    def test_tier_one_is_an_exact_cell(self, case, action):
        written = write(
            case, action, Customer(id=1, language="en"), vertical="food_delivery"
        )
        assert written.tier == 1
        assert written.source == "copy"

    def test_tier_two_falls_back_to_the_generic_vertical(self, case, action, monkeypatch):
        for cell in list(copy_cache._TABLE["R6_CARD_BLOCKED_ONLINE"]):
            if cell == ("en", "travel"):
                monkeypatch.delitem(copy_cache._TABLE["R6_CARD_BLOCKED_ONLINE"], cell)
        written = write(case, action, Customer(id=1, language="en"), vertical="travel")
        assert written.tier == 2
        assert written.source == "copy"

    def test_tier_three_falls_back_to_english_and_generic(self, case, action, monkeypatch):
        table = copy_cache._TABLE["R6_CARD_BLOCKED_ONLINE"]
        for cell in [("hi", "travel"), ("hi", "generic")]:
            if cell in table:
                monkeypatch.delitem(table, cell)
        written = write(case, action, Customer(id=1, language="hi"), vertical="travel")
        assert written.tier == 3
        assert written.source == "copy"

    def test_tier_four_is_the_hand_written_template(self, case, action, monkeypatch):
        monkeypatch.delitem(copy_cache._TABLE, "R6_CARD_BLOCKED_ONLINE")
        written = write(case, action, Customer(id=1, language="en"))
        assert written.tier == 4
        assert written.source == "template"
        assert "blocked for online payments" in written.body

    def test_an_unknown_rule_still_sends(self, case, action):
        written = messages.write_body(
            case, action, Customer(id=1), RESUME_URL, rule_id="R99_INVENTED"
        )
        assert written.tier == 4
        assert written.body


class TestBrokenCopyNeverBreaksASend:
    def test_a_bad_placeholder_falls_through_instead_of_raising(
        self, case, action, monkeypatch
    ):
        broken = dict(copy_cache._TABLE["R6_CARD_BLOCKED_ONLINE"][("en", "ecommerce")][0])
        broken["body_template"] = "Hi {customer_name}, see {not_a_real_slot}: {resume_url}"
        monkeypatch.setitem(
            copy_cache._TABLE["R6_CARD_BLOCKED_ONLINE"], ("en", "ecommerce"), [broken]
        )
        written = write(case, action, Customer(id=1, language="en"))
        assert written.tier == 4
        assert written.source == "template"
        assert "bad slot" in written.detail

    def test_copy_that_breaks_a_trust_rule_after_filling_is_rejected(
        self, case, action, monkeypatch
    ):
        """A stored template can look clean and still produce a bad message:
        an item name carrying its own price puts a second amount in the body."""
        sneaky = dict(copy_cache._TABLE["R6_CARD_BLOCKED_ONLINE"][("en", "ecommerce")][0])
        sneaky["body_template"] = (
            "Hi {customer_name}, {order_id} ({item_names}) is {amount}: {resume_url}"
        )
        monkeypatch.setitem(
            copy_cache._TABLE["R6_CARD_BLOCKED_ONLINE"], ("en", "ecommerce"), [sneaky]
        )
        case.cart_json = json.dumps([{"name": "Gift card worth ₹500"}])
        written = write(case, action, Customer(id=1, language="en"))
        assert written.tier == 4
        assert "rejected after filling" in written.detail

    def test_a_missing_table_degrades_to_templates(self, case, action, monkeypatch):
        monkeypatch.setattr(copy_cache, "APPROVED", copy_cache.COPY_DIR / "nope.json")
        cov = copy_cache.load()
        assert "missing" in cov["error"]
        assert cov["cells_loaded"] == 0
        written = write(case, action, Customer(id=1))
        assert written.tier == 4
        assert written.body  # still sends

    def test_the_chaos_toggle_forces_the_templates(self, case, action):
        copy_cache.chaos_copy_down = True
        written = write(case, action, Customer(id=1, language="en"))
        assert written.tier == 4
        assert written.source == "template"


class TestVariantSelection:
    def test_it_is_deterministic_for_a_case(self, case, action):
        customer = Customer(id=1, language="en")
        picks = {write(case, action, customer).variant for _ in range(12)}
        assert len(picks) == 1

    def test_different_cases_can_get_different_wording(self, action):
        seen = set()
        for case_id in range(40):
            case = Case(
                id=case_id, razorpay_order_id=f"order_{case_id}", amount_paise=400000,
                card_last4="4321", cart_json="[]", failed_at=datetime(2026, 3, 3, 12, 0),
            )
            seen.add(write(case, action, Customer(id=1, language="en")).variant)
        assert len(seen) > 1, "every case picked the same variant"


class TestNoCustomerDataIsNeededToWriteCopy:
    def test_a_stored_template_carries_slots_not_values(self):
        """Nothing personal is in the table, so nothing personal was needed to
        write it -- which is what lets generation happen offline."""
        for by_cell in copy_cache._TABLE.values():
            for entries in by_cell.values():
                for entry in entries:
                    body = entry["body_template"]
                    assert "{resume_url}" in body
                    assert "@" not in body
                    assert "₹" not in body
