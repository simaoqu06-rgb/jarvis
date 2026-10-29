"""Version de JARVIS qui tourne, comparée à `main` sur GitHub (rubrique « À faire » du cockpit, 29/10/2026).

Le dépôt du serveur (/root/jarvis) est monté en lecture seule dans le conteneur, sous /repo (`docker-compose.yml`) :
- **code qui tourne** : l'empreinte de `app/` et de `requirements.txt` de l'image, calculée au démarrage ;
- **code sur le disque** : la même empreinte sur /repo, et le commit de `HEAD`. Les deux empreintes égales : JARVIS
  tourne bien sur le commit du disque, même après un redémarrage. Différentes : un `git pull` a été fait sans
  reconstruire (`docker compose up -d --build`). Comparer le contenu plutôt que le `HEAD` lu au démarrage évite le
  faux « à jour » d'un conteneur relancé (`restart`, `--force-recreate`) sur une image ancienne.
Ensuite, l'API publique de GitHub (dépôt public, sans jeton ; `JARVIS_GITHUB_TOKEN` en lecture seule s'il redevient
privé) compare ce commit à `main` : les PR fusionnées depuis, qui touchent au code de JARVIS, sont « pas encore
déployées ». Une requête toutes les 30 min, ou dès que le commit du disque change.
Le fichier `.git/config` n'est jamais lu (une adresse distante peut porter un jeton) : seulement `HEAD`, `refs/` et
`packed-refs`. Rien n'est envoyé à GitHub, hormis la lecture de la comparaison.
"""
import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import TIMEOUT_SECONDS, USER_AGENT, secret

log = logging.getLogger(__name__)
REPO = os.getenv("JARVIS_GITHUB_REPO", "simaoqu06-rgb/jarvis")
BRANCH = "main"
TOKEN_NAME = "JARVIS_GITHUB_TOKEN"
CHECK_EVERY_S = 1800
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REF_RE = re.compile(r"^refs/[A-Za-z0-9._/\-]{1,200}$")
MERGE_RE = re.compile(r"^Merge pull request #(\d+)\b")
SQUASH_RE = re.compile(r"\(#(\d+)\)\s*$")
CODE_PATHS = ("app", "requirements.txt")  # ce que l'image copie (Dockerfile)
RUNTIME_FILES = {"requirements.txt", "Dockerfile", "docker-compose.yml"}  # hors app/ : demandent une reconstruction
IMAGE_DIR = Path(__file__).resolve().parents[2]  # /app dans l'image


def repo_dir() -> Path:
    """Le dépôt du serveur : /repo dans le conteneur (monté en lecture seule), le dossier courant ailleurs."""
    if os.getenv("JARVIS_REPO_DIR"):
        return Path(os.environ["JARVIS_REPO_DIR"])
    return Path("/repo") if Path("/repo/.git").exists() else Path(".")


