import { ORIGIN, BLOG_URL, relevantPosts, shortName, formatDuration, withoutLinks } from './common.js';

// 2: post text keeps links as "label (url)"; a change re-reads the whole blog once.
const POST_FORMAT = 2;

// A JAF's selection rounds in order, with readable durations.
const roundsFrom = list => [...(list || [])].sort((x, y) => x.order - y.order)
  .map(p => ({ name: p.name, duration: formatDuration(p.duration) }));

const SYNC_MINUTES = 15;
const MAX_PAGES = 40;
const TEXT_LIMIT = 8000;
const DASHBOARD = chrome.runtime.getURL('dashboard.html');
const BLOG_HOME = `${ORIGIN}/blog/`;

class AuthError extends Error {}

// READ-ONLY: this is the only way the extension talks to the portal, and it can only send GET.
// It never signs, unsigns, applies, uploads or changes anything.
async function portalGet(url, { token, raw = false } = {}) {
  const res = await fetch(url, {
    method: 'GET',
    credentials: 'include',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (res.status === 401 || res.status === 403 || new URL(res.url).pathname.startsWith('/auth/')) throw new AuthError();
  if (raw) return res;
  if (!res.ok) throw new Error(`${new URL(url).pathname} → HTTP ${res.status}`);
  return res.json();
}

function decodeJwt(token) {
  try {
    return JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
  } catch {
    return null;
  }
}

// ---------- blog parsing (no DOMParser in service workers) ----------

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', ndash: '–', mdash: '—', rsquo: '’', lsquo: '‘', rdquo: '”', ldquo: '“', hellip: '…' };

function decode(s) {
  return s.replace(/&(#x[\da-f]+|#\d+|\w+);/gi, (m, e) => {
    if (e[0] === '#') return String.fromCodePoint(e[1].toLowerCase() === 'x' ? parseInt(e.slice(2), 16) : +e.slice(1));
    return ENTITIES[e.toLowerCase()] ?? m;
  });
}

// <a href="https://x">Click Here</a> -> "Click Here (https://x)", so the text keeps where links point.
function keepLink(m, q, href, inner) {
  const label = decode(inner.replace(/<[^>]+>/g, '')).replace(/\s+/g, ' ').trim();
  href = decode(href).trim();
  let url;
  try { url = new URL(href, BLOG_URL).href; } catch { return inner; }
  if (href.startsWith('#') || !/^https?:/.test(url)) return inner;
  url = url.replace(/\(/g, '%28').replace(/\)/g, '%29');
  return !label || label === href || label === url ? url : `${label} (${url})`;
}

function htmlToText(html, links = false) {
  if (links) html = html.replace(/<a\b[^>]*?\bhref\s*=\s*(["'])(.*?)\1[^>]*>([\s\S]*?)<\/a>/gi, keepLink);
  return decode(html
    .replace(/<(script|style)[\s\S]*?<\/\1>/gi, '')
    .replace(/<br\s*\/?>|<\/(p|li|tr|h\d|div|ol|ul|table)>/gi, '\n')
    .replace(/<\/t[dh]>/gi, '\t')
    .replace(/<[^>]+>/g, ''))
    .split('\n').map(l => l.replace(/[ \t ]+/g, ' ').trim()).join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function parseBlog(html, me) {
  const mention = me ? mentionTest(me) : () => false;
  return html.split(/<article\b/).slice(1).map(part => {
    const chunk = part.split(/<\/article>/)[0];
    const id = (chunk.match(/id="post-(\d+)"/) || [])[1];
    const date = (chunk.match(/<time[^>]*datetime="([^"]+)"/) || [])[1] || null;
    const t = chunk.match(/class="[^"]*entry-title[^"]*"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/);
    const body = (chunk.match(/class="[^"]*entry-content[^"]*"[^>]*>([\s\S]*)$/) || [])[1] || '';
    const text = htmlToText(body, true);
    return {
      id,
      date,
      link: t ? decode(t[1]) : BLOG_URL,
      title: t ? htmlToText(t[2]) : '(untitled)',
      text: text.length > TEXT_LIMIT ? `${text.slice(0, TEXT_LIMIT)}\n…` : text,
      mentionsMe: mention(text),
    };
  }).filter(p => p.id);
}

function mentionTest(me) {
  const res = [];
  if (me.roll) res.push(new RegExp(`\\b${me.roll}\\b`, 'i'));
  if (me.name && me.name.trim().includes(' ')) {
    res.push(new RegExp(me.name.trim().split(/\s+/).join('\\s+'), 'i'));
  }
  return text => res.some(re => re.test(text));
}

// ---------- sync ----------

let syncing = false;
let again = false;
let lastTokenSync = 0;

async function sync() {
  if (syncing) { again = true; return; }
  syncing = true;
  again = false;
  await chrome.storage.local.set({ syncing: true });
  try {
    await doSync();
  } catch (e) {
    if (e instanceof AuthError) await needLogin('Your placement portal session has ended.');
    else await chrome.storage.local.set({ error: String(e.message || e) });
  } finally {
    syncing = false;
    await chrome.storage.local.set({ syncing: false });
    await updateBadge();
    await pushSession().catch(() => {});
    await syncReadIds().catch(() => {});
    await syncHidden().catch(() => {});
  }
  // A sync was requested while this one ran (e.g. you just logged in): run once more.
  if (again) sync();
}

async function doSync() {
  const st = await chrome.storage.local.get(null);
  const jwt = decodeJwt(st.token || '');
  if (!jwt || jwt.exp * 1000 < Date.now()) return needLogin('Log in to the placement portal so the tracker can read your JAFs.');
  const token = st.token;

  let me = st.me;
  if (!me || me.applicantId !== jwt.id || !('programName' in me)) {
    const ap = await portalGet(`${ORIGIN}/api/v1/applicant/${jwt.id}`, { token });
    const s = ap.student || {};
    let programId = null;
    let programName = '';
    try {
      programId = (await portalGet(`${ORIGIN}/api/v1/academic-combination/${s.enrolledInId}`, { token })).programId;
      const progs = await portalGet(`${ORIGIN}/api/v1/program`, { token });
      const p = (Array.isArray(progs) ? progs : progs.data || []).find(x => x.id === programId);
      programName = p?.abbreviation || p?.shortName || p?.name || '';
    } catch (e) {
      if (e instanceof AuthError) throw e;
    }
    me = { applicantId: jwt.id, roll: s.roll || jwt.username, name: s.person?.name || '', programId, programName };
  }

  const apps = await portalGet(`${ORIGIN}/api/v1/job/application?applicantId=${jwt.id}`, { token });
  const jobs = {};
  for (const a of apps) {
    const d = await portalGet(`${ORIGIN}/api/v1/job/${a.jobId}`, { token });
    jobs[a.jobId] = {
      ...jobDetail(d, me.programId),
      id: a.jobId,
      company: d.company?.name || a.job?.company?.name || 'Unknown company',
      title: (d.title || '').trim(),
      code: d.code,
      opensAt: d.opensAt,
      closesAt: d.closesAt,
      signedAt: a.createdAt,
      rounds: roundsFrom(d.processes?.length ? d.processes : a.job?.processes),
    };
  }

  const { open: openJobs, details: jobDetails } = await loadOpenJobs(jwt, token, me, jobs, st.jobDetails || {});

  // Blog. A blog hiccup is not a logout: keep the old posts, retry next sync, warn only after an hour.
  const firstRun = !st.lastSync;
  const result = await readBlog(st, me, firstRun || st.postFormat !== POST_FORMAT); // re-read all pages when the format changed
  const blog = result.known ? result : null;
  const known = blog ? blog.known : new Map((st.posts || []).map(p => [p.id, p]));
  const fresh = blog ? blog.fresh : [];
  const blogFailingSince = blog ? null : st.blogFailingSince || Date.now();
  const blogWarn = !!blogFailingSince && Date.now() - blogFailingSince > BLOG_WARN_MS;
  // Your IITB SSO session ended: only you can fix that, so say so right away (once).
  if (result.ssoLoggedOut && !st.ssoAlerted) {
    chrome.notifications.create('sso', {
      type: 'basic', iconUrl: 'icons/icon128.png', priority: 2, title: 'IITB JAF Tracker: SSO login needed',
      message: 'Open the placement blog and log in with IITB SSO (Placement) so new posts can be read again.',
    });
  }
  if (blogWarn && !st.blogWarned) {
    chrome.notifications.create('blog', {
      type: 'basic', iconUrl: 'icons/icon128.png', priority: 1, title: 'IITB JAF Tracker: blog unreachable',
      message: "Couldn't read the placement blog for an hour. JAFs and reminders still update; new posts will appear once it's back.",
    });
  }

  const allPosts = [...known.values()].sort((a, b) => new Date(b.date) - new Date(a.date));
  const events = extractEvents(relevantPosts(allPosts, Object.values(jobs)), jobs);
  await chrome.storage.local.set({
    me, jobs, posts: allPosts, events, openJobs, jobDetails, lastSync: new Date().toISOString(), auth: { ok: true },
    ...(blog ? { postFormat: POST_FORMAT } : {}),
    blogFailingSince, blogWarned: blogWarn, blogError: blog ? null : result.error, ssoAlerted: !!result.ssoLoggedOut,
    error: blogWarn ? "Couldn't read the placement blog for over an hour (still retrying). JAFs and reminders are up to date." : null,
    seenOpenIds: [...new Set([...(st.seenOpenIds || []), ...openJobs.map(j => j.id)])],
  });
  chrome.notifications.clear('login');
  if (blog) chrome.notifications.clear('blog');
}

// ---------- events (tests, PPTs, interviews) read from blog posts, on this computer ----------

const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
const MON = 'jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec';
const DATE_DM = new RegExp(`(\\d{1,2})(?:st|nd|rd|th)?\\s+(?:of\\s+)?(${MON})[a-z]*\\.?,?\\s*(?:\\(\\w+\\)\\s*,?\\s*)?(\\d{4})?`, 'i');
const DATE_MD = new RegExp(`\\b(${MON})[a-z]*\\.?\\s+(\\d{1,2})(?:st|nd|rd|th)?\\b,?\\s*(\\d{4})?`, 'i');
const TIME = /(\d{1,2})(?:[:.](\d{2}))?\s*([ap])\.?\s*m\b/i;

const KINDS = [
  [/registration|mock/i, 'Registration / mock test due', '📝'],
  [/\btest\b|assessment|exam/i, 'Test', '📝'],
  [/pre-placement talk|\bppt\b/i, 'PPT', '🎤'],
  [/group discussion|\bgd\b/i, 'Group discussion', '🗣️'],
  [/\binterview\b(?!\s*shortlist)/i, 'Interview', '💼'],
];

function findDate(s, year) {
  let m = s.match(DATE_DM);
  if (m) return { d: +m[1], mo: MONTHS.indexOf(m[2].toLowerCase().slice(0, 3)), y: +(m[3] || year) };
  m = s.match(DATE_MD);
  if (m) return { d: +m[2], mo: MONTHS.indexOf(m[1].toLowerCase().slice(0, 3)), y: +(m[3] || year) };
  return null;
}

function findTime(s) {
  const m = s.match(TIME);
  if (!m) return null;
  return { h: (+m[1] % 12) + (m[3].toLowerCase() === 'p' ? 12 : 0), mi: +(m[2] || 0) };
}

// The event a post announces (kind, IST date/time, venue), or null. Uses the post's text only here.
function extractEvent(post, company) {
  const kind = KINDS.find(([re]) => re.test(post.title));
  if (!kind) return null;
  const text = withoutLinks(post.text);
  const seg = label => (text.match(new RegExp(`\\b${label}\\s*[:\\-–]\\s*([^\\n]{0,90})`, 'i')) || [])[1] || null;
  const year = new Date(post.date || Date.now()).getFullYear();
  const date = findDate(seg('date') || seg('deadline') || '', year) || findDate(text, year);
  const time = findTime(seg('updated time') || seg('time') || seg('deadline') || '') || findTime(text);
  if (!date || !time || date.mo < 0) return null;
  const at = new Date(Date.UTC(date.y, date.mo, date.d, time.h, time.mi) - 330 * 60000).toISOString();
  const venue = (seg('venue') || '').replace(/\s+/g, ' ').trim().slice(0, 60);
  return { id: post.id, company, kind: kind[1], icon: kind[2], at, venue, title: post.title, link: post.link };
}

// Newest post wins for each company + kind, so an "update" post replaces the earlier announcement.
function extractEvents(posts, jobs) {
  const byKey = new Map();
  for (const p of posts) {
    const e = extractEvent(p, companyOf(p, jobs));
    if (e && !byKey.has(`${e.company}|${e.kind}`)) byKey.set(`${e.company}|${e.kind}`, e);
  }
  return [...byKey.values()].sort((a, b) => new Date(a.at) - new Date(b.at));
}

function companyOf(post, jobs) {
  const j = jobs[post.jobIds?.[0]];
  return j ? shortName(j.company) : post.title.split('|')[0].trim();
}

// Fields shared by signed and open JAFs, from GET /api/v1/job/<id>. CTC is for your program.
function jobDetail(d, programId) {
  const mine = d.salaries?.find(s => s.programId === programId);
  const sal = mine || d.salaries?.[0];
  const clip = (html, n) => { const t = htmlToText(html || ''); return t.length > n ? `${t.slice(0, n)}…` : t; };
  return {
    currency: d.currency || 'INR',
    ctc: sal?.ctc ?? null,
    gross: sal?.amount ?? null,
    designation: sal?.designation?.trim() || '',
    forMyProgram: !!mine,
    locations: (d.locations || []).map(l => l.location.trim()),
    cpiCutoff: d.cpiCutoff,
    bond: d.bond || 0,
    requirements: clip(d.additionalRequirements, 1500),
    info: clip(d.additionalInfo, 2000),
    description: clip(d.jobDescription, 3000),
    rounds: roundsFrom(d.processes),
  };
}

const DETAIL_TTL = 60 * 60 * 1000;

// All JAFs open right now, with the portal's own eligibility flags. Read-only (GET).
async function loadOpenJobs(jwt, token, me, signed, cache) {
  const qs = new URLSearchParams({ sendEligibility: 'true', service: jwt.service, year: jwt.batch, season: jwt.season });
  const list = await portalGet(`${ORIGIN}/api/v1/job?${qs}`, { token });
  const now = Date.now();
  const details = {};
  const open = [];
  for (const j of list) {
    if (j.archivedOn || new Date(j.opensAt) > now || new Date(j.closesAt) <= now) continue;
    let d = cache[j.id];
    if (!d || !d.rounds || now - d.fetchedAt > DETAIL_TTL) {
      d = { ...jobDetail(await portalGet(`${ORIGIN}/api/v1/job/${j.id}`, { token }), me.programId), fetchedAt: now };
    }
    details[j.id] = d;
    open.push({
      ...d,
      id: j.id,
      company: j.company?.name || 'Unknown company',
      code: j.code,
      title: (j.title || '').trim(),
      opensAt: j.opensAt,
      closesAt: j.closesAt,
      eligible: !!j.eligible,
      bonus: !!j.allowBonusApplications,
      signed: !!signed[j.id],
    });
  }
  return { open, details };
}

// ---------- sending your login to your server (laptop) ----------
// After you log in normally, sends the portal token and the cookies for the portal and IITB SSO
// (nothing else) to your own server over Tailscale, so it can sync without a browser.

const SESSION_HOSTS = ['campus.placements.iitb.ac.in', 'sso.iitb.ac.in'];
const SESSION_RESEND_MS = 6 * 3600 * 1000;

async function sha256(text) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
}

async function pushSession(force = false) {
  const st = await chrome.storage.local.get(['serverSync', 'token', 'sessionPush']);
  const cfg = st.serverSync;
  if (!cfg?.enabled || !cfg.url || !cfg.key || !st.token) return { ok: false, msg: 'not set up' };
  const cookies = (await Promise.all(SESSION_HOSTS.map(domain => chrome.cookies.getAll({ domain })))).flat()
    .filter(c => SESSION_HOSTS.includes(c.domain.replace(/^\./, '')))
    .map(({ domain, name, value, path, secure, httpOnly, expirationDate }) => ({ domain, name, value, path, secure, httpOnly, expirationDate }));
  const hash = await sha256(st.token + cookies.map(c => `${c.domain}|${c.name}|${c.value}`).sort().join(';'));
  const last = st.sessionPush;
  if (!force && last?.ok && last.hash === hash && Date.now() - new Date(last.at) < SESSION_RESEND_MS) return last;
  let result;
  try {
    const res = await fetch(`${cfg.url.replace(/\/+$/, '')}/api/session`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-JAF-Key': cfg.key },
      body: JSON.stringify({ token: st.token, cookies, sentAt: new Date().toISOString() }),
    });
    result = res.status === 204 ? { ok: true } : { ok: false, msg: res.status === 403 ? 'wrong server key' : `HTTP ${res.status}` };
  } catch {
    result = { ok: false, msg: 'server not reachable (is Tailscale on?)' };
  }
  const status = { ...result, at: new Date().toISOString(), hash };
  await chrome.storage.local.set({ sessionPush: status });
  return status;
}

// Read/unread is shared with the website through the server: send ours, get back everyone's.
async function syncReadIds() {
  const { serverSync: cfg, readIds = [] } = await chrome.storage.local.get(['serverSync', 'readIds']);
  if (!cfg?.enabled || !cfg.url) return;
  const res = await fetch(`${cfg.url.replace(/\/+$/, '')}/api/read`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ add: readIds }),
  });
  if (!res.ok) return;
  const all = (await res.json()).readIds;
  if (all.length !== readIds.length) await chrome.storage.local.set({ readIds: all });
}

// Hidden companies are shared with the website and the server's messages the same way.
async function syncHidden(hide = [], unhide = []) {
  const { serverSync: cfg, hiddenCompanies = [] } = await chrome.storage.local.get(['serverSync', 'hiddenCompanies']);
  if (!cfg?.enabled || !cfg.url) return;
  const res = await fetch(`${cfg.url.replace(/\/+$/, '')}/api/hidden`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ hide, unhide }),
  });
  if (!res.ok) return;
  const server = (await res.json()).hidden;
  if ([...server].sort().join('\n') !== [...hiddenCompanies].sort().join('\n')) {
    await chrome.storage.local.set({ hiddenCompanies: server });
  }
}

