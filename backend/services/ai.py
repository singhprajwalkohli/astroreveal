import os
import base64
import logging
logger = logging.getLogger(__name__)

from google import genai
from google.genai import types


# Fields the model does not need. Interpretation works from planets, houses, nakshatras
# and dashas that the server already calculated, so identity and birth-place details are
# not sent to the third-party AI provider. (Dasha dates can still imply a birth date.)
_NOT_SENT_TO_LLM = {
    "interpretation", "user_id", "id", "created_at", "unlocked", "_id",
    "name", "dob", "birth_time", "birthplace", "location", "coordinates",
    "utc_datetime", "julian_day_ut",
}


def chart_for_llm(kundli: dict) -> dict:
    return {k: v for k, v in kundli.items() if k not in _NOT_SENT_TO_LLM}


class AIServiceUnavailable(Exception):
    pass


def get_client():
    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:
        raise AIServiceUnavailable(
            "AI interpretation is not configured on the server"
        )

    return genai.Client(api_key=api_key)


async def ask_ai(
    prompt: str,
    image_base64: str | None = None,
    system: str = "",
):
    try:
        client = get_client()

        parts = []

        if image_base64:
            mime_type = "image/jpeg"

            if image_base64.startswith("data:"):
                header, image_base64 = image_base64.split(",", 1)

                if ";" in header:
                    mime_type = header.split(";", 1)[0].replace(
                        "data:", ""
                    )

            try:
                image_bytes = base64.b64decode(
                    image_base64,
                    validate=True,
                )
            except Exception as exc:
                logger.exception("ask_ai failed: %s", exc)
                raise AIServiceUnavailable(
                    "The uploaded image could not be processed"
                ) from exc

            parts.append(
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type=mime_type,
                )
            )

        parts.append(
            types.Part.from_text(
                text=prompt,
            )
        )

        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Content(
                    role="user",
                    parts=parts,
                )
            ],
            config=types.GenerateContentConfig(
                system_instruction=system
                or (
                    "You are a responsible Vedic astrology assistant. "
                    "Use traditional language. Never make certain "
                    "predictions. Never provide medical advice. "
                    "Never invent chart data."
                )
            ),
        )

        text = (response.text or "").strip()

        if not text:
            raise AIServiceUnavailable(
                "AI interpretation returned no content"
            )

        return text

    except AIServiceUnavailable:
        raise

    except Exception as exc:
        raise AIServiceUnavailable(
            "AI interpretation is temporarily unavailable. "
            "Please try again."
        ) from exc


class PalmReadingService:

    async def analyze(
        self,
        image_base64: str,
        hand: str,
    ):
        prompt = f"""
Analyze this {hand} palm photograph for a traditional
palmistry reading.

First determine whether a clear human palm is visible.

If the palm is unclear, too blurry, partially hidden,
or not actually a palm, return exactly:

INVALID_PALM

If a clear palm is visible, provide these sections:

Personality
Life line
Heart line
Head line
Fate line
Career
Relationships
Finance
Strengths
Challenges
Outlook

Only describe palm lines and features that are reasonably
visible in the photograph.

Use responsible, non-certain language.

Clearly state that palmistry is a traditional/spiritual
interpretation and is not scientifically validated.

Do not diagnose medical conditions.
Do not predict death.
Do not make guaranteed predictions.
"""

        system = """
You are a careful palmistry image analyst.

Never invent palm lines or features that cannot reasonably
be seen.

If the image quality is insufficient to interpret the palm,
return INVALID_PALM.

Do not provide medical diagnoses.
Do not predict death.
Do not present palmistry as scientific fact.
"""

        return await ask_ai(
            prompt=prompt,
            image_base64=image_base64,
            system=system,
        )


class AstrologyInterpretationService:

    async def interpret(self, kundli: dict):
        calculation = chart_for_llm(kundli)

        prompt = f"""
Interpret ONLY the following server-calculated Vedic
astrology data:

{calculation}

Cover the following areas where the supplied data
supports them:

Career
Finance
Love & Relationships
Marriage
Education
Family
Travel
Life Periods
Strengths
Challenges

IMPORTANT:

Do not calculate planetary positions yourself.

Do not estimate missing information.

Do not invent houses, planets, nakshatras, dashas,
yogas, or dates.

If something cannot be determined from the supplied
data, clearly say that it cannot be determined.

Use language such as:

"may suggest"
"traditionally interpreted as"
"could indicate"

Do not make guaranteed predictions.

Do not provide medical diagnoses.

Do not predict death.

Do not guarantee wealth or financial outcomes.

Explain that this is a traditional astrological
interpretation.
"""

        system = """
You are a responsible Vedic astrology interpretation
assistant.

The astrology calculation engine has already calculated
the chart data.

Your job is ONLY to interpret the supplied data.

Never invent or recalculate planetary positions.
Never create missing chart information.
"""

        return await ask_ai(
            prompt=prompt,
            system=system,
        )


class KundliChatService:

    async def answer(
        self,
        question: str,
        kundli: dict,
    ):
        calculation = chart_for_llm(kundli)

        prompt = f"""
Answer the user's question using ONLY the following
server-calculated Vedic astrology data:

{calculation}

User question:

{question}

Do not invent or estimate planetary positions.

This chart belongs to ONE person. If the question is about
any other person, say you can only answer about this chart and
that other people need their own Kundli.

If the question cannot be answered using the supplied
chart data, clearly say so.

Use responsible traditional astrology language.

Do not provide medical diagnoses.

Do not predict death.

Do not guarantee financial or relationship outcomes.
"""

        system = """
You are the AstroReveal Ask Your Kundli AI assistant.

Use only the server-calculated Kundli data provided to you.

Never invent missing chart information.
Never calculate planetary positions yourself.
"""

        return await ask_ai(
            prompt=prompt,
            system=system,
        )
