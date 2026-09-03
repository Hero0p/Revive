"""FastAPI app: routes, startup, and the background worker."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import worker
from app.clock import clock, iso
from app.config import LIVE_RAZORPAY, LLM_ENABLED, LLM_MODEL, PUBLIC_BASE_URL
from app.db import create_all
from app.razorpay_client import client
from app.routes import cases, orders, runs, sim, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_all()
    task = asyncio.create_task(worker.worker_loop())
    yield
    task.cancel()


app = FastAPI(title="Recovery Router", version="1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhooks.router)
app.include_router(cases.router)
app.include_router(sim.router)
app.include_router(runs.router)
app.include_router(orders.router)


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
