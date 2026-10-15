"""Weather from Open-Meteo (free, no account) and news headlines from RSS."""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET

import httpx

from . import config

log = logging.getLogger("weather")

WMO = {
    0: "clear skies", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains", 80: "light showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms", 96: "thunderstorms with hail",
    99: "thunderstorms with hail",
}


class Weather:
    def __init__(self) -> None:
        self._home: dict | None = None
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._http = httpx.AsyncClient(timeout=8, headers={"User-Agent": "pi-esp-jarvis/1.0"})

    # ------------------------------------------------------------ location
    async def home(self) -> dict | None:
        if self._home:
            return self._home
        if config.WEATHER_LOCATION:
            self._home = await self.geocode(config.WEATHER_LOCATION)
        if not self._home:
            try:  # rough location from the internet connection
                r = await self._http.get("http://ip-api.com/json/?fields=status,city,countryCode,lat,lon,timezone")
                j = r.json()
                if j.get("status") == "success":
                    self._home = {"name": j["city"], "lat": j["lat"], "lon": j["lon"],
                                  "country": j.get("countryCode", ""), "timezone": j.get("timezone", "")}
            except Exception as exc:
                log.warning("Could not detect location: %s", exc)
        if self._home:
            log.info("Weather location: %s", self._home["name"])
        return self._home

    async def geocode(self, name: str) -> dict | None:
        q = name.split(",")[0].strip()
        try:
            r = await self._http.get("https://geocoding-api.open-meteo.com/v1/search",
                                     params={"name": q, "count": 5, "language": "en", "format": "json"})
            results = r.json().get("results") or []
            if "," in name and len(results) > 1:  # "Paris, US" -> prefer that country
                want = name.split(",", 1)[1].strip().lower()
                results.sort(key=lambda x: want not in (x.get("country", "") + " " + x.get("country_code", "")).lower())
            if results:
                x = results[0]
                return {"name": x["name"], "lat": x["latitude"], "lon": x["longitude"],
                        "country": x.get("country_code", ""), "timezone": x.get("timezone", "")}
        except Exception as exc:
            log.warning("Geocoding %r failed: %s", name, exc)
        return None

    # ------------------------------------------------------------ forecast
    async def forecast(self, place: dict) -> dict | None:
        key = (round(place["lat"], 2), round(place["lon"], 2))
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        params = {
            "latitude": place["lat"], "longitude": place["lon"], "timezone": "auto", "forecast_days": 7,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,precipitation",
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                     "precipitation_sum,sunrise,sunset,uv_index_max,wind_speed_10m_max",
        }
        if config.WEATHER_UNITS == "imperial":
            params.update(temperature_unit="fahrenheit", wind_speed_unit="mph", precipitation_unit="inch")
        elif config.WEATHER_UNITS == "uk":
            params.update(wind_speed_unit="mph")
        try:
            r = await self._http.get("https://api.open-meteo.com/v1/forecast", params=params)
            r.raise_for_status()
            data = r.json()
            self._cache[key] = (time.time(), data)
            return data
        except Exception as exc:
            log.warning("Forecast failed: %s", exc)
            return None

    @staticmethod
    def _wind_unit() -> str:
        return "miles per hour" if config.WEATHER_UNITS in ("imperial", "uk") else "kilometres per hour"

    def as_context(self, place: dict, data: dict) -> str:
        """Compact facts for the language model."""
        c, d, h = data["current"], data["daily"], data["hourly"]
        lines = [f"Location: {place['name']}. Local time {c['time'].replace('T', ' ')}.",
                 f"Now: {round(c['temperature_2m'])} degrees (feels like {round(c['apparent_temperature'])}), "
                 f"{WMO.get(c['weather_code'], 'unknown')}, humidity {c['relative_humidity_2m']}%, "
                 f"wind {round(c['wind_speed_10m'])} {self._wind_unit()}, precipitation {c['precipitation']}."]
        now_idx = next((i for i, t in enumerate(h["time"]) if t >= c["time"][:13]), 0)
        nxt = [f"{h['time'][i][11:16]} {round(h['temperature_2m'][i])}° {h['precipitation_probability'][i]}% rain"
               for i in range(now_idx, min(now_idx + 12, len(h["time"])), 3)]
        lines.append("Next hours: " + "; ".join(nxt) + ".")
        for i, day in enumerate(d["time"]):
            name = ["Today", "Tomorrow"][i] if i < 2 else time.strftime("%A", time.strptime(day, "%Y-%m-%d"))
            lines.append(f"{name}: {WMO.get(d['weather_code'][i], 'unknown')}, high {round(d['temperature_2m_max'][i])}, "
                         f"low {round(d['temperature_2m_min'][i])}, {d['precipitation_probability_max'][i]}% chance of rain, "
                         f"UV {d['uv_index_max'][i]}, sunrise {d['sunrise'][i][11:]}, sunset {d['sunset'][i][11:]}.")
        return "\n".join(lines)

    def spoken(self, place: dict, data: dict, day: int = 0) -> str:
        c, d = data["current"], data["daily"]
        day = max(0, min(day, len(d["time"]) - 1))
        rain = d["precipitation_probability_max"][day]
        rain_txt = (f"an {rain}" if str(rain).startswith(("8", "11", "18")) else f"a {rain}") + " percent chance of rain" if rain else "no rain expected"
        if day == 0:
            return (f"Right now in {place['name']} it's {round(c['temperature_2m'])} degrees and "
                    f"{WMO.get(c['weather_code'], 'unclear')}. Today's high is {round(d['temperature_2m_max'][0])} "
                    f"with a low of {round(d['temperature_2m_min'][0])}, and {rain_txt}.")
        name = "Tomorrow" if day == 1 else "On " + time.strftime("%A", time.strptime(d["time"][day], "%Y-%m-%d"))
        return (f"{name} in {place['name']}: {WMO.get(d['weather_code'][day], 'unclear')}, with a high of "
                f"{round(d['temperature_2m_max'][day])} and a low of {round(d['temperature_2m_min'][day])}, "
                f"and {rain_txt}.")


# ---------------------------------------------------------------------------
# News headlines
# ---------------------------------------------------------------------------
async def headlines(limit: int = 5) -> list[str]:
    titles: list[str] = []
    async with httpx.AsyncClient(timeout=8, follow_redirects=True,
                                 headers={"User-Agent": "pi-esp-jarvis/1.0"}) as http:
        for url in [u.strip() for u in config.NEWS_FEEDS.split(",") if u.strip()]:
            try:
                r = await http.get(url)
                root = ET.fromstring(r.content)
                for item in root.iter():
                    if item.tag.endswith("item") or item.tag.endswith("entry"):
                        for child in item:
                            if child.tag.endswith("title") and child.text:
                                t = re.sub(r"\s+", " ", child.text).strip()
                                if t and t not in titles:
                                    titles.append(t)
                                break
                    if len(titles) >= limit:
                        break
            except Exception as exc:
                log.warning("News feed %s failed: %s", url, exc)
            if len(titles) >= limit:
                break
    return titles[:limit]
