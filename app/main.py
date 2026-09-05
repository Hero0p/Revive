"""FastAPI app: routes, startup, and the background worker."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import copy_cache, worker
from app.clock import clock, iso
from app.config import (
    CORS_ORIGINS,
    DELIVERY_ALLOWLIST,
    DELIVER_FOR_REAL,
    DEMO_SEED_COUNT,
    EMAIL_FROM_ADDRESS,
    EMAIL_FROM_IS_PUBLIC_MAILBOX,
    LIVE_RAZORPAY,
    LLM_ENABLED,
    LLM_MODEL,
    PUBLIC_BASE_URL,
    PUBLIC_BASE_URL_IS_LOCAL,
)
from app.db import create_all
from app.razorpay_client import client
from app.routes import cases, orders, runs, sim, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_all()

    # Load the pre-written copy. A shortfall is printed loudly rather than
    # left to be noticed in a customer's inbox: every message silently falling
    # back to the eight hand-written templates looks, from the outside,
    # exactly like a table that is working.
    cov = copy_cache.load()
    if cov["error"]:
        print(f"[startup] WARNING: copy table unavailable ({cov['error']}) -- every "
              "message will use the hand-written templates")
    elif not cov["complete"]:
        print(f"[startup] WARNING: copy coverage {cov['percent']}% "
              f"({cov['cells_loaded']}/{cov['cells_expected']} cells)")
    else:
        print(f"[startup] copy table loaded: {cov['entries']} entries, "
              f"{cov['cells_expected']} cells, {cov['prompt_version']}")
    # An empty database gets a small comparison in the background, so the
    # Cases, Outbox and Audit screens have real rows the moment anyone looks.
    # Returns immediately; the run happens on its own thread.
    if runs.seed_demo_if_empty():
        print(f"[startup] empty database -- seeding {DEMO_SEED_COUNT} demo cases in the background")
    if DELIVER_FOR_REAL and not DELIVERY_ALLOWLIST:
        # Legitimate for a demo that has to reach an arbitrary address, but it
        # is the one configuration where a wrong address in a case becomes a
        # real message to a real stranger. Synthetic runs are still refused
        # outright by delivery.py, so a comparison can never mail anyone.
        print(
            "[startup] WARNING: real delivery is on with an empty "
            "DELIVERY_ALLOWLIST -- every live case can email whoever its "
            "customer record names."
        )
    if DELIVER_FOR_REAL and EMAIL_FROM_IS_PUBLIC_MAILBOX:
        # Resend rejects this with "The gmail.com domain is not verified",
        # which sounds like the sending domain failed verification rather than
        # like the wrong address is set. Say what it actually means.
        print(
            f"[startup] WARNING: EMAIL_FROM_ADDRESS is {EMAIL_FROM_ADDRESS!r}. "
            "No one can verify a public mailbox domain, so every send will be "
            "rejected. Set it to an address on a domain you verified with "
            "Resend, or unset it to use the default."
        )
    if DELIVER_FOR_REAL and PUBLIC_BASE_URL_IS_LOCAL:
        # Every message links to PUBLIC_BASE_URL. Sending real email whose only
        # link points at localhost delivers a message the recipient cannot act
        # on, and nothing downstream can tell that has happened.
        print(
            "[startup] WARNING: real delivery is on but PUBLIC_BASE_URL is "
            f"{PUBLIC_BASE_URL!r}. Recovery messages will link somewhere the "
            "recipient cannot open. Set PUBLIC_BASE_URL to the public address."
        )
    task = asyncio.create_task(worker.worker_loop())
    yield
    task.cancel()


app = FastAPI(title="Revive", version="1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhooks.router)
app.include_router(cases.router)
app.include_router(sim.router)
app.include_router(runs.router)
app.include_router(orders.router)

@app.get("/")
@app.get("/api/health")
def health():
    return {
        "ok": True,
        "clock": iso(clock.now()),
        "razorpay": client.status(),
        "razorpay_live": LIVE_RAZORPAY,
        "llm_enabled": LLM_ENABLED,
        "llm_model": LLM_MODEL if LLM_ENABLED else "templates only",
        "copy": copy_cache.coverage(),
        "public_base_url": PUBLIC_BASE_URL,
    }
