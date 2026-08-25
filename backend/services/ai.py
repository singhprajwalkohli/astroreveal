import os
import uuid

from emergentintegrations.llm.chat import ImageContent, LlmChat, StreamDone, TextDelta, UserMessage


class AIServiceUnavailable(Exception):
    pass


async def ask_ai(prompt: str, image_base64: str | None = None, system: str = ""):
    key = os.environ.get("EMERGENT_LLM_KEY")
    if not key:
        raise AIServiceUnavailable("AI interpretation is not configured on the server")
    try:
        chat = LlmChat(api_key=key, session_id=str(uuid.uuid4()), system_message=system or "You are a responsible Vedic astrology assistant. Use traditional language, never make certain predictions, never provide medical advice, and never invent chart data.").with_model("openai", "gpt-5.4")
        attachments = [ImageContent(image_base64=image_base64)] if image_base64 else []
        result = []
        async for event in chat.stream_message(UserMessage(text=prompt, file_contents=attachments)):
            if isinstance(event, TextDelta):
                result.append(event.content)
            elif isinstance(event, StreamDone):
                break
        text = "".join(result).strip()
        if not text:
            raise AIServiceUnavailable("AI interpretation returned no content")
        return text
    except AIServiceUnavailable:
        raise
    except Exception as exc:
        raise AIServiceUnavailable("AI interpretation is temporarily unavailable. Please try again.") from exc


class PalmReadingService:
    async def analyze(self, image_base64: str, hand: str):
        return await ask_ai(f"Analyze this {hand} palm photograph for a traditional palmistry reading. First say whether a clear palm is visible. If unclear, say INVALID_PALM. Otherwise return concise sections titled Personality, Life line, Heart line, Head line, Fate line, Career, Relationships, Finance, Strengths, Challenges, and Outlook. Use responsible, non-certain language and mention this is traditional palmistry. Do not claim scientific validation.", image_base64=image_base64, system="You are a careful palmistry image analyst. Never invent details when the palm is not visible or clear. Do not diagnose health or predict death, and clearly distinguish traditional interpretation from scientific fact.")


class AstrologyInterpretationService:
    async def interpret(self, kundli: dict):
        calculation = {key: value for key, value in kundli.items() if key not in {"interpretation", "user_id", "id", "created_at"}}
        return await ask_ai(f"Interpret only this server-calculated Vedic chart data: {calculation}. Cover Career, Finance, Love & Relationships, Marriage, Education, Family, Travel, and Life Periods. Never calculate, estimate, or add planetary positions. Use suggests/may language and include no medical, death, or guaranteed wealth claims.")


class KundliChatService:
    async def answer(self, question: str, kundli: dict):
        calculation = {key: value for key, value in kundli.items() if key not in {"interpretation", "user_id", "id", "created_at"}}
        return await ask_ai(f"Answer the user's question using only this server-calculated chart data: {calculation}. If the answer is not supported by those fields, say that clearly. Never invent or estimate planetary positions. Use responsible traditional astrology language. Question: {question}")