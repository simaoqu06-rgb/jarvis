"""Labo 3 : message du matin (fenêtre 8 h - 9 h, une fois par jour, rodage), propositions du labo,
annonces du worker et commandes Discord (hors ligne)."""
import asyncio
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import scheduler
from app.lab import method as M
from app.lab import morning as MO
from app.lab import validator as V
from app.lab.db import LabDB, open_readonly
from app.notify import bot
from app.trading import proposals as PR
from app.trading.settings import Settings
from tests.lab_fixtures import insert_validated, synthetic_trades

PARIS = ZoneInfo("Europe/Paris")
SIGNAL = {"field": "context.signal", "op": ">", "value": 0.8}


def paris(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=PARIS).astimezone(timezone.utc)


AT_0805 = paris(2026, 10, 7, 8, 5)


class FakeUser:
    def __init__(self, fail: int = 0):
        self.sent, self.fail = [], fail

    async def send(self, text, view=None):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("Discord en panne")
        self.sent.append((text, view))


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "lab.db"
    monkeypatch.setenv("JARVIS_LAB_DB", str(path))
    monkeypatch.setenv("JARVIS_LAB_DM", "on")
    db = LabDB(str(path))
    synthetic_trades(db, n_days=3, per_day=2)
    props = PR.Proposals(str(tmp_path / "p.json"), Settings(None))
    monkeypatch.setattr(PR, "proposals", props)
    monkeypatch.setattr(scheduler, "proposals", props)
    st = MO.State(str(tmp_path / "matin.json"))
    monkeypatch.setattr(MO, "state", st)
    conn = lambda: open_readonly(str(path))  # noqa: E731
    return db, props, st, conn


# --- Fenêtre 8 h - 9 h, heure d'été et d'hiver ----------------------------------------------

@pytest.mark.parametrize("utc, inside", [
    (datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc), True),     # 8 h 00 heure d'été
    (datetime(2026, 10, 7, 6, 59, 59, tzinfo=timezone.utc), True),
    (datetime(2026, 10, 7, 7, 0, tzinfo=timezone.utc), False),    # 9 h 00
    (datetime(2026, 10, 7, 5, 59, tzinfo=timezone.utc), False),   # 7 h 59
    (datetime(2026, 10, 25, 6, 30, tzinfo=timezone.utc), False),  # jour du passage à l'heure d'hiver : 7 h 30
    (datetime(2026, 10, 25, 7, 30, tzinfo=timezone.utc), True),   # 8 h 30 heure d'hiver
    (datetime(2026, 3, 29, 6, 30, tzinfo=timezone.utc), True),    # passage à l'heure d'été : 8 h 30
    (datetime(2026, 3, 29, 7, 30, tzinfo=timezone.utc), False),   # 9 h 30
    (datetime(2026, 12, 31, 7, 15, tzinfo=timezone.utc), True),   # hiver : 8 h 15
])
def test_fenetre_du_matin(utc, inside):
    assert MO.in_window(utc) is inside


def test_jour_de_paris_et_soiree():
    from app.lab import brief as B

    assert MO.paris_day(datetime(2026, 10, 6, 22, 30, tzinfo=timezone.utc)) == "2026-10-07"  # 0 h 30 à Paris
    assert B.soiree(paris(2026, 10, 6, 23, 0)) == B.soiree(paris(2026, 10, 7, 0, 40)) == "2026-10-06"
    assert B.soiree(paris(2026, 10, 7, 12, 0)) == "2026-10-07"


# --- Préparation et envoi ---------------------------------------------------------------------

def test_hors_fenetre_rien(env):
    db, props, st, conn = env
    insert_validated(db, "signal tendu", SIGNAL)
    assert MO.tick(paris(2026, 10, 7, 7, 59), props, st, True, conn_factory=conn) is None
    assert MO.tick(paris(2026, 10, 7, 9, 0), props, st, True, conn_factory=conn) is None
    assert props.items == []


