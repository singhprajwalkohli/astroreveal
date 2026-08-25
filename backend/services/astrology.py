import asyncio
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import swisseph as swe
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder


SIGNS = ["Mesha", "Vrishabha", "Mithuna", "Karka", "Simha", "Kanya", "Tula", "Vrischika", "Dhanu", "Makara", "Kumbha", "Meena"]
NAKSHATRAS = ["Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashira", "Ardra", "Punarvasu", "Pushya", "Ashlesha", "Magha", "Purva Phalguni", "Uttara Phalguni", "Hasta", "Chitra", "Swati", "Vishakha", "Anuradha", "Jyeshtha", "Mula", "Purva Ashadha", "Uttara Ashadha", "Shravana", "Dhanishtha", "Shatabhisha", "Purva Bhadrapada", "Uttara Bhadrapada", "Revati"]
NAK_LORDS = ["Ketu", "Venus", "Sun", "Moon", "Mars", "Rahu", "Jupiter", "Saturn"]
DASHA_YEARS = {"Ketu": 7, "Venus": 20, "Sun": 6, "Moon": 10, "Mars": 7, "Rahu": 18, "Jupiter": 16, "Saturn": 19, "Mercury": 17}
PLANETS = [("Sun", swe.SUN), ("Moon", swe.MOON), ("Mars", swe.MARS), ("Mercury", swe.MERCURY), ("Jupiter", swe.JUPITER), ("Venus", swe.VENUS), ("Saturn", swe.SATURN), ("Rahu", swe.MEAN_NODE), ("Ketu", swe.MEAN_NODE)]


class AstrologyCalculationError(Exception):
    pass


class LocationService:
    def __init__(self):
        self.geocoder = Nominatim(user_agent="astroai-kundli/1.0")
        self.timezone_finder = TimezoneFinder()

    async def resolve(self, birthplace: str, latitude: float | None = None, longitude: float | None = None, timezone_name: str | None = None):
        if latitude is not None and longitude is not None and timezone_name:
            try:
                ZoneInfo(timezone_name)
                return {"query": birthplace, "display_name": birthplace, "latitude": latitude, "longitude": longitude, "timezone": timezone_name, "source": "user-provided coordinates"}
            except Exception as exc:
                raise AstrologyCalculationError("The supplied timezone is not valid") from exc
        try:
            result = await asyncio.to_thread(self.geocoder.geocode, birthplace, exactly_one=True, addressdetails=True, timeout=8)
        except Exception as exc:
            raise AstrologyCalculationError("Birthplace lookup is temporarily unavailable. Please try again or provide exact coordinates.") from exc
        if not result:
            raise AstrologyCalculationError("We could not resolve that birthplace. Please enter a more specific city or town.")
        tz = self.timezone_finder.timezone_at(lng=result.longitude, lat=result.latitude)
        if not tz:
            raise AstrologyCalculationError("We could not determine a timezone for that birthplace.")
        return {"query": birthplace, "display_name": result.address, "latitude": round(result.latitude, 6), "longitude": round(result.longitude, 6), "timezone": tz, "source": "Nominatim geocoder + TimezoneFinder"}


