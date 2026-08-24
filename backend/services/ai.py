import os
import uuid
from emergentintegrations.llm.chat import LlmChat, UserMessage, ImageContent, TextDelta


async def ask_ai(prompt: str, image_base64: str | None = None, system: str = ""):
    key = os.environ.get("EMERGENT_LLM_KEY")
    if not key:
        return None
    try:
        chat = LlmChat(api_key=key, session_id=str(uuid.uuid4()), system_message=system or "You are a responsible Vedic astrology assistant. Use traditional language, never make certain predictions, and never provide medical advice.").with_model("openai", "gpt-5.4")
        contents = [ImageContent(image_base64=image_base64)] if image_base64 else None
        result = []
        async for event in chat.stream_message(UserMessage(text=prompt, file_contents=contents or [])):
            if isinstance(event, TextDelta):
                result.append(event.content)
        return "".join(result).strip() or None
    except Exception:
        return None


class PalmReadingService:
    async def analyze(self, image_base64: str, hand: str):
        prompt = f"Analyze this {hand} palm photograph for a traditional palmistry reading. First say whether a clear palm is visible. If unclear, say INVALID_PALM. Otherwise return concise sections titled Personality, Life line, Heart line, Head line, Fate line, Career, Relationships, Finance, Strengths, Challenges, and Outlook. Use responsible, non-certain language and mention this is traditional palmistry."
        return await ask_ai(prompt, image_base64=image_base64)


class AstrologyInterpretationService:
    async def interpret(self, kundli: dict):
        prompt = f"Give a warm, concise traditional Vedic astrology interpretation based only on this calculated chart data: {kundli}. Cover Career, Finance, Love & Relationships, Marriage, Education, Family, Travel, and Life Periods. Do not invent planets. Use suggests/may language and include no medical, death, or guaranteed wealth claims."
        return await ask_ai(prompt)


class KundliChatService:
    async def answer(self, question: str, kundli: dict):
        return await ask_ai(f"Answer this user's Kundli question using only the chart data below. If it is not answerable from the data, say so. Use responsible traditional astrology language. Chart: {kundli}\nQuestion: {question}")