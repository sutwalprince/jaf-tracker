import { postsWithPpts, PPT_TITLE, money, fmtDate, ago, esc, shortName, when, until, roundsHtml, roundsDone, postKind, cleanTitle, dayLabel, timeOf, linkify, stat } from './common.js';
import { renderOpen, onOpenClick } from './jafs.js';

const $ = id => document.getElementById(id);

let state = {};
let filterJob = null;
let showHidden = false;
let unreadOnly = false;
let snapshotting = false; // true while drawing a neighbouring tab for the swipe preview
// Three views on this page: "updates" (the blog feed, default), "jafs" (your signed JAFs), "open" (open JAFs).
const currentView = () => ({ '#jafs': 'jafs', '#open': 'open' })[location.hash] || 'updates';
let view = currentView();
// Tabs in bottom-bar order, for swiping and for which side a new tab slides in from.
const ORDER = ['jafs', 'updates', 'open'];
window.addEventListener('hashchange', () => {
  const prev = view;
  view = currentView();
  render();
  window.scrollTo({ top: 0 });
  const pane = document.getElementById('views');
  if (!pane || prev === view) return;
  const forward = ORDER.indexOf(view) > ORDER.indexOf(prev);
  if (swipeEnter) { swipeEnter(forward); return; }
  pane.removeAttribute('data-enter');
  void pane.offsetWidth; // restart the slide-in animation
  pane.dataset.enter = forward ? 'right' : 'left';
});
let swipeEnter = null; // set by the swipe code while a swipe is switching tabs
const expanded = new Set();

let formFilled = false;

