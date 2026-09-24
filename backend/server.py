from dotenv import load_dotenv
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field
from passlib.context import CryptContext
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal
import asyncio, base64, logging, os, re, jwt, uuid
from services.astrology import AstrologyCalculationError, AstrologyService
from services.ai import AIServiceUnavailable, PalmReadingService, AstrologyInterpretationService, KundliChatService
from services.payments import (
    PaymentService, PaymentConfigurationError, PRICES_PAISE, PRODUCT_TITLES,
    is_valid_product, now_iso, truncate_for_preview,
)
from services import credits

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")
def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set. Set it in Railway's Variables tab.")
    return value

client = AsyncIOMotorClient(_required_env("MONGO_URL"))
db = client[_required_env("DB_NAME")]
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
# No fallback: a default secret in a public repo lets anyone forge login tokens.
SECRET = _required_env("JWT_SECRET")
_DUMMY_HASH = pwd.hash("not-a-real-password")   # lets us spend the same time on unknown emails as on real ones
app = FastAPI(title="AstroReveal API")
api = APIRouter(prefix="/api")
astrology = AstrologyService()
payments = PaymentService()

KUNDLI_PREVIEW_CHARS = 620
PALM_PREVIEW_CHARS = 420
# Abuse limits (per user). Chat and kundli creation are the endpoints a bot would hammer.
CHAT_MAX_PER_WINDOW = int(os.environ.get("CHAT_MAX_PER_WINDOW", "8"))
CHAT_WINDOW_MINUTES = int(os.environ.get("CHAT_WINDOW_MINUTES", "5"))
KUNDLI_MAX_PER_DAY = int(os.environ.get("KUNDLI_MAX_PER_DAY", "10"))
MAX_PROFILES = int(os.environ.get("MAX_PROFILES", "5"))
# Sign-in protection. Per-EMAIL limits are strict (they stop password guessing on one account and cannot be
# dodged by changing IP). Per-IP limits are generous on purpose: behind some proxies many real users can share one IP.
LOGIN_FAILS_PER_EMAIL = int(os.environ.get("LOGIN_FAILS_PER_EMAIL", "5"))
LOGIN_FAILS_PER_IP = int(os.environ.get("LOGIN_FAILS_PER_IP", "60"))
GOOGLE_FAILS_PER_IP = int(os.environ.get("GOOGLE_FAILS_PER_IP", "60"))
REGISTER_PER_IP_PER_HOUR = int(os.environ.get("REGISTER_PER_IP_PER_HOUR", "20"))
AUTH_WINDOW_MINUTES = 15
MAX_BODY_BYTES = 14 * 1024 * 1024   # a 10 MB palm photo becomes ~13.4 MB once base64-encoded
# Google sign-in is optional: with no client id set, the endpoint answers 503 and the button stays hidden.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
# Automatic deletion of long-inactive accounts. OFF by default: while off, it only logs what it WOULD delete.
RETENTION_ENABLED = os.environ.get("RETENTION_ENABLED", "false").strip().lower() == "true"
RETENTION_DAYS = max(int(os.environ.get("RETENTION_DAYS", "730")), 90)   # never less than 90 days
_IDENTITY = ("name", "dob", "birth_time", "birthplace")
# Correcting a typo in birth details is free for a short window after a Kundli is unlocked,
# and only a couple of times, because every correction re-runs the paid AI interpretation.
EDIT_WINDOW_HOURS = int(os.environ.get("EDIT_WINDOW_HOURS", "48"))
MAX_FREE_EDITS = int(os.environ.get("MAX_FREE_EDITS", "2"))
_BIRTH_FIELDS = ("dob", "birth_time", "birthplace", "latitude", "longitude", "timezone_name")

