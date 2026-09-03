"""Live tail of the pipeline. Run this next to the browser during a demo.

    python scripts/watch.py

Prints every webhook as it arrives, the case it becomes, and every message that
goes out. Useful for proving the webhook secret is right: a delivery with the
wrong secret shows up here as a rejected raw event, not as silence.
"""

import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://localhost:8000"
POLL_SECONDS = 1.5

GREY, GREEN, AMBER, RED, BOLD, OFF = (
    "\033[90m", "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m",
)


def rupees(paise):
    return f"Rs {(paise or 0) / 100:,.0f}"


def main() -> None:
    seen_events: set[int] = set()
    seen_cases: set[int] = set()
    seen_actions: set[int] = set()
    first_pass = True

    print(f"{BOLD}Watching {BASE} - Ctrl+C to stop{OFF}\n")

    while True:
        try:
            events = httpx.get(f"{BASE}/api/events", timeout=10).json()["events"]
            cases = httpx.get(f"{BASE}/api/cases", params={"run_id": "live", "limit": 50}, timeout=10).json()["cases"]
            outbox = httpx.get(f"{BASE}/api/outbox", params={"run_id": "live", "limit": 50}, timeout=10).json()["messages"]
        except Exception as exc:
            print(f"{RED}api unreachable: {exc}{OFF}")
            time.sleep(POLL_SECONDS)
            continue

        for event in reversed(events):
            if event["id"] in seen_events:
                continue
            seen_events.add(event["id"])
            if first_pass:
                continue
            if event["error"]:
                print(
                    f"{RED}webhook  #{event['id']} {event['event_type']} REJECTED: "
                    f"{event['error']}{OFF}"
                )
                if event["error"] == "invalid signature":
                    print(
                        f"{GREY}         either the dashboard secret differs from "
                        f"RAZORPAY_WEBHOOK_SECRET, or this was a tampered delivery{OFF}"
                    )
                else:
                    print(
                        f"{GREY}         the signature was fine; processing failed. "
                        f"The payload is still stored in raw_events and can be replayed{OFF}"
                    )
            else:
                print(
                    f"{GREEN}webhook  #{event['id']} {event['event_type']} "
                    f"verified and processed{OFF}"
                )

        for case in reversed(cases):
            if case["id"] in seen_cases:
                continue
            seen_cases.add(case["id"])
            if first_pass:
                continue
            print(
                f"{BOLD}case     #{case['id']} {rupees(case['amount_paise'])} "
                f"{case['error_reason']} -> {case['root_cause']} "
                f"[{case['rule_id']}]{OFF}"
            )
            if case["next_action_at"]:
                print(
                    f"{AMBER}         next: {case['next_action_channel']} at "
                    f"{case['next_action_at']}{OFF}"
                )

        for message in reversed(outbox):
            if message["id"] in seen_actions or message["status"] == "pending":
                continue
            seen_actions.add(message["id"])
            if first_pass:
                continue
            if message["status"] == "sent":
                print(
                    f"{GREEN}message  #{message['id']} {message['channel']} "
                    f"written by {message['message_source']}{OFF}"
                )
                print(f"{GREY}         {(message['message_body'] or '')[:150]}{OFF}")
            else:
                print(
                    f"{GREY}blocked  #{message['id']} {message['blocked_reason']}{OFF}"
                )

        if first_pass:
            print(
                f"{GREY}baseline: {len(seen_events)} events, {len(seen_cases)} live cases, "
                f"{len(seen_actions)} messages. Waiting for new activity...{OFF}\n"
            )
            first_pass = False

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
