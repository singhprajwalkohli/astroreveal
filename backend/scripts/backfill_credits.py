"""One-off: grant credits for orders that were paid BEFORE the credit system existed.

Run once after deploying, from the backend folder, with the same env vars as the server:
    python -m scripts.backfill_credits
Safe to re-run (grants are unique per order+kind). Credits expire CREDIT_VALIDITY_DAYS from now.
"""
import asyncio, os
from motor.motor_asyncio import AsyncIOMotorClient
from services import credits


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    await credits.ensure_indexes(db)
    print("orders processed:", await credits.backfill_paid_orders(db))

asyncio.run(main())
