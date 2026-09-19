from dotenv import load_dotenv
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field
from passlib.context import CryptContext
from datetime import datetime, timezone, timedelta
from pathlib import Path
import base64, os, re, jwt, uuid
from services.astrology import AstrologyCalculationError, AstrologyService
from services.ai import AIServiceUnavailable, PalmReadingService, AstrologyInterpretationService, KundliChatService
from services.payments import (
    PaymentService, PaymentConfigurationError, PRICES_PAISE, PRODUCT_TITLES,
    granted_features_for_product, is_valid_product, now_iso, truncate_for_preview,
)

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")
def _required_env(name: str) -> str: value = os.environ.get(name) if not value: raise RuntimeError(f"Required environment variable {name} is not set. Set it in Railway's Variables tab.") 
return value client = AsyncIOMotorClient(_required_env("MONGO_URL"))
db = client[_required_env("DB_NAME")]
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET = os.environ.get("JWT_SECRET", "astroai-local-secret")
app = FastAPI(title="AstroAI API")
api = APIRouter(prefix="/api")
astrology = AstrologyService()
payments = PaymentService()

KUNDLI_PREVIEW_CHARS = 620
PALM_PREVIEW_CHARS = 420

class AuthInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
class ProfileInput(BaseModel):
    name: str; dob: str; birth_time: str; birthplace: str
    latitude: float | None = None; longitude: float | None = None; timezone_name: str | None = None
class PalmInput(BaseModel):
    image_base64: str; hand: str
class ChatInput(BaseModel):
    question: str = Field(min_length=2)
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
    user = await db.users.find_one({"id": uid}, {"_id": 0, "password_hash": 0})
    if not user: raise HTTPException(401, "User not found")
    return user

async def entitlements_for(user_id: str) -> dict:
    features: set[str] = set()
    products: set[str] = set()
    async for doc in db.orders.find({"user_id": user_id, "payment_status": "paid"}, {"_id": 0, "product": 1}):
        products.add(doc["product"])
        features |= granted_features_for_product(doc["product"])
    return {"palm": "palm" in features, "kundli": "kundli" in features, "products": sorted(products)}

def _apply_kundli_entitlement(chart: dict, ent: dict) -> dict:
    chart = {**chart}
    if ent["kundli"]:
        chart["locked"] = False
        chart["preview"] = False
        return chart
    interpretation = chart.get("interpretation") or ""
    chart["interpretation"] = truncate_for_preview(interpretation, KUNDLI_PREVIEW_CHARS)
    chart["locked"] = True
    chart["preview"] = True
    chart["unlock_product"] = "kundli"
    chart["unlock_price_paise"] = PRICES_PAISE["kundli"]
    return chart

def _apply_palm_entitlement(doc: dict, ent: dict) -> dict:
    doc = {**doc}
    if ent["palm"]:
        doc["locked"] = False
        doc["preview"] = False
        return doc
    reading = doc.get("reading") or ""
    doc["reading"] = truncate_for_preview(reading, PALM_PREVIEW_CHARS)
    doc["locked"] = True
    doc["preview"] = True
    doc["unlock_product"] = "palm"
    doc["unlock_price_paise"] = PRICES_PAISE["palm"]
    return doc

@api.get("/")
async def root(): return {"message": "AstroAI API ready"}
@app.get("/") async def health(): return {"status": "ok"}
    
@api.get("/geocode/search")
async def geocode_search(q: str):
    q = q.strip()
    if len(q) < 3:
        return {"results": []}
    results = await astrology.locations.search(q, limit=6)
    return {"results": results}

@api.post("/auth/register")
async def register(data: AuthInput):
    if await db.users.find_one({"email": data.email.lower()}): raise HTTPException(409, "An account with this email already exists")
    user = {"id": str(uuid.uuid4()), "email": data.email.lower(), "password_hash": pwd.hash(data.password), "created_at": now_iso()}
    await db.users.insert_one(user)
    return {"token": token_for(user["id"]), "user": {"id": user["id"], "email": user["email"]}}

@api.post("/auth/login")
async def login(data: AuthInput):
    user = await db.users.find_one({"email": data.email.lower()})
    if not user or not pwd.verify(data.password, user["password_hash"]): raise HTTPException(401, "Email or password is incorrect")
    return {"token": token_for(user["id"]), "user": {"id": user["id"], "email": user["email"]}}

