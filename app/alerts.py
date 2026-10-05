"""Contenu des notifications Discord : point du matin, mouvements de cours, séismes, actus.

Chaque fonction `check_*` renvoie la liste des messages à envoyer et retient ce qui a
déjà été signalé (fichier data/alerts_state.json) pour ne jamais envoyer deux fois la même alerte.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import config, needs
from app.core.result import Result

log = logging.getLogger(__name__)
PARIS = ZoneInfo("Europe/Paris")
MAX_SEEN = 500
EVENT_LABELS = {"forecast": "prévu", "previous": "précédent", "actual": "publié"}


class State:
    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self.data: dict = {}
        if self.path and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("État des alertes illisible, on repart de zéro")

    def seen(self, kind: str) -> list:
        return self.data.setdefault(kind, [])

    def remember(self, kind: str, items: list) -> None:
        self.data[kind] = (self.seen(kind) + items)[-MAX_SEEN:]

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


def _time_fr(iso: str | None) -> str:
    if not iso:
        return "heure inconnue"
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return moment.astimezone(PARIS).strftime("%d/%m %H:%M")


def _footer(*results: Result) -> str:
    sources = sorted({r.attribution or r.source for r in results if r.source})
    stale = [r for r in results if r.stale and r.data is not None]
    line = f"-# {' · '.join(sources)}" if sources else ""
    if stale:
        line += "\n-# ⚠️ Valeurs non à jour : " + ", ".join(f"{r.need} ({_time_fr(r.updated_at)})" for r in stale)
    return line


def _pct(value: float | None) -> str:
    return "n/d" if value is None else f"{value:+.2f} %"


def _money(value: float | None, unit: str) -> str:
    return "n/d" if value is None else f"{value:,.2f} {unit}".replace(",", " ")


async def morning_brief(now: datetime | None = None) -> str:
    """Point avant l'ouverture de New York : Bourse, agenda économique, cours, change, météo."""
    now = now or datetime.now(timezone.utc)
    lines = [f"## ☀️ Point JARVIS du {now.astimezone(PARIS).strftime('%d/%m/%Y')}"]
    results: list[Result] = []

    status = await needs.us_market_status(now)
    lines.append(f"**Bourse de New York** : {'ouverte' if status['is_open'] else 'fermée'} ({status['reason']}).")

    calendar = await needs.economic_calendar()
    results.append(calendar)
    today = now.date().isoformat()
    events = [e for e in (calendar.data or []) if (e["datetime_utc"] or "").startswith(today)
              and e["impact"] in ("High", "Medium") and e["country"] in ("US", "EU", "FR", "DE")]
    if events:
        lines.append("**Agenda économique du jour** :")
        for e in events[:15]:
            details = ", ".join(f"{label} {e[k]}" for k, label in EVENT_LABELS.items() if e.get(k))
            lines.append(f"- {_time_fr(e['datetime_utc'])} · {e['country']} · {e['title']}"
                         f"{' (impact fort)' if e['impact'] == 'High' else ''}{f' — {details}' if details else ''}")
    else:
        lines.append("**Agenda économique du jour** : rien d'important pour les États-Unis et l'Europe.")

    if config.WATCHLIST_CRYPTO:
        prices = await needs.crypto_prices(config.WATCHLIST_CRYPTO)
        ranking = await needs.crypto_ranking(100)
        results += [prices, ranking]
        change = {c["symbol"]: c["change_24h_pct"] for c in (ranking.data or [])}
        lines.append("**Crypto** : " + " · ".join(
            f"{p['symbol']} {_money(p['price'], '€')} ({_pct(p['change_24h_pct'] if p['change_24h_pct'] is not None else change.get(p['symbol']))} sur 24 h)"
            for p in (prices.data or [])) if prices.data else "**Crypto** : indisponible.")

    if config.WATCHLIST_STOCKS:
        quotes = await needs.stock_quotes(config.WATCHLIST_STOCKS)
        results.append(quotes)
        lines.append("**Actions suivies** (dernière clôture) : " + " · ".join(
            f"{q['symbol']} {_money(q['price'], '$')} ({_pct(q['change_pct'])})" for q in (quotes.data or []))
            if quotes.data else f"**Actions suivies** : indisponibles ({quotes.message}).")

    fx = await needs.fx_rates("EUR", ["USD", "GBP", "CHF"])
    results.append(fx)
    if fx.data:
        lines.append("**Change** (BCE) : " + " · ".join(f"1 € = {r:.4f} {c}" for c, r in fx.data["rates"].items() if r))

    if config.HOME_CITY:
        place = await needs.geocode_city(config.HOME_CITY)
        if place.data:
            lat, lon = place.data[0]["lat"], place.data[0]["lon"]
            weather, air = await needs.weather(lat, lon), await needs.air_quality(lat, lon)
            results += [weather, air]
            if weather.data:
                w = weather.data
                lines.append(f"**Météo à {config.HOME_CITY}** : {w['condition']}, {w['temperature_c']} °C, "
                             f"vent {w['wind_speed_kmh']} km/h")
            if air.data:
                lines.append(f"**Air** : indice européen {air.data['aqi']:.0f} (PM2.5 {air.data['pm2_5']} µg/m³)")

    lines.append(_footer(*results))
    return "\n".join(line for line in lines if line)


