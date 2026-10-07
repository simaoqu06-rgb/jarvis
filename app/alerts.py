"""Contenu des notifications Discord : point du matin, mouvements de cours, séismes, actus.

Chaque fonction `check_*` renvoie la liste des messages à envoyer et retient ce qui a
déjà été signalé (fichier data/alerts_state.json) pour ne jamais envoyer deux fois la même alerte.
"""
import difflib
import json
import logging
import math
import os
import re
import shutil
import unicodedata
from datetime import datetime, timedelta, timezone
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


def _nd(value) -> str:
    return "n/d" if value is None else str(value)


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
                lines.append(f"**Météo à {config.HOME_CITY}** : {_nd(w.get('condition'))}, "
                             f"{_nd(w.get('temperature_c'))} °C, vent {_nd(w.get('wind_speed_kmh'))} km/h")
            if air.data:
                aqi = air.data.get("aqi")
                lines.append(f"**Air** : indice européen {'n/d' if aqi is None else f'{aqi:.0f}'} "
                             f"(PM2.5 {_nd(air.data.get('pm2_5'))} µg/m³)")

    lines.append(_footer(*results))
    return "\n".join(line for line in lines if line)


MOVE_WINDOW = timedelta(hours=24)


async def check_price_moves(state: State, now: datetime | None = None) -> list[str]:
    """Alerte quand une valeur suivie dépasse ±X % (une fois par palier et par sens sur 24 h glissantes).

    La variation crypto est calculée sur 24 h glissantes : la mémoire suit donc une fenêtre de 24 h
    (et non le jour de Paris, sinon la même baisse était réannoncée chaque nuit à minuit).
    Dans la fenêtre, on ne réalerte que si la variation franchit un palier de plus.
    """
    now = now or datetime.now(timezone.utc)
    step = config.MOVE_ALERT_PCT if config.MOVE_ALERT_PCT > 0 else 5.0  # 0 : division par zéro
    sent = state.data.get("moves")
    sent = state.data["moves"] = {
        k: v for k, v in (sent if isinstance(sent, dict) else {}).items()
        if isinstance(v, dict) and now - datetime.fromisoformat(v["at"]) < MOVE_WINDOW
    }
    # (symbole, unité, prix, variation, résultat, période, clé de séance)
    moves: list[tuple[str, str, float | None, float, Result, str, str]] = []

    if config.WATCHLIST_CRYPTO:
        ranking = await needs.crypto_ranking(100)
        for c in ranking.data or []:
            if c["symbol"] in config.WATCHLIST_CRYPTO and c["change_24h_pct"] is not None:
                moves.append((c["symbol"], "$", c["price_usd"], c["change_24h_pct"], ranking, "sur 24 h", ""))
    if config.WATCHLIST_STOCKS:
        status = await needs.us_market_status(now)
        if status["is_open"]:
            quotes = await needs.stock_quotes(config.WATCHLIST_STOCKS)
            # Variation des actions = depuis la clôture de la veille : une mémoire par séance.
            session = now.astimezone(needs.NEW_YORK).date().isoformat()
            for q in quotes.data or []:
                if q.get("change_pct") is not None:
                    moves.append((q["symbol"], "$", q["price"], q["change_pct"], quotes, "sur la séance", f":{session}"))

    messages = []
    for symbol, unit, price, change, result, period, session in moves:
        try:  # une valeur mal formée ne bloque pas les autres
            level = int(abs(change) // step)
            key = f"{symbol}:{'+' if change > 0 else '-'}{session}"
            if level >= 1 and level > sent.get(key, {}).get("level", 0):
                arrow = "📈" if change > 0 else "📉"
                messages.append(f"{arrow} **{symbol}** {_pct(change)} {period}, à {_money(price, unit)}\n{_footer(result)}")
                sent[key] = {"level": level, "at": now.isoformat(timespec="seconds")}
        except Exception:
            log.exception("Mouvement de %s : alerte impossible à construire", symbol)
    return messages


QUAKE_SAME_MINUTES = 10    # même séisme vu par deux agences : heures d'origine à quelques minutes près,
QUAKE_SAME_MAGNITUDE = 0.5  # magnitudes un peu différentes (méthodes de calcul propres à chaque agence)
QUAKE_SAME_KM = 100        # et épicentres à moins de 100 km


def _quake_fingerprint(q: dict) -> dict | None:
    """Empreinte indépendante de la source (USGS ou EMSC n'ont pas les mêmes identifiants)."""
    try:
        moment = datetime.fromisoformat(str(q["time"]).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return {"t": moment.timestamp(), "m": float(q["magnitude"]),
                "lat": float(q["lat"]) if q.get("lat") is not None else None,
                "lon": float(q["lon"]) if q.get("lon") is not None else None}
    except (KeyError, TypeError, ValueError):
        return None


def _km(a: dict, b: dict) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _same_quake(a: dict, b: dict) -> bool:
    if abs(a["t"] - b["t"]) > QUAKE_SAME_MINUTES * 60 or abs(a["m"] - b["m"]) > QUAKE_SAME_MAGNITUDE + 1e-9:
        return False
    if None in (a["lat"], a["lon"], b["lat"], b["lon"]):
        return True  # sans coordonnées, l'heure et la magnitude suffisent
    return _km(a, b) <= QUAKE_SAME_KM


def _quake_message(q: dict, result: Result) -> str:
    try:
        magnitude = f"M{float(q['magnitude']):.1f}"
    except (KeyError, TypeError, ValueError):
        magnitude = "de magnitude inconnue"
    try:
        depth = f"profondeur {float(q['depth_km']):.0f} km"
    except (KeyError, TypeError, ValueError):
        depth = "profondeur n/d"
    try:
        when = _time_fr(q.get("time"))
    except (TypeError, ValueError):
        when = "heure inconnue"
    return (f"🌍 **Séisme {magnitude}** — {q.get('place') or 'lieu inconnu'} ({when}, {depth})"
            f"{' · ⚠️ alerte tsunami' if q.get('tsunami') else ''}\n{q.get('url') or ''}\n{_footer(result)}")


async def check_earthquakes(state: State) -> list[str]:
    """Nouveaux séismes forts (magnitude ≥ JARVIS_QUAKE_MIN_MAGNITUDE).

    Un séisme déjà annoncé n'est pas réannoncé quand on passe de l'USGS à l'EMSC (ou l'inverse) :
    en plus des identifiants, on compare heure, magnitude et position.
    """
    result = await needs.earthquakes(config.QUAKE_MIN_MAGNITUDE, 24)
    if result.data is None:
        return []
    first_run = "quakes" not in state.data
    seen = set(state.seen("quakes"))
    known = [f for f in state.data.get("quake_events", []) if isinstance(f, dict)]
    messages, new_ids, new_prints = [], [], []
    for q in reversed(result.data):  # du plus ancien au plus récent
        if q.get("id") in seen or q.get("id") in new_ids:
            continue
        fingerprint = _quake_fingerprint(q)
        duplicate = fingerprint is not None and any(_same_quake(fingerprint, k) for k in known + new_prints)
        if not first_run and not duplicate:
            try:
                messages.append(_quake_message(q, result))
            except Exception:  # message impossible à écrire : on réessaiera au prochain passage
                log.exception("Séisme %s : message impossible à construire", q.get("id"))
                continue
        # On ne retient le séisme qu'une fois son message construit.
        new_ids.append(q.get("id"))
        if fingerprint is not None:
            new_prints.append(fingerprint)
    state.remember("quakes", new_ids)
    state.data["quake_events"] = (known + new_prints)[-MAX_SEEN:]
    return messages  # au premier lancement, on mémorise sans renvoyer l'historique


def _norm_title(title: str) -> str:
    text = unicodedata.normalize("NFKD", title.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _similar_title(a: str, b: str) -> bool:
    """Même titre aux détails près (ponctuation, casse, accents, mot ajouté par une autre source)."""
    if a == b:
        return True
    if min(len(a), len(b)) < 25:  # titres courts : seule l'égalité compte
        return False
    matcher = difflib.SequenceMatcher(None, a, b)
    return matcher.real_quick_ratio() >= 0.9 and matcher.quick_ratio() >= 0.9 and matcher.ratio() >= 0.9


async def check_news(state: State) -> list[str]:
    """Nouvelles actus : marché global, et actus des actions suivies si la clé Finnhub est là.

    Les doublons sont repérés par titre+lien, mais aussi par titre normalisé : quand la source
    principale tombe et que le secours prend le relais, liens et titres diffèrent un peu.
    """
    batches = [("Marché", await needs.market_news(20))]
    for symbol in config.WATCHLIST_STOCKS:
        try:  # une action en échec ne fait pas tomber les actus des autres
            batches.append((symbol, await needs.company_news(symbol, 5)))
        except Exception:
            log.exception("Actus de %s indisponibles", symbol)
    first_run = "news" not in state.data
    if first_run and all(result.data is None for _, result in batches):
        return []  # toutes les sources en panne : on garde le premier lancement pour plus tard
    seen = set(state.seen("news"))
    seen_titles = list(state.data.get("news_titles", []))
    messages, new_keys, new_titles = [], [], []
    for label, result in batches:
        for item in result.data or []:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            # Le titre fait partie de la clé : certains flux réutilisent le même lien.
            key = f"{item['title']}|{item.get('url')}"
            title = _norm_title(item["title"])
            if key in seen or key in new_keys or any(_similar_title(title, t) for t in seen_titles + new_titles):
                continue
            new_keys.append(key)
            new_titles.append(title)
            messages.append(f"📰 [{label}] **{item['title']}**\n<{item.get('url') or ''}>\n{_footer(result)}")
    state.remember("news", new_keys)
    state.data["news_titles"] = (seen_titles + new_titles)[-MAX_SEEN:]
    return [] if first_run else messages[:10]


DISK_CRITICAL_PCT = 90.0


def _size(n: float) -> str:
    """Taille lisible à la française : 52 Ko, 12 Mo, 1,8 Go."""
    for unit in ("o", "Ko", "Mo"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} Go".replace(".", ",")


def _biggest_files(folder: Path, n: int = 4) -> list[tuple[str, int]]:
    try:
        files = [(p.name, p.stat().st_size) for p in folder.iterdir() if p.is_file()]
    except OSError:
        return []
    return sorted(files, key=lambda f: f[1], reverse=True)[:n]


def check_disk(state: State, now: datetime, folder: str | None = None, usage=shutil.disk_usage) -> list[str]:
    """Disque du serveur presque plein : une alerte par jour (une de plus si ça passe en critique).

    Le dossier data/ est monté depuis le serveur : son disque est celui de la machine entière
    (labo, images Docker, journaux). Rien n'est effacé ici, JARVIS prévient seulement.
    """
    folder = folder or config.DATA_DIR
    try:
        total, used, _free = usage(folder)
    except OSError:
        log.warning("Occupation du disque illisible pour %s", folder)
        return []
    pct = used / total * 100 if total else 0.0
    level = 2 if pct >= DISK_CRITICAL_PCT else 1 if pct >= config.DISK_ALERT_PCT else 0
    day = now.astimezone(PARIS).date().isoformat()
    last = state.data.get("disk_alert") or {}
    if not level or (last.get("day") == day and last.get("level", 0) >= level):
        return []
    state.data["disk_alert"] = {"day": day, "level": level}
    head = "🚨 **Disque du serveur critique**" if level == 2 else "💾 **Disque du serveur bien rempli**"
    lines = [f"{head} : {pct:.0f} % occupé ({_size(used)} sur {_size(total)})."]
    files = ", ".join(f"{name} {_size(size)}" for name, size in _biggest_files(Path(folder)))
    if files:
        lines.append(f"Plus gros fichiers de data/ : {files}.")
    lines.append("Ménage sans risque : `docker image prune -a -f && docker builder prune -f`. "
                 "Les données du labo ne s'effacent pas sans ta décision.")
    return ["\n".join(lines)]
