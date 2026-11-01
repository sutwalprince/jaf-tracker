import { relevantPosts, money, fmtDate, ago, esc, shortName, when, until, roundsHtml, postKind, cleanTitle, dayLabel, timeOf } from './common.js';
import { renderOpen, onOpenClick } from './jafs.js';

const $ = id => document.getElementById(id);

let state = {};
let filterJob = null;
let showHidden = false;
let unreadOnly = false;
// Three views on this page: "updates" (the blog feed, default), "jafs" (your signed JAFs), "open" (open JAFs).
const currentView = () => ({ '#jafs': 'jafs', '#open': 'open' })[location.hash] || 'updates';
let view = currentView();
window.addEventListener('hashchange', () => { view = currentView(); render(); window.scrollTo({ top: 0 }); });
const expanded = new Set();

let formFilled = false;

async function load() {
  state = await chrome.storage.local.get(null);
  if (!formFilled) {
    const srv = state.serverSync || {};
    $('srvUrl').value = srv.url || '';
    $('srvKey').value = srv.key || '';
    $('srvOn').checked = !!srv.enabled;
    formFilled = true;
  }
  render();
}

// Secrets stay masked unless you click Show; they hide again when you leave the page.
document.querySelectorAll('[data-reveal]').forEach(btn => btn.addEventListener('click', () => {
  const input = $(btn.dataset.reveal);
  const show = input.type === 'password';
  input.type = show ? 'text' : 'password';
  btn.textContent = show ? 'Hide' : 'Show';
}));

const srvSettings = () => ({ url: $('srvUrl').value.trim(), key: $('srvKey').value.trim(), enabled: $('srvOn').checked });

$('srvSave').addEventListener('click', async () => {
  await chrome.storage.local.set({ serverSync: srvSettings() });
  $('srvMsg').textContent = 'Saved.';
});

$('srvSend').addEventListener('click', async () => {
  const srv = srvSettings();
  if (!srv.url || !srv.key) { $('srvMsg').textContent = 'Enter the server address and key first.'; return; }
  await chrome.storage.local.set({ serverSync: { ...srv, enabled: true } });
  $('srvOn').checked = true;
  $('srvMsg').textContent = 'Sending…';
  const r = await chrome.runtime.sendMessage({ type: 'push-session' });
  $('srvMsg').textContent = r?.ok ? 'Sent. The server is syncing with your login now.' : `Failed: ${r?.msg || 'unknown error'}`;
});

// Your JAFs, minus companies you've hidden (unless "Show hidden" is on).
function visibleJobs() {
  const hidden = new Set(state.hiddenCompanies || []);
  const all = Object.values(state.jobs || {});
  return { all, hidden, jobs: showHidden ? all : all.filter(j => !hidden.has(j.company)) };
}