def test_point_du_matin_prepare_une_fois(env):
    db, props, st, conn = env
    insert_validated(db, "signal tendu", SIGNAL, pourquoi="Les signaux tendus s'essoufflent dès l'ouverture.")
    db.conn.execute("INSERT INTO rules (hash, name, rule, status, reason, submitted_at, tested_at) VALUES "
                    "('r1', 'x', '{}', 'rejetee', '[]', 'x', '2026-10-06T23:30:00+00:00')")
    d = MO.tick(AT_0805, props, st, True, conn_factory=conn)
    assert d["etat"] == "a_envoyer" and len(d["propositions"]) == 1
    p = props.get(d["propositions"][0])
    assert p["kind"] == "labo" and p["status"] == "pending" and p["groupe"] == "2026-10-07"
    text = d["texte"]
    assert "« signal tendu »" in text and "font −0,40 R en moyenne sur 212 trades" in text
    assert "Pourquoi : Les signaux tendus" in text and "1 autre idée testée depuis hier" in text
    assert f"`!oui {p['id']}`" in text and len(text) <= MO.MESSAGE_MAX
    assert MO.tick(paris(2026, 10, 7, 8, 30), props, st, True, conn_factory=conn) is None  # une seule fois


def test_rien_de_valide_pas_de_message(env):
    db, props, st, conn = env
    d = MO.tick(AT_0805, props, st, True, conn_factory=conn)
    assert d["etat"] == "rien" and d["texte"] is None
    assert asyncio.run(MO.send_due(st, props, AT_0805, {1}, None, None)) == 0


def test_rodage_garde_sans_envoyer(env, monkeypatch, caplog):
    db, props, st, conn = env
    caplog.set_level("INFO")
    monkeypatch.setenv("JARVIS_LAB_DM", "off")
    insert_validated(db, "signal tendu", SIGNAL)
    d = MO.tick(AT_0805, props, st, True, conn_factory=conn)
    assert d["etat"] == "journalise" and "signal tendu" in caplog.text
    user = FakeUser()
    assert asyncio.run(MO.send_due(st, props, AT_0805, {1}, lambda oid: _async(user), lambda pids: None)) == 0
    assert user.sent == [] and props.pending("labo")  # approuvable quand même (!oui, cockpit)


def test_bot_absent_garde_sans_envoyer(env):
    db, props, st, conn = env
    insert_validated(db, "signal tendu", SIGNAL)
    assert MO.tick(AT_0805, props, st, False, conn_factory=conn)["etat"] == "journalise"


async def _async(x):
    return x


def test_envoi_une_seule_fois_meme_apres_redemarrage(env, tmp_path):
    db, props, st, conn = env
    insert_validated(db, "signal tendu", SIGNAL)
    insert_validated(db, "rvol fou", {"field": "context.rvol", "op": ">", "value": 6})
    d = MO.tick(AT_0805, props, st, True, conn_factory=conn)
    user = FakeUser()
    views = []
    sent = asyncio.run(MO.send_due(st, props, AT_0805, {1}, lambda oid: _async(user), lambda pids: views.append(pids) or "vue"))
    assert sent == 1 and user.sent[0] == (d["texte"], "vue") and views == [d["propositions"]]
    assert all(props.get(i)["offerte"] is True for i in d["propositions"])
    assert asyncio.run(MO.send_due(st, props, AT_0805, {1}, lambda oid: _async(user), lambda pids: "vue")) == 0
    again = MO.State(str(tmp_path / "matin.json"))  # redémarrage du serveur
    assert again.day("2026-10-07")["etat"] == "envoye"
    assert MO.tick(paris(2026, 10, 7, 8, 40), props, again, True, conn_factory=conn) is None
    assert asyncio.run(MO.send_due(again, props, AT_0805, {1}, lambda oid: _async(user), lambda pids: "vue")) == 0
    assert len(user.sent) == 1


def test_envoi_interrompu_jamais_refait(env, tmp_path):
    db, props, st, conn = env
    insert_validated(db, "signal tendu", SIGNAL)
    MO.tick(AT_0805, props, st, True, conn_factory=conn)
    st.data["jours"]["2026-10-07"]["etat"] = "envoi"  # le serveur s'est arrêté pendant l'envoi
    st.save()
    again = MO.State(str(tmp_path / "matin.json"))
    assert again.day("2026-10-07")["etat"] == "interrompu"
    user = FakeUser()
    assert asyncio.run(MO.send_due(again, props, AT_0805, {1}, lambda oid: _async(user), lambda p: None)) == 0


def test_echec_reessaye_seulement_pour_qui_n_a_rien_recu(env):
    db, props, st, conn = env
    insert_validated(db, "signal tendu", SIGNAL)
    MO.tick(AT_0805, props, st, True, conn_factory=conn)
    users = {1: FakeUser(), 2: FakeUser(fail=1)}
    fetch = lambda oid: _async(users[oid])  # noqa: E731
    assert asyncio.run(MO.send_due(st, props, AT_0805, {1, 2}, fetch, lambda p: None)) == 1
    assert st.day("2026-10-07")["etat"] == "a_envoyer"
    assert asyncio.run(MO.send_due(st, props, paris(2026, 10, 7, 8, 6), {1, 2}, fetch, lambda p: None)) == 1
    assert len(users[1].sent) == 1 and len(users[2].sent) == 1 and st.day("2026-10-07")["etat"] == "envoye"