@api.get("/auth/me")
async def me(user=Depends(current_user)): return user

@api.get("/entitlements")
async def get_entitlements(user=Depends(current_user)):
    return await entitlements_for(user["id"])

@api.post("/kundli")
async def create_kundli(data: ProfileInput, user=Depends(current_user)):
    try:
        result = await astrology.calculate_kundli(**data.model_dump())
    except AstrologyCalculationError as exc:
        raise HTTPException(422, str(exc)) from exc
    chart_id = str(uuid.uuid4())
    created_at = now_iso()
    chart_doc = {**result, "id": chart_id, "user_id": user["id"], "created_at": created_at}
    await db.kundlis.insert_one(chart_doc)
    try:
        interpretation = await AstrologyInterpretationService().interpret(chart_doc)
    except AIServiceUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    await db.kundlis.update_one({"id": chart_id, "user_id": user["id"]}, {"$set": {"interpretation": interpretation}})
    chart_doc["interpretation"] = interpretation
    chart_doc.pop("user_id", None); chart_doc.pop("_id", None)
    ent = await entitlements_for(user["id"])
    return _apply_kundli_entitlement(chart_doc, ent)

@api.get("/kundli/latest")
async def latest_kundli(user=Depends(current_user)):
    result = await db.kundlis.find_one({"user_id": user["id"]}, {"_id": 0, "user_id": 0}, sort=[("created_at", -1)])
    if not result: raise HTTPException(404, "Create your first Kundli to see it here")
    ent = await entitlements_for(user["id"])
    return _apply_kundli_entitlement(result, ent)

@api.post("/palm")
async def palm(data: PalmInput, user=Depends(current_user)):
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
    try:
        reading = await PalmReadingService().analyze(match.group(2), data.hand)
    except AIServiceUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if "INVALID_PALM" in reading.upper():
        raise HTTPException(422, "The AI could not confirm a clear palm. Please use good lighting and show the entire palm.")
    doc = {"id": str(uuid.uuid4()), "user_id": user["id"], "hand": data.hand, "reading": reading, "created_at": now_iso()}
    await db.palm_readings.insert_one(doc); doc.pop("user_id", None); doc.pop("_id", None)
    ent = await entitlements_for(user["id"])
    return _apply_palm_entitlement(doc, ent)

@api.get("/palm/latest")
async def latest_palm(user=Depends(current_user)):
    result = await db.palm_readings.find_one({"user_id": user["id"]}, {"_id": 0, "user_id": 0}, sort=[("created_at", -1)])
    if not result: raise HTTPException(404, "No palm readings yet")
    ent = await entitlements_for(user["id"])
    return _apply_palm_entitlement(result, ent)

@api.post("/chat")
async def chat(data: ChatInput, user=Depends(current_user)):
    chart = await db.kundlis.find_one({"user_id": user["id"]}, {"_id": 0}, sort=[("created_at", -1)])
    if not chart: raise HTTPException(400, "Create a Kundli before asking your chart a question")
    try:
        answer = await KundliChatService().answer(data.question, chart)
    except AIServiceUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    await db.ai_conversations.insert_one({"id": str(uuid.uuid4()), "user_id": user["id"], "question": data.question, "answer": answer, "created_at": now_iso()})
    return {"answer": answer}

@api.get("/dashboard")
async def dashboard(user=Depends(current_user)):
    kundli = await db.kundlis.find_one({"user_id": user["id"]}, {"_id": 0, "user_id": 0}, sort=[("created_at", -1)])
    palms = await db.palm_readings.count_documents({"user_id": user["id"]})
    ent = await entitlements_for(user["id"])
    if kundli:
        kundli = _apply_kundli_entitlement(kundli, ent)
    return {"user": user, "kundli": kundli, "palm_readings": palms, "entitlements": ent}

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
        # Idempotent: already verified before (perhaps by webhook).
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

app.include_router(api)
app.add_middleware(CORSMiddleware, allow_credentials=True, allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","), allow_methods=["*"], allow_headers=["*"])
@app.on_event("shutdown")
async def shutdown(): client.close()
