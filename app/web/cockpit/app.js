/* JARVIS · Trading Cockpit. JavaScript sans framework ni étape de build ; contrat d'API et source de chaque valeur :
   docs/CARTE-INTERFACE.md. Règles : rien d'inventé (une valeur absente s'affiche « — », jamais 0), aucun ordre envoyé à
   un courtier, aucune clé manipulée, aucun appel Twelve Data déclenché par la navigation (seules les actions demandées
   au clic, comme « Nouveau plan », en dépensent, et le disent). DOM construit avec el() et textContent : jamais
   innerHTML avec des données ; aucun style en ligne (la CSP ne l'autorise pas), les positions passent par CSSOM. */
(() => {
'use strict';

/* ---------- CSP : Lightweight Charts injecte une balise <style> pour son logo ; on lui rend un gabarit inerte
   (ses règles sont recopiées dans app.css). */
const nativeCreate = document.createElement.bind(document);
document.createElement = function (tag, options) {
  return String(tag).toLowerCase() === 'style' ? nativeCreate('template') : nativeCreate(tag, options);
};

/* ---------- DOM sûr */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const root = document.documentElement;
const put = (node, ...kids) => { node.replaceChildren(); return add(node, kids); };
function add(node, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    node.append(k instanceof Node ? k : String(k));
  }
  return node;
}
function el(tag, props, ...kids) {
  const n = nativeCreate(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k === 'data') Object.assign(n.dataset, v);
      else if (k === 'vars') for (const [vk, vv] of Object.entries(v)) n.style.setProperty(vk, vv);
      else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
      else if (k === 'hidden' || k === 'disabled') n[k] = !!v;
      else n.setAttribute(k, v === true ? '' : String(v));
    }
  }
  return add(n, kids);
}
const SVGNS = 'http://www.w3.org/2000/svg';
function svg(tag, attrs, ...kids) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) n.setAttribute(k, String(v));
  return add(n, kids);
}
const P = (d) => ['path', { d }];
const ICONS = {
  cockpit: [['rect', { x: 3, y: 3, width: 7, height: 9, rx: 2 }], ['rect', { x: 14, y: 3, width: 7, height: 5, rx: 2 }], ['rect', { x: 14, y: 12, width: 7, height: 9, rx: 2 }], ['rect', { x: 3, y: 16, width: 7, height: 5, rx: 2 }]],
  marches: [P('M7 4v16M17 4v16'), ['rect', { x: 4.5, y: 8, width: 5, height: 7, rx: 1 }], ['rect', { x: 14.5, y: 6, width: 5, height: 9, rx: 1 }]],
  positions: [P('M12 3l9 5-9 5-9-5 9-5z'), P('M3 13l9 5 9-5')],
  risque: [P('M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6l8-3z'), P('M9 12l2 2 4-4')],
  labo: [P('M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3'), P('M7.5 15h9')],
  agenda: [['rect', { x: 3, y: 5, width: 18, height: 16, rx: 2 }], P('M3 10h18M8 3v4M16 3v4')],
  journal: [P('M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3V4z'), P('M5 17a3 3 0 0 1 3-3h11')],
  sante: [P('M3 12h4l2-5 4 10 2-5h6')],
  search: [['circle', { cx: 11, cy: 11, r: 7 }], P('M20 20l-3.5-3.5')],
  keys: [['rect', { x: 3, y: 6, width: 18, height: 12, rx: 2 }], P('M7 10h.01M11 10h.01M15 10h.01M7 14h10')],
  more: [['circle', { cx: 5, cy: 12, r: 1.5 }], ['circle', { cx: 12, cy: 12, r: 1.5 }], ['circle', { cx: 19, cy: 12, r: 1.5 }]],
  logo: [P('M4 17l5-6 4 3 7-9'), P('M15 5h5v5')],
};
function icon(name, size = 18, sw = 1.9) {
  const s = svg('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': sw, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' });
  for (const [t, a] of ICONS[name] || []) s.append(svg(t, a));
  return s;
}

/* ---------- formats fr-FR (vrai signe moins, « — » quand c'est inconnu) */
const NFc = new Map();
const nf = (a, b = a) => { const k = a + '-' + b; let f = NFc.get(k); if (!f) { f = new Intl.NumberFormat('fr-FR', { minimumFractionDigits: a, maximumFractionDigits: b }); NFc.set(k, f); } return f; };
const isNum = (x) => typeof x === 'number' && isFinite(x);
const DASH = '—';
const sgn = (x, plus) => (x < 0 ? '−' : plus && x > 0 ? '+' : '');
const fx = (x, d, plus, unit) => (isNum(x) ? sgn(x, plus) + nf(d).format(Math.abs(x)) + unit : DASH);
const usd = (x, plus) => fx(x, 2, plus, ' $');
const eur = (x, plus) => fx(x, 2, plus, ' €');
const eur0 = (x, plus) => fx(x, 0, plus, ' €');
const pct = (x, plus = true, d = 2) => fx(x, d, plus, ' %');
const rr = (x, plus = true) => fx(x, 2, plus, ' R');
const int = (x) => (isNum(x) ? nf(0).format(x) : DASH);
const price = (x) => (!isNum(x) ? DASH : Math.abs(x) >= 1 ? nf(2).format(x) : nf(4, 6).format(x));
const qty = (x) => (!isNum(x) ? DASH : x >= 1 ? nf(0, 2).format(x) : nf(2, 5).format(x));
function dur(m) {
  m = Math.max(0, Math.round(m)); const h = Math.floor(m / 60), mm = m % 60;
  if (h >= 24) { const d = Math.floor(h / 24); return d + ' j ' + (h % 24) + ' h'; }
  return h ? h + ' h ' + String(mm).padStart(2, '0') : mm + ' min';
}
const TZ = 'Europe/Paris';
const DTF = {
  clock: new Intl.DateTimeFormat('fr-FR', { timeZone: TZ, weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' }),
  hm: new Intl.DateTimeFormat('fr-FR', { timeZone: TZ, hour: '2-digit', minute: '2-digit' }),
  dm: new Intl.DateTimeFormat('fr-FR', { timeZone: TZ, day: 'numeric', month: 'short' }),
  day: new Intl.DateTimeFormat('fr-FR', { timeZone: TZ, weekday: 'long', day: 'numeric', month: 'long' }),
  full: new Intl.DateTimeFormat('fr-FR', { timeZone: TZ, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }),
  key: new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' }),
  parts: new Intl.DateTimeFormat('en-GB', { timeZone: TZ, hourCycle: 'h23', hour: '2-digit', minute: '2-digit' }),
};
const toDate = (iso) => { if (!iso) return null; const d = new Date(iso); return isNaN(d) ? null : d; };
function hfr(d) { const [h, m] = DTF.hm.format(d).split(':'); return (+h) + ' h' + (m === '00' ? '' : ' ' + m); }
function hhmm(d) { return DTF.hm.format(d); }
const dayKey = (d) => DTF.key.format(d);
function whenText(iso, prefix = 'vers ') {
  const d = toDate(iso); if (!d) return DASH;
  const k = dayKey(d), today = dayKey(new Date()), tomorrow = dayKey(new Date(Date.now() + 864e5));
  if (k === today) return prefix + hfr(d);
  if (k === tomorrow) return 'demain ' + prefix + hfr(d);
  return 'le ' + DTF.dm.format(d) + ' ' + prefix + hfr(d);
}
function stamp(iso) {
  const d = toDate(iso); if (!d) return DASH;
  return dayKey(d) === dayKey(new Date()) ? hfr(d) : DTF.dm.format(d) + ' ' + hfr(d);
}
function ago(sec) {
  if (!isNum(sec)) return 'âge inconnu';
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return 'il y a ' + sec + ' s';
  return 'il y a ' + dur(sec / 60);
}
function parisMin(d) { const [h, m] = DTF.parts.format(d).split(':').map(Number); return (h % 24) * 60 + m; }
const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const MOD = isMac ? '⌘' : 'Ctrl';
const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

/* ---------- API (délai de 8 s, messages en français, jeton CSRF sur les actions) */
class ApiError extends Error { constructor(msg, status) { super(msg); this.status = status; } }
function csrf() {
  const m = document.cookie.match(/(?:^|;\s*)jarvis_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}
function errorText(status, data) {
  const d = data && data.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d) && d.length) return 'Requête refusée : ' + d.map((x) => (x.loc || []).slice(-1)[0] + ' ' + (x.msg || '')).join(' ; ');
  return { 400: 'Requête invalide', 403: 'Refusé par le serveur', 404: 'Introuvable', 422: 'Données refusées', 500: 'Erreur du serveur JARVIS', 503: 'Source indisponible' }[status] || 'Erreur ' + status;
}
async function api(path, { method = 'GET', body, timeout = 8000 } = {}) {
  const ctl = new AbortController(); const timer = setTimeout(() => ctl.abort(), timeout);
  const headers = { Accept: 'application/json' };
  if (body !== undefined) { headers['Content-Type'] = 'application/json'; headers['X-CSRF-Token'] = csrf(); }
  try {
    const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: ctl.signal, credentials: 'same-origin', cache: 'no-store' });
    let data = null; try { data = await r.json(); } catch (_) { data = null; }
    if (!r.ok) throw new ApiError(errorText(r.status, data), r.status);
    return data;
  } catch (e) {
    if (e instanceof ApiError) throw e;
    if (e && e.name === 'AbortError') throw new ApiError('Le serveur JARVIS ne répond pas (délai de ' + Math.round(timeout / 1000) + ' s dépassé)', 0);
    throw new ApiError('Connexion au serveur JARVIS impossible (réseau ou Tailscale coupé ?)', 0);
  } finally { clearTimeout(timer); }
}
const SLOW = { plan: 90000, take: 90000, reopen: 90000, opinion: 90000, company: 60000, scan_now: 240000, advice: 120000 };
function action(name, args = {}) {
  const body = { action: name, space: apiSpace(), args };
  if (st.space === 'live' && st.unlocked) body.confirm = 'MES_TRADES';
  return api('/api/cockpit/actions', { method: 'POST', body, timeout: SLOW[name] || 20000 });
}

/* ---------- état */
const VIEWS = [
  ['cockpit', 'Cockpit', "Vue d'ensemble", 'c'],
  ['marches', 'Marchés', 'Graphique, watchlist et plan du jour', 'm'],
  ['positions', 'Positions', 'Trades ouverts, ordres IBKR, clôtures, conseils', 'p'],
  ['risque', 'Risque', 'Grade, risque ouvert, arrêt du jour', 'r'],
  ['labo', 'Labo', 'Déclarations, Killmode, challenger, council', 'l'],
  ['agenda', 'Agenda', "Aujourd'hui, heure de Paris", 'a'],
  ['journal', 'Journal', 'Clôtures, désaccords, confirmations', 'j'],
  ['sante', 'Santé', 'Sources, boucles, erreurs', 'h'],
];
const TFS = ['1m', '5m', '15m', '1h', '1j'];
const TF_S = { '1m': 60, '5m': 300, '15m': 900, '1h': 3600, '1j': 86400 };
const st = {
  view: 'cockpit', space: 'paper', unlocked: false, state: null, stateAt: 0, apiOk: true, apiErr: '', sig: '',
  chartSym: null, chartMarket: null, tf: '15m', cursor: -1, drawer: null, instruments: null, instrumentsAt: 0,
  instrument: null, posCharts: null, journal: null, journalAt: 0, prevPos: null, lastPlan: null, lastSizing: null,
  labTab: 'strategies', lab: null, labAt: 0, agenda: null, agendaAt: 0, health: null, healthAt: 0,
};
const apiSpace = () => (st.space === 'live' ? 'trades' : 'paper');
const spaceName = () => (st.space === 'live' ? 'Mes trades' : 'Papier');
try { const v = localStorage.getItem('cockpit.view'); if (v && VIEWS.some((x) => x[0] === v)) st.view = v; } catch (_) { /* stockage indisponible */ }
try { const tf = localStorage.getItem('cockpit.tf'); if (TFS.includes(tf)) st.tf = tf; } catch (_) { /* idem */ }
const fromHash = location.hash.replace('#', ''); if (VIEWS.some((x) => x[0] === fromHash)) st.view = fromHash;

/* ---------- scrutation : une requête à la fois, arrêt quand l'onglet est masqué */
function poller(fn, ms) {
  let timer = 0, busy = false, on = false, again = false;
  const run = async () => {
    clearTimeout(timer);
    if (!on || document.hidden) return;
    if (busy) { again = true; return; } // changement de vue pendant une lecture lente : on repasse dès qu'elle finit
    busy = true;
    try { await fn(); } catch (_) { /* l'erreur est déjà affichée */ } finally { busy = false; if (on) timer = setTimeout(run, again ? 0 : ms()); again = false; }
  };
  return { start() { on = true; run(); }, stop() { on = false; clearTimeout(timer); }, now() { if (on) run(); } };
}
async function fetchState() {
  const space = apiSpace();
  try {
    const s = await api('/api/cockpit/state?space=' + space);
    if (space !== apiSpace()) return;
    st.state = s; st.stateAt = Date.now(); st.apiOk = true; st.apiErr = '';
    onState();
  } catch (e) {
    st.apiOk = false; st.apiErr = e.message; renderBanner(); updatePills();
  }
}
const older = (key, ms) => !st[key] || Date.now() - st[key + 'At'] > ms;
async function loadJournal(ms) {
  const space = apiSpace();
  if (!older('journal', ms) && st.journal.space === space) return;
  try { const d = await api('/api/cockpit/journal?space=' + space + '&limit=100', { timeout: 20000 }); if (space !== apiSpace()) return; st.journal = d; st.journalAt = Date.now(); }
  catch (e) { if (!st.journal) waitFailed('journal', e.message); return; }
  emit('journal');
}
async function fetchHeavy() {
  const space = apiSpace(), view = st.view;
  if (view === 'positions') {
    await Promise.allSettled([api('/api/cockpit/positions/charts?space=' + space + '&tf=15m').then((d) => { if (space === apiSpace()) { st.posCharts = d; updatePosCharts(); } }), loadJournal(60000)]);
  } else if (view === 'marches') {
    await Promise.allSettled([loadInstrument(), loadInstruments()]);
  } else if (view === 'labo') {
    await Promise.allSettled([older('lab', 30000) ? loadInto('lab', '/api/cockpit/lab', 30000) : null, loadText('labo'), loadText(st.labTab)]);
  } else if (view === 'agenda') {
    if (older('agenda', 60000)) await loadInto('agenda', '/api/cockpit/agenda', 30000);
  } else if (view === 'journal') {
    await loadJournal(30000);
  } else if (view === 'sante') {
    await loadInto('health', '/api/cockpit/health');
  } else if (view === 'risque') {
    await Promise.allSettled([loadText('grade'), loadText('frais')]);
  }
}
const statePoll = poller(fetchState, () => 5000);
const heavyPoll = poller(fetchHeavy, () => 10000);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { statePoll.stop(); heavyPoll.stop(); } else { statePoll.start(); heavyPoll.start(); }
});

/* ---------- liaisons en place : la vue n'est reconstruite qu'à la navigation, au changement d'espace ou quand sa
   structure change (un trade de plus ou de moins) ; le reste se met à jour dans les nœuds existants. */
let binders = [], seconds = [];
const bind = (fn) => binders.push(fn);
const everySecond = (fn) => seconds.push(fn);
function runBinders() { for (const f of binders) { try { f(st.state); } catch (e) { console.error(e); } } }
function signature(view) {
  const s = st.state; if (!s) return 'vide';
  const pos = (s.positions_open || []).map((p) => p.id).join(',');
  if (view === 'cockpit') {
    const wait = ((s.portfolio || {}).en_attente || []).map((r) => r.id).join(',');
    const props = (s.proposals_pending || []).map((p) => p.id).join(',');
    const picks = ((s.plans_today || {}).picks || []).map((p) => p.symbol + (p.confirmation || '')).join(',');
    return [st.space, pos, wait, props, picks, (s.agenda_today || []).length].join('|');
  }
  if (view === 'positions' || view === 'marches') return st.space + '|' + pos;
  return st.space;
}
function onState() {
  diffPositions(st.state.positions_open || []);
  updatePills(); renderBanner();
  const sig = signature(st.view);
  if (sig !== st.sig) render(false); else runBinders();
  if (st.drawer) bindDrawer();
}

/* Toasts quand le suivi enregistre un objectif touché, un stop remonté ou une clôture. */
function diffPositions(list) {
  const now = new Map(list.map((p) => [p.id, { kind: p.stop && p.stop.kind, hit: p.targets_hit || 0, sym: p.symbol, targets: p.targets }]));
  const prev = st.prevPos;
  st.prevPos = { space: st.space, map: now };
  if (!prev || prev.space !== st.space) return;
  for (const [id, p] of now) {
    const o = prev.map.get(id); if (!o) continue;
    if (p.hit > o.hit) { const t = p.targets[p.hit - 1]; toast((t ? t.name : 'Objectif') + ' touché · ' + p.sym, 'good'); buzz(); }
    if (o.kind === 'fixed' && p.kind === 'breakeven') toast('Stop au point mort · ' + p.sym, 'mode');
  }
  for (const [id, o] of prev.map) if (!now.has(id)) { toast(o.sym + ' n\'est plus ouvert (voir Positions · clôturés)', 'info'); buzz(); }
}
const buzz = () => { try { if (navigator.vibrate) navigator.vibrate(80); } catch (_) { /* pas de vibreur */ } };

/* ---------- barre du haut : pastilles JARVIS et labo, horloge, bannière */
function updatePills() {
  const s = st.state, pill = $('#jarvis-pill'), txt = $('#jarvis-txt');
  if (!st.apiOk) { pill.className = 'pill hb ba'; txt.textContent = 'JARVIS · API muette'; }
  else if (s) {
    const tr = s.jarvis.suivi, age = isNum(tr.age_s) ? tr.age_s + (Date.now() - st.stateAt) / 1000 : null;
    pill.className = 'pill hb ' + (tr.stale ? 'wa' : 'ok');
    txt.textContent = 'JARVIS · ' + (age == null ? 'suivi jamais passé' : 'suivi ' + ago(age));
    const lab = s.lab, lp = $('#lab-pill');
    const lage = isNum(lab.age_s) ? lab.age_s + (Date.now() - st.stateAt) / 1000 : null;
    lp.className = 'pill hb lab-pill ' + (lab.stale ? 'wa' : 'ok');
    $('#lab-txt').textContent = 'Labo · ' + (lage == null ? 'pas de battement' : (lab.etape ? lab.etape + ' · ' : '') + ago(lage));
    lp.title = lab.raison || 'Battement du worker du labo (périmé au-delà de 30 min)';
  }
}
function renderBanner() {
  const b = $('#banner');
  if (st.apiOk) { b.hidden = true; return; }
  const since = st.stateAt ? ' Dernières données ' + ago((Date.now() - st.stateAt) / 1000) + ' : les P&L latents ne sont plus calculés.' : '';
  b.className = 'banner'; b.hidden = false;
  b.replaceChildren(el('b', { text: 'L\'API JARVIS ne répond plus.' }), el('span', { text: st.apiErr + '.' + since }));
}
function clock() {
  $('#clock').textContent = DTF.clock.format(new Date());
  updatePills();
  if (!st.apiOk) renderBanner();
  for (const f of seconds) { try { f(); } catch (e) { console.error(e); } }
}

