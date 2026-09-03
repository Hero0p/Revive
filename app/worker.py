"""Polls for due actions and runs them.

In the demo the clock is what moves, not wall time, so /api/clock/advance calls
tick() directly and the run is instant. The background loop exists for the live
webhook path.
"""

import asyncio
from collections import Counter
from datetime import datetime

from sqlalchemy.orm import Session

from app import executor, gate, outbox
from app.clock import clock
from app.db import SessionLocal

POLL_SECONDS = 2.0


def tick(
    session: Session,
    now: datetime,
    *,
    run_id: str | None = None,
    budget: gate.Budget | None = None,
    gate_mode: str = "full",
    use_llm: bool = True,
    real_links: bool = True,
) -> dict:
    """Run everything that is due. Returns a count of outcomes."""
    outcomes: Counter[str] = Counter()
    for action in outbox.due_actions(session, now, run_id):
        outcome = executor.execute(
            session,
            action,
            now,
            budget=budget,
            gate_mode=gate_mode,
            use_llm=use_llm,
            real_links=real_links,
        )
        outcomes[outcome] += 1
        if outcome == "halted":
            outcomes["halted_run"] = 1
            break
    session.commit()
    return dict(outcomes)


async def worker_loop() -> None:
    """Live cases only. Simulation runs drive tick() themselves."""
    while True:
        try:
            session = SessionLocal()
            try:
                tick(session, clock.now(), run_id=None)
            finally:
                session.close()
        except Exception as exc:  # noqa: BLE001 -- a worker crash must not kill the app
            print(f"[worker] {type(exc).__name__}: {exc}")
        await asyncio.sleep(POLL_SECONDS)
