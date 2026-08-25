from dotenv import load_dotenv
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field
from passlib.context import CryptContext
from datetime import datetime, timezone, timedelta
from pathlib import Path
import base64, os, re, jwt, uuid
from services.astrology import AstrologyCalculationError, AstrologyService
from services.ai import AIServiceUnavailable, PalmReadingService, AstrologyInterpretationService, KundliChatService

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")
client = AsyncIOMotorClient(os.environ["MONGO_URL"])
db = client[os.environ["DB_NAME"]]
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET = os.environ.get("JWT_SECRET", "astroai-local-secret")
app = FastAPI(title="AstroAI API")
api = APIRouter(prefix="/api")
astrology = AstrologyService()

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

@api.get("/")
async def root(): return {"message": "AstroAI API ready"}

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
    user = {"id": str(uuid.uuid4()), "email": data.email.lower(), "password_hash": pwd.hash(data.password), "created_at": datetime.now(timezone.utc).isoformat()}
    await db.users.insert_one(user)
    return {"token": token_for(user["id"]), "user": {"id": user["id"], "email": user["email"]}}

@api.post("/auth/login")
async def login(data: AuthInput):
    user = await db.users.find_one({"email": data.email.lower()})
    if not user or not pwd.verify(data.password, user["password_hash"]): raise HTTPException(401, "Email or password is incorrect")
    return {"token": token_for(user["id"]), "user": {"id": user["id"], "email": user["email"]}}

@api.get("/auth/me")
async def me(user=Depends(current_user)): return user

@api.post("/kundli")
async def create_kundli(data: ProfileInput, user=Depends(current_user)):
    try:
        result = await astrology.calculate_kundli(**data.model_dump())
    except AstrologyCalculationError as exc:
        raise HTTPException(422, str(exc)) from exc
    chart_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    chart_doc = {**result, "id": chart_id, "user_id": user["id"], "created_at": created_at}
    await db.kundlis.insert_one(chart_doc)
    try:
        interpretation = await AstrologyInterpretationService().interpret(chart_doc)
    except AIServiceUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    await db.kundlis.update_one({"id": chart_id, "user_id": user["id"]}, {"$set": {"interpretation": interpretation}})
    chart_doc["interpretation"] = interpretation
    chart_doc.pop("user_id", None); chart_doc.pop("_id", None); return chart_doc

@api.get("/kundli/latest")
async def latest_kundli(user=Depends(current_user)):
    result = await db.kundlis.find_one({"user_id": user["id"]}, {"_id": 0, "user_id": 0}, sort=[("created_at", -1)])
    if not result: raise HTTPException(404, "Create your first Kundli to see it here")
    return result

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
    doc = {"id": str(uuid.uuid4()), "user_id": user["id"], "hand": data.hand, "reading": reading, "created_at": datetime.now(timezone.utc).isoformat()}
    await db.palm_readings.insert_one(doc); doc.pop("user_id", None); doc.pop("_id", None); return doc

@api.post("/chat")
async def chat(data: ChatInput, user=Depends(current_user)):
    chart = await db.kundlis.find_one({"user_id": user["id"]}, {"_id": 0}, sort=[("created_at", -1)])
    if not chart: raise HTTPException(400, "Create a Kundli before asking your chart a question")
    try:
        answer = await KundliChatService().answer(data.question, chart)
    except AIServiceUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    await db.ai_conversations.insert_one({"id": str(uuid.uuid4()), "user_id": user["id"], "question": data.question, "answer": answer, "created_at": datetime.now(timezone.utc).isoformat()})
    return {"answer": answer}

@api.get("/dashboard")
async def dashboard(user=Depends(current_user)):
    kundli = await db.kundlis.find_one({"user_id": user["id"]}, {"_id": 0, "user_id": 0}, sort=[("created_at", -1)])
    palms = await db.palm_readings.count_documents({"user_id": user["id"]})
    return {"user": user, "kundli": kundli, "palm_readings": palms}

app.include_router(api)
app.add_middleware(CORSMiddleware, allow_credentials=True, allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","), allow_methods=["*"], allow_headers=["*"])
@app.on_event("shutdown")
async def shutdown(): client.close()