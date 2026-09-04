"""FastAPI app: routes, startup, and the background worker."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import worker
from app.clock import clock, iso
from app.config import (
    CORS_ORIGINS,
    DEMO_SEED_COUNT,
    LIVE_RAZORPAY,
    LLM_ENABLED,
    LLM_MODEL,
    PUBLIC_BASE_URL,
)
from app.db import create_all
from app.razorpay_client import client
from app.routes import cases, orders, runs, sim, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_all()
    # An empty database gets a small comparison in the background, so the
    # Cases, Outbox and Audit screens have real rows the moment anyone looks.
    # Returns immediately; the run happens on its own thread.
    if runs.seed_demo_if_empty():
        print(f"[startup] empty database -- seeding {DEMO_SEED_COUNT} demo cases in the background")
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
        "public_base_url": PUBLIC_BASE_URL,
    }