/* ---------- petits rendus communs */
function card(cls, eyebrow, meta, ...body) {
  return el('article', { class: 'card ' + cls },
    el('div', { class: 'card-h' }, el('span', { class: 'eyebrow', text: eyebrow }), meta ? el('span', { class: 'card-meta' }, meta) : null), ...body);
}
function empty(title, reasons) {
  return el('div', { class: 'empty' }, el('b', { text: title }), reasons && reasons.length ? el('ul', null, reasons.filter(Boolean).map((r) => el('li', { text: r }))) : null);
}
const kbd = (k) => el('span', { class: 'kbd', text: k });
const chip = (text, cls = '', title) => el('span', { class: 'chip ' + cls, text, title });
function monogram(sym, market) { return el('span', { class: 'mono-chip' + (market === 'crypto' ? ' crypto' : ''), 'aria-hidden': 'true', text: String(sym).slice(0, 4) }); }
const clsTag = (market) => el('small', { text: market === 'crypto' ? 'CRYPTO' : 'ACTION' });
function verdictChip(v) {
  if (!v) return el('span', { class: 'verdict v-none', text: 'SANS NOTE' });
  return el('span', { class: 'verdict ' + ({ GO: 'v-go', 'NO GO': 'v-nogo', KILL: 'v-kill' }[v] || 'v-none'), text: v });
}
function segm(v, max, n = 24) {
  const r = isNum(v) && max > 0 ? Math.max(0, Math.min(1, v / max)) : 0, on = Math.round(r * n), lvl = r >= 1 ? ' b' : r >= 0.75 ? ' w' : '';
  const box = el('div', { class: 'segm' + (n === 20 ? ' s20' : ''), role: 'img', 'aria-label': isNum(v) ? Math.round(r * 100) + ' % de la limite' : 'valeur inconnue' });
  for (let k = 0; k < n; k++) box.append(el('i', { class: k < on ? 'on' + lvl : '' }));
  return box;
}
function setSegm(box, v, max) {
  const n = box.children.length, r = isNum(v) && max > 0 ? Math.max(0, Math.min(1, v / max)) : 0, on = Math.round(r * n), lvl = r >= 1 ? ' b' : r >= 0.75 ? ' w' : '';
  [...box.children].forEach((i, k) => { i.className = k < on ? 'on' + lvl : ''; });
  box.setAttribute('aria-label', isNum(v) ? Math.round(r * 100) + ' % de la limite' : 'valeur inconnue');
}
function tween(node, to, fmt) {
  const from = parseFloat(node.dataset.v);
  node.dataset.v = to;
  if (reduced() || !isNum(from) || from === to) { node.textContent = fmt(to); return; }
  const t0 = performance.now(), D = 450;
  const step = (t) => { const k = Math.min(1, (t - t0) / D), e = 1 - Math.pow(1 - k, 3); node.textContent = fmt(from + (to - from) * e); if (k < 1) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}
function flash(node, up) {
  node.classList.remove('flash-up', 'flash-down'); void node.offsetWidth;
  node.classList.add(up ? 'flash-up' : 'flash-down');
  setTimeout(() => node.classList.remove('flash-up', 'flash-down'), 60);
}
function setPrice(node, v) {
  const t = price(v);
  if (node.textContent !== t && node.textContent && isNum(v)) { const last = parseFloat(node.dataset.last); if (isNum(last) && last !== v) flash(node, v > last); }
  node.textContent = t; if (isNum(v)) node.dataset.last = v;
}
function sparkSvg(vals, w = 120, h = 30) {
  const box = svg('svg', { class: 'mini', viewBox: '0 0 ' + w + ' ' + h, preserveAspectRatio: 'none', 'aria-hidden': 'true' });
  const v = (vals || []).filter(isNum);
  if (v.length < 2) return box;
  const mn = Math.min(...v), mx = Math.max(...v), sp = mx - mn || 1;
  const d = v.map((x, k) => (k ? 'L' : 'M') + ((k / (v.length - 1)) * w).toFixed(1) + ' ' + (h - 2 - ((x - mn) / sp) * (h - 4)).toFixed(1)).join('');
  box.classList.add(v[v.length - 1] >= v[0] ? 'up' : 'down');
  box.append(svg('path', { d }));
  return box;
}

/* Niveaux d'une position (bloc §9.5 du serveur ; l'interface ne recalcule ni SL ni TP). */
const sideSign = (p) => (p.side === 'short' ? -1 : 1);
function riskOf(p) { return p.stop && isNum(p.entry) && isNum(p.stop.initial) ? Math.abs(p.entry - p.stop.initial) : null; }
function exitTarget(p) {
  const ex = (p.targets || []).filter((t) => t.role === 'exit');
  return ex.find((t) => !t.hit_at) || ex[ex.length - 1] || null;
}
const nextTarget = (p) => (p.targets || []).find((t) => !t.hit_at) || null;
function dirPct(p, level) { return isNum(level) && isNum(p.entry) ? sideSign(p) * (level - p.entry) / p.entry * 100 : null; }
function legQty(p, t) {
  if (!isNum(p.qty)) return null;
  if (p.sortie_unique != null) return p.qty;
  const o = (p.ibkr_orders || []).find((x) => x.name === t.name);
  return o ? o.quantity : p.qty / Math.max(1, (p.targets || []).length);
}
function eurAt(p, level, q) {
  if (!isNum(q) || !isNum(p.eurusd) || !isNum(level) || !isNum(p.entry)) return null;
  let v = q * sideSign(p) * (level - p.entry);
  if (p.fees_est && isNum(p.fees_est.usd)) v -= p.fees_est.usd * (q / p.qty);
  return v / p.eurusd;
}
function rAt(p, level) { const r = riskOf(p); return r && isNum(level) ? sideSign(p) * (level - p.entry) / r : null; }
function rrRatio(p) {
  const t = exitTarget(p), r = riskOf(p);
  if (!t || !r || !p.stop || p.stop.kind === 'breakeven') return null;
  return Math.abs(t.price - p.entry) / r;
}
function trackVars(p) {
  const t = exitTarget(p), s = sideSign(p);
  if (!t || !p.stop || !isNum(p.stop.price)) return null;
  const pts = [p.stop.price, t.price, p.entry].concat(isNum(p.last_price) ? [p.last_price] : []).map((v) => s * v);
  const lo = Math.min(...pts), hi = Math.max(...pts), span = hi - lo || 1;
  const at = (v) => ((s * v - lo) / span * 100).toFixed(1) + '%';
  return { '--e': at(p.entry), '--n': isNum(p.last_price) ? at(p.last_price) : at(p.entry) };
}
function nextChip(p) {
  const t = nextTarget(p);
  if (!t) return chip('tous les objectifs touchés', 'up');
  if (t.late) return chip(t.name + ' en retard · attendu ' + whenText(t.eta_median), 'warn');
  if (!t.eta_median) return chip(t.name + ' · délai non estimé', 'ghost');
  return chip(t.name + ' · ' + whenText(t.eta_median), t.role === 'exit' ? 'up' : 'info');
}

/* ---------- vues */
const BUILD = {}, AFTER = {};
function loadingCard() { return card('c-12', 'Chargement', null, el('div', { class: 'skeleton' })); }

function render(animate) {
  const sec = $('#v-' + st.view);
  destroyCharts('view');
  binders = []; seconds = []; heavyBinders = [];
  const nodes = st.state ? BUILD[st.view]() : [loadingCard()];
  sec.replaceChildren(...nodes);
  st.sig = signature(st.view); st.cursor = -1;
  if (st.state) runBinders();
  if (animate && !reduced()) { sec.classList.remove('enter'); void sec.offsetWidth; sec.classList.add('enter'); }
  if (st.state && AFTER[st.view]) AFTER[st.view]();
}

/* --- Cockpit */
BUILD.cockpit = () => {
  const s = st.state, out = [heroCard(), sessionsCard(), openCard(), riskCard(), plansCard(), agendaCard(6)];
  if ((s.proposals_pending || []).length) out.push(proposalsCard());
  return out;
};

function heroCard() {
  const live = st.space === 'live', p0 = st.state.portfolio;
  const megaV = el('span', { 'data-mega': '' }), cur = el('span', { class: 'cur' }), mega = el('div', { class: 'mega' }, megaV, cur);
  const delta = el('span', { class: 'delta' }), subTxt = el('span');
  const curve = el('div'), stats = el('div', { class: 'stats' });
  const statCells = [0, 1, 2, 3].map(() => { const k = el('div', { class: 'k' }), v = el('div', { class: 'v' }), s = el('div', { class: 's' }); stats.append(el('div', { class: 'stat' }, k, v, s)); return { k, v, s }; });
  const meta = live
    ? (isNum(p0.capital.eur) ? 'capital ' + eur0(p0.capital.eur) + ' · déclaré' : 'capital non déclaré (JARVIS_CAPITAL)')
    : 'si j\'avais suivi chaque recommandation depuis le ' + (toDate(p0.si_tout_suivi.depuis) ? DTF.dm.format(toDate(p0.si_tout_suivi.depuis)) : DASH);
  bind((s) => {
    const p = s.portfolio, j = p.jour, w = p.semaine;
    if (live) {
      const n = p.positions_ouvertes, L = p.latent;
      let v = null, unit = '€';
      if (n === 0) v = 0; else if (isNum(L.eur)) v = L.eur; else if (isNum(L.r)) { v = L.r; unit = 'R'; }
      mega.classList.toggle('unknown', v == null);
      if (v == null) { megaV.textContent = DASH; delete megaV.dataset.v; } else tween(megaV, v, (x) => sgn(x, true) + nf(2).format(Math.abs(x)));
      cur.textContent = unit;
      delta.className = 'delta ' + (isNum(L.r) ? (L.r >= 0 ? 'up' : 'down') : 'flat');
      delta.textContent = isNum(L.r) ? rr(L.r) + ' latent' : n ? 'latent inconnu' : 'aucun trade ouvert';
      subTxt.textContent = n ? n + ' trade' + (n > 1 ? 's' : '') + ' ouvert' + (n > 1 ? 's' : '') + (L.inconnus ? ' · ' + L.inconnus + ' sans cotation fraîche' : '') : 'P&L latent de tes trades ouverts';
      setStat(statCells[0], 'Trades ouverts', int(n), L.inconnus ? L.inconnus + ' sans cotation fraîche' : 'déclarés à JARVIS');
      setStat(statCells[1], 'Réalisé aujourd\'hui', j.n ? rr(j.r_net) : rr(0), j.n ? eur(j.eur, true) + ' nets' + (j.eur_inconnus ? ' · ' + j.eur_inconnus + ' sans taille' : '') : 'aucune clôture', j.r_net);
      setStat(statCells[2], 'Cette semaine', w.n ? rr(w.r_net) : rr(0), w.n ? eur(w.eur, true) + ' nets · ' + w.n + ' clôture' + (w.n > 1 ? 's' : '') : 'aucune clôture', w.r_net);
      setStat(statCells[3], 'Frais IBKR estimés (jour)', j.n ? eur(j.fees_eur) : eur(0), 'déduits des R nets');
    } else {
      const b = p.si_tout_suivi, v = isNum(b.equite_eur) ? b.equite_eur : isNum(b.eur_net) ? b.eur_net : b.closes ? null : 0;
      mega.classList.toggle('unknown', v == null);
      if (v == null) { megaV.textContent = DASH; delete megaV.dataset.v; } else tween(megaV, v, (x) => (isNum(b.equite_eur) ? '' : sgn(x, true)) + nf(2).format(Math.abs(x)));
      cur.textContent = '€';
      delta.className = 'delta ' + (isNum(b.r_net) && b.closes ? (b.r_net >= 0 ? 'up' : 'down') : 'flat');
      delta.textContent = b.closes ? rr(b.r_net) + ' nets · ' + b.closes + ' close' + (b.closes > 1 ? 's' : '') : 'aucune recommandation close';
      subTxt.textContent = isNum(b.equite_eur) ? 'capital déclaré ' + eur0(b.capital_declare_eur) + ' + résultats nets de frais' : 'résultats nets de frais (capital non déclaré)' + (b.eur_sans_taille ? ' · ' + b.eur_sans_taille + ' sans taille' : '');
      const d = p.recos_du_jour;
      setStat(statCells[0], 'Recommandations du jour', int((d.ouvert || 0) + (d.clos || 0)) + ' déclenchée' + ((d.ouvert || 0) + (d.clos || 0) > 1 ? 's' : ''), (d.attente || 0) + ' en attente · ' + (d.pas_declenche || 0) + ' pas déclenchée' + ((d.pas_declenche || 0) > 1 ? 's' : '') + ' · ' + (p.go_en_attente_de_confirmation || 0) + ' GO à confirmer');
      setStat(statCells[1], 'R nets aujourd\'hui', j.n ? rr(j.r_net) : rr(0), j.n ? eur(j.eur, true) : 'aucune clôture', j.r_net);
      setStat(statCells[2], 'Cette semaine', w.n ? rr(w.r_net) : rr(0), w.n ? eur(w.eur, true) : 'aucune clôture', w.r_net);
      setStat(statCells[3], 'Taux de réussite', isNum(b.taux_reussite) ? pct(b.taux_reussite * 100, false, 0) : DASH, b.closes ? b.gagnantes + ' gagnante' + (b.gagnantes > 1 ? 's' : '') + ' sur ' + b.closes : 'aucune close');
    }
    curve.replaceChildren(curveSvg(p.courbe));
  });
  const eyebrow = live ? 'P&L · Mes trades' : 'Si j\'avais tout suivi · Papier';
  return card('c-8 hero', eyebrow, meta,
    el('div', { class: 'hero-row' }, el('div', null, mega, el('div', { class: 'hero-sub' }, delta, subTxt)), curve), stats);
}
function setStat(c, k, v, s, signValue) {
  c.k.textContent = k; c.v.textContent = v; c.s.textContent = s || '';
  c.v.className = 'v' + (isNum(signValue) && signValue !== 0 ? (signValue > 0 ? ' up' : ' down') : '');
}
function curveSvg(c) {
  const pts = (c && c.points) || [];
  if (!pts.length) {
    return el('p', { class: 'muted small m-0', text: st.space === 'live' ? 'Aucune clôture aujourd\'hui : la courbe démarre à la première sortie.' : 'Aucune recommandation close depuis la mise en service : la courbe démarre à la première.' });
  }
  const W = 600, H = 96, vals = [0].concat(pts.map((x) => x[1]));
  const mn = Math.min(...vals), mx = Math.max(...vals), sp = mx - mn || 1;
  const y = (v) => (H - 6 - ((v - mn) / sp) * (H - 14)).toFixed(1);
  const xs = vals.map((_, k) => ((k / Math.max(1, vals.length - 1)) * W).toFixed(1));
  const line = vals.map((v, k) => (k ? 'L' : 'M') + xs[k] + ' ' + y(v)).join('');
  const box = svg('svg', { class: 'spark', viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none', role: 'img', 'aria-label': c.titre + ' : ' + (c.unite === '€' ? eur(vals[vals.length - 1], true) : rr(vals[vals.length - 1])) });
  box.append(svg('line', { class: 'spark-zero', x1: 0, x2: W, y1: y(0), y2: y(0) }));
  box.append(svg('path', { class: 'spark-area', d: line + 'L' + W + ' ' + H + 'L0 ' + H + 'Z' }));
  box.append(svg('path', { class: 'spark-line', d: line }));
  box.append(svg('circle', { class: 'spark-dot', cx: xs[xs.length - 1], cy: y(vals[vals.length - 1]), r: 4 }));
  box.append(svg('title', null, c.titre));
  return box;
}

/* Cadran de séance : anneau 24 h en heure de Paris ; arcs calculés par le serveur depuis l'heure de New York. */
const ETATS = { premarche: 'Pré-marché', seance: 'Séance', apres_bourse: 'Après-bourse', ferme: 'Fermé' };
function sessionNext(n) {
  const left = (iso) => { const d = toDate(iso); return d ? dur(Math.max(0, (d - Date.now()) / 60000)) : DASH; };
  if (n.etat === 'seance') return 'clôture dans ' + left(n.prochain_changement);
  if (n.etat === 'apres_bourse') return 'fin dans ' + left(n.prochain_changement);
  return 'ouverture dans ' + left(n.prochaine_ouverture || n.prochain_changement);
}
function dialSvg(ms) {
  const n = ms.nyse, C = 110, Rr = 80, Rc = 97;
  const ang = (m) => (((m % 1440) + 1440) % 1440) / 1440 * Math.PI * 2 - Math.PI / 2;
  const pt = (m, r) => [C + r * Math.cos(ang(m)), C + r * Math.sin(ang(m))];
  const arc = (a, b, r) => { const [x1, y1] = pt(a, r), [x2, y2] = pt(b, r), large = ((b - a + 1440) % 1440) > 720 ? 1 : 0; return 'M' + x1.toFixed(2) + ' ' + y1.toFixed(2) + 'A' + r + ' ' + r + ' 0 ' + large + ' 1 ' + x2.toFixed(2) + ' ' + y2.toFixed(2); };
  let ph = n.aujourd_hui, dim = false;
  if (!ph && n.prochaine_ouverture) {
    const o = toDate(n.prochaine_ouverture).getTime();
    ph = { premarche: new Date(o - 330 * 60000), ouverture: new Date(o), cloture: new Date(o + 390 * 60000), fin_apres_bourse: new Date(o + 630 * 60000) };
    dim = true;
  }
  const g = svg('svg', { class: 'dial', viewBox: '0 0 220 220', role: 'img' });
  g.append(svg('circle', { class: 'track', cx: C, cy: C, r: Rr }));
  g.append(svg('circle', { class: 'crypto', cx: C, cy: C, r: Rc }));
  if (ph) {
    const m = (x) => parisMin(x instanceof Date ? x : toDate(x));
    for (const [cls, a, b] of [['pre', ph.premarche, ph.ouverture], ['reg', ph.ouverture, ph.cloture], ['aft', ph.cloture, ph.fin_apres_bourse]]) {
      g.append(svg('path', { class: 'arc ' + cls + (dim ? ' dim' : ''), d: arc(m(a), m(b), Rr) }));
    }
  }
  for (const h of [0, 12]) { const [x, y] = pt(h * 60, 56); g.append(svg('text', { class: 'hour', x: x.toFixed(1), y: (y + 4).toFixed(1), 'text-anchor': 'middle' }, h + ' h')); }
  for (let h = 0; h < 24; h++) { const [x1, y1] = pt(h * 60, 68), [x2, y2] = pt(h * 60, h % 6 ? 70 : 72); g.append(svg('line', { class: 'tick', x1: x1.toFixed(1), y1: y1.toFixed(1), x2: x2.toFixed(1), y2: y2.toFixed(1) })); }
  const nowDot = svg('circle', { class: 'now', r: 7 });
  const stateT = svg('text', { class: 'state', x: C, y: C - 4, 'text-anchor': 'middle' }, ETATS[n.etat] || n.etat);
  const nextT = svg('text', { class: 'next', x: C, y: C + 16, 'text-anchor': 'middle' }, sessionNext(n));
  g.append(nowDot, stateT, nextT);
  const tickNow = () => {
    const [x, y] = pt(parisMin(new Date()), Rr); nowDot.setAttribute('cx', x.toFixed(1)); nowDot.setAttribute('cy', y.toFixed(1));
    nextT.textContent = sessionNext(n);
    g.setAttribute('aria-label', 'NYSE : ' + (ETATS[n.etat] || n.etat) + ', ' + sessionNext(n) + '. Crypto ouverte 24 h sur 24.');
  };
  tickNow();
  return { node: g, tick: tickNow };
}
function sessionsCard() {
  const wrap = el('div', { class: 'dial-wrap' }), note = el('div', { class: 'dial-note' });
  let dial = null;
  bind((s) => {
    dial = dialSvg(s.market_sessions);
    wrap.replaceChildren(dial.node,
      el('div', { class: 'dial-legend' }, ['pre:Pré-marché', 'reg:Séance', 'aft:Après-bourse', 'cry:Crypto 24/7'].map((x) => { const [c, t] = x.split(':'); return el('span', null, el('i', { class: 'lg-' + c }), t); })),
      note);
    const n = s.market_sessions.nyse, bits = [];
    if (n.raison_fermeture) bits.push('Fermé aujourd\'hui : ' + n.raison_fermeture + '.');
    if (n.demi_seance && n.aujourd_hui) bits.push('Demi-séance : clôture à ' + hfr(toDate(n.aujourd_hui.cloture)) + ' à Paris.');
    if (n.decalage_inhabituel) {
      const o = n.aujourd_hui ? n.aujourd_hui.ouverture : n.prochaine_ouverture;
      bits.push('Semaine de décalage horaire : écart Paris–New York de ' + nf(0, 1).format(n.ecart_paris_new_york_h) + ' h' + (o ? ', ouverture à ' + hfr(toDate(o)) + ' à Paris.' : '.'));
    }
    note.textContent = bits.join(' ');
  });
  everySecond(() => { if (dial) dial.tick(); });
  return card('c-4', 'Séances', 'heure de Paris', wrap);
}

function posRow(p, idx, opts = {}) {
  const live = st.space === 'live';
  const tv = trackVars(p), et = exitTarget(p);
  const track = el('span', { class: 'track' + (p.side === 'short' ? ' short' : '') + (p.stop && p.stop.kind === 'breakeven' ? ' safe' : ''), vars: tv || {} }, el('span', { class: 'e' }), el('span', { class: 'n' + (isNum(p.last_price) ? '' : ' unknown') }));
  const nowL = el('span', { class: 'now' });
  const next = el('span', { class: 'p-next' });
  const pnl = el('span', { class: 'pnl' }), pnlR = el('span'), pnlE = el('small');
  pnl.append(pnlR, pnlE);
  const held = el('span');
  const origin = live ? 'pris ' + whenText(p.created_at, 'à ') : (p.verdict === 'KILL' ? 'KILL' : 'GO confirmé') + ' · déclenché ' + whenText(p.created_at, 'à ');
  const ident = el('span', { class: 'p-id' }, monogram(p.symbol, p.market),
    el('span', { class: 'blk' },
      el('span', { class: 'sym' }, p.symbol, clsTag(p.market), p.side === 'short' ? chip('SHORT', 'down') : null, verdictChip(p.verdict), p.against_jarvis ? chip('contre l\'avis de JARVIS', 'against') : null),
      el('span', { class: 'nm', text: origin })));
  const qtyB = el('span', { class: 'p-qty blk' }, el('span', { class: 'num', text: isNum(p.qty) ? qty(p.qty) : 'taille ?' }), el('span', { class: 'nm', text: 'à ' + price(p.entry) }));
  const trackB = el('span', { class: 'p-track' },
    tv ? track : chip(p.stop ? 'objectif inconnu' : 'SL inconnu', 'ghost'),
    el('span', { class: 'track-l' }, el('span', { text: p.stop ? 'SL ' + price(p.stop.price) : 'SL inconnu' }), nowL, el('span', { text: et ? et.name + ' ' + price(et.price) : 'objectif ?' })),
    next);
  const row = el(opts.static ? 'div' : 'button', { class: 'prow', type: opts.static ? null : 'button', data: { sym: p.symbol, market: p.market, navItem: idx, pos: p.id } },
    ident, qtyB, trackB, pnl, el('span', { class: 'p-act blk' }, 'depuis', el('br'), held));
  bind((s) => {
    const q = (s.positions_open || []).find((x) => x.id === p.id); if (!q) return;
    const v = trackVars(q); if (v) for (const [k, val] of Object.entries(v)) track.style.setProperty(k, val);
    track.classList.toggle('safe', !!(q.stop && q.stop.kind === 'breakeven'));
    nowL.textContent = isNum(q.last_price) ? price(q.last_price) + (q.freshness.stale ? ' ⚠' : '') : DASH;
    nowL.className = 'now' + (q.freshness.stale ? ' stale' : '');
    nowL.title = q.freshness.stale ? 'cotation périmée (' + ago(q.freshness.age_s) + ') : P&L latent non calculé' : (q.price_source || '') + ' · ' + ago(q.freshness.age_s);
    const u = q.unrealized;
    pnl.className = 'pnl ' + (u && isNum(u.r) ? (u.r >= 0 ? 'up' : 'down') : 'unknown');
    pnlR.textContent = u ? rr(u.r) : DASH;
    pnlE.textContent = u ? (isNum(u.eur) ? eur(u.eur, true) : 'taille inconnue') : q.freshness.stale ? 'cotation périmée' : 'inconnu';
    const chips = [nextChip(q)];
    if (q.stop && q.stop.kind === 'breakeven') chips.push(chip('stop au point mort', 'mode'));
    for (const sgnl of (q.exit_signals || []).slice(-2)) chips.push(chip(typeof sgnl === 'string' ? sgnl : (sgnl.kind || 'signe de sortie'), 'warn'));
    next.replaceChildren(...chips);
  });
  everySecond(() => { const d = toDate(p.created_at); held.textContent = d ? dur((Date.now() - d) / 60000) : DASH; });
  return row;
}
function openCard() {
  const s = st.state, pos = s.positions_open || [], live = st.space === 'live';
  const body = [];
  if (pos.length) body.push(el('div', { class: 'ptable' }, pos.map((p, k) => posRow(p, k))));
  else if (live) body.push(empty('Aucun trade ouvert dans Mes trades', ['Un trade apparaît ici quand tu le déclares : « Je prends ce trade » (touche +), `!prends` sur Discord, ou un GO confirmé pris.']));
  else body.push(empty('Aucune recommandation déclenchée en cours', ['Les GO confirmés et les KILL envoyés sur Discord apparaissent ici dès que leur ordre se déclenche.', s.market_sessions.nyse.etat !== 'seance' ? 'Séance NYSE fermée : ' + sessionNext(s.market_sessions.nyse) + '.' : null]));
  const waiting = live ? [] : (s.portfolio.en_attente || []);
  if (waiting.length) {
    body.push(el('div', { class: 'mt-12 small soft', text: 'En attente du déclenchement :' }),
      el('div', { class: 'feed' }, waiting.map((w) => el('div', null, el('span', { class: 'mono', text: w.symbol }), el('span', { class: 'mono', text: price(w.entry) }), chip(w.type_fr, w.type_fr === 'KILL' ? 'warn' : 'up'), el('span', { class: 'soft right', text: 'stop ' + price(w.stop) + ' · valable jusqu\'à ' + (toDate(w.valid_until) ? hfr(toDate(w.valid_until)) : DASH) + (isNum(w.qty) ? ' · ' + w.qty + ' actions' : ' · sans taille') })))));
  }
  const meta = el('span', { class: 'card-meta' }, pos.length + ' ouvert' + (pos.length > 1 ? 's' : ''), el('button', { class: 'btn sm', type: 'button', data: { go: 'positions' } }, 'Tout voir ', el('span', { class: 'kh' }, kbd('G'), ' ', kbd('P'))));
  return card('c-8', live ? 'Trades ouverts · Mes trades' : 'Recommandations en cours · Papier', meta, ...body);
}

function riskCard() {
  const openM = segm(0, 1), dayM = segm(0, 1);
  const openV = el('span', { class: 'num' }), dayV = el('span', { class: 'num' }), dayN = el('div', { class: 'meter-n' }), openN = el('div', { class: 'meter-n' });
  const grades = el('div', { class: 'grades' });
  bind((s) => {
    const r = s.risk;
    openV.replaceChildren(pct(r.risque_ouvert_pct, false), el('span', { class: 'muted', text: ' / ' + pct(r.risque_ouvert_max_pct, false, 0) }));
    setSegm(openM, r.risque_ouvert_pct, r.risque_ouvert_max_pct);
    openN.textContent = 'risque au stop des trades ouverts, en % du capital déclaré';
    const day = r.r_du_jour_net;
    dayV.replaceChildren(rr(day), el('span', { class: 'muted', text: ' · arrêt à ' + rr(r.arret_jour_r) }));
    setSegm(dayM, isNum(day) ? Math.max(0, -day) : null, Math.abs(r.arret_jour_r));
    dayN.textContent = 'R nets du jour sur les trades pris ; à ' + rr(r.arret_jour_r) + ', JARVIS ne propose plus de taille';
    grades.replaceChildren(...['stock', 'crypto'].map((m) => {
      const g = r.grades[m] || {};
      return el('div', { class: 'grade-row' }, el('span', { class: 'soft', text: m === 'stock' ? 'Actions' : 'Crypto' }), el('span', { class: 'letter' + (String(g.grade || '?').length > 2 ? ' long' : ''), text: g.grade || '?' }),
        el('span', { class: 'why', text: g.raison || '', title: g.raison || '' }), el('span', { class: 'num', text: pct(g.risque_propose, false) + (isNum(g.risque_propose_eur) ? ' · ' + eur0(g.risque_propose_eur) : '') }));
    }));
  });
  return card('c-4', 'Risque', 'règles de Mes trades', el('div', { class: 'meters' },
    el('div', null, el('div', { class: 'meter-h' }, el('span', { text: 'Risque ouvert' }), openV), openM, openN),
    el('div', null, el('div', { class: 'meter-h' }, el('span', { text: 'P&L du jour' }), dayV), dayM, dayN),
    el('div', null, el('div', { class: 'meter-h' }, el('span', { text: 'Grade et risque proposé' }), el('span', { class: 'muted small', text: 'par marché' })), grades)));
}

const CONF = { attente: ['confirmation attendue', 'info'], repli: ['repli sous la VWAP, en attente', 'warn'], 'confirmé': ['confirmé', 'up'], 'annulé': ['annulé', 'down'], 'expiré': ['expiré', ''] };
function plansCard() {
  const pt = st.state.plans_today, picks = pt.picks || [];
  let body;
  if (!pt.du_jour) body = empty('Pas encore de scan aujourd\'hui', [pt.scan_at ? 'Dernier scan : ' + stamp(pt.scan_at) + '.' : 'Aucun scan enregistré.', 'Le scanner pré-ouverture passe chaque jour de Bourse (vue Agenda).']);
  else if (!picks.length) body = empty('Le scanner n\'a retenu aucun plan aujourd\'hui', ['Scan de ' + stamp(pt.scan_at) + '.']);
  else {
    body = el('div', { class: 'sig-list' }, picks.map((p) => {
      const c = p.confirmation ? CONF[p.confirmation] || [p.confirmation, ''] : null;
      return el('div', { class: 'sig' },
        el('div', { class: 'sig-top' }, verdictChip(p.verdict), el('button', { class: 'btn sm', type: 'button', data: { sym: p.symbol, market: 'stock' } }, p.symbol),
          c ? chip(c[0] + (p.confirmation === 'confirmé' && isNum(p.declencheur) ? ' · ordre à ' + price(p.declencheur) : ''), c[1]) : null,
          el('span', { class: 'score' }, el('b', { text: isNum(p.composite) ? nf(1).format(p.composite) : DASH }), el('span', { text: 'priorité, pas une probabilité' }))),
        el('div', { class: 'small soft mono', text: 'entrée ' + price(p.entry) + ' · stop ' + price(p.stop) + ' · objectifs ' + (p.targets || []).map((t) => price(isNum(t) ? t : t.price)).join(' · ') }),
        (p.raisons || []).length ? el('ul', { class: 'conds' }, p.raisons.slice(0, 3).map((r) => el('li', null, el('span', { class: 'ic ' + (p.verdict === 'GO' ? 'up' : 'down'), text: p.verdict === 'GO' ? '✓' : '✗' }), el('span', { text: r })))) : null);
    }));
  }
  return card('c-6', 'Plans du jour', pt.scan_at ? 'scanner · ' + stamp(pt.scan_at) : 'scanner pré-ouverture', body);
}

function agendaList(items, limit) {
  const now = Date.now();
  let list = items;
  if (limit) list = items.filter((a) => toDate(a.at) >= now - 30 * 60000).slice(0, limit);
  if (!list.length) return empty(items.length ? 'Plus rien de prévu aujourd\'hui' : 'Rien de prévu par JARVIS aujourd\'hui', [items.length ? null : 'Pas de séance NYSE aujourd\'hui (week-end ou jour férié).']);
  const ol = el('ol', { class: 'tl' });
  let placed = false;
  const nowLine = () => el('li', { class: 'now-line', 'aria-label': 'Maintenant' }, el('span'), el('em', { text: hhmm(new Date()) }));
  for (const a of list) {
    const d = toDate(a.at);
    if (!placed && d > now) { ol.append(nowLine()); placed = true; }
    const imp = a.importance === 'haute' ? 3 : 1;
    ol.append(el('li', { class: (d <= now ? 'past' : '') + (imp === 3 ? ' hot' : '') },
      el('span', { class: 'time', text: hhmm(d) }),
      el('span', null, el('span', { class: 'ttl', text: a.titre }), el('span', { class: 'meta', text: (CAT[a.categorie] || a.categorie) + ' · source : ' + a.source + (a.detail ? ' · ' + a.detail : '') })),
      el('span', { class: 'imp', 'aria-label': 'importance ' + imp + ' sur 3' }, [1, 2, 3].map((k) => el('i', { class: k <= imp ? 'on' : '' })))));
  }
  if (!placed) ol.append(nowLine());
  return ol;
}
function agendaCard(limit) {
  const box = el('div');
  bind((s) => box.replaceChildren(agendaList(s.agenda_today || [], limit)));
  return card('c-6', 'À venir aujourd\'hui', DTF.day.format(new Date()), box);
}

function proposalText(p) {
  if (p.kind === 'grade') return 'Grade : ' + p.key + ' ' + (p.current ?? DASH) + ' → ' + (p.value ?? DASH);
  if (p.kind === 'labo') return 'Labo : « ' + (p.nom || p.key) + ' » en essai (challenger)';
  if (p.kind === 'promotion') return 'Méthode : passer à ' + (p.value ?? DASH);
  return (p.key || 'Réglage') + ' : ' + (p.current ?? DASH) + ' → ' + (p.value ?? DASH);
}
function proposalsCard() {
  const items = st.state.proposals_pending;
  return card('c-12', 'Propositions en attente', items.length + ' à décider', el('div', { class: 'props' }, items.map((p) => el('div', { class: 'prop' },
    el('div', null, el('div', { text: '#' + p.id + ' · ' + proposalText(p) }), p.why ? el('div', { class: 'why', text: Array.isArray(p.why) ? p.why.join(' ; ') : p.why }) : null),
    el('div', { class: 'row-gap' },
      el('button', { class: 'btn sm', type: 'button', onclick: () => refuseProposal(p) }, 'Refuser'),
      el('button', { class: 'btn sm primary', type: 'button', onclick: () => approveProposal(p) }, 'Approuver', el('span', { class: 'lock', text: '●', title: 'appui maintenu' })))))));
}

/* --- Positions */
BUILD.positions = () => {
  const s = st.state, pos = s.positions_open || [], live = st.space === 'live';
  const out = [];
  const grid = el('div', { class: 'pgrid', id: 'pgrid' });
  if (pos.length) {
    for (const p of pos) {
      const head = el('button', { class: 'pcard-h', type: 'button', data: { chart: p.symbol, market: p.market } }, monogram(p.symbol, p.market),
        el('span', { class: 'blk' }, el('span', { class: 'sym' }, p.symbol, clsTag(p.market), verdictChip(p.verdict)), el('span', { class: 'nm', text: (isNum(p.qty) ? qty(p.qty) : 'taille ?') + ' à ' + price(p.entry) })));
      const pnl = el('span', { class: 'pnl' }); head.append(pnl);
      const foot = el('div', { class: 'pcard-f' });
      bind((s2) => {
        const q = (s2.positions_open || []).find((x) => x.id === p.id); if (!q) return;
        const u = q.unrealized;
        pnl.className = 'pnl ' + (u && isNum(u.r) ? (u.r >= 0 ? 'up' : 'down') : 'unknown');
        pnl.replaceChildren(u ? rr(u.r) : DASH, el('small', { text: u ? (isNum(u.eur) ? eur(u.eur, true) : 'taille inconnue') : 'cotation périmée' }));
        const et = exitTarget(q), ratio = rrRatio(q), nt = nextTarget(q);
        foot.replaceChildren(
          el('span', { class: 'down', text: q.stop ? (q.stop.kind === 'breakeven' ? 'SL ' + price(q.stop.price) + ' · point mort' : 'SL ' + price(q.stop.price) + ' (' + pct(dirPct(q, q.stop.price)) + ')') : 'SL inconnu' }),
          el('span', { class: 'soft', text: ratio != null ? 'R:R ' + nf(1).format(ratio) : q.stop && q.stop.kind === 'breakeven' ? 'sans risque' : 'R:R ?' }),
          el('span', { class: 'up', text: et ? et.name + ' ' + price(et.price) + ' (' + pct(dirPct(q, et.price)) + ')' + (nt && nt.eta_median ? ' ' + whenText(nt.eta_median) : '') : 'objectif ?' }));
      });
      grid.append(el('div', { class: 'pcard' }, head, el('div', { class: 'pchart', id: 'pc-' + p.id }, el('div', { class: 'chart-empty', text: 'Bougies 15 min…' })), foot));
    }
  }
  out.push(card('c-12', 'Trades sur le graphique · ' + spaceName(), 'bougies 15 min · zones risque / gain depuis l\'entrée · clic = graphique complet',
    pos.length ? grid : empty('Aucun trade ouvert dans l\'espace ' + spaceName(), [live ? 'Déclare un trade avec « Nouveau plan » (touche +) puis « Je prends ce trade ».' : 'Les recommandations déclenchées apparaissent ici avec leurs niveaux.'])));
  out.push(card('c-12', 'Trades ouverts · ' + spaceName(), el('span', { class: 'kh' }, 'P&L latent sur cotations fraîches · ', kbd('J'), ' ', kbd('K'), ' · ', kbd('Entrée')),
    pos.length ? el('div', null, el('div', { class: 'phead', 'aria-hidden': 'true' }, el('span', { text: 'Instrument' }), el('span', { class: 'p-qty', text: 'Quantité' }), el('span', { text: 'Stop → objectif' }), el('span', { text: 'P&L latent' }), el('span')),
      el('div', { class: 'ptable' }, pos.map((p, k) => el('div', null, posRow(p, k), live ? tradeActions(p) : null)))) : empty('Rien d\'ouvert', [])));
  out.push(ordersCard(pos));
  out.push(closedCard());
  out.push(adviceCard(pos));
  return out;
};
function tradeActions(p) {
  return el('div', { class: 'trade-actions' },
    el('button', { class: 'btn sm', type: 'button', onclick: () => closeForm(p) }, 'Clôturer…', el('span', { class: 'lock', text: '●', title: 'appui maintenu' })),
    el('button', { class: 'btn sm', type: 'button', onclick: () => exitForm(p.symbol, p.market) }, 'J\'ai vendu à…', el('span', { class: 'lock', text: '●' })),
    el('button', { class: 'btn sm danger', type: 'button', onclick: () => deleteTrade(p) }, 'Supprimer…'));
}
function ordersCard(pos) {
  const live = st.space === 'live';
  const items = pos.filter((p) => (p.ibkr_orders || []).length);
  const body = items.length ? el('div', { class: 'orders' }, items.map((p) => el('div', { class: 'order' },
    el('div', { class: 'h' }, el('span', { text: p.symbol + (p.sortie_unique != null ? ' · sortie unique' : ' · un bracket par objectif') }), el('span', { class: 'muted small', text: p.fees_est ? 'commissions ≈ ' + usd(p.fees_est.usd) + ' (' + rr(p.fees_est.r, false) + ', ' + (p.fees_est.bareme || 'IBKR') + ')' : live ? 'commissions inconnues' : 'commissions déduites au résultat' })),
    p.ibkr_orders.map((o, k) => el('div', { text: 'Bracket ' + (k + 1) + ' : ' + o.quantity + ' @ entrée ' + price(o.entry) + ' · SL ' + price(o.stop) + ' · ' + o.name + ' ' + price(o.take_profit) }))))) :
    empty('Aucun ordre en prix à montrer', [live ? 'Les ordres bracket viennent du plan enregistré (taille connue seulement).' : 'Une recommandation sans taille dans son message n\'a pas d\'ordre en quantité.']);
  return card('c-6', 'Ordres IBKR en prix', live ? 'posés par toi chez IBKR · JARVIS suit sur les bougies' : 'tels que le message les donnait', body);
}
let journalBox = null;
function closedCard() {
  journalBox = el('div');
  rebindJournal(); onData('journal', rebindJournal);
  return card('c-6', 'Clôturés récemment · ' + spaceName(), st.space === 'live' ? 'R nets de frais estimés' : 'résultats nets de frais', journalBox);
}
function rebindJournal() {
  if (!journalBox) return;
  const j = st.journal;
  if (!j || j.space !== apiSpace()) { journalBox.replaceChildren(el('div', { class: 'skeleton' })); return; }
  const rows = (j.clotures || []).slice(0, 10);
  if (!rows.length) { journalBox.replaceChildren(empty('Aucune clôture pour l\'instant', [st.space === 'live' ? 'Les trades terminés (objectif, stop, clôture déclarée) s\'affichent ici.' : 'Les recommandations closes ou pas déclenchées s\'affichent ici.'])); return; }
  journalBox.replaceChildren(el('div', { class: 'feed' }, rows.map((r) => {
    if (st.space === 'live') {
      return el('div', null, el('span', { class: 'mono', text: r.symbol }), el('span', { class: 'mono ' + (isNum(r.r_net) ? (r.r_net >= 0 ? 'up' : 'down') : 'muted'), text: rr(r.r_net) }),
        chip((r.raison || '?') + (r.corrige ? ' · corrigé' : ''), r.raison === 'stop' ? 'down' : String(r.raison || '').startsWith('tp') ? 'up' : ''),
        el('span', { class: 'soft right', text: stamp(r.closed_at) + ' · ' + eur(r.eur, true) }));
    }
    const res = r.result || {};
    return el('div', null, el('span', { class: 'mono', text: r.symbol }), el('span', { class: 'mono ' + (isNum(res.r_net) ? (res.r_net >= 0 ? 'up' : 'down') : 'muted'), text: r.status === 'pas_declenche' ? DASH : rr(res.r_net) }),
      chip(r.status === 'pas_declenche' ? 'pas déclenchée' : res.raison || r.status_fr, r.status === 'pas_declenche' ? '' : 'info'),
      el('span', { class: 'soft right', text: r.type_fr + ' · ' + stamp(res.closed_at || r.sent_at) + (r.status === 'clos' ? ' · ' + eur(res.eur, true) : '') }));
  })));
}
function adviceCard(pos) {
  if (st.space !== 'live') {
    return card('c-12', 'Conseils en prix', 'Mes trades seulement', empty('Les recommandations gardent la gestion de leur message', ['Pas de conseils sur le Papier : il mesure « si j\'avais suivi exactement le message ».']));
  }
  const withAdvice = pos.filter((p) => p.conseil);
  const body = [el('div', { class: 'row-gap' },
    el('button', { class: 'btn primary', type: 'button', onclick: runAdvice }, 'Demander les conseils maintenant'),
    el('button', { class: 'btn', type: 'button', onclick: () => exitForm('', null) }, 'Déclarer une sortie…'),
    el('button', { class: 'btn', type: 'button', onclick: reopenForm }, 'Rouvrir un trade arrêté…'),
    el('span', { class: 'muted small', text: 'les conseils relisent les bougies (crédits Twelve Data, à la demande seulement)' }))];
  if (withAdvice.length) {
    body.push(el('div', { class: 'sig-list mt-12' }, withAdvice.map((p) => el('div', { class: 'advice' },
      el('div', { class: 'h' }, el('b', { text: p.symbol }), chip(p.conseil.decision, /SORTIR/.test(p.conseil.decision) ? 'down' : /ALL/.test(p.conseil.decision) ? 'warn' : 'up'), el('span', { class: 'muted small', text: stamp(p.conseil.at) + ' · cours ' + price(p.conseil.price) })),
      p.conseil.orders ? el('div', { class: 'mono small', text: p.conseil.orders }) : null,
      p.conseil.why ? el('div', { class: 'soft small', text: p.conseil.why }) : null))));
  } else body.push(el('p', { class: 'muted small mt-12', text: 'Aucun conseil gardé sur les trades ouverts aujourd\'hui. Les KILL n\'en ont pas : ils se vendent à la clôture.' }));
  return card('c-12', 'Conseils en prix', 'sortir, alléger ou garder ; ventes et stop en prix', ...body);
}
AFTER.positions = () => { updatePosCharts(); heavyPoll.now(); };
function updatePosCharts() {
  const d = st.posCharts; if (!d || d.space !== apiSpace() || st.view !== 'positions') return;
  for (const c of d.charts) {
    const host = document.getElementById('pc-' + c.position.id); if (!host) continue;
    let h = charts.find((x) => x.host === host);
    if (!c.bars.length) { if (!h) host.replaceChildren(el('div', { class: 'chart-empty', text: c.manque || 'Aucune bougie disponible' })); continue; }
    if (!h) { host.replaceChildren(); h = makeChart(host, { bars: c.bars, positions: [c.position], tf: '15m', compact: true, sym: c.position.symbol }); }
    else { setBars(h, c.bars); setPositions(h, [c.position]); }
  }
}

/* --- Marchés */
BUILD.marches = () => {
  const s = st.state, pos = s.positions_open || [];
  if (!st.chartSym) {
    const first = pos[0] || null;
    st.chartSym = first ? first.symbol : ((s.plans_today || {}).picks || [])[0]?.symbol || 'BTC';
    st.chartMarket = first ? first.market : st.chartSym === 'BTC' ? 'crypto' : 'stock';
  }
  const sym = st.chartSym, mine = pos.filter((p) => p.symbol === sym);
  const strip = el('div', { class: 'eng-strip', role: 'group', 'aria-label': 'Trades ouverts (touche N)' }, el('span', { class: 'lab', text: 'En position · ' + spaceName() }));
  if (pos.length) {
    for (const p of pos) {
      const r = el('span');
      strip.append(el('button', { class: 'eng', type: 'button', data: { eng: p.symbol, market: p.market }, 'aria-pressed': String(p.symbol === sym) }, el('span', { class: 'engdot' }), p.symbol, ' ', r));
      bind((s2) => { const q = (s2.positions_open || []).find((x) => x.id === p.id); const u = q && q.unrealized; r.textContent = u ? rr(u.r) : DASH; r.className = u && isNum(u.r) ? (u.r >= 0 ? 'up' : 'down') : 'muted'; });
    }
    strip.append(el('span', { class: 'kbd kh', title: 'Trade suivant', text: 'N' }));
  } else strip.append(el('span', { class: 'muted small', text: 'aucun trade ouvert dans cet espace' }));
  const big = el('div', { class: 'big num', id: 'mk-price', text: DASH });
  const chg = el('span', { class: 'chip', id: 'mk-chg', text: DASH });
  const tabs = el('div', { class: 'seg-tabs', role: 'group', 'aria-label': 'Intervalle ([ et ])' }, TFS.map((t) => el('button', { type: 'button', data: { tf: t }, 'aria-pressed': String(t === st.tf), text: t })));
  const top = el('div', { class: 'chart-top' }, monogram(sym, st.chartMarket), el('div', null, el('div', { class: 'sym' }, sym, clsTag(st.chartMarket)), el('div', { class: 'nm', id: 'mk-src', text: DASH })), big, chg, tabs);
  const posbar = el('div', { id: 'mk-posbar' });
  if (mine.length) posbar.replaceChildren(posBar(mine[0], mine.length));
  const chartBox = el('div', { class: 'chart', id: 'chart', 'aria-label': 'Graphique ' + sym + (mine.length ? ', avec niveaux SL et TP du trade' : '') }, el('div', { class: 'chart-empty', text: 'Bougies ' + st.tf + '…' }));
  const src = el('div', { class: 'chart-src', id: 'mk-age' });
  const chartCard = el('article', { class: 'card chart-card c-8' }, strip, top, posbar, chartBox, src);
  const watch = el('div', { class: 'rows', id: 'watch' }, el('div', { class: 'skeleton' }));
  const side = card('c-4', 'Watchlist', el('span', { class: 'kh' }, kbd('J'), ' ', kbd('K'), ' naviguer · ', kbd('Entrée'), ' au graphique'), watch);
  const tools = card('c-12', 'Plan du jour · ' + sym, null, el('div', { id: 'mk-plan' }, el('p', { class: 'muted small m-0', text: 'Chargement…' })),
    el('div', { class: 'row-gap mt-12' },
      el('button', { class: 'btn', type: 'button', onclick: () => opinion(sym, st.chartMarket) }, 'Avis JARVIS ', el('span', { class: 'kh' }, kbd('A'))),
      st.chartMarket === 'stock' ? el('button', { class: 'btn', type: 'button', onclick: () => company(sym) }, 'Fiche société') : null,
      el('button', { class: 'btn primary', type: 'button', onclick: () => planForm({ symbol: sym, market: st.chartMarket }) }, 'Nouveau plan ', el('span', { class: 'kh' }, kbd('+'))),
      el('span', { class: 'muted small', text: 'calculés à la demande (crédits Twelve Data pour une action)' })));
  return [chartCard, side, tools];
};
function posBar(p, n) {
  const sl = p.stop, et = exitTarget(p), ratio = rrRatio(p), nt = nextTarget(p), live = st.space === 'live';
  const items = [el('span', { class: 'lvl lvl-en' }, el('b', { text: p.side === 'short' ? 'SHORT' : 'LONG' }), ' ' + (isNum(p.qty) ? qty(p.qty) : '?') + ' à ' + price(p.entry)), verdictChip(p.verdict)];
  if (p.against_jarvis) items.push(chip('contre l\'avis de JARVIS', 'against'));
  items.push(sl ? el('span', { class: 'lvl lvl-sl' }, el('b', { text: 'SL' }), ' ' + price(sl.price) + ' · ' + (sl.kind === 'breakeven' ? 'point mort' : pct(dirPct(p, sl.price)) + ' · ' + rr(rAt(p, sl.price)))) : chip('SL inconnu', 'ghost'));
  if (et) items.push(el('span', { class: 'lvl lvl-tp' }, el('b', { text: et.name }), ' ' + price(et.price) + ' · ' + pct(dirPct(p, et.price)) + ' · sortie' + (et.eta_median && !et.hit_at ? ' · ' + whenText(et.eta_median) : '')));
  items.push(el('span', { class: 'lvl', text: ratio != null ? 'R:R ' + nf(1).format(ratio) : sl && sl.kind === 'breakeven' ? 'R:R sans risque' : 'R:R ?' }));
  const dist = el('span', { class: 'lvl lvl-dist', id: 'mk-dist' });
  items.push(dist);
  if (nt && nt.late) items.push(chip(nt.name + ' en retard', 'warn'));
  if (n > 1) items.push(chip(n + ' trades sur ce symbole', 'ghost'));
  items.push(el('span', { class: 'lvl lvl-note', text: live ? 'Ordres chez IBKR, posés par toi · JARVIS suit sur les bougies' : 'Gestion du message Discord · suivi par JARVIS sur les bougies' }));
  const bar = el('div', { class: 'posbar' }, items);
  bind((s) => {
    const q = (s.positions_open || []).find((x) => x.id === p.id); if (!q) return;
    const t = exitTarget(q), px = q.last_price, sg = sideSign(q);
    if (!t || !q.stop || !isNum(px)) { dist.textContent = 'distances inconnues'; return; }
    const toTp = sg * (t.price - px) / px * 100, toSl = sg * (px - q.stop.price) / px * 100;
    const crossed = toTp <= 0 ? t.name + ' franchi' : toSl <= 0 ? 'SL franchi' : null;
    dist.classList.toggle('warn', !!crossed);
    dist.textContent = crossed ? crossed + ' · à confirmer au prochain passage du suivi' : t.name + ' à ' + pct(toTp, false) + ' · SL à ' + pct(toSl, false);
  });
  return bar;
}
AFTER.marches = () => { loadInstruments(); loadInstrument(true); };
async function loadInstruments() {
  if (st.view !== 'marches' && Date.now() - st.instrumentsAt < 60000) return;
  const space = apiSpace();
  try { const d = await api('/api/cockpit/instruments?space=' + space, { timeout: 15000 }); if (space === apiSpace()) { st.instruments = d; st.instrumentsAt = Date.now(); renderWatch(); } }
  catch (e) { const w = $('#watch'); if (w) w.replaceChildren(empty('Watchlist indisponible', [e.message])); }
}
function renderWatch() {
  const w = $('#watch'); if (!w || !st.instruments) return;
  const rows = st.instruments.instruments || [];
  if (!rows.length) { w.replaceChildren(empty('Watchlist vide', ['JARVIS_WATCHLIST_STOCKS et JARVIS_WATCHLIST_CRYPTO ne listent rien et aucun trade n\'est ouvert.'])); return; }
  const existing = new Map($$('[data-wsym]', w).map((n) => [n.dataset.wsym, n]));
  if (existing.size !== rows.length || rows.some((r) => !existing.has(r.symbol))) {
    w.replaceChildren(...rows.map((r, k) => el('button', { class: 'row', type: 'button', data: { sym: r.symbol, market: r.market, navItem: k, wsym: r.symbol } }, monogram(r.symbol, r.market),
      el('span', { class: 'blk' }, el('span', { class: 'sym' }, r.symbol, clsTag(r.market), r.en_position ? el('span', { class: 'engdot', title: 'Trade ouvert dans l\'espace ' + spaceName() }) : null), el('span', { class: 'nm', text: r.source || 'pas de prix' })),
      el('span', { class: 'sp' }), el('span', { class: 'px' }, el('span', { class: 'p' }), el('span', { class: 'c' }), el('span', { class: 'a' })))));
    if (st.instruments.manque) w.append(el('p', { class: 'muted small', text: st.instruments.manque }));
  }
  for (const r of rows) {
    const n = $('[data-wsym="' + CSS.escape(r.symbol) + '"]', w); if (!n) continue;
    n.querySelector('.sp').replaceChildren(sparkSvg(r.spark));
    setPrice(n.querySelector('.p'), r.price);
    const c = n.querySelector('.c'); c.textContent = pct(r.change_pct); c.className = 'c ' + (isNum(r.change_pct) ? (r.change_pct >= 0 ? 'up' : 'down') : 'muted');
    const a = n.querySelector('.a'); a.textContent = r.freshness.etat === 'cloture' ? 'clôture' : (r.freshness.stale ? '⚠ ' : '') + ago(r.freshness.age_s); a.className = 'a' + (r.freshness.stale ? ' stale' : '');
    if (r.symbol === st.chartSym) { const chgEl = $('#mk-chg'); if (chgEl) { chgEl.textContent = pct(r.change_pct); chgEl.className = 'chip ' + (isNum(r.change_pct) ? (r.change_pct >= 0 ? 'up' : 'down') : ''); } }
  }
}
async function loadInstrument(fresh) {
  if (st.view !== 'marches') return;
  const sym = st.chartSym, tf = st.tf, space = apiSpace();
  const q = '/api/cockpit/instrument/' + encodeURIComponent(sym) + '?space=' + space + '&tf=' + tf + (st.chartMarket ? '&market=' + st.chartMarket : '');
  try {
    const d = await api(q, { timeout: 15000 });
    if (sym !== st.chartSym || tf !== st.tf || space !== apiSpace() || st.view !== 'marches') return;
    st.instrument = d; drawInstrument(fresh);
  } catch (e) {
    const box = $('#chart'); if (box && !charts.some((h) => h.host === box)) box.replaceChildren(el('div', { class: 'chart-empty', text: e.message }));
  }
}
function drawInstrument(fresh) {
  const d = st.instrument, box = $('#chart'); if (!box) return;
  const last = d.bars.length ? d.bars[d.bars.length - 1] : null;
  setPrice($('#mk-price'), last ? last[4] : null);
  $('#mk-src').textContent = d.source ? d.source : d.manque || 'pas de source';
  const age = $('#mk-age');
  put(age, el('span', { text: 'Source : ' + (d.source || DASH) }), el('span', { text: d.as_of ? 'fin de la dernière bougie : ' + stamp(d.as_of) : 'aucune bougie' }), d.manque ? el('span', { class: 'warn', text: d.manque }) : null,
    d.positions.length > 1 ? el('span', { text: 'plusieurs trades sur ce symbole : une ligne d\'entrée par trade' }) : null);
  let h = charts.find((x) => x.host === box);
  if (!d.bars.length) { if (h) destroyChart(h); box.replaceChildren(el('div', { class: 'chart-empty', text: d.manque || 'Aucune bougie disponible pour ' + d.symbol })); }
  else if (!h || fresh) { if (h) destroyChart(h); box.replaceChildren(); makeChart(box, { bars: d.bars, positions: d.positions, tf: d.tf, volume: true, sym: d.symbol }); }
  else { setBars(h, d.bars); setPositions(h, d.positions); }
  const plan = $('#mk-plan');
  if (plan) {
    const p = d.plan_du_jour;
    plan.replaceChildren(p ? el('div', { class: 'sig' }, el('div', { class: 'sig-top' }, verdictChip(p.verdict), el('b', { text: p.symbol }), p.confirmation ? chip((CONF[p.confirmation] || [p.confirmation])[0], (CONF[p.confirmation] || ['', ''])[1]) : null,
      el('span', { class: 'score' }, el('b', { text: isNum(p.composite) ? nf(1).format(p.composite) : DASH }), el('span', { text: 'priorité, pas une probabilité' }))),
    el('div', { class: 'small soft mono', text: 'entrée ' + price(p.entry) + ' · stop ' + price(p.stop) + ' · objectifs ' + (p.targets || []).map((t) => price(isNum(t) ? t : t.price)).join(' · ') }))
      : el('p', { class: 'muted small m-0', text: 'Pas de plan du scanner aujourd\'hui sur ' + d.symbol + '.' }));
  }
}

/* ---------- données des vues Risque, Labo, Agenda, Journal et Santé : relues seulement quand la vue est affichée ;
   une carte n'est re-remplie que si ses données ont changé (fill). */
const sigOf = (x) => { try { const s = JSON.stringify(x); return s === undefined ? '∅' : s; } catch (_) { return String(Date.now()); } };
function fill(box, data, build) {
  if (!box) return;
  const s = sigOf(data);
  if (box.jsig === s) return;
  box.jsig = s;
  put(box, build(data));
}
let heavyBinders = [];
function onData(key, fn) { heavyBinders.push([key, fn]); if (st[key]) { try { fn(st[key]); } catch (e) { console.error(e); } } }
function emit(key) { if (!st[key]) return; for (const [k, f] of heavyBinders) if (k === key) { try { f(st[key]); } catch (e) { console.error(e); } } }
function waitFailed(key, msg) { for (const b of $$('[data-wait="' + key + '"]')) put(b, empty('Lecture impossible', [msg])); }
async function loadInto(key, path, timeout = 15000) {
  try { st[key] = await api(path, { timeout }); st[key + 'At'] = Date.now(); }
  catch (e) { if (!st[key]) waitFailed(key, e.message); return; }
  emit(key);
}
const waitBox = (key, cls) => el('div', { class: cls || null, data: { wait: key } }, st[key] ? null : el('div', { class: 'skeleton short' }));
const kvRow = (k, v, cls) => el('div', null, el('dt', { text: k }), el('dd', { class: cls, text: v }));
const STATUS_PROP = { pending: ['en attente', 'info'], approved: ['approuvée', 'up'], refused: ['refusée', 'down'], replaced: ['remplacée', 'ghost'] };
const propChip = (p) => { const s = STATUS_PROP[p.status] || [p.status || '?', '']; return chip(s[0], s[1]); };

/* Texte d'une commande Discord (même fonction côté serveur) : gras, code, petites lignes « -# », blocs ```. */
function inlineMd(s) {
  const out = [], re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0, m;
  while ((m = re.exec(s))) {
    if (m.index > last) out.push(s.slice(last, m.index));
    out.push(m[0][0] === '*' ? el('b', { text: m[0].slice(2, -2) }) : el('code', { text: m[0].slice(1, -1) }));
    last = re.lastIndex;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}
function discordText(text) {
  const box = el('div', { class: 'dtext' });
  let code = null;
  for (const raw of String(text || '').split('\n')) {
    if (raw.trim().startsWith('```')) { if (code) { box.append(el('pre', { class: 'pre', text: code.join('\n') })); code = null; } else code = []; continue; }
    if (code) { code.push(raw); continue; }
    if (!raw.trim()) { box.append(el('div', { class: 'gap' })); continue; }
    let line = raw, cls = 'ln';
    if (line.startsWith('-# ')) { line = line.slice(3); cls += ' sub'; }
    else if (/^#{1,3} /.test(line)) { line = line.replace(/^#{1,3} /, ''); cls += ' h'; }
    else if (line.startsWith('> ')) { line = line.slice(2); cls += ' q'; }
    box.append(el('div', { class: cls }, inlineMd(line)));
  }
  if (code) box.append(el('pre', { class: 'pre', text: code.join('\n') }));
  return box;
}
const TEXTS = {};
async function loadText(mod) {
  const c = TEXTS[mod];
  if (c && (c.loading || Date.now() - c.at < 120000)) return;
  TEXTS[mod] = Object.assign(c || {}, { loading: true });
  try { TEXTS[mod] = { at: Date.now(), data: await api('/api/cockpit/texte/' + mod, { timeout: 30000 }) }; }
  catch (e) { TEXTS[mod] = { at: Date.now() - 90000, data: Object.assign({}, c && c.data, { erreur: e.message }) }; }
  for (const b of $$('[data-tmod="' + mod + '"]')) renderTextBox(b);
}
function renderTextBox(box) {
  const c = TEXTS[box.dataset.tmod];
  if (!c || !c.data) { if (!box.firstChild) box.append(el('div', { class: 'skeleton' })); return; }
  fill(box, c.data, (d) => (!d.texte ? empty('Lecture impossible', [d.erreur || 'réponse vide']) : [
    el('div', { class: 'tmeta' }, chip(d.commande, 'ghost'), el('span', { text: 'lu ' + stamp(d.at) + ' · le texte de la commande sur Discord' + (d.erreur ? ' · relecture en échec : ' + d.erreur : '') })),
    d.note ? el('p', { class: 'small muted m-0' }, inlineMd(d.note)) : null,
    discordText(d.texte)]));
}
function textBox(mod) {
  const box = el('div', { class: 'tbox', data: { tmod: mod } });
  renderTextBox(box); loadText(mod);
  return box;
}
const textCard = (mod, cls, eyebrow, meta) => card(cls, eyebrow, meta || null, textBox(mod));
function textDetails(mod, label) {
  const d = el('details', { class: 'tdetails' }, el('summary', { class: 'small soft' }, inlineMd(label)));
  d.addEventListener('toggle', () => { if (d.open && !d.querySelector('.tbox')) d.append(textBox(mod)); });
  return d;
}

/* --- Risque */
BUILD.risque = () => [gradeCard(), budgetCard(), sizingCard(), feesCard(), textCard('grade', 'c-6', 'Grade détaillé'), textCard('frais', 'c-6', 'Ta mise de 20 € chez IBKR')];
function gradeTile(m, g) {
  const chips = [];
  if (g.perime) chips.push(chip('périmé', 'warn'));
  if (g.mode_survie) chips.push(chip('mode survie', 'warn'));
  if (g.non_note) chips.push(chip('non noté', 'ghost'));
  if (g.observation) chips.push(chip('plans en observation', 'info'));
  return el('div', { class: 'gtile' },
    el('div', { class: 'gtile-h' }, el('span', { class: 'soft', text: m === 'stock' ? 'Actions US' : 'Crypto' }), el('span', { class: 'gl' + (String(g.grade || '?').length > 2 ? ' long' : ''), text: g.grade || '?' })),
    el('dl', { class: 'kv' },
      kvRow('Risque proposé', pct(g.risque_propose, false) + (isNum(g.risque_propose_eur) ? ' · ' + eur(g.risque_propose_eur) : '')),
      kvRow('Risque du grade', pct(g.risque_grade, false)),
      kvRow('Plafond de la lettre', isNum(g.risque_max) ? pct(g.risque_max, false) : DASH)),
    el('p', { class: 'small soft m-0', text: g.raison || '' }),
    chips.length ? el('div', { class: 'row-gap' }, chips) : null);
}
function gradeCard() {
  const box = el('div', { class: 'gtiles' }), age = el('span');
  bind((s) => {
    const r = s.risk, stale = isNum(r.grade_age_h) && r.grade_age_h > r.grade_max_age_h;
    age.className = stale ? 'warn' : '';
    age.textContent = isNum(r.grade_age_h) ? 'calculé ' + ago(r.grade_age_h * 3600) + (stale ? ' ⚠ périmé au-delà de ' + r.grade_max_age_h + ' h' : '') : 'jamais calculé par le labo';
    fill(box, r.grades, (gr) => ['stock', 'crypto'].map((m) => gradeTile(m, gr[m] || {})));
  });
  return card('c-6', 'Grade par marché', age, box, el('div', { class: 'small muted mt-12', text: 'Les hausses de lettre restent à toi (propositions) ; le council du soir ne baisse le grade que pour un défaut d\'intégrité.' }));
}
function budgetCard() {
  const openM = segm(0, 1, 20), dayM = segm(0, 1, 20), openV = el('span', { class: 'num' }), dayV = el('span', { class: 'num' });
  const kv = el('dl', { class: 'kv mt-12' }), note = el('p', { class: 'note-warn' });
  bind((s) => {
    const r = s.risk, cap = r.capital_declare_eur;
    openV.textContent = pct(r.risque_ouvert_pct, false) + ' / ' + pct(r.risque_ouvert_max_pct, false, 0);
    setSegm(openM, r.risque_ouvert_pct, r.risque_ouvert_max_pct);
    dayV.textContent = rr(r.r_du_jour_net) + ' · arrêt à ' + rr(r.arret_jour_r);
    setSegm(dayM, isNum(r.r_du_jour_net) ? Math.max(0, -r.r_du_jour_net) : null, Math.abs(r.arret_jour_r));
    put(kv, kvRow('Ta règle : risque max par trade', eur(r.risque_max_par_trade_eur)),
      kvRow('Capital', isNum(cap) ? eur0(cap) + ' · déclaré' : 'non déclaré (JARVIS_CAPITAL)'),
      kvRow('Risque ouvert en euros', isNum(cap) && isNum(r.risque_ouvert_pct) ? '≈ ' + eur(cap * r.risque_ouvert_pct / 100) : DASH),
      kvRow('Budget encore libre', isNum(cap) && isNum(r.risque_ouvert_pct) ? pct(Math.max(0, r.risque_ouvert_max_pct - r.risque_ouvert_pct), false) + ' · ≈ ' + eur(cap * Math.max(0, r.risque_ouvert_max_pct - r.risque_ouvert_pct) / 100) : DASH),
      kvRow('Taille d\'un nouveau plan', 'min(capital × % du grade, ' + eur0(r.risque_max_par_trade_eur) + ')'));
    note.textContent = '⚠ ' + r.note.charAt(0).toUpperCase() + r.note.slice(1) + '.';
  });
  return card('c-6', 'Risque ouvert et arrêt du jour', 'trades déclarés dans Mes trades', el('div', { class: 'meters' },
    el('div', null, el('div', { class: 'meter-h' }, el('span', { text: 'Risque ouvert' }), openV), openM, el('div', { class: 'meter-n', text: 'risque au stop des trades ouverts, en % du capital déclaré ; au-delà, JARVIS plafonne la taille' })),
    el('div', null, el('div', { class: 'meter-h' }, el('span', { text: 'Arrêt de la journée' }), dayV), dayM, el('div', { class: 'meter-n', text: 'R nets du jour des trades pris ; à l\'arrêt, plus de taille proposée (message sans taille)' }))), kv, note);
}
/* Dernier plan calculé dans ce navigateur : gardé en local (la taille et ses entrées, rien d'autre). */
function keepPlan(p) {
  st.lastSizing = { at: new Date().toISOString(), symbol: p.symbol, market: p.market, side: p.side, entry: p.entry, stop: p.stop, grade: p.grade || null, sizing: p.sizing || null, sizing_regle: p.sizing_regle || null, ibkr: p.ibkr || null };
  try { localStorage.setItem('cockpit.lastPlan', JSON.stringify(st.lastSizing)); } catch (_) { /* stockage indisponible */ }
}
try { const lp = JSON.parse(localStorage.getItem('cockpit.lastPlan') || 'null'); if (lp && lp.symbol) st.lastSizing = lp; } catch (_) { /* illisible : ignoré */ }
function sizingSteps(p, ruleEur) {
  const g = p.grade || {}, s = p.sizing || p.sizing_regle, rule = !p.sizing && !!p.sizing_regle, ib = p.ibkr, stock = p.market !== 'crypto';
  const li = (label, value, sub, cls) => el('li', { class: cls || null }, el('span', null, el('span', { text: label }), sub ? el('span', { class: 'small muted blk', text: sub }) : null), el('span', { class: 'num', text: value }));
  const out = [li('Grade ' + (g.grade || '?') + (stock ? ' des actions' : ' de la crypto') + ' : risque proposé', pct(g.risque_propose, false), g.raison || null)];
  if (!s) { out.push(li('Pas de taille', DASH, 'grade à 0 % et pas de taille à ta règle pour ce plan', 'final')); return out; }
  if (rule) out.push(li('Grade à 0 % : ta règle, hors avis de JARVIS', eur(s.risk_amount_eur), 'JARVIS ne propose pas de taille ; le plan donne celle de ta règle', 'bind'));
  else {
    const raw = isNum(s.capital) && isNum(g.risque_propose) ? s.capital * g.risque_propose / 100 : null;
    const capped = isNum(raw) && isNum(ruleEur) && raw > ruleEur + 1e-9;
    out.push(li('Risque en euros', eur(s.risk_amount_eur), isNum(raw) ? 'min(' + eur0(s.capital) + ' × ' + pct(g.risque_propose, false) + ' = ' + eur(raw) + ', ta règle ' + eur0(ruleEur) + ')' + (capped ? ' : ta règle plafonne' : '') : 'capital non déclaré : ta règle de ' + eur0(ruleEur), capped ? 'bind' : null));
  }
  out.push(li('Au change EUR/USD ' + nf(4).format(s.eurusd), usd(s.risk_amount), null));
  const unit = Math.abs(p.entry - p.stop);
  out.push(li('Risque par ' + (stock ? 'action' : 'unité') + ' : |' + price(p.entry) + ' − ' + price(p.stop) + '|', usd(unit), '≈ ' + eur(s.unit_risk_eur) + ' par ' + (stock ? 'action' : 'unité')));
  out.push(li('Quantité : ' + usd(s.risk_amount) + ' ÷ ' + usd(unit) + (stock ? ', arrondie à l\'action entière' : ', arrondie vers le bas'), stock ? int(s.quantity) + ' action' + (s.quantity > 1 ? 's' : '') : qty(s.quantity),
    'nominal ' + usd(s.notional) + (isNum(s.leverage) ? ' · levier ×' + nf(2).format(s.leverage) : ''), ib ? null : 'final'));
  if (ib && ib.quantity) {
    const f = ib.fees && ib.fees[ib.plan];
    out.push(li('Chez IBKR : perte réelle au SL', usd(ib.real_risk_usd), (ib.sortie_unique ? 'un bracket sur ' + ((ib.brackets || [])[0] || {}).name + ' (' + (ib.reperes || []).join(' et ') + ' : repères)' : (ib.brackets || []).length + ' brackets, un par objectif') + (f ? ' · commissions ' + (ib.plan === 'degressif' ? 'Dégressif' : 'Fixe') + ' ≈ ' + usd(f.usd) + ' (' + rr(f.r, false) + ')' : ''), 'final'));
  } else if (ib && ib.raison) out.push(li('Chez IBKR', '0 action', ib.raison, 'final'));
  return out;
}
function sizingCard() {
  const box = el('div');
  const draw = () => {
    const p = st.lastSizing, ruleEur = st.state && st.state.risk.risque_max_par_trade_eur;
    fill(box, [p, ruleEur], () => (p ? [el('div', { class: 'row-gap small soft' }, el('b', { text: p.symbol }), chip(p.side === 'short' ? 'SHORT' : 'LONG', p.side === 'short' ? 'down' : 'up'), el('span', { text: 'entrée ' + price(p.entry) + ' · stop ' + price(p.stop) + ' · calculé ' + stamp(p.at) })), el('ol', { class: 'steps mt-12' }, sizingSteps(p, ruleEur))]
      : empty('Aucun plan calculé dans ce navigateur', ['« Nouveau plan » (touche +) calcule la taille ; ses étapes s\'affichent ici et restent gardées sur cet appareil.'])));
  };
  bind(draw);
  return card('c-7', 'Dernier calcul de taille', el('button', { class: 'btn sm', type: 'button', onclick: () => planForm() }, 'Nouveau plan ', el('span', { class: 'kh' }, kbd('+'))), box);
}
function feesCard() {
  const box = el('div');
  bind((s) => fill(box, [s.risk.ibkr, st.lastSizing && st.lastSizing.ibkr], ([f, last]) => {
    if (!f) return empty('Barème IBKR inconnu', []);
    const rows = Object.entries(f.baremes).map(([k, b]) => el('div', { class: 'fee' + (k === f.bareme ? ' mine' : '') },
      el('div', { class: 'fee-h' }, el('b', { text: b.label }), k === f.bareme ? chip('ton compte', 'mode') : null),
      el('div', { class: 'small soft', text: nf(4).format(b.par_action_usd) + ' $ par action · minimum ' + usd(b.minimum_usd) + ' · plafond ' + pct(b.plafond_pct, false, 0) + ' de l\'ordre' + (b.frais_place_usd ? ' · + frais de place ≈ ' + nf(4).format(b.frais_place_usd) + ' $ par action' : '') }),
      el('div', { class: 'small' }, 'Plancher d\'un aller-retour (1 bracket) : ', el('b', { class: 'num', text: usd(b.plancher_aller_retour_usd) }), el('span', { class: 'muted', text: ' · ' + rr(b.plancher_r, false) + ' à ' + eur0(s.risk.risque_max_par_trade_eur) }))));
    const lf = last && last.fees;
    return [el('div', { class: 'fees' }, rows),
      el('div', { class: 'small soft mt-12', text: f.sortie_unique ? 'Sortie unique sur TP2 (TP1 et TP3 : repères sans ordre) : un bracket, deux ordres. JARVIS_IBKR_BRACKETS=3 pour un bracket par objectif.' : 'Un bracket par objectif (JARVIS_IBKR_BRACKETS=3) : deux ordres par bracket.' }),
      lf ? el('div', { class: 'small mt-8' }, 'Dernier plan (' + st.lastSizing.symbol + ', ' + int(last.quantity) + ' actions) : ', el('b', { text: 'Fixe ' + usd(lf.fixe.usd) + ' (' + rr(lf.fixe.r, false) + ')' }), ' · ', el('b', { text: 'Dégressif ' + usd(lf.degressif.usd) + ' (' + rr(lf.degressif.r, false) + ')' })) : null];
  }));
  return card('c-5', 'Frais IBKR', 'barème du compte (JARVIS_IBKR_PLAN)', box);
}

/* --- Labo */
const LAB_TABS = [['strategies', 'Déclarations'], ['ouverture', 'N° 12 ouverture'], ['gemini', 'Gemini'], ['lecons', 'Leçons'], ['autopsie', 'Autopsie'], ['rates', 'Ratés +10 %'], ['etude8k', 'Étude 8-K'], ['sec8k', 'Mesure 8-K'], ['polyfed', 'Fed'], ['potentiel', 'Potentiel +10']];
try { const t = localStorage.getItem('cockpit.labTab'); if (LAB_TABS.some((x) => x[0] === t)) st.labTab = t; } catch (_) { /* idem */ }
BUILD.labo = () => [labStatusCard(), killCard(), labTabsCard(), councilCard(), declarationsCard()];
function labFacts(lab) {
  const s = lab.statut || {}, r = lab.regles || {}, l = lab.lecons || {}, base = lab.base || {};
  if (s.erreur || base.erreur) return [empty('Labo illisible', [s.erreur || base.erreur])];
  const fact = (k, v, sub) => el('div', { class: 'fact' }, el('span', { class: 'k', text: k }), el('span', { class: 'v num', text: v }), sub ? el('span', { class: 's', text: sub }) : null);
  const rep = s.replay || {}, last = (m) => ((rep[m] || {}).last_day || null);
  return [el('div', { class: 'facts' },
    fact('Méthode en place', s.method_version || DASH, s.exists ? (isNum(s.db_size_mb) ? 'base ' + (s.db_size_mb >= 1024 ? nf(1).format(s.db_size_mb / 1024) + ' Go' : nf(s.db_size_mb < 10 ? 1 : 0).format(s.db_size_mb) + ' Mo') : 'base présente') : 'base absente'),
    fact('Rejeu jusqu\'au', last('stock') ? DTF.dm.format(toDate(last('stock'))) : DASH, 'actions' + (last('crypto') ? ' · crypto ' + DTF.dm.format(toDate(last('crypto'))) : '')),
    fact('Règles', int(r.validee) + ' validée' + ((r.validee || 0) > 1 ? 's' : ''), int(r.rejetee) + ' rejetées · ' + int(r.en_attente) + ' en attente · ' + int(r.essais) + ' essais'),
    fact('Leçons', int(l.confirmee) + ' confirmée' + ((l.confirmee || 0) > 1 ? 's' : ''), int(l.a_confirmer) + ' à confirmer · ' + int(l.dementie) + ' démenties'))];
}
function labStatusCard() {
  const facts = waitBox('lab');
  onData('lab', (lab) => fill(facts, [lab.statut, lab.regles, lab.lecons, lab.base], () => labFacts(lab)));
  return card('c-8', 'Labo', el('span', null, 'lecture seule de lab.db · ', el('a', { href: '/cockpit/labo' }, 'page Labo actuelle')), facts, el('div', { class: 'mt-16' }, textBox('labo')));
}
function killCard() {
  const head = el('div', { class: 'row-gap' }), list = waitBox('lab');
  const btn = el('button', { class: 'btn', type: 'button', onclick: () => killmode(!(st.state && st.state.killmode.actif)) });
  bind((s) => {
    const k = s.killmode;
    put(head, chip(k.actif ? 'allumé' : 'éteint', k.actif ? 'warn' : 'ghost'), el('span', { class: 'small soft', text: k.depuis ? 'depuis le ' + stamp(k.depuis) : 'éteint par défaut' }));
    put(btn, k.actif ? 'Éteindre…' : 'Allumer…', el('span', { class: 'lock', text: '●', title: 'appui maintenu' }));
  });
  onData('lab', (lab) => fill(list, lab.killmode, (k) => {
    if (!k || k.erreur) return empty('Journal de Killmode illisible', [k && k.erreur]);
    const rows = (k.derniers || []).slice().reverse();
    return [el('p', { class: 'small soft m-0', text: int(k.kill) + ' KILL notés · ' + int(k.temoins) + ' témoins (version en vigueur et précédentes)' }),
      rows.length ? el('div', { class: 'feed mt-8' }, rows.map((c) => el('div', null, el('span', { class: 'mono', text: c.symbol }), el('span', { class: 'mono ' + (isNum(c.r_20e) ? (c.r_20e >= 0 ? 'up' : 'down') : 'muted'), text: isNum(c.r_20e) ? rr(c.r_20e) : 'à solder' }), el('span', { class: 'soft fv', text: 'entrée ' + price(c.entry) }), el('span', { class: 'soft right', text: c.day || DASH }))))
        : el('p', { class: 'small muted' }, inlineMd('Aucun KILL noté : le journal se remplit par `!kill` entre 9 h 45 et 11 h (New York).'))];
  }));
  const journalBtn = el('button', { class: 'btn', type: 'button', onclick: async () => { openSheet('Journal de Killmode', textBox('killmode'), el('div', { class: 'acts' }, el('button', { class: 'btn', type: 'button', onclick: closeSheet }, 'Fermer'))); } }, 'Journal complet');
  return card('c-4', 'Killmode', 'ne touche ni seuils ni version', head, el('div', { class: 'mt-12' }, list), el('div', { class: 'row-gap mt-12' }, btn, journalBtn),
    el('div', { class: 'small muted mt-8' }, inlineMd('Lecture finale après 8 semaines minimum. Le journal complet ne solde pas les appels : `!kill bilan` sur Discord le fait.')));
}
function labTabsCard() {
  const tabs = el('div', { class: 'seg-tabs wrap', role: 'tablist', 'aria-label': 'Modules du labo' });
  const body = el('div', { class: 'mt-12' });
  const show = (mod) => {
    st.labTab = mod;
    try { localStorage.setItem('cockpit.labTab', mod); } catch (_) { /* idem */ }
    for (const b of tabs.children) b.setAttribute('aria-pressed', String(b.dataset.lt === mod));
    put(body, textBox(mod));
  };
  for (const [m, t] of LAB_TABS) tabs.append(el('button', { type: 'button', data: { lt: m }, onclick: () => show(m), text: t }));
  show(st.labTab);
  return card('c-12', 'Modules du labo', 'mêmes textes que les commandes Discord · relus toutes les 2 min', tabs, body);
}
function councilCard() {
  const box = waitBox('lab');
  onData('lab', (lab) => fill(box, lab.propositions, (ps) => {
    if (!Array.isArray(ps)) return empty('Propositions illisibles', [ps && ps.erreur]);
    if (!ps.length) return empty('Aucune décision du labo enregistrée', ['Les règles approuvées, les hausses de grade proposées et les promotions de méthode apparaissent ici.']);
    return el('div', { class: 'props' }, ps.slice(0, 8).map((p) => el('div', { class: 'prop' },
      el('div', null, el('div', { text: '#' + p.id + ' · ' + proposalText(p) }), el('div', { class: 'why', text: (p.decided_at ? 'décidée ' + stamp(p.decided_at) : 'créée ' + stamp(p.created_at)) + (p.why ? ' · ' + (Array.isArray(p.why) ? p.why.join(' ; ') : p.why) : '') })),
      propChip(p))));
  }));
  return card('c-6', 'Council et veto', 'le council déclare, approuve et promeut seul', box,
    el('div', { class: 'row-gap mt-12' }, el('button', { class: 'btn danger', type: 'button', onclick: veto }, 'Poser un veto…', el('span', { class: 'lock', text: '●', title: 'appui maintenu' })),
      el('span', { class: 'small muted' }, inlineMd('annule la dernière décision au prochain passage du labo (`!veto`)'))));
}
function declarationsCard() {
  const box = waitBox('lab');
  onData('lab', (lab) => fill(box, lab.declarations || lab.base, (ds) => {
    if (!Array.isArray(ds)) return empty('Déclarations du council illisibles', [ds && ds.erreur]);
    if (!ds.length) return empty('Aucune déclaration du council', ['Une déclaration = une règle pré-déclarée jouée contre un témoin aléatoire ; le tableau complet est dans l\'onglet « Déclarations ».']);
    return el('div', { class: 'feed decl' }, ds.slice().reverse().slice(0, 12).map((d) => el('div', null,
      el('span', { class: 'mono', text: d.strategie || '#' + d.id }), el('span', { class: 'soft', text: d.market === 'crypto' ? 'crypto' : 'actions' }),
      el('span', { class: 'grow', text: d.nom || DASH, title: d.pourquoi || '' }), el('span', { class: 'soft right', text: d.creee_le ? stamp(d.creee_le) : DASH }))));
  }));
  return card('c-6', 'Déclarations du council', 'les plus récentes', box);
}

/* --- Agenda */
const CAT = { jarvis: 'JARVIS', marche: 'marché', labo: 'labo', routine: 'routine Claude', macro: 'macro', resultats: 'résultats' };
const IMPACT = { High: ['fort', 'haute'], Medium: ['moyen', 'normale'] };
const HOUR = { bmo: 'avant l\'ouverture', amc: 'après la clôture', dmh: 'pendant la séance' };
BUILD.agenda = () => [agendaDayCard(), agendaSideCard(), agendaWeekCard()];
function ecoItem(e) {
  const det = [['prévision', e.prevision], ['précédent', e.precedent], ['réel', e.reel]].filter((x) => x[1] != null && x[1] !== '').map((x) => x[0] + ' ' + x[1]).join(' · ');
  return { at: e.at, titre: e.pays + ' · ' + e.titre, categorie: 'macro', source: 'Helious', importance: e.impact === 'High' ? 'haute' : 'normale', detail: 'impact ' + (IMPACT[e.impact] || [e.impact])[0] + (det ? ' · ' + det : '') };
}
function ecoUnknown(eco, day) {
  if (!eco) return 'en lecture…';
  if (eco.erreur) return 'indisponible (' + eco.erreur + ')';
  if (!eco.couvre_jusqu_au || day > eco.couvre_jusqu_au) return 'pas encore publié par la source';
  return '';
}
function agendaDayCard() {
  const box = el('div'), meta = el('span'), note = el('div', { class: 'small muted mt-8' });
  const draw = () => {
    const a = st.agenda, today = dayKey(new Date()), ecoAll = a && a.economique;
    const eco = ecoAll && ecoAll.evenements ? ecoAll.evenements.filter((e) => dayKey(toDate(e.at)) === today).map(ecoItem) : [];
    const items = ((st.state && st.state.agenda_today) || []).concat(eco).sort((x, y) => (x.at < y.at ? -1 : 1));
    const unknown = ecoUnknown(ecoAll, today);
    meta.textContent = DTF.day.format(new Date());
    note.textContent = unknown ? 'Calendrier économique ' + unknown + ' : les annonces du jour ne figurent pas ici.' : ecoAll.stale ? '⚠ Calendrier économique : dernière valeur connue' + (ecoAll.maj ? ' (' + stamp(ecoAll.maj) + ')' : '') + '.' : '';
    note.hidden = !note.textContent;
    fill(box, [items, Math.floor(Date.now() / 60000)], () => agendaList(items, 0)); // la ligne « maintenant » avance à la minute
  };
  bind(draw); onData('agenda', draw);
  return card('c-7', 'Aujourd\'hui', meta, box, note);
}
function earningsBlock(r) {
  if (!r) return el('div', { class: 'skeleton short' });
  if (r.erreur) return empty('Calendrier des résultats indisponible', [r.erreur]);
  if (r.manque) return empty('Pas de calendrier des résultats', [r.manque]);
  const today = (st.agenda.seances.nyse.aujourd_hui || {}).jour || dayKey(new Date());
  const mine = (r.suivies || []).filter((x) => x.date === today), day = (r.jours || {})[today];
  const names = (list) => list.slice(0, 14).join(' · ') + (list.length > 14 ? ' … (+' + (list.length - 14) + ')' : '');
  return el('div', null,
    mine.length ? el('div', { class: 'feed earn' }, mine.map((x) => el('div', null, el('b', { class: 'mono', text: x.symbol }), chip(HOUR[x.heure] || 'heure inconnue', x.heure ? 'info' : 'ghost'), el('span', { class: 'soft fv', text: x.trimestre || '' }), el('span', { class: 'soft right', text: 'BPA attendu ' + (isNum(x.bpa_estime) ? nf(2).format(x.bpa_estime) : DASH) + (isNum(x.bpa_reel) ? ' · publié ' + nf(2).format(x.bpa_reel) : '') }))))
      : el('p', { class: 'small soft m-0', text: 'Aucune valeur suivie ne publie aujourd\'hui.' }),
    day ? el('div', { class: 'small muted mt-8' }, el('div', { text: 'Toute la cote aujourd\'hui : ' + day.total + ' publications.' }),
      day.avant_ouverture.length ? el('div', { text: 'Avant l\'ouverture : ' + names(day.avant_ouverture) }) : null,
      day.apres_cloture.length ? el('div', { text: 'Après la clôture : ' + names(day.apres_cloture) }) : null) : null);
}
function secBlock(s) {
  if (!s) return el('div', { class: 'skeleton short' });
  if (s.erreur) return empty('Dépôts SEC indisponibles', [s.erreur]);
  if (s.manque) return empty('Pas de lecture des 8-K', [s.manque]);
  const rows = s.depots || [], list = (k) => s[k] || [];
  const unread = [[list('erreurs'), 'en erreur'], [list('trop_lentes'), 'SEC trop lente'], [list('sans_cik'), 'sans CIK'], [list('hors_plafond'), 'au-delà de ' + list('valeurs').length + ' valeurs']]
    .filter((x) => x[0].length).map((x) => x[1] + ' : ' + x[0].join(', '));
  return el('div', null,
    rows.length ? el('div', { class: 'feed sec' }, rows.map((f) => el('div', null, el('b', { class: 'mono', text: f.symbol }), el('span', { class: 'soft', text: DTF.dm.format(toDate(f.date + 'T12:00:00Z')) + (f.form !== '8-K' ? ' · ' + f.form : '') }),
      el('span', { class: 'chips' }, (f.items || []).filter((i) => i.code !== '9.01').map((i) => chip(i.label, i.code === '2.02' ? 'info' : i.code === '1.01' ? 'up' : '', 'item ' + i.code))),
      el('a', { class: 'right small', href: f.lien, target: '_blank', rel: 'noopener noreferrer', text: 'EDGAR ↗' }))))
      : (s.lues || []).length ? el('p', { class: 'small soft m-0', text: 'Aucun 8-K depuis le ' + DTF.dm.format(toDate(s.depuis + 'T12:00:00Z')) + ' sur les ' + s.lues.length + ' valeur' + (s.lues.length > 1 ? 's' : '') + ' lue' + (s.lues.length > 1 ? 's' : '') + '.' })
        : empty('8-K inconnus', [(s.valeurs || []).length ? 'Aucune valeur lue sur la SEC (détail ci-dessous).' : 'Aucune valeur suivie : watchlist vide, aucun trade ni plan du jour.']),
    unread.length ? el('div', { class: 'small warn mt-8', text: 'Non vérifiées : ' + unread.join(' · ') }) : null,
    el('div', { class: 'small muted mt-8', text: s.note }));
}
function agendaSideCard() {
  const earn = waitBox('agenda'), sec = waitBox('agenda'), who = el('p', { class: 'small muted m-0' });
  onData('agenda', (a) => {
    fill(earn, [a.resultats, (a.seances.nyse.aujourd_hui || {}).jour], () => earningsBlock(a.resultats));
    fill(sec, a.sec8k, () => secBlock(a.sec8k));
    who.textContent = 'Valeurs suivies : ' + ((a.valeurs_suivies || []).join(', ') || 'aucune (watchlist vide, aucun trade ni plan)') + '.';
  });
  return card('c-5', 'Résultats et 8-K', 'valeurs suivies : positions, plans du jour, watchlist', who,
    el('h3', { class: 'sub-h', text: 'Résultats du jour' }), earn, el('h3', { class: 'sub-h', text: '8-K des trois derniers jours' }), sec);
}
function agendaWeekCard() {
  const box = waitBox('agenda'), src = el('div', { class: 'small muted mt-12' });
  onData('agenda', (a) => {
    const eco = a.economique || {}, res = a.resultats || {};
    fill(box, [eco.evenements, eco.couvre_jusqu_au, res.suivies, res.jours, eco.erreur, res.erreur, res.manque, dayKey(new Date())], () => {
      const days = [];
      for (let k = 0; k < 7; k++) days.push(dayKey(new Date(Date.now() + k * 864e5)));
      const out = days.map((d) => {
        const ev = (eco.evenements || []).filter((e) => dayKey(toDate(e.at)) === d);
        const mine = (res.suivies || []).filter((x) => x.date === d), all = (res.jours || {})[d];
        const label = DTF.day.format(new Date(d + 'T12:00:00'));
        // Inconnu ≠ rien : un jour que la source ne couvre pas, ou des résultats sans clé, ne sont pas « rien de notable »
        const unknown = [ecoUnknown(eco, d) ? 'calendrier économique' : null, res.erreur || res.manque ? 'résultats' : null].filter(Boolean);
        const unk = unknown.length ? el('p', { class: 'small warn m-0 mt-8', text: unknown.length > 1 ? 'calendrier économique et résultats inconnus' : unknown[0] + (unknown[0] === 'résultats' ? ' inconnus' : ' inconnu') }) : null;
        if (!ev.length && !mine.length && !all) return el('div', { class: 'wday empty-day' }, el('h3', { text: label }), unk || el('p', { class: 'small muted m-0', text: 'rien de notable' }));
        return el('div', { class: 'wday' }, el('h3', { text: label }), unk,
          ev.length ? el('ul', { class: 'wlist' }, ev.map((e) => el('li', { class: e.impact === 'High' ? 'hot' : '' }, el('span', { class: 'mono', text: hhmm(toDate(e.at)) }), el('span', null, el('b', { text: e.pays + ' ' }), e.titre, el('span', { class: 'small muted blk', text: ecoItem(e).detail }))))) : null,
          mine.length ? el('div', { class: 'row-gap mt-8' }, mine.map((x) => chip(x.symbol + ' · ' + (HOUR[x.heure] || 'résultats'), 'info'))) : null,
          all ? el('p', { class: 'small muted m-0 mt-8', text: all.total + ' publications sur toute la cote' }) : null);
      });
      return el('div', { class: 'week' }, out);
    });
    src.textContent = 'Calendrier économique : ' + (eco.erreur ? 'indisponible (' + eco.erreur + ')' : (eco.source || 'source inconnue') + (eco.stale && eco.source ? ' · ⚠ dernière valeur connue' + (eco.maj ? ' (' + stamp(eco.maj) + ')' : '') : '') + (eco.couvre_jusqu_au ? ' · publié jusqu\'au ' + DTF.dm.format(toDate(eco.couvre_jusqu_au + 'T12:00:00Z')) : '') + ' · ' + (eco.filtre || '')) + '. Résultats : ' + (res.manque ? 'clé Finnhub absente' : res.erreur ? 'indisponibles' : 'Finnhub') + '. Rien ici ne dépense de crédit Twelve Data.';
  });
  return card('c-12', 'Cette semaine', 'heure de Paris', box, src);
}

/* --- Journal */
BUILD.journal = () => [closuresCard(), disagreeCard(), confirmCard(), forwardCard(), decisionsCard()];
function closuresCard() {
  const box = waitBox('journal'), meta = el('span');
  const draw = (j) => {
    if (j.space !== apiSpace()) return;
    const rows = j.clotures || [], live = st.space === 'live';
    const nets = rows.map((r) => (live ? r.r_net : (r.result || {}).r_net)).filter(isNum);
    meta.textContent = rows.length ? rows.length + ' terminé' + (rows.length > 1 ? 's' : '') + ' · ' + rr(nets.reduce((a, b) => a + b, 0)) + ' nets cumulés · ' + nets.filter((x) => x > 0).length + ' gagnant' + (nets.filter((x) => x > 0).length > 1 ? 's' : '') : '';
    fill(box, [st.space, rows], () => {
      if (!rows.length) return empty('Aucun trade terminé dans l\'espace ' + spaceName(), [live ? 'Un trade terminé (objectif, stop, clôture ou sortie déclarée) s\'ajoute ici.' : 'Les recommandations closes ou pas déclenchées s\'ajoutent ici.']);
      return el('div', { class: 'jtable' }, el('div', { class: 'jrow jhead', 'aria-hidden': 'true' }, ['Terminé', 'Symbole', 'R nets', 'Raison', 'Euros'].map((t) => el('span', { text: t }))),
        rows.map((r) => {
          if (live) {
            return el('div', { class: 'jrow' }, el('span', { class: 'soft j-d', text: stamp(r.closed_at) }), el('b', { class: 'mono j-s', text: r.symbol }),
              el('span', { class: 'mono j-r ' + (isNum(r.r_net) ? (r.r_net >= 0 ? 'up' : 'down') : 'muted'), text: rr(r.r_net), title: 'brut ' + rr(r.r) }),
              el('span', { class: 'chips j-c' }, chip((r.raison || '?') + (r.corrige ? ' · corrigé' : ''), r.raison === 'stop' ? 'down' : String(r.raison || '').startsWith('tp') ? 'up' : ''), r.verdict ? verdictChip(r.verdict) : null, r.against_jarvis ? chip('contre l\'avis de JARVIS', 'against') : null),
              el('span', { class: 'mono right j-e', text: eur(r.eur, true) }));
          }
          const res = r.result || {}, nd = r.status === 'pas_declenche';
          return el('div', { class: 'jrow' }, el('span', { class: 'soft j-d', text: stamp(res.closed_at || r.sent_at) }), el('b', { class: 'mono j-s', text: r.symbol }),
            el('span', { class: 'mono j-r ' + (isNum(res.r_net) ? (res.r_net >= 0 ? 'up' : 'down') : 'muted'), text: nd ? DASH : rr(res.r_net) }),
            el('span', { class: 'chips j-c' }, chip(r.type_fr, r.type === 'kill' ? 'warn' : 'up'), chip(nd ? 'pas déclenchée' : res.raison || r.status_fr, nd ? 'ghost' : 'info'), r.mes_trades ? chip('aussi dans Mes trades', 'mode') : null),
            el('span', { class: 'mono right j-e', text: nd ? DASH : eur(res.eur, true) }));
        }));
    });
  };
  onData('journal', draw);
  return card('c-12', 'Trades terminés · ' + spaceName(), meta, box);
}
function groupLine(name, g, hint) {
  if (!g) return null;
  return el('div', { class: 'dg' }, el('span', { class: 'soft', text: name }), el('span', { class: 'num', text: int(g.closed) + ' / ' + int(g.total) }),
    el('span', { class: 'num ' + (isNum(g.avg_r) ? (g.avg_r >= 0 ? 'up' : 'down') : 'muted'), text: isNum(g.avg_r) ? rr(g.avg_r) : DASH }),
    el('span', { class: 'small muted', text: (g.ci ? 'IC [' + nf(2).format(g.ci[0]) + ' ; ' + nf(2).format(g.ci[1]) + ']' : 'IC inconnu') + (hint ? ' · ' + hint : '') }));
}
function disagreeCard() {
  const box = waitBox('journal');
  onData('journal', (j) => fill(box, j.desaccords, (d) => (!d ? empty('Désaccords illisibles', []) : [
    el('div', { class: 'dgs' }, el('div', { class: 'dg dg-h', 'aria-hidden': 'true' }, ['', 'terminés / pris', 'moyenne nette par trade', ''].map((t) => el('span', { text: t }))),
      groupLine('Avec JARVIS (GO)', d.avec), groupLine('Contre (NO GO pris)', d.contre), groupLine('À ta taille, grade à 0 %', d.regle)),
    el('div', { class: 'small muted mt-8', text: 'Verdict seulement avec 30 trades sur 20 jours de chaque côté et un IC à 95 % qui exclut 0. Mes trades seulement (les KILL sont mesurés par leur journal).' })])));
  return card('c-6', 'Désaccords avec JARVIS', 'qui avait raison', box, textDetails('desaccords', 'Verdict complet (comme `!desaccords`)'));
}
const CONF_ROW = { 'confirmé': 'up', 'annulé': 'down', 'expiré': '', attente: 'info', repli: 'warn' };
function confirmCard() {
  const box = waitBox('journal');
  onData('journal', (j) => fill(box, j.confirmations, (c) => {
    if (!c) return empty('Confirmations illisibles', []);
    const today = c.jour && c.aujourd_hui ? Object.entries(c.aujourd_hui) : [];
    const rows = (c.lignes || []).slice(0, 10);
    const when = c.du_jour ? 'Aujourd\'hui :' : 'GO de la séance du ' + DTF.dm.format(toDate(c.jour + 'T12:00:00Z')) + ' (aucun aujourd\'hui pour l\'instant) :';
    return [today.length ? el('div', { class: 'row-gap' }, el('span', { class: 'small soft', text: when }), today.map(([sym, g]) => chip(sym + ' · ' + (CONF[g.status] || [g.status])[0], CONF_ROW[g.status] || ''))) : el('p', { class: 'small soft m-0', text: 'Aucun GO à confirmer aujourd\'hui.' }),
      rows.length ? el('div', { class: 'feed mt-8' }, rows.map((r) => el('div', null, el('b', { class: 'mono', text: r.symbol }), el('span', { class: 'mono ' + (isNum(r.confirme_r20) ? (r.confirme_r20 >= 0 ? 'up' : 'down') : 'muted'), text: isNum(r.confirme_r20) ? rr(r.confirme_r20) : isNum(r.confirme_r) ? rr(r.confirme_r) : DASH, title: 'R à 20 € (sinon R brut) de la politique confirmée' }), chip(r.status, CONF_ROW[r.status] || ''), el('span', { class: 'soft right', text: r.day })))) : null,
      el('div', { class: 'small muted mt-8', text: int(c.total) + ' GO mesurés depuis le début' })];
  }));
  return card('c-6', 'Confirmations des GO', 'ordre stop d\'achat au déclencheur', box, textDetails('confirmations', 'Bilan complet (comme `!confirmations`)'));
}
function forwardCard() {
  const box = waitBox('journal');
  onData('journal', (j) => fill(box, j.forward_test, (f) => {
    if (!f) return empty('Forward-test illisible', []);
    const g = (name, x) => (x ? el('div', { class: 'dg' }, el('span', { class: 'soft', text: name }), el('span', { class: 'num', text: int(x.n) + ' trades' }), el('span', { class: 'num ' + (x.avg_r >= 0 ? 'up' : 'down'), text: rr(x.avg_r) + ' par trade' }), el('span', { class: 'small muted', text: 'réussite ' + pct(x.win_rate * 100, false, 0) + (isNum(x.ci_low) ? ' · IC [' + nf(2).format(x.ci_low) + ' ; ' + nf(2).format(x.ci_high) + ']' : '') })) : null);
    return [el('p', { class: 'small soft m-0', text: int(f.total) + ' plans du scanner · ' + int(f.done) + ' terminés · ' + int(f.traded) + ' déclenchés' + (isNum(f.fill_rate) ? ' (' + pct(f.fill_rate * 100, false, 0) + ')' : '') }),
      f.overall ? el('div', { class: 'dgs mt-8' }, g('Tous (GO et NO GO)', f.overall), g('GO seulement', f.go)) : el('p', { class: 'small muted', text: 'Aucun plan déclenché et terminé pour l\'instant.' }),
      el('div', { class: 'small muted mt-8', text: 'Compte fictif Alpaca : GO et NO GO du scanner, 3 brackets, sans frais. Une mesure de critères, pas des recommandations.' })];
  }));
  return card('c-6', 'Forward-test', 'compte fictif Alpaca', box, textDetails('bilan', 'Bilan complet (comme `!bilan`)'));
}
function decisionsCard() {
  const box = waitBox('journal');
  onData('journal', (j) => fill(box, j.decisions, (ds) => (!ds || !ds.length ? empty('Aucune décision enregistrée', ['Les propositions approuvées, refusées ou remplacées s\'ajoutent ici.'])
    : el('div', { class: 'props' }, ds.slice(0, 12).map((p) => el('div', { class: 'prop' }, el('div', null, el('div', { text: '#' + p.id + ' · ' + proposalText(p) }), el('div', { class: 'why', text: p.decided_at ? 'décidée ' + stamp(p.decided_at) : 'créée ' + stamp(p.created_at) })), propChip(p)))))));
  return card('c-6', 'Décisions', 'propositions de réglages et du labo', box);
}

/* --- Santé */
BUILD.sante = () => [sourcesCard(), jarvisCard(), loopsCard(), errorsCard()];
function sourcesCard() {
  const box = waitBox('health');
  onData('health', (h) => fill(box, h.sources, (s) => {
    const td = s.twelve_data || {}, lect = Object.entries(s.lectures || {});
    const tile = (name, statut, lines, cls) => el('div', { class: 'src' + (cls ? ' ' + cls : '') }, el('div', { class: 'src-h' }, el('b', { text: name }), statut), lines.filter(Boolean).map((t) => el('div', { class: 'small soft', text: t })));
    const tiles = [tile('Twelve Data · crédits', chip(td.depenses + ' / ' + td.par_jour, td.depenses >= td.par_jour ? 'down' : td.depenses >= td.par_jour * 0.8 ? 'warn' : 'up'), [s.twelve_data_configure ? 'clé présente' : 'clé absente', 'jour de New York ' + (td.jour_new_york || DASH), td.portee])];
    tiles.push(tile('Alpaca', chip(s.alpaca_configure ? 'clés présentes' : 'clés absentes', s.alpaca_configure ? 'up' : 'warn'), ['données IEX gratuites et compte fictif']));
    for (const [name, x] of lect) {
      const bad = x.statut === 'erreur';
      tiles.push(tile(name, chip(bad ? 'erreur' : 'ok', bad ? 'down' : 'up'), [
        x.dernier_succes ? 'dernière lecture réussie ' + stamp(x.dernier_succes) : 'aucune lecture réussie',
        (isNum(x.latence_ms) ? 'latence ' + int(x.latence_ms) + ' ms · ' : '') + int(x.ok) + ' lectures · ' + int(x.erreurs) + ' erreurs',
        x.erreur ? (bad ? '⚠ ' : 'dernière erreur ') + x.erreur + (x.derniere_erreur ? ' (' + stamp(x.derniere_erreur) + ')' : '') : null], bad ? 'bad' : ''));
    }
    return [el('div', { class: 'srcs' }, tiles), lect.length ? null : el('div', { class: 'small muted mt-8', text: 'Aucune lecture depuis le démarrage du serveur : chaque source apparaît à sa première lecture.' })];
  }));
  return card('c-8', 'Sources', 'statut prouvé par la dernière lecture réussie de ce processus', box);
}
function jarvisCard() {
  const box = el('dl', { class: 'kv', data: { wait: 'health' } }, st.health ? null : el('div', { class: 'skeleton short' }));
  onData('health', (h) => {
    const lab = h.labo, sv = h.planificateur.suivi, d = h.discord, disk = h.disque || {}, lag = (Date.now() - st.healthAt) / 1000;
    put(box, kvRow('API', 'répond · ' + stamp(h.generated_at), 'txt up'),
      kvRow('Suivi des trades', sv.at ? ago(sv.age_s + lag) + (sv.stale ? ' ⚠' : '') : 'jamais passé', sv.stale ? 'txt warn' : 'txt'),
      kvRow('Labo (battement)', lab.age_s == null ? (lab.raison || 'aucun battement') : (lab.etape ? lab.etape + ' · ' : '') + ago(lab.age_s + lag) + (lab.stale ? ' ⚠' : ''), lab.stale ? 'txt warn' : 'txt'),
      kvRow('Discord', (d.notifications ? 'notifications actives' : 'notifications coupées') + ' · ' + d.file_en_attente + ' en file' + (d.plus_ancien ? ' depuis ' + stamp(d.plus_ancien) : ''), d.file_en_attente ? 'txt warn' : 'txt'),
      kvRow('Disque', disk.erreur ? disk.erreur : isNum(disk.libre_go) ? nf(1).format(disk.libre_go) + ' Go libres sur ' + nf(1).format(disk.total_go) + ' (' + pct(disk.utilise_pct, false, 0) + ' utilisés)' : DASH, isNum(disk.utilise_pct) && disk.utilise_pct > 90 ? 'txt warn' : 'txt'));
  });
  return card('c-4', 'JARVIS', null, box, el('div', { class: 'row-gap mt-12' }, el('a', { class: 'btn primary', href: '/api/cockpit/diagnostics', download: '' }, 'Télécharger le diagnostic')),
    el('div', { class: 'small muted mt-8', text: 'JSON sans aucun secret ni valeur de variable d\'environnement (noms et présence des clés seulement).' }));
}
function loopsCard() {
  const box = waitBox('health');
  onData('health', (h) => fill(box, [h.planificateur.boucles, st.healthAt], ([b]) => {
    const rows = Object.entries(b || {}), lag = (Date.now() - st.healthAt) / 1000;
    if (!rows.length) return empty('Aucune boucle n\'a encore tourné', ['Le planificateur démarre avec le serveur ; chaque boucle s\'ajoute à son premier passage.']);
    return el('div', { class: 'feed loops' }, rows.sort((x, y) => (x[0] < y[0] ? -1 : 1)).map(([name, r]) => el('div', null,
      el('span', { class: 'grow', text: name }), chip(!r.ok ? 'échec' + (r.erreur ? ' · ' + r.erreur : '') : r.stale ? 'en retard' : 'ok', !r.ok ? 'down' : r.stale ? 'warn' : 'up'),
      el('span', { class: 'soft fv', text: 'toutes les ' + (r.every_s < 60 ? r.every_s + ' s' : dur(r.every_s / 60)) }), el('span', { class: 'soft right', text: (isNum(r.age_s) ? ago(r.age_s + lag) : DASH) + (isNum(r.duree_s) ? ' · ' + nf(1).format(r.duree_s) + ' s' : '') }))));
  }));
  return card('c-6', 'Boucles du planificateur', 'en retard au-delà de 3 passages (3 min au moins)', box);
}
function errorsCard() {
  const box = waitBox('health');
  onData('health', (h) => fill(box, h.erreurs_recentes, (es) => (!es || !es.length ? empty('Aucune erreur depuis le démarrage', ['Avertissements et erreurs du serveur web s\'affichent ici, clés masquées.'])
    : el('div', { class: 'errs' }, es.slice(0, 25).map((e) => el('div', { class: 'errl' }, el('div', { class: 'row-gap' }, chip(e.niveau, e.niveau === 'WARNING' ? 'warn' : 'down'), el('span', { class: 'mono small soft', text: e.module }), el('span', { class: 'small muted', text: stamp(e.at) })), el('div', { class: 'small', text: e.message })))))));
  return card('c-6', 'Erreurs récentes', 'serveur web, 50 dernières', box);
}

/* ---------- graphiques (TradingView Lightweight Charts 5.0.8, servi par JARVIS) */
let charts = [];
const COL = { up: '#3ddc97', down: '#ff5c7a', paper: '#9d8cff', live: '#ff7a45', grey: '#6f758d' };
const modeColor = (k = 0) => { const c = st.space === 'live' ? [255, 122, 69] : [157, 140, 255]; return 'rgba(' + c.join(',') + ',' + (k % 2 ? 0.6 : 1) + ')'; };
const PARIS_HM = new Intl.DateTimeFormat('fr-FR', { timeZone: TZ, hour: '2-digit', minute: '2-digit' });
const tickFmt = (time, type) => { const d = new Date(time * 1000); return type <= 2 ? DTF.dm.format(d) : PARIS_HM.format(d); };
const ohlc = (b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close });
const volOf = (b) => ({ time: b.time, value: b.v || 0, color: b.close >= b.open ? 'rgba(61,220,151,.28)' : 'rgba(255,92,122,.28)' });
const toBars = (rows) => (rows || []).map(([t, o, hi, lo, c, v]) => ({ time: t, open: o, high: hi, low: lo, close: c, v }));

class PositionZones {
  constructor(h) { this.h = h; this.view = new PositionZonesView(this); this.requestUpdate = null; }
  attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; this.requestUpdate = requestUpdate; }
  detached() { this.chart = null; this.series = null; }
  updateAllViews() { this.view.update(); }
  paneViews() { return [this.view]; }
}
class PositionZonesView {
  constructor(src) { this.src = src; this.items = []; }
  zOrder() { return 'bottom'; }
  update() {
    const s = this.src; this.items = [];
    if (!s.chart) return;
    const ts = s.chart.timeScale(), w = ts.width();
    for (const z of s.h.zoneData || []) {
      const x = z.t == null ? 0 : ts.timeToCoordinate(z.t);
      const y = (v) => (isNum(v) ? s.series.priceToCoordinate(v) : null);
      this.items.push({ x: x == null ? 0 : Math.max(0, x), w, e: y(z.entry), sl: y(z.stop), tps: z.targets.map(y) });
    }
  }
  renderer() {
    const items = this.items;
    return { draw(target) {
      target.useBitmapCoordinateSpace(({ context: g, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
        for (const c of items) {
          if (c.e == null) continue;
          const x = Math.round(c.x * hr), w = Math.round((c.w - c.x) * hr);
          const band = (a, b, fill, edge) => {
            if (a == null || b == null) return;
            const y1 = Math.round(Math.min(a, b) * vr), y2 = Math.round(Math.max(a, b) * vr);
            g.fillStyle = fill; g.fillRect(x, y1, w, y2 - y1);
            if (edge) { g.fillStyle = edge; g.fillRect(x, y1, Math.max(1, Math.round(2 * hr)), y2 - y1); }
          };
          let from = c.e;
          c.tps.forEach((tp, k) => { band(from, tp, 'rgba(61,220,151,' + (0.13 - k * 0.035).toFixed(3) + ')', k === 0 ? 'rgba(61,220,151,.55)' : null); if (tp != null) from = tp; });
          band(c.e, c.sl, 'rgba(255,92,122,.14)', 'rgba(255,92,122,.6)');
        }
      });
    } };
  }
}
function levelPrices(positions) {
  const out = [];
  for (const p of positions) {
    if (isNum(p.entry)) out.push(p.entry);
    if (p.stop && isNum(p.stop.price)) out.push(p.stop.price);
    for (const t of p.targets || []) if (isNum(t.price)) out.push(t.price);
  }
  return out;
}
function makeChart(host, o) {
  const L = window.LightweightCharts;
  if (!host) return null;
  if (!L || !L.createChart) { host.replaceChildren(el('div', { class: 'chart-empty', text: 'Bibliothèque de graphique non chargée : recharge la page.' })); return null; }
  const chart = L.createChart(host, {
    autoSize: true,
    layout: { background: { type: 'solid', color: 'transparent' }, textColor: '#a6acc2', fontFamily: 'Geist Mono, ui-monospace, monospace', fontSize: o.compact ? 11 : 12 },
    grid: { vertLines: { color: 'rgba(178,188,230,.05)' }, horzLines: { color: 'rgba(178,188,230,.05)' } },
    rightPriceScale: { borderColor: 'rgba(178,188,230,.12)', scaleMargins: { top: 0.08, bottom: o.volume ? 0.2 : 0.08 } },
    timeScale: { borderColor: 'rgba(178,188,230,.12)', timeVisible: o.tf !== '1j', secondsVisible: false, rightOffset: o.compact ? 3 : 8, tickMarkFormatter: tickFmt },
    crosshair: { mode: 0, vertLine: { color: 'rgba(178,188,230,.35)', labelBackgroundColor: '#262b3f' }, horzLine: { color: 'rgba(178,188,230,.35)', labelBackgroundColor: '#262b3f' } },
    localization: { locale: 'fr-FR', priceFormatter: price, timeFormatter: (t) => (typeof t === 'number' ? DTF.full.format(new Date(t * 1000)) : String(t)) },
    handleScroll: !o.compact, handleScale: !o.compact,
  });
  const h = { chart, host, group: o.group || 'view', sym: o.sym, tf: o.tf, compact: !!o.compact, lines: new Map(), positions: [], bars: [], zoneData: [] };
  h.cs = chart.addSeries(L.CandlestickSeries, { upColor: COL.up, downColor: COL.down, borderVisible: false, wickUpColor: COL.up, wickDownColor: COL.down,
    autoscaleInfoProvider: (orig) => { const r = orig(); if (!r || !r.priceRange || !h.positions.length) return r; let mn = r.priceRange.minValue, mx = r.priceRange.maxValue; for (const v of levelPrices(h.positions)) { mn = Math.min(mn, v); mx = Math.max(mx, v); } return { priceRange: { minValue: mn, maxValue: mx }, margins: r.margins }; } });
  if (o.volume) {
    h.vs = chart.addSeries(L.HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: '', lastValueVisible: false, priceLineVisible: false });
    h.vs.priceScale().applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
  }
  h.markers = L.createSeriesMarkers ? L.createSeriesMarkers(h.cs, []) : null;
  h.zones = new PositionZones(h);
  if (h.cs.attachPrimitive) h.cs.attachPrimitive(h.zones);
  h.eta = el('div', { class: 'eta-marks', 'aria-hidden': 'true' }); host.append(h.eta);
  setBars(h, o.bars);
  setPositions(h, o.positions || []);
  const n = h.bars.length;
  if (o.compact) chart.timeScale().setVisibleLogicalRange({ from: n - 70, to: n + 2 });
  else if (h.positions.length) chart.timeScale().setVisibleLogicalRange({ from: n - 130, to: n + 8 });
  else chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 150), to: n + 8 });
  chart.timeScale().subscribeVisibleLogicalRangeChange(() => drawEta(h));
  h.ro = new ResizeObserver(() => { drawEta(h); if (!h.compact) relabel(h); });
  h.ro.observe(host);
  charts.push(h);
  return h;
}
function setBars(h, rows) {
  const bars = toBars(rows), old = h.bars;
  const at = bars.length && old.length ? old.findIndex((b) => b.time === bars[0].time) : -1;
  if (at >= 0 && bars[bars.length - 1].time >= old[old.length - 1].time) {
    const lastOld = old[old.length - 1].time;
    for (const b of bars) if (b.time >= lastOld) { h.cs.update(ohlc(b)); if (h.vs) h.vs.update(volOf(b)); }
    h.bars = old.slice(0, at).concat(bars);
  } else {
    h.cs.setData(bars.map(ohlc)); if (h.vs) h.vs.setData(bars.map(volOf));
    h.bars = bars;
  }
}
function barTimeOf(h, iso) {
  const d = toDate(iso); if (!d || !h.bars.length) return null;
  const t = d.getTime() / 1000;
  if (t < h.bars[0].time) return null;
  for (let k = h.bars.length - 1; k >= 0; k--) if (h.bars[k].time <= t) return h.bars[k].time;
  return null;
}
function lineTitle(h, p, kind, t) {
  const mode = h.compact ? 'mini' : (h.host.clientWidth < 560 ? 'short' : 'full');
  if (kind === 'entry') return mode === 'full' ? 'Entrée' + (isNum(p.qty) ? ' · ' + qty(p.qty) : '') : '';
  if (kind === 'sl') {
    if (p.stop.kind === 'breakeven') return mode === 'mini' ? 'SL' : 'SL · point mort';
    if (mode === 'mini') return 'SL';
    if (mode === 'short') return 'SL ' + pct(dirPct(p, p.stop.price));
    const e = eurAt(p, p.stop.price, p.qty);
    return 'SL ' + pct(dirPct(p, p.stop.price)) + ' · ' + rr(rAt(p, p.stop.price)) + (e != null ? ' · ≈ ' + eur0(e, true) : '');
  }
  if (t.hit_at) return mode === 'mini' ? '' : t.name + ' ✓ ' + hfr(toDate(t.hit_at));
  if (t.role === 'repere') return mode === 'full' ? t.name + ' · repère' : '';
  if (mode === 'mini') return t.name;
  if (mode === 'short') return t.name + ' ' + pct(dirPct(p, t.price));
  const e = eurAt(p, t.price, legQty(p, t));
  const r = isNum(t.r_multiple) ? t.r_multiple : rAt(p, t.price);
  return t.name + ' · sortie ' + pct(dirPct(p, t.price)) + ' · ' + rr(r) + (e != null ? ' · ≈ ' + eur0(e, true) : '');
}
function lineSpecs(h, positions) {
  const specs = [];
  positions.forEach((p, k) => {
    if (isNum(p.entry)) specs.push([p.id + ':entry', { price: p.entry, color: modeColor(k), lineWidth: 1, lineStyle: 2, axisLabelVisible: !h.compact, title: lineTitle(h, p, 'entry') }]);
    if (p.stop && isNum(p.stop.price)) specs.push([p.id + ':sl', { price: p.stop.price, color: COL.down, lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: lineTitle(h, p, 'sl') }]);
    (p.targets || []).forEach((t, i) => {
      if (!isNum(t.price)) return;
      const hit = !!t.hit_at, exit = t.role === 'exit';
      specs.push([p.id + ':tp' + i, { price: t.price, color: hit ? COL.grey : exit ? COL.up : 'rgba(61,220,151,.5)', lineWidth: exit && !hit ? 2 : 1, lineStyle: exit && !hit ? 0 : 2, axisLabelVisible: exit || !h.compact, title: lineTitle(h, p, 'tp', t) }]);
    });
  });
  return specs;
}
function setPositions(h, positions) {
  h.positions = positions || [];
  const specs = lineSpecs(h, h.positions), keep = new Set();
  for (const [key, opt] of specs) {
    keep.add(key);
    const line = h.lines.get(key);
    if (line) line.applyOptions(opt); else h.lines.set(key, h.cs.createPriceLine(opt));
  }
  for (const [key, line] of h.lines) if (!keep.has(key)) { h.cs.removePriceLine(line); h.lines.delete(key); }
  h.zoneData = h.positions.filter((p) => isNum(p.entry)).map((p) => {
    const exits = (p.targets || []).filter((t) => t.role === 'exit' && isNum(t.price)).map((t) => t.price);
    return { t: barTimeOf(h, p.created_at), entry: p.entry, stop: p.stop && p.stop.kind !== 'breakeven' ? p.stop.price : null, targets: p.sortie_unique != null ? exits.slice(0, 1) : exits };
  });
  if (h.zones.requestUpdate) h.zones.requestUpdate();
  if (h.markers) {
    const mk = h.positions.map((p) => ({ time: barTimeOf(h, p.created_at), position: p.side === 'short' ? 'aboveBar' : 'belowBar', color: modeColor(), shape: p.side === 'short' ? 'arrowDown' : 'arrowUp' })).filter((m) => m.time != null).sort((a, b) => a.time - b.time);
    h.markers.setMarkers(mk);
  }
  drawEta(h);
}
function relabel(h) { const specs = new Map(lineSpecs(h, h.positions)); for (const [key, line] of h.lines) { const o = specs.get(key); if (o) line.applyOptions({ title: o.title }); } }
function drawEta(h) {
  if (!h.eta || !h.bars.length) return;
  const ts = h.chart.timeScale(), step = TF_S[h.tf] || 900, n = h.bars.length, last = h.bars[n - 1].time;
  const width = ts.width(), marks = [];
  for (const p of h.positions) {
    const t = nextTarget(p); if (!t || !t.eta_median) continue;
    const eta = toDate(t.eta_median).getTime() / 1000;
    let logical;
    if (eta > last) logical = n - 1 + (eta - last) / step;
    else { let k = n - 1; while (k > 0 && h.bars[k].time > eta) k--; logical = k; }
    // logicalToCoordinate ne prend que des index entiers (0 sinon) : interpolation entre deux bougies.
    const k = Math.floor(logical), x0 = ts.logicalToCoordinate(k), x1 = ts.logicalToCoordinate(k + 1);
    const x = x0 == null || x1 == null ? null : x0 + (logical - k) * (x1 - x0);
    if (x == null || x < 0) continue;
    const beyond = x > width - 2;  // au-delà du bord droit : épinglé au bord, avec une flèche
    const m = el('div', { class: 'eta-mark' + (t.late ? ' late' : '') + (x > width * 0.55 ? ' flip' : '') + (beyond ? ' beyond' : '') },
      el('span', { text: t.name + (t.late ? ' en retard' : ' ' + whenText(t.eta_median)) + (beyond ? ' →' : '') }));
    m.style.left = Math.round(beyond ? width - 2 : x) + 'px';
    marks.push(m);
  }
  h.eta.replaceChildren(...marks);
}
function destroyChart(h) { try { h.ro && h.ro.disconnect(); h.chart.remove(); } catch (_) { /* déjà retiré */ } charts = charts.filter((x) => x !== h); }
function destroyCharts(group) { for (const h of charts.slice()) if (!group || h.group === group) destroyChart(h); }

