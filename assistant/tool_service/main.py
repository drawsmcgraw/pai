import re
import httpx
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Kronk Tool Service")

# WMO weather code descriptions
WMO_CODES = {
    0: "clear sky",
    1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "icy fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    61: "light rain", 63: "moderate rain", 65: "heavy rain",
    71: "light snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "moderate showers", 82: "heavy showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "heavy thunderstorm with hail",
}


def clean_location(location: str) -> str:
    """Strip US state abbreviations that confuse the geocoder.
    'Laurel, MD' -> 'Laurel'   'Las Vegas, NV' -> 'Las Vegas'
    Full state names and country names are fine as-is.
    """
    return re.sub(r',\s*[A-Z]{2}\s*$', '', location).strip()


@app.get("/weather")
async def weather(location: str = Query(..., description="City name or city, state/country")):
    query = clean_location(location)
    async with httpx.AsyncClient(timeout=10) as client:
        # Step 1: geocode the location name
        geo_resp = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": query, "count": 1, "language": "en", "format": "json"},
        )
        geo = geo_resp.json()
        if not geo.get("results"):
            raise HTTPException(status_code=404, detail=f"Location not found: {location}")

        place = geo["results"][0]
        lat = place["latitude"]
        lon = place["longitude"]
        display_name = place["name"]
        admin = place.get("admin1", "")
        country = place.get("country", "")
        full_name = ", ".join(filter(None, [display_name, admin, country]))

        # Step 2: fetch current conditions
        wx_resp = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,weathercode,windspeed_10m,relativehumidity_2m",
                "temperature_unit": "fahrenheit",
                "windspeed_unit": "mph",
                "timezone": "auto",
            },
        )
        wx = wx_resp.json()

    current = wx["current"]
    condition = WMO_CODES.get(current["weathercode"], "unknown conditions")

    return {
        "location": full_name,
        "temperature_f": current["temperature_2m"],
        "condition": condition,
        "wind_mph": current["windspeed_10m"],
        "humidity_pct": current["relativehumidity_2m"],
        "summary": (
            f"{current['temperature_2m']}°F, {condition}, "
            f"wind {current['windspeed_10m']} mph, "
            f"humidity {current['relativehumidity_2m']}%"
        ),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
