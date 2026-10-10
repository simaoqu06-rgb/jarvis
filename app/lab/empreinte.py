"""Empreintes du code du labo et compteurs de révision des données (avis de GPT et Gemini du 10 octobre, I2 et I3).

« v1 » fige les réglages, pas le code : sans trace, une ligne de trades ne dit pas quel code l'a produite, et un
cache de statistiques ne voit pas qu'un trade a été réécrit sur place ou que le code a changé.

- v1_fingerprint() : empreinte du COMPORTEMENT du Labo 1, comme kill_complet.cost_fingerprint : des bougies
  synthétiques figées (actions décidées à 9 h 10, dernière bougie connue 15 h 45 la veille ; crypto à 4 h UTC)
  passent par generator.stock_inputs / crypto_inputs, make_plan (stock_plans en actions, avec le plan du direct
  gardé dans trades.plan), compact_plan, setup_type, simulator.simulate (réglages par défaut) et simulator.costs.
  V1_FINGERPRINT est la valeur du code d'aujourd'hui, suivie par tests/test_empreinte_v1.py : un comportement qui
  change casse le test et oblige à choisir, revenir en arrière ou ouvrir une nouvelle version. Un commentaire ou
  un texte modifié (raisons, sources des niveaux, verdict) ne la change pas.
- code_rev(kind) : hachage du source (arbre syntaxique, sans commentaires ni docstrings) des fonctions et
  constantes qui produisent les lignes, « v1 » (trades) ou « strat » (lignes du labo v2), pas des fichiers
  entiers de service.py ; lu une fois à l'import, sans rien importer, écrit dans chaque ligne. Un changement de
  source change la révision même sans changer le comportement : c'est un indice, la preuve est l'empreinte v1.
- cost_rev() : empreinte du modèle de coût v1 (simulator.costs sur des prix et rangs types), trades.cout_rev.
- STATS_CODE : hachage des sources des modules qui calculent les caches (app/lab, killmode), ajouté à leurs
  signatures : un déploiement les recalcule une fois, tout de suite (code_changed), sans attendre leur âge maximal.
- revision(conn, table) : compteurs rev:trades et rev:strat (progress « lab »), incrémentés dans la transaction
  de chaque écriture (LabDB.bump) : un trade réécrit sur place (même COUNT, même MAX(id)) invalide les caches.
"""
import ast
import hashlib
import json
from datetime import date
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
# Code du Labo 1 : (fichier sous app/, noms de premier niveau ; None = tout le module).
V1_CODE = (
    ("trading/service.py", ("SETTINGS", "_market", "with_open_bar", "plan_levels", "plan_from_bars",
                            "STOP_FLOOR_DAILY_ATR", "_floor_stop")),
    ("trading/plan.py", None),
    ("trading/score.py", None),
    ("trading/signals.py", ("_item", "checklist")),
    ("trading/indicators.py", None),
    ("lab/generator.py", None),
    ("lab/collector.py", ("BASE_TF", "STOCK_HISTORY_SESSIONS", "CRYPTO_HISTORY_DAYS", "DAILY_HISTORY_DAYS", "_new",
                          "_merge", "aggregate", "regular_hours", "known_before", "in_progress", "stock_window",
                          "crypto_window")),
    ("lab/simulator.py", ("BAR_SECONDS", "STOCK_SLIPPAGE_BPS", "STOCK_COMMISSION", "CRYPTO_SLIPPAGE_BPS",
                          "OKX_MAKER_FEE", "OKX_TAKER_FEE", "stock_slippage_bps", "crypto_slippage_bps",
                          "stock_slippage", "costs", "tick_size", "round_stock", "_minutes", "simulate", "net_r",
                          "day_range_pct")),
    ("lab/universe.py", ("STOCK_TOP_DOLLAR", "STOCK_TOP_MOVERS", "CRYPTO_TOP", "RVOL_DAYS", "DECISION_NY",
                         "stock_decision_ts", "_metrics", "stock_universe", "crypto_universe", "build", "regime")),
)
# Labo v2 : le code du Labo 1 (v1 rejouée, plans des GO), les stratégies et le rattrapage.
STRAT_CODE = V1_CODE + (
    ("lab/strategies.py", None),
    ("lab/rattrapage.py", ("DAILY_LOOKBACK_DAYS", "STOCK_BATCH", "SPLIT_TOL", "v1_trades", "_v1", "_factor",
                           "operation", "_daily", "_lost", "_too_many", "stock_rows", "crypto_rows")),
    ("lab/plus10.py", ("OP_RATIO", "scale")),  # n° 10 : opérations sur titre, journalier remis à l'échelle
)
STATS_FILES = ("lab/*.py", "trading/killmode.py")
COST_PRICES = (0.5, 0.9999, 1.0, 2.5, 4.99, 5.0, 7.5, 19.99, 20.0, 150.0)
COST_RANKS = (1, 10, 11, None)


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body \
                and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) \
                and isinstance(node.body[0].value.value, str):
            node.body = node.body[1:] or [ast.Pass()]
    return tree


