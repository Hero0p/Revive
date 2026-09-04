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
# The domain every recovery message links to.
#
# Falls back to RENDER_EXTERNAL_URL, which Render sets on its own, before
# falling back to localhost -- a message whose only link points at localhost is
# useless to whoever receives it, and that is exactly what a deployed instance
# produced before this. Set PUBLIC_BASE_URL explicitly to the dashboard's own
# domain; the Render fallback only exists so a deployment is never silently
# broken, and the localhost default keeps local development pointing locally.
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or "http://localhost:5173"
).rstrip("/")

PUBLIC_BASE_URL_IS_LOCAL = any(
    host in PUBLIC_BASE_URL for host in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'recovery.db'}")

# Browser origins allowed to call the API. The local dev server is always
# allowed; a deployed frontend on another origin (Vercel) has to be named here.
# Not needed when the frontend proxies /api to the backend on its own domain,
# which is what vercel.json does -- the browser then sees one origin.
CORS_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
] + [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://revive-eight-orpin.vercel.app",
]

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

# On startup, if the database holds no cases at all, run a small comparison in
# the background so the Cases, Outbox and Audit screens have real rows to show.
#
# A deployed instance starts empty every time -- the disk is ephemeral, and a
# free one is rebuilt whenever it wakes from idle -- so without this the first
# visitor can read the Overview (which ships with a committed run) but finds
# every other screen blank. Small on purpose: this must not tie up a small
# instance for minutes. Set to 0 to disable.
DEMO_SEED_COUNT = int(os.getenv("DEMO_SEED_COUNT", "200"))

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

# --- Email transport -----------------------------------------------------
# Brevo's transactional API over HTTPS.
#
# SMTP is not an option here. Render, like most hosting platforms, blocks
# outbound SMTP ports (25/465/587) to keep spammers off its address space, so
# a deployed instance failed every send with "[Errno 101] Network is
# unreachable" however correct the credentials were. An HTTPS API on port 443
# is never blocked.
#
# Brevo specifically because its free tier verifies a single *sender address*
# rather than a whole domain, so mail can go to real recipients without owning
# one. Verify the address you put in EMAIL_FROM_ADDRESS from Brevo's Senders
# page before the first send, or the API rejects it.
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
# Must be an address verified on the Brevo account. No default: a wrong sender
# fails at send time with a message nobody expects, and a placeholder here
# would make that more likely, not less.
EMAIL_FROM_ADDRESS = os.getenv("EMAIL_FROM_ADDRESS", "")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "Blue Tokai Coffee")

EMAIL_CONFIGURED = bool(BREVO_API_KEY and EMAIL_FROM_ADDRESS)