// A fresh SSO login (e.g. you logged in to the blog) is sent straight away.
chrome.cookies.onChanged.addListener(({ cookie, removed }) => {
  if (!removed && cookie.domain.replace(/^\./, '') === 'sso.iitb.ac.in' && cookie.name === 'connect.sid') {
    pushSession().catch(() => {});
  }
});

// ---------- reading the blog ----------

const BLOG_WARN_MS = 60 * 60 * 1000; // how long the blog must keep failing before telling you

// Without a blog session the server answers 401 or silently redirects to the portal's front page.
const isBlogPage = r => r && r.status === 200 && new URL(r.url).pathname.startsWith('/blog/') && r.html.includes('<article');

async function swGet(url) {
  try {
    const r = await portalGet(url, { raw: true });
    return { status: r.status, url: r.url, html: await r.text() };
  } catch (e) {
    if (e instanceof AuthError) return { status: 401, url, html: '' };
    throw e;
  }
}

// GET from inside a blog tab, where the browser's own blog session always applies. Read-only.
async function tabGet(tabId, url) {
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: async u => {
      const r = await fetch(u, { method: 'GET', credentials: 'include' });
      return { status: r.status, url: r.url, html: await r.text() };
    },
    args: [url],
  });
  return res.result;
}

function tabLoaded(tabId, ms = 20000) {
  return new Promise(resolve => {
    const done = () => { chrome.tabs.onUpdated.removeListener(listener); clearTimeout(timer); resolve(); };
    const listener = (id, info) => { if (id === tabId && info.status === 'complete') done(); };
    const timer = setTimeout(done, ms);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// The blog home page, which carries the IITB SSO login widgets.
async function openBlogTab() {
  const tab = await chrome.tabs.create({ url: BLOG_HOME, active: false });
  await tabLoaded(tab.id);
  return tab.id;
}

// The blog session comes from the "Login as <you>" button in the Placement SSO widget on the blog home.
// If your IITB SSO session is still active, clicking it signs you back in to the blog; no password is
// entered. Returns 'clicked', or 'sso-logged-out' when the widget has no "Login as" button.
async function ssoBlogLogin(tabId) {
  const frames = await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    func: () => {
      if (location.hostname !== 'sso.iitb.ac.in' || !location.search.includes('sso-widget-placement')) return null;
      const link = [...document.querySelectorAll('a')].find(a => a.href.includes('/blog/placement/authplacement/'));
      if (!link) return 'sso-logged-out';
      link.click();
      return 'clicked';
    },
  });
  const outcome = frames.map(f => f.result).find(Boolean) || 'sso-logged-out';
  if (outcome === 'clicked') {
    await new Promise(r => setTimeout(r, 500)); // let the navigation start
    await tabLoaded(tabId);
  }
  return outcome;
}

const describe = (how, r) => `${how}: HTTP ${r?.status} at ${r?.url ? new URL(r.url).pathname : '?'}${r?.html?.includes('<article') ? '' : ', no posts'}`;

// Newest posts first, stopping once a page ends with a post we already have.
// Returns { known, fresh }, or { error } saying why the blog can't be read right now.
async function readBlog(st, me, firstRun) {
  let tabId = null;
  try {
    let get = swGet;
    const direct = await get(BLOG_URL);
    if (!isBlogPage(direct)) {
      tabId = await openBlogTab();
      get = url => tabGet(tabId, url);
      const sso = await ssoBlogLogin(tabId);
      if (sso === 'sso-logged-out') return { error: 'IITB SSO session ended', ssoLoggedOut: true };
      const viaTab = await get(BLOG_URL);
      if (!isBlogPage(viaTab)) return { error: `${describe('direct', direct)}; after SSO ${describe('tab', viaTab)}` };
    }
    const known = new Map((st.posts || []).map(p => [p.id, p]));
    const fresh = [];
    for (let page = 1; page <= MAX_PAGES; page++) {
      const res = await get(page === 1 ? BLOG_URL : `${BLOG_URL}?paged=${page}`);
      if (res.status === 404) break;
      if (!isBlogPage(res)) {
        if (page === 1) return { error: describe('page 1', res) };
        break;
      }
      const posts = parseBlog(res.html, me);
      for (const p of posts) {
        if (!known.has(p.id)) fresh.push(p);
        known.set(p.id, p); // refresh edited posts too
      }
      if (!firstRun && st.posts?.some(p => p.id === posts.at(-1).id)) break;
    }
    return { known, fresh };
  } catch (e) {
    return { error: `exception: ${e?.message || e}` };
  } finally {
    if (tabId != null) chrome.tabs.remove(tabId).catch(() => {});
  }
}

async function needLogin(msg, url = `${ORIGIN}/applicant/home`) {
  const { auth } = await chrome.storage.local.get('auth');
  await chrome.storage.local.set({ auth: { ok: false, msg, url } });
  if (auth?.ok !== false) {
    chrome.notifications.create('login', {
      type: 'basic', iconUrl: 'icons/icon128.png', priority: 2,
      title: 'IITB JAF Tracker: please log in',
      message: `${msg} Click here to open the portal.`,
    });
  }
}

async function updateBadge() {
  const st = await chrome.storage.local.get(['posts', 'jobs', 'readIds', 'auth', 'hiddenCompanies']);
  if (st.auth && !st.auth.ok) {
    chrome.action.setBadgeBackgroundColor({ color: '#d93025' });
    chrome.action.setBadgeText({ text: '!' });
    chrome.action.setTitle({ title: 'IITB JAF Tracker: log in needed' });
    return;
  }
  const read = new Set(st.readIds || []);
  const hidden = new Set(st.hiddenCompanies || []);
  const jobs = Object.values(st.jobs || {}).filter(j => !hidden.has(j.company));
  const unread = relevantPosts(st.posts || [], jobs).filter(p => !read.has(p.id)).length;
  chrome.action.setBadgeBackgroundColor({ color: '#4f46e5' });
  chrome.action.setBadgeText({ text: unread ? String(unread) : '' });
  chrome.action.setTitle({ title: `IITB JAF Tracker: ${unread} unread update${unread === 1 ? '' : 's'}` });
}

async function openDashboard() {
  const [tab] = await chrome.tabs.query({ url: DASHBOARD });
  if (tab) {
    await chrome.tabs.update(tab.id, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
  } else {
    await chrome.tabs.create({ url: DASHBOARD });
  }
}

async function openPortal() {
  const { auth } = await chrome.storage.local.get('auth');
  chrome.tabs.create({ url: auth?.url || `${ORIGIN}/applicant/home` });
}

// ---------- wiring ----------

// Next sync in 15–30 minutes, at random (not on a fixed beat).
function schedule() {
  chrome.alarms.create('sync', { delayInMinutes: SYNC_MINUTES + Math.random() * SYNC_MINUTES });
}

chrome.runtime.onInstalled.addListener(() => { schedule(); sync(); openDashboard(); });
chrome.runtime.onStartup.addListener(() => { schedule(); sync(); });
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'sync') { schedule(); sync(); } });
chrome.action.onClicked.addListener(openDashboard);
chrome.notifications.onClicked.addListener(id => {
  chrome.notifications.clear(id);
  if (id === 'login') openPortal();
  else openDashboard();
});
chrome.storage.onChanged.addListener(changes => {
  if (changes.readIds || changes.auth || changes.hiddenCompanies) updateBadge();
  if (changes.readIds) syncReadIds().catch(() => {});
  if (changes.hiddenCompanies) {
    const before = new Set(changes.hiddenCompanies.oldValue || []);
    const after = new Set(changes.hiddenCompanies.newValue || []);
    const hide = [...after].filter(c => !before.has(c));
    const unhide = [...before].filter(c => !after.has(c));
    if (hide.length || unhide.length) syncHidden(hide, unhide).catch(() => {});
  }
});

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type === 'token') {
    chrome.storage.local.get(['token', 'auth']).then(async st => {
      if (st.token !== msg.token) {
        await chrome.storage.local.set({ token: msg.token });
        pushSession().catch(() => {}); // new login: hand it to the server right away
      }
      // New login, or you opened the portal while the tracker was waiting for a login
      // (at most once every 2 minutes, since portal tabs report the token every few seconds).
      const waiting = st.auth?.ok === false && Date.now() - lastTokenSync > 120000;
      if (st.token !== msg.token || waiting) {
        lastTokenSync = Date.now();
        sync();
      }
    });
  } else if (msg.type === 'sync') {
    sync().then(() => reply(true));
    return true;
  } else if (msg.type === 'login') {
    openPortal();
  } else if (msg.type === 'sync-read') {
    Promise.all([syncReadIds(), syncHidden()]).then(() => reply(true), () => reply(false));
    return true;
  } else if (msg.type === 'push-session') {
    pushSession(true).then(reply, e => reply({ ok: false, msg: String(e.message || e) }));
    return true;
  }
});
