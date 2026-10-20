"""Page « Labo » du cockpit (/cockpit/labo) : rendue côté serveur, en lecture seule, sans script.

Grade de JARVIS (F- à S+, risque permis), avancement du rejeu, chiffres clés, méthode en place et challenger, dernières propositions du labo et
leur état, dernières règles testées, dernier message du matin. Les décisions se prennent dans le
cockpit principal (propositions en attente), sur Discord (boutons, `!oui N` / `!non N`).
"""
from datetime import datetime, timezone
from html import escape

from app.lab import method as M
from app.lab import morning as MO

STATUS_FR = {"pending": "en attente", "approved": "approuvée", "refused": "refusée", "replaced": "remplacée"}
RULE_FR = {"en_attente": "en attente", "validee": "validée", "rejetee": "rejetée", "invalide": "invalide"}
DECISION_FR = {"approuvee": "approuvée, en attente d'essai", "en_essai": "en essai (challenger)",
               "adoptee": "adoptée (méthode en place)", "retiree": "retirée après essai", "refusee": "refusée par Simão",
               "inapplicable": "inapplicable"}

CSS = """
:root { --bg:#f4f6fa; --card:#fff; --text:#0f172a; --muted:#64748b; --line:#e2e8f0; --up:#059669; --down:#dc2626;
  --accent:#4f46e5; --mono:"JetBrains Mono",ui-monospace,Menlo,monospace; }
@media (prefers-color-scheme: dark) { :root { --bg:#0b0d12; --card:#141821; --text:#e8ecf3; --muted:#8b95a7;
  --line:#232a36; --up:#34d399; --down:#f87171; --accent:#818cf8; } }
* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--text);
  font:14px/1.45 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
main { max-width:1040px; margin:0 auto; padding:16px; } h1 { font-size:19px; margin:0 0 4px; }
h2 { font-size:15px; margin:0 0 10px; } a { color:var(--accent); }
.card { background:var(--card); border:1px solid var(--line); border-radius:18px; padding:16px; margin-bottom:14px; }
.muted { color:var(--muted); } .small { font-size:12px; } .num { font-family:var(--mono); }
.up { color:var(--up); } .down { color:var(--down); }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; }
.bar { height:6px; background:var(--line); border-radius:6px; overflow:hidden; margin-top:6px; }
.bar > span { display:block; height:100%; background:var(--accent); }
table { width:100%; border-collapse:collapse; } td, th { text-align:left; padding:6px 4px; border-top:1px solid var(--line);
  vertical-align:top; } th { font-weight:600; color:var(--muted); font-size:12px; }
pre { white-space:pre-wrap; font:13px/1.45 inherit; margin:0; }
"""


def _e(x) -> str:
    return escape("" if x is None else str(x))


def _r(x, digits: int = 3) -> str:
    cls = "" if x is None else ("up" if x > 0 else "down" if x < 0 else "")
    return f'<span class="num {cls}">{_e(M.fr(x, digits))}</span>'


def _proposal_name(p: dict) -> str:
    return p.get("nom") or f"{p.get('key')} → {p.get('value')}"


def _grade_card(grade: dict | None) -> str:
    from app.lab import grade as GR

    marches = (grade or {}).get("marches") or {}
    if not marches:
        return '<div class="card"><h2>🏅 Grade de JARVIS</h2><div class="small muted">Pas encore calculé.</div></div>'
    cells = []
    for market in GR.MARKETS:
        g = marches.get(market)
        if not g:
            continue
        m = g.get("mesure") or {}
        detail = (f'{_r(m.get("esperance_r"))} par trade sur {m.get("trades")} trades · réussite '
                  f'{round(100 * (m.get("reussite") or 0))} % · TP1 {round(100 * (m.get("tp1") or 0))} %'
                  if not g.get("non_note") else _e(m.get("raison")))
        window = GR.window_text(m.get("fenetre"))
        detail += f'<br>{_e(window)}' if window else ""
        flags = (" · ☠️ méthode morte" if g.get("morte") else " · 🆘 mode survie" if g.get("mode_survie") else "")
        wait = (f'<div class="small">⏳ hausse vers {_e(GR.name(g["hausse"]["vers"]))} en attente de ton accord</div>'
                if g.get("hausse") else "")
        cells.append(f'<div><b>{_e(GR.MARKET_FR[market].capitalize())} : <span class="num">{_e(g["grade"])}</span></b>'
                     f' <span class="small muted">mesuré {_e(m.get("grade"))}</span><div class="small">'
                     f'{_e(GR.allowed(g["risque_grade"]))} (plafond {g["risque_max"]:g} %){flags}</div>'
                     f'<div class="small muted">{detail}</div>{wait}</div>')
    age = GR.age_h(grade, datetime.now(timezone.utc))
    stale = ""
    if age is None or age > GR.MAX_AGE_H:
        when = "aucun passage du labo daté" if age is None else f"dernier passage du labo il y a {age:.0f} h"
        stale = f'<div class="small">⚠️ Grade périmé ({when}) : JARVIS ne propose aucun risque (0 %)</div>'
    return f'<div class="card"><h2>🏅 Grade de JARVIS</h2>{stale}<div class="grid">{"".join(cells)}</div></div>'