function render() {
  const { all, hidden, jobs } = visibleJobs();
  const hiddenShort = new Set([...hidden].map(shortName));
  const byId = Object.fromEntries(all.map(j => [j.id, j]));
  const read = new Set(state.readIds || []);
  const posts = relevantPosts(state.posts || [], jobs);
  const q = $('q').value.trim().toLowerCase();

  // View
  document.body.dataset.view = view;
  document.querySelectorAll('.tabs [data-view]').forEach(a => a.classList.toggle('on', a.dataset.view === view));
  $('pageTitle').textContent = { jafs: 'My JAFs', open: 'Open JAFs' }[view] || 'Updates';
  $('q').placeholder = view !== 'updates' ? 'Search company, role or location…' : 'Search companies or updates…';

  // Header
  const loggedOut = state.auth && !state.auth.ok;
  $('status').textContent = state.syncing ? 'Syncing…' : loggedOut ? 'Logged out' : state.lastSync ? `Synced ${ago(state.lastSync)}` : 'Not synced yet';
  $('status').classList.toggle('bad', !!loggedOut);
  $('sync').disabled = !!state.syncing;
  $('login').hidden = !loggedOut;
  $('loginMsg').textContent = state.auth?.msg || '';
  $('error').hidden = !state.error;
  $('error').textContent = state.error ? `Last sync failed: ${state.error}` : '';
  const chState = (cfg, st) => !cfg?.enabled ? '· off'
    : st && !st.ok ? `· last send failed: ${st.msg}`
    : st ? `· on, last sent ${ago(st.at)}` : '· on';
  $('srvState').textContent = chState(state.serverSync, state.sessionPush);

  // Stats
  const now = Date.now();
  const unread = posts.filter(p => !read.has(p.id)).length;
  const mentions = posts.filter(p => p.mentionsMe).length;
  const nextClose = jobs.filter(j => new Date(j.closesAt) > now).sort((a, b) => new Date(a.closesAt) - new Date(b.closesAt))[0];
  $('stats').innerHTML = [
    stat('JAFs signed', all.length, `${new Set(all.map(j => j.company)).size} companies${hidden.size ? `, ${hidden.size} hidden` : ''}`),
    stat('Unread updates', unread, `${posts.length} total`),
    stat('Mentions you', mentions, 'shortlists & lists with your roll/name', mentions > 0),
    stat('Next JAF deadline', nextClose ? fmtDate(nextClose.closesAt) : '—', nextClose ? nextClose.company : 'none open'),
  ].join('');

  // Upcoming tests / PPTs / interviews (read from blog posts)
  const ups = (state.events || [])
    .filter(e => new Date(e.at) > now - 2 * 3600000 && (showHidden || !hiddenShort.has(e.company))).slice(0, 8);
  $('upcoming').hidden = !ups.length;
  $('upcoming').innerHTML = `<h2>Upcoming</h2><div class="uplist">${ups.map(e => `
    <a class="up" href="${esc(e.link)}" target="_blank" rel="noopener">
      <span class="upicon">${e.icon}</span>
      <span><b>${esc(e.company)}</b> · ${esc(e.kind)}<br><span class="muted">${esc(when(e.at))}${e.venue ? ` · ${esc(e.venue)}` : ''}</span></span>
      <span class="upleft">${new Date(e.at) > now ? esc(until(e.at)) : 'now'}</span>
    </a>`).join('')}</div>`;

  // Company chips
  const companies = [...new Map(jobs.map(j => [j.company, j.id])).entries()];
  $('chips').innerHTML = [`<button class="chip ${filterJob ? '' : 'on'}" data-filter="">All</button>`,
    ...companies.map(([c, id]) => {
      const ids = jobs.filter(j => j.company === c).map(j => j.id);
      const on = filterJob && ids.includes(filterJob);
      return `<button class="chip ${on ? 'on' : ''}" data-filter="${id}">${esc(shortName(c))}</button>`;
    })].join('');

  // Feed
  const selected = filterJob ? jobs.filter(j => j.company === byId[filterJob]?.company).map(j => j.id) : null;
  // Unread-only keeps a post you've just opened on screen until you close it.
  const shown = posts.filter(p =>
    (!unreadOnly || !read.has(p.id) || expanded.has(p.id)) &&
    (!selected || p.jobIds.some(id => selected.includes(id))) &&
    (!q || `${p.title} ${p.text}`.toLowerCase().includes(q)));
  $('feedCount').textContent = `(${shown.length})`;
  let lastDay = null; // a heading before each day's posts
  $('feed').innerHTML = shown.length ? shown.map(p => {
    const day = dayLabel(p.date);
    const head = day !== lastDay ? `<h3 class="dayhead">${esc(day)}</h3>` : '';
    lastDay = day;
    return head + postHtml(p, byId, read);
  }).join('')
    : `<div class="empty">${!state.lastSync ? 'Waiting for the first sync…' : unreadOnly ? 'No unread updates.' : 'No updates match.'}</div>`;
  $('unreadOnly').textContent = unreadOnly ? 'Show all updates' : `Show unread updates (${unread})`;
  $('tabUnread').hidden = !unread;
  $('tabUnread').textContent = unread;
  $('unreadOnly').setAttribute('aria-pressed', String(unreadOnly));
  $('unreadOnly').classList.toggle('on', unreadOnly);

  // Jobs: ones with a test/PPT coming up first (soonest first), then JAFs still open, then the rest by latest update.
  const nextEvent = j => (state.events || []).filter(e => e.company === shortName(j.company) && new Date(e.at) > now - 2 * 3600000)
    .sort((a, b) => new Date(a.at) - new Date(b.at))[0];
  const rank = x => x.next ? [0, new Date(x.next.at)] : new Date(x.j.closesAt) > now ? [1, new Date(x.j.closesAt)]
    : [2, -new Date(x.ups[0]?.date || x.j.signedAt)];
  const jobsShown = jobs
    .filter(j => !q || `${j.company} ${j.title} ${j.locations.join(' ')}`.toLowerCase().includes(q))
    .map(j => ({ j, ups: posts.filter(p => p.jobIds.includes(j.id)), next: nextEvent(j) }))
    .sort((a, b) => { const [ra, ka] = rank(a), [rb, kb] = rank(b); return ra - rb || ka - kb; });
  $('jobs').innerHTML = jobsShown.length ? jobsShown.map(({ j, ups, next }) => jobHtml(j, ups, selected, hidden.has(j.company), next)).join('')
    : `<div class="empty">${state.lastSync ? 'No JAFs match.' : 'Waiting for the first sync…'}</div>`;
  $('toggleHidden').hidden = !hidden.size;
  $('toggleHidden').textContent = showHidden ? 'Hide hidden companies' : `Show hidden (${hidden.size})`;

  if (view === 'open') renderOpen(state, q); // its own stats and cards
}