def _parts(rel: str, names: tuple[str, ...] | None) -> list[str]:
    """Arbre syntaxique (sans commentaires ni docstrings) des noms de premier niveau d'un fichier ; un nom absent
    (renommé, supprimé) est noté tel quel : la révision change, et tests/test_empreinte_v1.py le signale."""
    try:
        tree = _strip_docstrings(ast.parse((APP / rel).read_text(encoding="utf-8")))
    except (OSError, SyntaxError):
        return [f"{rel} illisible"]
    if names is None:
        return [ast.dump(tree)]
    found: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found[node.name] = node
        elif isinstance(node, ast.Assign):
            found.update({t.id: node for t in node.targets if isinstance(t, ast.Name)})
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found[node.target.id] = node
    return [ast.dump(found[n]) if n in found else f"{rel}:{n} absent" for n in names]


def missing() -> list[str]:
    """Noms de V1_CODE et STRAT_CODE introuvables dans le source (tests)."""
    return [p for rel, names in STRAT_CODE for p in _parts(rel, names) if p.endswith((" absent", " illisible"))]


def _code_hash(spec) -> str:
    return _sha([[rel, _parts(rel, names)] for rel, names in spec])


def _files_hash(patterns) -> str:
    h = hashlib.sha256()
    for pattern in patterns:
        for path in sorted(APP.glob(pattern)):
            h.update(path.name.encode())
            h.update(path.read_bytes())
    return h.hexdigest()[:10]


# À l'import : le code tel qu'il tourne.
CODE_REV = {"v1": _code_hash(V1_CODE), "strat": _code_hash(STRAT_CODE)}
STATS_CODE = _files_hash(STATS_FILES)


def code_rev(kind: str = "v1") -> str:
    """Révision du code qui produit les lignes : « v1 » (trades) ou « strat » (lignes du labo v2)."""
    return CODE_REV[kind]


def cost_rev() -> str:
    """Empreinte du modèle de coût du simulateur (actions par prix, crypto par rang de volume) : trades.cout_rev."""
    from app.lab import simulator as SIM  # import tardif : ce module reste sans dépendance à l'import

    return _sha([SIM.costs("stock", p) for p in COST_PRICES] + [SIM.costs("crypto", 100.0, r) for r in COST_RANKS])


def revision(conn, table: str) -> int:
    """Compteur de révision des données d'une table (« trades » ou « strat »), 0 avant la première écriture."""
    row = conn.execute("SELECT value FROM progress WHERE job = 'lab' AND key = ?", (f"rev:{table}",)).fetchone()
    return json.loads(row[0]) if row else 0


def sig(conn, *tables: str) -> list:
    """Fin de signature d'un cache : compteurs de révision des tables lues, puis l'empreinte du code des stats."""
    return [revision(conn, t) for t in tables] + [STATS_CODE]


