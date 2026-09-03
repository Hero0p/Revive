"""Fails the build if anything outside clock.py reads the real clock.

The demo depends on being able to move time. One stray datetime.now() and a
scheduled action silently ignores the simulated clock.
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
FORBIDDEN = re.compile(r"datetime\.(now|utcnow)\s*\(|time\.time\s*\(")


def test_no_direct_clock_reads_outside_clock_module():
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name == "clock.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(APP.parent)}:{lineno}: {line.strip()}")

    assert not offenders, "Use clock.now() instead:\n" + "\n".join(offenders)


def test_clock_advances_and_resets():
    from app.clock import Clock

    c = Clock()
    start = c.now()
    c.advance(days=3)
    assert (c.now() - start).days == 3
    c.reset()
    assert abs((c.now() - start).total_seconds()) < 5


def test_clock_freeze_makes_time_stand_still():
    from app.clock import Clock

    c = Clock()
    c.freeze()
    first = c.now()
    assert c.now() == first  # no drift between reads
    c.advance(hours=2)
    assert (c.now() - first).total_seconds() == 7200
