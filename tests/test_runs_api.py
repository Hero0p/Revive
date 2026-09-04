"""A comparison run must not happen inside the HTTP request.

At 3,000 cases a run takes minutes. Deployed behind a hosting proxy that was
exactly the bug: the proxy returned 502 long before the work finished and the
dashboard could never start a comparison at all. So the endpoint starts the run
on a background thread, answers immediately, and the dashboard polls.
"""

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal, create_all, reset_database
from app.main import app
from app.models import Case, RunMetric
from app.routes import runs as runs_route


@pytest.fixture(autouse=True)
def fresh_db():
    create_all()
    reset_database()
    runs_route._job.clear()
    runs_route._job.update(state="idle")
    yield
    # Never leave a thread writing into a database the next test drops.
    _drain()


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def _drain(timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = runs_route._snapshot()
        if state.get("state") != "running":
            return state
        time.sleep(0.1)
    raise AssertionError("the background run never finished")


class TestTheRunIsBackgrounded:
    def test_starting_a_run_answers_immediately(self, api):
        started = time.time()
        response = api.post("/api/runs", json={"policy": "both", "count": 30, "seed": 3})
        elapsed = time.time() - started

        assert response.status_code == 202
        # The whole point. A synchronous run of even 30 cases takes longer.
        assert elapsed < 1.0
        assert response.json()["state"] == "running"

    def test_the_api_stays_answerable_while_a_run_is_going(self, api):
        api.post("/api/runs", json={"policy": "both", "count": 30, "seed": 3})
        assert api.get("/api/health").status_code == 200
        assert api.get("/api/runs/status").status_code == 200

    def test_the_run_finishes_and_stores_its_metrics(self, api):
        api.post("/api/runs", json={"policy": "both", "count": 30, "seed": 3})
        final = _drain()

        assert final["state"] == "done", final.get("error")
        assert final["result"]["run_ids"] == ["baseline-s3-n30", "router-s3-n30"]

        session = SessionLocal()
        stored = {r.run_id for r in session.scalars(select(RunMetric)).all()}
        session.close()
        assert stored == {"baseline-s3-n30", "router-s3-n30"}

    def test_a_second_run_is_refused_while_one_is_going(self, api):
        api.post("/api/runs", json={"policy": "both", "count": 30, "seed": 3})
        second = api.post("/api/runs", json={"policy": "both", "count": 30, "seed": 3})
        assert second.status_code == 409

    def test_a_bad_policy_is_still_rejected_before_anything_starts(self, api):
        response = api.post("/api/runs", json={"policy": "nonsense", "count": 30})
        assert response.status_code == 400
        assert runs_route._snapshot()["state"] == "idle"

    def test_a_failing_run_reports_the_error_instead_of_hanging(self, api, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("simulator exploded")

        monkeypatch.setattr(runs_route.simulator, "run_policy", explode)
        api.post("/api/runs", json={"policy": "both", "count": 30, "seed": 3})
        final = _drain()

        assert final["state"] == "error"
        assert "simulator exploded" in final["error"]


class TestColdStartSeeding:
    """A deployed instance boots with an empty database every time, so the
    Cases, Outbox and Audit screens would be blank for the first visitor."""

    def test_an_empty_database_gets_seeded(self):
        assert runs_route.seed_demo_if_empty() is True
        final = _drain()
        assert final["state"] == "done", final.get("error")
        assert final["result"]["seeded"] is True

        session = SessionLocal()
        cases = session.scalar(select(func.count()).select_from(Case))
        session.close()
        assert cases > 0

    def test_a_database_that_already_has_cases_is_left_alone(self, api):
        api.post("/api/runs", json={"policy": "router", "count": 20, "seed": 4})
        _drain()
        # Second boot against the same data must not redo the work.
        assert runs_route.seed_demo_if_empty() is False

    def test_seeding_can_be_switched_off(self, monkeypatch):
        monkeypatch.setattr(runs_route, "DEMO_SEED_COUNT", 0)
        assert runs_route.seed_demo_if_empty() is False