async def check_price_moves(state: State, now: datetime | None = None) -> list[str]:
    """Alerte quand une valeur suivie dépasse ±X % sur 24 h (une fois par palier et par jour)."""
    now = now or datetime.now(timezone.utc)
    step = config.MOVE_ALERT_PCT
    day = now.astimezone(PARIS).date().isoformat()
    sent = state.data.setdefault("moves", {})
    sent = state.data["moves"] = {k: v for k, v in sent.items() if k.startswith(day)}
    moves: list[tuple[str, str, float | None, float, Result]] = []

    if config.WATCHLIST_CRYPTO:
        ranking = await needs.crypto_ranking(100)
        for c in ranking.data or []:
            if c["symbol"] in config.WATCHLIST_CRYPTO and c["change_24h_pct"] is not None:
                moves.append((c["symbol"], "$", c["price_usd"], c["change_24h_pct"], ranking))
    if config.WATCHLIST_STOCKS:
        status = await needs.us_market_status(now)
        if status["is_open"]:
            quotes = await needs.stock_quotes(config.WATCHLIST_STOCKS)
            for q in quotes.data or []:
                if q["change_pct"] is not None:
                    moves.append((q["symbol"], "$", q["price"], q["change_pct"], quotes))

    messages = []
    for symbol, unit, price, change, result in moves:
        level = int(abs(change) // step)
        key = f"{day}:{symbol}:{'+' if change > 0 else '-'}"
        if level >= 1 and level > sent.get(key, 0):
            sent[key] = level
            arrow = "📈" if change > 0 else "📉"
            messages.append(f"{arrow} **{symbol}** {_pct(change)} sur la journée, à {_money(price, unit)}\n{_footer(result)}")
    return messages


async def check_earthquakes(state: State) -> list[str]:
    """Nouveaux séismes forts (magnitude ≥ JARVIS_QUAKE_MIN_MAGNITUDE)."""
    result = await needs.earthquakes(config.QUAKE_MIN_MAGNITUDE, 24)
    if result.data is None:
        return []
    first_run = "quakes" not in state.data
    seen = set(state.seen("quakes"))
    new = [q for q in result.data if q["id"] not in seen]
    state.remember("quakes", [q["id"] for q in new])
    if first_run:
        return []  # au premier lancement, on mémorise sans renvoyer l'historique
    return [
        f"🌍 **Séisme M{q['magnitude']}** — {q['place']} ({_time_fr(q['time'])}, profondeur {q['depth_km']:.0f} km)"
        f"{' · ⚠️ alerte tsunami' if q['tsunami'] else ''}\n{q['url'] or ''}\n{_footer(result)}"
        for q in reversed(new)
    ]


async def check_news(state: State) -> list[str]:
    """Nouvelles actus : marché global, et actus des actions suivies si la clé Finnhub est là."""
    batches = [("Marché", await needs.market_news(20))]
    for symbol in config.WATCHLIST_STOCKS:
        batches.append((symbol, await needs.company_news(symbol, 5)))
    first_run = "news" not in state.data
    seen = set(state.seen("news"))
    messages, new_keys = [], []
    for label, result in batches:
        for item in result.data or []:
            # Le titre fait partie de la clé : certains flux réutilisent le même lien.
            key = f"{item['title']}|{item['url']}"
            if not item["title"] or key in seen or key in new_keys:
                continue
            new_keys.append(key)
            messages.append(f"📰 [{label}] **{item['title']}**\n<{item['url']}>\n{_footer(result)}")
    state.remember("news", new_keys)
    return [] if first_run else messages[:10]
