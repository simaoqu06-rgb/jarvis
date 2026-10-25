"""Veille des routines Claude (audits de GPT et Gemini, 25 octobre 2026) : une routine qui ne tourne plus doit se voir.

Du 10 au 20 octobre 2026, l'équipe du soir et la recherche avant l'ouverture n'ont pas tourné (leurs fils étaient
marqués résolus) et personne ne l'a vu pendant dix jours. Le serveur note quand chaque routine dépose quelque chose
et prévient sur Discord, une fois, quand le dépôt attendu manque (zéro jeton) :

- équipe du soir (lun-ven, 22 h 52 Paris) : l'avis du council sur le grade (POST /api/lab/grade/avis) de la soirée
  de la veille, vérifié à SOIR_LIMITE_PARIS, du mardi au samedi ;
- recherche avant l'ouverture (lun-ven, 15 h 12 Paris) : ses verdicts (POST /api/lab/recherche) sur le scan du
  jour, vérifiés à RECHERCHE_LIMITE_NY les jours où le scanner a retenu au moins une valeur.

Fonctions pures : l'état (dernière soirée vue, alertes déjà envoyées) vit dans l'état des alertes du serveur.
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
NEW_YORK = ZoneInfo("America/New_York")
SOIR_LIMITE_PARIS = time(9, 0)
RECHERCHE_LIMITE_NY = time(10, 30)
CONSEIL = ("Vérifie que son fil n'est pas marqué résolu (une routine dont le fil est résolu ne tourne plus), "
           "puis rouvre-le.")


def expected_soiree(now: datetime) -> str | None:
    """Soirée (date de Paris) dont l'avis du council doit être arrivé à `now` : celle de la veille, du mardi au
    samedi après SOIR_LIMITE_PARIS ; None avant l'heure ou le dimanche et le lundi."""
    local = now.astimezone(PARIS)
    if local.time() < SOIR_LIMITE_PARIS or local.weekday() not in (1, 2, 3, 4, 5):
        return None
    return (local.date() - timedelta(days=1)).isoformat()


def soir_alert(now: datetime, last_soiree: str | None, sent: str | None) -> tuple[str, str] | None:
    """(clé, message) si l'équipe du soir n'a déposé aucun avis pour la soirée attendue ; None sinon ou si déjà
    envoyé (`sent` : clé de la dernière alerte)."""
    want = expected_soiree(now)
    if want is None or (last_soiree or "") >= want:
        return None
    key = f"soir:{want}"
    if sent == key:
        return None
    return key, (f"⚠️ **Équipe du soir du labo** : aucun avis du council reçu pour la soirée du "
                 f"{date.fromisoformat(want):%d/%m} (dernier : {_fr(last_soiree)}). {CONSEIL}")


def recherche_alert(now: datetime, scan_day: str | None, picks: int, verdicts: int,
                    sent: str | None) -> tuple[str, str] | None:
    """(clé, message) si la recherche avant l'ouverture n'a rendu aucun verdict sur le scan du jour, à partir de
    RECHERCHE_LIMITE_NY ; rien les jours sans scan ou sans valeur retenue (la routine n'a rien à déposer)."""
    local = now.astimezone(NEW_YORK)
    today = local.date().isoformat()
    if local.time() < RECHERCHE_LIMITE_NY or scan_day != today or picks <= 0 or verdicts > 0:
        return None
    key = f"recherche:{today}"
    if sent == key:
        return None
    return key, (f"⚠️ **Recherche avant l'ouverture** : aucun verdict sur les {picks} valeurs du scan d'aujourd'hui. "
                 f"{CONSEIL}")


def _fr(day: str | None) -> str:
    return f"{date.fromisoformat(day):%d/%m}" if day else "jamais vu depuis la mise en service de la veille"
