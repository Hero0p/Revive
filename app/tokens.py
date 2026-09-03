"""Signed resume tokens. The message links to the merchant's own page, and
that page has to prove the link was issued by us."""

import base64
import hmac
from datetime import datetime, timedelta
from hashlib import sha256

from app.config import RESUME_TOKEN_SECRET

TOKEN_TTL = timedelta(hours=24)


def make_token(order_id: str, issued_at: datetime) -> str:
    expires = issued_at + TOKEN_TTL
    payload = f"{order_id}:{int(expires.timestamp())}"
    signature = hmac.new(
        RESUME_TOKEN_SECRET.encode(), payload.encode(), sha256
    ).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode()).decode().rstrip("=")


def verify_token(token: str, order_id: str, now: datetime) -> tuple[bool, str]:
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
        token_order, expires_raw, signature = decoded.rsplit(":", 2)
    except (ValueError, UnicodeDecodeError):
        return False, "malformed token"

    payload = f"{token_order}:{expires_raw}"
    expected = hmac.new(
        RESUME_TOKEN_SECRET.encode(), payload.encode(), sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(expected, signature):
        return False, "bad signature"
    if token_order != order_id:
        return False, "token is for a different order"
    if now.timestamp() > int(expires_raw):
        return False, "link expired"
    return True, "ok"
