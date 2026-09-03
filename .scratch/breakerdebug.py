"""What actually tripped the breaker, and why is its retry math insane?"""

import sys
from datetime import datetime

sys.path.insert(0, r"D:\Coding\Razorpay Revive")

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Action
from app.razorpay_client import client

print("=== live breaker internals ===")
print("consecutive_failures:", client._consecutive_failures)
print("_opened_at          :", client._opened_at)
print("real wall time now  :", datetime.now())
if client._opened_at:
    real_diff = datetime.now() - client._opened_at
    print("wall time minus opened_at:", real_diff, f"({real_diff.total_seconds():.0f}s)")

print("\n=== actions ever blocked with a Razorpay-unavailable reason ===")
session = SessionLocal()
actions = session.scalars(
    select(Action).where(Action.blocked_reason.isnot(None)).order_by(Action.id)
).all()
razorpay_down = [a for a in actions if a.blocked_reason and "Razorpay unavailable" in a.blocked_reason]
print(f"{len(razorpay_down)} such actions")
for a in razorpay_down[:5]:
    print(f"  action #{a.id}  executed_at={a.executed_at}  reason={a.blocked_reason}")
print("  ...")
for a in razorpay_down[-5:]:
    print(f"  action #{a.id}  executed_at={a.executed_at}  reason={a.blocked_reason}")
session.close()