class AuthInput(BaseModel):      # login: no minimum, so people with older short passwords can still sign in
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
class RegisterInput(BaseModel):  # new accounts need a stronger password
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
RelationType = Literal["self", "spouse", "parent", "child", "sibling", "friend", "other"]
class BirthDetails(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    dob: str
    birth_time: str
    birthplace: str = Field(min_length=1, max_length=200)
    latitude: float | None = None; longitude: float | None = None; timezone_name: str | None = None
class ProfileInput(BirthDetails):
    relation: RelationType = "self"
    consent_confirmed: bool = False
class PalmInput(BaseModel):
    image_base64: str; hand: Literal["left", "right", "both"]
class ProfileEdit(ProfileInput):
    confirm_spend: bool = False   # must be true before a correction that costs a credit
class ChatInput(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    profile_id: str | None = None
class AccountDelete(BaseModel):
    confirm: str
class GoogleInput(BaseModel):
    credential: str = Field(min_length=20, max_length=4096)
class OrderInput(BaseModel):
    product: str
class VerifyInput(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

def token_for(user_id: str):
    return jwt.encode({"sub": user_id, "exp": datetime.now(timezone.utc) + timedelta(days=7)}, SECRET, algorithm="HS256")

async def current_user(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Please sign in to continue")
    try: uid = jwt.decode(authorization[7:], SECRET, algorithms=["HS256"])["sub"]
    except Exception: raise HTTPException(401, "Your session has expired")
    user = await db.users.find_one({"id": uid}, {"_id": 0, "password_hash": 0, "google_sub": 0})
    if not user: raise HTTPException(401, "User not found")
    return user

async def entitlements_for(user_id: str) -> dict:
    """Credit balances. `palm`/`kundli` mean "has a valid credit to spend now"."""
    bal = await credits.balances(db, user_id)
    c = bal["credits"]
    products: set[str] = set()
    async for doc in db.orders.find({"user_id": user_id, "payment_status": "paid"}, {"_id": 0, "product": 1}):
        products.add(doc["product"])
    return {"palm": c["palm"] > 0, "kundli": c["kundli"] > 0, "products": sorted(products), **bal}

def _payment_required(exc: "credits.InsufficientCredits") -> HTTPException:
    label = {"kundli": "a Kundli", "palm": "a palm reading", "chat": "more chat messages"}.get(exc.kind, "this")
    return HTTPException(402, f"You need credits to get {label}. Please purchase a pack to continue.")

async def _rate_limit(collection, user_id: str, max_count: int, window: timedelta, message: str):
    cutoff = (datetime.now(timezone.utc) - window).isoformat()
    n = await collection.count_documents({"user_id": user_id, "created_at": {"$gte": cutoff}})
    if n >= max_count:
        raise HTTPException(429, message)

def _teaser(chart: dict) -> str:
    """Free, template-only preview. No AI call is made for unpaid charts."""
    nak = (chart.get("nakshatra") or {}).get("name", "")
    return (f"Your ascendant (Lagna) is {chart.get('lagna')} and your Moon sign (Rashi) is {chart.get('rashi')}"
            f"{', in the ' + nak + ' nakshatra' if nak else ''}. "
            "Unlock your Kundli to read the full AI interpretation covering career, finance, relationships, "
            "education, family and life periods.")

def _apply_kundli_lock(chart: dict) -> dict:
    """Locked/unlocked is a property of each report, not of the account."""
    chart = {**chart}
    if chart.get("unlocked"):
        chart["locked"] = False
        chart["preview"] = False
        return chart
    chart["interpretation"] = truncate_for_preview(chart.get("interpretation") or "", KUNDLI_PREVIEW_CHARS)
    chart["locked"] = True
    chart["preview"] = True
    chart["unlock_product"] = "kundli"
    chart["unlock_price_paise"] = PRICES_PAISE["kundli"]
    return chart

def _apply_palm_lock(doc: dict) -> dict:
    doc = {**doc}
    if doc.get("unlocked"):
        doc["locked"] = False
        doc["preview"] = False
        return doc
    doc["reading"] = truncate_for_preview(doc.get("reading") or "", PALM_PREVIEW_CHARS)
    doc["locked"] = True
    doc["preview"] = True
    doc["unlock_product"] = "palm"
    doc["unlock_price_paise"] = PRICES_PAISE["palm"]
    return doc

@api.get("/")
async def root(): return {"message": "AstroReveal API ready"}

@app.get("/")
async def health(): return {"status": "ok"}
    
@api.get("/geocode/search")
async def geocode_search(q: str):
    q = q.strip()
    if len(q) < 3:
        return {"results": []}
    results = await astrology.locations.search(q, limit=6)
    return {"results": results}

@api.post("/auth/register")
async def register(data: RegisterInput, request: Request):
    ip = client_ip(request)
    if await _recent_attempts("register_ip", ip, 60) >= REGISTER_PER_IP_PER_HOUR:
        raise _too_many_attempts(60)
    await _note_attempt("register_ip", ip)
    if await db.users.find_one({"email": data.email.lower()}): raise HTTPException(409, "An account with this email already exists")
    user = {"id": str(uuid.uuid4()), "email": data.email.lower(), "password_hash": pwd.hash(data.password), "created_at": now_iso(), "last_active_at": now_iso()}
    await db.users.insert_one(user)
    return {"token": token_for(user["id"]), "user": {"id": user["id"], "email": user["email"]}}

@api.post("/auth/login")
async def login(data: AuthInput, request: Request):
    email, ip = data.email.lower(), client_ip(request)
    if (await _recent_attempts("login_fail_email", email, AUTH_WINDOW_MINUTES) >= LOGIN_FAILS_PER_EMAIL
            or await _recent_attempts("login_fail_ip", ip, AUTH_WINDOW_MINUTES) >= LOGIN_FAILS_PER_IP):
        raise _too_many_attempts()
    user = await db.users.find_one({"email": email})
    if user and not user.get("password_hash"):
        raise HTTPException(401, "This email uses Google sign-in. Please use the Google button.")
    password_ok = pwd.verify(data.password, user["password_hash"] if user else _DUMMY_HASH) and user is not None
    if not password_ok:
        # failures are counted for unknown emails too, so the response never reveals whether an account exists
        await _note_attempt("login_fail_email", email)
        await _note_attempt("login_fail_ip", ip)
        raise HTTPException(401, "Email or password is incorrect")
    await db.auth_attempts.delete_many({"kind": "login_fail_email", "key": email})
    await touch_user(user["id"])
    return {"token": token_for(user["id"]), "user": {"id": user["id"], "email": user["email"]}}

def _verify_google_credential(credential: str) -> dict:
    """Blocking network call. Checks Google's signature, expiry, issuer and that the token was issued for OUR client id."""
    from google.oauth2 import id_token
    from google.auth.transport import requests as g_requests
    return id_token.verify_oauth2_token(credential, g_requests.Request(), GOOGLE_CLIENT_ID, clock_skew_in_seconds=10)

@api.post("/auth/google")
async def google_login(data: GoogleInput, request: Request):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google sign-in is not configured yet")
    ip = client_ip(request)
    if await _recent_attempts("google_fail_ip", ip, AUTH_WINDOW_MINUTES) >= GOOGLE_FAILS_PER_IP:
        raise _too_many_attempts()
    try:
        claims = await asyncio.to_thread(_verify_google_credential, data.credential)
    except Exception:
        await _note_attempt("google_fail_ip", ip)
        raise HTTPException(401, "Google sign-in could not be verified. Please try again.")
    sub, email = claims.get("sub"), (claims.get("email") or "").lower()
    if not sub or not email or claims.get("email_verified") is not True:
        raise HTTPException(401, "Your Google account email is not verified")

    user = await db.users.find_one({"google_sub": sub})
    if not user:
        user = await db.users.find_one({"email": email})
        if user and user.get("google_sub"):
            raise HTTPException(409, "This email is already linked to a different Google account")
        if user:
            # Password sign-ups were never email-verified, so anyone could have registered this address.
            # Google has now proven who really owns it: link the account and remove the old password,
            # so a squatter who registered first cannot keep access to it.
            await db.users.update_one({"id": user["id"]}, {"$set": {"google_sub": sub}, "$unset": {"password_hash": ""}})
        else:
            user = {"id": str(uuid.uuid4()), "email": email, "google_sub": sub, "created_at": now_iso(), "last_active_at": now_iso()}
            await db.users.insert_one(dict(user))
    await touch_user(user["id"])
    return {"token": token_for(user["id"]), "user": {"id": user["id"], "email": user["email"]}}

def client_ip(request: Request) -> str:
    """Best-effort visitor IP behind a proxy: the last X-Forwarded-For entry is the one our own proxy added."""
    fwd = (request.headers.get("x-forwarded-for") or "").strip()
    if fwd:
        return fwd.split(",")[-1].strip()
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    return request.client.host if request.client else "unknown"

async def _recent_attempts(kind: str, key: str, minutes: int) -> int:
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return await db.auth_attempts.count_documents({"kind": kind, "key": key, "at": {"$gte": since}})

async def _note_attempt(kind: str, key: str) -> None:
    await db.auth_attempts.insert_one({"kind": kind, "key": key, "at": datetime.now(timezone.utc)})

def _too_many_attempts(minutes: int = AUTH_WINDOW_MINUTES) -> HTTPException:
    return HTTPException(429, f"Too many attempts. Please wait {minutes} minutes and try again.", headers={"Retry-After": str(minutes * 60)})

async def touch_user(user_id: str) -> None:
    """Record activity at most once a day (used only to decide when an abandoned account can be deleted)."""
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    await db.users.update_one(
        {"id": user_id, "$or": [{"last_active_at": {"$exists": False}}, {"last_active_at": {"$lt": day_ago}}]},
        {"$set": {"last_active_at": now_iso()}})

async def erase_user(user_id: str) -> dict:
    """Delete everything personal about a user. Payment order records (amount, date, Razorpay ids;
    no name or email) are kept for accounting, but the raw payment-webhook copies, which contain the
    customer's contact details, are deleted."""
    order_ids = [o["razorpay_order_id"] async for o in db.orders.find({"user_id": user_id}, {"_id": 0, "razorpay_order_id": 1})]
    counts: dict[str, int] = {}
    for label, coll in (("kundlis", db.kundlis), ("profiles", db.profiles), ("palm_readings", db.palm_readings),
                        ("ai_conversations", db.ai_conversations), ("credit_grants", db.credit_grants)):
        counts[label] = (await coll.delete_many({"user_id": user_id})).deleted_count
    if order_ids:
        res = await db.payment_events.delete_many({"$or": [{"raw.payload.payment.entity.order_id": {"$in": order_ids}},
                                                           {"raw.payload.order.entity.id": {"$in": order_ids}}]})
        counts["payment_events"] = res.deleted_count
    await db.users.delete_one({"id": user_id})
    return counts

async def purge_inactive_accounts(dry_run: bool) -> list[str]:
    """Find (and, unless dry_run, erase) accounts with no activity for RETENTION_DAYS."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    query = {"$or": [{"last_active_at": {"$lt": cutoff}},
                     {"last_active_at": {"$exists": False}, "created_at": {"$lt": cutoff}}]}
    ids = [u["id"] async for u in db.users.find(query, {"_id": 0, "id": 1})]
    if not dry_run:
        for uid in ids:
            await erase_user(uid)
    return ids

async def _retention_loop():
    log = logging.getLogger("uvicorn.error")
    await asyncio.sleep(300)
    while True:
        try:
            ids = await purge_inactive_accounts(dry_run=not RETENTION_ENABLED)
            if ids:
                log.warning("Retention: %s %d account(s) inactive for %d+ days",
                            "deleted" if RETENTION_ENABLED else "WOULD delete (dry run)", len(ids), RETENTION_DAYS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Retention job failed: %r", exc)
        await asyncio.sleep(24 * 3600)

@api.post("/account/delete")
async def delete_account(data: AccountDelete, user=Depends(current_user)):
    """Permanently delete the signed-in account and everything stored about it."""
    if data.confirm != "DELETE":
        raise HTTPException(400, "Type DELETE to confirm")
    return {"deleted": True, **(await erase_user(user["id"]))}

@api.get("/auth/me")
async def me(user=Depends(current_user)): return user

@api.get("/entitlements")
async def get_entitlements(user=Depends(current_user)):
    return await entitlements_for(user["id"])

def _public_chart(chart: dict) -> dict:
    chart = {k: v for k, v in chart.items() if k not in ("user_id", "_id")}
    return _apply_kundli_lock(chart)

async def _adopt_legacy_kundlis(user_id: str) -> None:
    """Kundlis saved before profiles existed get a profile automatically (first one = "self")."""
    legacy = await db.kundlis.find({"user_id": user_id, "profile_id": {"$exists": False}}, {"_id": 0}).sort("created_at", 1).to_list(200)
    if not legacy:
        return
    has_self = await db.profiles.find_one({"user_id": user_id, "relation": "self"}, {"_id": 0, "id": 1}) is not None
    for k in legacy:
        ident = {"user_id": user_id, **{f: k.get(f) for f in _IDENTITY}}
        profile = await db.profiles.find_one(ident, {"_id": 0})
        if not profile:
            coords = k.get("coordinates") or {}
            profile = {**ident, "id": str(uuid.uuid4()), "relation": "other" if has_self else "self", "consent_confirmed": None,
                       "legacy": True, "latitude": coords.get("latitude"), "longitude": coords.get("longitude"),
                       "timezone_name": coords.get("timezone"), "created_at": k.get("created_at") or now_iso(), "updated_at": now_iso()}
            has_self = True
            await db.profiles.insert_one(dict(profile))
        await db.kundlis.update_one({"id": k["id"]}, {"$set": {"profile_id": profile["id"]}})

async def _profile_for_submission(user_id: str, data: ProfileInput) -> tuple[dict, bool]:
    """Find this person among the user's saved profiles, or prepare a new one (not saved yet)."""
    ident = {"user_id": user_id, "name": data.name.strip(), "dob": data.dob, "birth_time": data.birth_time, "birthplace": data.birthplace}
    existing = await db.profiles.find_one(ident, {"_id": 0})
    if existing:
        return existing, False
    if data.relation == "self":
        if await db.profiles.find_one({"user_id": user_id, "relation": "self"}, {"_id": 0, "id": 1}):
            raise HTTPException(409, "You already have a profile for yourself. Choose another relation, or delete your existing profile first.")
    elif not data.consent_confirmed:
        raise HTTPException(400, "Please confirm you have this person's permission, or that you are their parent or legal guardian.")
    if await db.profiles.count_documents({"user_id": user_id}) >= MAX_PROFILES:
        raise HTTPException(400, f"You can save up to {MAX_PROFILES} people. Delete one to add another.")
    now = now_iso()
    profile = {**ident, "id": str(uuid.uuid4()), "relation": data.relation,
               "consent_confirmed": None if data.relation == "self" else True,
               "consent_at": None if data.relation == "self" else now,
               "latitude": data.latitude, "longitude": data.longitude, "timezone_name": data.timezone_name,
               "created_at": now, "updated_at": now}
    return profile, True

async def _unlock_chart(user_id: str, chart: dict) -> dict:
    """Spend one kundli credit to generate the full interpretation of an existing chart."""
    async def interpret():
        text = await AstrologyInterpretationService().interpret(chart)
        res = await db.kundlis.update_one(
            {"id": chart["id"], "user_id": user_id, "unlocked": {"$ne": True}},
            {"$set": {"interpretation": text, "unlocked": True, "unlocked_at": now_iso()}},
        )
        if res.matched_count == 0:  # another request unlocked it first: don't charge twice
            raise HTTPException(409, "This Kundli was already unlocked")
        return text
    text = await credits.spend(db, user_id, "kundli", interpret)
    return {**chart, "interpretation": text, "unlocked": True}

@api.post("/kundli")
async def create_kundli(data: ProfileInput, user=Depends(current_user)):
    uid = user["id"]
    await _adopt_legacy_kundlis(uid)
    profile, is_new = await _profile_for_submission(uid, data)

    if not is_new:
        # Same person again: never create a duplicate chart or charge twice.
        latest = await db.kundlis.find_one({"user_id": uid, "profile_id": profile["id"]}, {"_id": 0}, sort=[("created_at", -1)])
        if latest:
            if not latest.get("unlocked"):
                try:
                    latest = await _unlock_chart(uid, latest)
                except credits.InsufficientCredits:
                    pass  # no credit: they keep the free preview
                except AIServiceUnavailable as exc:
                    raise HTTPException(503, str(exc)) from exc
            return _public_chart(latest)

    await _rate_limit(db.kundlis, uid, KUNDLI_MAX_PER_DAY, timedelta(days=1),
                      "You have reached today's limit for creating Kundlis. Please try again tomorrow.")
    try:
        result = await astrology.calculate_kundli(**data.model_dump(include=set(BirthDetails.model_fields)))
    except AstrologyCalculationError as exc:
        raise HTTPException(422, str(exc)) from exc
    chart_doc = {**result, "id": str(uuid.uuid4()), "user_id": uid, "profile_id": profile["id"], "created_at": now_iso()}

    # The chart is deterministic and free. The AI interpretation costs money, so it is
    # generated only when the user spends a kundli credit.
    async def interpret():
        return await AstrologyInterpretationService().interpret(chart_doc)
    try:
        chart_doc["interpretation"] = await credits.spend(db, uid, "kundli", interpret)
        chart_doc["unlocked"] = True
        chart_doc["unlocked_at"] = now_iso()
    except credits.InsufficientCredits:
        chart_doc["interpretation"] = _teaser(chart_doc)
        chart_doc["unlocked"] = False
    except AIServiceUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc

    if is_new:  # saved only after the chart worked, so a bad birth time doesn't use up a slot
        await db.profiles.insert_one(dict(profile))
    await db.kundlis.insert_one(dict(chart_doc))
    return _public_chart(chart_doc)

@api.post("/kundli/{chart_id}/unlock")
async def unlock_kundli(chart_id: str, user=Depends(current_user)):
    chart = await db.kundlis.find_one({"id": chart_id, "user_id": user["id"]}, {"_id": 0})
    if not chart: raise HTTPException(404, "Kundli not found")
    if not chart.get("unlocked"):
        try:
            chart = await _unlock_chart(user["id"], chart)
        except credits.InsufficientCredits as exc:
            raise _payment_required(exc) from exc
        except AIServiceUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
    return _public_chart(chart)

@api.get("/kundli/latest")
async def latest_kundli(profile_id: str | None = None, user=Depends(current_user)):
    query = {"user_id": user["id"], **({"profile_id": profile_id} if profile_id else {})}
    result = await db.kundlis.find_one(query, {"_id": 0}, sort=[("created_at", -1)])
    if not result: raise HTTPException(404, "Create your first Kundli to see it here")
    return _public_chart(result)

async def _profiles_view(user_id: str) -> list[dict]:
    await _adopt_legacy_kundlis(user_id)
    profiles = await db.profiles.find({"user_id": user_id}, {"_id": 0}).sort("created_at", 1).to_list(100)
    newest: dict[str, dict] = {}
    async for k in db.kundlis.find({"user_id": user_id}, {"_id": 0, "id": 1, "profile_id": 1, "unlocked": 1}).sort("created_at", -1):
        newest.setdefault(k.get("profile_id"), k)
    return [{"id": p["id"], "name": p["name"], "relation": p["relation"], "created_at": p["created_at"],
             "has_kundli": p["id"] in newest, "kundli_id": (newest.get(p["id"]) or {}).get("id"),
             "unlocked": bool((newest.get(p["id"]) or {}).get("unlocked"))} for p in profiles]

@api.get("/profiles")
async def list_profiles(user=Depends(current_user)):
    return {"profiles": await _profiles_view(user["id"]), "max_profiles": MAX_PROFILES}

def _free_edit_status(chart: dict | None) -> tuple[bool, str | None]:
    """Would correcting this chart's birth details be free right now? Returns (free, free_until)."""
    if not chart or not chart.get("unlocked"):
        return True, None          # no paid reading exists yet: recalculating costs us nothing
    started = chart.get("unlocked_at") or chart.get("created_at") or now_iso()
    try:
        t0 = datetime.fromisoformat(started)
        if t0.tzinfo is None: t0 = t0.replace(tzinfo=timezone.utc)
    except ValueError:
        t0 = datetime.now(timezone.utc) - timedelta(days=365)
    until = t0 + timedelta(hours=EDIT_WINDOW_HOURS)
    free = datetime.now(timezone.utc) < until and int(chart.get("free_edits_used") or 0) < MAX_FREE_EDITS
    return free, until.isoformat()

async def _latest_chart(user_id: str, profile_id: str) -> dict | None:
    return await db.kundlis.find_one({"user_id": user_id, "profile_id": profile_id}, {"_id": 0}, sort=[("created_at", -1)])

@api.get("/profiles/{profile_id}")
async def get_profile(profile_id: str, user=Depends(current_user)):
    """Full details of one saved person (owner only), used to pre-fill the edit form."""
    profile = await db.profiles.find_one({"id": profile_id, "user_id": user["id"]}, {"_id": 0, "user_id": 0})
    if not profile: raise HTTPException(404, "Profile not found")
    chart = await _latest_chart(user["id"], profile_id)
    free, until = _free_edit_status(chart)
    return {**profile, "has_kundli": chart is not None, "unlocked": bool(chart and chart.get("unlocked")),
            "edit_free": free, "edit_free_until": until if chart and chart.get("unlocked") else None}

@api.put("/profiles/{profile_id}")
async def edit_profile(profile_id: str, data: ProfileEdit, user=Depends(current_user)):
    """Correct a saved person. Name/relation changes are always free. Changing birth details
    recalculates the chart: free for a preview, free for a short window after unlocking
    (limited times), otherwise it costs one Kundli credit and needs confirm_spend=true."""
    uid = user["id"]
    profile = await db.profiles.find_one({"id": profile_id, "user_id": uid}, {"_id": 0})
    if not profile: raise HTTPException(404, "Profile not found")

    name = data.name.strip()
    if await db.profiles.find_one({"user_id": uid, "id": {"$ne": profile_id}, "name": name, "dob": data.dob,
                                   "birth_time": data.birth_time, "birthplace": data.birthplace}, {"_id": 0, "id": 1}):
        raise HTTPException(409, "You already have this person saved.")
    if data.relation == "self" and data.relation != profile["relation"]:
        if await db.profiles.find_one({"user_id": uid, "relation": "self", "id": {"$ne": profile_id}}, {"_id": 0, "id": 1}):
            raise HTTPException(409, "You already have a profile for yourself. Choose another relation.")
    consented = data.relation == "self" or bool(profile.get("consent_confirmed")) or data.consent_confirmed
    if not consented:
        raise HTTPException(400, "Please confirm you have this person's permission, or that you are their parent or legal guardian.")

    new_birth = {"dob": data.dob, "birth_time": data.birth_time, "birthplace": data.birthplace,
                 "latitude": data.latitude, "longitude": data.longitude, "timezone_name": data.timezone_name}
    birth_changed = any(new_birth[f] != profile.get(f) for f in _BIRTH_FIELDS)
    now = now_iso()
    profile_update = {"name": name, "relation": data.relation, "updated_at": now, **new_birth}
    if data.relation != "self" and not profile.get("consent_confirmed"):
        profile_update.update(consent_confirmed=True, consent_at=now)
    chart = await _latest_chart(uid, profile_id)

    if not birth_changed or chart is None:
        await db.profiles.update_one({"id": profile_id, "user_id": uid}, {"$set": profile_update})
        if chart is not None:   # only the name/relation changed, so the chart itself stays as it is
            await db.kundlis.update_one({"id": chart["id"], "user_id": uid}, {"$set": {"name": name}})
            chart = {**chart, "name": name}
        return {"profile_id": profile_id, "kundli": _public_chart(chart) if chart else None, "charged": False}

    free, _ = _free_edit_status(chart)
    if chart.get("unlocked") and not free and not data.confirm_spend:
        raise HTTPException(409, "Correcting birth details now costs 1 Kundli credit. Please confirm to continue.")
    try:
        result = await astrology.calculate_kundli(name=name, **new_birth)
    except AstrologyCalculationError as exc:
        raise HTTPException(422, str(exc)) from exc
    new_chart = {**result, "id": chart["id"], "user_id": uid, "profile_id": profile_id, "created_at": chart["created_at"]}

    async def save(extra: dict):
        new_chart.update(extra)
        await db.kundlis.replace_one({"id": chart["id"], "user_id": uid}, dict(new_chart))
        await db.profiles.update_one({"id": profile_id, "user_id": uid}, {"$set": profile_update})

    charged = False
    try:
        if not chart.get("unlocked"):                       # free preview: recalculate, no AI, no cost
            await save({"interpretation": _teaser(new_chart), "unlocked": False})
        elif free:                                          # free correction inside the window
            async def free_edit():
                text = await AstrologyInterpretationService().interpret(new_chart)
                await save({"interpretation": text, "unlocked": True, "unlocked_at": chart.get("unlocked_at") or chart["created_at"],
                            "free_edits_used": int(chart.get("free_edits_used") or 0) + 1})
            await free_edit()
        else:                                               # paid correction: a new reading with a fresh window
            async def paid_edit():
                text = await AstrologyInterpretationService().interpret(new_chart)
                await save({"interpretation": text, "unlocked": True, "unlocked_at": now_iso(), "free_edits_used": 0})
            await credits.spend(db, uid, "kundli", paid_edit)
            charged = True
    except credits.InsufficientCredits as exc:
        raise _payment_required(exc) from exc
    except AIServiceUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"profile_id": profile_id, "kundli": _public_chart(new_chart), "charged": charged}

@api.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: str, user=Depends(current_user)):
    """Deletes the person and everything stored about them: their Kundlis and chat history."""
    uid = user["id"]
    if not await db.profiles.find_one({"id": profile_id, "user_id": uid}, {"_id": 0, "id": 1}):
        raise HTTPException(404, "Profile not found")
    k = await db.kundlis.delete_many({"user_id": uid, "profile_id": profile_id})
    c = await db.ai_conversations.delete_many({"user_id": uid, "profile_id": profile_id})
    await db.profiles.delete_one({"id": profile_id, "user_id": uid})
    return {"deleted": True, "kundlis": k.deleted_count, "conversations": c.deleted_count}

@api.post("/palm")
async def palm(data: PalmInput, user=Depends(current_user)):
    if not await credits.has_credit(db, user["id"], "palm"):
        raise _payment_required(credits.InsufficientCredits("palm"))    # cheap check first, before any image work
    match = re.match(r"^data:(image/(?:jpeg|png|webp));base64,(.+)$", data.image_base64, re.DOTALL)
    if not match:
        raise HTTPException(400, "Please upload a JPG, PNG, or WEBP image")
    try:
        image_bytes = base64.b64decode(match.group(2), validate=True)
    except Exception as exc:
        raise HTTPException(400, "The uploaded image is not valid base64 data") from exc
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(413, "Please upload an image smaller than 10MB")
    signatures = {"image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG\r\n\x1a\n",), "image/webp": (b"RIFF",)}
    if not any(image_bytes.startswith(signature) for signature in signatures[match.group(1)]):
        raise HTTPException(400, "The file contents do not match a JPG, PNG, or WEBP image")
    async def analyze():
        reading = await PalmReadingService().analyze(match.group(2), data.hand)
        if "INVALID_PALM" in reading.upper():
            raise HTTPException(422, "The AI could not confirm a clear palm. Please use good lighting and show the entire palm. Your credit was not used.")
        return reading
    try:
        reading = await credits.spend(db, user["id"], "palm", analyze)
    except credits.InsufficientCredits as exc:
        raise _payment_required(exc) from exc
    except AIServiceUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    doc = {"id": str(uuid.uuid4()), "user_id": user["id"], "hand": data.hand, "reading": reading, "unlocked": True, "created_at": now_iso()}
    await db.palm_readings.insert_one(dict(doc)); doc.pop("user_id", None)
    return _apply_palm_lock(doc)

@api.get("/palm/latest")
async def latest_palm(user=Depends(current_user)):
    result = await db.palm_readings.find_one({"user_id": user["id"]}, {"_id": 0, "user_id": 0}, sort=[("created_at", -1)])
    if not result: raise HTTPException(404, "No palm readings yet")
    return _apply_palm_lock(result)

@api.post("/chat")
async def chat(data: ChatInput, user=Depends(current_user)):
    """Every chat is locked to ONE saved person. Only that person's chart is ever given to the AI."""
    uid = user["id"]
    await _adopt_legacy_kundlis(uid)
    if data.profile_id:
        profile = await db.profiles.find_one({"id": data.profile_id, "user_id": uid}, {"_id": 0})
        if not profile: raise HTTPException(404, "That person is not in your saved profiles")
    else:
        mine = await db.profiles.find({"user_id": uid}, {"_id": 0}).to_list(2)
        if not mine: raise HTTPException(400, "Create a Kundli before asking your chart a question")
        if len(mine) > 1: raise HTTPException(400, "Choose whose Kundli you want to ask about")
        profile = mine[0]
    chart = await db.kundlis.find_one({"user_id": uid, "profile_id": profile["id"]}, {"_id": 0}, sort=[("created_at", -1)])
    if not chart: raise HTTPException(400, f"Create a Kundli for {profile['name']} before asking about it")
    await _rate_limit(db.ai_conversations, uid, CHAT_MAX_PER_WINDOW, timedelta(minutes=CHAT_WINDOW_MINUTES),
                      "You are asking too quickly. Please wait a few minutes and try again.")
    async def answer_it():
        return await KundliChatService().answer(data.question, chart)
    try:
        answer = await credits.spend(db, uid, "chat", answer_it)
    except credits.InsufficientCredits as exc:
        raise _payment_required(exc) from exc
    except AIServiceUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    await db.ai_conversations.insert_one({"id": str(uuid.uuid4()), "user_id": uid, "profile_id": profile["id"], "question": data.question, "answer": answer, "created_at": now_iso()})
    return {"answer": answer, "profile_id": profile["id"], **(await credits.balances(db, uid))}

@api.get("/dashboard")
async def dashboard(user=Depends(current_user)):
    await touch_user(user["id"])
    profiles = await _profiles_view(user["id"])  # first: adopts pre-profile Kundlis
    kundli = await db.kundlis.find_one({"user_id": user["id"]}, {"_id": 0, "user_id": 0}, sort=[("created_at", -1)])
    palms = await db.palm_readings.count_documents({"user_id": user["id"]})
    ent = await entitlements_for(user["id"])
    if kundli:
        kundli = _apply_kundli_lock(kundli)
    return {"user": user, "kundli": kundli, "palm_readings": palms, "entitlements": ent, "profiles": profiles}

# -------- Razorpay payments --------

@api.get("/payments/config")
async def payments_config():
    return {
        "configured": payments.configured,
        "key_id": payments.key_id if payments.configured else "",
        "test_mode": payments.test_mode,
        "prices": PRICES_PAISE,
        "currency": "INR",
    }

@api.post("/payments/order")
async def create_payment_order(data: OrderInput, user=Depends(current_user)):
    if not is_valid_product(data.product):
        raise HTTPException(400, "Unknown product")
    await _rate_limit(db.orders, user["id"], 20, timedelta(hours=1), "Too many payment attempts. Please try again later.")
    if not payments.configured:
        raise HTTPException(503, "Razorpay is not configured on the server. Ask the administrator to set RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, and RAZORPAY_WEBHOOK_SECRET.")
    try:
        order = payments.create_order(data.product, user["id"])
    except PaymentConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "Could not create a Razorpay order. Please try again.") from exc
    await db.orders.insert_one({
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "product": data.product,
        "amount": order["amount"],
        "currency": order["currency"],
        "razorpay_order_id": order["razorpay_order_id"],
        "razorpay_payment_id": None,
        "payment_status": "created",
        "receipt": order["receipt"],
        "created_at": now_iso(),
        "paid_at": None,
    })
    return {
        "razorpay_order_id": order["razorpay_order_id"],
        "amount": order["amount"],
        "currency": order["currency"],
        "product": order["product"],
        "product_title": order["product_title"],
        "key_id": order["key_id"],
    }

