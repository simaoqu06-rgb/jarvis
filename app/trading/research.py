"""Recherche avant l'ouverture (lot D, 8 octobre 2026) : une routine Claude planifiée (abonnement de Simão,
pas de clé API sur le serveur) lit le scanner du jour, cherche pourquoi chaque valeur bouge (vraie actu ?
dilution ? résultats ?) et rend un verdict par valeur : « soutient », « contre » ou « neutre ».

Le serveur ne fait que trois choses, sans tokens :
- montrer le dernier passage du scanner (data/scans.jsonl) par le chemin public du labo (/api/lab/scan),
  le seul que le cloud peut joindre ;
- garder les verdicts (data/research.json), un par valeur et par jour, rattachés aux valeurs du scanner ;
- les comparer au forward-test silencieux (paper.py : chaque valeur du scanner y est passée, GO comme
  NO GO) : espérance en R des valeurs « soutient » contre « contre ». C'est la seule façon de savoir si la
  recherche aide ou si elle ne fait que raconter une histoire. Rien ici ne filtre ni ne note un plan.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.trading import scanner

RESEARCH_PATH = os.getenv("JARVIS_RESEARCH_PATH", "data/research.json")
NEW_YORK = ZoneInfo("America/New_York")
VERDICTS = ("soutient", "neutre", "contre")
RAISON_MAX = 1000  # 5 valeurs au plus par scan : le tout tient sous les 16 Ko d'un dépôt au labo
RESUME_MAX = 300
SOURCES_MAX = 4
MIN_N = 15  # comme le forward-test : en dessous, un groupe ne dit encore rien


class ResearchError(ValueError):
    pass


def _ny_day(iso: str | None) -> str | None:
    try:
        return datetime.fromisoformat(iso).astimezone(NEW_YORK).date().isoformat()
    except (TypeError, ValueError):
        return None


def last_scan(path: str | None = None) -> dict | None:
    """Dernier passage enregistré du scanner (avec son jour à New York), ou None."""
    p = Path(path or scanner.SCANS_PATH)
    try:
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        try:
            scan = json.loads(line)
        except ValueError:
            continue
        return scan | {"jour": _ny_day(scan.get("at"))}
    return None


def scan_view(scan: dict | None, now: datetime | None = None) -> dict:
    """Ce que lit la routine : le scan du jour s'il existe, sinon le dernier avec « du_jour » à faux."""
    today = (now or datetime.now(timezone.utc)).astimezone(NEW_YORK).date().isoformat()
    if not scan:
        return {"du_jour": False, "jour": today, "scan": None}
    return {"du_jour": scan["jour"] == today, "jour": today, "scan": scan}


class ResearchBook:
    def __init__(self, path: str | None = RESEARCH_PATH):
        self.path = Path(path) if path else None
        self.items: list[dict] = []
        if self.path and self.path.exists():
            try:
                self.items = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.items = []

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def day(self, jour: str) -> list[dict]:
        return [i for i in self.items if i["jour"] == jour]


def _clean(item, picks: set[str]) -> dict:
    if not isinstance(item, dict):
        raise ResearchError("Chaque élément : {\"symbol\": …, \"verdict\": …, \"raison\": …}")
    symbol = str(item.get("symbol") or "").upper().strip()
    if symbol not in picks:
        raise ResearchError(f"{symbol or '?'} n'est pas une valeur retenue par le scanner du jour "
                            f"({', '.join(sorted(picks)) or 'aucune'})")
    verdict = item.get("verdict")
    if verdict not in VERDICTS:
        raise ResearchError(f"verdict de {symbol} : {verdict!r} (permis : {', '.join(VERDICTS)})")
    raison = item.get("raison")
    if not isinstance(raison, str) or not 10 <= len(raison.strip()) <= RAISON_MAX:
        raise ResearchError(f"raison de {symbol} : texte de 10 à {RAISON_MAX} caractères")
    resume = item.get("resume") or ""
    if not isinstance(resume, str) or len(resume) > RESUME_MAX:
        raise ResearchError(f"resume de {symbol} : texte de {RESUME_MAX} caractères au plus")
    sources = item.get("sources") or []
    if (not isinstance(sources, list) or len(sources) > SOURCES_MAX
            or not all(isinstance(s, str) and s.startswith(("http://", "https://")) and len(s) <= 300
                       for s in sources)):
        raise ResearchError(f"sources de {symbol} : {SOURCES_MAX} liens http(s) au plus")
    return {"symbol": symbol, "verdict": verdict, "raison": raison.strip(), "resume": resume.strip(),
            "sources": sources}