/* ---------- navigation */
function buildNav() {
  $('#brand-mark').append(icon('logo', 18, 2.2));
  $('#cta-palette').prepend(icon('search', 16, 2));
  $('#cta-help').prepend(icon('keys', 16, 2));
  $('#search-btn').prepend(icon('search', 16, 2));
  $('#pal-ico').append(icon('search', 18, 2));
  const nav = $('#nav');
  VIEWS.forEach((v, k) => nav.append(el('button', { type: 'button', data: { go: v[0] }, title: v[1] + ' (' + (k + 1) + ')' }, icon(v[0]), el('span', { class: 'lbl', text: v[1] }), kbd(String(k + 1)))));
  const tb = $('#tabbar');
  for (const id of ['cockpit', 'marches', 'positions', 'journal']) { const v = VIEWS.find((x) => x[0] === id); tb.append(el('button', { type: 'button', data: { go: id } }, icon(id, 20), v[1])); }
  tb.append(el('button', { type: 'button', data: { act: 'palette' } }, icon('more', 20), 'Plus'));
  $$('[data-modk]').forEach((k) => { k.textContent = MOD + ' K'; });
}
function moveInd() {
  const btn = $('#nav [data-go="' + st.view + '"]'), ind = $('#nav-ind');
  if (btn && btn.offsetParent) { ind.style.height = btn.offsetHeight + 'px'; ind.style.transform = 'translateY(' + btn.offsetTop + 'px)'; }
}
function go(view, opts = {}) {
  if (!BUILD[view]) return;
  const changed = view !== st.view;
  st.view = view;
  try { localStorage.setItem('cockpit.view', view); } catch (_) { /* stockage indisponible */ }
  if (location.hash !== '#' + view) history.replaceState(null, '', '#' + view);
  const meta = VIEWS.find((v) => v[0] === view);
  $('#view-h').textContent = meta[1]; $('#view-sub').textContent = meta[2];
  $$('#nav [data-go], #tabbar [data-go]').forEach((b) => b.setAttribute('aria-current', b.dataset.go === view ? 'page' : 'false'));
  $$('.view').forEach((s) => { s.hidden = s.dataset.view !== view; });
  if (changed || opts.force) render(true);
  moveInd();
  heavyPoll.now();
}

