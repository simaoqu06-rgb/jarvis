"""Serveur JARVIS : expose chaque donnée au même format JSON.

Lancer : uvicorn app.main:app --reload
"""
from fastapi import FastAPI, HTTPException, Query

from app import needs

app = FastAPI(title="JARVIS", version="0.1.0")


def _list(value: str) -> list[str]:
    items = [v for v in value.split(",") if v.strip()]
    if not items:
        raise HTTPException(422, "Liste vide")
    return items


async def _city(city: str) -> tuple[float, float]:
    place = await needs.geocode_city(city)
    if not place.data:
        raise HTTPException(503, place.message)
    return place.data[0]["lat"], place.data[0]["lon"]


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/crypto/prices")
async def crypto_prices(symbols: str = "BTC,ETH"):
    return (await needs.crypto_prices(_list(symbols))).to_dict()


@app.get("/api/crypto/ranking")
async def crypto_ranking(limit: int = Query(20, ge=1, le=100)):
    return (await needs.crypto_ranking(limit)).to_dict()


@app.get("/api/fx")
async def fx(base: str = "EUR", symbols: str = "USD,GBP,CHF,JPY"):
    return (await needs.fx_rates(base, _list(symbols))).to_dict()


@app.get("/api/stocks/quotes")
async def stock_quotes(symbols: str = Query(..., description="ex. AAPL,MSFT (8 au maximum par minute en gratuit)")):
    return (await needs.stock_quotes(_list(symbols))).to_dict()


@app.get("/api/markets/us/status")
async def us_market_status():
    return await needs.us_market_status()


@app.get("/api/markets/us/holidays")
async def us_market_holidays(year: int | None = None):
    return (await needs.us_market_holidays(year)).to_dict()


@app.get("/api/holidays")
async def holidays(country: str = "FR", year: int | None = None):
    return (await needs.holidays(country, year)).to_dict()


@app.get("/api/calendar/economic")
async def economic_calendar():
    return (await needs.economic_calendar()).to_dict()


@app.get("/api/news/market")
async def market_news(limit: int = Query(20, ge=1, le=50)):
    return (await needs.market_news(limit)).to_dict()


@app.get("/api/news/company/{symbol}")
async def company_news(symbol: str, limit: int = Query(10, ge=1, le=50)):
    return (await needs.company_news(symbol, limit)).to_dict()


@app.get("/api/news/tech")
async def tech_news(q: str | None = None, limit: int = Query(20, ge=1, le=50)):
    return (await needs.tech_news(q, limit)).to_dict()


@app.get("/api/news/space")
async def space_news(q: str | None = None, limit: int = Query(20, ge=1, le=50)):
    return (await needs.space_news(q, limit)).to_dict()


@app.get("/api/weather")
async def weather(city: str | None = None, lat: float | None = None, lon: float | None = None):
    if lat is None or lon is None:
        if not city:
            raise HTTPException(422, "Donne une ville (city) ou des coordonnées (lat, lon)")
        lat, lon = await _city(city)
    return (await needs.weather(lat, lon)).to_dict()


@app.get("/api/air-quality")
async def air_quality(city: str | None = None, lat: float | None = None, lon: float | None = None):
    if lat is None or lon is None:
        if not city:
            raise HTTPException(422, "Donne une ville (city) ou des coordonnées (lat, lon)")
        lat, lon = await _city(city)
    return (await needs.air_quality(lat, lon)).to_dict()


@app.get("/api/earthquakes")
async def earthquakes(min_magnitude: float = Query(4.5, ge=0), hours: int = Query(24, ge=1, le=720)):
    return (await needs.earthquakes(min_magnitude, hours)).to_dict()


@app.get("/api/geocode")
async def geocode(city: str):
    return (await needs.geocode_city(city)).to_dict()


@app.get("/api/address/france")
async def address_france(q: str):
    return (await needs.address_france(q)).to_dict()


@app.get("/api/address/world")
async def address_world(q: str):
    return (await needs.address_world(q)).to_dict()


@app.get("/api/wikipedia")
async def wikipedia(topic: str):
    return (await needs.encyclopedia(topic)).to_dict()


@app.get("/api/books")
async def books(q: str):
    return (await needs.books(q)).to_dict()