def code_changed(last) -> bool:
    """Le cache a été calculé par un autre code (déploiement) ou jamais : à recalculer sans attendre son âge."""
    return not last or STATS_CODE not in last


# --- Empreinte de comportement du Labo 1 ------------------------------------------------------------

# Comportement du code du 10 octobre 2026 (924e4d6, identique à celui de ce module) : ne se change qu'en revenant
# en arrière ou en ouvrant une nouvelle version, jamais pour faire passer le test.
V1_FINGERPRINT = "4f5ef18e6e"
# Réglages de la note figés pour l'empreinte (ceux de la naissance de v1) : un défaut changé dans settings.py ne
# change pas v1, qui note avec son cliché.
NOTE_V1 = {"rr_min": 2.0, "rr_tolerance": 0.2, "go_min": 7.0, "earnings_veto_hours": 48,
           "weights": {"technique": 0.4, "catalyst": 0.3, "risk": 0.3}, "rsi_min": 45.0, "rsi_max": 75.0,
           "max_stop_atr": 2.5, "min_tp1_r": 0.8}
FP_STOCKS = (("AAA", 12.0, 0.004), ("BBB", 3.2, 0.006), ("CCC", 48.0, 0.003))
FP_STOCK_DAYS = (date(2025, 3, 5), date(2025, 3, 6))
FP_CRYPTO = (("AAA-USDT", 3, 2.4, 0.005), ("BBB-USDT", 25, 0.087, 0.008))
FP_CRYPTO_DAYS = (date(2025, 3, 5), date(2025, 3, 6))
FP_CRYPTO_HOUR = 4
# Ce qui fait le résultat d'un trade v1 (les champs ajoutés plus tard au résultat n'y entrent pas).
RESULT_KEYS = ("triggered", "filled_at", "fill_price", "entry_mode", "entry_liquidity", "exit_reason", "exits",
               "r_gross", "r_net", "tp_hits", "minutes_to_tp", "mfe_r", "mae_r", "closed_at", "minutes_to_fill",
               "slippage_bps", "fees", "order_prices", "data_incomplete")


def _walk(times: list[int], start: float, seed: int, vol: float) -> list[dict]:
    """Marche pseudo-aléatoire figée : générateur congruentiel et arithmétique seule (pas de module random ni de
    fonction transcendante), mêmes bougies sur toute version de Python."""
    x = seed

    def u() -> float:
        nonlocal x
        x = (1103515245 * x + 12345) % 2147483648
        return x / 2147483648

    out, p = [], start
    for t in times:
        o = p
        p = max(0.05, p * (1 + (u() - 0.4995) * 2 * vol))
        h = max(o, p) * (1 + u() * vol)
        lo = min(o, p) * (1 - u() * vol)
        out.append({"t": t, "o": round(o, 4), "h": round(h, 4), "l": round(lo, 4), "c": round(p, 4),
                    "v": float(1000 + int(u() * 9000))})
    return out