function stat(label, value, sub, hot = false) {
  return `<div class="stat ${hot ? 'hot' : ''}"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>`;
}


function postHtml(p, byId, read) {
  const cos = [...new Set(p.jobIds.map(id => shortName(byId[id]?.company || '')))].filter(Boolean);
  const open = expanded.has(p.id);
  const [kind, kcls] = postKind(p.title);
  const fullNames = [...new Set(p.jobIds.map(id => byId[id]?.company).filter(Boolean))];
  return `<article class="post ${read.has(p.id) ? '' : 'unread'} ${p.mentionsMe ? 'mention' : ''}">
    <button class="head" data-post="${esc(p.id)}" aria-expanded="${open}">
      <span class="dot"></span>
      <span class="pmain">
        <span class="meta"><span class="kind ${kcls}">${esc(kind)}</span>${cos.length ? `<span class="co">${esc(cos.join(', '))}</span>` : ''}<span class="time">${esc(timeOf(p.date))}</span></span>
        <span class="title">${esc(cleanTitle(p.title, fullNames))}</span>
      </span>
      ${p.mentionsMe ? '<span class="badge">Mentions you</span>' : ''}
    </button>
    ${open ? `<div class="body"><pre>${esc(p.text)}</pre><a href="${esc(p.link)}" target="_blank" rel="noopener">Open on blog ↗</a></div>` : ''}
  </article>`;
}