async function load() {
  state = await chrome.storage.local.get(null);
  invalidateSnaps();
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
  if (showHidden && !(state.hiddenCompanies || []).length) showHidden = false; // nothing hidden any more
  const { all, hidden, jobs } = visibleJobs();
  const hiddenShort = new Set([...hidden].map(shortName));
  const byId = Object.fromEntries(all.map(j => [j.id, j]));
  const read = new Set(state.readIds || []);
  // Your companies' posts, plus every company's PPTs (they come before the JAF opens).
  const posts = postsWithPpts(state.posts || [], jobs, all, hiddenShort);
  const q = $('q').value.trim().toLowerCase();

  // View
  document.body.dataset.view = view;
  document.querySelectorAll('.tabs [data-view]').forEach(a => a.classList.toggle('on', a.dataset.view === view));
  $('pageTitle').textContent = { jafs: 'My JAFs', open: 'Open JAFs' }[view] || 'Updates';
  $('q').placeholder = view !== 'updates' ? 'Search company, role or location…' : 'Search companies or updates…';

  // Header
  const loggedOut = state.auth && !state.auth.ok;
  const synced = state.lastSync ? `Synced ${ago(state.lastSync)}` : 'Not synced yet';
  const offlineNow = state.offline && !state.syncing;
  $('status').textContent = state.syncing ? 'Syncing…' : offlineNow ? `Offline · ${synced.toLowerCase()}` : loggedOut ? 'Logged out' : synced;
  $('status').classList.toggle('bad', !!loggedOut && !offlineNow);
  $('status').classList.toggle('off', !!offlineNow);
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
  $('stats').innerHTML = [
    stat(['JAFs signed', 'Signed'], all.length, `${new Set(all.map(j => j.company)).size} companies${hidden.size ? `, ${hidden.size} hidden` : ''}`),
    stat(['Unread updates', 'Unread'], unread, `${posts.length} total`),
    stat(['Mentions you', 'Mentions'], mentions, 'shortlists & lists with your roll/name', mentions > 0),
  ].join('');

  // Upcoming tests / PPTs / interviews (read from blog posts)
  const ups = (state.events || [])
    .filter(e => new Date(e.at) > now - 2 * 3600000 && (showHidden || !hiddenShort.has(e.company))).slice(0, 8);
  $('upcoming').hidden = !ups.length;
  if (view === 'updates') $('upcoming').innerHTML = `<h2>Upcoming</h2><div class="uplist">${ups.map(e => `
    <a class="up" href="${esc(e.link)}" target="_blank" rel="noopener">
      <span class="upicon">${e.icon}</span>
      <span><b>${esc(e.company)}</b> · ${esc(e.kind)}<br><span class="muted">${esc(when(e.at))}${placeHtml(e)}</span></span>
      <span class="upleft">${new Date(e.at) > now ? esc(until(e.at)) : 'now'}</span>
    </a>`).join('')}</div>`;

  // Company chips
  const companies = [...new Map(jobs.map(j => [j.company, j.id])).entries()];
  const pptCount = posts.filter(p => PPT_TITLE.test(p.title)).length;
  $('chips').innerHTML = [`<button class="chip ${filterJob ? '' : 'on'}" data-filter="">All</button>`,
    pptCount ? `<button class="chip ${filterJob === 'ppt' ? 'on' : ''}" data-filter="ppt">🎤 PPTs (${pptCount})</button>` : '',
    ...companies.map(([c, id]) => {
      const ids = jobs.filter(j => j.company === c).map(j => j.id);
      const on = filterJob && ids.includes(filterJob);
      return `<button class="chip ${on ? 'on' : ''}" data-filter="${id}">${esc(shortName(c))}</button>`;
    })].join('');

  // Feed
  const selected = filterJob && filterJob !== 'ppt' ? jobs.filter(j => j.company === byId[filterJob]?.company).map(j => j.id) : null;
  // Unread-only keeps a post you've just opened on screen until you close it.
  const shown = posts.filter(p =>
    (!unreadOnly || !read.has(p.id) || expanded.has(p.id)) &&
    (!selected || p.jobIds.some(id => selected.includes(id))) &&
    (filterJob !== 'ppt' || PPT_TITLE.test(p.title)) &&
    (!q || `${p.title} ${p.text}`.toLowerCase().includes(q)));
  $('feedCount').textContent = `(${shown.length})`;
  let lastDay = null; // a heading before each day's posts
  if (view === 'updates') $('feed').innerHTML = shown.length ? shown.map(p => {
    const day = dayLabel(p.date);
    const head = day !== lastDay ? `<h3 class="dayhead">${esc(day)}</h3>` : '';
    lastDay = day;
    return head + postHtml(p, byId, read);
  }).join('')
    : `<div class="empty">${!state.lastSync ? 'Waiting for the first sync…' : unreadOnly ? 'No unread updates.' : 'No updates match.'}</div>`;
  $('unreadOnly').textContent = unreadOnly ? 'Show all updates' : `Show unread updates (${unread})`;
  $('tabUnread').hidden = !unread;
  $('tabUnread').textContent = unread;
  // Open JAFs you can still sign (eligible or bonus allowed), shown on the Open JAFs tab.
  const signable = (state.openJobs || []).filter(j => !j.signed && (j.eligible || j.bonus) && new Date(j.closesAt) > now).length;
  $('tabOpen').hidden = !signable;
  $('tabOpen').textContent = signable;
  $('tabOpen').title = `${signable} open JAF${signable === 1 ? '' : 's'} you can sign`;
  $('unreadOnly').setAttribute('aria-pressed', String(unreadOnly));
  $('unreadOnly').classList.toggle('on', unreadOnly);

  // Jobs: ones with a test/PPT coming up first (soonest first), then JAFs still open, then the rest by latest update.
  const nextEvent = j => (state.events || []).filter(e => e.company === shortName(j.company) && new Date(e.at) > now - 2 * 3600000)
    .sort((a, b) => new Date(a.at) - new Date(b.at))[0];
  const rank = x => x.next ? [0, new Date(x.next.at)] : new Date(x.j.closesAt) > now ? [1, new Date(x.j.closesAt)]
    : [2, -new Date(x.ups[0]?.date || x.j.signedAt)];
  // "Show hidden" lists only the hidden companies' JAFs; normally only the others.
  const jobsShown = view !== 'jafs' ? [] : jobs
    .filter(j => !showHidden || hidden.has(j.company))
    .filter(j => !q || `${j.company} ${j.title} ${j.locations.join(' ')}`.toLowerCase().includes(q))
    .map(j => ({ j, ups: posts.filter(p => p.jobIds.includes(j.id)), next: nextEvent(j) }))
    .sort((a, b) => { const [ra, ka] = rank(a), [rb, kb] = rank(b); return ra - rb || ka - kb; });
  if (view === 'jafs') $('jobs').innerHTML = jobsShown.length ? jobsShown.map(({ j, ups, next }) => jobHtml(j, ups, selected, hidden.has(j.company), next)).join('')
    : `<div class="empty">${!state.lastSync ? 'Waiting for the first sync…' : showHidden ? 'No hidden JAFs match.' : 'No JAFs match.'}</div>`;
  $('jobsTitle').textContent = showHidden ? `Hidden JAFs (${jobsShown.length})` : 'My signed JAFs';
  $('toggleHidden').hidden = !hidden.size;
  $('toggleHidden').textContent = showHidden ? '← Back to my JAFs' : `Show hidden (${hidden.size})`;

  if (view === 'open') renderOpen(state, q); // its own stats and cards
  if (!snapshotting) scheduleSnapshots();
}

