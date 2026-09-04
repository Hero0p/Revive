"""Policy runs, metrics, and the sensitivity sweep.

A 3,000-case comparison takes minutes, which is longer than any HTTP request
between a browser and this service is allowed to live: a hosting proxy returns
502 long before the work finishes, and the browser gives up before that. So a
run is started in a background thread and the request returns immediately.
The dashboard polls /api/runs/status until it finishes.

A thread rather than a FastAPI BackgroundTask on purpose: run_policy is
synchronous and CPU-bound, so awaiting it on the event loop would freeze every
other request for the whole run, including the platform's health check.
"""

import json
import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import simulator
from app.clock import iso
from app.config import DEMO_SEED_COUNT, SEED
from app.db import SessionLocal, get_session
from app.models import Case, RunMetric

router = APIRouter(prefix="/api")

# One job at a time. Two concurrent comparisons would fight over the same run
# ids and, on a small instance, over the CPU.
_job_lock = threading.Lock()
_job: dict = {"state": "idle"}


def _snapshot() -> dict:
    job = dict(_job)
    if job.get("started_at_monotonic"):
        end = job.get("finished_at_monotonic") or time.monotonic()
        job["elapsed_seconds"] = round(end - job.pop("started_at_monotonic"), 1)
    job.pop("started_at_monotonic", None)
    job.pop("finished_at_monotonic", None)
    return job


def _start_job(label: str, work) -> dict:
    """Run `work(session)` on a background thread with its own session."""
    with _job_lock:
        if _job.get("state") == "running":
            raise HTTPException(
                409, f"{_job.get('label')} is still running -- poll /api/runs/status"
            )
        _job.clear()
        _job.update(
            state="running",
            label=label,
            step=None,
            result=None,
            error=None,
            started_at_monotonic=time.monotonic(),
        )

    def target():
        session = SessionLocal()
        try:
            result = work(session)
            with _job_lock:
                _job.update(state="done", result=result)
        except Exception as exc:  # noqa: BLE001 -- surfaced through the status endpoint
            with _job_lock:
                _job.update(state="error", error=f"{type(exc).__name__}: {exc}")
        finally:
            session.close()
            with _job_lock:
                _job["finished_at_monotonic"] = time.monotonic()

    threading.Thread(target=target, daemon=True, name=f"job:{label}").start()
    return _snapshot()


def _set_step(step: str) -> None:
    with _job_lock:
        _job["step"] = step


@router.get("/runs/status")
def run_status():
    """What the background job is doing. The dashboard polls this."""
    return _snapshot()


def seed_demo_if_empty() -> bool:
    """Populate an empty database with a small comparison, in the background.

    Called once at startup. A deployed instance begins with nothing in it, so
    without this the Cases, Outbox and Audit screens are empty until somebody
    waits out a full run. Returns whether a seed run was started.
    """
    if DEMO_SEED_COUNT <= 0:
        return False

    session = SessionLocal()
    try:
        already_has_cases = session.scalar(select(func.count()).select_from(Case))
    finally:
        session.close()
    if already_has_cases:
        return False

    def work(job_session: Session) -> dict:
        run_ids = []
        for index, policy in enumerate(("baseline", "router"), start=1):
            _set_step(f"{policy} ({index} of 2)")
            run_ids.append(
                simulator.run_policy(
                    job_session, policy, count=DEMO_SEED_COUNT, seed=SEED
                )
            )
        return {"run_ids": run_ids, "seeded": True}

    try:
        _start_job(f"seeding {DEMO_SEED_COUNT} demo cases", work)
    except HTTPException:
        return False  # something else is already running; it will fill the database
    return True


class RunRequest(BaseModel):
    policy: str = "router"  # baseline | router | both
    count: int = 200
    seed: int = 42
    message_budget: int | None = None
    use_llm: bool = False


@router.post("/runs", status_code=202)
def create_run(body: RunRequest):
    """Starts the comparison and returns immediately. Poll /api/runs/status."""
    policies = ["baseline", "router"] if body.policy == "both" else [body.policy]
    if any(p not in {"baseline", "router"} for p in policies):
        raise HTTPException(400, "policy must be baseline, router, or both")

    def work(session: Session) -> dict:
        run_ids = []
        for index, policy in enumerate(policies, start=1):
            _set_step(f"{policy} ({index} of {len(policies)})")
            run_ids.append(
                simulator.run_policy(
                    session,
                    policy,
                    count=body.count,
                    seed=body.seed,
                    message_budget=body.message_budget,
                    use_llm=body.use_llm,
                )
            )
        return {
            "run_ids": run_ids,
            "metrics": [simulator.compute_metrics(session, rid) for rid in run_ids],
        }

    return _start_job(f"{body.count} failures, seed {body.seed}", work)


