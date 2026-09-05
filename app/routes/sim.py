"""Simulation console, clock control, and the chaos toggles.

/api/sim/inject constructs a webhook payload, signs it, and sends it through
the same /webhooks/razorpay handler the real Razorpay traffic uses. There is no
parallel code path -- the payload is synthetic, the pipeline is identical.
"""

import json

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import copy_cache, worker
from app.clock import clock, iso
from app.config import LLM_ENABLED, LLM_MODEL
from app.db import get_session, reset_database
from app.models import Action, Case
from app.razorpay_client import client as razorpay
from app.routes.webhooks import sign
from app.rules import error_code_for

router = APIRouter(prefix="/api")

DEFAULT_CART = [
    {"name": "Attikan Estate Coffee 250g", "price_paise": 65000},
    {"name": "Ceramic Pour-Over Dripper", "price_paise": 145000},
]


class InjectRequest(BaseModel):
    error_reason: str = "payment_timed_out"
    amount_paise: int = 210000
    customer_name: str = "Aarav Sharma"
    contact: str = "+919812345678"
    email: str = "aarav@example.com"
    language: str = "en"
    method: str = "card"
    issuer: str = "HDFC"
    card_last4: str = "1111"
    order_id: str | None = None
    payday_days: list[int] = []
    cart: list[dict] | None = None
    opted_out: bool = False


class CaptureRequest(BaseModel):
    order_id: str
    amount_paise: int | None = None


@router.post("/sim/inject")
async def inject(body: InjectRequest, request: Request):
    """Synthetic payload, real pipeline: signed, verified, stored, processed."""
    now = clock.now()
    order_id = body.order_id or f"order_live{int(now.timestamp())}"
    cart = body.cart if body.cart is not None else DEFAULT_CART

    event = {
        "entity": "event",
        "event": "payment.failed",
        "created_at": int(now.timestamp()),
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_live{int(now.timestamp())}",
                    "entity": "payment",
                    "order_id": order_id,
                    "amount": body.amount_paise,
                    "currency": "INR",
                    "status": "failed",
                    "method": body.method,
                    "error_code": error_code_for(body.error_reason),
                    "error_description": body.error_reason.replace("_", " "),
                    "error_source": "customer",
                    "error_step": "payment_authentication",
                    "error_reason": body.error_reason,
                    "card": {
                        "last4": body.card_last4,
                        "network": "Visa",
                        "issuer": body.issuer,
                    },
                    "email": body.email,
                    "contact": body.contact,
                    "notes": {
                        "customer_name": body.customer_name,
                        "language": body.language,
                        "payday_days_json": json.dumps(body.payday_days),
                        "cart": json.dumps(cart),
                    },
                }
            }
        },
    }
    result = await _post_webhook(request, event)

    if body.opted_out and result.get("case_id"):
        _opt_out_case_customer(result["case_id"])
    return result


@router.post("/sim/capture")
async def capture(body: CaptureRequest, request: Request, session: Session = Depends(get_session)):
    """The customer paid on their own. Demonstrates the already-paid guard."""
    now = clock.now()
    case = session.scalars(
        select(Case).where(Case.razorpay_order_id == body.order_id).order_by(Case.id.desc())
    ).first()
    event = {
        "entity": "event",
        "event": "payment.captured",
        "created_at": int(now.timestamp()),
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_ok{int(now.timestamp())}",
                    "entity": "payment",
                    "order_id": body.order_id,
                    "amount": body.amount_paise or (case.amount_paise if case else 0),
                    "currency": "INR",
                    "status": "captured",
                }
            }
        },
    }
    return await _post_webhook(request, event)


@router.post("/sim/tamper")
async def tamper(request: Request):
    """A webhook with a bad signature. Rejected and logged -- see /api/events."""
    raw = json.dumps({"event": "payment.failed", "payload": {}}).encode()
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as ac:
        response = await ac.post(
            "/webhooks/razorpay",
            content=raw,
            headers={
                "X-Razorpay-Signature": "0" * 64,
                "Content-Type": "application/json",
            },
        )
    return {"status_code": response.status_code, "detail": response.json().get("detail")}