/* ---------- espaces : Papier à l'ouverture, « Mes trades » derrière un appui maintenu */
/* applySpace ne demande rien : ses appelants vers « Mes trades » viennent toujours d'un appui maintenu. */
function applySpace(sp) {
  if (sp === st.space) return;
  st.space = sp; st.unlocked = sp === 'live'; root.dataset.space = sp;
  $('#sp-paper').setAttribute('aria-pressed', String(sp === 'paper')); $('#sp-trades').setAttribute('aria-pressed', String(sp === 'live'));
  const sw = $('.sweep'); if (!reduced()) { sw.classList.remove('go'); void sw.offsetWidth; sw.classList.add('go'); }
  st.state = null; st.posCharts = null; st.journal = null; st.instruments = null; st.prevPos = null; st.chartSym = null;
  render(true); if (st.drawer) closeDrawer();
  toast(sp === 'live' ? 'Mes trades : tes trades réels déclarés à JARVIS. Le cockpit n\'envoie aucun ordre.' : 'Papier : les recommandations de JARVIS suivies telles qu\'envoyées.', 'mode');
  statePoll.now(); heavyPoll.now();
}
function setSpace(sp) {
  if (sp === st.space) return;
  if (sp === 'live') {
    holdConfirm({ title: 'Entrer dans « Mes trades »', text: 'Tu verras tes trades réels, passés par toi chez IBKR et déclarés à JARVIS. Les actions de cet espace modifient ton journal ; le cockpit n\'envoie jamais d\'ordre.', label: 'Maintenir pour entrer', run: async () => applySpace('live') });
  } else applySpace(sp);
}

