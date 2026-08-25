```python
import os
import base64

from google import genai
from google.genai import types


class AIServiceUnavailable(Exception):
    pass


def _client():
    key = os.environ.get("GEMINI_API_KEY")

    if not key:
        raise AIServiceUnavailable(
            "AI interpretation is not configured on the server"
        )

    return genai.Client(api_key=key)


async def ask_ai(
    prompt: str,
    image_base64: str | None = None,
    system: str = "",
):
    try:
        client = _client()

        contents = []

        if system:
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(
                            text=system
                        )
                    ],
                )
            )

        contents.append(
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text=prompt
                    )
                ],
            )
        )

        if image_base64:
            # Remove data URL prefix if present
            if "," in image_base64 and image_base64.startswith("data:"):
                image_base64 = image_base64.split(",", 1)[1]

            try:
                image_bytes = base64.b64decode(
                    image_base64,
                    validate=True
                )
            except Exception as exc:
                raise AIServiceUnavailable(
                    "The uploaded image could not be processed"
                ) from exc

            contents[-1].parts.append(
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type="image/jpeg",
                )
            )

        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
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
            "AI interpretation is temporarily unavailable. Please try again."
        ) from exc


class PalmReadingService:

    async def analyze(
        self,
        image_base64: str,
        hand: str
    ):
        return await ask_ai(
            f"""
Analyze this {hand} palm photograph for a traditional palmistry reading.

First determine whether a clear human palm is actually visible.

If the palm is unclear, too blurry, partially hidden, or not a palm,
return exactly:

INVALID_PALM

If a clear palm is visible, provide concise sections:

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

Only describe lines or features that are reasonably visible.

Use responsible, non-certain language.

Clearly state that palmistry is a traditional/spiritual interpretation
and is not scientifically validated.

Do not diagnose medical conditions.
Do not predict death.
Do not make guaranteed predictions.
""",
            image_base64=image_base64,
            system="""
You are a careful palmistry image analyst.

Never invent palm lines or features that cannot reasonably be seen.

If the image quality is insufficient to interpret the palm,
return INVALID_PALM.

Do not provide medical diagnoses or claims.
Do not present palmistry as scientific fact.
""",
        )


class AstrologyInterpretationService:

    async def interpret(self, kundli: dict):

        calculation = {
            key: value
            for key, value in kundli.items()
            if key not in {
                "interpretation",
                "user_id",
                "id",
                "created_at",
            }
        }

        return await ask_ai(
            f"""
Interpret ONLY the following server-calculated Vedic astrology data:

{calculation}

Cover, where supported by the supplied data:

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

Do not invent houses, planets, nakshatras, dashas, yogas, or dates.

If something cannot be determined from the supplied data,
clearly say that it cannot be determined.

Use language such as:
"may suggest"
"traditionally interpreted as"
"could indicate"

Do not make guaranteed predictions.

Do not provide medical diagnoses.

Do not predict death.

Do not guarantee wealth or financial outcomes.

Explain that this is a traditional astrological interpretation.
""",
            system="""
You are a responsible Vedic astrology interpretation assistant.

The calculation engine has already calculated the astrology data.

Your job is ONLY to interpret the supplied data.

Never invent or recalculate planetary positions.
""",
        )


class KundliChatService:

    async def answer(
        self,
        question: str,
        kundli: dict
    ):

        calculation = {
            key: value
            for key, value in kundli.items()
            if key not in {
                "interpretation",
                "user_id",
                "id",
                "created_at",
            }
        }

        return await ask_ai(
            f"""
Answer the user's question using ONLY the following
server-calculated Vedic astrology data:

{calculation}

User question:

{question}

Do not invent or estimate planetary positions.

If the question cannot be answered using the supplied
chart data, clearly say so.

Use responsible traditional astrology language.

Do not provide medical diagnoses.

Do not predict death.

Do not guarantee financial or relationship outcomes.
""",
            system="""
You are the Ask Your Kundli AI assistant.

Use only the server-calculated Kundli data provided to you.

Never invent missing chart information.
Never calculate planetary positions yourself.
""",
        )
```
