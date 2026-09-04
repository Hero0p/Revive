"""Policy runs, metrics, and the sensitivity sweep."""

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import simulator
from app.clock import iso
from app.db import get_session
from app.models import RunMetric

router = APIRouter(prefix="/api")


class RunRequest(BaseModel):
    policy: str = "router"  # baseline | router | both
    count: int = 200
    seed: int = 42
    message_budget: int | None = None
    use_llm: bool = False


@router.post("/runs")
def create_run(body: RunRequest, session: Session = Depends(get_session)):
    policies = ["baseline", "router"] if body.policy == "both" else [body.policy]
    if any(p not in {"baseline", "router"} for p in policies):
        raise HTTPException(400, "policy must be baseline, router, or both")

    run_ids = []
    for policy in policies:
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


@router.post("/runs/sweep")
def sweep(body: RunRequest, session: Session = Depends(get_session)):
    """~45 parameter settings. Reports how many the router wins.

    The claim is directional robustness, not one number."""
    result = simulator.sensitivity_sweep(seed=body.seed, count=body.count)
    for policy in ("baseline", "router"):
        run_id = f"{policy}-s{body.seed}-n{body.count}"
        row = session.scalar(select(RunMetric).where(RunMetric.run_id == run_id))
        if row is not None:
            existing = json.loads(row.sweep_json or "{}")
            row.sweep_json = json.dumps({**existing, "sweep": result})
    session.commit()
    return result


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
