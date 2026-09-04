"""Order creation and the resume page.

The resume page is the whole trust design. A bare payment link looks exactly
like a scam; a page on the merchant's own domain showing the customer's real
cart, the original amount, and the standard Razorpay checkout does not.
"""

import hashlib
import hmac
import html
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ingest
from app.clock import clock
from app.config import LIVE_RAZORPAY, RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET
from app.db import get_session
from app.messages import MERCHANT_NAME, format_rupees
from app.models import Case, Customer
from app.razorpay_client import RazorpayDown, client
from app.tokens import verify_token

router = APIRouter()

MIN_AMOUNT_PAISE = 100


class OrderRequest(BaseModel):
    amount_paise: int = 210000
    receipt: str | None = None
    cart: list[dict] = []
    customer_name: str = "Aarav Sharma"
    contact: str = "+919812345678"
    email: str = "aarav@example.com"


class VerifyRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@router.post("/api/orders")
def create_order(body: OrderRequest):
    """Create a real test-mode order for the checkout modal.

    The key secret never leaves the backend; the browser gets only key_id.
    """
    if body.amount_paise < MIN_AMOUNT_PAISE:
        raise HTTPException(400, f"amount must be at least {MIN_AMOUNT_PAISE} paise")

    receipt = body.receipt or f"rcpt_{int(clock.now().timestamp())}"
    try:
        order = client.create_order(
            amount_paise=body.amount_paise,
            receipt=receipt,
            notes={
                "cart": json.dumps(body.cart),
                "customer_name": body.customer_name,
                # Carried so the failure arrives with the address the customer
                # actually typed here, not whatever Razorpay's modal auto-fills
                # for a returning phone number. See find_or_create_customer.
                "email": body.email,
                "contact": body.contact,
            },
        )
    except RazorpayDown as exc:
        detail = str(exc)
        # Bad or missing keys are a configuration problem, not an outage.
        status = 401 if "authentication" in detail.lower() or "401" in detail else 500
        raise HTTPException(status, f"could not create the order: {detail}") from exc

    return {
        "order": order,
        "key_id": RAZORPAY_KEY_ID if LIVE_RAZORPAY else None,
        "live": LIVE_RAZORPAY,
    }


@router.post("/api/verify-payment")
def verify_payment(body: VerifyRequest, session: Session = Depends(get_session)):
    """Verify a checkout callback signature.

    This is NOT the webhook algorithm. Here the signed payload is
    "order_id|payment_id" and the key is the API key secret; a webhook signs
    the raw request body with the webhook secret. Using one for the other
    fails silently forever, so they stay deliberately separate.
    """
    payload = f"{body.razorpay_order_id}|{body.razorpay_payment_id}"
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, body.razorpay_signature):
        # Do not mark anything as paid.
        raise HTTPException(400, "signature mismatch")

    # A verified success is the already-paid signal, so it goes through the
    # same handler a capture webhook uses -- one code path, not two.
    now = clock.now()
    case = ingest.handle_success(
        session,
        {
            "id": body.razorpay_payment_id,
            "order_id": body.razorpay_order_id,
            "amount": None,
        },
        now,
    )
    session.commit()

    return {
        "verified": True,
        "order_id": body.razorpay_order_id,
        "payment_id": body.razorpay_payment_id,
        "case_id": case.id if case else None,
        "case_status": case.status if case else None,
    }


@router.post("/api/razorpay/sync")
async def sync_payments(request: Request, count: int = 25):
    """Pull real payments from Razorpay and feed the failures into the pipeline.

    Razorpay delivers webhooks only to a public URL. This is the documented
    fallback: it polls the Payments API and pushes each failed payment through
    the same signed /webhooks/razorpay route, so a real failure reaches the
    decision table from localhost with no tunnel running.
    """
    if not LIVE_RAZORPAY:
        raise HTTPException(400, "no Razorpay keys configured")

    from app.db import SessionLocal
    from app.routes.sim import _post_webhook

    try:
        payments = client.fetch_payments(count=count)
    except RazorpayDown as exc:
        raise HTTPException(502, f"could not reach Razorpay: {exc}") from exc

    session = SessionLocal()
    try:
        seen = {
            c.razorpay_payment_id
            for c in session.scalars(select(Case)).all()
            if c.razorpay_payment_id
        }
    finally:
        session.close()

    ingested, skipped, results = 0, 0, []
    for payment in payments:
        status = payment.get("status")
        if status == "failed" and payment.get("id") not in seen:
            result = await _post_webhook(
                request,
                {
                    "entity": "event",
                    "event": "payment.failed",
                    "created_at": payment.get("created_at"),
                    "payload": {"payment": {"entity": payment}},
                },
            )
            ingested += 1
            results.append(
                {
                    "payment_id": payment.get("id"),
                    "error_reason": payment.get("error_reason"),
                    "case_id": result.get("case_id"),
                    "case_status": result.get("status"),
                }
            )
        elif status == "captured" and payment.get("order_id"):
            # A success closes any open case for that order.
            await _post_webhook(
                request,
                {
                    "entity": "event",
                    "event": "payment.captured",
                    "created_at": payment.get("created_at"),
                    "payload": {"payment": {"entity": payment}},
                },
            )
        else:
            skipped += 1

    return {
        "fetched": len(payments),
        "ingested_failures": ingested,
        "skipped": skipped,
        "cases": results,
    }