def read_head(folder: Path | None = None) -> dict:
    """{"sha", "branche"} du `HEAD` d'un dossier .git, sans lancer git ; avec "erreur" si illisible ou absent."""
    folder = folder or repo_dir() / ".git"
    try:
        head = (folder / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return {"sha": None, "branche": None, "erreur": "dépôt du serveur non monté dans le conteneur"}
    if SHA_RE.match(head):
        return {"sha": head, "branche": None}
    if not head.startswith("ref: "):
        return {"sha": None, "branche": None, "erreur": "HEAD illisible"}
    ref = head[5:].strip()
    if not REF_RE.match(ref) or ".." in ref:
        return {"sha": None, "branche": None, "erreur": "HEAD illisible"}
    branch = ref.removeprefix("refs/heads/")
    try:
        sha = (folder / ref).read_text(encoding="utf-8").strip()
        if SHA_RE.match(sha):
            return {"sha": sha, "branche": branch}
    except OSError:
        pass
    try:  # référence rangée par `git gc` dans packed-refs
        for line in (folder / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref and SHA_RE.match(parts[0]):
                return {"sha": parts[0], "branche": branch}
    except OSError:
        pass
    return {"sha": None, "branche": branch, "erreur": f"référence {ref} introuvable"}


def _files(root: Path, rel: str) -> list[Path]:
    p = root / rel
    if p.is_file():
        return [p]
    if not p.is_dir():
        return []
    found = [f for f in p.rglob("*") if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc"]
    return sorted(found, key=lambda f: f.relative_to(root).as_posix())


def code_hash(root: Path) -> str | None:
    """Empreinte du contenu de `app/` et de `requirements.txt` (sans les __pycache__) ; None si rien n'est lisible."""
    h, found = hashlib.sha256(), False
    for rel in CODE_PATHS:
        for f in _files(root, rel):
            try:
                data = f.read_bytes()
            except OSError:
                return None
            h.update(f.relative_to(root).as_posix().encode() + b"\0" + hashlib.sha256(data).digest())
            found = True
    return h.hexdigest()[:16] if found else None


IMAGE_CODE = code_hash(IMAGE_DIR)  # au démarrage : le code de l'image qui tourne
state: dict = {"verifie_le": None, "base": None, "main": None, "en_retard": [], "commits_en_retard": None,
               "statut": None, "code_touche": None, "erreur": None}


def disk() -> dict:
    """Commit et empreinte du dépôt du serveur ; `code` : « identique » (JARVIS tourne sur ce commit), « different »
    (pull sans reconstruction) ou None (dépôt non monté)."""
    head = read_head()
    if not head.get("sha"):
        return head | {"empreinte": None, "code": None}
    mine = code_hash(repo_dir())
    return head | {"empreinte": mine, "code": None if mine is None or IMAGE_CODE is None else
                   "identique" if mine == IMAGE_CODE else "different"}


def merged_prs(commits: list[dict]) -> list[dict]:
    """PR fusionnées parmi des commits de l'API GitHub : « Merge pull request #N … » (titre sur la 3e ligne) ou
    « Titre (#N) » (fusion écrasée). Dans l'ordre des commits, sans doublon."""
    out, seen = [], set()
    for c in commits:
        message = ((c.get("commit") or {}).get("message") or "").strip()
        lines = message.splitlines()
        first = lines[0] if lines else ""
        m = MERGE_RE.match(first)
        if m:
            title = next((ln.strip() for ln in lines[1:] if ln.strip()), "")
        else:
            m = SQUASH_RE.search(first)
            title = SQUASH_RE.sub("", first).strip()
        if not m or int(m.group(1)) in seen:
            continue
        seen.add(int(m.group(1)))
        out.append({"numero": int(m.group(1)), "titre": title[:140], "sha": (c.get("sha") or "")[:7]})
    return out


def touches_code(files: list[dict] | None) -> bool:
    """Un des fichiers changés compte pour JARVIS (app/, dépendances, image). Liste absente ou tronquée : oui."""
    if not files or len(files) >= 300:  # GitHub coupe la liste à 300 fichiers
        return True
    return any((f.get("filename") or "").startswith("app/") or f.get("filename") in RUNTIME_FILES for f in files)


def due(now: datetime) -> bool:
    """Vérification à faire : jamais faite, commit du disque changé depuis, ou plus de 30 min."""
    base = read_head().get("sha")
    last = state["verifie_le"]
    return bool(base) and (last is None or state["base"] != base
                           or (now - datetime.fromisoformat(last)).total_seconds() >= CHECK_EVERY_S)


async def check(transport: httpx.AsyncBaseTransport | None = None, now: datetime | None = None) -> dict:
    """Compare le commit du disque à `main` (une requête). L'état garde la dernière réponse, ou l'erreur en clair."""
    now = now or datetime.now(timezone.utc)
    base = read_head().get("sha")
    state.update(verifie_le=now.isoformat(timespec="seconds"), base=base)
    if not base:
        state.update(erreur="dépôt du serveur non monté dans le conteneur", statut=None, en_retard=[],
                     commits_en_retard=None, main=None, code_touche=None)
        return state
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT,
               "X-GitHub-Api-Version": "2022-11-28"}
    token = secret(TOKEN_NAME)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{REPO}/compare/{base}...{BRANCH}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, headers=headers, transport=transport) as client:
            r = await client.get(url, params={"per_page": 100})
    except httpx.HTTPError as exc:
        state.update(erreur=f"GitHub injoignable ({type(exc).__name__})")
        return state
    if r.status_code != 200:
        why = {404: "dépôt privé ou commit inconnu de GitHub (ajoute JARVIS_GITHUB_TOKEN en lecture seule s'il est "
                    "privé)", 403: "limite de l'API GitHub atteinte", 429: "limite de l'API GitHub atteinte"}
        state.update(erreur=f"HTTP {r.status_code} : {why.get(r.status_code, 'réponse inattendue de GitHub')}")
        return state
    try:
        data = r.json()
        commits = data.get("commits") or []
    except (ValueError, AttributeError):
        state.update(erreur="réponse de GitHub illisible")
        return state
    state.update(erreur=None, statut=data.get("status"), commits_en_retard=data.get("ahead_by"),
                 main=(commits[-1].get("sha") if commits else base), en_retard=merged_prs(commits),
                 code_touche=touches_code(data.get("files")))
    return state


async def tick(now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if due(now):
        await check(now=now)


def snapshot() -> dict:
    """Pour la santé, le diagnostic et la page À faire : commits et empreintes, jamais le jeton."""
    d = disk()
    return {"sur_le_disque": d.get("sha"), "branche": d.get("branche"), "erreur_disque": d.get("erreur"),
            "code": d["code"], "empreinte_image": IMAGE_CODE, "empreinte_disque": d["empreinte"], "depot": REPO,
            "base_verifiee": state["base"], "verifie_le": state["verifie_le"], "statut": state["statut"],
            "commits_en_retard": state["commits_en_retard"], "pr_en_retard": list(state["en_retard"]),
            "code_touche": state["code_touche"], "erreur": state["erreur"]}
