"""Page « Labo » du cockpit (/cockpit/labo) : rendue côté serveur, en lecture seule, sans script.

Avancement du rejeu, chiffres clés, méthode en place et challenger, dernières propositions du labo et
leur état, dernières règles testées, dernier message du matin. Les décisions se prennent dans le
cockpit principal (propositions en attente), sur Discord (boutons, `!oui N` / `!non N`).
"""
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


def render(st: dict, proposals: list[dict], rules: list[dict], morning: dict | None, dm: bool) -> str:
    """HTML complet de la page. `st` : lab_status.status() ; `proposals` : propositions du labo ;
    `rules` : validator.list_rules ; `morning` : dernier point du matin."""
    parts = []
    head = (f'<h1>🧪 Labo de performance</h1><div class="muted small">Lecture seule · '
            f'<a href="/cockpit">← cockpit</a> · message privé du matin : '
            f'{"activé" if dm else "en rodage (JARVIS_LAB_DM=off)"}</div>')
    parts.append(head)
    if not st.get("exists"):
        parts.append('<div class="card">Le labo n\'a pas encore démarré (aucune base).</div>')
        return _page(parts)
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
        n = cmp["challenger"]["declenches"]
        rows = "".join(f'<li>{_e(r["nom"])} : <span class="muted">{_e(r["regle"])}</span></li>' for r in ch["regles"])
        block += (f'<div><b>Challenger : {_e(ch["version"])}</b> <span class="small muted">depuis {_e(ch["depuis"])}</span>'
                  f'<div class="bar"><span style="width:{min(100, 100 * n / max(1, ch["objectif"]))}%"></span></div>'
                  f'<div class="small">{n}/{ch["objectif"]} trades en direct · {_r(cmp["challenger"]["esperance_r"])} '
                  f'contre {_r(cmp["champion"]["esperance_r"])} pour {_e(champ.get("version"))} · potentiel +10 '
                  f'{cmp["challenger"]["capture"]} contre {cmp["champion"]["capture"]}</div><ul class="small">{rows}</ul></div>')
    else:
        block += '<div class="small muted">Pas de challenger en essai.</div>'
    parts.append(f'<div class="card"><h2>Méthode</h2><div class="grid">{block}</div></div>')
    # Propositions
    if proposals:
        trs = "".join(f'<tr><td class="num">{p["id"]}</td><td>{_e(p.get("nom"))}<div class="small muted">{_e(p.get("why"))}</div>'
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
                     f'<th>Gain / trade</th><th>Le</th></tr>{"".join(trs)}</table></div>')
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
