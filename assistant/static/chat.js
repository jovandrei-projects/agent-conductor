/* Ask AI - the dashboard's assistant surface (assistant/static/chat.js).
 *
 * One durable, non-modal chat surface living OUTSIDE #detail/#dbody so
 * modal navigation, repaints and view changes never reset it. Covered by
 * the design contract (.agent_tasks/ask-ai/DESIGN.md section 6.4):
 *
 * - Launchers: the page-level floating button (#aifab) plus #dask pinned
 *   into every #detail modal's chrome - one surface, many entry points,
 *   never a second assistant inside the first.
 * - States: closed (launchers only) / compact (floating card) /
 *   expanded (right-docked workspace). Conversation, draft and panel
 *   state persist across modal navigation, minimize and reload.
 * - Context: the header chip names the view the NEXT question will use;
 *   each turn's own view stays frozen on its message.
 * - Replies render through Aimd (escape-first markdown) - model text is
 *   never executed markup. Citations navigate the dashboard via
 *   window.aiNav without touching chat state.
 * - The model name in the header and each reply badge opens the
 *   settings dialog (model / effort / fast / read-only with capability
 *   + price notes); settings changes apply to the NEXT exchange only.
 *
 * Host bridge (provided by the dashboard's inline script):
 *   window.aiView() -> {detailKey, summaryView, runModalKey,
 *     modalRequest, modalQA, power, only, facets, projs, selectedRun,
 *     agents}
 *   window.aiNav = {agent(key), run(key), summary(key),
 *     runSummary(key), request(id)}
 *   window.aiSync is installed HERE and called by the host's applySel
 *   so the view chip tracks modal changes without polling.
 */
