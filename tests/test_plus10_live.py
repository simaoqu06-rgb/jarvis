"""Potentiel +10 en direct (déclaration n° 9) : liste d'attente de 9 h 10, liste confirmée de 9 h 46 avec les niveaux
des essais du labo, range de 30 min de Rebond à 10 h 01."""
import json
from types import SimpleNamespace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.lab import collector as CO
from app.lab import strategies as ST
from app.trading import plus10 as P
from tests.test_killmode import RANGE, RUN, daily, hist
from tests.test_lab_strategies import DAY, OPEN, bars_from

NY = ZoneInfo("America/New_York")


def at(h, m):
    return datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=NY).astimezone(timezone.utc)


def _scaled(rows, k):
    return [b | {"o": b["o"] * k, "h": b["h"] * k, "l": b["l"] * k, "c": b["c"] * k} for b in rows]


RANGE30 = RANGE + [(106.3, 106.6, 106.0, 106.4), (106.4, 106.9, 106.2, 106.8), (106.8, 107.0, 106.5, 106.9)]


@pytest.fixture
def live(monkeypatch, tmp_path):
    ctx = SimpleNamespace(now=at(9, 10), path=tmp_path)
    monkeypatch.setattr(P, "JOURNAL_PATH", str(tmp_path / "plus10_scans.jsonl"))
    monkeypatch.setattr(P, "STATE_PATH", str(tmp_path / "plus10_state.json"))
    monkeypatch.setattr(P.config, "WATCHLIST_STOCKS", [])
    monkeypatch.setattr(P.alpaca, "configured", lambda: True)
    pre = {"UPA": 108.0, "UPB": 104.0, "DOWN": 93.0, "CALM": 100.2}       # dernier échange à 9 h 10
    opens = {"UPA": 105.0, "UPB": 106.0, "DOWN": 94.0, "CALM": 100.1}     # ouverture
    scale = {"UPA": 1.0, "UPB": 106.0 / 105.0, "DOWN": 0.9, "CALM": 1.0}

    async def most_actives(top=50):
        return ["CALM", "UPA"]

    async def gainers(top=50):
        return ["UPA", "UPB"]

    async def losers(top=50):
        return ["DOWN"]

    async def snapshots(symbols, feed="iex"):
        opened = ctx.now >= at(9, 30)
        return {s: {"price": pre[s], "open": opens[s] if opened else None, "open_day": DAY.isoformat() if opened else None}
                for s in symbols if s in pre}

    async def bars(symbols, tf, days, now=None, feed="sip"):
        if tf == 1440:
            return {s: daily() for s in symbols}
        assert feed == "iex"
        session = bars_from(OPEN, RANGE30 + [(107.0, 107.5, 106.8, 107.2)])
        return {s: _scaled(hist() + session, scale[s]) for s in symbols}

    async def news(symbol, limit, days=7):
        return [{"title": f"{symbol} wins contract", "url": "https://example.com"}]

    for name, fn in (("most_actives", most_actives), ("top_gainers", gainers), ("top_losers", losers),
                     ("snapshots", snapshots), ("bars", bars)):
        monkeypatch.setattr(P.alpaca, name, fn)
    monkeypatch.setattr(P.finnhub, "company_news", news)
    monkeypatch.setattr(P.PO, "current", lambda: {"par_gap": {"gap +5 à +10 %": {"n": 100, "zone_pct": 32.5}}})
    return ctx


def _journal(path):
    return [json.loads(x) for x in (path / "plus10_scans.jsonl").read_text().splitlines()]


async def test_liste_d_attente_au_gap_de_pre_ouverture(live):
    w = await P.waitlist(live.now)
    assert w["listes"] == {"s10": ["UPA"], "rebond": ["DOWN"]}   # UPB à +4 % avant l'ouverture
    text = P.waitlist_text(w)
    assert "liste d'attente" in text and "pas un GO" in text and "**UPA** +8,0 %" in text
    assert "labo : 32,5 % des séances" in text and "UPA wins contract" in text and "n'est pas" in text
    assert _journal(live.path)[0]["kind"] == "attente" and _journal(live.path)[0]["gaps"]["UPA"] == 8.0


async def test_liste_confirmee_au_gap_d_ouverture_avec_les_niveaux_du_labo(live):
    await P.waitlist(live.now)
    live.now = at(9, 46)
    assert P.due(live.now) == "confirmee"
    c = await P.confirmed("confirmee", live.now, risk_usd=23.0)
    assert c["listes"] == {"s10": ["UPB", "UPA"], "rebond": ["DOWN"]}  # UPB : +6 % à l'ouverture
    assert c["mouvements"]["s10"] == {"entrees": ["UPB"], "sorties": []}
    orb = c["niveaux_complets"]["UPA"]["orb15"]
    assert orb["entry"] == pytest.approx(106.51) and orb["stop"] == pytest.approx(104.49)
    assert c["niveaux_complets"]["UPA"]["killmode"]["entry"] == pytest.approx(106.51)
    assert "orb30" not in c["niveaux_complets"]["DOWN"] and c["niveaux_complets"]["DOWN"]["orb15"]
    text = P.confirmed_text(c)
    assert "liste confirmée" in text and "(pré-ouverture +8,0 %)" in text and "(absente à 9 h 10)" in text
    assert "ordre stop d'achat 106.51" in text and "règle Killmode : ✅" in text and "11 actions" in text
    assert "à 16 h 01" in text
    P.mark_done("confirmee", live.now)
    assert P.due(live.now) is None and _journal(live.path)[-1]["kind"] == "confirmee"

    live.now = at(10, 1)
    assert P.due(live.now) == "confirmee_30"
    late = await P.confirmed("confirmee_30", live.now, risk_usd=23.0)
    assert late["listes"] == {"s10": [], "rebond": ["DOWN"]} and late["niveaux_complets"]["DOWN"]["orb30"]
    assert "range 30 min : ordre stop d'achat" in P.confirmed_text(late)


