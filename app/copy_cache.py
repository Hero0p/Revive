"""Pre-written message copy, looked up instead of generated.

The send path used to call an LLM for every failed payment: one failure, one
call, under a two-second deadline, with the customer's name, order id, amount
and card digits in the request body. A million failures meant a million calls.

But the copy only varies along three axes -- the failure reason, the language,
and the kind of business -- so the whole space is about 700 strings. They are
written once, reviewed by a person, committed, and loaded here at startup.
Sending is then a dictionary lookup and a slot fill: no network, no deadline,
no customer data leaving the system.

Lookup degrades rather than fails, because a missing cell must never stop a
message going out:

    tier 1  exact  (rule, locale, vertical)
    tier 2  same rule and locale, vertical "generic"
    tier 3  same rule, locale "en", vertical "generic"
    tier 4  the hand-written template in messages.py

Tier 4 is the reason this file can be deleted, or its data file corrupted,
without breaking a single send.
"""

import hashlib
import json
import random
from pathlib import Path

from app.config import ROOT
from app.rules import RULES

COPY_DIR = ROOT / "copy"
APPROVED = COPY_DIR / "approved.json"

# Bump when the wording brief changes, so old rows stop counting as coverage
# instead of quietly serving text written to a superseded brief.
PROMPT_VERSION = "v1"

LOCALES = ("en", "hi", "hinglish")
VERTICALS = (
    "generic",
    "food_delivery",
    "ecommerce",
    "edtech",
    "saas",
    "travel",
    "healthcare",
    "services",
)
VARIANTS = ("a", "b", "c")

# This deployment is one merchant, so there is one vertical at render time.
# The other seven exist because the table is the reusable part of the product.
DEFAULT_VERTICAL = "ecommerce"

FALLBACK_LOCALE = "en"
FALLBACK_VERTICAL = "generic"

# Every placeholder a stored template may contain. Anything else is rejected at
# build time -- an unknown name would raise KeyError mid-send.
ALLOWED_PLACEHOLDERS = frozenset(
    {
        "customer_name",
        "merchant_name",
        "order_id",
        "item_names",
        "amount",
        "last4",
        "attempt_time",
        "resume_url",
        "alt_method",
    }
)

# rule_id -> {(locale, vertical): [entry, ...]}
_TABLE: dict[str, dict[tuple[str, str], list[dict]]] = {}
_LOADED = False
_LOAD_ERROR: str | None = None

# Chaos toggle, flipped by POST /api/chaos. Forces tier 4 so the hand-written
# fallback can be demonstrated.
chaos_copy_down = False


def key(rule_id: str, locale: str, vertical: str, variant: str) -> str:
    """Stable id for one cell, so a re-run of the writer is a no-op diff."""
    raw = f"{rule_id}|{locale}|{vertical}|{variant}|{PROMPT_VERSION}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load() -> dict:
    """Read the committed table. Never raises: a broken file degrades to
    tier 4, which is the whole point of having tier 4."""
    global _LOADED, _LOAD_ERROR
    _TABLE.clear()
    _LOAD_ERROR = None

    try:
        raw = json.loads(APPROVED.read_text(encoding="utf-8"))
        for entry in raw["entries"]:
            if entry.get("prompt_version") != PROMPT_VERSION:
                continue
            cell = _TABLE.setdefault(entry["rule_id"], {}).setdefault(
                (entry["locale"], entry["vertical"]), []
            )
            cell.append(entry)
    except FileNotFoundError:
        _LOAD_ERROR = f"{APPROVED.name} is missing"
    except Exception as exc:  # noqa: BLE001 -- malformed data must not stop startup
        _TABLE.clear()
        _LOAD_ERROR = f"{type(exc).__name__}: {exc}"

    _LOADED = True
    return coverage()


def coverage() -> dict:
    """Cells expected against cells loaded.

    Surfaced on /api/health because silent degradation to tier 4 -- every
    message quietly falling back to the eight hand-written templates -- is the
    failure this design is most exposed to.
    """
    expected = [
        (rule.rule_id, locale, vertical)
        for rule in RULES.values()
        for locale in LOCALES
        for vertical in VERTICALS
    ]
    present = sum(
        1
        for rule_id, locale, vertical in expected
        if _TABLE.get(rule_id, {}).get((locale, vertical))
    )
    total = len(expected)
    return {
        "loaded": _LOADED,
        "error": _LOAD_ERROR,
        "prompt_version": PROMPT_VERSION,
        "cells_expected": total,
        "cells_loaded": present,
        "entries": sum(len(v) for cell in _TABLE.values() for v in cell.values()),
        "complete": present == total,
        "percent": round(present / total * 100, 1) if total else 0.0,
    }


def select_variant(seed_key: str, candidates: list[dict]) -> dict:
    """Deterministic for a given case, so a re-run picks the same wording.

    Behind a function because this is where a bandit would go: swapping the
    choice for a learned one touches nothing else.
    """
    return random.Random(seed_key).choice(candidates)


def lookup(rule_id: str, locale: str, vertical: str, seed_key: str) -> tuple[dict | None, int]:
    """Best available entry for a cell, and which tier it came from.

    Returns (None, 4) when nothing is stored, which means the caller renders
    the hand-written template.
    """
    if chaos_copy_down or not _LOADED:
        return None, 4

    by_cell = _TABLE.get(rule_id)
    if not by_cell:
        return None, 4

    for tier, cell in (
        (1, (locale, vertical)),
        (2, (locale, FALLBACK_VERTICAL)),
        (3, (FALLBACK_LOCALE, FALLBACK_VERTICAL)),
    ):
        candidates = by_cell.get(cell)
        if candidates:
            return select_variant(seed_key, candidates), tier

    return None, 4