(function () {
'use strict';

var API = '/api/assistant';
var POLL_MS = 1100;

/* ------------------------------------------------------------ helpers */
function esc(s) {
  return (s == null ? '' : String(s)).replace(/[&<>"]/g,
    function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
}
function byId(id) { return document.getElementById(id); }
function base(p) {
  return String(p || '').split(/[\\/]/).filter(Boolean).pop() || '';
}
function uid(p) {
  var c = (typeof crypto !== 'undefined') ? crypto : null;
  var tail = c && c.randomUUID ? c.randomUUID()
    : Date.now().toString(36) + Math.random().toString(36).slice(2) +
      Math.random().toString(36).slice(2);
  return (p + '-' + tail).slice(0, 64);   // server: [A-Za-z0-9_-]{8,64}
}
function store(key, val) {          // localStorage, never fatal
  try {
    var ls = window.localStorage;
    if (val === undefined) return ls && ls.getItem(key);
    if (val === null) { ls && ls.removeItem(key); return null; }
    ls && ls.setItem(key, val);
    return val;
  } catch (e) { return null; }
}
function sessStore(key, val) {      // per-tab identity
  try {
    var ss = window.sessionStorage;
    if (val === undefined) return ss && ss.getItem(key);
    ss && ss.setItem(key, val);
    return val;
  } catch (e) { return null; }
}
function thaw(s) {                  // stored JSON column -> object
  if (!s || typeof s !== 'string') return (typeof s === 'object' && s) || {};
  try { return JSON.parse(s); } catch (e) { return {}; }
}
function fmtTok(n) {
  if (n == null) return '-';
  n = +n;
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(Math.round(n));
}
function fmtUsd(n) {
  if (n == null) return null;
  var v = +n;
  /* a real-but-tiny cost must never round-display as $0.00 - that
     reads as 'free'; show the floor instead */
  if (v > 0 && v < 0.0001) return '<$0.0001';
  return '$' + v.toFixed(v < 0.01 ? 4 : 2);
}
function timeShort(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleString([], { month: 'numeric', day: 'numeric',
    hour: '2-digit', minute: '2-digit' });
}
function timeCompact(iso) {
  /* '10/17 16:25' - the rail's sub line is too narrow for the
     locale '10/17, 04:25 PM' once model + size join it */
  if (!iso) return '';
  var d = new Date(iso);
  if (isNaN(d)) return '';
  var pad = function (n) { return String(n).padStart(2, '0'); };
  return (d.getMonth() + 1) + '/' + d.getDate() + ' ' +
    pad(d.getHours()) + ':' + pad(d.getMinutes());
}

async function aapi(method, path, body, raw) {
  /* the dashboard api() shape, but attachments-ready: `raw` sends the
     body verbatim (image upload) instead of JSON */
  var opt = method === 'GET' ? {} : {
    method: method,
    headers: raw ? {} : { 'Content-Type': 'application/json' },
    body: raw ? body : JSON.stringify(body || {})
  };
  var res = await fetch(API + path, opt);
  var out;
  var ct = (res.headers && res.headers.get &&
    res.headers.get('content-type')) || '';
  out = ct.indexOf('json') >= 0 ? await res.json()
    : { error: await res.text() };
  if (!res.ok) throw new Error(out.error || ('HTTP ' + res.status));
  return out;
}

/* ------------------------------------------------------------- state */
var S = {
  mode: 'closed',              // closed | compact | expanded
  rail: false,                 // history drawer/column visible
  conv: null,                  // conversation record
  msgs: [],                    // chronological message rows
  exch: {},                    // user_msg_id -> exchange view
  exchByAid: {},               // assistant_msg_id -> exchange view
  actions: {},                 // exchange id -> action rows
  armed: {},                   // action id -> launch-tier confirm armed
  sending: false,
  convList: null,
  catalog: null,
  defaults: null,
  usage: null,
  win: '',
  convRef: '',                 // dedupe ref for the NEXT conv create
  pollXid: null,
  pinBottom: true,
  lastFocus: null,
  ctxEnv: null,                // last computed next-question envelope
  attach: [],                  // pending uploads for the CURRENT conv
  pendAtt: {},                 // conv id -> stashed pending uploads
  renaming: null,              // conv id whose rail row is an <input>
  delArm: {},                  // conv id -> delete confirm-armed (fallback)
  dict: null                   // live dictation {rec,base,final} or null
};

var TERMINAL = { done: 1, partial: 1, error: 1, cancelled: 1 };

function convId() { return S.conv && S.conv.id; }
function draftKey() {
  return 'ac-ai-draft' + (convId() ? ':' + convId() : '');
}

/* ------------------------------------------------------ view context */
function findAgent(akey) {
  var v = (window.aiView ? window.aiView() : {}) || {};
  var list = v.agents || [];
  for (var i = 0; i < list.length; i++) {
    var a = list[i];
    if (((a.run_key || a.run_dir || '') + '|' + a.step) === akey) return a;
  }
  return null;
}

/* The envelope the NEXT question freezes onto its turn. view slug names
   the surface; refs carry stable record ids the server re-resolves. */
function curCtx() {
  var v = (window.aiView ? window.aiView() : {}) || {};
  var view = 'dashboard', refs = {};
  /* precedence mirrors z-order: overlays (power, planner questions,
     the request form) report above whatever detail modal sits under
     them; inside #detail the request view and summary/detail views
     are mutually exclusive so their order is by convention only. */
  if (v.power) {
    view = 'power';
  } else if (v.modalQA && v.modalQA.id) {
    view = 'questions';
    refs.request_id = v.modalQA.id;
  } else if (v.modalRequest) {
    view = 'request';
    if (v.modalRequest.id) refs.request_id = v.modalRequest.id;
  } else if (v.summaryView) {
    var sk = String(v.summaryView.key || '');
    if (sk.indexOf('run|') === 0) {
      view = 'run-summary';
      refs.summary_key = sk;
      refs.run_key = sk.slice(4);
    } else {
      view = 'summary';
      refs.summary_key = sk;
      var sa = findAgent(sk);
      if (sa) { refs.run_key = sa.run_key || sa.run_dir; refs.step = sa.step; }
      if (!refs.step) {
        var si = sk.lastIndexOf('|');
        if (si >= 0) {
          refs.run_key = refs.run_key || sk.slice(0, si);
          refs.step = sk.slice(si + 1);
        }
      }
    }
  } else if (v.detailKey != null) {
    view = 'agent';
    var i = String(v.detailKey).lastIndexOf('|');
    refs.run_key = i >= 0 ? String(v.detailKey).slice(0, i)
                          : String(v.detailKey);
    if (i >= 0) refs.step = String(v.detailKey).slice(i + 1);
    var a = findAgent(String(v.detailKey));
    if (a && a.run_dir) refs.run_dir = base(a.run_dir);
  } else if (v.runModalKey) {
    view = 'run';
    refs.run_key = v.runModalKey;
  }
  var sel = '';
  try {
    sel = String((window.getSelection && window.getSelection()) || '')
      .slice(0, 2000);
  } catch (e) {}
  return {
    view: view,
    refs: refs,
    filters: {
      only: v.only || [], facets: v.facets || [], projs: v.projs || [],
      selected_run: v.selectedRun || ''
    },
    selection: sel,
    window: S.win,
    at: new Date().toISOString()
  };
}

function ctxLabel(env) {
  env = env || {};
  var v = env.view || 'dashboard', r = env.refs || {};
  switch (v) {
    case 'agent': return 'task ' + (r.step || '?');
    case 'run': return 'run ' + (base(r.run_key) || r.run_key || '?');
    case 'summary':
      return 'summary ' + (r.step || base(r.summary_key) || '?');
    case 'run-summary': return 'run summary ' + (base(r.run_key) || '');
    case 'request':
      return 'request ' + (r.request_id || '(new)');
    case 'questions': return 'planner questions';
    case 'power': return 'power dialog';
    default: return 'dashboard';
  }
}

/* chip under the header: which view the NEXT question freezes to */
function syncView() {
  var el = byId('ai-ctx');
  if (!el) return;
  var env = curCtx();
  S.ctxEnv = env;
  el.textContent = 'about: ' + ctxLabel(env);
  el.title = 'the next question is answered against this view';
}

/* ------------------------------------------------------------- paint */
function settingsLabel(snap) {
  snap = snap || {};
  var m = snap.model || 'default';
  var bits = [m];
  if (snap.effort) bits.push(snap.effort);
  if (snap.fast) bits.push('fast');
  return bits.join(' \u00b7 ');
}

function srcLabel(src, i) {
  var label = src.label;
  if (!label && src.key) {
    /* legacy rows stored before labels were synthesized fall back to
       the key - its last segment (the step name) is the readable part */
    var k = String(src.key).replace(/#(run-)?summary$/, '');
    var cut = k.lastIndexOf('|');
    label = cut >= 0 ? k.slice(cut + 1) : k;
  }
  label = label || src.kind || ('source ' + (i + 1));
  return '[' + (i + 1) + '] ' + label;
}

function msgHtml(m) {
  var x = S.exch[m.id];                 // exchange OWNED by this user msg
  var xa = S.exchByAid[m.id];           // exchange this reply belongs to
  var h = '';
  var role = m.role === 'user' ? 'aim-u' : 'aim-a';
  var env = thaw(m.ctx_ref);
  var snap = thaw(m.settings_json);
  var meta = '';
  if (m.role === 'user') {
    meta = (m.pending ? '<span class="aim-st">sending\u2026</span>' : '') +
      '<span class="aim-ctxm" title="' + esc(ctxLabel(env)) +
      '">' + esc(ctxLabel(env)) + '</span>';
  } else {
    var st = xa ? xa.status : m.status;
    if (st === 'queued' || st === 'running' || st === 'pending')
      meta = '<span class="aim-st">' +
        (st === 'running' ? 'answering\u2026' : 'queued\u2026') +
        '</span>';
    else if (st === 'error')
      meta = '<span class="aim-st err">failed</span>';
    else if (st === 'cancelled')
      meta = '<span class="aim-st">stopped</span>';
    else if (st === 'partial')
      meta = '<span class="aim-st" title="interrupted - the reply ' +
        'may be incomplete">partial</span>';
    /* the reply badge shows THIS turn's recorded snapshot (never the
       current settings); model_label from the exchange is the fallback
       for rows that predate snapshot storage */
    var blabel = snap.model ||
      (xa && (xa.model_label || xa.model_uid)) || 'model unknown';
    meta += '<button type="button" class="aim-modelb" data-ai-set ' +
      'title="' + esc((snap.model ? settingsLabel(snap) + ' - ' : '') +
      'model this answer ran on - click to open settings') + '">' +
      esc(blabel) + '</button>' +
      (snap.readonly === false
        ? ' <span class="aim-st err" title="this answer ran with ' +
          'mutations allowed">rw</span>' : '');
  }
  var atts = thaw(m.attachments);
  var attHtml = '';
  if (m.role === 'user' && atts && atts.length) {
    /* previews come back through the id-gated binary route - the raw
       path stays server-side and the reopened conversation keeps its
       image references + thumbnails */
    attHtml = '<div class="aim-ats">' + atts.map(function (a) {
      var url = API + '/attachments/' + encodeURIComponent(a.id || '');
      return '<a class="aim-at" href="' + url + '" target="_blank" ' +
        'rel="noopener" title="' + esc(a.name || 'image') + '">' +
        '<img src="' + url + '" alt="' + esc(a.name || 'image') + '"' +
        ' loading="lazy"></a>';
    }).join('') + '</div>';
  }
  /* the in-flight reply renders as a bare gray "thinking" line, no
     bubble chrome - the .think class strips the box on the body cell */
  var thinking = m.role !== 'user' &&
    !((xa && xa.text) || m.text) &&
    ((xa && (xa.status === 'queued' || xa.status === 'running')) ||
     (!xa && m.status === 'pending'));
  h += '<div class="aim ' + role + '" data-mid="' + esc(m.id) + '">' +
    '<div class="aim-meta">' + meta + '</div>' +
    '<div class="aim-body' + (thinking ? ' think' : '') + '">' +
    (m.role === 'user'
      ? (m.text ? '<p>' + esc(m.text).replace(/\n/g, '<br>') + '</p>'
                : '') + attHtml
      : renderReply(m, xa)) +
    '</div>';
  if (xa && xa.sources && xa.sources.length) {
    h += '<div class="aim-srcs">' +
      xa.sources.map(function (s, i) {
        var nav = srcNav(s);
        return '<button type="button" class="aim-src" data-ai-nav="' +
          esc(nav.view || '') + '" data-ai-key="' + esc(nav.key || '') +
          '" title="' + esc(s.kind || '') + ' - opens in the dashboard">' +
          esc(srcLabel(s, i)) + '</button>';
      }).join('') + '</div>';
  }
  var acts = xa ? (S.actions[xa.id] || []) : [];
  for (var k = 0; k < acts.length; k++) {
    h += actHtml(acts[k]);
  }
  h += usageHtml(xa, snap);
  h += '</div>';
  return h;
}

function msgById(id) {
  for (var i = 0; i < S.msgs.length; i++)
    if (S.msgs[i].id === id) return S.msgs[i];
  return null;
}

function fmtDay(epoch) {
  if (!epoch) return '';
  var d = new Date(epoch * 1000);           // price stamps are epoch s
  return isNaN(d) ? '' : d.toLocaleDateString();
}

function reqLine(r) {
  /* one internal request boundary (a provider dispatch hop: the first
     turn, a resume, a tool continuation, or a reconcile adoption).
     Billable retries and failed hops stay listed - 'internal' never
     means 'free'. */
  var bits = ['#' + (r.n || '?') + ' ' + (r.kind || 'request')];
  if (r.request_id) bits.push(r.request_id);
  if (r.started_at != null && r.ended_at != null)
    bits.push(Math.max(0, Math.round(r.ended_at - r.started_at)) + 's');
  if (r.exit_code != null) bits.push('exit ' + r.exit_code);
  var u = r.usage || {};
  if (u.prompt_t != null || u.completion_t != null) {
    var rt = 'in ' + fmtTok(u.prompt_t) + ' \u00b7 out ' +
      fmtTok(u.completion_t);
    if (u.cached_t) rt += ' (' + fmtTok(u.cached_t) + ' cached)';
    bits.push(rt);
  }
  if (u.llm_steps != null)
    bits.push(u.llm_steps + ' turn' + (u.llm_steps === 1 ? '' : 's'));
  if (u.extra && typeof u.extra === 'object')
    Object.keys(u.extra).forEach(function (k) {
      bits.push(k.replace(/^total_/, '').replace(/_/g, ' ') + ' ' +
        fmtTok(u.extra[k]));
    });
  var row = '<div class="um-r">' + esc(bits.join(' \u00b7 ')) + '</div>';
  if (r.error)
    row += '<div class="um-r err">' + esc('error: ' + r.error) + '</div>';
  return row;
}

function usageHtml(xa, snap) {
  /* Per-exchange usage: a compact summary line plus a native
     <details> breakdown - accessible expand/collapse needs no extra
     wiring. Token counters are provider-REPORTED deltas; the $
     figure is a CALCULATED USD estimate from this exchange's stored
     rate snapshot; a missing report stays 'unknown', never $0. */
  if (!xa || !TERMINAL[xa.status]) return '';
  var hasU = xa.prompt_t != null || xa.completion_t != null ||
    xa.cached_t != null || xa.llm_steps != null;
  var bits = [];
  if (hasU) {
    var inp = 'in ' + fmtTok(xa.prompt_t);
    if (xa.cached_t) inp += ' (' + fmtTok(xa.cached_t) + ' cached)';
    bits.push(inp, 'out ' + fmtTok(xa.completion_t));
    if (xa.llm_steps) bits.push(xa.llm_steps + ' turn' +
      (xa.llm_steps === 1 ? '' : 's'));
    if (xa.usage_extra && Object.keys(xa.usage_extra).length)
      bits.push('+' + Object.keys(xa.usage_extra).length +
        ' extra counter' +
        (Object.keys(xa.usage_extra).length === 1 ? '' : 's'));
  } else {
    bits.push('usage not reported');
  }
  if (xa.usd != null) bits.push('~' + fmtUsd(xa.usd) + ' est.');
  else if (hasU || xa.status !== 'cancelled')
    bits.push('cost unknown');
  var h = '<details class="aim-use" data-xu="' +
    esc(xa.id || '') + '"><summary>' +
    esc(bits.join(' \u00b7 ')) + '</summary><div class="aim-udet">';
  /* the settings snapshot this exchange actually ran under (frozen on
     the message - later settings edits never rewrite it) */
  var mb = xa.model_uid || xa.model_label || '?';
  if (xa.model_label && xa.model_label !== xa.model_uid)
    mb += ' (' + xa.model_label + ')';
  var sb = [];
  if (snap.model) sb.push(snap.model);
  if (snap.effort) sb.push(snap.effort);
  if (snap.fast) sb.push('fast');
  sb.push(snap.readonly === false ? 'read-write' : 'read-only');
  h += '<div class="um-l">model ' + esc(mb) + ' \u00b7 ' +
    esc(sb.join(' ')) + ' \u00b7 status ' + esc(xa.status) +
    (xa.provider_session_id
      ? ' \u00b7 session ' + esc(xa.provider_session_id) : '') + '</div>';
  var reqs = (xa.requests || []).filter(function (r) {
    return r && typeof r === 'object'; });
  if (reqs.length)
    h += '<div class="um-l um-h">internal requests</div>' +
      reqs.map(reqLine).join('');
  var extra = xa.usage_extra || {};
  var ekeys = Object.keys(extra);
  if (ekeys.length)
    h += '<div class="um-l">also reported (no price semantics ' +
      'attached): ' + esc(ekeys.map(function (k) {
        return k.replace(/^total_/, '').replace(/_/g, ' ') + ' ' +
          fmtTok(extra[k]);
      }).join(' \u00b7 ')) + '</div>';
  if (hasU && xa.usd != null) {
    var rb = [];
    if (xa.price_in != null) {
      rb.push('rates $' + xa.price_in + ' in / $' +
        (xa.price_cached != null ? xa.price_cached : '?') +
        ' cached / $' + xa.price_out + ' out per 1M tok');
      if (xa.price_source) rb.push('source ' + xa.price_source);
      if (xa.price_fetched_at)
        rb.push('fetched ' + fmtDay(xa.price_fetched_at));
      h += '<div class="um-l">' + esc(rb.join(' \u00b7 ')) + '</div>';
    }
    h += '<div class="um-l">cost ~' + esc(fmtUsd(xa.usd)) +
      ' USD - a calculated estimate, not an invoice</div>';
  } else if (hasU) {
    h += '<div class="um-l">no rate data for this model - the cost ' +
      'is unknown, not zero</div>';
  } else {
    h += '<div class="um-l">the provider reported no usage for this ' +
      'exchange - cost unknown, not zero</div>';
  }
  var um = msgById(xa.user_msg_id);
  if (um && thaw(um.attachments).length)
    h += '<div class="um-l">image input included - the current rate ' +
      'table has no separate image charge; image processing lands ' +
      'inside the token counters</div>';
  return h + '</div></details>';
}

function usageViewHtml() {
  /* The conversation-level breakdown behind the header chip: grand
     totals, a per-model split (a mixed-model chat must not blend
     rates), the unpriced count and pricing provenance - under one
     native <details> so keyboard and screen-reader access need no
     extra wiring. */
  var u = S.usage, t = (u && u.totals) || {};
  if (!t.exchanges) return '<div class="um-l">no usage yet</div>';
  var bm = t.by_model || {}, keys = Object.keys(bm), h = '';
  if (keys.length > 1) h += '<div class="um-h">by model</div>';
  keys.forEach(function (k) {
    var g = bm[k];
    h += '<div class="um-g">' +
      (keys.length > 1
        ? '<span class="um-m">' + esc(k) + '</span> ' : '') +
      esc(g.exchanges + ' exch \u00b7 in ' + fmtTok(g.prompt_t) +
        ' \u00b7 out ' + fmtTok(g.completion_t) +
        (g.cached_t ? ' (' + fmtTok(g.cached_t) + ' cached)' : '') +
        (g.usd != null ? ' \u00b7 ~' + fmtUsd(g.usd)
                      : ' \u00b7 unpriced')) +
      (g.unpriced ? ' <span class="um-warn">' + esc(g.unpriced +
        ' unpriced') + '</span>' : '') + '</div>';
  });
  h += '<div class="um-t">' + esc('total ' + t.exchanges +
    ' exchange' + (t.exchanges === 1 ? '' : 's') + ' \u00b7 in ' +
    fmtTok(t.prompt_t) + ' \u00b7 out ' + fmtTok(t.completion_t) +
    (t.cached_t ? ' (' + fmtTok(t.cached_t) + ' cached)' : '') +
    (t.usd != null ? ' \u00b7 ~' + fmtUsd(t.usd) : '')) + '</div>';
  if (t.unpriced)
    h += '<div class="um-l um-warn">' + esc(t.unpriced + ' exchange' +
      (t.unpriced === 1 ? ' has' : 's have') +
      ' no price data - their cost is unknown, not zero') + '</div>';
  h += '<div class="um-l">' + esc('costs are calculated ' +
    (t.currency || 'USD') + ' estimates' +
    (t.price_source ? ' - rates ' + t.price_source : '') +
    (t.price_fetched_at
      ? ' - fetched ' + fmtDay(t.price_fetched_at) : '') +
    ' - not an invoice') + '</div>';
  return h;
}

/* --------------------------------------------------- action cards
 * A model proposal renders as a card under the reply that emitted it.
 * Every state transition is a server POST - the card is a display of the
 * audited row, never the authority. Launch-tier kinds need the armed
 * second step (the card shows the adapter's exact-target ack verbatim);
 * read-only renders every button inert with a hint instead of hiding it.
 */
function actTarget(ac) {
  var ack = ac.expected_ack || '';
  return ack.indexOf(':') >= 0 ? ack.slice(ack.indexOf(':') + 1) : '';
}

function paramLine(payload) {
  var bits = [];
  Object.keys(payload || {}).forEach(function (k) {
    var v = payload[k];
    if (v === null || v === undefined || v === '') return;
    if (k === 'raw' || k === 'manifest' || k === 'prompt' ||
        k === 'notes' || k === 'instructions')
      v = String(v).slice(0, 60) + (v.length > 60 ? '\u2026' : '');
    else if (Array.isArray(v)) v = v.join(', ');
    else if (typeof v === 'object') v = JSON.stringify(v).slice(0, 60);
    bits.push(k + '=' + String(v));
  });
  return bits.join('  ');
}

function actHtml(ac) {
  var st = ac.state || 'proposed';
  var ro = S.conv ? !!S.conv.readonly : true;
  var armed = !!S.armed[ac.id];
  var launch = !!ac.expected_ack;      // launch-tier rows carry an ack
  var res = thaw(ac.result);
  var h = '<div class="aim-card st-' + esc(st) + '">';
  h += '<div class="k">proposed: ' +
    esc(String(ac.kind || '?').replace(/_/g, ' ')) + '</div>';
  if (ac.explain)
    h += '<div class="act-why">' + esc(ac.explain) + '</div>';
  var pline = paramLine(thaw(ac.payload));
  if (pline) h += '<div class="act-params">' + esc(pline) + '</div>';
  if (st === 'proposed' || st === 'confirmed') {
    if (st === 'confirmed')
      h += '<div class="sub">approved - still needs the run click' +
        '</div>';
    if (launch && armed) {
      /* the second, separate confirmation: exact target + effect,
         verbatim from the adapter's ack + confirm text */
      h += '<div class="act-armed">' +
        '<div class="act-warn">' + esc(ac.confirm_text || ac.kind) +
        '</div>' +
        '<div class="act-tgt">target: ' + esc(actTarget(ac) || '?') +
        '</div>' +
        '<div class="act-btns">' +
        '<button type="button" class="act-btn danger" data-ai-act="' +
        esc(ac.id) + ':exec">Confirm &amp; run</button>' +
        '<button type="button" class="act-btn" data-ai-act="' +
        esc(ac.id) + ':disarm">Cancel</button>' +
        '</div></div>';
    } else {
      h += '<div class="act-btns">';
      if (launch)
        h += '<button type="button" class="act-btn" data-ai-act="' +
          esc(ac.id) + ':arm"' + (ro ? ' disabled' : '') +
          '>Review &amp; run</button>';
      else
        h += '<button type="button" class="act-btn" data-ai-act="' +
          esc(ac.id) + ':exec"' + (ro ? ' disabled' : '') +
          '>Run</button>';
      h += '<button type="button" class="act-btn" data-ai-act="' +
        esc(ac.id) + ':deny">' +
        (st === 'confirmed' ? 'Retract' : 'Deny') + '</button>';
      h += '</div>';
      if (ro)
        h += '<div class="sub">read-only is on - turn it off in ' +
          'settings to run actions</div>';
      if (res && res.refused)
        h += '<div class="sub">' + esc(res.refused) + '</div>';
    }
  } else if (st === 'denied') {
    h += '<div class="sub">dismissed</div>';
  } else if (st === 'executed') {
    h += '<div class="act-ok">done</div>';
    if (res && Object.keys(res).length)
      h += '<div class="act-params">' +
        esc(JSON.stringify(res).slice(0, 300)) + '</div>';
  } else if (st === 'failed') {
    h += '<div class="act-err">failed</div>';
    h += '<div class="sub">' +
      esc((res && (res.error || res.refused)) || 'rejected') + '</div>';
  }
  h += '</div>';
  return h;
}

function traceHtml(xa) {
  /* the honest mid-flight breadcrumb: what the exchange has already
     fetched (recorded sources) and which provider hops it took - never
     invented narration. Collapsed by default like the model-picker's
     compact trace in other assistants, expandable on click */
  if (!xa) return '';
  /* rows that came straight from a POST response still carry JSON
     strings - only exchange_view thaws them */
  var reqs = Array.isArray(xa.requests) ? xa.requests
    : (thaw(xa.requests) || []);
  var srcs = Array.isArray(xa.sources) ? xa.sources
    : (thaw(xa.sources) || []);
  if (!reqs.length && !srcs.length) return '';
  var last = srcs.length
    ? 'looking at ' + esc((srcs[srcs.length - 1].label ||
        srcs[srcs.length - 1].key || 'a source'))
    : 'request ' + (reqs[reqs.length - 1].n || reqs.length) + ' of ' +
      'the exchange';
  var det = '';
  reqs.forEach(function (r) {
    det += '<div class="ai-trace-l">request ' + esc(String(r.n || '?')) +
      ' \u00b7 ' + esc(r.kind || 'call') + '</div>';
  });
  srcs.forEach(function (s, i) {
    det += '<div class="ai-trace-l">read ' + esc(srcLabel(s, i)
      .replace(/^\[\d+\] /, '')) + '</div>';
  });
  return '<details class="ai-trace"><summary>' + last + '</summary>' +
    '<div class="ai-trace-d">' + det + '</div></details>';
}

function renderReply(m, xa) {
  /* exchange_view text carries the newest reply text even mid-run;
     messages row text only lands at completion */
  var text = (xa && xa.text) || m.text || '';
  if (!text && xa && (xa.status === 'queued' || xa.status === 'running'))
    return '<span class="ai-dots">thinking' +
      '<i>.</i><i>.</i><i>.</i></span>' + traceHtml(xa);
  if (!text && m.status === 'pending')
    return '<span class="ai-dots">thinking' +
      '<i>.</i><i>.</i><i>.</i></span>' + traceHtml(xa);
  if (!text && m.status === 'error')
    return '<p><em>the reply failed' +
      (xa && xa.error ? ': ' + esc(xa.error) : '') + '</em></p>';
  var html = (window.Aimd ? window.Aimd.render(text)
    : '<p>' + esc(text) + '</p>');
  if (xa && (xa.status === 'running' || xa.status === 'queued'))
    html += '<span class="ai-caret">\u258d</span>';
  return html || '<p><em>(no text)</em></p>';
}

function fmtKb(b) {
  /* the stored-bytes estimate the list endpoint ships - '~' is honest:
     it counts characters + attachment bytes, not sqlite pages */
  if (b == null) return '';
  b = +b;
  return '~' + (b < 1024 ? Math.max(1, Math.ceil(b / 1024 * 10) / 10)
                        : Math.round(b / 1024)) + ' KB';
}

function railHtml() {
  var list = S.convList;
  var h = '';
  if (list === null) h = '<div class="ai-empty">loading\u2026</div>';
  else if (!list.length)
    h = '<div class="ai-empty">no conversations yet</div>';
  else h = list.map(function (c) {
    var on = convId() === c.id ? ' on' : '';
    var t = c.title || '(untitled chat)';
    if (S.renaming === c.id)
      return '<div class="ai-conv-row' + on + '">' +
        '<input type="text" class="ai-ren-in" data-ai-renin="' +
        esc(c.id) + '" value="' +
        esc(t === '(untitled chat)' ? '' : t) + '" maxlength="120" ' +
        'aria-label="Rename this chat" placeholder="chat title">' +
        '</div>';
    var subRaw = timeCompact(c.last_active_at) +
      (c.model ? ' \u00b7 ' + c.model : '') +
      (c.size_bytes != null ? ' \u00b7 ' + fmtKb(c.size_bytes) : '');
    /* the sub line ellipsizes on narrow panels - the full string sits
       on the row tooltip so the KB count is always hoverable */
    return '<div class="ai-conv-row' + on + '">' +
      '<button type="button" class="ai-conv" data-ai-conv="' +
      esc(c.id) + '" title="' + esc(t) + '\n' + esc(subRaw) + '">' +
      '<span class="t">' + esc(t) + '</span>' +
      '<span class="s" title="' + esc(subRaw) + '">' + esc(subRaw) +
      '</span></button>' +
      '<span class="ai-cvops">' +
      '<button type="button" class="ai-cvop" data-ai-ren="' +
      esc(c.id) + '" title="Rename chat" ' +
      'aria-label="Rename ' + esc(t) + '">&#9998;</button>' +
      '<button type="button" class="ai-cvop del" data-ai-del="' +
      esc(c.id) + '" title="Delete chat" ' +
      'aria-label="Delete ' + esc(t) + '">&times;</button>' +
      '</span></div>';
  }).join('');
  return h;
}

function paint() {
  var el = byId('aichat');
  if (!el) return;
  el.className = S.mode === 'expanded' ? 'expanded' : '';
  /* header */
  var nameEl = byId('ai-name');
  if (nameEl) {
    var snap = S.conv
      ? { model: S.conv.model, effort: S.conv.effort, fast: S.conv.fast }
      : (S.defaults || {});
    nameEl.textContent = snap.model || 'default';
    nameEl.title = 'assistant settings - ' + settingsLabel(snap) +
      ' - model, effort, fast, read-only';
  }
  var pill = byId('ai-state');
  if (pill) {
    var ro = S.conv ? !!S.conv.readonly
      : S.defaults ? S.defaults.readonly !== false : true;
    pill.textContent = ro ? 'read-only' : 'actions on';
    pill.className = 'ai-pill ' + (ro ? 'ro' : 'rw');
    pill.title = (ro
      ? 'the assistant can read the dashboard but cannot change anything'
      : 'the assistant may propose actions for your confirmation') +
      ' - open assistant settings';
  }
  var use = byId('ai-usage');
  if (use) {
    var tot = (S.usage && S.usage.totals) || {};
    var sum = byId('ai-usage-sum');
    if (sum) {
      sum.textContent = tot.exchanges
        ? 'tok ' + fmtTok((tot.prompt_t || 0) + (tot.completion_t || 0))
          + (tot.cached_t ? ' \u00b7 cached ' + fmtTok(tot.cached_t) : '')
          + (tot.usd != null ? ' \u00b7 ~' + fmtUsd(tot.usd) : '')
        : '';
      sum.title = 'conversation usage: ' + fmtTok(tot.prompt_t) +
        ' in (' + fmtTok(tot.cached_t || 0) + ' cached) / ' +
        fmtTok(tot.completion_t) + ' out' +
        (tot.unpriced
          ? ' - ' + tot.unpriced + ' exchange(s) unpriced' : '');
    }
    var uv = byId('ai-useview');
    if (uv) uv.innerHTML = usageViewHtml();
  }
  var histBtn = byId('ai-hist');
  if (histBtn) histBtn.className = 'ai-hbtn' + (S.rail ? ' on' : '');
  var sizeBtn = byId('ai-size');
  if (sizeBtn) {
    sizeBtn.textContent = S.mode === 'expanded' ? '\u2922' : '\u2921';
    sizeBtn.setAttribute('aria-label',
      S.mode === 'expanded' ? 'Collapse chat' : 'Expand chat');
    sizeBtn.title = S.mode === 'expanded'
      ? 'Collapse to the floating card' : 'Expand into the workspace';
  }
  var rail = byId('ai-rail');
  if (rail) {
    /* the panel floats left of the card: it must also hide when the
       chat itself is minimized, and its own geometry tracks the card's
       (compact vs expanded) via the 'exp' class */
    rail.hidden = !S.rail || S.mode === 'closed';
    rail.className = 'ai-rail' + (S.mode === 'expanded' ? ' exp' : '');
    var cl = byId('ai-convs');
    if (cl && S.rail) cl.innerHTML = railHtml();
  }
  /* message list */
  var list = byId('ai-list');
  if (list) {
    var wasBottom = S.pinBottom;
    /* usage <details> survive the repaint: innerHTML rebuilds collapse
       them, so collect which ones are open (keyed by exchange id) and
       re-open after the new markup lands */
    var openDet = {};
    if (list.querySelectorAll)
      list.querySelectorAll('details.aim-use[open]').forEach(
        function (d) { if (d.dataset.xu) openDet[d.dataset.xu] = 1; });
    if (!S.msgs.length)
      list.innerHTML = '<div class="ai-empty">Ask about what ' +
        'you\u2019re looking at - the current view is attached to each ' +
        'question, and replies can cite runs, tasks and proposals.' +
        '</div>';
    else
      list.innerHTML = S.msgs.map(msgHtml).join('');
    if (Object.keys(openDet).length && list.querySelectorAll)
      list.querySelectorAll('details.aim-use').forEach(
        function (d) { if (openDet[d.dataset.xu]) d.open = true; });
    if (wasBottom) list.scrollTop = list.scrollHeight;
  }
  paintAtt();
  syncView();
}

function note(msg, isErr) {
  var n = byId('ai-note');
  if (!n) return;
  if (!msg) { n.hidden = true; n.innerHTML = ''; return; }
  n.hidden = false;
  n.style.borderLeftColor = isErr === false ? '#d29922' : '#f85149';
  n.innerHTML = '<button type="button" data-ai-note-x ' +
    'aria-label="dismiss">\u00d7</button>' + esc(msg);
}

function statusLine(msg) {
  var s = byId('ai-status');
  if (s) s.textContent = msg || '';
}

/* ------------------------------------------------------------- convs */
function normConv(c) {
  /* sqlite keeps fast/readonly as INTEGER 0/1 - the wire record carries
     ints, and `0 !== false` is TRUE in JS, so any `!== false` check
     (the settings form's checkbox seeding) misreads a saved 'off' as
     'on'. Coerce once at ingest; an absent key keeps the safe
     read-only default */
  if (c) {
    c.fast = !!c.fast;
    c.readonly = c.readonly !== false && c.readonly !== 0;
  }
  return c;
}

async function ensureConv() {
  if (S.conv) return S.conv;
  var env = curCtx();
  /* client_ref dedupes a retried POST - it must be PER CREATE ATTEMPT,
     never per window: a stable window ref would collapse every later
     "New chat" into the first conversation */
  var out = await aapi('POST', '/conversations', {
    client_ref: S.convRef, title: '', origin: env
  });
  S.conv = normConv(out.conversation || out);
  S.convRef = uid('conv');            // next attempt gets a fresh ref
  store('ac-ai-conv', convId());
  return S.conv;
}

async function loadConv(id) {
  var out = await aapi('GET',
    '/conversations/' + encodeURIComponent(id));
  if (!out.conversation) throw new Error('conversation not found');
  S.conv = normConv(out.conversation);
  S.msgs = out.items || [];
  /* slots pushed into S.attach during the load window belong to OTHER
     conversations (their ensureConv created its own) - re-park them
     before the restore's stash comes back, and keep the conv filter
     so a foreign slot can never surface on the wrong thread */
  S.attach.forEach(function (a) {
    if (a.conv && a.conv !== id &&
        (S.pendAtt[a.conv] = S.pendAtt[a.conv] || []).indexOf(a) < 0)
      S.pendAtt[a.conv].push(a);
  });
  var foreign = S.attach.filter(function (a) {
    return a.conv && a.conv !== id;
  });
  var keep = S.attach.filter(function (a) {
    return foreign.indexOf(a) < 0;
  });
  S.attach = (S.pendAtt[id] || []).filter(function (a) {
    return !a.conv || a.conv === id;
  }).concat(keep);
  store('ac-ai-conv', convId());
  var ta = byId('ai-text');
  if (ta) ta.value = store(draftKey()) || '';
  refreshExch().catch(function () {});
  refreshUsage().catch(function () {});
  loadActions().catch(function () {});
  paint();
}

async function loadConvs() {
  try {
    var out = await aapi('GET', '/conversations?limit=30');
    S.convList = out.items || [];
  } catch (e) { S.convList = S.convList || []; }
  if (S.rail) paint();
}

function startRename(id) {
  S.renaming = id;
  paint();
  var inp = document.querySelector &&
    document.querySelector('[data-ai-renin="' + id + '"]');
  if (inp && inp.focus) { inp.focus(); if (inp.select) inp.select(); }
}

async function renameConv(id, title) {
  S.renaming = null;
  try {
    var out = await aapi('POST',
      '/conversations/' + encodeURIComponent(id) + '/title',
      { title: title });
    var rec = out.conversation || out;
    if (S.convList)
      S.convList.forEach(function (c) {
        if (c.id === id) c.title = rec.title || '';
      });
    if (S.conv && S.conv.id === id) S.conv.title = rec.title || '';
  } catch (e) {
    note('Rename failed: ' + (e.message || e));
  }
  paint();
}

async function delConv(id) {
  var c = (S.convList || []).filter(function (r) {
    return r.id === id; })[0] || {};
  var label = c.title || '(untitled chat)';
  /* real confirmation first (the dashboard's modal); where it does not
     exist fall back to a two-click arm on the same button */
  var ok = false;
  if (typeof askConfirm === 'function') {
    ok = await askConfirm('Delete "' + label + '" - its messages and ' +
      'uploads are removed permanently.' + (c.size_bytes != null
        ? ' (' + fmtKb(c.size_bytes) + ' stored)' : ''),
      { danger: true, confirmText: 'Delete' });
  } else if (S.delArm[id]) {
    ok = true;
  } else {
    S.delArm[id] = true;
    note('Click the \u00d7 on "' + label + '" again to delete it.',
         false);
    paint();
    return;
  }
  delete S.delArm[id];
  try {
    await aapi('POST',
      '/conversations/' + encodeURIComponent(id) + '/delete', {});
  } catch (e) {
    note('Delete failed: ' + (e.message || e));
    return;
  }
  if (S.convList)
    S.convList = S.convList.filter(function (r) { return r.id !== id; });
  if (convId() === id) {               // the open chat is gone - reset
    S.conv = null; S.msgs = []; S.exch = {}; S.exchByAid = {};
    S.usage = null; S.actions = {}; S.armed = {}; S.pollXid = null;
    S.convRef = uid('conv');
    store('ac-ai-conv', '');
    var ta = byId('ai-text');
    if (ta) ta.value = store(draftKey()) || '';
  }
  paint();
}

async function refreshExch() {
  if (!convId()) return;
  var out = await aapi('GET',
    '/conversations/' + encodeURIComponent(convId()) + '/exchanges');
  S.exch = {}; S.exchByAid = {};
  (out.items || []).forEach(function (x) {
    if (x.user_msg_id) S.exch[x.user_msg_id] = x;
    if (x.assistant_msg_id) S.exchByAid[x.assistant_msg_id] = x;
    /* a live exchange picked up after a reload resumes its poll */
    if (!TERMINAL[x.status] && S.pollXid !== x.id) pollX(x.id);
  });
}

async function refreshUsage() {
  if (!convId()) { S.usage = null; return; }
  try { S.usage = await aapi('GET',
    '/conversations/' + encodeURIComponent(convId()) + '/usage'); }
  catch (e) { S.usage = null; }
}

async function loadActions() {
  if (!convId()) { S.actions = {}; return; }
  try {
    var out = await aapi('GET',
      '/conversations/' + encodeURIComponent(convId()) + '/actions');
    var by = {};
    (out.items || []).forEach(function (a) {
      (by[a.exchange_id] = by[a.exchange_id] || []).push(a);
    });
    S.actions = by;
  } catch (e) { S.actions = {}; }
}

function findAction(aid) {
  var found = null;
  Object.keys(S.actions || {}).forEach(function (k) {
    (S.actions[k] || []).forEach(function (a) {
      if (a.id === aid) found = a;
    });
  });
  return found;
}

async function actDecide(aid, op) {
  var ac = findAction(aid);
  if (!ac) { note('That action is gone - reload the conversation.'); return; }
  var cid = ac.conversation_id;
  if (op === 'arm') { S.armed[aid] = true; paint(); return; }
  if (op === 'disarm') { delete S.armed[aid]; paint(); return; }
  delete S.armed[aid];
  var body = { approve: op === 'exec' };
  if (op === 'exec') {
    body.execute = true;
    if (ac.expected_ack) body.ack = ac.expected_ack;
  }
  try {
    statusLine(op === 'exec' ? 'Running action' : 'Updating action');
    var out = await aapi('POST',
      '/conversations/' + encodeURIComponent(cid) + '/actions/' +
      encodeURIComponent(aid) + '/confirm', body);
    ac = out || ac;
    // reflect the fresh row in the cached list so the card repaints
    // with the audited state even before loadActions returns
    Object.keys(S.actions || {}).forEach(function (k) {
      var i = (S.actions[k] || []).findIndex(function (x) {
        return x.id === ac.id; });
      if (i >= 0) S.actions[k][i] = ac;
    });
    if (op === 'exec' && ac.state === 'executed')
      note('Action ran: ' + String(ac.kind).replace(/_/g, ' '), false);
    else if (ac.state === 'failed')
      note('Action failed: ' +
        (thaw(ac.result).error || 'see the card'));
  } catch (e) {
    note('Action failed: ' + (e.message || e));
  }
  await loadActions();
  statusLine('');
  paint();
}

/* ------------------------------------------------------------- send */
async function send() {
  var ta = byId('ai-text');
  var text = ta ? ta.value.trim() : '';
  /* only ids already uploaded under THIS conversation bind - a slot
     that migrated to another conv's stash is never sent here, and a
     slot can never send without a live conversation to scope it */
  var ready = S.attach.filter(function (a) {
    return a.id && a.conv && a.conv === convId();
  });
  if (S.sending) return;
  if (S.attach.some(function (a) { return a.status === 'uploading'; })) {
    note('Image uploads are still in flight - send in a moment.',
         false);
    return;
  }
  if (S.attach.some(function (a) { return a.status === 'error'; })) {
    note('An image failed to upload - remove it (\u00d7) or re-attach ' +
         'before sending.');
    return;
  }
  if (!text && !ready.length) return;
  teardownDict();              // the box clears; the recording must not
                               // keep writing into the next draft
  S.sending = true;
  setSendState();
  note('');
  statusLine('Sending');
  /* optimistic bubble: the user sees their message at once - the conv
     create + message POST round-trips below can take seconds */
  var cmid = uid('m'), optId = 'local-' + cmid;
  S.msgs.push({id: optId, role: 'user', text: text,
    client_msg_id: cmid, pending: true});
  S.pinBottom = true;
  paint();
  try {
    var conv = await ensureConv();
    var out = await aapi('POST',
      '/conversations/' + encodeURIComponent(conv.id) + '/messages',
      { text: text, client_msg_id: cmid, client_ctx: curCtx(),
        attachments: ready.map(function (a) { return a.id; }) });
    /* the returned pair joins the local list; dedupe by id/client_msg_id
       keeps a re-send from double-showing */
    var um = out.message || out.user_message;
    var am = out.assistant_message;
    var x = out.exchange;
    /* the exchange is persisted server-side either way; only reflect it
       here when the visible thread is still the one it belongs to - a
       conv switch during the POST must not inject foreign messages */
    var sameConv = S.conv && S.conv.id === conv.id;
    if (sameConv) {
      /* swap the optimistic placeholder for the persisted row - the
         client_msg_id dedupe below must see the real message */
      S.msgs = S.msgs.filter(function (o) { return o.id !== optId; });
      [um, am].forEach(function (m) {
        if (m && !S.msgs.some(function (o) {
          return o.id === m.id ||
            (o.client_msg_id && o.client_msg_id === m.client_msg_id);
        })) S.msgs.push(m);
      });
      if (x && um) S.exch[um.id] = x;
      if (x && am) S.exchByAid[am.id] = x;
    }
    if (ta) { ta.value = ''; }
    S.attach = [];
    if (convId()) S.pendAtt[convId()] = [];
    paintAtt();
    store(draftKey(), '');
    S.pinBottom = true;
    paint();
    statusLine('Waiting for a reply');
    if (x && !TERMINAL[x.status]) pollX(x.id);
    else { loadConvs(); refreshUsage().then(paint); }
  } catch (e) {
    S.msgs = S.msgs.filter(function (o) { return o.id !== optId; });
    paint();
    note('Send failed: ' + (e.message || e));
    statusLine('Send failed');
  } finally {
    S.sending = false;
    setSendState();
  }
}

function setSendState() {
  var sb = byId('ai-send'), ta = byId('ai-text'), st = byId('ai-stop');
  if (sb) sb.disabled = !!S.sending;
  var live = S.pollXid != null;
  if (st) st.hidden = !live;
}

async function pollX(xid) {
  if (S.pollXid === xid) return;              // already tracking
  S.pollXid = xid;
  setSendState();
  var ticks = 0;
  while (S.pollXid === xid) {
    await new Promise(function (r) { setTimeout(r, POLL_MS); });
    if (S.pollXid !== xid) break;
    /* stop watching after ~10 min - the exchange keeps running
       server-side and the next reload/history pass re-arms polling on
       any still-live exchange */
    if (++ticks > 540) {
      S.pollXid = null;
      note('The reply is taking a long time - it keeps running '
        + 'server-side; reopen this conversation later to check on it.');
      statusLine('Long reply in progress');
      setSendState();
      return;
    }
    var x;
    try { x = await aapi('GET', '/exchanges/' + encodeURIComponent(xid)); }
    catch (e) {
      note('Reply status check failed: ' + (e.message || e));
      break;
    }
    if (S.pollXid !== xid) break;
    if (x.user_msg_id) S.exch[x.user_msg_id] = x;
    if (x.assistant_msg_id) S.exchByAid[x.assistant_msg_id] = x;
    paint();
    if (TERMINAL[x.status]) {
      S.pollXid = null;
      statusLine(x.status === 'done' ? 'Reply received'
        : x.status === 'cancelled' ? 'Reply stopped'
        : 'Reply failed');
      if (x.status === 'error') {
        var etxt = String(x.error || '?');
        note('Reply failed: ' + etxt +
          (/image attachment refused|no verified image support/i
            .test(etxt)
            ? ' - open settings (the model name in the header) to pick ' +
              'a vision-capable model' : ''));
      }
      loadConvs(); refreshUsage(); loadActions();
      setSendState();
      paint();
      return;
    }
  }
  setSendState();
}

async function stopReply() {
  var xid = S.pollXid;
  if (!xid) return;
  try { await aapi('POST', '/exchanges/' + encodeURIComponent(xid) +
    '/cancel'); }
  catch (e) { note('Stop failed: ' + (e.message || e)); }
}

/* ------------------------------------------------------ attachments */
var ATT_MAX_BYTES = 20 * 1024 * 1024;    // mirrors the server cap
var ATT_PER_MSG = 20;

function stashAttach() {
  /* park pending uploads under the conversation each one belongs to -
     server-side rows are conv-scoped, so they can never ride along to
     a different chat; they come back via loadConv when their own
     conversation does. Unresolved slots (ensureConv still in flight)
     park with the conv that was visible at stash time - once their
     upload resolves, the migrate step re-parks them correctly */
  var cur = convId();
  S.attach.forEach(function (a) {
    var cid = a.conv || cur;
    if (cid)
      (S.pendAtt[cid] = S.pendAtt[cid] || []).push(a);
  });
  S.attach = [];
}

function paintAtt() {
  var el = byId('ai-ats');
  if (!el) return;
  el.hidden = !S.attach.length;
  el.innerHTML = S.attach.map(function (a, i) {
    var inner;
    if (a.status === 'uploading')
      inner = '<span class="ai-at-msg">uploading\u2026</span>';
    else if (a.status === 'error')
      inner = '<span class="ai-at-msg err">upload failed</span>';
    else
      inner = '<img src="' + API + '/attachments/' +
        encodeURIComponent(a.id) + '" alt="' +
        esc(a.name || 'image') + '">';
    return '<span class="ai-at' + (a.status ? ' ' + a.status : '') +
      '">' + inner + '<button type="button" data-ai-atrm="' + i + '" ' +
      'aria-label="Remove ' + esc(a.name || 'image') + '" ' +
      'title="Remove attachment">\u00d7</button></span>';
  }).join('');
}

function removeAtt(i) {
  /* removal only drops the pending reference: the server row stays
     orphaned under its conversation (message_id never set) - same
     tolerance as the request form, no delete endpoint needed. `dead`
     marks a slot removed while its upload was still in flight - the
     post-upload park below must never resurrect it onto the stash. */
  var slot = S.attach[i];
  if (slot) slot.dead = true;
  S.attach.splice(i, 1);
  paintAtt();
}

async function uploadAtt(file) {
  var slot = { id: null, name: file.name ||
    ('pasted-' + Date.now().toString(36) +
      ((String(file.type || '').split('/')[1]) || 'png')
      .replace(/[^a-z0-9]/gi, '') + '.png'),
    ctype: file.type || '', bytes: file.size || 0,
    status: 'uploading', conv: null };
  S.attach.push(slot);
  paintAtt();
  statusLine('Uploading image');
  try {
    /* uploads are conversation-scoped - attaching before the first
       send materializes the conversation now so the file can never
       land on the wrong thread */
    var conv = await ensureConv();
    slot.conv = conv.id;
    /* the slot is scoped to ITS conv - if the visible thread moved on
       while this upload was in flight (a switch, or a still-running
       restore whose loadConv already replaced S.attach), park it under
       its own conv's stash instead of leaving it on the wrong thread */
    if (!slot.dead &&
        (convId() !== conv.id || S.attach.indexOf(slot) < 0)) {
      var i = S.attach.indexOf(slot);
      if (i >= 0) S.attach.splice(i, 1);
      var target = S.pendAtt[conv.id] = S.pendAtt[conv.id] || [];
      if (target.indexOf(slot) < 0) target.push(slot);
      paintAtt();
    }
    var out = await aapi('POST',
      '/attachments?conversation=' + encodeURIComponent(conv.id) +
      '&ref=' + encodeURIComponent(uid('att')), file, true);
    slot.id = out.id;
    slot.name = out.name || slot.name;
    slot.ctype = out.ctype || slot.ctype;
    slot.bytes = out.bytes;
    slot.status = '';
    statusLine('Image attached');
  } catch (e) {
    slot.status = 'error';
    note('Image upload failed: ' + (e.message || e));
    statusLine('Image upload failed');
  }
  paintAtt();
}

function attachFiles(files) {
  files = Array.prototype.slice.call(files || []);
  var picked = [], skipped = [];
  files.forEach(function (f) {
    var t = String((f && f.type) || '');
    if (t.indexOf('image/') !== 0)
      skipped.push((f && f.name) || 'a non-image file');
    else if (f.size > ATT_MAX_BYTES)
      skipped.push(((f && f.name) || 'an image') + ' (over 20 MB)');
    else picked.push(f);
  });
  if (skipped.length)
    note('Skipped: ' + skipped.join(', ') +
         ' - only PNG/JPEG/GIF/WebP/BMP images up to 20 MB attach.');
  var room = ATT_PER_MSG - S.attach.length;
  if (picked.length > room) {
    note('At most ' + ATT_PER_MSG + ' images per message - the extra ' +
         'ones were skipped.');
    picked = picked.slice(0, room);
  }
  if (!picked.length) return;
  /* honest heads-up when the catalog is loaded AND says the current
     family lacks verified vision; the server still makes the call at
     dispatch (catalog absent = no warning, never a fake verdict) */
  var fam = S.conv ? S.conv.model : (S.defaults || {}).model;
  var ent = fam && famEntry(fam);
  if (S.catalog && fam && ent && ent.vision !== true)
    note('Heads-up: ' + fam + ' has no verified image support - the ' +
         'reply may refuse these images; assistant settings lists ' +
         'vision-capable models.', false);
  picked.forEach(uploadAtt);
}

/* ------------------------------------------------------- dictation */
function micCtor() {
  var w = (typeof window !== 'undefined') ? window : {};
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

function dictUI() {
  var b = byId('ai-mic');
  if (!b) return;
  var on = !!S.dict, avail = !!micCtor();
  b.className = 'ai-cbtn' + (on ? ' rec' : '') + (avail ? '' : ' na');
  b.setAttribute('aria-pressed', on ? 'true' : 'false');
  if (on) {
    b.title = 'Stop dictation - the transcript stays editable';
    statusLine('Listening - click the mic again to stop');
  } else {
    b.title = avail
      ? 'Dictate into the text box - audio is processed by your ' +
        'browser\u2019s speech service; words land as editable text ' +
        'and are never sent automatically'
      : 'Dictation unavailable - this browser has no speech ' +
        'recognition (Chrome, Edge and Safari support it; Firefox ' +
        'does not)';
  }
}

function startDict() {
  if (S.dict) {                       // explicit stop: results still in
    var d0 = S.dict;                  // flight may land, then onend
    d0.stopping = true;               // clears state
    try { d0.rec.stop(); } catch (e) { S.dict = null; }
    dictUI();
    return;
  }
  var Ctor = micCtor();
  if (!Ctor) {
    note('Voice input is not available in this browser - speech ' +
         'recognition works in Chrome, Edge and Safari but not here. ' +
         'The microphone stays off.', false);
    return;
  }
  var rec;
  try { rec = new Ctor(); }
  catch (e) {
    note('Could not start speech recognition: ' + (e.message || e));
    return;
  }
  var ta = byId('ai-text');
  var base = ta ? String(ta.value) : '';
  if (base && !/\s$/.test(base)) base += ' ';
  var d = { rec: rec, base: base, final: '', stopping: false };
  S.dict = d;
  rec.continuous = true;
  rec.interimResults = true;
  /* interim + final results rewrite only the dictated TAIL of the box
     (base + final + interim) - text the user typed stays untouched and
     everything remains editable until they send it themselves */
  rec.onresult = function (ev) {
    if (S.dict !== d) return;
    var fin = '', inter = '';
    var res = ev.results || [];
    for (var i = ev.resultIndex || 0; i < res.length; i++) {
      var r = res[i];
      var t = (r[0] && r[0].transcript) || '';
      if (r.isFinal) fin += t; else inter += t;
    }
    /* engines differ on whether transcripts arrive with their own
       spacing - join segments on a boundary so words can never fuse */
    if (fin) {
      if (d.final && !/\s$/.test(d.final) && !/^\s/.test(fin))
        d.final += ' ';
      d.final += fin;
    }
    var tail = d.final;
    if (inter) {
      if (tail && !/\s$/.test(tail) && !/^\s/.test(inter)) tail += ' ';
      tail += inter;
    }
    var t2 = byId('ai-text');
    if (t2) {
      t2.value = d.base + tail;
      store(draftKey(), t2.value);
    }
  };
  rec.onerror = function (ev) {
    var e = ev && ev.error;
    if (!e || e === 'aborted' || S.dict !== d) return;
    var known = {
      'not-allowed': 'Microphone permission denied - allow microphone ' +
        'access for this page to dictate.',
      'service-not-allowed': 'Microphone permission denied - allow ' +
        'microphone access for this page to dictate.',
      'audio-capture': 'No microphone was found on this device.',
      'network': 'The browser\u2019s speech service is unreachable - ' +
        'dictation audio is processed externally and needs that ' +
        'connection.'
    };
    if (e === 'no-speech')
      note('No speech was detected - dictation stopped.', false);
    else note(known[e] || ('Dictation error: ' + e));
    teardownDict();
  };
  rec.onend = function () {
    if (S.dict === d) { S.dict = null; dictUI(); }
  };
  try { rec.start(); }
  catch (e) {
    S.dict = null;
    dictUI();
    note('Could not start dictation: ' + (e.message || e));
    return;
  }
  dictUI();
  /* external-processing disclosure, once per tab; Web Speech hands
     audio to the browser vendor's service - we never see or store the
     recording, only the transcript */
  if (!sessStore('ac-ai-dict-note')) {
    sessStore('ac-ai-dict-note', '1');
    note('Dictation is on. Audio is processed by your browser\u2019s ' +
         'speech service (for Chrome/Edge that is the vendor\u2019s ' +
         'cloud service). Words land in the box as editable text - ' +
         'nothing sends automatically and no audio is stored here.',
         false);
  }
}

function teardownDict() {
  var d = S.dict;
  S.dict = null;                    // late results die with the state
  if (d && d.rec) {                 // reference, never the textarea
    try { d.rec.abort(); } catch (e) {}
  }
  dictUI();
}

/* ---------------------------------------------------------- settings */
function famList() {
  var fams = [], seen = {};
  ((S.catalog && S.catalog.models) || []).forEach(function (m) {
    var f = m.family || m.uid;
    if (f && !seen[f]) { seen[f] = 1; fams.push(f); }
  });
  return fams;
}
function suffixesFor(fam) {
  return ((S.catalog && S.catalog.models) || [])
    .filter(function (m) { return m.family === fam; })
    .map(function (m) {
      return m.uid === fam ? '' : m.uid.slice(fam.length + 1);
    });
}
var EFFORT_HEADS = { none: 1, low: 1, medium: 1, high: 1, xhigh: 1,
  max: 1 };
function effortsFor(fam) {
  var out = [], seen = {};
  suffixesFor(fam).forEach(function (s) {
    var head = s.split('-', 2)[0];
    /* effort heads are a fixed vocabulary - a bare marker suffix like
       '-priority' must not surface as an effort choice */
    if (s && !seen[head] && EFFORT_HEADS[head]) {
      seen[head] = 1; out.push(head);
    }
  });
  return out;
}
function fastMarkersFor(fam) {
  var out = [];
  suffixesFor(fam).forEach(function (s) {
    var p = s.split('-', 2);
    if (p.length === 2 && EFFORT_HEADS[p[0]] && out.indexOf(p[1]) < 0)
      out.push(p[1]);
  });
  return out;
}
function famEntry(fam) {
  var found = null;
  ((S.catalog && S.catalog.models) || []).forEach(function (m) {
    if (m.family === fam && !found) found = m;
  });
  return found;
}
function uidEntry(uid) {
  var found = null;
  ((S.catalog && S.catalog.models) || []).forEach(function (m) {
    if (m.uid === uid && !found) found = m;
  });
  return found;
}
function famIsFastBrand(fam) {
  return /(fast|lightning|priority|turbo)$/i.test(
    String(fam || '').replace(/[._]/g, '-'));
}

function roBool(v) {
  /* wire `readonly` is a sqlite int 0/1 - only an explicit off (false or
     0) means off; a missing key keeps the safe default (read-only) */
  return v !== false && v !== 0;
}

function settingsForm() {
  var cur = S.conv
    ? { model: S.conv.model, effort: S.conv.effort, fast: S.conv.fast,
        readonly: S.conv.readonly }
    : (S.defaults || {});
  var fams = famList();
  var fam = cur.model || (fams[0] || '');
  if (fams.indexOf(fam) < 0 && fams.length) fam = fams[0];
  /* draft selection lives in S.sel - the dialog never re-reads control
     values, so partial DOMs cannot desync it */
  S.sel = { model: fam, effort: cur.effort || '', fast: !!cur.fast,
    readonly: roBool(cur.readonly) };
  var catDown = !fams.length;
  var sel = byId('ais-model');
  if (sel) {
    sel.innerHTML = fams.map(function (f) {
      return '<option value="' + esc(f) + '"' +
        (f === fam ? ' selected' : '') + '>' + esc(f) + '</option>';
    }).join('') || '<option value="">(catalog unavailable)</option>';
    sel.disabled = catDown;
    sel.value = fam;
  }
  var mnote = byId('ais-model-note');
  if (mnote) {
    mnote.textContent = catDown
      ? 'the model catalog is unavailable - the devin CLI cannot be ' +
        'reached; settings cannot be changed right now'
      : 'only models in the live devin catalog are listed';
  }
  var saveBtn = byId('ais-save');
  if (saveBtn) saveBtn.disabled = catDown;   // never save a model-less form
  refillEffort();
  var fast = byId('ais-fast'), ro = byId('ais-ro');
  if (fast) fast.checked = !!cur.fast;
  if (ro) ro.checked = S.sel.readonly;
  settingsNotes();
}

function refillEffort() {
  var sel = S.sel || {};
  var es = effortsFor(sel.model);
  if (sel.effort && es.indexOf(sel.effort) < 0)
    sel.effort = es[0] || '';
  else if (!sel.effort && es.length)
    sel.effort = es[0];
  var el = byId('ais-effort');
  if (el) {
    el.innerHTML = es.map(function (e) {
      return '<option value="' + esc(e) + '"' +
        (e === sel.effort ? ' selected' : '') + '>' + esc(e) +
        '</option>';
    }).join('') || '<option value="">(default)</option>';
    el.value = sel.effort || '';
  }
}

function resolvedUid() {
  /* preview the uid the server will resolve - mirrors modelpick's
     family + effort + fast-marker composition */
  var sel = S.sel || {};
  var fam = sel.model, eff = sel.effort, fast = sel.fast;
  if (!fam) return null;
  if (fast && !famIsFastBrand(fam)) {
    var ms = fastMarkersFor(fam);
    var want = ms.map(function (m) { return eff ? eff + '-' + m : m; });
    var suf = suffixesFor(fam);
    var pick = want.filter(function (w) { return suf.indexOf(w) >= 0; })[0];
    if (pick == null) return fam + '-(' + (eff || 'default') + '-' +
      (ms[0] || 'fast') + ')';   // not resolvable - show the gap
    return fam + '-' + pick;
  }
  return eff ? fam + '-' + eff : fam;
}

function settingsNotes() {
  var fam = (S.sel || {}).model;
  var fast = byId('ais-fast');
  var markers = fastMarkersFor(fam);
  if (fast) {
    var hasFast = markers.length > 0 || famIsFastBrand(fam);
    fast.disabled = !hasFast;
    if (!hasFast) fast.checked = false, S.sel && (S.sel.fast = false);
    var fn = byId('ais-fast-note');
    if (fn) fn.textContent = hasFast
      ? '(variant: ' + (famIsFastBrand(fam)
          ? 'the family itself is the fast variant'
          : '-' + markers.join(' / -') + ')')
      : '(this model has no fast variant in the catalog)';
  }
  var pr = byId('ais-price');
  if (pr) {
    /* price + capability trade-offs BEFORE the selection lands: the
       resolved variant's own rates (family fallback when the variant is
       unpriced), the standard-vs-fast comparison when the fast toggle
       reprices, image capability, and where the numbers come from.
       Nothing here changes the selection by itself - a more expensive
       model is only ever the user's explicit pick. */
    var uidv = resolvedUid();
    var ent = uidv && uidEntry(uidv);
    var fent = famEntry(fam);
    var vis = ent ? ent.vision : (fent && fent.vision);
    var bits = [];
    bits.push(uidv ? 'resolves to ' + uidv : 'unresolved');
    if (ent && (ent['in'] != null || ent.out != null)) {
      bits.push('~$' + ent['in'] + ' in / $' + ent.out +
        ' out per 1M tok' + (ent.cached != null
          ? ' ($' + ent.cached + ' cached)' : ''));
      if (S.sel && S.sel.fast && !famIsFastBrand(fam)) {
        var plainUid = S.sel.effort
          ? fam + '-' + S.sel.effort : fam;
        var plain = uidEntry(plainUid);
        if (plain && plain['in'] != null && plain.uid !== ent.uid &&
            (plain['in'] !== ent['in'] || plain.out !== ent.out))
          bits.push('vs standard ' + (S.sel.effort || 'default') +
            ': ~$' + plain['in'] + ' / $' + plain.out);
      }
    } else if (fent && (fent['in'] != null || fent.out != null))
      bits.push('family ~$' + fent['in'] + ' in / $' + fent.out +
        ' out per 1M tok');
    else bits.push('no price data for this variant');
    bits.push(vis === true ? 'image input verified'
      : 'image input unverified');
    var pm = (S.catalog && S.catalog.pricing) || {};
    bits.push('estimates in ' + (pm.currency || 'USD') +
      (pm.fetched_at ? ' - rates fetched ' + fmtDay(pm.fetched_at)
                     : ' - rates never fetched') +
      (pm.source ? ' (' + pm.source + ')' : '') +
      ' - not an invoice');
    pr.textContent = bits.join(' \u00b7 ');
  }
}

function openSettings() {
  var w = byId('aisetwrap');
  if (!w) return;
  /* open FIRST, fetch SECOND: /models can take seconds cold (a `devin
     models list` subprocess server-side, cached ~10min there) - the old
     version hid that lag behind a dead-looking click. The last catalog
     is kept in localStorage so even a first-open-after-reload paints
     real selects immediately and revalidates in the background */
  w.hidden = false;
  if (!S.catalog) {
    try {
      var cc = JSON.parse(store('ac-ai-catalog') || 'null');
      if (cc && cc.data && cc.data.models) S.catalog = cc.data;
    } catch (e) { /* corrupt cache - the network copy replaces it */ }
  }
  settingsForm();
  var sel = byId('ais-model');
  if (sel && sel.focus) sel.focus();
  var need = [];
  need.push(aapi('GET', '/models').then(function (o) {
    S.catalog = o;
    try { store('ac-ai-catalog', JSON.stringify({ data: o })); }
    catch (e) {}
  }).catch(function (e) {
    if (!S.catalog || !S.catalog.models || !S.catalog.models.length) {
      var m = byId('ais-msg');
      if (m) { m.hidden = false; m.textContent =
        'Could not load the model catalog: ' + (e.message || e); }
    }
  }));
  if (!S.defaults) need.push(aapi('GET', '/settings').then(function (o) {
    S.defaults = (o && o.defaults) || {};
  }).catch(function () { S.defaults = S.defaults || {}; }));
  Promise.all(need).then(function () {
    if (!w.hidden) settingsForm();   // refresh the selects if still open
  });
}

function closeSettings() {
  var w = byId('aisetwrap');
  if (w) w.hidden = true;
  var nb = byId('ai-name');
  if (nb && nb.focus) nb.focus();
}

async function saveSettings() {
  var sel = S.sel || {};
  var fam = sel.model, eff = sel.effort;
  var fast = !!sel.fast, ro = !!sel.readonly;
  var m = byId('ais-msg');
  if (m) { m.hidden = true; m.textContent = ''; }
  var save = byId('ais-save');
  if (save) save.disabled = true;
  try {
    var body = { model: fam, effort: eff || '', fast: !!fast,
      readonly: !!ro };
    if (convId()) {
      var out = await aapi('POST',
        '/conversations/' + encodeURIComponent(convId()) + '/settings',
        body);
      S.conv = normConv(out.conversation || out);
    } else {
      var dres = await aapi('POST', '/settings', body);
      S.defaults = (dres && dres.defaults) || S.defaults;
    }
    closeSettings();
    paint();
  } catch (e) {
    if (m) { m.hidden = false; m.textContent = e.message || String(e); }
  } finally {
    if (save) save.disabled = false;
  }
}

/* -------------------------------------------------------- navigation */
var KIND_VIEW = { agent: 'agent', log: 'agent', run: 'run',
  summary: 'summary', 'run-summary': 'run-summary', request: 'request' };
function srcNav(s) {
  /* the stored nav descriptor wins; sources persisted before the
     server filled one in still navigate - kind+key are enough to
     rebuild the same descriptor (mirrors _resolve_src server-side) */
  var nav = (s && s.nav) || {};
  if (nav.view && nav.key) return nav;
  var v = KIND_VIEW[s && s.kind];
  var k = String((s && s.key) || '')
    .replace(/#(run-)?summary$/, '');
  if (!v || !k) return {};
  return { view: v, key: k };
}

function navSrc(view, key) {
  /* citation click: open the record in the dashboard's own modal chain;
     the chat surface is outside it and survives untouched */
  var nav = window.aiNav || {};
  var ok = false;
  try {
    if (view === 'agent' || view === 'log')
      ok = nav.agent ? nav.agent(key) : false;
    else if (view === 'summary')
      ok = nav.summary ? nav.summary(key) : false;
    else if (view === 'run-summary')
      ok = nav.runSummary ? nav.runSummary(key) : false;
    else if (view === 'run')
      ok = nav.run ? nav.run(key) : false;
    else if (view === 'request')
      ok = nav.request ? nav.request(key) : false;
  } catch (e) { ok = false; }
  if (!ok) note('That record is not on the dashboard right now ' +
    '(it may have finished or been filtered out).', false);
}

/* ------------------------------------------------------------- modes */
function applyMode() {
  var el = byId('aichat');
  if (!el) return;
  el.hidden = S.mode === 'closed';
  el.className = S.mode === 'expanded' ? 'expanded' : '';
  var fab = byId('aifab');
  if (fab) fab.hidden = S.mode !== 'closed';
  var dask = byId('dask');
  if (dask) dask.hidden = false;
  paint();
  if (S.mode === 'closed') {
    if (S.lastFocus && S.lastFocus.focus) {
      try { S.lastFocus.focus(); } catch (e) {}
    } else if (fab && fab.focus) { try { fab.focus(); } catch (e) {} }
  } else {
    var ta = byId('ai-text');
    if (ta && ta.focus && document.activeElement !== ta) {
      try { ta.focus(); } catch (e) {}
    }
  }
}

function openChat(mode) {
  if (document.activeElement && document.activeElement !== byId('aifab'))
    S.lastFocus = document.activeElement;
  S.mode = mode || (S.mode === 'closed' ? 'compact' : S.mode);
  if (S.mode === 'closed') S.mode = 'compact';
  store('ac-ai-open', S.mode);
  applyMode();
  /* first open (or a fresh reload) resumes the last conversation */
  if (!S.conv) {
    var stored = store('ac-ai-conv');
    var goal = stored
      ? loadConv(stored).catch(function () {
          return aapi('GET', '/last').then(function (o) {
            if (o.conversation) return loadConv(o.conversation.id);
          });
        })
      : aapi('GET', '/last').then(function (o) {
          if (o.conversation) return loadConv(o.conversation.id);
        });
    goal.catch(function (e) {
      note('Assistant is unavailable: ' + (e.message || e), false);
      paint();
    });
  }
  loadConvs();
}

function toggleChat() {
  if (S.mode === 'closed') openChat('compact');
  else minimize();
}
function minimize() {
  teardownDict();                // the mic never keeps recording into
                                 // a closed composer
  S.mode = 'closed';
  store('ac-ai-open', '');
  applyMode();
}
function setExpanded(on) {
  S.mode = on ? 'expanded' : 'compact';
  store('ac-ai-open', S.mode);
  applyMode();
}

function openConv(id) {
  if (id === convId()) return;
  /* the composer draft AND pending uploads are per-conversation -
     stash both before switching (uploads are conv-scoped server-side,
     so they can only ever rejoin their own conversation) */
  teardownDict();
  var ta = byId('ai-text');
  if (ta) store(draftKey(), ta.value);
  stashAttach();
  S.conv = null; S.msgs = []; S.exch = {}; S.exchByAid = {};
  S.usage = null; S.actions = {}; S.armed = {};
  S.renaming = null;
  S.pollXid = null;                    // orphan the old conv's poller
  paint();
  loadConv(id).catch(function (e) {
    note('Could not open that conversation: ' + (e.message || e));
  });
}

function newConv() {
  teardownDict();
  var ta = byId('ai-text');
  if (ta) store(draftKey(), ta.value);   // stash before switching
  stashAttach();
  S.conv = null; S.msgs = []; S.exch = {}; S.exchByAid = {};
  S.usage = null; S.actions = {}; S.armed = {};
  S.renaming = null;
  S.pollXid = null;                    // the previous conv's poller dies
  S.convRef = uid('conv');           // fresh create, fresh dedupe ref
  store('ac-ai-conv', '');
  if (ta) ta.value = store(draftKey()) || '';
  paint();
}

/* ------------------------------------------------------------- init */
function injectDom() {
  if (byId('aichat') || !document.body ||
      !document.body.insertAdjacentHTML) return;
  document.body.insertAdjacentHTML('beforeend',
    '<button type="button" id="aifab" data-ask-ai hidden ' +
      'aria-label="Ask AI" title="Ask AI about what you are looking at">' +
      '<span class="ai-spark" aria-hidden="true">\u2726</span>' +
      '<span>Ask AI</span></button>' +
    '<div id="aichat" role="dialog" aria-modal="false" ' +
      'aria-label="Ask AI assistant" hidden>' +
    '<div class="ai-head">' +
      '<button type="button" id="ai-name" class="ai-name"></button>' +
      '<button type="button" id="ai-state" class="ai-pill ro">' +
        'read-only</button>' +
      '<span class="ai-flex"></span>' +
      '<button type="button" id="ai-hist" class="ai-hbtn" ' +
        'aria-label="Conversation history" ' +
        'title="Conversation history">&#9776;</button>' +
      '<button type="button" id="ai-size" class="ai-hbtn" ' +
        'aria-label="Expand chat" title="Expand into the workspace">' +
        '\u2921</button>' +
      '<button type="button" id="ai-min" class="ai-hbtn" ' +
        'aria-label="Minimize chat" title="Minimize - the conversation ' +
        'keeps running">\u2014</button>' +
    '</div>' +
    /* second header line: the view chip + usage chip could never share
       the 400px title row without ellipsizing one of them into
       uselessness - they get their own strip */
    '<div class="ai-sub">' +
      '<span id="ai-ctx" class="ai-ctx">about: dashboard</span>' +
      '<span class="ai-flex"></span>' +
      '<details id="ai-usage" class="ai-usage">' +
        '<summary id="ai-usage-sum" ' +
          'title="conversation usage - expand for the breakdown">' +
        '</summary>' +
        '<div id="ai-useview" class="ai-useview" role="region" ' +
          'aria-label="Conversation usage breakdown"></div>' +
      '</details>' +
    '</div>' +
    '<div id="ai-note" class="ai-note" role="alert" hidden></div>' +
    '<div class="ai-main">' +
      '<div id="ai-list" class="ai-list" tabindex="0" ' +
        'aria-label="Conversation"></div>' +
    '</div>' +
    '<div class="ai-compose">' +
      '<div class="ai-cwrap">' +
        '<div id="ai-ats" class="ai-ats" hidden></div>' +
        '<textarea id="ai-text" rows="2" maxlength="90000" ' +
          'aria-label="Message the assistant" ' +
          'placeholder="Ask about this view\u2026"></textarea>' +
      '</div>' +
      '<div class="ai-cbtns">' +
        '<button type="button" id="ai-attach" class="ai-cbtn" ' +
          'aria-label="Attach images" ' +
          'title="Attach an image - or paste one straight into the ' +
          'text box"><svg viewBox="0 0 24 24" width="15" height="15" ' +
          'fill="none" stroke="currentColor" stroke-width="2" ' +
          'stroke-linecap="round" stroke-linejoin="round" ' +
          'aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 ' +
          '01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 ' +
          '01-2.83-2.83l8.49-8.48"/></svg></button>' +
        '<button type="button" id="ai-mic" class="ai-cbtn" ' +
          'aria-pressed="false" aria-label="Dictate into the text box" ' +
          'title="Dictate into the text box"><svg viewBox="0 0 24 24" ' +
          'width="15" height="15" fill="none" stroke="currentColor" ' +
          'stroke-width="2" stroke-linecap="round" ' +
          'stroke-linejoin="round" aria-hidden="true"><path d="M12 1a3 ' +
          '3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/><path d="M19 10v2a7 ' +
          '7 0 01-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/>' +
          '<line x1="8" y1="23" x2="16" y2="23"/></svg></button>' +
        '<button type="button" id="ai-send" class="ai-send" ' +
          'title="Send (Enter; Shift+Enter adds a newline)">Send</button>' +
        '<button type="button" id="ai-stop" class="ai-stop" hidden ' +
          'aria-label="Stop the reply" title="Stop the reply">' +
          '\u25a0</button>' +
      '</div>' +
      '<input type="file" id="ai-file" accept="image/*" multiple ' +
        'hidden aria-hidden="true" tabindex="-1">' +
    '</div>' +
    '<div id="ai-status" class="ai-sr" aria-live="polite"></div>' +
    '</div>' +
    /* history panel: a SEPARATE surface docked to the chat's left, not
       a column inside it - the 400px card keeps its full width for the
       conversation while the list floats beside it */
    '<div id="ai-rail" class="ai-rail" hidden ' +
      'aria-label="Conversation history">' +
      '<div class="ai-rail-h"><button type="button" id="ai-new">' +
        '+ New chat</button></div>' +
      '<div id="ai-convs" class="ai-convs"></div>' +
    '</div>' +
    '<div id="aisetwrap" hidden>' +
    '<div id="aiset" role="dialog" aria-modal="true" ' +
      'aria-labelledby="aiset-t">' +
      '<h4 id="aiset-t">Assistant settings</h4>' +
      '<div class="sub">Apply to the next exchange - earlier replies ' +
        'keep the settings they ran under.</div>' +
      '<label for="ais-model">Model</label>' +
      '<select id="ais-model"></select>' +
      '<div id="ais-model-note" class="sub"></div>' +
      '<label for="ais-effort">Effort</label>' +
      '<select id="ais-effort"></select>' +
      '<label class="check"><input type="checkbox" id="ais-fast"> ' +
        'Fast mode <span class="sub" id="ais-fast-note"></span></label>' +
      '<label class="check"><input type="checkbox" id="ais-ro" checked> ' +
        'Read-only <span class="sub">the assistant can look but not ' +
        'touch; turning this off still requires your confirmation per ' +
        'action</span></label>' +
      '<div id="ais-price" class="sub"></div>' +
      '<div id="ais-msg" class="notice" hidden></div>' +
      '<div class="actions">' +
        '<button type="button" id="ais-cancel" class="btn secondary">' +
          'Cancel</button>' +
        '<button type="button" id="ais-save" class="btn">' +
          'Save</button>' +
      '</div>' +
    '</div></div>');
}

/* the chat card and the history rail are sibling surfaces - one
   delegated click handler serves both */
function chatClick(e) {
  var t = e.target && e.target.closest ? e.target : e.target;
  if (!t || !t.closest) return;
  var el;
  if ((el = t.closest('#ai-send'))) { send(); return; }
  if ((el = t.closest('#ai-stop'))) { stopReply(); return; }
  if ((el = t.closest('#ai-attach'))) {
    var fi = byId('ai-file');
    if (fi && fi.click) fi.click();
    return;
  }
  if ((el = t.closest('#ai-mic'))) { startDict(); return; }
  if ((el = t.closest('[data-ai-atrm]'))) {
    removeAtt(+el.getAttribute('data-ai-atrm')); return; }
  if ((el = t.closest('#ai-min'))) { minimize(); return; }
  if ((el = t.closest('#ai-size'))) {
    setExpanded(S.mode !== 'expanded'); return; }
  if ((el = t.closest('#ai-hist'))) {
    S.rail = !S.rail; if (S.rail) loadConvs(); paint(); return; }
  if ((el = t.closest('#ai-name'))) { openSettings(); return; }
  if ((el = t.closest('#ai-state'))) { openSettings(); return; }
  if ((el = t.closest('#ai-new'))) { newConv(); return; }
  if ((el = t.closest('[data-ai-ren]'))) {
    startRename(el.getAttribute('data-ai-ren')); return; }
  if ((el = t.closest('[data-ai-del]'))) {
    delConv(el.getAttribute('data-ai-del')); return; }
  if ((el = t.closest('[data-ai-set]'))) { openSettings(); return; }
  if ((el = t.closest('[data-ai-note-x]'))) { note(''); return; }
  if ((el = t.closest('[data-ai-nav]'))) {
    if (e.preventDefault) e.preventDefault();  // in-text nav links carry '#'
    navSrc(el.getAttribute('data-ai-nav'),
           el.getAttribute('data-ai-key')); return; }
  if ((el = t.closest('[data-ai-conv]'))) {
    openConv(el.getAttribute('data-ai-conv')); return; }
  if ((el = t.closest('[data-ai-act]'))) {
    var ap = String(el.getAttribute('data-ai-act')).split(':');
    if (ap.length === 2 && !el.disabled) actDecide(ap[0], ap[1]);
    return; }
}

function bind() {
  /* launchers live outside #aichat (page FAB + the modal shell's #dask);
     one delegated document listener covers both plus anything the host
     emits later - a click never reaches the row handlers because these
     controls are not inside a table row */
  document.addEventListener('click', function (e) {
    var t = e.target && e.target.closest ? e.target : null;
    var lb = t && t.closest && t.closest('[data-ask-ai]');
    if (lb) { e.preventDefault(); toggleChat(); return; }
    /* light-dismiss the header usage dropdown - a native <details>
       stays open until its summary is clicked again, so a click on any
       neutral surface closes it here (a click inside it is left alone) */
    var ud = byId('ai-usage');
    if (ud && ud.open && !(t && t.closest && t.closest('#ai-usage')))
      ud.open = false;
    /* the history rail is NOT light-dismissed: only the header's
       hamburger toggles it (a clicked-inside element may be detached by
       the rail's own repaint before this delegated listener runs, so a
       closest() guard here can never be trusted anyway) */
  });
  var rail = byId('ai-rail');
  if (rail) {
    rail.addEventListener('click', chatClick);
    /* rename <input>: Enter commits, Escape cancels without letting the
       chat-level Escape handler minimize; leaving the field commits */
    rail.addEventListener('keydown', function (e) {
      var inp = e.target && e.target.closest &&
        e.target.closest('[data-ai-renin]');
      if (!inp) return;
      if (e.key === 'Enter') {
        e.preventDefault();
        renameConv(inp.getAttribute('data-ai-renin'),
                   inp.value || '');
      } else if (e.key === 'Escape') {
        e.preventDefault();
        if (e.stopPropagation) e.stopPropagation();
        S.renaming = null; paint();
      }
    });
    rail.addEventListener('focusout', function (e) {
      var inp = e.target && e.target.closest &&
        e.target.closest('[data-ai-renin]');
      if (inp && S.renaming === inp.getAttribute('data-ai-renin'))
        renameConv(inp.getAttribute('data-ai-renin'), inp.value || '');
    });
  }
  var chat = byId('aichat');
  if (chat) {
    chat.addEventListener('click', chatClick);
    /* Escape minimizes chat (never the modal behind it); Left/Right are
       swallowed so a focused chat control cannot drive modal sibling
       nav underneath */
    chat.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        e.stopPropagation();
        var ud = byId('ai-usage');
        if (ud && ud.open) {            // topmost layer dismisses first
          ud.open = false;
        } else if (byId('aisetwrap') && !byId('aisetwrap').hidden) {
          closeSettings();
        } else minimize();
        e.preventDefault();
        return;
      }
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        var tag = e.target && e.target.tagName;
        if (tag !== 'TEXTAREA' && tag !== 'INPUT')
          e.stopPropagation();
      }
    });
    chat.addEventListener('input', function (e) {
      if (e.target && e.target.id === 'ai-text') {
        store(draftKey(), e.target.value);
      }
    });
    var list = byId('ai-list');
    if (list) list.addEventListener('scroll', function () {
      S.pinBottom = list.scrollTop + list.clientHeight >=
        list.scrollHeight - 30;
    });
    var ta = byId('ai-text');
    if (ta) {
      ta.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          e.stopPropagation();
          send();
        }
      });
      /* paste carries two payloads: FILE items go through attachFiles
         (images upload, everything else is skipped with a note - the
         default paste is suppressed only once a file is present), and
         a clipboard without files falls through to the browser's
         normal text paste untouched */
      ta.addEventListener('paste', function (e) {
        var cd = e.clipboardData;
        if (!cd || !cd.items) return;
        var files = [];
        for (var i = 0; i < cd.items.length; i++) {
          var it = cd.items[i];
          if (it && it.kind === 'file' && it.getAsFile) {
            var f = it.getAsFile();
            if (f) files.push(f);
          }
        }
        if (!files.length) return;
        e.preventDefault();
        attachFiles(files);
        /* a clipboard can carry files AND text together (copied from a
           document the caption rides alongside) - keep the text too */
        var txt = '';
        try { txt = cd.getData ? cd.getData('text/plain') : ''; }
        catch (e2) {}
        if (txt) {
          var st = ta.selectionStart, en = ta.selectionEnd;
          ta.value = ta.value.slice(0, st) + txt + ta.value.slice(en);
          ta.selectionStart = ta.selectionEnd = st + txt.length;
        }
      });
    }
    var fi = byId('ai-file');
    if (fi) fi.addEventListener('change', function (e) {
      attachFiles(e.target && e.target.files);
      try { e.target.value = ''; }   // re-picking the same file refires
      catch (e2) {}
    });
  }
  var setw = byId('aisetwrap');
  if (setw) {
    setw.addEventListener('click', function (e) {
      var t = e.target && e.target.closest ? e.target : null;
      if (!t || !t.closest) return;
      if (t === setw) { closeSettings(); return; }
      if (t.closest('#ais-cancel')) { closeSettings(); return; }
      if (t.closest('#ais-save')) { saveSettings(); return; }
    });
    setw.addEventListener('change', function (e) {
      var id = e.target && e.target.id;
      var v = e.target ? e.target.value : '';
      var ck = e.target ? !!e.target.checked : false;
      if (id === 'ais-model') {
        S.sel.model = v;
        S.sel.effort = '';
        refillEffort();
        settingsNotes();
      } else if (id === 'ais-effort') { S.sel.effort = v; settingsNotes(); }
      else if (id === 'ais-fast') { S.sel.fast = ck; settingsNotes(); }
      else if (id === 'ais-ro') { S.sel.readonly = ck; }
    });
    setw.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        e.stopPropagation();
        closeSettings();
        return;
      }
      /* aria-modal means focus stays inside - cycle Tab between the
         dialog's focusables instead of leaking to the page behind */
      if (e.key === 'Tab') {
        var f = setw.querySelectorAll(
          'select, input, button, [tabindex]:not([tabindex="-1"])');
        f = Array.prototype.filter.call(f, function (el) {
          return !el.disabled && el.offsetParent !== null; });
        if (!f.length) return;
        var first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault(); last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault(); first.focus();
        }
      }
    });
  }
}

