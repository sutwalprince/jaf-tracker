export const ORIGIN = 'https://campus.placements.iitb.ac.in';
export const BLOG_URL = `${ORIGIN}/blog/placement/`;

// Legal suffixes dropped from the end of a company name before matching.
const LEGAL = new Set(['pvt', 'private', 'ltd', 'limited', 'llp', 'inc', 'india', 'co', 'corp', 'corporation', 'company']);
// Words too common to identify a company on their own.
const GENERIC = new Set(['research', 'technologies', 'technology', 'solutions', 'services', 'systems', 'global', 'capital',
  'software', 'group', 'labs', 'india', 'indian', 'american', 'national', 'international', 'digital', 'financial', 'bank']);

const escapeRe = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const patternCache = new Map();

// "QuantBox Research Pvt Ltd [QB TRADING LLP]" -> /quantbox research/, /quantbox/
export function companyPatterns(name) {
  if (patternCache.has(name)) return patternCache.get(name);
  const words = name.replace(/\[.*?\]|\(.*?\)/g, ' ').replace(/[.,]/g, ' ').toLowerCase().split(/\s+/).filter(Boolean);
  while (words.length > 1 && LEGAL.has(words.at(-1))) words.pop();
  const phrases = new Set([words.join(' ')]);
  if (words[0]?.length >= 5 && !GENERIC.has(words[0])) phrases.add(words[0]);
  const res = [...phrases].map(p => new RegExp(`\\b${escapeRe(p).replace(/ /g, '\\s+')}\\b`, 'i'));
  patternCache.set(name, res);
  return res;
}

// Post text keeps where links point as "Click Here (https://…)"; these helpers find or drop those parts.
const LINK_IN_TEXT = / ?\((https?:\/\/[^\s()]+)\)/g;
export const withoutLinks = text => String(text ?? '').replace(LINK_IN_TEXT, '');

// Post text as HTML: escaped, with "label (https://…)" shown as "label [docs.google.com ↗]" and bare URLs clickable.
export function linkify(text) {
  const s = String(text ?? '');
  const re = / ?\((https?:\/\/[^\s()]+)\)|(https?:\/\/[^\s()<>"']*[^\s()<>"'.,;:!?])/g;
  let out = '', last = 0;
  for (const m of s.matchAll(re)) {
    out += esc(s.slice(last, m.index));
    const url = m[1] || m[2];
    let host = url;
    try { host = new URL(url).hostname.replace(/^www\./, ''); } catch { /* keep the url */ }
    out += m[1]
      ? ` <a class="plink" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(host)} ↗</a>`
      : `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a>`;
    last = m.index + m[0].length;
  }
  return out + esc(s.slice(last));
}

// Which of your jobs a blog post is about (by company name in its title / opening lines).
export function matchJobs(post, jobs) {
  const head = `${post.title} ${withoutLinks(post.text).slice(0, 300)}`;
  return jobs.filter(j => companyPatterns(j.company).some(re => re.test(head))).map(j => j.id);
}

// Posts worth showing: about a company you applied to, or naming you (e.g. a shortlist).
export function relevantPosts(posts, jobs) {
  return posts
    .map(p => ({ ...p, jobIds: matchJobs(p, jobs) }))
    .filter(p => p.jobIds.length || p.mentionsMe);
}

export function money(value, currency = 'INR') {
  if (value == null) return '—';
  if (currency === 'INR') {
    if (value >= 1e5) return `₹${(value / 1e5).toFixed(2).replace(/\.?0+$/, '')} L`;
    return `₹${value.toLocaleString('en-IN')}`;
  }
  return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 0 }).format(value);
}

export function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit',
  });
}

