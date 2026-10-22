"""SEC EDGAR : dépôts 8-K des sociétés (codes d'item, heure d'acceptation), sans clé.

Demande de Simão (22 octobre 2026, fil « Skills de trading pour JARVIS ») : juger le catalyseur avec une info
datée que le prix seul n'a pas. Un 8-K porte un ou plusieurs codes d'item (2.02 résultats, 1.01 contrat important,
5.02 dirigeant, 7.01 et 8.01 communiqués…) et l'heure à laquelle la SEC l'a accepté, donc rendu public : rejouable
sans regard vers l'avenir, contrairement à l'heure des actus d'un agrégateur.

La SEC exige un User-Agent qui nomme la personne et son e-mail (« Prénom Nom e-mail ») : variable SEC_USER_AGENT.
Sans elle, rien n'est appelé. Débit : la SEC tolère 10 requêtes par seconde ; le labo en fait bien moins.

Sources :
- https://www.sec.gov/files/company_tickers.json : ticker → CIK (tickers actuels seulement : une valeur radiée ou
  renommée depuis n'a pas de CIK, et un ticker repris par une autre société pointe vers la nouvelle) ;
- https://data.sec.gov/submissions/CIK##########.json : les dépôts de la société (« recent » : au moins un an ou
  1 000 dépôts) et, au-delà, des pages d'archive (« files ») lues par `page()`.

Heure d'acceptation (acceptanceDateTime, suffixée « Z ») : lue telle quelle, en UTC. Elle est juste pour une partie
des dépôts seulement : pour les autres, le JSON est EN RETARD du décalage de New York (4 h l'été, 5 h l'hiver).
Mesuré le 22 octobre 2026 sur 66 194 8-K du serveur : pics à 16 h New York (heure juste) ET à 20-21 h (les mêmes
dépôts d'après la clôture, en retard) ; 890 8-K lus entre 0 h et 3 h le jour même de leur date de dépôt, alors
qu'EDGAR est fermé de 22 h à 6 h ; pas un 8-K lu entre 3 h et 6 h (aucun dépôt en avance). Environ deux tiers en
retard, sans lien avec le mois (1996-2026), la saison ni l'agent de dépôt : rien dans le JSON ne dit lequel est juste.
Conséquence : lue en UTC, l'heure ne peut que retarder un dépôt, jamais l'avancer (pas d'information future) ;
`shifted_back()` donne l'autre heure possible, celle d'un JSON en retard, et la mesure écarte les séances où cette
autre heure changerait la fenêtre (voir sec8k.py). `suspicious()` repère les retards certains (heure New York après
17 h 30 alors que la date de dépôt est ce même jour) et la mesure les compte.
"""
import unicodedata
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from app.config import secret
from app.core.http import SourceError, get_json

KEY_NAME = "SEC_USER_AGENT"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions"
FORMS = ("8-K", "8-K/A", "6-K")
NEW_YORK = ZoneInfo("America/New_York")
CUTOFF_FILING_DAY = time(17, 30)  # après 17 h 30 New York, un 8-K prend la date du jour ouvré suivant
EDGAR_OPEN, EDGAR_CLOSE = time(6, 0), time(22, 0)  # heures d'acceptation d'EDGAR (New York, jours ouvrés)


def user_agent() -> str:
    """SEC_USER_AGENT en ASCII : un en-tête HTTP refuse « Simão » (httpx lève UnicodeEncodeError), il part en « Simao »."""
    ua = secret(KEY_NAME)
    if not ua:
        raise SourceError(f"{KEY_NAME} absent (nom et e-mail exigés par la SEC)")
    return unicodedata.normalize("NFKD", ua).encode("ascii", "ignore").decode()