def test_manque_reporte_au_lendemain(env):
    db, props, st, conn = env
    insert_validated(db, "signal tendu", SIGNAL)
    d = MO.tick(paris(2026, 10, 7, 8, 59), props, st, True, conn_factory=conn)
    user = FakeUser()
    # Le bot ne se connecte qu'à 9 h 02 : pas d'envoi hors de la fenêtre.
    assert asyncio.run(MO.send_due(st, props, paris(2026, 10, 7, 9, 2), {1}, lambda o: _async(user), lambda p: None)) == 0
    assert st.day("2026-10-07")["etat"] == "manque" and user.sent == []
    nxt = MO.tick(paris(2026, 10, 8, 8, 1), props, st, True, conn_factory=conn)
    assert nxt["propositions"] == d["propositions"] and nxt["etat"] == "a_envoyer"
    assert props.get(d["propositions"][0])["groupe"] == "2026-10-08"


def test_proposition_deja_decidee_pas_renvoyee(env):
    db, props, st, conn = env
    insert_validated(db, "signal tendu", SIGNAL)
    d = MO.tick(AT_0805, props, st, True, conn_factory=conn)
    props.decide(d["propositions"][0], False)  # refusée dans le cockpit avant l'envoi
    user = FakeUser()
    assert asyncio.run(MO.send_due(st, props, AT_0805, {1}, lambda o: _async(user), lambda p: None)) == 0
    assert st.day("2026-10-07")["etat"] == "sans_objet"


def test_texte_borne_avec_un_long_pourquoi():
    items = [{"id": i, "nom": f"règle {i}", "why": "x" * 300, "pourquoi": "y" * 1000} for i in range(1, 6)]
    text = MO.text("2026-10-07", items, 12)
    assert len(text) <= MO.MESSAGE_MAX and "règle 5" in text and "!non N" in text


def test_base_absente_ou_illisible(env, tmp_path):
    db, props, st, conn = env
    assert MO.tick(AT_0805, props, st, True, conn_factory=lambda: None) is None
    assert MO.pull_events(st, AT_0805, conn_factory=lambda: None) == []
    import sqlite3

    broken = tmp_path / "vide.db"
    sqlite3.connect(broken).close()
    assert MO.tick(AT_0805, props, st, True, conn_factory=lambda: open_readonly(str(broken))) is None