// " · LH 301 · 💺 Your room: LH 101" for a test (your room comes from the seating sheet, on the website).
function placeHtml(e) {
  const tba = /announc|tba|tbd/i.test(e.venue || '');
  return (e.venue && !(e.seat && tba) ? ` · ${esc(e.venue)}` : '') + (e.seat ? ` · 💺 Your room: <b>${esc(e.seat)}</b>` : '');
}



function postHtml(p, byId, read) {
  const cos = p.ppt ? [p.ppt] : [...new Set(p.jobIds.map(id => shortName(byId[id]?.company || '')))].filter(Boolean);
  const open = expanded.has(p.id);
  // Posts asking you to do something (fill a form, register…) are marked by the server.
  const [kind, kcls] = state.postActions && p.id in state.postActions ? ['Action', 'k-red'] : postKind(p.title);
  // Tests, PPTs and interviews that are over (3 h after they start) are greyed out.
  const at = (state.postEvents || {})[p.id];
  const done = ['Test', 'PPT', 'Interview'].includes(kind) && at && Date.now() > new Date(at).getTime() + 3 * 3600000;
  const seat = (state.postSeats || {})[p.id];
  const fullNames = p.ppt ? [p.ppt] : [...new Set(p.jobIds.map(id => byId[id]?.company).filter(Boolean))];
  return `<article class="post ${read.has(p.id) ? '' : 'unread'} ${p.mentionsMe ? 'mention' : ''} ${done ? 'done' : ''}">
    <button class="head" data-post="${esc(p.id)}" aria-expanded="${open}">
      <span class="dot"></span>
      <span class="pmain">
        <span class="meta"><span class="kind ${done ? 'k-done' : kcls}">${esc(kind)}${done ? ' · ✓ Done' : ''}</span>${seat ? `<span class="seat">💺 ${esc(seat)}</span>` : ''}${cos.length ? `<span class="co">${esc(cos.join(', '))}</span>` : ''}<span class="time">${esc(timeOf(p.date))}</span></span>
        <span class="title">${esc(cleanTitle(p.title, fullNames))}</span>
      </span>
      ${p.mentionsMe ? '<span class="badge">Mentions you</span>' : ''}
    </button>
    ${open ? `<div class="body"><pre>${linkify(p.text)}</pre><a href="${esc(p.link)}" target="_blank" rel="noopener">Open on blog ↗</a></div>` : ''}
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
    ${next ? `<a class="nextup" href="${esc(next.link)}" target="_blank" rel="noopener"><span>${next.icon}</span><span><b>${esc(next.kind)}</b> ${esc(when(next.at))}${next.seat ? ` · 💺 <b>${esc(next.seat)}</b>` : ''}</span><span class="upleft">${new Date(next.at) > Date.now() ? esc(until(next.at)) : 'now'}</span></a>` : ''}
    <dl>
      <div><dt>Location</dt><dd>${esc(j.locations.join(', ') || '—')}</dd></div>
      <div><dt>CPI cutoff</dt><dd>${j.cpiCutoff ? esc(j.cpiCutoff) : 'None'}</dd></div>
      <div><dt>Signed</dt><dd>${esc(fmtDate(j.signedAt))}</dd></div>
      <div><dt>JAF closes</dt><dd class="${closed ? 'closed' : 'open'}">${esc(fmtDate(j.closesAt))}${closed ? ' (closed)' : ''}</dd></div>
    </dl>
    ${roundsHtml(j, roundsDone(j, ups, state.postEvents || {}))}
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
  if (!e.target.closest('.tabs')) invalidateSnaps(); // a filter, chip, post or button may change what a tab shows
  const tab = e.target.closest('.tabs [data-view]');
  if (tab && tab.dataset.view === view) { e.preventDefault(); window.scrollTo({ top: 0, behavior: 'smooth' }); return; }
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
    const id = f.dataset.filter === 'ppt' ? 'ppt' : f.dataset.filter ? Number(f.dataset.filter) : null;
    if (view === 'jafs') { filterJob = id; location.hash = 'updates'; return; } // jump to that company's updates
    filterJob = filterJob === id ? null : id;
    render();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
});

$('q').addEventListener('input', () => { invalidateSnaps(); render(); });
$('openSort').addEventListener('change', () => { invalidateSnaps(); render(); });
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

// ---------- Swipe between tabs, like swiping photos ----------
// Finger right = the tab on the left (My JAFs ‹ Updates › Open JAFs). While you drag, the neighbouring tab is
// visible beside the current one (a snapshot drawn when the page is idle); let go past a quarter of the screen,
// or flick, and both slide together; then the real tab replaces the identical snapshot. Vertical scrolls, the
// sideways-scrolling chips, form fields and the screen edges (Android's back gesture) are left alone.

// What each tab hides (mirrors the body[data-view] rules in dashboard.css).
const HIDDEN_IN = {
  jafs: ['feedSection', 'upcoming', 'chips', 'unreadOnly', 'openBar', 'openList'],
  updates: ['jobsSection', 'openBar', 'openList'],
  open: ['upcoming', 'chips', 'unreadOnly'],
};
const snaps = {};
let snapTimer = 0, swipeBusy = false;
function invalidateSnaps() { for (const k of Object.keys(snaps)) delete snaps[k]; }

// A copy of #views as tab `v` looks: what that tab hides is dropped, ids removed (they must stay unique).
function prepare(copy, v) {
  HIDDEN_IN[v].forEach(id => copy.querySelector(`#${id}`)?.remove());
  if (v === 'open') copy.querySelector('main.grid')?.remove();
  copy.querySelectorAll('[id]').forEach(n => n.removeAttribute('id'));
  copy.removeAttribute('id');
  copy.removeAttribute('data-enter');
  copy.style.cssText = '';
  return copy;
}
// The tab on screen right now: just a copy, no drawing.
const captureCurrent = () => prepare($('views').cloneNode(true), view);
// Draws tab `v` into the page, copies it, and puts the current tab back, all in one go (nothing is painted between).
function snapshot(v) {
  const keep = view, y = scrollY;
  snapshotting = true;
  try {
    view = v;
    render();
    const copy = $('views').cloneNode(true);
    view = keep;
    render();
    if (scrollY !== y) scrollTo(0, y);
    return prepare(copy, v);
  } finally {
    snapshotting = false;
  }
}
function neighbours() {
  const i = ORDER.indexOf(view);
  return [ORDER[i - 1], ORDER[i + 1]].filter(Boolean);
}
// Prepares missing neighbour previews when the phone is idle, one per idle moment, never during a swipe.
function scheduleSnapshots() {
  if (!matchMedia('(pointer: coarse)').matches) return; // only touch screens swipe
  clearTimeout(snapTimer);
  snapTimer = setTimeout(function build() {
    const v = neighbours().find(n => !snaps[n]);
    if (!v) return;
    const idle = window.requestIdleCallback || (f => setTimeout(f, 50));
    idle(() => {
      if (swipeBusy || document.hidden) { snapTimer = setTimeout(build, 500); return; }
      if (!snaps[v]) snaps[v] = snapshot(v);
      snapTimer = setTimeout(build, 60);
    });
  }, 300);
}