def test_niveaux_du_direct_egaux_a_ceux_du_labo():
    full = ST.Day("stock", DAY, CO.regular_hours(hist())[-ST.STOCK_WARMUP_BARS:] + bars_from(OPEN, RANGE + RUN), daily())
    early = ST.Day("stock", DAY, CO.regular_hours(hist())[-ST.STOCK_WARMUP_BARS:] + bars_from(OPEN, RANGE), daily())
    assert ST.orb(early, 15) == []                                   # le labo attend une bougie de plus
    lab, now = ST.orb(full, 15)[0], P.orb_now(early, 15)
    assert (now["entry"], now["stop"], now["entry_until"]) == (lab["entry"], lab["stop"], lab["entry_until"])
    assert P.orb_now(early, 30) is None


def test_passages_une_fois_par_seance(live):
    assert P.due(at(9, 45)) is None and P.due(at(9, 46)) == "confirmee" and P.due(at(9, 55)) is None
    P.mark_done("confirmee", at(9, 47))
    assert P.due(at(9, 48)) is None and P.due(at(10, 2)) == "confirmee_30"


async def test_tick_du_scheduler_envoie_salon_et_mp(live, monkeypatch):
    from app import scheduler as SCH

    async def rate():
        return 1.15

    sent, dms = [], []
    monkeypatch.setattr(SCH, "_usd_per_eur", rate)
    monkeypatch.setattr(SCH, "_queue", lambda texts, kind="", **kw: sent.append(texts))
    monkeypatch.setattr(SCH, "_dm_trades", lambda texts: dms.append(texts))
    monkeypatch.setattr(SCH, "_kick_outbox", lambda: None)
    await SCH.tick_plus10(at(9, 40))
    assert not sent
    live.now = at(9, 46)
    await SCH.tick_plus10(at(9, 46))
    assert len(sent) == 1 and sent == dms and "liste confirmée" in sent[0] and "11 actions" in sent[0]  # 20 € × 1,15 = 23 $ / 2,02
    await SCH.tick_plus10(at(9, 47))
    assert len(sent) == 1  # une fois par passage


async def test_tick_taille_selon_le_grade(live, monkeypatch):
    from app import scheduler as SCH
    from app.lab import grade as GR

    async def rate():
        return 1.15

    sent = []
    monkeypatch.setattr(SCH, "_usd_per_eur", rate)
    monkeypatch.setattr(SCH, "_queue", lambda texts, kind="", **kw: sent.append(texts))
    monkeypatch.setattr(SCH, "_dm_trades", lambda texts: None)
    monkeypatch.setattr(SCH, "_kick_outbox", lambda: None)
    monkeypatch.setattr(SCH.book, "trades", [])
    monkeypatch.setenv("JARVIS_CAPITAL", "500")
    stock = {"grade": "A", "rang": 16, "non_note": False, "risque_grade": 3.0, "risque_max": 3.0, "mode_survie": False}
    monkeypatch.setattr(GR, "current", lambda: {"marches": {"stock": stock}, "verifie_le": at(9, 46).isoformat()})
    live.now = at(9, 46)
    await SCH.tick_plus10(at(9, 46))
    assert "8 actions" in sent[0]  # 500 € × 3 % = 15 € : 17,25 $ / 2,02
    assert "taille pour 15 € de risque (règle de JARVIS : grade A, 3 % proposé)" in sent[0]
    assert "hors avis" not in sent[0]


def test_texte_grade_f_hors_avis_et_action_hors_budget():
    c = {"kind": "confirmee", "listes": {"s10": [], "rebond": []}, "mesures": {}, "niveaux_complets": {},
         "gaps_attente": {}, "risk_usd": 23.0, "eurusd": 1.15, "risk_eur": 20.0,
         "grade": {"grade": "F", "risque_propose": 0.0, "observation": True, "raison": "grade F"}}
    txt = P.confirmed_text(c)
    assert "-# Taille à ta règle de 20 € (hors avis de JARVIS : grade F, 0 % proposé)." in txt
    assert "🏅 Grade JARVIS **F** · risque proposé **0 %** : plan en observation (grade F)" in txt
    until = int(at(10, 0).timestamp())
    assert P._order({"entry": 130.0, "stop": 100.0, "entry_until": until}, 23.0, 1.15).endswith(
        " · 1 action = 26 € de risque, au-delà de 20 €")
    assert P._order({"entry": 13.0, "stop": 12.0, "entry_until": until}, 23.0, 1.15).endswith(
        " · 23 actions pour ce risque")
