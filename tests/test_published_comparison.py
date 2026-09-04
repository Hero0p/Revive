"""The Overview opens with a committed run, so those numbers must stay true.

web/src/data/comparison.json is real simulator output (exported by
scripts/export_comparison.py) that ships inside the frontend bundle, so a
visitor sees results immediately instead of an empty page on a cold instance.

That convenience carries a risk: change a rule, and the dashboard would go on
showing yesterday's figures with nothing to flag it. These tests pin the
committed run to the numbers the README quotes, so any change to the decision
table breaks the build until both are regenerated together.

They do not re-run the simulation -- 3,000 cases takes minutes. They check the
artifact is internally consistent and still says what the documentation says.
"""

import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PUBLISHED = ROOT / "web" / "src" / "data" / "comparison.json"

# The figures in the README's Results section. Regenerate the fixture and
# update both together if a rule changes.
DOCUMENTED = {
    "baseline": {
        "messages_sent": 3000,
        "wrong_advice_count": 167,
        "already_paid_contacts": 23,
        "suppressed_count": 0,
        "amount_recovered_paise": 68602000,
        "cases_recovered": 280,
    },
    "router": {
        "messages_sent": 4582,
        "wrong_advice_count": 0,
        "already_paid_contacts": 0,
        "suppressed_count": 323,
        "amount_recovered_paise": 98105000,
        "cases_recovered": 398,
    },
}
AMOUNT_AT_RISK_PAISE = 755153000


@pytest.fixture(scope="module")
def published() -> dict:
    assert PUBLISHED.exists(), f"{PUBLISHED} is missing -- run scripts/export_comparison.py"
    return json.loads(PUBLISHED.read_text(encoding="utf-8"))


class TestTheCommittedRun:
    def test_it_is_the_run_the_dashboard_asks_for(self, published):
        assert published["seed"] == 42
        assert published["count"] == 3000

    def test_it_says_where_it_came_from(self, published):
        """A reader must be able to tell these are generated, not typed."""
        assert "export_comparison" in published["_comment"]

    @pytest.mark.parametrize("policy", ["baseline", "router"])
    def test_it_still_matches_the_documented_figures(self, published, policy):
        actual = published["policies"][policy]
        for field, expected in DOCUMENTED[policy].items():
            assert actual[field] == expected, (
                f"{policy}.{field} is {actual[field]}, the README says {expected}. "
                "If a rule changed, re-run the comparison, re-export the fixture, "
                "and update the README together."
            )

    @pytest.mark.parametrize("policy", ["baseline", "router"])
    def test_both_policies_saw_the_same_money(self, published, policy):
        """The comparison is only fair over an identical batch."""
        assert published["policies"][policy]["amount_at_risk_paise"] == AMOUNT_AT_RISK_PAISE
        assert published["policies"][policy]["case_count"] == 3000

    @pytest.mark.parametrize("policy", ["baseline", "router"])
    def test_the_recovery_rate_is_consistent_with_its_own_amounts(self, published, policy):
        metrics = published["policies"][policy]
        expected = metrics["amount_recovered_paise"] / metrics["amount_at_risk_paise"]
        assert metrics["recovery_rate"] == pytest.approx(expected, abs=0.0001)

    def test_the_headline_claim_holds(self, published):
        """Revive recovers more, and gives no structurally impossible advice."""
        baseline = published["policies"]["baseline"]
        router = published["policies"]["router"]
        assert router["recovery_rate"] > baseline["recovery_rate"]
        assert router["wrong_advice_count"] == 0
        assert baseline["wrong_advice_count"] > 0

    def test_the_charts_have_data_for_both_policies(self, published):
        for policy in ("baseline", "router"):
            causes = published["causes"][policy]
            assert causes, f"no by-cause rows for {policy}"
            assert {"root_cause", "recovery_rate"} <= set(causes[0])