/* ---------- confirmation par appui maintenu (1,1 s ; souris, doigt, Entrée ou Espace) */
let modalBack = null;
function holdButton(label, red, run) {
  const b = el('button', { class: 'hold' + (red ? ' red' : ''), type: 'button' }, el('span', { text: label }));
  let p = 0, raf = 0, dir = 0, last = 0, done = false;
  const loop = (t) => {
    const dt = last ? t - last : 16; last = t;
    p = Math.max(0, Math.min(1, p + dir * dt / (dir > 0 ? 1100 : 300)));
    b.style.setProperty('--p', p);
    if (p >= 1 && !done) { done = true; raf = 0; Promise.resolve(run(b)).finally(() => { done = false; p = 0; b.style.setProperty('--p', 0); }); return; }
    if (p > 0 || dir > 0) raf = requestAnimationFrame(loop); else { raf = 0; last = 0; }
  };
  const start = () => { if (done || b.disabled) return; dir = 1; if (!raf) { last = 0; raf = requestAnimationFrame(loop); } };
  const stop = () => { dir = -1; if (!raf) { last = 0; raf = requestAnimationFrame(loop); } };
  b.addEventListener('pointerdown', (e) => { e.preventDefault(); try { b.setPointerCapture(e.pointerId); } catch (_) { /* déjà capturé */ } start(); });
  b.addEventListener('pointerup', stop); b.addEventListener('pointercancel', stop); b.addEventListener('lostpointercapture', stop);
  b.addEventListener('contextmenu', (e) => e.preventDefault());
  b.addEventListener('keydown', (e) => { if ((e.key === 'Enter' || e.key === ' ') && !e.repeat) { e.preventDefault(); start(); } });
  b.addEventListener('keyup', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); stop(); } });
  return b;
}
function holdConfirm(o) {
  const m = $('#modal'); modalBack = document.activeElement;
  const h = holdButton(o.label, o.red, async (btn) => {
    btn.disabled = true;
    try { await o.run(); closeModal(); } catch (e) { toast(e.message, 'bad'); } finally { btn.disabled = false; }
  });
  m.replaceChildren(el('h2', { id: 'modal-h', text: o.title }), el('p', { text: o.text }),
    el('div', { class: 'acts' }, el('button', { class: 'btn', type: 'button', onclick: closeModal }, 'Annuler ', kbd('Échap')), h));
  $('#scrim').classList.add('on'); m.classList.add('on'); setTimeout(() => h.focus(), 30);
}
function closeModal() {
  const m = $('#modal'); if (!m.classList.contains('on')) return;
  m.classList.remove('on');
  if (!$('#sheet').classList.contains('on')) $('#scrim').classList.remove('on');
  if (modalBack && modalBack.isConnected) modalBack.focus();
}