def _sign(longitude: float):
    index = int(longitude // 30) % 12
    return {"name": SIGNS[index], "index": index, "degrees": round(longitude % 30, 6)}


def _nakshatra(longitude: float):
    span = 360 / 27
    index = min(26, int(longitude / span))
    within = longitude - index * span
    pada = min(4, int(within / (span / 4)) + 1)
    return {"name": NAKSHATRAS[index], "index": index, "pada": pada, "lord": NAK_LORDS[index % 8]}


def _whole_sign_house(planet_sign_index: int, asc_sign_index: int):
    return ((planet_sign_index - asc_sign_index) % 12) + 1


def _dasha_schedule(moon_longitude: float, birth_local: datetime):
    nak = _nakshatra(moon_longitude)
    lord_index = NAK_LORDS.index(nak["lord"])
    span = 360 / 27
    elapsed = (moon_longitude % span) / span
    remaining_years = DASHA_YEARS[nak["lord"]] * (1 - elapsed)
    schedule, cursor = [], birth_local
    for offset in range(9):
        lord = NAK_LORDS[(lord_index + offset) % 8]
        years = remaining_years if offset == 0 else DASHA_YEARS[lord]
        end = cursor + timedelta(days=years * 365.2425)
        schedule.append({"period": f"{lord} Mahadasha", "lord": lord, "start": cursor.date().isoformat(), "end": end.date().isoformat(), "years": round(years, 4)})
        cursor = end
    return schedule


class AstrologyService:
    """Swiss Ephemeris calculation boundary. No planetary values are estimated or mocked."""

    def __init__(self):
        self.locations = LocationService()

    async def calculate_kundli(self, name: str, dob: str, birth_time: str, birthplace: str, latitude: float | None = None, longitude: float | None = None, timezone_name: str | None = None):
        try:
            location = await self.locations.resolve(birthplace, latitude, longitude, timezone_name)
            birth_local = datetime.strptime(f"{dob} {birth_time}", "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(location["timezone"]))
        except AstrologyCalculationError:
            raise
        except Exception as exc:
            raise AstrologyCalculationError("Date, time, or timezone could not be parsed") from exc

        birth_utc = birth_local.astimezone(timezone.utc)
        hour_ut = birth_utc.hour + birth_utc.minute / 60 + birth_utc.second / 3600
        jd_ut = swe.julday(birth_utc.year, birth_utc.month, birth_utc.day, hour_ut)
        swe.set_sid_mode(swe.SIDM_LAHIRI)
        flags = swe.FLG_SWIEPH | swe.FLG_SIDEREAL | swe.FLG_SPEED
        cusps, ascmc = swe.houses_ex(jd_ut, location["latitude"], location["longitude"], b"W", swe.FLG_SIDEREAL)
        ascendant = ascmc[0] % 360
        asc_sign = _sign(ascendant)
        planets, moon_longitude = [], None
        for planet_name, body in PLANETS:
            body_id, retrograde = body, False
            if planet_name == "Ketu":
                body_id, retrograde = swe.MEAN_NODE, False
            values, _, _ = swe.calc_ut(jd_ut, body_id, flags)
            longitude_value, speed = values[0] % 360, values[3]
            if planet_name == "Ketu":
                longitude_value = (longitude_value + 180) % 360
            if planet_name == "Moon":
                moon_longitude = longitude_value
            sign = _sign(longitude_value)
            planets.append({"planet": planet_name, "longitude": round(longitude_value, 6), "sign": sign["name"], "sign_degrees": sign["degrees"], "house": _whole_sign_house(sign["index"], asc_sign["index"]), "nakshatra": _nakshatra(longitude_value), "retrograde": bool(speed < 0) if planet_name not in ("Sun", "Moon", "Rahu", "Ketu") else False})

        houses = []
        for house in range(1, 13):
            cusp = cusps[house] if len(cusps) > 12 else cusps[house - 1]
            sign = _sign(cusp % 360)
            houses.append({"house": house, "cusp_longitude": round(cusp % 360, 6), "sign": sign["name"]})
        ayanamsa = swe.get_ayanamsa_ut(jd_ut)
        return {"name": name, "dob": dob, "birth_time": birth_time, "birthplace": birthplace, "engine_status": "SWISS EPHEMERIS · LAHIRI SIDEREAL", "calculation_system": "Vedic sidereal zodiac with Lahiri ayanamsa and whole-sign houses", "location": location, "coordinates": {"latitude": location["latitude"], "longitude": location["longitude"], "timezone": location["timezone"]}, "utc_datetime": birth_utc.isoformat(), "julian_day_ut": round(jd_ut, 8), "ayanamsa_degrees": round(ayanamsa, 8), "lagna": asc_sign["name"], "lagna_longitude": round(ascendant, 6), "rashi": _sign(moon_longitude)["name"], "nakshatra": _nakshatra(moon_longitude), "planets": planets, "houses": houses, "dashas": _dasha_schedule(moon_longitude, birth_local), "yogas": []}