// Round durations come as free text in many formats: '["01:30"]', '["90"]', '["3"]', '["45 min","45 min"]'…
const fmtMins = m => (m < 60 ? `${m} min` : m % 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m / 60} h`);

function oneDuration(raw) {
  const s = String(raw).trim();
  let m;
  if ((m = s.match(/^(\d{1,2}):(\d{2})$/))) return fmtMins(+m[1] * 60 + +m[2]);
  if ((m = s.match(/^(\d+(?:\.\d+)?)$/))) return +m[1] <= 5 ? fmtMins(Math.round(+m[1] * 60)) : fmtMins(+m[1]); // bare: hours if small
  if ((m = s.match(/^(\d+)\s*(?:m|min|mins|minutes?)\.?$/i))) return fmtMins(+m[1]);
  if ((m = s.match(/^(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hours?)\.?$/i))) return fmtMins(Math.round(+m[1] * 60));
  return s;
}

export function formatDuration(raw) {
  if (raw == null || raw === '') return '';
  let list;
  try { list = JSON.parse(raw); } catch { list = [raw]; }
  list = (Array.isArray(list) ? list : [list]).filter(x => x != null && String(x).trim()).map(oneDuration);
  if (!list.length) return '';
  return list.length > 1 && list.every(x => x === list[0]) ? `${list.length} × ${list[0]}` : list.join(' + ');
}

// Selection rounds in order, as { name, duration } (older data had plain names).
export const roundsOf = j => (j.rounds || []).map(r => (typeof r === 'string' ? { name: r, duration: '' } : r));

// The numbered round chips on a JAF card, with durations when the company gave them.
export function roundsHtml(j) {
  const rounds = roundsOf(j);
  if (!rounds.length) return '';
  return `<div class="rounds">${rounds.map((r, i) => `<span>${i + 1}. ${esc(r.name)}${
    r.duration ? ` <b class="dur">· ${esc(r.duration)}</b>` : ''}</span>`).join('')}</div>`;
}

export const shortName = c =>c.replace(/\[.*?\]/g, '').replace(/\b(pvt|private|ltd|limited|llp|inc)\b\.?/gi, '').replace(/\s+/g, ' ').trim();

// "today at 9:00 pm", "tomorrow at 11:59 pm", "on Sat, 1 Nov at 5:00 pm" (IST)
export function when(iso) {
  const d = new Date(iso);
  const day = x => new Date(x).toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
  const time = d.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: 'numeric', minute: '2-digit' });
  if (day(d) === day(Date.now())) return `today at ${time}`;
  if (day(d) === day(Date.now() + 86400000)) return `tomorrow at ${time}`;
  return `on ${d.toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', weekday: 'short', day: 'numeric', month: 'short' })} at ${time}`;
}

export const esc =s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export function until(iso) {
  const s = Math.round((new Date(iso) - Date.now()) / 1000);
  if (s <= 0) return 'closed';
  if (s < 3600) return `in ${Math.ceil(s / 60)} min`;
  if (s < 86400) return `in ${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  return `in ${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`;
}

export function ago(iso) {
  if (!iso) return 'never';
  const s = Math.round((Date.now() - new Date(iso)) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

// What a blog post is about, from its title: [label, colour class].
export function postKind(title) {
  const t = String(title).toLowerCase();
  if (/shortlist|result|selected|final list/.test(t)) return ['Shortlist', 'k-good'];
  if (/deadline/.test(t)) return ['Deadline', 'k-red'];
  if (/pre-placement talk|\bppt\b/.test(t)) return ['PPT', 'k-blue'];
  if (/interview|group discussion|\bgd\b/.test(t)) return ['Interview', 'k-violet'];
  if (/test|assessment|exam|registration/.test(t)) return ['Test', 'k-amber'];
  if (/jafs? open/.test(t)) return ['JAF open', 'k-green'];
  if (/jaf/.test(t)) return ['JAF update', 'k-gray'];
  return ['Update', 'k-gray'];
}

// "Bajaj Auto Ltd. | Test Announcement" -> "Test Announcement" (the company is shown separately).
export function cleanTitle(title, companies) {
  const parts = String(title).split('|').map(x => x.trim()).filter(Boolean);
  const reminder = parts.some(x => /^(urgent )?reminder$/i.test(x));
  const keep = parts.filter(x => !/^(urgent )?reminder$/i.test(x) && !companies.some(c => companyPatterns(c).some(re => re.test(x))));
  // If every part names the company (e.g. "Urgent Reminder | Texas Instruments Test Registration"), keep them.
  const rest = keep.length ? keep : parts.filter(x => !/^(urgent )?reminder$/i.test(x));
  if (!rest.length) return title;
  return (reminder ? 'Reminder: ' : '') + rest.join(' · ');
}

const istDay = x => new Date(x).toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
// "Today", "Yesterday", "Fri, 31 Oct": headings for the updates list.
export function dayLabel(iso) {
  if (istDay(iso) === istDay(Date.now())) return 'Today';
  if (istDay(iso) === istDay(Date.now() - 86400000)) return 'Yesterday';
  return new Date(iso).toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', weekday: 'short', day: 'numeric', month: 'short' });
}
export const timeOf = iso => new Date(iso).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: 'numeric', minute: '2-digit' });

// label / value can be [full, short]: phones show the short one.
export function stat(label, value, sub, hot = false, urgent = false) {
  const [lf, ls] = [label].flat(), [vf, vs] = [value].flat();
  const two = (f, s, cls) => s == null || s === f ? `<div class="${cls}">${esc(f)}</div>`
    : `<div class="${cls}"><span class="full">${esc(f)}</span><span class="short">${esc(s)}</span></div>`;
  return `<div class="stat ${hot ? 'hot' : ''}" title="${esc(lf)}: ${esc(vf)}${sub ? ` (${esc(sub)})` : ''}">${two(lf, ls, 'label')}${
    two(vf, vs, `value ${urgent ? 'urgent' : ''}`)}<div class="sub">${esc(sub)}</div></div>`;
}