/* ---------- fenêtres de formulaire et de résultat */
let sheetBack = null;
function openSheet(title, ...body) {
  const s = $('#sheet'); if (!s.classList.contains('on')) sheetBack = document.activeElement;
  put(s, el('h2', { id: 'sheet-h', text: title }), body);
  $('#scrim').classList.add('on'); s.classList.add('on');
  const f = s.querySelector('input, select, button'); if (f) setTimeout(() => f.focus(), 30);
}
function closeSheet() {
  const s = $('#sheet'); if (!s.classList.contains('on')) return;
  s.classList.remove('on'); destroyCharts('sheet');
  if (!$('#modal').classList.contains('on')) $('#scrim').classList.remove('on');
  if (sheetBack && sheetBack.isConnected) sheetBack.focus();
}
const field = (label, input, hint) => el('label', { class: 'field' }, label, input, hint ? el('span', { class: 'hint', text: hint }) : null);
const input = (name, props = {}) => el('input', Object.assign({ name, autocomplete: 'off', spellcheck: 'false' }, props));
function select(name, opts, value) { return el('select', { name }, opts.map(([v, t]) => { const o = el('option', { value: v, text: t }); if (v === value) o.selected = true; return o; })); }
const numOrNull = (v) => { v = String(v || '').trim().replace(',', '.'); return v ? Number(v) : null; };
const listOf = (v) => String(v || '').replace(/,(?=\d{3}\b)/g, '').split(/[\s;]+/).map((x) => x.replace(',', '.')).filter(Boolean).map(Number);
function textBlock(text) { return el('pre', { class: 'pre', text: String(text || '').replace(/\*\*/g, '').replace(/^-# /gm, '') }); }

function planForm(pre = {}) {
  const f = el('form', { class: 'form' });
  const sym = input('symbol', { required: true, placeholder: 'NVDA, BTC…', value: pre.symbol || '', autocapitalize: 'characters' });
  const market = select('market', [['', 'Auto'], ['stock', 'Action'], ['crypto', 'Crypto']], pre.market || '');
  const side = select('side', [['long', 'Long'], ['short', 'Short']], 'long');
  const entry = input('entry', { inputmode: 'decimal', placeholder: 'vide = dernier cours' });
  const stop = input('stop', { inputmode: 'decimal', placeholder: 'vide = structure' });
  const tps = input('targets', { inputmode: 'decimal', placeholder: 'vide = résistances' });
  const out = el('div', { class: 'mt-16' });
  const submit = el('button', { class: 'btn primary', type: 'submit' }, 'Calculer le plan');
  f.append(el('div', { class: 'grid3' }, field('Symbole', sym), field('Marché', market), field('Sens', side)),
    el('div', { class: 'grid3' }, field('Entrée', entry), field('Stop', stop), field('Objectifs', tps, 'séparés par un espace')),
    el('div', { class: 'acts' }, el('span', { class: 'muted small grow', text: 'Le calcul lit l\'historique (crédits Twelve Data pour une action).' }), el('button', { class: 'btn', type: 'button', onclick: closeSheet }, 'Fermer'), submit), out);
  f.addEventListener('submit', async (e) => {
    e.preventDefault();
    const args = { symbol: sym.value.trim().toUpperCase(), side: side.value, entry: numOrNull(entry.value), stop: numOrNull(stop.value), targets: listOf(tps.value) };
    if (market.value) args.market = market.value;
    submit.disabled = true; submit.textContent = 'Calcul…';
    out.replaceChildren(el('div', { class: 'skeleton' }), el('p', { class: 'muted small', text: 'Historique, délais rejoués et checklist en cours…' }));
    try { const r = await action('plan', args); st.lastPlan = { args, plan: r.plan }; keepPlan(r.plan); out.replaceChildren(planView(r.plan, args)); }
    catch (err) { out.replaceChildren(el('p', { class: 'err', text: err.message })); }
    finally { submit.disabled = false; submit.textContent = 'Calculer le plan'; }
  });
  openSheet('Nouveau plan', f);
  if (pre.symbol) setTimeout(() => entry.focus(), 40);
}
function planView(p, args) {
  const sc = p.score || {}, g = p.grade, live = st.space === 'live';
  const note = input('note', { placeholder: 'Note (facultatif)', maxlength: 300 });
  const take = async () => {
    const r = await action('take', Object.assign({}, args, { note: note.value.trim() || null }));
    toast(r.text, 'good'); closeSheet(); statePoll.now(); heavyPoll.now();
  };
  const takeBtn = live
    ? el('button', { class: 'btn primary', type: 'button', onclick: async (e) => { e.target.disabled = true; try { await take(); } catch (err) { toast(err.message, 'bad'); } finally { e.target.disabled = false; } } }, 'Je prends ce trade')
    : el('button', { class: 'btn primary', type: 'button', onclick: () => holdConfirm({ title: 'Déclarer ' + p.symbol + ' dans « Mes trades »', text: 'Ouvre « Mes trades » et y déclare ce trade (passé par toi chez IBKR). JARVIS annoncera les TP et SL touchés sur Discord ; aucun ordre n\'est envoyé.', label: 'Maintenir pour déclarer', run: async () => { applySpace('live'); await take(); } }) }, 'Je prends ce trade…');
  return el('div', { class: 'plan' },
    el('div', { class: 'plan-h' }, chip(p.side === 'short' ? 'SHORT' : 'LONG', p.side === 'short' ? 'down' : 'up'), el('span', { class: 'sym', text: p.symbol }),
      sc.decision ? verdictChip(sc.go ? 'GO' : 'NO GO') : null, isNum(sc.composite) ? chip(nf(1).format(sc.composite) + '/10 · priorité', 'info') : null,
      p.checklist ? chip(p.checklist.verdict, '') : null),
    el('div', { class: 'mono small', text: 'Entrée ' + price(p.entry) + ' · SL ' + price(p.stop) + ' (' + pct(p.stop_pct) + ') · ' + (p.stop_source || '') }),
    g ? el('div', { class: 'small soft', text: 'Grade ' + g.grade + ' · risque proposé ' + pct(g.risque_propose, false) + ' (' + (g.raison || '') + ')' }) : null,
    el('div', { class: 'tps' }, (p.targets || []).map((t, i) => el('div', { class: 'tp' + (p.sortie_unique == null || p.sortie_unique === i ? ' exit' : '') },
      el('span', { class: 'n', text: t.name + (p.sortie_unique != null ? (p.sortie_unique === i ? ' · SORTIE' : ' · REPÈRE') : '') }),
      el('span', { class: 'p', text: price(t.price) }), el('span', { class: 'muted', text: pct(t.pct) + ' · ' + rr(t.r_multiple, false) }),
      el('span', { text: t.eta_median ? '⏱ ' + whenText(t.eta_median) + (t.eta_latest ? ' · au plus tard ' + whenText(t.eta_latest, '') : '') : 'délai non estimé' }),
      isNum(t.hit_rate) ? el('span', { class: 'muted', text: Math.round(t.hit_rate * 100) + ' % avant le stop' }) : null))),
    el('details', null, el('summary', { class: 'small soft', text: 'Texte complet (comme sur Discord)' }), textBlock(p.text)),
    el('div', { class: 'acts' }, note, takeBtn));
}
function closeForm(p) {
  const px = input('price', { inputmode: 'decimal', placeholder: 'vide = dernier cours suivi (' + price(p.last_price) + ')' });
  const txt = el('p', { class: 'soft' });
  const upd = () => { const v = numOrNull(px.value), at = isNum(v) ? v : p.last_price; const r = rAt(p, at); txt.textContent = 'Clôturer ' + p.symbol + ' à ' + price(at) + ' : le trade passe dans le journal à ≈ ' + rr(r) + ' brut ; aucun ordre n\'est envoyé à IBKR.'; };
  px.addEventListener('input', upd); upd();
  const btn = holdButton('Maintenir pour clôturer', true, async (b) => {
    b.disabled = true;
    try { const r = await action('close', { id: p.id, price: numOrNull(px.value) }); toast(r.text, 'good'); closeSheet(); statePoll.now(); heavyPoll.now(); }
    catch (e) { toast(e.message, 'bad'); } finally { b.disabled = false; }
  });
  openSheet('Clôturer ' + p.symbol, el('div', { class: 'form' }, field('Prix de sortie', px), txt, el('div', { class: 'acts' }, el('button', { class: 'btn', type: 'button', onclick: closeSheet }, 'Annuler'), btn)));
}
function exitForm(symbol, market) {
  const sym = input('symbol', { value: symbol || '', placeholder: 'NVDA', autocapitalize: 'characters' });
  const px = input('price', { inputmode: 'decimal', placeholder: 'ton prix réel de sortie' });
  const btn = holdButton('Maintenir pour enregistrer', true, async (b) => {
    b.disabled = true;
    try { const r = await action('exit', { symbol: sym.value.trim().toUpperCase(), price: numOrNull(px.value) }); toast(r.text, 'good'); closeSheet(); statePoll.now(); heavyPoll.now(); }
    catch (e) { toast(e.message, 'bad'); } finally { b.disabled = false; }
  });
  openSheet('J\'ai vendu à…', el('div', { class: 'form' }, el('div', { class: 'grid2' }, field('Symbole', sym), field('Prix', px)),
    el('p', { class: 'soft small', text: 'Comme `!sortie` : ferme à ce prix le dernier trade ouvert de ce symbole ; s\'il est déjà terminé, corrige le prix de sa dernière sortie (le prix du suivi reste gardé). Aucun ordre envoyé.' }),
    el('div', { class: 'acts' }, el('button', { class: 'btn', type: 'button', onclick: closeSheet }, 'Annuler'), btn)));
}
function reopenForm() {
  const sym = input('symbol', { placeholder: 'MRVL', autocapitalize: 'characters' }), entry = input('entry', { inputmode: 'decimal', placeholder: 'ton prix d\'entrée' });
  const stop = input('stop', { inputmode: 'decimal', placeholder: 'vide = structure' }), tps = input('targets', { inputmode: 'decimal', placeholder: 'vide = résistances' });
  const side = select('side', [['long', 'Long'], ['short', 'Short']], 'long');
  const btn = holdButton('Maintenir pour rouvrir', true, async (b) => {
    b.disabled = true;
    try { const r = await action('reopen', { symbol: sym.value.trim().toUpperCase(), side: side.value, entry: numOrNull(entry.value), stop: numOrNull(stop.value), targets: listOf(tps.value) }); toast(r.text, 'good'); closeSheet(); statePoll.now(); }
    catch (e) { toast(e.message, 'bad'); } finally { b.disabled = false; }
  });
  openSheet('Rouvrir un trade arrêté', el('div', { class: 'form' }, el('div', { class: 'grid3' }, field('Symbole', sym), field('Sens', side), field('Entrée', entry)), el('div', { class: 'grid2' }, field('Stop', stop), field('Objectifs', tps)),
    el('p', { class: 'soft small', text: 'Comme `!rouvre` : le stop suivi a été touché mais il n\'a pas été exécuté chez IBKR et tu tiens toujours la position. L\'ancienne sortie est effacée du journal.' }),
    el('div', { class: 'acts' }, el('button', { class: 'btn', type: 'button', onclick: closeSheet }, 'Annuler'), btn)));
}
function deleteTrade(p) {
  holdConfirm({ title: 'Supprimer ' + p.symbol + ' du journal', text: 'Le trade disparaît du cockpit et du journal (ses mesures aussi). Aucun ordre n\'est envoyé à IBKR.', label: 'Maintenir pour supprimer', red: true,
    run: async () => { const r = await action('delete', { id: p.id }); toast(r.text + ' · ' + p.symbol, 'good'); statePoll.now(); heavyPoll.now(); } });
}
async function runAdvice() {
  toast('Conseils en cours : lecture des bougies de chaque trade…', 'info');
  try {
    const r = await action('advice');
    const list = (r.advices || []).map((a) => el('div', { class: 'advice' }, el('div', { class: 'h' }, el('b', { text: a.symbol || a.id }), a.error ? chip('impossible', 'down') : chip(a.decision, /SORTIR/.test(a.decision) ? 'down' : /ALL/.test(a.decision) ? 'warn' : 'up')),
      a.error ? el('div', { class: 'err', text: a.error }) : el('div', { class: 'mono small', text: a.orders || '' }), a.why ? el('div', { class: 'soft small', text: a.why }) : null));
    openSheet('Conseils en prix', list.length ? el('div', { class: 'sig-list' }, list) : empty(r.text, []), el('div', { class: 'acts' }, el('button', { class: 'btn', type: 'button', onclick: closeSheet }, 'Fermer')));
    statePoll.now();
  } catch (e) { toast(e.message, 'bad'); }
}
async function opinion(sym, market) {
  if (!sym) return;
  openSheet('Avis JARVIS · ' + sym, el('div', { class: 'skeleton' }), el('p', { class: 'muted small', text: 'Lecture des bougies et des niveaux…' }));
  try { const r = await action('opinion', { symbol: sym, market: market || undefined }); openSheet('Avis JARVIS · ' + sym, textBlock(r.text), el('div', { class: 'acts' }, el('button', { class: 'btn', type: 'button', onclick: closeSheet }, 'Fermer'))); }
  catch (e) { openSheet('Avis JARVIS · ' + sym, el('p', { class: 'err', text: e.message }), el('div', { class: 'acts' }, el('button', { class: 'btn', type: 'button', onclick: closeSheet }, 'Fermer'))); }
}
async function company(sym) {
  openSheet('Fiche société · ' + sym, el('div', { class: 'skeleton' }));
  try { const r = await action('company', { symbol: sym }); openSheet('Fiche société · ' + sym, textBlock(r.text), el('div', { class: 'acts' }, el('button', { class: 'btn', type: 'button', onclick: closeSheet }, 'Fermer'))); }
  catch (e) { openSheet('Fiche société · ' + sym, el('p', { class: 'err', text: e.message }), el('div', { class: 'acts' }, el('button', { class: 'btn', type: 'button', onclick: closeSheet }, 'Fermer'))); }
}
async function scanNow() {
  toast('Scanner lancé : il peut prendre plusieurs minutes (rien n\'est enregistré, aucun ordre fictif).', 'info');
  try { const r = await action('scan_now'); openSheet('Scanner · maintenant', textBlock(r.text), el('div', { class: 'acts' }, el('button', { class: 'btn', type: 'button', onclick: closeSheet }, 'Fermer'))); }
  catch (e) { toast(e.message, 'bad'); }
}
function approveProposal(p) {
  holdConfirm({ title: 'Approuver la proposition #' + p.id, text: proposalText(p) + '. Seule l\'approbation change le réglage ; elle ne s\'annule pas depuis le cockpit.', label: 'Maintenir pour approuver',
    run: async () => { const r = await action('proposal_approve', { id: p.id }); toast(r.text, 'good'); statePoll.now(); } });
}
async function refuseProposal(p) {
  try { const r = await action('proposal_refuse', { id: p.id }); toast(r.text, 'info'); statePoll.now(); } catch (e) { toast(e.message, 'bad'); }
}
function killmode(on, viaUndo) {
  const run = async () => {
    const r = await action(on ? 'killmode_on' : 'killmode_off');
    toast(r.text, 'mode', viaUndo ? null : () => killmode(!on, true));
    statePoll.now();
  };
  if (viaUndo) { run().catch((e) => toast(e.message, 'bad')); return; }
  holdConfirm({ title: on ? 'Allumer Killmode' : 'Éteindre Killmode', text: on ? 'Comme `!kill on` : JARVIS pourra analyser et proposer des KILL (`!kill TICKER`, `!kill scan`). Les ordres restent les tiens.' : 'Comme `!kill off` : plus d\'analyse ni de KILL proposé. Le journal de Killmode reste lisible.', label: on ? 'Maintenir pour allumer' : 'Maintenir pour éteindre', red: !on, run });
}
function veto() {
  holdConfirm({ title: 'Poser un veto', text: 'Comme `!veto` : le labo annule la dernière décision du council à son prochain passage (méthode promue remise à l\'ancienne, challenger retiré ou règles approuvées refusées) et l\'annonce sur Discord.', label: 'Maintenir pour poser le veto', red: true,
    run: async () => { const r = await action('veto'); toast(r.text, 'mode'); } });
}

/* ---------- inspecteur */
async function openDrawer(sym, market) {
  st.drawer = { sym, market }; const d = $('#drawer');
  d.replaceChildren(el('div', { class: 'drawer-h' }, monogram(sym, market), el('div', { class: 'grow' }, el('div', { class: 'sym', text: sym })), el('button', { class: 'btn sm', type: 'button', onclick: closeDrawer, 'aria-label': 'Fermer (Échap)' }, 'Fermer ', kbd('Échap'))), el('div', { class: 'drawer-b' }, el('div', { class: 'skeleton' })));
  d.classList.add('on');
  try {
    const r = await api('/api/cockpit/instrument/' + encodeURIComponent(sym) + '?space=' + apiSpace() + '&tf=15m' + (market ? '&market=' + market : ''), { timeout: 15000 });
    if (!st.drawer || st.drawer.sym !== sym) return;
    st.drawer.data = r; drawDrawer();
  } catch (e) { const b = $('.drawer-b', d); if (b) b.replaceChildren(empty('Données indisponibles', [e.message])); }
}
function drawDrawer() {
  const r = st.drawer.data, d = $('#drawer'), sess = st.state ? st.state.market_sessions.nyse : null;
  destroyCharts('drawer');
  const last = r.bars.length ? r.bars[r.bars.length - 1] : null, live = st.space === 'live';
  const p = r.positions[0];
  const body = el('div', { class: 'drawer-b' },
    el('div', null, el('div', { class: 'big num', id: 'dr-price', text: last ? price(last[4]) : DASH }),
      el('div', { class: 'hero-sub' }, el('span', { text: r.market === 'crypto' ? 'Crypto · 24/7' : 'NYSE · ' + (sess ? (ETATS[sess.etat] || sess.etat).toLowerCase() + ' · ' + sessionNext(sess) : DASH) }))),
    p ? el('div', { class: 'dchart', id: 'dchart', 'aria-label': r.symbol + ' avec SL et TP' }) : (r.bars.length ? sparkSvg(r.bars.slice(-60).map((b) => b[4]), 400, 70) : null),
    p ? card('', 'Trade · ' + spaceName(), r.positions.length > 1 ? r.positions.length + ' trades' : null, el('dl', { class: 'kv' },
      [['Sens', p.side === 'short' ? 'short' : 'long'], ['Quantité', isNum(p.qty) ? qty(p.qty) : 'inconnue'], ['Entrée', price(p.entry)], ['Stop suivi', p.stop ? price(p.stop.price) + (p.stop.kind === 'breakeven' ? ' (point mort)' : '') : 'inconnu'],
        ['Objectifs', (p.targets || []).map((t) => t.name + ' ' + price(t.price) + (t.hit_at ? ' ✓' : '')).join(' · ') || DASH], ['P&L latent', p.unrealized ? rr(p.unrealized.r) + ' · ' + eur(p.unrealized.eur, true) : 'inconnu'], ['Pris', stamp(p.created_at)]]
        .map(([k, v]) => el('div', null, el('dt', { text: k }), el('dd', { text: v })))))
      : card('', 'Trade · ' + spaceName(), null, el('p', { class: 'muted small m-0', text: 'Aucun trade ouvert sur ' + r.symbol + ' dans l\'espace ' + spaceName() + '.' })),
    r.plan_du_jour ? card('', 'Plan du jour', 'scanner', el('div', { class: 'row-gap' }, verdictChip(r.plan_du_jour.verdict), chip(isNum(r.plan_du_jour.composite) ? nf(1).format(r.plan_du_jour.composite) + ' · priorité' : 'sans note', 'info')),
      el('div', { class: 'small soft mono mt-8', text: 'entrée ' + price(r.plan_du_jour.entry) + ' · stop ' + price(r.plan_du_jour.stop) })) : null,
    card('', 'Données', null, el('dl', { class: 'kv' }, el('div', null, el('dt', { text: 'Source' }), el('dd', { text: r.source || DASH })), el('div', null, el('dt', { text: 'Dernière bougie' }), el('dd', { text: r.as_of ? stamp(r.as_of) : DASH })),
      r.manque ? el('div', null, el('dt', { text: 'Manque' }), el('dd', { text: r.manque })) : null)),
    el('div', { class: 'row-gap' },
      el('button', { class: 'btn primary', type: 'button', data: { chart: r.symbol, market: r.market } }, 'Voir au graphique'),
      el('button', { class: 'btn', type: 'button', onclick: () => opinion(r.symbol, r.market) }, 'Avis JARVIS'),
      r.market === 'stock' ? el('button', { class: 'btn', type: 'button', onclick: () => company(r.symbol) }, 'Fiche société') : null,
      live && p ? el('button', { class: 'btn danger', type: 'button', onclick: () => closeForm(p) }, 'Clôturer…') : null));
  const head = $('.drawer-h', d); d.replaceChildren(head, body);
  if (p && r.bars.length) setTimeout(() => { if (st.drawer && st.drawer.data === r) makeChart($('#dchart'), { bars: r.bars, positions: r.positions, tf: '15m', compact: true, group: 'drawer', sym: r.symbol }); }, 60);
}
function bindDrawer() {
  const r = st.drawer && st.drawer.data; if (!r) return;
  const h = charts.find((x) => x.group === 'drawer'); if (!h) return;
  const mine = (st.state.positions_open || []).filter((p) => p.symbol === r.symbol);
  if (mine.length) setPositions(h, mine);
}
function closeDrawer() { st.drawer = null; $('#drawer').classList.remove('on'); destroyCharts('drawer'); }

/* ---------- palette de commandes */
let palItems = [], palSel = 0, palBack = null;
function commands() {
  const c = [], s = st.state, live = st.space === 'live';
  VIEWS.forEach((v, k) => c.push({ g: 'Navigation', t: 'Aller à ' + v[1], ico: icon(v[0], 15), h: [String(k + 1)], run: () => go(v[0]) }));
  for (const p of (s && s.positions_open) || []) c.push({ g: 'Trades ouverts · ' + spaceName(), t: p.symbol + ' au graphique avec SL et TP', ico: p.symbol.slice(0, 3), sub: price(p.last_price), run: () => { st.chartSym = p.symbol; st.chartMarket = p.market; go('marches', { force: true }); } });
  for (const i of ((st.instruments && st.instruments.instruments) || [])) c.push({ g: 'Instruments', t: i.symbol + ' · ' + (i.market === 'crypto' ? 'crypto' : 'action'), ico: i.symbol.slice(0, 3), sub: price(i.price), run: () => openDrawer(i.symbol, i.market) });
  c.push({ g: 'Actions', t: 'Nouveau plan', ico: '+', h: ['+'], run: () => planForm() });
  c.push({ g: 'Actions', t: 'Trade suivant au graphique', ico: '→', h: ['N'], run: () => nextEngaged(1) });
  c.push({ g: 'Actions', t: live ? 'Revenir au Papier' : 'Entrer dans « Mes trades »', ico: '⇄', h: ['M'], danger: !live, run: () => setSpace(live ? 'paper' : 'live') });
  if (live) {
    c.push({ g: 'Actions', t: 'Demander les conseils en prix', ico: '🧭', run: runAdvice });
    c.push({ g: 'Actions', t: 'J\'ai vendu à… (déclarer une sortie)', ico: '✎', danger: true, run: () => exitForm('', null) });
    c.push({ g: 'Actions', t: 'Rouvrir un trade arrêté', ico: '♻', danger: true, run: reopenForm });
  }
  c.push({ g: 'Actions', t: 'Lancer le scanner maintenant', ico: '⌕', run: scanNow });
  const km = s && s.killmode && s.killmode.actif;
  c.push({ g: 'Actions', t: km ? 'Éteindre Killmode' : 'Allumer Killmode', ico: 'K', danger: true, run: () => killmode(!km) });
  c.push({ g: 'Actions', t: 'Poser un veto sur la dernière décision du council', ico: '⊘', danger: true, run: veto });
  for (const p of (s && s.proposals_pending) || []) {
    c.push({ g: 'Propositions', t: 'Approuver #' + p.id + ' · ' + proposalText(p), ico: '✓', danger: true, run: () => approveProposal(p) });
    c.push({ g: 'Propositions', t: 'Refuser #' + p.id + ' · ' + proposalText(p), ico: '✗', run: () => refuseProposal(p) });
  }
  if (st.chartSym) c.push({ g: 'Actions', t: 'Avis JARVIS sur ' + st.chartSym, ico: 'A', h: ['A'], run: () => opinion(st.chartSym, st.chartMarket) });
  c.push({ g: 'Liens', t: 'Télécharger le diagnostic (sans secret)', ico: '⇣', run: () => { location.href = '/api/cockpit/diagnostics'; } });
  c.push({ g: 'Liens', t: 'Ouvrir l\'ancien cockpit', ico: '↺', run: () => { location.href = '/cockpit/ancien'; } });
  c.push({ g: 'Liens', t: 'Ouvrir la page Labo actuelle', ico: 'L', run: () => { location.href = '/cockpit/labo'; } });
  c.push({ g: 'Aide', t: 'Afficher les raccourcis', ico: '?', h: ['?'], run: openHelp });
  return c;
}
const norm = (x) => x.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
function fuzzy(q, s) {
  q = norm(q); s = norm(s);
  if (!q) return 1; const ix = s.indexOf(q); if (ix >= 0) return 100 - ix;
  let j = 0, sc = 0; for (const ch of s) { if (ch === q[j]) { j++; sc++; if (j === q.length) return sc; } } return 0;
}
function renderPal() {
  const q = $('#pal-q').value.trim();
  palItems = commands().map((c) => Object.assign(c, { s: fuzzy(q, c.t) })).filter((c) => c.s > 0);
  if (q) palItems.sort((a, b) => b.s - a.s);
  palSel = Math.min(palSel, Math.max(0, palItems.length - 1));
  const list = $('#pal-list'), out = [];
  let g = null;
  palItems.forEach((c, k) => {
    if (!q && c.g !== g) { g = c.g; out.push(el('div', { class: 'pal-g', text: g, role: 'presentation' })); }
    out.push(el('button', { type: 'button', role: 'option', id: 'pal-' + k, class: 'pal-it' + (k === palSel ? ' sel' : '') + (c.danger ? ' danger' : ''), data: { k }, 'aria-selected': String(k === palSel) },
      el('span', { class: 'ico' }, c.ico), el('span', { text: c.t }), el('span', { class: 'h' }, c.sub ? el('span', { class: 'num', text: c.sub }) : null, (c.h || []).map(kbd))));
  });
  list.replaceChildren(...(out.length ? out : [el('div', { class: 'pal-empty', text: 'Rien ne correspond à « ' + q + ' ».' })]));
  $('#pal-q').setAttribute('aria-activedescendant', palItems.length ? 'pal-' + palSel : '');
  const sel = $('#pal-list .sel'); if (sel) sel.scrollIntoView({ block: 'nearest' });
}
function openPal() {
  closeOverlays(); palBack = document.activeElement;
  $('#pal-q').value = ''; palSel = 0; renderPal();
  $('#scrim').classList.add('on'); $('#palette').classList.add('on'); $('#pal-q').focus();
  if (!st.instruments || Date.now() - st.instrumentsAt > 60000) loadInstruments().then(() => { if ($('#palette').classList.contains('on')) renderPal(); });
}
function runPal(k) { const c = palItems[k]; if (!c) return; closeOverlays(); c.run(); }
$('#pal-q').addEventListener('input', () => { palSel = 0; renderPal(); });
$('#pal-q').addEventListener('keydown', (e) => {
  if (e.key === 'ArrowDown') { e.preventDefault(); palSel = (palSel + 1) % Math.max(1, palItems.length); renderPal(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); palSel = (palSel - 1 + palItems.length) % Math.max(1, palItems.length); renderPal(); }
  else if (e.key === 'Enter') { e.preventDefault(); runPal(palSel); }
});
$('#pal-list').addEventListener('click', (e) => { const b = e.target.closest('[data-k]'); if (b) runPal(+b.dataset.k); });

/* ---------- aide */
function openHelp() {
  closeOverlays();
  const g = [
    ['Partout', [['Palette de commandes', [MOD, 'K']], ['Palette (autre touche)', ['/']], ['Raccourcis', ['?']], ['Fermer', ['Échap']]]],
    ['Navigation', [['Vues 1 à 8', ['1', '…', '8']], ['Cockpit', ['G', 'C']], ['Marchés', ['G', 'M']], ['Positions', ['G', 'P']], ['Risque', ['G', 'R']], ['Labo', ['G', 'L']], ['Agenda', ['G', 'A']], ['Journal', ['G', 'J']], ['Santé', ['G', 'H']]]],
    ['Listes et graphique', [['Ligne suivante / précédente', ['J', 'K']], ['Inspecteur (ou graphique)', ['Entrée']], ['Trade suivant au graphique', ['N']], ['Trade précédent', ['Maj', 'N']], ['Intervalle', ['[', ']']]]],
    ['Actions', [['Papier ⇄ Mes trades', ['M']], ['Nouveau plan', ['+']], ['Avis JARVIS', ['A']]]],
  ];
  $('#help').replaceChildren(el('h2', { text: 'Raccourcis clavier' }), el('div', { class: 'help-g' }, g.map((x) => el('div', null, el('h3', { text: x[0] }), el('dl', null, x[1].map((r) => el('div', null, el('dt', { text: r[0] }), el('dd', null, r[1].map(kbd)))))))),
    el('p', { class: 'muted small mt-16', text: 'Entrer dans « Mes trades », clôturer, supprimer, déclarer une sortie, rouvrir, approuver une proposition, Killmode et veto demandent un appui maintenu, au clavier comme au doigt. Le cockpit n\'envoie jamais d\'ordre.' }));
  $('#scrim').classList.add('on'); $('#help').classList.add('on');
}
function closeOverlays() {
  ['#palette', '#help'].forEach((s) => $(s).classList.remove('on'));
  closeModal(); closeSheet();
  $('#scrim').classList.remove('on');
  if (palBack && palBack.isConnected && document.activeElement === $('#pal-q')) palBack.focus();
}
const overlayOpen = () => ['#palette', '#help', '#modal', '#sheet'].some((s) => $(s).classList.contains('on'));

/* ---------- toasts */
function toast(msg, kind = 'info', undo) {
  const t = el('div', { class: 'toast ' + kind, role: kind === 'bad' ? 'alert' : 'status' }, el('span', { class: 'ti' }), el('span', { text: msg }));
  const kill = () => { t.classList.add('out'); setTimeout(() => t.remove(), 220); };
  if (undo) t.append(el('button', { type: 'button', onclick: () => { kill(); undo(); } }, 'Annuler'));
  $('#toasts').append(t);
  setTimeout(kill, undo ? 6000 : 4000);
}

/* ---------- clavier */
let chordG = false, chordT = 0;
const CHORD = { c: 'cockpit', m: 'marches', p: 'positions', r: 'risque', l: 'labo', a: 'agenda', j: 'journal', h: 'sante' };
function showChord(on) {
  const c = $('#chord');
  if (on) { c.replaceChildren(el('b', { text: 'G' }), ' puis : ', ...Object.entries(CHORD).flatMap(([k, v], i) => [i ? ' · ' : '', el('b', { text: k.toUpperCase() }), ' ' + VIEWS.find((x) => x[0] === v)[1]])); c.classList.add('on'); }
  else c.classList.remove('on');
}
function moveCursor(d) {
  const items = $$('#v-' + st.view + ' [data-nav-item]'); if (!items.length) return;
  st.cursor = (st.cursor + d + items.length) % items.length;
  items.forEach((x, k) => x.classList.toggle('cursor', k === st.cursor));
  items[st.cursor].focus({ preventScroll: false });
}
function nextEngaged(d) {
  const e = (st.state && st.state.positions_open) || [];
  if (!e.length) { toast('Aucun trade ouvert dans l\'espace ' + spaceName(), 'info'); return; }
  const ix = e.findIndex((p) => p.symbol === st.chartSym);
  const p = e[((ix < 0 ? (d > 0 ? -1 : 0) : ix) + d + e.length) % e.length];
  st.chartSym = p.symbol; st.chartMarket = p.market;
  if (st.view === 'marches') render(false); else go('marches', { force: true });
}
function setTf(tf) { st.tf = tf; try { localStorage.setItem('cockpit.tf', tf); } catch (_) { /* idem */ } if (st.view === 'marches') { $$('[data-tf]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.tf === tf))); loadInstrument(true); } }
document.addEventListener('keydown', (e) => {
  const tgt = e.target, typing = tgt && (/^(INPUT|TEXTAREA|SELECT)$/.test(tgt.tagName) || tgt.isContentEditable);
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); $('#palette').classList.contains('on') ? closeOverlays() : openPal(); return; }
  if (e.key === 'Escape') { if (overlayOpen()) closeOverlays(); else if (st.drawer) closeDrawer(); showChord(false); chordG = false; return; }
  if (typing || overlayOpen() || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key;
  if (chordG) { chordG = false; clearTimeout(chordT); showChord(false); if (CHORD[k.toLowerCase()]) { e.preventDefault(); go(CHORD[k.toLowerCase()]); } return; }
  if (k === 'g' || k === 'G') { chordG = true; showChord(true); chordT = setTimeout(() => { chordG = false; showChord(false); }, 1500); return; }
  if (/^[1-8]$/.test(k)) { e.preventDefault(); go(VIEWS[+k - 1][0]); return; }
  if (k === '/') { e.preventDefault(); openPal(); return; }
  if (k === '?') { e.preventDefault(); openHelp(); return; }
  if (k === '+') { e.preventDefault(); planForm(st.view === 'marches' ? { symbol: st.chartSym, market: st.chartMarket } : {}); return; }
  if (k === 'm' || k === 'M') { e.preventDefault(); setSpace(st.space === 'paper' ? 'live' : 'paper'); return; }
  if (k === 'n' || k === 'N') { e.preventDefault(); nextEngaged(e.shiftKey ? -1 : 1); return; }
  if (k === 'a' || k === 'A') { const s = st.drawer ? st.drawer.sym : st.view === 'marches' ? st.chartSym : null; if (s) { e.preventDefault(); opinion(s, st.drawer ? st.drawer.market : st.chartMarket); } return; }
  if (k === 'j' || k === 'J') { e.preventDefault(); moveCursor(1); return; }
  if (k === 'k' || k === 'K') { e.preventDefault(); moveCursor(-1); return; }
  if ((k === '[' || k === ']') && st.view === 'marches') { e.preventDefault(); const ix = TFS.indexOf(st.tf) + (k === ']' ? 1 : -1); setTf(TFS[(ix + TFS.length) % TFS.length]); toast('Intervalle ' + st.tf, 'info'); }
});

/* ---------- clics délégués */
document.addEventListener('click', (e) => {
  const t = e.target.closest('[data-go],[data-act],[data-sym],[data-tf],[data-chart],[data-eng]'); if (!t) return;
  if (t.closest('#palette')) return;
  if (t.dataset.go) { go(t.dataset.go); return; }
  if (t.dataset.act === 'palette') { openPal(); return; }
  if (t.dataset.act === 'help') { openHelp(); return; }
  if (t.dataset.tf) { setTf(t.dataset.tf); return; }
  if (t.dataset.eng) { st.chartSym = t.dataset.eng; st.chartMarket = t.dataset.market || null; render(false); return; }
  if (t.dataset.chart) { st.chartSym = t.dataset.chart; st.chartMarket = t.dataset.market || null; closeDrawer(); closeSheet(); if (st.view === 'marches') render(false); else go('marches', { force: true }); return; }
  if (t.dataset.sym) {
    if (st.view === 'marches' && t.classList.contains('row')) { st.chartSym = t.dataset.sym; st.chartMarket = t.dataset.market || null; render(false); return; }
    openDrawer(t.dataset.sym, t.dataset.market || null);
  }
});
$('#scrim').addEventListener('click', closeOverlays);
$('#sp-paper').addEventListener('click', () => setSpace('paper'));
$('#sp-trades').addEventListener('click', () => setSpace('live'));
window.addEventListener('hashchange', () => { const v = location.hash.replace('#', ''); if (BUILD[v] && v !== st.view) go(v); });
window.addEventListener('resize', moveInd);

/* ---------- démarrage */
buildNav();
root.dataset.space = st.space;
go(st.view, { force: true });
clock();
setInterval(clock, 1000);
statePoll.start(); heavyPoll.start();
if (!csrf()) toast('Jeton de sécurité absent (cockpit ouvert en http ?) : les actions seront refusées. Ouvre-le en https, port 4443 du tailnet.', 'bad');
if (document.fonts && document.fonts.ready) document.fonts.ready.then(moveInd);
})();
