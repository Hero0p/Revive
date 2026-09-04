"""Six tables, kept flat and readable.

All datetime columns hold naive IST, exactly what clock.now() returns.
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class RawEvent(Base):
    """Every webhook, stored before processing. Replay and audit foundation."""

    __tablename__ = "raw_events"

    id = Column(Integer, primary_key=True)
    event_type = Column(String)
    payload_json = Column(Text)
    signature = Column(String)
    received_at = Column(DateTime)
    processed_at = Column(DateTime)
    error = Column(Text)


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True)
    name = Column(String)
    contact = Column(String)
    email = Column(String)
    language = Column(String, default="en")  # en | hi | hinglish
    opted_out = Column(Boolean, default=False)
    opted_out_at = Column(DateTime)
    last_contacted_at = Column(DateTime)

    # Observed payday pattern, learned from past successful payments.
    payday_days_json = Column(Text, default="[]")

    # Null for live/webhook customers. Simulation runs get their own copies so
    # one run's contact history cannot leak into another's gate decisions.
    run_id = Column(String, index=True)


class Case(Base):
    """One per failed payment attempt group. See ingest.py for the grouping."""

    __tablename__ = "cases"

    id = Column(Integer, primary_key=True)
    razorpay_payment_id = Column(String)
    razorpay_order_id = Column(String, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), index=True)

    amount_paise = Column(Integer)
    currency = Column(String, default="INR")
    method = Column(String)
    issuer = Column(String)
    card_last4 = Column(String)

    error_code = Column(String)  # raw Razorpay values
    error_reason = Column(String)
    # Razorpay reuses "payment_failed" as a catch-all, so these two are what
    # separate a bank decline from everything else it covers.
    error_source = Column(String)  # bank | business | customer | gateway
    error_step = Column(String)  # payment_authorization | payment_initiation | ...
    error_description = Column(Text)
    root_cause = Column(String)  # our bucket

    recovery_mode = Column(String, default="link_only")  # link_only | mandate_retry
    status = Column(String, default="detected")
    # detected | planned | acting | recovered | exhausted | suppressed | escalated

    cart_json = Column(Text, default="[]")
    amount_recovered_paise = Column(Integer, default=0)
    failed_at = Column(DateTime)
    resolved_at = Column(DateTime)

    attempt_count = Column(Integer, default=1)  # failures folded into this case
    policy = Column(String, default="router")  # baseline | router
    run_id = Column(String, index=True)  # null for live/webhook cases

    # The customer's name as of this failure, not a live join. The same phone
    # number is often reused across unrelated test checkouts with different
    # names, and each one now updates the shared customer row -- without this
    # snapshot, an old case would silently start displaying whoever that
    # number is named today.
    customer_name_snapshot = Column(String)

    customer = relationship("Customer")
    actions = relationship("Action", back_populates="case", order_by="Action.id")


class Action(Base):
    """A scheduled or completed intervention. Also the outbox: every outbound
    message is a row here before anything is sent."""

    __tablename__ = "actions"

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, ForeignKey("cases.id"), index=True)

    action_type = Column(String)  # send_link | suppress | escalate
    channel = Column(String)  # email -- every rule in rules.py chooses it
    message_intent = Column(String)
    suggests_alt_method = Column(Boolean, default=False)
    message_index = Column(Integer, default=1)  # 1st, 2nd message for this case

    scheduled_for = Column(DateTime, index=True)
    executed_at = Column(DateTime)
    status = Column(String, default="pending")  # pending | sent | blocked | failed

    idempotency_key = Column(String, unique=True)
    razorpay_link_id = Column(String)
    # Set only when create_payment_link failed but the message sent anyway --
    # the resume page opens Razorpay's checkout live from the browser, so it
    # never depended on this record existing. Null on every ordinary send.
    payment_link_error = Column(String)
    resume_url = Column(String)
    message_body = Column(Text)
    message_source = Column(String)  # template | llm
    blocked_reason = Column(String)
    discount_paise = Column(Integer, default=0)

    # Who this was actually addressed to, at send time -- not a live join to
    # the customer's current name. See Case.customer_name_snapshot for why.
    customer_name_snapshot = Column(String)

    # Did a real message actually leave the building?
    # sent | skipped | failed. "skipped" is normal: it means the system chose
    # not to deliver (delivery off, synthetic run, recipient not allowlisted).
    delivery_status = Column(String)
    delivery_detail = Column(String)
    delivery_id = Column(String)  # the mail server's Message-ID
    delivered_at = Column(DateTime)

    case = relationship("Case", back_populates="actions")


class DecisionRecord(Base):
    """WHY, for every decision. Rendered in the UI on every case."""

    __tablename__ = "decision_records"

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, ForeignKey("cases.id"), index=True)
    # Indexed because every send looks its record up by action_id twice (gate
    # checks, then message meta). Unindexed, that is a full scan of a table
    # with one row per decision -- which is what made a 3000-case run
    # quadratic rather than linear.
    action_id = Column(Integer, ForeignKey("actions.id"), index=True)

    rule_id = Column(String)  # e.g. R5_INSUFFICIENT_FUND
    inputs_json = Column(Text)  # what the rule saw
    decision = Column(Text)  # human-readable one-liner
    why = Column(Text)  # the rule's why, copied so the record stands alone
    expected_value_paise = Column(Integer)
    gate_checks_json = Column(Text)  # every check and its result
    llm_rationale = Column(Text)
    llm_model = Column(String)
    created_at = Column(DateTime)  # clock time, not wall time


class RunMetric(Base):
    __tablename__ = "run_metrics"

    id = Column(Integer, primary_key=True)
    run_id = Column(String, index=True)
    policy = Column(String)
    case_count = Column(Integer)

    # Facts about what the system did.
    messages_sent = Column(Integer)
    wrong_advice_count = Column(Integer)
    already_paid_contacts = Column(Integer)
    suppressed_count = Column(Integer)

    # Modelled outcome, depends on the oracle.
    amount_at_risk_paise = Column(Integer)
    amount_recovered_paise = Column(Integer)

    seed = Column(Integer)
    sweep_json = Column(Text)  # sensitivity sweep result, runs.py fills it
    created_at = Column(DateTime)