def render(st: dict, proposals: list[dict], rules: list[dict], morning: dict | None, dm: bool,
           grade: dict | None = None) -> str:
    """HTML complet de la page. `st` : lab_status.status() ; `proposals` : propositions du labo ;
    `rules` : validator.list_rules ; `morning` : dernier point du matin ; `grade` : lab_grade.json."""
    parts = []
    head = (f'<h1>🧪 Labo de performance</h1><div class="muted small">Lecture seule · '
            f'<a href="/cockpit">← cockpit</a> · message privé du matin : '
            f'{"activé" if dm else "en rodage (JARVIS_LAB_DM=off)"}</div>')
    parts.append(head)
    if not st.get("exists"):
        parts.append('<div class="card">Le labo n\'a pas encore démarré (aucune base).</div>')
        return _page(parts)
    parts.append(_grade_card(grade))
    # Avancement
    cells = []
    for market, label in (("stock", "Actions"), ("crypto", "Crypto")):
        r = (st.get("replay") or {}).get(market) or {}
        t = (st.get("trades") or {}).get(market) or {}
        cells.append(f'<div><b>{label}</b> <span class="muted small">rejeu {r.get("days_done", 0)}/{r.get("days_total", 0)} jours'
                     f'</span><div class="bar"><span style="width:{min(100.0, float(r.get("pct") or 0))}%"></span></div>'
                     f'<div class="small muted">{t.get("replay", 0)} trades rejoués · {t.get("live", 0)} en direct · '
                     f'dernier jour en direct {_e((st.get("live") or {}).get(market, {}).get("last_day") or "—")}</div></div>')
    parts.append(f'<div class="card"><h2>Avancement</h2><div class="grid">{"".join(cells)}</div></div>')
    # Chiffres clés
    l2 = st.get("labo2") or {}
    stt = l2.get("stats") or {}
    regles = l2.get("regles") or {}
    fiches = l2.get("fiches") or {}
    ci = stt.get("ic95")
    parts.append(
        '<div class="card"><h2>Chiffres clés</h2><div class="grid">'
        f'<div>Espérance par trade<br>{_r(stt.get("esperance_r"))}'
        + (f' <span class="small muted num">[{_e(M.fr(ci[0], 2, ""))} ; {_e(M.fr(ci[1], 2, ""))}]</span>' if ci else "")
        + f'<div class="small muted">{stt.get("declenches") or 0} trades déclenchés</div></div>'
        f'<div>Règles<br><span class="num">{regles.get("validee", 0)}</span> validées · '
        f'<span class="num">{regles.get("rejetee", 0)}</span> rejetées · <span class="num">{regles.get("en_attente", 0)}</span>'
        f' en attente<div class="small muted">{regles.get("essais", 0)} essais au compteur</div></div>'
        f'<div>Fiches catalyseurs<br><span class="num">{fiches.get("total", 0)}</span>'
        f'<div class="small muted">{fiches.get("a_classer", 0)} à classer</div></div></div></div>')
    # Méthode
    meth = st.get("methode") or {}
    champ = meth.get("champion") or {}
    rows = "".join(f'<li>{_e(r["nom"])} : <span class="muted">{_e(r["regle"])}</span></li>' for r in champ.get("regles") or [])
    block = (f'<div><b>En place : {_e(champ.get("version") or "n/d")}</b>'
             + (f'<ul class="small">{rows}</ul>' if rows else '<div class="small muted">aucun filtre</div>') + '</div>')
    ch = meth.get("challenger")
    if ch:
        cmp = ch["comparaison"]
        n, last = cmp["jours"], ch["regards"][-1]
        nxt = f'prochain regard à {ch["prochain_regard"]} jours' if ch.get("prochain_regard") else "regards faits"
        rows = "".join(f'<li>{_e(r["nom"])} : <span class="muted">{_e(r["regle"])}</span></li>' for r in ch["regles"])
        seuil = ch.get("seuil_texte") or f'seuil z {M.num_fr(ch["seuil_z"])}'
        # Étiquette absolue (M.absolute_label) : un écart relatif positif ne dit pas que la méthode gagne.
        label = ch.get("etiquette")
        block += (f'<div><b>Challenger : {_e(ch["version"])}</b> <span class="small muted">depuis {_e(ch["depuis"])}</span>'
                  f'<div class="bar"><span style="width:{min(100, 100 * n / max(1, last))}%"></span></div>'
                  f'<div class="small">{n} jours de trades en direct ({nxt}) · écart {_r(cmp["delta_r"])} par trade, '
                  f'z {_e(M.num_fr(cmp["z"]))}, {_e(seuil)} · {_r(cmp["challenger"]["esperance_r"])} '
                  f'sur {cmp["challenger"]["declenches"]} trades contre {_r(cmp["champion"]["esperance_r"])} pour '
                  f'{_e(champ.get("version"))} · potentiel +10 {cmp["challenger"]["capture"]} contre '
                  f'{cmp["champion"]["capture"]}</div>'
                  + (f'<div class="small"><b>{_e(label[0].upper() + label[1:])}.</b></div>' if label else "")
                  + ('<div class="small"><b>Promotion proposée à Simão</b>, en attente de sa réponse</div>'
                     if ch.get("promotion") else "")
                  + f'<ul class="small">{rows}</ul></div>')
    else:
        block += '<div class="small muted">Pas de challenger en essai.</div>'
    parts.append(f'<div class="card"><h2>Méthode</h2><div class="grid">{block}</div></div>')
    # Propositions
    if proposals:
        trs = "".join(f'<tr><td class="num">{p["id"]}</td><td>{_e(_proposal_name(p))}'
                      f'<div class="small muted">{_e(p.get("why"))}</div>'
                      f'</td><td>{_e(STATUS_FR.get(p["status"], p["status"]))}</td><td class="small muted">'
                      f'{_e((p.get("created_at") or "")[:10])}</td></tr>' for p in proposals[:15])
        parts.append(f'<div class="card"><h2>Dernières propositions</h2><table><tr><th>N°</th><th>Règle</th><th>État</th>'
                     f'<th>Le</th></tr>{trs}</table></div>')
    # Règles testées
    if rules:
        trs = []
        for r in rules[:20]:
            m = r.get("mesures") or {}
            state = DECISION_FR.get(r.get("decision")) or RULE_FR.get(r["etat"], r["etat"])
            reason = (r.get("raisons") or [""])[0]
            trs.append(f'<tr><td>{_e(r.get("nom") or r["empreinte"][:10])}<div class="small muted">'
                       f'{_e(M.rule_fr(r["regle"]))}</div></td><td>{_e(state)}<div class="small muted">{_e(reason)}</div></td>'
                       f'<td>{_r(m.get("amelioration_moyenne_r"))}</td><td class="small muted">'
                       f'{_e((r.get("testee_le") or r.get("soumise_le") or "")[:10])}</td></tr>')
        parts.append(f'<div class="card"><h2>Règles testées</h2><table><tr><th>Règle</th><th>Verdict</th>'
                     f'<th>Écart / trade (relatif)</th><th>Le</th></tr>{"".join(trs)}</table></div>')
    if morning:
        parts.append(f'<div class="card"><h2>Message du matin · {_e(morning["jour"])}</h2><div class="small muted">'
                     f'{_e(MO.ETATS.get(morning.get("etat"), morning.get("etat")))}</div>'
                     + (f'<pre>{_e(morning.get("texte"))}</pre>' if morning.get("texte") else "") + '</div>')
    if st.get("error"):
        parts.append(f'<div class="card down">{_e(st["error"])}</div>')
    return _page(parts)


def _page(parts: list[str]) -> str:
    return ('<!doctype html><html lang="fr"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Labo · JARVIS</title><link rel="icon" type="image/svg+xml" href="/cockpit-static/icon.svg">'
            f'<style>{CSS}</style></head><body><main>{"".join(parts)}</main></body></html>')