def test_annonces_relayees_une_fois(env):
    db, props, st, conn = env
    M.event(db, "bascule", "v2", "ancienne", now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    M.event(db, "bascule", "v2", "🧪 Méthode v2 en place", now=AT_0805)
    evs = MO.pull_events(st, AT_0805, conn_factory=conn)
    assert [e["texte"] for e in evs] == ["🧪 Méthode v2 en place"]  # premier lancement : seulement les récentes
    assert MO.pull_events(st, AT_0805, conn_factory=conn) == []
    M.event(db, "retrait", "v3", "🧪 Méthode v3 retirée", now=AT_0805)
    assert [e["texte"] for e in MO.pull_events(st, AT_0805, conn_factory=conn)] == ["🧪 Méthode v3 retirée"]


def test_ligne_d_etat(env, monkeypatch):
    db, props, st, conn = env
    insert_validated(db, "signal tendu", SIGNAL)
    MO.tick(AT_0805, props, st, True, conn_factory=conn)
    line = MO.status_line(st, props)
    assert "Propositions du labo en attente : 1" in line and "à envoyer" in line and "activé" in line
    monkeypatch.setenv("JARVIS_LAB_DM", "off")
    assert "rodage" in MO.status_line(st, props)


# --- Propositions du labo ---------------------------------------------------------------------

def test_proposition_du_labo_approuvee_va_dans_la_file(env):
    db, props, st, conn = env
    h = insert_validated(db, "signal tendu", SIGNAL)
    p = props.propose_lab(h, "signal tendu", "v1", "preuve", None, "2026-10-07")
    assert props.propose_lab(h, "signal tendu", "v1", "preuve", None, "2026-10-08") is None  # une seule fois
    before = props.settings.all()
    out = props.decide(p["id"], True)
    assert out["status"] == "approved" and props.settings.all() == before  # aucun réglage touché
    data = json.loads((M.queue_root(db.path) / "decisions" / f"{h}.json").read_text())
    assert data["decision"] == "approuvee" and data["proposition"] == p["id"]
    assert PR.text(out).startswith(f"**Proposition {p['id']}** : méthode v1 + « signal tendu »")
    assert "part en essai" in PR.decision_text(out)


def test_file_inaccessible_proposition_reste_en_attente(env, monkeypatch):
    db, props, st, conn = env
    h = insert_validated(db, "signal tendu", SIGNAL)
    p = props.propose_lab(h, "signal tendu", "v1", "preuve", None, "2026-10-07")

    def boom(*a, **k):
        raise OSError("disque plein")
    monkeypatch.setattr(M, "enqueue_decision", boom)
    with pytest.raises(ValueError, match="toujours en attente"):
        props.decide(p["id"], False)
    assert props.get(p["id"])["status"] == "pending"


async def test_commandes_discord_du_labo(env):
    db, props, st, conn = env
    h = insert_validated(db, "signal tendu", SIGNAL)
    p = props.propose_lab(h, "signal tendu", "v1", "preuve", None, "2026-10-07")
    listing = await bot.handle("!propositions")
    assert "méthode v1 + « signal tendu »" in listing
    assert "ne sera plus proposée" in await bot.handle(f"!non {p['id']}")
    M.ingest_decisions(db)
    assert V.get_rule(db.conn, h)["decision"] == "refusee"
    labo = await bot.handle("!labo")
    assert "Méthode en place v1" in labo and "Propositions du labo en attente : 0" in labo


async def test_bot_n_envoie_pas_les_propositions_du_labo_une_par_une(env, monkeypatch):
    db, props, st, conn = env
    monkeypatch.setattr(bot, "outbox", asyncio.Queue())
    props.propose("rsi_max", 70, "test")
    props.propose_lab("a" * 64, "x", "v1", "preuve", None, "2026-10-07")
    assert bot.queue_pending() == 1
    sent = []

    async def fetch(oid):
        raise AssertionError("ne doit pas être appelé")
    assert await bot.deliver_once(props.lab("a" * 64), {1}, fetch, lambda pid: None) is True
    assert sent == []


async def test_tick_du_serveur_prepare_et_relaie(env, monkeypatch):
    db, props, st, conn = env
    monkeypatch.setattr(bot, "enabled", lambda: True)
    monkeypatch.setattr(bot, "lab_texts", asyncio.Queue())
    insert_validated(db, "signal tendu", SIGNAL)
    M.event(db, "bascule", "v2", "🧪 Méthode v2 en place : test", now=AT_0805)
    d = await scheduler.tick_lab(AT_0805)
    assert d["etat"] == "a_envoyer" and bot.lab_texts.get_nowait() == "🧪 Méthode v2 en place : test"
    assert await scheduler.tick_lab(paris(2026, 10, 7, 8, 6)) is None and bot.lab_texts.empty()


async def test_annonce_sur_le_salon_sans_bot(env, monkeypatch, tmp_path):
    db, props, st, conn = env
    monkeypatch.setattr(bot, "enabled", lambda: False)
    monkeypatch.setattr(scheduler, "enabled", lambda: True)
    monkeypatch.setattr(scheduler, "_kick_outbox", lambda: None)
    from app.core.outbox import Outbox

    monkeypatch.setattr(scheduler, "outbox", Outbox(str(tmp_path / "o.json")))
    M.event(db, "retrait", "v2", "🧪 Méthode v2 retirée", now=AT_0805)
    await scheduler.tick_lab(paris(2026, 10, 7, 12, 0))
    assert scheduler.outbox.items[0]["parts"] == ["🧪 Méthode v2 retirée"]


def test_rodage_remis_apres_preparation_rien_n_est_envoye(env, monkeypatch, tmp_path):
    """Relecture : point préparé « à envoyer » (JARVIS_LAB_DM=on), puis JARVIS_LAB_DM=off et redémarrage
    dans la fenêtre : rien ne doit partir."""
    db, props, st, conn = env
    insert_validated(db, "signal tendu", SIGNAL)
    assert MO.tick(AT_0805, props, st, True, conn_factory=conn)["etat"] == "a_envoyer"
    monkeypatch.setenv("JARVIS_LAB_DM", "off")
    again = MO.State(str(tmp_path / "matin.json"))
    user = FakeUser()
    assert asyncio.run(MO.send_due(again, props, paris(2026, 10, 7, 8, 10), {1}, lambda o: _async(user),
                                   lambda p: None)) == 0
    assert user.sent == [] and again.day("2026-10-07")["etat"] == "journalise"