@router.get("/runs")
def list_runs(session: Session = Depends(get_session)):
    rows = list(session.scalars(select(RunMetric).order_by(RunMetric.id.desc())).all())
    return {"runs": [_metric_row(r) for r in rows]}


@router.get("/runs/{run_id}/by-cause")
def by_cause(run_id: str, session: Session = Depends(get_session)):
    """Recovery broken down by failure cause. Drives the overview chart."""
    from app.models import Action, Case

    cases = list(session.scalars(select(Case).where(Case.run_id == run_id)).all())
    by_id = {c.id: c for c in cases}
    actions = [a for a in session.scalars(select(Action)).all() if a.case_id in by_id]

    buckets: dict[str, dict] = {}
    for case in cases:
        bucket = buckets.setdefault(
            case.root_cause or "unknown",
            {"root_cause": case.root_cause or "unknown", "cases": 0,
             "at_risk_paise": 0, "recovered_paise": 0, "messages": 0},
        )
        bucket["cases"] += 1
        bucket["at_risk_paise"] += case.amount_paise or 0
        if case.status == "recovered" and case.recovery_mode != "self_recovered":
            bucket["recovered_paise"] += case.amount_recovered_paise or 0

    for action in actions:
        if action.status != "sent":
            continue
        cause = by_id[action.case_id].root_cause or "unknown"
        if cause in buckets:
            buckets[cause]["messages"] += 1

    rows = sorted(buckets.values(), key=lambda b: -b["at_risk_paise"])
    for row in rows:
        row["recovery_rate"] = (
            round(row["recovered_paise"] / row["at_risk_paise"], 4) if row["at_risk_paise"] else 0
        )
    return {"run_id": run_id, "causes": rows}


@router.get("/runs/compare/{seed}/{count}")
def compare(seed: int, count: int, session: Session = Depends(get_session)):
    """The two metric tables side by side, facts first."""
    out = {}
    for policy in ("baseline", "router"):
        run_id = f"{policy}-s{seed}-n{count}"
        row = session.scalar(select(RunMetric).where(RunMetric.run_id == run_id))
        out[policy] = (
            {**simulator.compute_metrics(session, run_id), "policy": policy} if row else None
        )
    return {"seed": seed, "count": count, "policies": out}


@router.post("/runs/sweep", status_code=202)
def sweep(body: RunRequest):
    """~45 parameter settings. Reports how many settings Revive wins.

    Backgrounded for the same reason as a run: re-scoring 45 settings over a
    3,000-case batch outlives the request. The claim is directional
    robustness, not one number."""

    def work(session: Session) -> dict:
        _set_step("re-scoring 45 parameter settings")
        result = simulator.sensitivity_sweep(seed=body.seed, count=body.count)
        for policy in ("baseline", "router"):
            run_id = f"{policy}-s{body.seed}-n{body.count}"
            row = session.scalar(select(RunMetric).where(RunMetric.run_id == run_id))
            if row is not None:
                existing = json.loads(row.sweep_json or "{}")
                row.sweep_json = json.dumps({**existing, "sweep": result})
        session.commit()
        return result

    return _start_job(f"sensitivity sweep, seed {body.seed}", work)


def _metric_row(row: RunMetric) -> dict:
    at_risk = row.amount_at_risk_paise or 0
    return {
        "run_id": row.run_id,
        "policy": row.policy,
        "case_count": row.case_count,
        "messages_sent": row.messages_sent,
        "wrong_advice_count": row.wrong_advice_count,
        "already_paid_contacts": row.already_paid_contacts,
        "suppressed_count": row.suppressed_count,
        "amount_at_risk_paise": at_risk,
        "amount_recovered_paise": row.amount_recovered_paise,
        "recovery_rate": round((row.amount_recovered_paise or 0) / at_risk, 4) if at_risk else 0,
        "seed": row.seed,
        "extra": json.loads(row.sweep_json or "{}"),
        "created_at": iso(row.created_at),
    }