def record(book: ResearchBook, items, scan: dict | None, now: datetime | None = None) -> dict:
    """Enregistre les verdicts du jour ; un nouveau verdict sur la même valeur remplace l'ancien."""
    now = now or datetime.now(timezone.utc)
    view = scan_view(scan, now)
    if not view["du_jour"]:
        raise ResearchError("Pas de scan aujourd'hui : rien à juger (séance fermée ou scanner pas encore passé)")
    if not isinstance(items, list) or not items:
        raise ResearchError("Liste de verdicts attendue")
    picks = {p["symbol"] for p in view["scan"].get("picks") or []}
    clean = [_clean(i, picks) for i in items]
    jour, at = view["jour"], now.isoformat(timespec="seconds")
    done = {c["symbol"] for c in clean}
    book.items = [i for i in book.items if not (i["jour"] == jour and i["symbol"] in done)]
    book.items += [c | {"jour": jour, "le": at} for c in clean]
    book.save()
    return {"enregistres": len(clean), "jour": jour}


def _group(rs: list[float]) -> dict:
    return {"n": len(rs), "esperance_r": round(sum(rs) / len(rs), 3) if rs else None,
            "reussite": round(sum(r > 0 for r in rs) / len(rs), 2) if rs else None,
            "echantillon_faible": len(rs) < MIN_N}


def bilan(book: ResearchBook, paper_entries: list[dict]) -> dict:
    """Verdicts de la recherche face au résultat du forward-test des mêmes valeurs, le même jour."""
    outcome: dict[tuple[str, str], dict] = {}
    for e in paper_entries:
        if e.get("origin") == "scanner":
            outcome[(_ny_day(e.get("created_at")), e["symbol"])] = e
    groups: dict[str, list[float]] = {v: [] for v in VERDICTS}
    attente = non_declenche = sans_test = 0
    for item in book.items:
        e = outcome.get((item["jour"], item["symbol"]))
        if e is None:
            sans_test += 1
        elif e.get("status") != "done":
            attente += 1
        elif e.get("result_r") is None:
            non_declenche += 1
        else:
            groups[item["verdict"]].append(e["result_r"])
    par = {v: _group(rs) for v, rs in groups.items()}
    s, c = par["soutient"], par["contre"]
    ecart = (round(s["esperance_r"] - c["esperance_r"], 3)
             if s["esperance_r"] is not None and c["esperance_r"] is not None else None)
    return {"verdicts": len(book.items), "jours": len({i["jour"] for i in book.items}), "par_verdict": par,
            "ecart_soutient_contre_r": ecart, "en_attente": attente, "non_declenches": non_declenche,
            "sans_forward_test": sans_test, "min_n": MIN_N}


def _fr(x: float) -> str:
    return f"{x:+.2f}".replace(".", ",")


def bilan_text(b: dict) -> str:
    lines = [f"🔎 **Recherche avant l'ouverture** : {b['verdicts']} verdicts sur {b['jours']} jours"]
    for v, g in b["par_verdict"].items():
        if g["n"]:
            lines.append(f"{v} : {g['n']} trades, {_fr(g['esperance_r'])} R, {round(100 * g['reussite'])} % gagnants"
                         + (" · échantillon faible" if g["echantillon_faible"] else ""))
    if b["ecart_soutient_contre_r"] is not None:
        lines.append(f"Écart « soutient » − « contre » : {_fr(b['ecart_soutient_contre_r'])} R")
    lines.append(f"En attente du forward-test : {b['en_attente']} · non déclenchés : {b['non_declenches']}")
    return "\n".join(lines)
