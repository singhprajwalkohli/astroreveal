from datetime import datetime
from hashlib import sha256


class AstrologyService:
    """Replaceable calculation boundary. Current engine is clearly marked development data."""

    async def calculate_kundli(self, name: str, dob: str, birth_time: str, birthplace: str):
        seed = int(sha256(f"{dob}{birth_time}{birthplace}".encode()).hexdigest()[:6], 16)
        signs = ["Mesha", "Vrishabha", "Mithuna", "Karka", "Simha", "Kanya", "Tula", "Vrischika", "Dhanu", "Makara", "Kumbha", "Meena"]
        nakshatras = ["Ashwini", "Rohini", "Mrigashira", "Pushya", "Magha", "Hasta", "Swati", "Anuradha", "Mula", "Shravana", "Shatabhisha", "Revati"]
        planets = [
            ("Sun", signs[seed % 12], "10th"), ("Moon", signs[(seed // 3) % 12], "4th"),
            ("Mars", signs[(seed // 5) % 12], "7th"), ("Mercury", signs[(seed // 7) % 12], "11th"),
            ("Jupiter", signs[(seed // 11) % 12], "2nd"), ("Venus", signs[(seed // 13) % 12], "5th"),
            ("Saturn", signs[(seed // 17) % 12], "9th"), ("Rahu", signs[(seed // 19) % 12], "6th"),
            ("Ketu", signs[(seed // 23) % 12], "12th"),
        ]
        return {
            "name": name, "dob": dob, "birth_time": birth_time, "birthplace": birthplace,
            "engine_status": "MOCK DEVELOPMENT CALCULATIONS",
            "lagna": signs[seed % 12], "rashi": signs[(seed // 2) % 12], "nakshatra": nakshatras[seed % 12],
            "coordinates": {"latitude": round(8 + (seed % 2900) / 100, 4), "longitude": round(68 + (seed % 2900) / 100, 4), "timezone": "Asia/Kolkata"},
            "planets": [{"planet": p, "sign": s, "house": h} for p, s, h in planets],
            "dashas": [{"period": "Jupiter Mahadasha", "range": "2022 — 2038", "theme": "Learning, guidance and expansion"}, {"period": "Saturn Antardasha", "range": "2025 — 2028", "theme": "Structure, patience and responsibility"}],
            "yogas": ["Budhaditya Yoga — analytical expression", "Dhana tendency — steady resource building"],
        }