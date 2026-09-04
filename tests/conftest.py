"""Point the tests at a throwaway database before anything imports config."""

import os
from pathlib import Path

# Deliberately not tempfile.gettempdir(): that resolves to the system temp
# directory, which on a dev machine with a full system drive raises
# "database or disk is full" for a database that is a few KB. Keeping it
# beside the project ties it to whichever drive the project itself is on.
TEST_DB = Path(__file__).resolve().parents[1] / ".pytest_tmp" / "revive_test.db"
TEST_DB.parent.mkdir(exist_ok=True)
if TEST_DB.exists():
    TEST_DB.unlink()

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["RESUME_TOKEN_SECRET"] = "test-resume-secret"
os.environ["GROQ_API_KEY"] = ""  # templates only: the tests never hit the network

# Hermetic on purpose. A real .env would otherwise put the suite into live mode
# and have it create payment links against the actual Razorpay account. An
# empty key id keeps LIVE_RAZORPAY false; the secret is still needed because
# checkout-callback signatures are signed with it.
os.environ["RAZORPAY_KEY_ID"] = ""
os.environ["RAZORPAY_KEY_SECRET"] = "test-key-secret"

# Same reasoning for delivery: a real .env would otherwise decide the outcome
# of these tests, and a real allowlist would make them fail for the wrong
# reason. Each test opts into what it needs with monkeypatch.
for _key in ("DELIVERY_ALLOWLIST", "RESEND_API_KEY", "EMAIL_FROM_ADDRESS"):
    os.environ[_key] = ""
os.environ["DELIVER_FOR_REAL"] = "false"

# Pinned to the product default. This one is loosened to 0 in a local .env to
# demo back-to-back checkouts through a single phone number, and the gate tests
# assert the shipped 24-hour behaviour -- they must not start passing or
# failing based on how someone last set up their demo.
os.environ["MIN_HOURS_BETWEEN_CONTACTS"] = "24"