@api.post("/payments/verify")
async def verify_payment(data: VerifyInput, user=Depends(current_user)):
    # Look up the order server-side by the razorpay_order_id, restricted to this user.
    order = await db.orders.find_one({"razorpay_order_id": data.razorpay_order_id, "user_id": user["id"]}, {"_id": 0})
    if not order:
        raise HTTPException(404, "We could not find that order. Please try again.")
    if order.get("payment_status") == "paid":
        # Idempotent: already verified before (perhaps by webhook). Re-granting is a no-op if it exists.
        await credits.grant_for_order(db, order)
        ent = await entitlements_for(user["id"])
        return {"status": "paid", "already": True, "product": order["product"], "entitlements": ent}
    if not payments.verify_checkout_signature(data.razorpay_order_id, data.razorpay_payment_id, data.razorpay_signature):
        await db.orders.update_one(
            {"razorpay_order_id": data.razorpay_order_id, "user_id": user["id"]},
            {"$set": {"payment_status": "signature_failed", "razorpay_payment_id": data.razorpay_payment_id, "signature_checked_at": now_iso()}},
        )
        raise HTTPException(400, "Payment signature could not be verified. If your card was charged, our webhook will reconcile the payment shortly.")
    await db.orders.update_one(
        {"razorpay_order_id": data.razorpay_order_id, "user_id": user["id"], "payment_status": {"$ne": "paid"}},
        {"$set": {"payment_status": "paid", "razorpay_payment_id": data.razorpay_payment_id, "paid_at": now_iso(), "verification_source": "checkout"}},
    )
    await credits.grant_for_order(db, order)
    ent = await entitlements_for(user["id"])
    return {"status": "paid", "already": False, "product": order["product"], "entitlements": ent}

