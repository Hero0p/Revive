"""The real entry point.

Signature verification here differs from the checkout callback: it is computed
over the raw request bytes with the webhook secret. Re-serialising the parsed
JSON produces different bytes and the signature never matches.
"""

import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import ingest
from app.clock import clock
from app.config import RAZORPAY_WEBHOOK_SECRET
from app.db import get_session
from app.models import RawEvent

router = APIRouter()


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, session: Session = Depends(get_session)):
    raw = await request.body()  # RAW BYTES, not the parsed dict
    signature = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    now = clock.now()

    # The row is written before processing, valid or not. This table is the
    # replay mechanism, the audit foundation, and the debugging tool.
    event_row = RawEvent(
        event_type=_peek_event_type(raw),
        payload_json=raw.decode("utf-8", errors="replace"),
        signature=signature,
        received_at=now,
    )
    session.add(event_row)
    session.commit()

    if not hmac.compare_digest(expected, signature):
        # A tampered or unsigned webhook. Logged, then refused.
        event_row.error = "invalid signature"
        session.commit()
        raise HTTPException(status_code=400, detail="invalid signature")

    try:
        event = json.loads(raw)
    except ValueError as exc:
        event_row.error = f"malformed json: {exc}"
        session.commit()
        raise HTTPException(status_code=400, detail="malformed json") from exc

    try:
        case = ingest.handle_event(session, event, now)
        event_row.processed_at = clock.now()
        session.commit()
    except Exception as exc:  # noqa: BLE001 -- a bad event must not lose the row
        session.rollback()
        event_row.error = f"{type(exc).__name__}: {exc}"
        session.commit()
        raise HTTPException(status_code=500, detail="processing failed") from exc

    return {
        "ok": True,
        "raw_event_id": event_row.id,
        "case_id": case.id if case else None,
        "status": case.status if case else None,
    }


def _peek_event_type(raw: bytes) -> str:
    try:
        return json.loads(raw).get("event", "unknown")
    except (ValueError, AttributeError):
        return "unparseable"


def sign(raw: bytes) -> str:
    """Used by the simulation console so injected events take the same path."""
    return hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