@router.get("/clock")
def read_clock():
    return _clock_state()


@router.post("/clock/advance")
def advance_clock(body: dict, session: Session = Depends(get_session)):
    """{"days": 3} | {"minutes": 30} | {"to_next_action": true}"""
    if body.get("to_next_action"):
        now = clock.now()
        # Only ever jump *forward*. An action left pending in the past -- an old
        # case, or anything scheduled before the clock was last reset -- is
        # already due, so it needs a tick, not a rewind. Jumping back to it used
        # to drag the clock days backwards, which put every other scheduled
        # action in the far future and made the demo look frozen.
        target = session.scalar(
            select(Action.scheduled_for)
            .join(Case, Case.id == Action.case_id)
            .where(
                Action.status == "pending",
                Case.run_id.is_(None),
                Action.scheduled_for > now,
            )
            .order_by(Action.scheduled_for)
            .limit(1)
        )
        overdue = session.scalar(
            select(func.count())
            .select_from(Action)
            .join(Case, Case.id == Action.case_id)
            .where(
                Action.status == "pending",
                Case.run_id.is_(None),
                Action.scheduled_for <= now,
            )
        )
        if target is None and not overdue:
            return {**_clock_state(), "ran": {}, "note": "nothing scheduled"}
        if target is not None and not overdue:
            clock.jump_to(target)
    else:
        deltas = {k: v for k, v in body.items() if k in {"days", "hours", "minutes", "seconds"}}
        clock.advance(**(deltas or {"minutes": 5}))

    ran = worker.tick(session, clock.now(), run_id=None)
    return {**_clock_state(), "ran": ran}


@router.post("/clock/reset")
def reset_clock():
    clock.reset()
    return _clock_state()


@router.post("/chaos")
def chaos(body: dict):
    """{"razorpay_down": true} | {"llm_down": true} | {"reset": true}"""
    if body.get("reset"):
        razorpay.chaos_down = False
        copy_cache.chaos_copy_down = False
        razorpay.reset_breaker()
    if "razorpay_down" in body:
        razorpay.chaos_down = bool(body["razorpay_down"])
    if "llm_down" in body:
        copy_cache.chaos_copy_down = bool(body["llm_down"])
    return chaos_status()


@router.get("/chaos")
def chaos_status():
    return {
        "razorpay": razorpay.status(),
        "llm_down": copy_cache.chaos_copy_down,
        "llm_configured": LLM_ENABLED,
        "llm_model": LLM_MODEL if LLM_ENABLED else None,
    }


@router.post("/demo/reset")
def demo_reset():
    """Wipe everything and put the clock back. For repeated live demos."""
    reset_database()
    clock.reset()
    razorpay.chaos_down = False
    copy_cache.chaos_copy_down = False
    razorpay.reset_breaker()
    return {"ok": True, **_clock_state()}


async def _post_webhook(request: Request, event: dict) -> dict:
    raw = json.dumps(event).encode()
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as ac:
        response = await ac.post(
            "/webhooks/razorpay",
            content=raw,
            headers={
                "X-Razorpay-Signature": sign(raw),
                "Content-Type": "application/json",
            },
        )
    payload = response.json()
    return {**payload, "sent_through": "/webhooks/razorpay", "signature_verified": True}


def _opt_out_case_customer(case_id: int) -> None:
    from app.db import SessionLocal
    from app.models import Customer

    session = SessionLocal()
    try:
        case = session.get(Case, case_id)
        customer = session.get(Customer, case.customer_id) if case else None
        if customer:
            customer.opted_out = True
            customer.opted_out_at = clock.now()
            session.commit()
    finally:
        session.close()


def _clock_state() -> dict:
    now = clock.now()
    return {
        "now": iso(now),
        "display": now.strftime("%a %d %b %Y, %H:%M"),
        "frozen": clock.is_frozen,
        "offset_seconds": int(clock.offset.total_seconds()),
    }