def _norm(x):
    if isinstance(x, float):
        return round(x, 6)
    if isinstance(x, dict):
        return {k: _norm(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_norm(v) for v in x]
    return x


def _plan_part(G, plan: dict) -> dict:
    """Plan gardé par le journal, sans ses textes (raisons, sources, verdict, méthode) : un mot changé n'est pas
    un comportement (les prix, la note et la checklist le sont)."""
    p = G.compact_plan(plan)
    p.pop("method", None)
    p.pop("stop_source", None)
    p["checklist"].pop("verdict", None)
    for t in p["targets"]:
        t.pop("source", None)
    p["score"].pop("reasons", None)
    for part in p["score"]["parts"].values():
        part.pop("reasons", None)
    return p


def _trade(G, SIM, plan: dict, sim_bars: list[dict], market: str, moment: int, end: int, last: float,
           rank, floored: dict | None = None) -> dict:
    cost = SIM.costs(market, plan["entry"], rank)
    result = SIM.simulate(plan, sim_bars, market=market, decided_at=moment, entry_until=end, exit_at=end,
                          last_price=last, cost=cost)
    out = {"plan": _plan_part(G, plan), "setup": G.setup_type(plan), "cout": cost,
           "resultat": {k: result.get(k) for k in RESULT_KEYS}}
    if market == "stock":  # plan du direct (n° 7), gardé dans trades.plan et rejoué par le labo v2
        floor = G.floor_summary(plan, floored)
        out["plancher"] = floor and {k: v for k, v in floor.items() if k != "stop_source"}
    return out


def v1_fingerprint() -> str:
    """Empreinte du comportement du Labo 1 sur des bougies synthétiques figées (sans réseau ni base sur disque)."""
    from app.lab import collector as CO  # imports tardifs : à l'import, ce module ne lit que des fichiers
    from app.lab import generator as G
    from app.lab import simulator as SIM
    from app.lab.db import LabDB
    from app.trading import service as T
    from app.trading.settings import DEFAULTS

    s = json.loads(json.dumps(DEFAULTS)) | json.loads(json.dumps(NOTE_V1))
    db = LabDB(":memory:")
    out = []
    try:
        first = CO.sessions_back(FP_STOCK_DAYS[0], CO.STOCK_HISTORY_SESSIONS + 5)
        times = [t for d in CO.trading_days(first, FP_STOCK_DAYS[-1])
                 for t in range(CO.session_ts(d)[0], CO.session_ts(d)[1], 300)]
        for i, (symbol, price, vol) in enumerate(FP_STOCKS):
            bars = _walk(times, price, 7 + i, vol)
            db.put_bars("stock", symbol, CO.BASE_TF, bars)
            db.put_bars("stock", symbol, 1440, CO.aggregate(bars, 1440, "stock"))
        for day in FP_STOCK_DAYS:
            for symbol, _, _ in FP_STOCKS:
                inp = G.stock_inputs(db, symbol, day)
                item = {"marche": "stock", "symbole": symbol, "jour": day.isoformat()}
                if inp is None:
                    out.append(item | {"plan": "sans données"})
                    continue
                try:
                    plan, floored = G.stock_plans(symbol, inp, s)  # make_plan, comme le générateur
                except T.PlanError:
                    out.append(item | {"plan": "impossible"})
                    continue
                out.append(item | _trade(G, SIM, plan, inp["session"], "stock", inp["moment"], inp["bounds"][1],
                                         inp["bars"][-1]["c"], None, floored))
        start = CO.utc_midnight(FP_CRYPTO_DAYS[0]) - (CO.CRYPTO_HISTORY_DAYS + 2) * 86400
        times = list(range(start, CO.utc_midnight(FP_CRYPTO_DAYS[-1]) + 2 * 86400, 300))
        for i, (symbol, _, price, vol) in enumerate(FP_CRYPTO):
            bars = _walk(times, price, 101 + i, vol)
            db.put_bars("crypto", symbol, CO.BASE_TF, bars)
            db.put_bars("crypto", symbol, 1440, CO.aggregate(bars, 1440, "crypto"))
        for day in FP_CRYPTO_DAYS:
            moment = CO.utc_midnight(day) + FP_CRYPTO_HOUR * 3600
            for symbol, rank, _, _ in FP_CRYPTO:
                inp = G.crypto_inputs(G.crypto_symbol_day(db, symbol, day), moment)
                item = {"marche": "crypto", "symbole": symbol, "jour": day.isoformat()}
                if inp is None:
                    out.append(item | {"plan": "sans données"})
                    continue
                try:
                    plan = G.make_plan(symbol, "crypto", inp["bars"], inp["daily"], inp["h4"], moment, s)
                except T.PlanError:
                    out.append(item | {"plan": "impossible"})
                    continue
                out.append(item | _trade(G, SIM, plan, inp["sim"], "crypto", moment, inp["horizon"], inp["last"],
                                         rank))
    finally:
        db.close()
    return _sha(_norm(out))
