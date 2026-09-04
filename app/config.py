"""Environment configuration. Everything has a working default except the
Razorpay credentials, which the system runs without in simulation mode."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "dev-webhook-secret")
RESUME_TOKEN_SECRET = os.getenv("RESUME_TOKEN_SECRET", "dev-resume-secret")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:5173").rstrip("/")

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'recovery.db'}")

# Browser origins allowed to call the API. The local dev server is always
# allowed; a deployed frontend on another origin (Vercel) has to be named here.
# Not needed when the frontend proxies /api to the backend on its own domain,
# which is what vercel.json does -- the browser then sees one origin.
CORS_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
] + ["http://localhost:5173", "http://127.0.0.1:5173"]

LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-20b")
# Only meaningful for reasoning models. Blank it when pointing LLM_MODEL at a
# model that rejects the parameter.
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "low")
# A 2-second budget. Anything slower falls back to the hand-written template,
# which is a supported mode rather than a failure. Measured on this prompt:
# 0.3-1.1s, so the budget is comfortable rather than tight.
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "2.0"))
LLM_ENABLED = bool(GROQ_API_KEY)

SEED = 42
LIVE_RAZORPAY = bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)

# The gate's per-customer contact cap, in hours. 24 is the product default and
# what every published result uses -- one message per customer per day, across
# all of their cases, is the whole anti-spam position.
#
# It is configurable only because repeated live test checkouts all share one
# phone number, so they are all one customer, and every checkout after the
# first is correctly deferred a full day. Set it to 0 to demo back-to-back
# checkouts. Leave it alone for anything you intend to quote.
MIN_HOURS_BETWEEN_CONTACTS = float(os.getenv("MIN_HOURS_BETWEEN_CONTACTS", "24"))

# --- Real delivery -------------------------------------------------------
# Off by default, and deliberately so: with this on, advancing the clock sends
# real messages to real people. Synthetic runs never deliver regardless.
DELIVER_FOR_REAL = os.getenv("DELIVER_FOR_REAL", "false").lower() == "true"

# Comma-separated. When non-empty, only these recipients can ever be contacted.
# Keep your own address here during a demo -- it is the difference between a
# bug costing nothing and a bug texting a stranger.
DELIVERY_ALLOWLIST = [
    entry.strip().lower()
    for entry in os.getenv("DELIVERY_ALLOWLIST", "").split(",")
    if entry.strip()
]

# Gmail: turn on 2-step verification, then create an App Password.
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Blue Tokai Coffee")

EMAIL_CONFIGURED = bool(SMTP_USER and SMTP_APP_PASSWORD)