function init() {
  if (typeof document === 'undefined' || !document.body) return;
  injectDom();
  if (!byId('aichat')) return;          // host page without body hooks
  bind();
  S.win = sessStore('ac-ai-win') || sessStore('ac-ai-win', uid('win'));
  S.convRef = uid('conv');
  /* restore: panel state, last conversation, composer draft */
  var mode = store('ac-ai-open');
  var dask = byId('dask');
  if (dask) dask.hidden = false;
  dictUI();                       // paint the mic's real availability
  if (mode === 'compact' || mode === 'expanded') {
    openChat(mode);
    var ta = byId('ai-text');
    var d = store(draftKey()) || store('ac-ai-draft');
    if (ta && d) ta.value = d;
  } else {
    var fab = byId('aifab');
    if (fab) fab.hidden = false;
  }
  paint();
}

var AskAI = {
  open: openChat, close: minimize, toggle: toggleChat,
  expand: function () { setExpanded(true); },
  compact: function () { setExpanded(false); },
  openSettings: openSettings, nav: navSrc, send: send,
  ctx: curCtx, ctxLabel: ctxLabel, syncView: syncView,
  _s: S, _paint: paint, _aapi: aapi, _paintAtt: paintAtt,
  _attachFiles: attachFiles, _dictUI: dictUI,
  _msgHtml: msgHtml, _settingsForm: settingsForm,
  _actHtml: actHtml, _actDecide: actDecide,
  _railHtml: railHtml, _delConv: delConv, _renameConv: renameConv
};
if (typeof window !== 'undefined') {
  window.AskAI = AskAI;
  window.aiSync = syncView;
}
if (typeof globalThis !== 'undefined') {
  /* globalThis === window in a browser; vm test contexts only have the
     former - the dashboard's applySel calls aiSync via globalThis */
  globalThis.AskAI = AskAI;
  globalThis.aiSync = syncView;
}
if (typeof document !== 'undefined' && document.body) init();
else if (typeof document !== 'undefined' &&
         document.addEventListener)
  document.addEventListener('DOMContentLoaded', init);
})();