function jobHtml(j, ups, selected, isHidden, next) {
  const closed = new Date(j.closesAt) < Date.now();
  const latest = ups[0];
  return `<article class="job ${selected?.includes(j.id) ? 'on' : ''} ${isHidden ? 'muted-card' : ''}">
    <div class="cardtools"><button class="hidebtn" data-hide="${esc(j.company)}" title="${isHidden
      ? 'Show this company again' : 'Stop showing updates and reminders for this company'}">${isHidden ? 'Unhide' : 'Hide'}</button></div>
    <div class="row">
      <div><div class="company">${esc(shortName(j.company))}</div><div class="role">${esc(j.title)} · Job code ${esc(j.code)}</div></div>
      <div class="ctc">${esc(money(j.ctc, j.currency))}<small>CTC${j.gross != null && j.gross !== j.ctc ? ` · ${esc(money(j.gross, j.currency))} gross` : ''}</small></div>
    </div>
    ${next ? `<a class="nextup" href="${esc(next.link)}" target="_blank" rel="noopener"><span>${next.icon}</span><span><b>${esc(next.kind)}</b> ${esc(when(next.at))}</span><span class="upleft">${new Date(next.at) > Date.now() ? esc(until(next.at)) : 'now'}</span></a>` : ''}
    <dl>
      <div><dt>Location</dt><dd>${esc(j.locations.join(', ') || '—')}</dd></div>
      <div><dt>CPI cutoff</dt><dd>${j.cpiCutoff ? esc(j.cpiCutoff) : 'None'}</dd></div>
      <div><dt>Signed</dt><dd>${esc(fmtDate(j.signedAt))}</dd></div>
      <div><dt>JAF closes</dt><dd class="${closed ? 'closed' : 'open'}">${esc(fmtDate(j.closesAt))}${closed ? ' (closed)' : ''}</dd></div>
    </dl>
    ${roundsHtml(j)}
    <button class="latest" data-filter="${j.id}">${latest
      ? `${ups.length} update${ups.length === 1 ? '' : 's'} · latest: <b>${esc(cleanTitle(latest.title, [j.company]))}</b> (${esc(ago(latest.date))}) →`
      : 'No blog updates yet'}</button>
  </article>`;
}

async function markRead(ids) {
  const { readIds = [] } = await chrome.storage.local.get('readIds');
  const set = new Set(readIds);
  ids.forEach(id => set.add(id));
  await chrome.storage.local.set({ readIds: [...set] });
}

async function toggleHidden(company) {
  const { hiddenCompanies = [] } = await chrome.storage.local.get('hiddenCompanies');
  const set = new Set(hiddenCompanies);
  if (set.has(company)) set.delete(company); else set.add(company);
  await chrome.storage.local.set({ hiddenCompanies: [...set] });
}

document.addEventListener('click', e => {
  if (view === 'open' && onOpenClick(e)) { render(); return; }
  const hide = e.target.closest('[data-hide]');
  if (hide) {
    toggleHidden(hide.dataset.hide);
    return;
  }
  if (e.target.closest('#toggleHidden')) {
    showHidden = !showHidden;
    render();
    return;
  }
  const post = e.target.closest('[data-post]');
  if (post) {
    const id = post.dataset.post;
    if (expanded.has(id)) expanded.delete(id);
    else { expanded.add(id); markRead([id]); }
    render();
    return;
  }
  const f = e.target.closest('[data-filter]');
  if (f) {
    const id = f.dataset.filter ? Number(f.dataset.filter) : null;
    if (view === 'jafs') { filterJob = id; location.hash = 'updates'; return; } // jump to that company's updates
    filterJob = filterJob === id ? null : id;
    render();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
});

$('q').addEventListener('input', render);
$('openSort').addEventListener('change', render);
$('sync').addEventListener('click', () => chrome.runtime.sendMessage({ type: 'sync' }));
$('loginBtn').addEventListener('click', () => chrome.runtime.sendMessage({ type: 'login' }));
$('unreadOnly').addEventListener('click', () => {
  unreadOnly = !unreadOnly;
  render();
});

chrome.storage.onChanged.addListener(load);
setInterval(render, 60_000); // keep "x min ago" fresh
load();
// Pick up posts you've read on your other devices.
const pullRead = () => chrome.runtime.sendMessage({ type: 'sync-read' }).catch(() => {});
pullRead();
document.addEventListener('visibilitychange', () => { if (!document.hidden) pullRead(); });