(() => {
  const pane = $('views');
  const EASE = 'cubic-bezier(.22,.8,.3,1)';
  // The neighbour preview: a fixed layer beside the page; only its inner sheet moves.
  const peek = document.createElement('div');
  peek.id = 'peek';
  peek.setAttribute('aria-hidden', 'true');
  const sheet = document.createElement('div');
  sheet.className = 'peek-sheet';
  peek.append(sheet);
  document.body.append(peek);

  let sw = null, frame = 0, shown = null;
  const move = (el, x, ms = 0) => {
    el.style.transition = ms ? `transform ${ms}ms ${EASE}` : 'none';
    el.style.transform = x ? `translate3d(${x}px,0,0)` : '';
  };
  const showPeek = v => {
    if (shown === v) return;
    shown = v;
    sheet.replaceChildren();
    if (!v) { peek.style.display = 'none'; return; }
    const copy = snaps[v] || (snaps[v] = snapshot(v));
    const r = pane.getBoundingClientRect();
    // The preview starts where the content starts (below the header, or at the top once scrolled past it).
    peek.style.top = `${Math.max(0, r.top)}px`;
    copy.style.cssText = `position:absolute;left:${r.left}px;width:${r.width}px;top:0;margin:0`;
    sheet.append(copy.cloneNode(true)); // keep the cached one untouched
    peek.style.display = 'block';
  };
  const hidePeek = () => { showPeek(null); move(sheet, 0); };
  const draw = () => {
    frame = 0;
    if (!sw) return;
    const w = sw.w, i = ORDER.indexOf(view);
    const v = sw.dx < 0 ? ORDER[i + 1] : sw.dx > 0 ? ORDER[i - 1] : null;
    showPeek(v);
    move(pane, sw.dx);
    if (v) move(sheet, sw.dx + (sw.dx < 0 ? w : -w));
  };

  pane.addEventListener('animationend', () => pane.removeAttribute('data-enter'));
  pane.addEventListener('touchstart', e => {
    const t = e.touches[0];
    sw = swipeBusy || e.touches.length > 1 || t.clientX < 24 || t.clientX > innerWidth - 24
      || e.target.closest('.chips, input, select, textarea') ? null
      : { x: t.clientX, y: t.clientY, dx: 0, dir: 0, lastX: t.clientX, lastT: e.timeStamp, v: 0, w: document.documentElement.clientWidth };
  }, { passive: true });
  pane.addEventListener('touchmove', e => {
    if (!sw) return;
    const t = e.touches[0], dx = t.clientX - sw.x, dy = t.clientY - sw.y;
    if (!sw.dir && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) {
      sw.dir = Math.abs(dx) > Math.abs(dy) * 1.2 ? 1 : -1;
      if (sw.dir === 1) { sw.x = t.clientX; pane.style.willChange = sheet.style.willChange = 'transform'; } // no jump
    }
    if (sw.dir !== 1) return;
    const dt = e.timeStamp - sw.lastT;
    if (dt > 0) sw.v = 0.7 * ((t.clientX - sw.lastX) / dt) + 0.3 * sw.v; // px/ms, smoothed
    sw.lastX = t.clientX; sw.lastT = e.timeStamp;
    const d = t.clientX - sw.x, i = ORDER.indexOf(view);
    const edge = (d > 0 && i === 0) || (d < 0 && i === ORDER.length - 1);
    sw.dx = edge ? d / 4 : d; // rubber band at the ends
    if (!frame) frame = requestAnimationFrame(draw);
  }, { passive: true });

  const finish = () => {
    const s = sw;
    sw = null;
    if (frame) { cancelAnimationFrame(frame); frame = 0; }
    if (!s || s.dir !== 1) return;
    const w = s.w, flick = Math.abs(s.v) > 0.3 && Math.sign(s.v) === Math.sign(s.dx);
    const next = ORDER[ORDER.indexOf(view) + (s.dx < 0 ? 1 : -1)];
    const cleanup = () => { pane.style.willChange = sheet.style.willChange = ''; pane.style.transition = ''; };
    if (!next || !(Math.abs(s.dx) > w / 4 || (flick && Math.abs(s.dx) > 20))) {
      move(pane, 0, 220); // snap back, the preview slides away with it
      if (shown) move(sheet, s.dx < 0 ? w : -w, 220);
      setTimeout(() => { hidePeek(); cleanup(); }, 230);
      return;
    }
    swipeBusy = true;
    setTimeout(() => { if (swipeBusy) { swipeBusy = false; swipeEnter = null; move(pane, 0); hidePeek(); cleanup(); } }, 1500); // never stay stuck
    snaps[view] = captureCurrent(); // the tab we're leaving is the preview for swiping back
    if (shown !== next) { showPeek(next); move(sheet, s.dx + (s.dx < 0 ? w : -w)); }
    const ms = Math.max(140, Math.min(260, (w - Math.abs(s.dx)) / Math.max(Math.abs(s.v), 1.1)));
    move(pane, s.dx < 0 ? -w : w, ms); // both slide together: current off, neighbour in
    move(sheet, 0, ms);
    setTimeout(() => {
      swipeEnter = () => {
        swipeEnter = null;
        move(pane, 0); // the real tab, drawn at the top, takes the snapshot's place
        requestAnimationFrame(() => { hidePeek(); cleanup(); swipeBusy = false; });
      };
      location.hash = next;
    }, ms);
  };
  pane.addEventListener('touchend', finish, { passive: true });
  pane.addEventListener('touchcancel', finish, { passive: true });
})();