@api.post("/payments/webhook")
async def payments_webhook(request: Request):
    raw = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    if not payments.webhook_secret:
        raise HTTPException(503, "Webhook secret is not configured")
    if not payments.verify_webhook_signature(raw, signature):
        raise HTTPException(400, "Invalid webhook signature")
    try:
        payload = __import__("json").loads(raw.decode())
    except Exception as exc:
        raise HTTPException(400, "Malformed webhook payload") from exc
    event_id = payload.get("id") or payload.get("event_id") or f"{payload.get('event','')}:{payload.get('payload',{}).get('payment',{}).get('entity',{}).get('id','')}"
    # Idempotency: reject if we have processed this event id before.
    existing = await db.payment_events.find_one({"event_id": event_id}, {"_id": 0, "event_id": 1})
    if existing:
        return {"status": "duplicate"}
    await db.payment_events.insert_one({"event_id": event_id, "event": payload.get("event"), "received_at": now_iso(), "raw": payload})
    event = payload.get("event")
    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {}) or {}
    order_entity = payload.get("payload", {}).get("order", {}).get("entity", {}) or {}
    razorpay_order_id = payment_entity.get("order_id") or order_entity.get("id")
    razorpay_payment_id = payment_entity.get("id")
    if not razorpay_order_id:
        return {"status": "ignored", "reason": "no order id"}
    order = await db.orders.find_one({"razorpay_order_id": razorpay_order_id}, {"_id": 0})
    if not order:
        return {"status": "ignored", "reason": "unknown order"}
    if event in ("payment.captured", "order.paid"):
        await db.orders.update_one(
            {"razorpay_order_id": razorpay_order_id, "payment_status": {"$ne": "paid"}},
            {"$set": {"payment_status": "paid", "razorpay_payment_id": razorpay_payment_id, "paid_at": now_iso(), "verification_source": event}},
        )
        await credits.grant_for_order(db, order)
    elif event == "payment.failed":
        await db.orders.update_one(
            {"razorpay_order_id": razorpay_order_id, "payment_status": {"$nin": ["paid"]}},
            {"$set": {"payment_status": "failed", "razorpay_payment_id": razorpay_payment_id, "failed_at": now_iso(), "failure_reason": payment_entity.get("error_description")}},
        )
    return {"status": "processed", "event": event}