@router.get("/orders/{order_id}/resume", response_class=HTMLResponse)
def resume(order_id: str, request: Request, session: Session = Depends(get_session)):
    token = request.query_params.get("token", "")
    now = clock.now()
    valid, reason = verify_token(token, order_id, now)

    case = session.scalars(
        select(Case).where(Case.razorpay_order_id == order_id).order_by(Case.id.desc())
    ).first()

    if not valid:
        return HTMLResponse(_page(_notice("This link is not valid", reason)), status_code=400)
    if case is None:
        return HTMLResponse(
            _page(_notice("Order not found", f"No saved cart for {order_id}.")), status_code=404
        )

    customer = session.get(Customer, case.customer_id) if case.customer_id else None
    if case.status == "recovered":
        return HTMLResponse(
            _page(
                _notice(
                    "This order is already paid",
                    f"Order {html.escape(order_id)} was completed. Nothing further is needed.",
                )
            )
        )

    return HTMLResponse(_page(_resume_body(case, customer)))


def _resume_body(case: Case, customer: Customer | None) -> str:
    try:
        items = json.loads(case.cart_json or "[]")
    except (ValueError, TypeError):
        items = []

    rows = "".join(
        f"<tr><td>{html.escape(str(item.get('name', 'Item')))}</td>"
        f"<td class='num'>{format_rupees(item.get('price_paise', 0))}</td></tr>"
        for item in items
    )
    name = html.escape((customer.name or "").split()[0]) if customer and customer.name else "there"
    attempt = case.failed_at.strftime("%d %b, %H:%M") if case.failed_at else "earlier"
    last4 = f" ending {html.escape(case.card_last4)}" if case.card_last4 else ""

    if LIVE_RAZORPAY:
        button = f"""
        <button id="pay" class="pay">Complete payment</button>
        <script src="https://checkout.razorpay.com/v1/checkout.js"></script>
        <script>
          document.getElementById('pay').onclick = function () {{
            new Razorpay({{
              key: "{RAZORPAY_KEY_ID}",
              amount: {case.amount_paise or 0},
              currency: "INR",
              name: "{MERCHANT_NAME}",
              description: "Order {html.escape(case.razorpay_order_id or '')}",
              prefill: {{ name: "{html.escape(customer.name if customer else '')}",
                          contact: "{html.escape(customer.contact if customer else '')}",
                          email: "{html.escape(customer.email if customer else '')}" }},
              theme: {{ color: "#0F7B4F" }}
            }}).open();
          }};
        </script>"""
    else:
        button = f"""
        <button id="pay" class="pay">Complete payment</button>
        <p class="sim">Simulated checkout - no Razorpay keys are configured, so this
        marks the order paid through the same webhook path a real capture uses.</p>
        <script>
          document.getElementById('pay').onclick = async function () {{
            this.disabled = true; this.textContent = 'Processing...';
            await fetch('/api/sim/capture', {{
              method: 'POST', headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{order_id: "{html.escape(case.razorpay_order_id or '')}"}})
            }});
            location.reload();
          }};
        </script>"""

    return f"""
    <p class="hello">Hi {name}, your cart is saved.</p>
    <p class="meta">Order {html.escape(case.razorpay_order_id or '')} &middot;
       payment attempted {attempt} with the card{last4}</p>
    <table>{rows}
      <tr class="total"><td>Total</td>
      <td class="num">{format_rupees(case.amount_paise)}</td></tr>
    </table>
    <p class="assure">This is the same total as your original order. Nothing has
       been charged yet, and no card details are stored on this page.</p>
    {button}
    """


def _notice(title: str, detail: str) -> str:
    return f"<p class='hello'>{html.escape(title)}</p><p class='meta'>{html.escape(detail)}</p>"


def _page(body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{MERCHANT_NAME} - complete your order</title>
<style>
  body {{ background:#FBFBF9; color:#16181C; margin:0;
         font:15px/1.55 ui-sans-serif, system-ui, -apple-system, sans-serif; }}
  .card {{ max-width:520px; margin:48px auto; padding:32px;
           background:#fff; border:1px solid #E4E4DF; border-radius:6px; }}
  .brand {{ font-weight:600; letter-spacing:-0.01em; margin:0 0 24px; }}
  .hello {{ font-size:20px; font-weight:600; margin:0 0 6px; letter-spacing:-0.01em; }}
  .meta {{ color:#6E7480; margin:0 0 24px; font-size:13px; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:20px;
           font-variant-numeric:tabular-nums; }}
  td {{ padding:9px 0; border-bottom:1px solid #E4E4DF; }}
  .num {{ text-align:right; }}
  .total td {{ font-weight:600; border-bottom:none; }}
  .assure {{ color:#6E7480; font-size:13px; margin:0 0 24px; }}
  .pay {{ width:100%; padding:13px; background:#0F7B4F; color:#fff; border:0;
          border-radius:5px; font-size:15px; font-weight:500; cursor:pointer; }}
  .pay:hover {{ background:#0c6641; }}
  .sim {{ color:#6E7480; font-size:12px; margin-top:14px; }}
</style></head>
<body><div class="card"><p class="brand">{MERCHANT_NAME}</p>{body}</div></body></html>"""