async def _get(url: str):
    return await get_json(url, headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"})


def ticker(symbol: str) -> str:
    """Ticker au format de la SEC (BRK.B → BRK-B)."""
    return symbol.strip().upper().replace(".", "-")


def parse_tickers(raw) -> dict[str, int]:
    """{"0": {"cik_str": 320193, "ticker": "AAPL", …}, …} → {"AAPL": 320193}. Premier CIK gardé en cas de doublon."""
    out: dict[str, int] = {}
    for row in (raw.values() if isinstance(raw, dict) else []):
        try:
            t, cik = ticker(str(row["ticker"])), int(row["cik_str"])
        except (KeyError, TypeError, ValueError):
            continue
        out.setdefault(t, cik)
    return out


async def tickers() -> dict[str, int]:
    return parse_tickers(await _get(TICKERS_URL))


def accepted_ts(value: str | None) -> int | None:
    """« 2026-07-28T10:58:13.000Z » → epoch (s). None si illisible."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def suspicious(ts: int | None, filing_date: str | None) -> bool:
    """Heure visiblement décalée : après 17 h 30 New York alors que la date de dépôt est ce même jour."""
    if ts is None or not filing_date:
        return False
    ny = datetime.fromtimestamp(ts, NEW_YORK)
    return ny.time() > CUTOFF_FILING_DAY and ny.date().isoformat() == filing_date


def shifted_back(ts: int | None) -> int | None:
    """Heure d'acceptation si le JSON est en retard du décalage de New York (4 h l'été, 5 h l'hiver) : `ts` moins ce
    décalage. None si cette heure tombe quand EDGAR est fermé (alors `ts` est forcément juste)."""
    if ts is None:
        return None
    alt = ts + int(datetime.fromtimestamp(ts, NEW_YORK).utcoffset().total_seconds())
    return alt if EDGAR_OPEN <= datetime.fromtimestamp(alt, NEW_YORK).time() < EDGAR_CLOSE else None


def items(value) -> list[str]:
    """« 2.02,9.01 » → ["2.02", "9.01"]."""
    if not value:
        return []
    return [i.strip() for i in str(value).split(",") if i.strip()]


def parse_filings(block: dict) -> list[dict]:
    """Colonnes parallèles d'un bloc « recent » ou d'une page d'archive → dépôts 8-K, 8-K/A et 6-K."""
    forms = block.get("form") or []
    out = []
    for i, form in enumerate(forms):
        if form not in FORMS:
            continue

        def col(name, i=i):
            values = block.get(name) or []
            return values[i] if i < len(values) else None

        acc = col("accessionNumber")
        if not acc:
            continue
        ts = accepted_ts(col("acceptanceDateTime"))
        fdate = col("filingDate")
        out.append({"accession": acc, "form": form, "items": items(col("items")), "accepted_ts": ts,
                    "filing_date": fdate, "suspect": suspicious(ts, fdate)})
    return out


def oldest_date(block: dict) -> str | None:
    dates = [d for d in (block.get("filingDate") or []) if d]
    return min(dates) if dates else None


async def submissions(cik: int) -> dict:
    """Bloc complet de la société : {"recent": dépôts, "files": pages d'archive [{name, filingFrom, filingTo}],
    "oldest": date du plus ancien dépôt de « recent »}."""
    raw = await _get(f"{SUBMISSIONS}/CIK{cik:010d}.json")
    filings = (raw or {}).get("filings") or {}
    recent = filings.get("recent") or {}
    return {"recent": parse_filings(recent), "files": filings.get("files") or [], "oldest": oldest_date(recent)}


async def page(name: str) -> list[dict]:
    """Page d'archive (« CIK0000320193-submissions-001.json ») : mêmes colonnes que « recent »."""
    return parse_filings(await _get(f"{SUBMISSIONS}/{name}") or {})


def pages_needed(files: list[dict], since: date) -> list[str]:
    """Pages d'archive qui couvrent des dépôts à partir de `since`."""
    out = []
    for f in files:
        try:
            if date.fromisoformat(f["filingTo"]) >= since:
                out.append(f["name"])
        except (KeyError, TypeError, ValueError):
            continue
    return out