@api.get("/payments/orders")
async def list_orders(user=Depends(current_user)):
    docs = await db.orders.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(50)
    return {"orders": docs}

@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    """Always answer with a plain-text message (the website shows it as-is; a list would crash the page)."""
    first = (exc.errors() or [{}])[0]
    field = ".".join(str(x) for x in first.get("loc", ()) if x not in ("body", "query"))
    msg = str(first.get("msg", "Invalid input")).replace("Value error, ", "")
    return JSONResponse(status_code=422, content={"detail": f"{field}: {msg}" if field else msg})

@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"detail": "Request is too large"})
    return await call_next(request)

app.include_router(api)
app.add_middleware(CORSMiddleware, allow_credentials=True, allow_origins=[o.strip() for o in os.environ.get("CORS_ORIGINS", "https://www.astroreveal.in,https://astroreveal.in").split(",") if o.strip()], allow_methods=["*"], allow_headers=["*"])
@app.on_event("startup")
async def startup():
    # A database hiccup while creating indexes must never stop the whole API from starting.
    # The error is logged loudly so it shows up in Railway's Deploy Logs.
    try:
        await credits.ensure_indexes(db)
    except Exception as exc:
        logging.getLogger("uvicorn.error").error("Could not create credit_grants indexes: %r", exc)
    try:
        await db.auth_attempts.create_index("at", expireAfterSeconds=24 * 3600)
        await db.auth_attempts.create_index([("kind", 1), ("key", 1), ("at", 1)])
    except Exception as exc:
        logging.getLogger("uvicorn.error").error("Could not create auth_attempts indexes: %r", exc)
    app.state.retention_task = asyncio.create_task(_retention_loop())
@app.on_event("shutdown")
async def shutdown():
    task = getattr(app.state, "retention_task", None)
    if task: task.cancel()
    client.close()
