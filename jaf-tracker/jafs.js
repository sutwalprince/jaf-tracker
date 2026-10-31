import { ORIGIN, money, fmtDate, ago, until, esc, roundsHtml } from './common.js';

const $ = id => document.getElementById(id);
const DAY = 86400000;

const VIEWS = {
  can: { label: 'You can sign', test: j => !j.signed && j.eligible },
  bonus: { label: 'Bonus allowed', test: j => !j.signed && !j.eligible && j.bonus },
  signed: { label: 'Already signed', test: j => j.signed },
  ineligible: { label: 'Ineligible', test: j => !j.signed && !j.eligible && !j.bonus },
  all: { label: 'All open', test: () => true },
};

let state = {};
let view = 'can';
const expanded = new Set();

async function load() {
  state = await chrome.storage.local.get(null);
  render();
}

function render() {
  const open = state.openJobs || [];
  const prog = state.me?.programName || 'your program';
  const q = $('q').value.trim().toLowerCase();

  const me = state.me;
  $('who').textContent = me ? [me.name, me.roll, me.programName].filter(Boolean).join(' · ') : 'Not synced yet';
  const loggedOut = state.auth && !state.auth.ok;
  $('status').textContent = state.syncing ? 'Syncing…' : loggedOut ? 'Logged out' : `Synced ${ago(state.lastSync)}`;
  $('status').classList.toggle('bad', !!loggedOut);
  $('sync').disabled = !!state.syncing;
  $('login').hidden = !loggedOut;
  $('loginMsg').textContent = state.auth?.msg || '';
  $('error').hidden = !state.error;
  $('error').textContent = state.error ? `Last sync failed: ${state.error}` : '';

  const can = open.filter(VIEWS.can.test);
  const bonus = open.filter(VIEWS.bonus.test);
  const soon = [...can, ...bonus].filter(j => new Date(j.closesAt) - Date.now() < DAY);
  const best = [...can, ...bonus].filter(j => j.currency === 'INR' && j.ctc).sort((a, b) => b.ctc - a.ctc)[0];
  $('stats').innerHTML = [
    stat('Open now', open.length, `${new Set(open.map(j => j.company)).size} companies`),
    stat('You can sign', can.length, 'eligible, not signed yet', can.length > 0),
    stat('Bonus allowed', bonus.length, 'ineligible, but bonus JAF allowed'),
    stat('Closing in 24 h', soon.length, 'that you can still sign', false, soon.length > 0),
    stat(`Top ${prog} CTC`, best ? money(best.ctc, best.currency) : '—', best ? best.company : 'among signable JAFs'),
  ].join('');

  $('views').innerHTML = Object.entries(VIEWS).map(([k, v]) =>
    `<button class="chip ${view === k ? 'on' : ''}" data-view="${k}">${esc(v.label)} (${open.filter(v.test).length})</button>`).join('');

  const sort = $('sort').value;
  const shown = open
    .filter(VIEWS[view].test)
    .filter(j => !q || `${j.company} ${j.title} ${j.locations.join(' ')} ${j.designation}`.toLowerCase().includes(q))
    .sort(sort === 'ctc' ? (a, b) => (b.currency === 'INR' ? b.ctc || 0 : 0) - (a.currency === 'INR' ? a.ctc || 0 : 0)
      : sort === 'newest' ? (a, b) => new Date(b.opensAt) - new Date(a.opensAt)
      : (a, b) => new Date(a.closesAt) - new Date(b.closesAt));

  $('list').innerHTML = shown.length ? shown.map(j => card(j, prog)).join('')
    : `<div class="empty">${state.lastSync ? 'No JAFs here right now.' : 'Waiting for the first sync…'}</div>`;
}

function stat(label, value, sub, good = false, urgent = false) {
  return `<div class="stat ${good ? 'hot' : ''}"><div class="label">${esc(label)}</div><div class="value ${urgent ? 'urgent' : ''}">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>`;
}

function card(j, prog) {
  const left = new Date(j.closesAt) - Date.now();
  const open = expanded.has(j.id);
  const tags = [
    j.signed ? '<span class="tag yes">✓ Signed</span>' : '',
    j.eligible ? '<span class="tag yes">Eligible</span>' : '<span class="tag no">Ineligible</span>',
    j.bonus ? '<span class="tag">Bonus allowed</span>' : '<span class="tag muted">Bonus disallowed</span>',
    j.bond ? `<span class="tag no">Bond: ${esc(j.bond)}</span>` : '',
  ].join('');
  return `<article class="job">
    <div class="row">
      <div><div class="company">${esc(j.company)}</div><div class="role">${esc(j.title)} · Job code ${esc(j.code)}</div></div>
      <div class="ctc">${esc(money(j.ctc, j.currency))}<small>${j.forMyProgram ? `${esc(prog)} CTC` : 'CTC (not listed for your program)'}${
        j.gross != null && j.gross !== j.ctc ? ` · ${esc(money(j.gross, j.currency))} gross` : ''}</small></div>
    </div>
    <div class="tags">${tags}</div>
    <dl>
      <div><dt>Location</dt><dd>${esc(j.locations.join(', ') || '—')}</dd></div>
      <div><dt>CPI cutoff</dt><dd>${j.cpiCutoff ? esc(j.cpiCutoff) : 'None'}</dd></div>
      <div><dt>Opened</dt><dd>${esc(fmtDate(j.opensAt))}</dd></div>
      <div><dt>Closes</dt><dd class="${left < DAY ? 'urgent' : 'open'}">${esc(fmtDate(j.closesAt))} · ${esc(until(j.closesAt))}</dd></div>
    </dl>
    ${roundsHtml(j)}
    ${j.designation && j.designation !== j.title ? `<div class="role">Designation: ${esc(j.designation)}</div>` : ''}
    <div class="cardfoot">
      <button class="more" data-more="${j.id}" aria-expanded="${open}">${open ? 'Hide details' : 'Show details'}</button>
      <a class="golink ${!j.signed && (j.eligible || j.bonus) ? '' : 'quiet'}" href="${ORIGIN}/applicant/jobs" target="_blank" rel="noopener">${
        j.signed ? 'View on portal ↗' : j.eligible ? 'Go to sign JAF ↗' : j.bonus ? 'Go to sign (bonus) ↗' : 'View on portal ↗'}</a>
    </div>
    ${open ? `<div class="detail">
      ${j.requirements && !/^n\/?a$/i.test(j.requirements) ? `<h4>Requirements</h4><pre>${esc(j.requirements)}</pre>` : ''}
      ${j.info && !/^(n\/?a|\.+)$/i.test(j.info) ? `<h4>Compensation &amp; info</h4><pre>${esc(j.info)}</pre>` : ''}
      ${j.description ? `<h4>Job description</h4><pre>${esc(j.description)}</pre>` : ''}
      <a href="${ORIGIN}/applicant/jobs" target="_blank" rel="noopener">Open Jobs page on the portal ↗</a>
    </div>` : ''}
  </article>`;
}

document.addEventListener('click', e => {
  const v = e.target.closest('[data-view]');
  if (v) { view = v.dataset.view; render(); return; }
  const m = e.target.closest('[data-more]');
  if (m) {
    const id = Number(m.dataset.more);
    if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
    render();
  }
});

$('q').addEventListener('input', render);
$('sort').addEventListener('change', render);
$('sync').addEventListener('click', () => chrome.runtime.sendMessage({ type: 'sync' }));
$('loginBtn').addEventListener('click', () => chrome.runtime.sendMessage({ type: 'login' }));

chrome.storage.onChanged.addListener(load);
setInterval(render, 60_000); // keep countdowns fresh
load();
