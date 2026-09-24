"""Credit packs for paid AI features.

A paid order grants credits that expire after CREDIT_VALIDITY_DAYS (default 30).
Credits are spent atomically, one at a time, before any AI call is made, and are
refunded if the AI call fails. Nothing here trusts the client.

Kinds:
  kundli - one AI interpretation of one Kundli (one person's chart)
  palm   - one AI palm reading
  chat   - one "Ask your Kundli" message
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

CREDIT_VALIDITY_DAYS = int(os.environ.get("CREDIT_VALIDITY_DAYS", "30"))

# What each product grants. Tune these numbers against your real Gemini cost.
PRODUCT_CREDITS: dict[str, dict[str, int]] = {
    "kundli": {"kundli": 1, "chat": 30},
    "palm": {"palm": 1},
    "bundle": {"kundli": 1, "palm": 1, "chat": 40},
}
KINDS = ("kundli", "palm", "chat")


class InsufficientCredits(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_indexes(db) -> None:
    # One grant per (order, kind): makes granting idempotent even if the checkout
    # verification and the webhook race each other.
    await db.credit_grants.create_index([("razorpay_order_id", 1), ("kind", 1)], unique=True)
    await db.credit_grants.create_index([("user_id", 1), ("kind", 1), ("expires_at", 1)])


async def grant_for_order(db, order: dict) -> None:
    """Idempotently grant the credits for a PAID order. Safe to call repeatedly."""
    now = _now()
    for kind, amount in PRODUCT_CREDITS.get(order["product"], {}).items():
        try:
            await db.credit_grants.update_one(
                {"razorpay_order_id": order["razorpay_order_id"], "kind": kind},
                {"$setOnInsert": {
                    "id": str(uuid.uuid4()),
                    "user_id": order["user_id"],
                    "product": order["product"],
                    "amount": amount,
                    "remaining": amount,
                    "granted_at": now,
                    "expires_at": now + timedelta(days=CREDIT_VALIDITY_DAYS),
                }},
                upsert=True,
            )
        except DuplicateKeyError:
            pass  # a concurrent call already created it


async def consume(db, user_id: str, kind: str) -> str | None:
    """Atomically spend one credit (soonest-expiring first). Returns the grant id, or None."""
    grant = await db.credit_grants.find_one_and_update(
        {"user_id": user_id, "kind": kind, "remaining": {"$gt": 0}, "expires_at": {"$gt": _now()}},
        {"$inc": {"remaining": -1}},
        sort=[("expires_at", 1)],
    )
    return grant["id"] if grant else None


async def refund(db, grant_id: str) -> None:
    await db.credit_grants.update_one({"id": grant_id}, {"$inc": {"remaining": 1}})


async def spend(db, user_id: str, kind: str, work):
    """Spend a credit, run `work()` (an async callable), and refund if it raises.

    Raises InsufficientCredits before `work` runs if the user has no valid credit.
    """
    grant_id = await consume(db, user_id, kind)
    if not grant_id:
        raise InsufficientCredits(kind)
    try:
        return await work()
    except BaseException:
        # shield: still refund if the request was cancelled (e.g. client disconnect)
        await asyncio.shield(refund(db, grant_id))
        raise


async def balances(db, user_id: str) -> dict:
    """Remaining, non-expired credits per kind, plus the soonest expiry."""
    totals = {k: 0 for k in KINDS}
    soonest = None
    async for g in db.credit_grants.find(
        {"user_id": user_id, "remaining": {"$gt": 0}, "expires_at": {"$gt": _now()}},
        {"_id": 0, "kind": 1, "remaining": 1, "expires_at": 1},
    ):
        totals[g["kind"]] = totals.get(g["kind"], 0) + g["remaining"]
        exp = g["expires_at"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        soonest = exp if soonest is None or exp < soonest else soonest
    return {"credits": totals, "credits_expire_at": soonest.isoformat() if soonest else None}


async def backfill_paid_orders(db) -> int:
    """One-off: give credits for orders that were paid before credits existed."""
    n = 0
    async for order in db.orders.find({"payment_status": "paid"}, {"_id": 0}):
        await grant_for_order(db, order)
        n += 1
    return n
