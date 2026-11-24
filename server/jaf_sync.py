#!/usr/bin/env python3
"""IITB JAF Tracker, server edition: does what the Chrome extension did, without a browser.

Every run (systemd timer, every 15 minutes) it:
  - reads your signed JAFs and the open JAFs from the placement portal API (GET only, read-only),
  - reads new placement-blog posts, renewing the blog session through IITB SSO when needed,
  - works out tests / PPTs / deadlines and sends reminders 24 h, 12 h and 4 h before,
  - sends short updates to Telegram, and writes the data the website shows.

Your login (portal token + IITB SSO cookies) arrives in ~/jaf-sync/session.json from the
extension on your laptop (see web.py /api/session). No password is ever stored.

Quiet hours (config.json → quiet, default 00:00–07:00 IST) hold messages until morning, except reminders
for things due before then. A morning plan goes out daily (config.json → morning, default 8 am), and tonight's remaining deadlines at
config.json → evening (default 8 pm).
If config.json has "healthcheck" (a healthchecks.io ping URL), every run pings it.

New JAFs you can sign get an AI summary on WhatsApp (Gemini or Claude, see jaf_ai.py) within a hard monthly budget.

Usage:  jaf_sync.py [sync | catchup | morning | evening | status | ai
                     | summarize-open [--dry-run] [--limit N] | set-gemini-key | set-anthropic-key | set-openrouter-key]
"""
import fcntl
import getpass
import hashlib
import html
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import csv
import io
from xml.etree import ElementTree
from base64 import urlsafe_b64decode
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
DIR = HOME / "jaf-sync"
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for jaf_ai.py next to this file (python -I skips it)
SESSION = DIR / "session.json"
STATE = DIR / "state.json"
CONFIG = DIR / "config.json"
HIDDEN = DIR / "hidden.json"  # companies you've hidden (set from the dashboard / website)
WA_OUTBOX = DIR / "wa-outbox"     # messages waiting for the WhatsApp service (jaf_whatsapp.py serve)
WA_STATUS = DIR / "wa-status.json"  # the service's heartbeat
WA_CHUNK = 4000
SENT_LOG = DIR / "sent.jsonl"  # every message sent to you, readable only by this account (0600)
SENT_LOG_MAX = 5_000_000  # bytes; then the older half is dropped
WEB_DATA = HOME / "jaf-web-data.json"

ORIGIN = "https://campus.placements.iitb.ac.in"
BLOG_HOME = f"{ORIGIN}/blog/"
BLOG_URL = f"{ORIGIN}/blog/placement/"
BLOG_LOGIN = f"{ORIGIN}/blog/placement/authplacement/"
IST = timezone(timedelta(hours=5, minutes=30))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
MAX_PAGES = 40
TEXT_LIMIT = 8000
POST_FORMAT = 4  # 2: links kept as "label (url)"; 3: broken links dropped; 4: once, to pick up old edits. A change re-reads the blog once
FULL_READ = 86400  # seconds between full blog reads (every page: edits to old posts, posts taken down)
GONE_MAX = 5
JAF_TITLE = re.compile(r"\bjaf\b", re.I)
EDIT_DAYS = 2  # shortcut: edits to posts older than this are missed; raise it if the blog edits old posts
DETAIL_TTL = 3600            # seconds before re-reading a JAF's details
BLOG_WARN = 3600             # seconds the blog must keep failing before telling you
REMIND_HOURS = (4, 12, 24)


# ---------- small helpers ----------

def load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save(path, data, private=False):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # its own: web.py writes hidden.json too
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def decode_jwt(token):
    try:
        part = token.split(".")[1]
        return json.loads(urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (AttributeError, IndexError, ValueError):
        return None


def money(value, currency="INR"):
    if value is None:
        return "—"
    if currency == "INR":
        if value >= 1e5:
            return "₹" + re.sub(r"\.?0+$", "", f"{value / 1e5:.2f}") + " L"
        return f"₹{value:,}"
    return f"${value:,.0f}" if currency == "USD" else f"{currency} {value:,.0f}"


def when(s):
    """"today at 9:00 pm", "tomorrow at 11:59 pm", "on Sat, 1 Nov at 5:00 pm" (IST)."""
    dt = parse_iso(s).astimezone(IST)
    today = now().astimezone(IST).date()
    time = dt.strftime("%-I:%M %p").lower()
    if dt.date() == today:
        return f"today at {time}"
    if dt.date() == today + timedelta(days=1):
        return f"tomorrow at {time}"
    return f"on {dt.strftime('%a, %-d %b')} at {time}"


def until(s):
    secs = round((parse_iso(s) - now()).total_seconds())
    if secs <= 0:
        return "closed"
    if secs < 3600:
        return f"in {-(-secs // 60)} min"
    if secs < 86400:
        return f"in {secs // 3600} h {secs % 3600 // 60} min"
    return f"in {secs // 86400} d {secs % 86400 // 3600} h"


def _fmt_mins(m):
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60} min" if m % 60 else f"{m // 60} h"


def _one_duration(raw):
    s = str(raw).strip()
    if m := re.fullmatch(r"(\d{1,2}):(\d{2})", s):
        return _fmt_mins(int(m[1]) * 60 + int(m[2]))
    if m := re.fullmatch(r"\d+(?:\.\d+)?", s):  # bare number: hours if small, else minutes
        n = float(s)
        return _fmt_mins(round(n * 60)) if n <= 5 else _fmt_mins(round(n))
    if m := re.fullmatch(r"(\d+)\s*(?:m|min|mins|minutes?)\.?", s, re.I):
        return _fmt_mins(int(m[1]))
    if m := re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hours?)\.?", s, re.I):
        return _fmt_mins(round(float(m[1]) * 60))
    return s


def format_duration(raw):
    """Round durations come as free text: '["01:30"]', '["90"]', '["3"]', '["45 min","45 min"]'…"""
    if raw in (None, ""):
        return ""
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        items = [raw]
    items = [_one_duration(x) for x in (items if isinstance(items, list) else [items]) if x is not None and str(x).strip()]
    if not items:
        return ""
    return f"{len(items)} × {items[0]}" if len(items) > 1 and len(set(items)) == 1 else " + ".join(items)


def rounds_from(processes):
    """A JAF's selection rounds in order, with readable durations."""
    return [{"name": p["name"], "duration": format_duration(p.get("duration"))}
            for p in sorted(processes or [], key=lambda p: p.get("order", 0))]


def short_name(c):
    c = re.sub(r"\[.*?\]", "", c)
    c = re.sub(r"\b(pvt|private|ltd|limited|llp|inc)\b\.?", "", c, flags=re.I)
    return re.sub(r"\s+", " ", c).strip()


# ---------- matching posts to your companies (same rules as the extension) ----------

LEGAL = {"pvt", "private", "ltd", "limited", "llp", "inc", "india", "co", "corp", "corporation", "company"}
GENERIC = {"research", "technologies", "technology", "solutions", "services", "systems", "global", "capital",
           "software", "group", "labs", "india", "indian", "american", "national", "international", "digital",
           "financial", "bank"}
_patterns = {}


def company_patterns(name):
    if name not in _patterns:
        words = re.sub(r"[.,]", " ", re.sub(r"\[.*?\]|\(.*?\)", " ", name)).lower().split()
        while len(words) > 1 and words[-1] in LEGAL:
            words.pop()
        phrases = {" ".join(words)}
        if words and len(words[0]) >= 5 and words[0] not in GENERIC:
            phrases.add(words[0])
        _patterns[name] = [re.compile(r"\b" + r"\s+".join(map(re.escape, p.split())) + r"\b", re.I) for p in phrases]
    return _patterns[name]


# "Google Form", "Microsoft Teams"… name a tool, not the company: they mustn't make a post count as Google's.
PRODUCT_NAMES = re.compile(r"https?://\S+|\b(?:google|microsoft|ms)\s+(?:forms?|sheets?|docs?|drive|meet|calendar|teams|excel)\b", re.I)


def match_jobs(post, jobs):
    head = PRODUCT_NAMES.sub(" ", f"{post['title']} {without_links(post['text'])[:300]}")
    return [j["id"] for j in jobs if any(p.search(head) for p in company_patterns(j["company"]))]


def relevant_posts(posts, jobs):
    out = []
    for p in posts:
        ids = match_jobs(p, jobs)
        if (ids and not p.get("forOthers")) or p.get("mentionsMe"):
            out.append({**p, "jobIds": ids})
    return out


def hidden_companies():
    return set(load(HIDDEN, []))


def active_jobs(jobs):
    """Your signed JAFs, minus companies you've hidden."""
    hidden = hidden_companies()
    return [j for j in jobs.values() if j["company"] not in hidden]


def visible_events(events):
    hidden = {short_name(c) for c in hidden_companies()}
    return [e for e in events if e["company"] not in hidden]


def title_company(title):
    """The company named in a post title: "Reminder | Navi Limited | Pre-Placement Talk (PPT)" -> "Navi"."""
    parts = [x.strip() for x in title.split("|") if x.strip()]
    for x in parts:
        if not re.search(r"^(?:reminder|urgent|update|important|mandatory)\b|pre-?placement|\bppt\b|\bset\s*\d", x, re.I):
            return short_name(x)
    return short_name(parts[0]) if parts else "Unknown"


def company_of(post, jobs):
    j = jobs.get(str(post["jobIds"][0])) if post.get("jobIds") else None
    return short_name(j["company"]) if j else title_company(post["title"])


PPT_TITLE = re.compile(r"pre-?placement talk|\bppt\b", re.I)


def ppt_posts(posts, jobs):
    """PPT posts of every other company: PPTs usually come before the JAF opens, so you're reminded of all of
    them (`hide <company>` stops one). Posts about your signed companies are already covered by relevant_posts."""
    hidden = {short_name(c).lower() for c in hidden_companies()}
    return [{**p, "jobIds": []} for p in posts
            if PPT_TITLE.search(p["title"]) and not match_jobs(p, jobs) and title_company(p["title"]).lower() not in hidden]


# ---------- blog parsing ----------

LINK_IN_TEXT = re.compile(r" ?\((https?://[^\s()]+)\)")


def without_links(text):
    """Post text minus the "(https://…)" parts html_to_text(links=True) adds."""
    return LINK_IN_TEXT.sub("", text)


def _link(m):
    href = html.unescape(m.group(2)).strip()
    label = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", m.group(3)))).strip()
    url = urllib.parse.urljoin(BLOG_URL, href)
    host = urllib.parse.urlparse(url).hostname or ""
    if not url.startswith(("http://", "https://")) or href.startswith("#") or not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", host):
        return label  # not a real web address (e.g. a file on the poster's laptop): keep only its text
    url = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return url if not label or label == href or label == url else f"{label} ({url})"


def html_to_text(s, links=False):
    s = re.sub(r"<(script|style)[\s\S]*?</\1>", "", s, flags=re.I)
    if links:  # keep where links point: <a href="https://x">Click Here</a> -> "Click Here (https://x)"
        s = re.sub(r"<a\b[^>]*?\bhref\s*=\s*([\"'])(.*?)\1[^>]*>([\s\S]*?)</a>", _link, s, flags=re.I)
    s = re.sub(r"<br\s*/?>|</(p|li|tr|h\d|div|ol|ul|table)>", "\n", s, flags=re.I)
    s = re.sub(r"</t[dh]>", "\t", s, flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    lines = (re.sub(r"[ \t ]+", " ", line).strip() for line in s.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def mention_test(me):
    """text -> why it names you ("your roll number is in the post"), or None."""
    pats = []
    if me.get("roll"):
        pats.append((re.compile(rf"\b{re.escape(me['roll'])}\b", re.I), "your roll number is in the post"))
    name = (me.get("name") or "").strip()
    if " " in name:
        pats.append((re.compile(r"\b" + r"\s+".join(map(re.escape, name.split())) + r"\b", re.I), "your full name is in the post"))
    return lambda text: next((why for p, why in pats if p.search(text)), None)


PROGRAMMES = re.compile(r"(?<![A-Za-z])(?:MS\s*\(\s*R\s*\)|M\.?\s?Tech|B\.?\s?Tech|Ph\.?\s?D|MBA|M\.?\s?Sc|M\.?\s?Des|B\.?\s?Des|"
                        r"Dual\s+Degree|PG|UG)(?![A-Za-z])", re.I)
PROGRAMME_GROUPS = {"pg": {"mtech", "msr", "phd", "mba", "msc", "mdes"}, "ug": {"btech", "bdes", "dualdegree"}}


def for_other_programme(text, me):
    """True for a post that's only for other programmes' students ("Test is only for MS(R) Students")."""
    mine = re.sub(r"[^a-z]", "", (me or {}).get("programName", "").lower())
    if not mine:
        return False
    for line in re.split(r"(?<=[.!])\s+|\n", text):
        if re.search(r"\bonly\b", line, re.I) and re.search(r"\bstudents?\b", line, re.I):
            named = {re.sub(r"[^a-z]", "", m.lower()) for m in PROGRAMMES.findall(line)}
            if named and mine not in named and not any(mine in PROGRAMME_GROUPS.get(n, ()) for n in named):
                return True
    return False


def parse_blog(page, me):
    mentions = mention_test(me)
    posts = []
    for part in page.split("<article")[1:]:
        chunk = part.split("</article>")[0]
        pid = re.search(r'id="post-(\d+)"', chunk)
        if not pid:
            continue
        date = re.search(r'<time[^>]*datetime="([^"]+)"', chunk)
        title = re.search(r'class="[^"]*entry-title[^"]*"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', chunk)
        body = re.search(r'class="[^"]*entry-content[^"]*"[^>]*>([\s\S]*)$', chunk)
        text = html_to_text(body.group(1) if body else "", links=True)
        posts.append({
            "id": pid.group(1),
            "date": date.group(1) if date else None,
            "link": html.unescape(title.group(1)) if title else BLOG_URL,
            "title": html_to_text(title.group(2)) if title else "(untitled)",
            "text": text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + "\n…",
            "mentionsMe": bool(why := mentions(text)),
            "mentionWhy": why,
            "forOthers": for_other_programme(text, me),
        })
    return posts


# ---------- events (tests, PPTs, interviews) read from posts ----------

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MON = "|".join(MONTHS)
DATE_DM = re.compile(rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MON})[a-z]*\.?,?\s*(?:\(\w+\)\s*,?\s*)?(\d{{4}})?", re.I)
DATE_MD = re.compile(rf"\b({MON})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b,?\s*(\d{{4}})?", re.I)
DATE_NUM = re.compile(r"(?<![\d/.-])(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})(?![\d/.-])")  # 22/11/2026, day first
# Lines about a form or registration, not the test itself ("Deadline of the Registration – 5:30 PM")
DEADLINE_LINE = re.compile(r"deadline|regist|\bforms?\b|submi|\bfill", re.I)
TIME = re.compile(r"(?<![\d:])(\d{1,2})(?:\s?[:.]\s?(\d{2}))?\s*([ap])\.?\s*m\b", re.I)  # also "9: 30 AM"
KINDS = [
    (re.compile(r"registration|mock", re.I), "Registration / mock test due", "📝"),
    (re.compile(r"\btest\b|assessment|exam", re.I), "Test", "📝"),
    (re.compile(r"pre-placement talk|\bppt\b", re.I), "PPT", "🎤"),
    (re.compile(r"group discussion|\bgd\b", re.I), "Group discussion", "🗣️"),
    (re.compile(r"\binterview\b(?!\s*shortlist)", re.I), "Interview", "💼"),
]


def find_date(s, year):
    m = DATE_DM.search(s)
    if m:
        return int(m.group(1)), MONTHS.index(m.group(2).lower()[:3]), int(m.group(3) or year)
    m = DATE_MD.search(s)
    if m:
        return int(m.group(2)), MONTHS.index(m.group(1).lower()[:3]), int(m.group(3) or year)
    m = DATE_NUM.search(s)
    if m and 1 <= int(m.group(2)) <= 12:
        return int(m.group(1)), int(m.group(2)) - 1, int(m.group(3))
    return None


def find_time(s):
    m = TIME.search(s)
    if not m:
        return None
    return int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0), int(m.group(2) or 0)


JOB_CODE = re.compile(r"\s*job\s*code\s*(\d+)", re.I)


def my_part(text, mine):
    """The lines about your own job, when a post lists several job codes with their own times/venues
    ("Job Code 2: Hardware … Timing 10 am / Job Code 3: System Software … Timing 11:30 am"); "" otherwise."""
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if JOB_CODE.match(line)]
    if len(starts) < 2 or not mine:
        return ""
    codes = {str(j.get("code")) for j in mine}
    titles = [j["title"].lower() for j in mine if j.get("title")]
    for a, b in zip(starts, starts[1:] + [len(lines)]):
        if JOB_CODE.match(lines[a]).group(1) in codes or any(t in lines[a].lower() for t in titles):
            return "\n".join(lines[a:b])
    return ""


def post_jobs(p, jobs):
    """Your signed JAFs a post is about."""
    return [jobs[str(i)] for i in p.get("jobIds") or [] if str(i) in jobs]


def label_value(label, text, between=25):
    """What follows "<label> …:" in text, allowing a few words between ("Date & Time of Test: 9:30 PM")."""
    # Same line only; no digits in between ("Deadline ( 11:59 pm …)": the colon in 11:59 isn't the label's).
    m = re.search(rf"\b{label}\b[^:\n–\d]{{0,{between}}}?[ \t]*[:\-–(][ \t]*([^\n]{{0,90}})", text, re.I)
    return m.group(1) if m else ""


def extract_event(post, company, mine=()):
    """The test / PPT / interview a post announces: the rules' reading, corrected or filled in by the AI's
    quote-checked one when there is one (post["ai"], see with_ai)."""
    kind = next((k for k in KINDS if k[0].search(post["title"])), None)
    return with_ai(rules_event(post, company, mine, kind) if kind else None, post, company)


def rules_event(post, company, mine, kind):
    text = without_links(post["text"])
    part = my_part(text, mine)  # your job's own lines win over the rest of the post

    def seg(label, src=None):
        return label_value(label, text if src is None else src)

    lines = text.split("\n")
    if kind is not KINDS[0]:  # a test's time is on a line about the test, not the registration deadline's
        about = "\n".join(line for line in lines if kind[0].search(line) and not DEADLINE_LINE.search(line))
        rest = "\n".join(line for line in lines if not DEADLINE_LINE.search(line))
    else:
        about, rest = "", text
    year = (parse_iso(post.get("date") or "") or now()).year
    # Your job's lines, then a "Date …:" / "Time …:" label (not on a registration line), then lines about the test.
    due = seg("deadline") if kind is KINDS[0] else ""  # a deadline is the time only of a registration / mock test
    date = (find_date(seg("date", part), year) or find_date(seg("date", rest), year) or find_date(about, year)
            or find_date(due, year) or find_date(rest, year) or find_date(text, year))
    time = ((find_time(part) if part else None) or find_time(seg("updated time")) or find_time(seg("tim(?:e|ings?)", rest))
            or find_time(about) or find_time(due) or find_time(rest) or find_time(text))
    if not date or not time:
        return None
    try:
        at = datetime(date[2], date[1] + 1, date[0], time[0], time[1], tzinfo=IST)
    except ValueError:
        return None
    venue = re.sub(r"\s+", " ", label_value("venue", part, 0) or label_value("venue", text, 0)).strip()[:60]
    return {"id": post["id"], "company": company, "kind": kind[1], "icon": kind[2], "at": iso(at),
            "venue": venue, "title": post["title"], "link": post["link"]}


def with_ai(e, post, company):
    """The AI's reading (post["ai"]: quote-checked, see jaf_ai.read_post) fills in an event the rules missed, and
    corrects their time when its quote is about the event itself ("Date & Time of Test: 9:30 PM"); otherwise a
    disagreement is only flagged (aiAt)."""
    ev = (post.get("ai") or {}).get("event")
    if not ev:
        return e
    kind = next((k for k in KINDS if k[1].lower().startswith(ev["kind"].lower()[:4])), None)
    if not e:
        return {"id": post["id"], "company": company, "kind": kind[1] if kind else ev["kind"],
                "icon": kind[2] if kind else "📌", "at": ev["at"], "venue": ev["venue"], "title": post["title"],
                "link": post["link"], "ai": "read", "quote": ev["quote"]}
    if ev["at"] == e["at"]:
        return e
    if kind and kind[0].search(ev["quote"]) and not DEADLINE_LINE.search(ev["quote"]):
        return {**e, "at": ev["at"], "venue": e["venue"] or ev["venue"], "ai": "fixed", "rulesAt": e["at"],
                "quote": ev["quote"]}
    return {**e, "aiAt": ev["at"]}


def where(e):
    """' · venue · 💺 Your room: LH 101' for an event line (your room from the seating sheet, when known)."""
    out = f" · {e['venue']}" if e.get("venue") and not (e.get("seat") and re.search(r"announc|tba|tbd", e["venue"], re.I)) else ""
    return out + (f" · 💺 Your seat: *{e['seat']}*" if e.get("seat") else "")


def post_event_times(posts, jobs):
    """{post id: when its test/PPT/interview is}, so the website can grey out ones that are over."""
    out = {}
    for p in posts:
        e = extract_event(p, company_of(p, jobs), post_jobs(p, jobs))
        if e:
            out[p["id"]] = e["at"]
    return out


# ---------- action items: forms / registrations you must do, and by when ----------

ACTION = re.compile(r"\b(fill(?:ing)?|submit|complete|register|upload|confirm|select|choose|apply)\b[^.\n]{0,100}?"
                    r"\b(form|registration|preference|track|response|details|portal|link|survey|consent)\b"
                    r"|\bgoogle form\b|\bmandatory (?:form|registration)\b", re.I)
MUST = re.compile(r"\bmust\b|mandator|required|need to|needs to|supposed to|\bplease\b|\bkindly\b"
                  r"|only (?:students|candidates) who|will not be (?:considered|permitted|allowed)|\bbefore\b|\bby\s+\d"
                  r"|for receiving|to (?:receive|get) the|in order to", re.I)
# "fill this form if you haven't received the link" is only for people with a problem
ONLY_IF = re.compile(r"\bif\b|in case|who(?:so)?ever|ha(?:ve|s)n'?t received|not received|facing|issues?\b|quer(?:y|ies)", re.I)
ACTION_TITLE = re.compile(r"mandatory|urgent|form|registration", re.I)
ACTION_HOURS = (24, 12, 4, 1)  # reminders before a form / registration deadline


def action_deadline(post, hits, text):
    """When the task is due: a date/time in the task's own sentences, else in a "Deadline:" line."""
    posted = (parse_iso(post.get("date") or "") or now()).astimezone(IST)
    m = label_value("deadline", text)
    for src in hits + ([m] if m else []):
        t, d = find_time(src), find_date(src, posted.year)
        day = (d[0], d[1] + 1, d[2]) if d else None
        if not day:
            rel = posted + timedelta(days=1) if re.search(r"\btomorrow\b", src, re.I) else posted
            if not t and not re.search(r"\btoday\b|\btonight\b|\btomorrow\b", src, re.I):
                continue
            day = (rel.day, rel.month, rel.year)
        try:
            return iso(datetime(day[2], day[1], day[0], *(t or (23, 59)), tzinfo=IST))
        except ValueError:
            continue
    return None


def rules_action(post, mine=()):
    return action_of({**post, "ai": None}, mine)


def action_of(post, mine=()):
    """{"what": the sentence, "at": deadline or None} when a post asks you to do something: fill a form, register
    on a company portal, choose a track… Skips forms only for people with a problem, and other job codes."""
    text = without_links(post["text"])
    codes = {str(j.get("code")) for j in mine}
    hits = []
    for line in re.split(r"(?<=[.!])\s+|\n", text):
        line = line.strip(" -–•*\t")
        if not ACTION.search(line) or ONLY_IF.search(line):
            continue
        if not (MUST.search(line) or ACTION_TITLE.search(post["title"])):
            continue
        named = set(re.findall(r"job\s*code\s*(\d+)", line, re.I))
        if named and codes and not named & codes:
            continue
        hits.append(line)
    if not hits:
        return None
    a = {"what": hits[0][:200], "at": action_deadline(post, hits, text)}
    dl = (post.get("ai") or {}).get("deadline")  # the AI's quote-checked reading: fills in or corrects
    if dl and dl["at"] != a["at"]:
        if not a["at"] or re.search(r"deadline|\bdue\b|\bby\b|before|last date|regist|fill|submi", dl["quote"], re.I):
            a.update({"at": dl["at"], "ai": "fixed" if a["at"] else "read", "rulesAt": a["at"], "quote": dl["quote"]})
        else:
            a["aiAt"] = dl["at"]
    return a


def action_events(posts, jobs):
    """Form / registration deadlines still ahead, as events (reminders, 8 am / 8 pm, website)."""
    out = {}
    for p in posts:  # newest first: one per company
        company = company_of(p, jobs)
        a = action_of(p, post_jobs(p, jobs))
        if a and a["at"] and parse_iso(a["at"]) > now() - timedelta(hours=1) and company not in out:
            out[company] = {"id": p["id"], "company": company, "kind": "Form / registration due", "icon": "🚨",
                            "at": a["at"], "venue": "", "title": p["title"], "link": p["link"], "what": a["what"],
                            **({"ai": a["ai"]} if a.get("ai") else {})}
    return list(out.values())


def action_line(p, a, company):
    due = (f"⏳ Due *{when(a['at'])}* ({until(a['at'])})" if a["at"] else "⏳ No deadline given: do it soon")
    return f"🚨 *ACTION NEEDED · {company}*\n{a['what']}\n{due}"


# ---------- your room, from seating sheets linked in posts ----------

SEAT_LINE = re.compile(r"seat|seating|allot|arrangement|venue|room", re.I)
SEAT_DAYS = 4            # seating links in posts from the last 4 days
SEAT_RECHECK = 30 * 60   # re-read a sheet every 30 min while its test is still ahead (sheets get updated)
SEAT_MAX_BYTES = 15 * 1024 * 1024


def seat_links(post):
    """Links on lines like "Seating Arrangement: Click Here (https://…)"."""
    return [u for line in post["text"].split("\n") if SEAT_LINE.search(without_links(line)) for u in LINK_IN_TEXT.findall(line)]


def sheet_download_url(url):
    u = urllib.parse.urlparse(url)
    if u.netloc == "bighome.iitb.ac.in" and "/s/" in u.path:  # IITB Nextcloud public share
        return f"https://bighome.iitb.ac.in/index.php/s/{u.path.split('/s/')[1].split('/')[0]}/download"
    if u.netloc == "docs.google.com" and "/spreadsheets/d/" in u.path:
        return f"https://docs.google.com/spreadsheets/d/{u.path.split('/d/')[1].split('/')[0]}/export?format=xlsx"
    if u.netloc == "drive.google.com" and "/file/d/" in u.path:
        return f"https://drive.google.com/uc?export=download&id={u.path.split('/d/')[1].split('/')[0]}"
    return None


def xlsx_rows(data):
    """[(header cells, row cells)] for every row of every sheet."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as z:
        names = z.namelist()
        shared = ["".join(t.text or "" for t in si.iter(ns + "t"))
                  for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).findall(ns + "si")] \
            if "xl/sharedStrings.xml" in names else []
        out = []
        for sheet in sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)):
            header = None
            for r in ElementTree.fromstring(z.read(sheet)).iter(ns + "row"):
                cells = []
                for c in r.findall(ns + "c"):
                    v = c.find(ns + "v")
                    if c.get("t") == "s" and v is not None:
                        cells.append(shared[int(v.text)])
                    else:
                        cells.append(v.text if v is not None else "".join(t.text or "" for t in c.iter(ns + "t")))
                cells = [(x or "").strip() for x in cells]
                if header is None and any(cells):
                    header = cells
                    continue
                out.append((header, cells))
        return out


def csv_rows(data):
    """[(header cells, row cells)] from a CSV file (header: the first row naming "roll", else the first row)."""
    text = data.decode("utf-8-sig", "replace")
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), csv.Sniffer().sniff(text[:2048], ",;\t"))]
    rows = [r for r in rows if any(r)]
    head = next((i for i, r in enumerate(rows[:10]) if any(re.search(r"\broll\b", c, re.I) for c in r)), 0)
    return [(rows[head], r) for r in rows[head + 1:]] if rows else []


def pdf_rows(data):
    """[(header cells, row cells)] from a PDF table, via pdftotext -layout (columns = runs of 2+ spaces)."""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        text = subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True, timeout=60).stdout
    out, header = [], None
    for line in text.split("\n"):
        cells = [c.strip() for c in re.split(r"\s{2,}", line.strip()) if c.strip()]
        if not cells:
            continue
        if re.search(r"\broll\b", line, re.I):
            header = cells
            continue
        out.append((header, cells))
    return out


def my_seat(rows, roll, name):
    """Your room (and seat, if the sheet has one) from your row, e.g. "LH 101" or "LA001 A11"; None if you're not listed."""
    roll_re = re.compile(rf"(?<![A-Za-z0-9]){re.escape(roll)}(?![A-Za-z0-9])", re.I)
    name_words = {w.lower() for w in (name or "").split()}
    for header, cells in rows:
        if not any(roll_re.search(c) for c in cells):
            continue
        keep = []
        for i, c in enumerate(cells):
            label = header[i] if header and len(header) == len(cells) else ""
            rest = roll_re.sub("", c).strip(" -:|")
            if not rest or re.search(r"name|roll|^s\.?\s?no|serial|^sr|^sl|email|branch|dept|depart|program|discipline|course|gender|batch|year|mobile|phone|contact", label, re.I):
                continue
            if (i == 0 and rest.isdigit()) or not re.search(r"\d", rest) and (not label or all(w.lower().strip(".,") in name_words for w in rest.split())):
                continue  # serial number, or a name
            if not re.search(r"\d", rest) and not label:
                continue
            room_label = re.search(r"venue|room|lab|hall|location|centre|center", label, re.I)
            keep.append((not room_label, rest if not label or room_label else f"{label}: {rest}"))
        seat = " · ".join(v for _, v in sorted(keep, key=lambda k: k[0]))[:60]  # the room first, then the seat
        return seat or "listed (room not shown)"
    return None


def fetch_rows(url):
    """Downloads a linked sheet (read-only GET) and returns its rows; the file isn't kept."""
    dl = sheet_download_url(url)
    if not dl:
        raise ValueError("unsupported link")
    req = urllib.request.Request(dl, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(SEAT_MAX_BYTES)
    if data[:4] == b"%PDF":
        rows = pdf_rows(data)
    elif data[:2] == b"PK":
        rows = xlsx_rows(data)
    elif b"\0" not in data[:4096] and re.search(rb"[,;\t]", data[:4096]):
        rows = csv_rows(data)
    else:
        raise ValueError("not a PDF, spreadsheet or CSV")
    return with_sections(rows)


SECTION = re.compile(r"^(?:venue|room|hall|lab|location|centre|center)\b[^:]{0,20}:\s*(\S.{0,40})$", re.I)


def with_sections(rows):
    """Sheets that list students under a heading ("Venue: LA 202") get that room added to each row below it."""
    out, room = [], None
    for header, cells in rows:
        line = " ".join(cells).strip()
        m = SECTION.match(line) if not ROLL_LIKE.search(line) else None
        if m:
            room = m.group(1).strip()
        elif room:
            out.append(((header or [""] * len(cells)) + ["Venue"], cells + [room]))
            continue
        out.append((header, cells))
    return out


def fetch_seat(url, me):
    """Your room from a seating sheet (None if you're not in it); only your own row is looked at."""
    return my_seat(fetch_rows(url), me["roll"], me.get("name", ""))


def update_seats(state, jobs, job_list, notify=True, fresh_ids=()):
    """Finds your room in seating sheets linked from recent posts about your companies; WhatsApp when it's found or
    changes; puts it on the matching test (reminders, website). Only your own row is kept.
    For a post that's new in this sync, the room goes in that post's own message instead (fresh_ids)."""
    me = state.get("me") or {}
    if not me.get("roll"):
        return
    seats = {u: r for u, r in (state.get("seats") or {}).items()
             if (parse_iso(r.get("postDate") or "") or now()) > now() - timedelta(days=10)}
    events = state.get("events") or []
    cutoff = now() - timedelta(days=SEAT_DAYS)
    msgs = []
    for p in relevant_posts(state.get("posts") or [], job_list):
        posted = parse_iso(p.get("date") or "")
        if not posted or posted < cutoff:
            continue
        company = company_of(p, jobs)
        ahead = [e for e in events if e["company"] == company and parse_iso(e["at"]) > now() - timedelta(hours=1)]
        for url in seat_links(p):
            old = seats.get(url)
            if old and "error" not in old and (not ahead or now().timestamp() - old.get("checkedAt", 0) < SEAT_RECHECK):
                continue
            rec = {"postId": p["id"], "company": company, "postDate": p["date"], "checkedAt": now().timestamp()}
            try:
                rec["seat"] = fetch_seat(url, me)
                rec["found"] = rec["seat"] is not None
            except Exception as e:  # noqa: BLE001 - a bad link shouldn't stop the sync
                seats[url] = {**(old or {}), **rec, "error": str(e)[:120]}
                if notify and old is None and (ahead or posted > now() - timedelta(hours=24)):
                    msgs.append(f"💺 *{company}*: I couldn't read the seating sheet, so check your room yourself:\n{url}")
                continue
            changed = old is None or "found" not in old or old.get("seat") != rec["seat"]
            fresh_post = posted > now() - timedelta(hours=24)
            if notify and changed and (ahead or fresh_post) and p["id"] not in fresh_ids:
                ev = ahead[0] if ahead else None
                when_line = f"\n{ev['icon']} {ev['kind']} {when(ev['at'])} ({until(ev['at'])})" if ev else ""
                if rec["found"]:
                    msgs.append(f"💺 *{company}*: your seat is *{rec['seat']}*"
                                + (" (updated)" if old and old.get("found") else "") + f"{when_line}\n{url}")
                elif old is None or old.get("found"):
                    msgs.append(f"💺 *{company}*: your roll number isn't in the seating sheet. Check it yourself:{when_line}\n{url}")
            seats[url] = rec
    state["seats"] = seats
    # Your room on each test: from the newest sheet for that company posted before it (within a week).
    found = sorted((r for r in seats.values() if r.get("found")), key=lambda r: r["postDate"], reverse=True)
    for e in events:
        at = parse_iso(e["at"])
        r = next((r for r in found if r["company"] == e["company"]
                  and timedelta(0) <= at - parse_iso(r["postDate"]) <= timedelta(days=7)), None)
        if r:
            e["seat"] = r["seat"]
        else:
            e.pop("seat", None)
    state["postSeats"] = {r["postId"]: r["seat"] for r in found}
    if msgs:
        send_digest("\n\n".join(msgs), msgs, state=state)


# ---------- shortlists: are you still in? ----------

LIST_TITLE = re.compile(r"short-?list|wait-?list|selected|selection list|final (?:list|selection)|result|offer", re.I)
LIST_LINK = re.compile("(?:" + LIST_TITLE.pattern + r")[^\n]*https?://", re.I)  # "Test Shortlist: Link (https://…)" in a "Test Announcement"
ROLL_LIKE = re.compile(r"(?<![A-Za-z0-9])\d{2}[A-Za-z]\d{4}(?![A-Za-z0-9])|(?<!\d)\d{9}(?!\d)")
LIST_DAYS = 10         # lists in posts from the last 10 days are read
LIST_MIN = 5           # a list needs this many roll numbers before "you're not on it" counts
LIST_NOTIFY_DAYS = 3   # older lists change things quietly
LIST_TRIES = 3


def check_lists(state, posts, jobs, fresh_ids=(), notify=True):
    """Reads shortlists / results about your companies: the post text and any linked sheet (bighome PDF, Google
    Sheet). Your roll number there: the post counts as naming you (🎉, Mentions). Roll numbers but not yours:
    you're out, so the company is hidden (once per list; `unhide` undoes it) and you're told.
    Only that yes/no is kept, never the list."""
    roll = (state.get("me") or {}).get("roll")
    if not roll:
        return
    mine = re.compile(rf"(?<![A-Za-z0-9]){re.escape(roll)}(?![A-Za-z0-9])", re.I)
    lists = {k: v for k, v in (state.get("lists") or {}).items()
             if now().timestamp() - v.get("checkedAt", 0) < 30 * 86400}
    cutoff = now() - timedelta(days=LIST_DAYS)
    msgs, hide = [], set()
    for p in relevant_posts(posts, active_jobs(jobs)):
        posted = parse_iso(p.get("date") or "")
        if not p["jobIds"] or not (LIST_TITLE.search(p["title"]) or LIST_LINK.search(p["text"])) or not posted or posted < cutoff:
            continue
        old = lists.get(p["id"]) or {}
        if "inList" in old or old.get("tries", 0) >= LIST_TRIES:
            continue
        texts, failed = [without_links(p["text"])], False
        for url in dict.fromkeys(u.rstrip(".,;:") for u in re.findall(r"https?://[^\s()<>]+", p["text"])):
            if sheet_download_url(url):  # "label (url)" links and bare ones
                try:
                    texts += [c for _, cells in fetch_rows(url) for c in cells]
                except Exception:  # noqa: BLE001 - try again next sync
                    failed = True
        names = sorted({jobs[str(i)]["company"] for i in p["jobIds"] if str(i) in jobs})
        rec = {"postId": p["id"], "companies": names, "checkedAt": now().timestamp(), "tries": old.get("tries", 0) + 1}
        if p.get("mentionsMe") or any(mine.search(t) for t in texts):
            rec["inList"] = True
        elif not failed and sum(len(ROLL_LIKE.findall(t)) for t in texts) >= LIST_MIN:
            rec["inList"] = False
        lists[p["id"]] = rec
        recent = notify and posted > now() - timedelta(days=LIST_NOTIFY_DAYS)
        short = " / ".join(short_name(n) for n in names)
        what = "|".join(p["title"].split("|")[1:]).strip() or p["title"]  # "Test Shortlist"
        if rec.get("inList") is True and not p.get("mentionsMe") and recent and p["id"] not in fresh_ids:
            msgs.append(f"🎉 You're on the *{short}* {what}!\n{p['link']}")
        elif rec.get("inList") is False:
            hide.update(names)
            rec["hid"] = names
            if recent:
                msgs.append(f"🙁 You're not on the *{short}* {what}, so I've hidden {short}: no more updates or "
                            f"reminders for it. Wrong? Reply `unhide {short_name(names[0]).split()[0].lower()}`.\n{p['link']}")
    state["lists"] = lists
    # A list naming you (even only in its linked sheet) counts as a post naming you.
    yes = {k for k, v in lists.items() if v.get("inList")}
    for p in posts:
        if p["id"] in yes:
            p["mentionsMe"] = True
            sheet = "seating sheet" if seat_links(p) and not LIST_TITLE.search(p["title"]) else "list"
            p["mentionWhy"] = p.get("mentionWhy") or f"your roll number is in the {sheet} linked in this post"
    if hide:
        save(HIDDEN, sorted(hidden_companies() | hide), private=True)
    if msgs:
        send_digest("\n\n".join(msgs), msgs, state=state)


def extract_events(posts, jobs):
    seen = {}
    for p in posts:  # newest first: an "update" post replaces the earlier announcement
        e = extract_event(p, company_of(p, jobs), post_jobs(p, jobs))
        if e and (e["company"], e["kind"]) not in seen:
            seen[(e["company"], e["kind"])] = e
    return sorted([*seen.values(), *action_events(posts, jobs)], key=lambda e: e["at"])


def describe_post(p, jobs, seat=None):
    """One short line built from the title and event time (and your room, when the seating sheet is known)."""
    company = company_of(p, jobs)
    room = f" · 💺 Your seat: *{seat}*" if seat else ""
    if p.get("mentionsMe"):
        if re.search(r"shortlist", p["title"], re.I):
            return f"🎉 You're on the *{company}* shortlist! ({p['title']})"
        return f"🎉 Your name is in: *{p['title']}*"
    e = extract_event(p, company, post_jobs(p, jobs))
    if e and parse_iso(e["at"]) > now():
        return f"{e['icon']} *{company}*: {e['kind']} {when(e['at'])}" + where({**e, "seat": seat}) + ai_note(e)
    rest = "|".join(p["title"].split("|")[1:]).strip()
    unread = "\n⚠️ I couldn't read the time from this post: check it yourself." if not e and unreadable(p) else ""
    return f"📢 *{company}*: {rest or p['title']}" + room + unread


def ai_note(e=None, a=None):
    """What the AI changed or doubts about an event's time (e) or a form's deadline (a), for the message."""
    out = ""
    for x, what in ((e, "time"), (a, "deadline")):
        if not x:
            continue
        if x.get("ai") == "read":
            out += f"\n🤖 The {what} was read by AI from “{x['quote']}”: check the post."
        elif x.get("ai") == "fixed":
            out += f"\n🤖 {what.capitalize()} corrected by AI (I'd read {when(x['rulesAt'])}) from “{x['quote']}”"
        elif x.get("aiAt"):
            out += f"\n⚠️ The AI read the {what} as *{when(x['aiAt'])}*: check the post."
    return out


def ai_read_posts(state, posts, jobs):
    """The AI reads recent posts about tests, interviews, PPTs and forms a second time (once per version of the
    post), into state["aiReads"]; attach_ai() puts it on the posts. Off without an AI key or with ai.postChecks false."""
    try:
        import jaf_ai
    except ImportError:
        return
    if not jaf_ai.settings()["key"] or not jaf_ai.settings()["postChecks"]:
        return
    reads = {k: v for k, v in (state.get("aiReads") or {}).items() if now().timestamp() - v.get("checkedAt", 0) < 30 * 86400}
    cutoff = now() - timedelta(days=EDIT_DAYS)
    for p in posts:
        posted = parse_iso(p.get("date") or "")
        mine = post_jobs(p, jobs)
        if not posted or posted < cutoff or not (any(k[0].search(p["title"]) for k in KINDS)
                                                 or rules_action(p, mine) or unreadable(p)):
            continue
        version = post_version(p)
        if (reads.get(p["id"]) or {}).get("version") == version:
            continue
        try:
            reads[p["id"]] = {**jaf_ai.read_post(p, mine), "version": version, "checkedAt": now().timestamp()}
            state.pop("aiError", None)  # the AI answers again
        except (jaf_ai.AIUnavailable, jaf_ai.BudgetExceeded) as e:
            state["aiError"] = str(e)
            break  # the rest wait for the next sync
    state["aiReads"] = reads


def attach_ai(state, posts):
    """Puts each post's AI reading on it (post["ai"]) when it was made from the post as it is now."""
    reads = state.get("aiReads") or {}
    for p in posts:
        r = reads.get(p["id"])
        if r and r.get("version") == post_version(p):
            p["ai"] = {"event": r.get("event"), "deadline": r.get("deadline")}
        else:
            p.pop("ai", None)


def post_version(p):
    return hashlib.sha1((p["title"] + p["text"]).encode()).hexdigest()[:12]


def unreadable(p):
    """A test / interview / PPT post, or an update that mentions one, with a date still ahead, but no event time
    I could read (extract_event needs the kind in the title, a date and a time)."""
    text = without_links(p["text"])
    if JAF_TITLE.search(p["title"]) or not any(k[0].search(p["title"] + "\n" + text) for k in KINDS[1:]):
        return False
    posted = parse_iso(p.get("date") or "") or now()
    d = find_date(text, posted.year)
    try:
        return bool(d) and datetime(d[2], d[1] + 1, d[0], 23, 59, tzinfo=IST) > now()
    except ValueError:
        return False


def post_seat(state, p):
    """Your room for a post: from its own seating sheet, or the sheet already matched to its test."""
    return (state.get("postSeats") or {}).get(p["id"]) or next(
        (e["seat"] for e in state.get("events") or [] if e["id"] == p["id"] and e.get("seat")), None)


def full_post(p, line):
    text = re.sub(r"\n{2,}", "\n", p["text"])
    return f"{line}\n{text}\n{p['link']}"


# ---------- sending (Telegram) ----------

def tg_html(text):
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", text)


def send_telegram(text, cfg):
    body = json.dumps({"chat_id": cfg["chatId"], "text": tg_html(text), "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{cfg['token']}/sendMessage", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        if not json.load(r).get("ok"):
            raise RuntimeError("Telegram refused the message")
    log_sent("telegram", text)


def log_sent(to, text):
    """Keeps what was sent (to check later what went out); never breaks sending."""
    try:
        fd = os.open(SENT_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps({"at": iso(now()), "to": to, "text": text}, ensure_ascii=False) + "\n")
        if SENT_LOG.stat().st_size > SENT_LOG_MAX:
            # shortcut: a line appended by the other process during this trim can be lost; fine for a log.
            lines = SENT_LOG.read_text().splitlines(keepends=True)
            save_text = "".join(lines[len(lines) // 2:])
            tmp = SENT_LOG.with_name(f".sent.{os.getpid()}.tmp")
            tmp.write_text(save_text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, SENT_LOG)
    except OSError as e:
        print(f"sent log: {e}", file=sys.stderr)


def chunks(text, size):
    """Splits on blank lines so a long digest goes out as several complete messages."""
    out, cur = [], ""
    for part in text.split("\n\n"):
        piece = part if len(part) <= size else part[:size - 1] + "…"
        if cur and len(cur) + len(piece) + 2 > size:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        out.append(cur)
    return out


def send_whatsapp(messages):
    """Queues messages for the WhatsApp service (spare number → your main number), one message per item.
    Raises if the service reports a problem, so you hear about it on Telegram; the messages stay queued."""
    parts = [c for m in messages for c in chunks(m, WA_CHUNK)]  # only very long items get split
    WA_OUTBOX.mkdir(mode=0o700, exist_ok=True)
    name = f"{time.time_ns()}.json"
    tmp = WA_OUTBOX / f".{name}.tmp"
    tmp.write_text(json.dumps({"messages": parts}, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, WA_OUTBOX / name)
    status = load(WA_STATUS, {})
    if status.get("error"):
        raise RuntimeError(status["error"])
    if now().timestamp() - status.get("at", 0) > 600:
        raise RuntimeError("the WhatsApp service isn't running (messages are queued)")


def quiet_window(cfg=None):
    """Today's quiet hours as (start, end) datetimes in IST, from config.json → quiet (default 00:00–07:00)."""
    q = (cfg or load(CONFIG, {})).get("quiet") or {}
    start_h, end_h = int(q.get("start", 0)), int(q.get("end", 7))
    day = now().astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    return day + timedelta(hours=start_h), day + timedelta(hours=end_h)


def in_quiet():
    start, end = quiet_window()
    return start <= now() < end


def send_digest(text, items=None, state=None, urgent=False):
    """Sends now, or during quiet hours keeps it for the morning (unless urgent)."""
    state = state if state is not None else {}
    if not urgent and in_quiet():
        state.setdefault("outbox", []).append({"text": text, "items": items or [text]})
        return
    deliver(text, items, state)


def flush_outbox(state):
    """After quiet hours: everything held overnight, in one go."""
    held = state.get("outbox") or []
    if not held or in_quiet():
        return
    state["outbox"] = []
    deliver("*While you were asleep*\n\n" + "\n\n".join(h["text"] for h in held),
            [i for h in held for i in h["items"]], state)


def deliver(text, items=None, state=None):
    """Telegram gets `text` as one summary. WhatsApp (your own number) gets `items`, one message each
    (full post text included), or `text` as a single message when there are no items."""
    state = state if state is not None else {}
    cfg = load(CONFIG, {})
    tg = cfg.get("telegram") or {}
    msg = text if len(text) <= 3000 else text[:2990] + "…"
    tg_on = tg.get("enabled") and tg.get("token") and tg.get("chatId")
    if tg_on:
        try:
            send_telegram(msg, tg)
            state["telegramStatus"] = {"ok": True, "at": iso(now())}
        except Exception as e:  # noqa: BLE001 - report any failure, keep going
            state["telegramStatus"] = {"ok": False, "at": iso(now()), "msg": str(e)}
    wa = cfg.get("whatsapp") or {}
    if wa.get("enabled") and wa.get("to"):
        try:
            send_whatsapp(items or [text])
            state["whatsappStatus"] = {"ok": True, "at": iso(now())}
            state["waAlerted"] = False
        except Exception as e:  # noqa: BLE001
            state["whatsappStatus"] = {"ok": False, "at": iso(now()), "msg": str(e)}
            # The spare number got unlinked: say so once on Telegram, since WhatsApp can't reach you.
            if re.search(r"linked|logged out", str(e)) and not state.get("waAlerted") and tg_on:
                try:
                    send_telegram("📵 *WhatsApp sender needs linking again*: the spare number was logged out on the "
                                  "server. Ask Claude to re-link it (pairing code).", tg)
                    state["waAlerted"] = True
                except Exception:  # noqa: BLE001
                    pass


# ---------- portal (GET only) ----------

class AuthError(Exception):
    pass


def api(path, token):
    req = urllib.request.Request(ORIGIN + path, headers={"Authorization": f"Bearer {token}", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError() from e
        raise RuntimeError(f"{path.split('?')[0]} → HTTP {e.code}") from e


def job_detail(d, program_id):
    salaries = d.get("salaries") or []
    mine = next((s for s in salaries if s.get("programId") == program_id), None)
    sal = mine or (salaries[0] if salaries else {})

    def clip(raw, n):
        t = html_to_text(raw or "")
        return t if len(t) <= n else t[:n] + "…"

    return {
        "currency": d.get("currency") or "INR",
        "ctc": sal.get("ctc"),
        "gross": sal.get("amount"),
        "designation": (sal.get("designation") or "").strip(),
        "forMyProgram": bool(mine),
        "locations": [l["location"].strip() for l in d.get("locations") or []],
        "cpiCutoff": d.get("cpiCutoff"),
        "bond": d.get("bond") or 0,
        "requirements": clip(d.get("additionalRequirements"), 1500),
        "info": clip(d.get("additionalInfo"), 2000),
        "description": clip(d.get("jobDescription"), 3000),
        "rounds": rounds_from(d.get("processes")),
    }


# ---------- blog (with your IITB SSO session) ----------

def cookie_jar(cookies):
    jar = http.cookiejar.CookieJar()
    for c in cookies:
        dom = c["domain"]
        jar.set_cookie(http.cookiejar.Cookie(
            0, c["name"], c["value"], None, False, dom, True, dom.startswith("."), c.get("path", "/"), True,
            bool(c.get("secure")), int(c["expirationDate"]) if c.get("expirationDate") else None,
            False, None, None, {"HttpOnly": None} if c.get("httpOnly") else {}))
    return jar


def jar_cookies(jar):
    return [{"domain": c.domain, "name": c.name, "value": c.value, "path": c.path, "secure": c.secure,
             "httpOnly": c.has_nonstandard_attr("HttpOnly"), "expirationDate": c.expires} for c in jar]


def read_blog(state, me, jar, first_run, full=False):
    """Returns {known, fresh, edited: [(old, new)], gone} or {error, ssoLoggedOut?}. first_run (or a format change)
    re-reads every page quietly; full re-reads every page and reports edits and posts taken down."""
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    # Like a normal page visit: the blog's SSO module only signs in requests that ask for a web page.
    op.addheaders = [("User-Agent", UA), ("Accept", "text/html,application/xhtml+xml,*/*;q=0.8"),
                     ("Accept-Language", "en-US,en;q=0.9")]

    def get(url, ref=None):
        req = urllib.request.Request(url, headers={"Referer": ref} if ref else {})
        try:
            with op.open(req, timeout=60) as r:
                return r.status, r.geturl(), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, url, e.read().decode("utf-8", "replace")

    def is_blog(r):
        return r[0] == 200 and urllib.parse.urlparse(r[1]).path.startswith("/blog/") and "<article" in r[2]

    first = get(BLOG_URL)
    if not is_blog(first):
        login = get(BLOG_LOGIN, ref=BLOG_HOME)  # signs in with your IITB SSO session, no password
        if urllib.parse.urlparse(login[1]).netloc == "sso.iitb.ac.in":
            return {"error": "IITB SSO session ended", "ssoLoggedOut": True}
        first = get(BLOG_URL)
        if not is_blog(first):
            return {"error": f"blog: HTTP {first[0]} at {urllib.parse.urlparse(first[1]).path}"}

    known = {p["id"]: p for p in state.get("posts", [])}
    old_ids = set(known)
    fresh, edited, seen = [], [], set()
    versions = state.get("postVersions") or {}  # {post id: versions already seen}
    last_date = ""
    for page in range(1, MAX_PAGES + 1):
        res = first if page == 1 else get(f"{BLOG_URL}?paged={page}")
        if res[0] == 404 or not is_blog(res):
            break
        posts = parse_blog(res[2], me)
        for p in posts:
            old = known.get(p["id"])
            if old is None:
                fresh.append(p)
            elif not first_run and (old["title"], old["text"]) != (p["title"], p["text"]):
                # A post flipping back and forth (the blog serving two versions) is reported once, not every sync.
                # shortcut: a real edit back to an earlier version isn't reported either.
                seen_v = versions.setdefault(p["id"], [post_version(old)])
                if post_version(p) not in seen_v:
                    edited.append((old, p))
                    versions[p["id"]] = (seen_v + [post_version(p)])[-5:]
            seen.add(p["id"])
            known[p["id"]] = p
        # Posts get edited after they go up (a draft first, the real title and job codes later): re-read 2 days back.
        last = parse_iso(posts[-1].get("date")) if posts else None
        if not (first_run or full) and posts and posts[-1]["id"] in old_ids and (not last or last < now() - timedelta(days=EDIT_DAYS)):
            break
        last_date = (posts[-1].get("date") or "") if posts else last_date
    gone = []
    if (first_run or full) and seen and last_date:
        # Taken down: newer than the last post we read, yet not on any page. (Not min(): a pinned old post sits on top.)
        gone = [p for i, p in known.items() if i not in seen and (p.get("date") or "") > last_date]
        if len(gone) > GONE_MAX:  # that many at once is a page we misread, not the placement team deleting posts
            gone = []
        for p in gone:
            known.pop(p["id"])
    state["postVersions"] = {i: v for i, v in versions.items() if i in known}
    return {"known": known, "fresh": fresh, "edited": edited, "gone": gone if not first_run else []}


# ---------- sync ----------

def need_login(state, msg):
    if state.get("auth", {}).get("ok") is not False:
        send_digest(f"🔑 *Login needed*: {msg}\nLog in to the placement portal in Chrome on your laptop; "
                    "the extension sends the new login to the server.", state=state)
    state["auth"] = {"ok": False, "msg": msg}


def sync():
    state = load(STATE, {})
    session = load(SESSION, {})
    jwt = decode_jwt(session.get("token", ""))
    try:
        if not jwt or jwt.get("exp", 0) < now().timestamp():
            need_login(state, "Your placement portal login has expired.")
            return state
        token = session["token"]
        me = state.get("me")
        if not me or me.get("applicantId") != jwt.get("id") or "programName" not in me:
            ap = api(f"/api/v1/applicant/{jwt['id']}", token)
            s = ap.get("student") or {}
            program_id, program_name = None, ""
            try:
                program_id = api(f"/api/v1/academic-combination/{s.get('enrolledInId')}", token).get("programId")
                progs = api("/api/v1/program", token)
                prog = next((p for p in (progs if isinstance(progs, list) else progs.get("data", [])) if p.get("id") == program_id), {})
                program_name = prog.get("abbreviation") or prog.get("shortName") or prog.get("name") or ""
            except RuntimeError:
                pass
            me = {"applicantId": jwt["id"], "roll": s.get("roll") or jwt.get("username"),
                  "name": (s.get("person") or {}).get("name", ""), "programId": program_id, "programName": program_name}

        # Your signed JAFs
        jobs = {}
        for a in api(f"/api/v1/job/application?applicantId={jwt['id']}", token):
            d = api(f"/api/v1/job/{a['jobId']}", token)
            jobs[str(a["jobId"])] = {
                **job_detail(d, me["programId"]),
                "id": a["jobId"],
                "company": (d.get("company") or {}).get("name") or "Unknown company",
                "title": (d.get("title") or "").strip(),
                "code": d.get("code"),
                "opensAt": d.get("opensAt"),
                "closesAt": d.get("closesAt"),
                "signedAt": a.get("createdAt"),
                "rounds": rounds_from(d.get("processes") or (a.get("job") or {}).get("processes")),
            }

        # All JAFs open right now, with the portal's own eligibility flags
        qs = urllib.parse.urlencode({"sendEligibility": "true", "service": jwt.get("service"),
                                     "year": jwt.get("batch"), "season": jwt.get("season")})
        cache = state.get("jobDetails", {})
        details, open_jobs = {}, []
        for j in api(f"/api/v1/job?{qs}", token):
            opens, closes = parse_iso(j.get("opensAt")), parse_iso(j.get("closesAt"))
            if j.get("archivedOn") or not opens or not closes or opens > now() or closes <= now():
                continue
            d = cache.get(str(j["id"]))
            if not d or "rounds" not in d or now().timestamp() - d.get("fetchedAt", 0) > DETAIL_TTL:
                d = {**job_detail(api(f"/api/v1/job/{j['id']}", token), me["programId"]), "fetchedAt": now().timestamp()}
            details[str(j["id"])] = d
            open_jobs.append({**d, "id": j["id"], "company": (j.get("company") or {}).get("name") or "Unknown company",
                              "code": j.get("code"), "title": (j.get("title") or "").strip(),
                              "opensAt": j.get("opensAt"), "closesAt": j.get("closesAt"),
                              "eligible": bool(j.get("eligible")), "bonus": bool(j.get("allowBonusApplications")),
                              "signed": str(j["id"]) in jobs})
    except AuthError:
        need_login(state, "Your placement portal session has ended.")
        return state

    # Blog (a hiccup isn't a logout: keep old posts, retry next time, warn after an hour)
    first_run = not state.get("lastSync")
    jar = cookie_jar(session.get("cookies", []))
    # Re-read every page (not just new posts) on the first run or when the post text format changed.
    full = now().timestamp() - state.get("fullReadAt", 0) > FULL_READ  # once a day: every page, edits and removals
    result = read_blog(state, me, jar, first_run or state.get("postFormat") != POST_FORMAT, full)
    session["cookies"] = jar_cookies(jar)  # keep any refreshed cookies
    save(SESSION, session, private=True)
    blog = result if "known" in result else None
    if blog:
        state["postFormat"] = POST_FORMAT
        if full:
            state["fullReadAt"] = now().timestamp()
    edited = blog["edited"] if blog else []
    for old, new in edited:  # a link added later (a shortlist sheet): read the post's lists again
        if re.findall(r"https?://\S+", old["text"]) != re.findall(r"https?://\S+", new["text"]):
            (state.get("lists") or {}).pop(new["id"], None)
    known = blog["known"] if blog else {p["id"]: p for p in state.get("posts", [])}
    fresh = blog["fresh"] if blog else []
    failing_since = None if blog else (state.get("blogFailingSince") or now().timestamp())
    blog_warn = bool(failing_since) and now().timestamp() - failing_since > BLOG_WARN
    if result.get("ssoLoggedOut") and not state.get("ssoAlerted"):
        send_digest("🔑 *IITB SSO login needed*: open the placement blog in Chrome on your laptop and log in with "
                    "SSO (Placement). JAFs and reminders keep updating meanwhile.", state=state)
    if blog_warn and not state.get("blogWarned"):
        send_digest("⚠️ Couldn't read the placement blog for about an hour. JAFs and reminders still update; "
                    "it keeps retrying.", state=state)

    all_posts = sorted(known.values(), key=lambda p: p.get("date") or "", reverse=True)
    try:
        check_lists(state, all_posts, jobs, {p["id"] for p in fresh}, notify=not first_run)
    except Exception as e:  # noqa: BLE001 - a bonus; never break the sync
        print(f"lists: {e}", file=sys.stderr)
    try:  # the AI's second reading, before times are worked out (events, reminders, website)
        ai_read_posts(state, relevant_posts(all_posts, active_jobs(jobs)) + ppt_posts(all_posts, list(jobs.values())), jobs)
    except Exception as e:  # noqa: BLE001 - a second opinion; never break the sync
        print(f"ai reads: {e}", file=sys.stderr)
    attach_ai(state, all_posts)
    events = extract_events(sorted(relevant_posts(all_posts, list(jobs.values())) + ppt_posts(all_posts, list(jobs.values())),
                                   key=lambda p: p.get("date") or "", reverse=True), jobs)
    job_list = active_jobs(jobs)  # messages skip hidden companies (posts naming you still come through)
    seen_open = set(state.get("seenOpenIds") or [])
    had_seen = "seenOpenIds" in state
    state.update({
        "me": me, "jobs": jobs, "posts": all_posts, "events": events, "openJobs": open_jobs, "jobDetails": details,
        "lastSync": iso(now()), "auth": {"ok": True}, "blogFailingSince": failing_since, "blogWarned": blog_warn,
        "blogError": None if blog else result["error"], "ssoAlerted": bool(result.get("ssoLoggedOut")),
        "error": "Couldn't read the placement blog for over an hour (still retrying). JAFs and reminders are up to date."
                 if blog_warn else None,
        "seenOpenIds": sorted(seen_open | {j["id"] for j in open_jobs}),
        "postEvents": post_event_times(relevant_posts(all_posts, list(jobs.values())) + ppt_posts(all_posts, list(jobs.values())), jobs),
        # Posts asking you to do something (website: red "Action" label): {post id: deadline or ""}
        "postActions": {p["id"]: (a["at"] or "") for p in relevant_posts(all_posts, list(jobs.values()))[:150]
                        if (a := action_of(p, post_jobs(p, jobs)))},
    })
    try:
        update_seats(state, jobs, job_list, notify=not first_run, fresh_ids={p["id"] for p in fresh})
    except Exception as e:  # noqa: BLE001 - seats are a bonus; never break the sync
        print(f"seats: {e}", file=sys.stderr)

    # Messages about what's new
    new_posts = sorted(relevant_posts(fresh, job_list) + ppt_posts(fresh, list(jobs.values())), key=lambda p: not p.get("mentionsMe"))
    # Things you must do (fill a form, register…) go first, on their own, even in quiet hours when due within 12 h.
    acts = {p["id"]: action_of(p, post_jobs(p, jobs)) for p in new_posts}
    act_posts = [p for p in new_posts if acts[p["id"]] and (not acts[p["id"]]["at"] or parse_iso(acts[p["id"]]["at"]) > now())]
    if not first_run and act_posts:
        act_lines = [action_line(p, acts[p["id"]], company_of(p, jobs)) + ai_note(a=acts[p["id"]]) for p in act_posts]
        soon = any(acts[p["id"]]["at"] and parse_iso(acts[p["id"]]["at"]) - now() < timedelta(hours=12) for p in act_posts)
        send_digest("\n\n".join(act_lines), [full_post(p, line) for p, line in zip(act_posts, act_lines)],
                    state=state, urgent=soon)
        # The reminders for the window we're already in would repeat this message: count them as sent.
        for p in act_posts:
            mark_sent(state, p["id"], acts[p["id"]]["at"], ACTION_HOURS)
    new_posts = [p for p in new_posts if p not in act_posts]
    lines = [describe_post(p, jobs, post_seat(state, p)) for p in new_posts]
    new_open = [j for j in open_jobs if had_seen and j["id"] not in seen_open and not j["signed"] and (j["eligible"] or j["bonus"])]
    open_lines = [f"🆕 New JAF you can sign{'' if j['eligible'] else ' (bonus)'}: *{short_name(j['company'])}* – "
                  f"{j['title']} · {money(j['ctc'], j['currency'])} · closes {when(j['closesAt'])}" for j in new_open]
    if not first_run and (lines or open_lines):
        open_items = [ai_summary(j, line, state) for j, line in zip(new_open, open_lines)]
        send_digest("*IITB placement updates*\n\n" + "\n\n".join(lines + open_lines),
                    [full_post(p, l) for p, l in zip(new_posts, lines)] + open_items, state=state)

    # Posts about your companies that were edited or taken down
    olds = {new["id"]: old for old, new in edited}
    news = [new for _, new in edited]
    changed = {p["id"]: p for p in relevant_posts(news, job_list) + ppt_posts(news, list(jobs.values()))}.values()
    gone = blog["gone"] if blog else []
    gone_mine = {p["id"]: p for p in relevant_posts(gone, job_list) + ppt_posts(gone, list(jobs.values()))}.values()
    if not first_run and (changed or gone_mine):
        edits = [edit_line(olds[p["id"]], p, jobs) for p in changed]
        for p, (_, at, hours) in zip(changed, edits):
            if at:
                mark_sent(state, p["id"], at, hours)
        lines = [line for line, _, _ in edits] + [f"🗑️ *{company_of(p, jobs)}*: post taken down from the blog: {p['title']}"
                                                   for p in gone_mine]
        soon = any(at and parse_iso(at) - now() < timedelta(hours=12) for _, at, _ in edits)
        send_digest("\n\n".join(lines), [full_post(p, line) for p, (line, _, _) in zip(changed, edits)]
                    + lines[len(edits):], state=state, urgent=soon)
    return state


def mark_sent(state, post_id, at, hours):
    """A message that already says a time: the reminders for the window we're in would repeat it."""
    if at and parse_iso(at) > now():
        left = (parse_iso(at) - now()).total_seconds() / 3600
        sent = state.setdefault("remindersSent", [])
        sent.extend(k for h in hours if h >= left and (k := f"ev:{post_id}:{at}:{h}") not in sent)


def edit_line(old, new, jobs):
    """(message, new time it gives or None, reminder hours) for a post about your company that was edited."""
    company, mine = company_of(new, jobs), post_jobs(new, jobs)
    a, b = extract_event(old, company, mine), extract_event(new, company, mine)
    x, y = action_of(old, mine), action_of(new, mine)
    out, at, hours = [], None, REMIND_HOURS
    if old["title"] != new["title"]:
        out.append(f"Title: {old['title']} → *{new['title']}*")
    if b and (not a or (a["at"], a["venue"]) != (b["at"], b["venue"])):
        was = f" (was {when(a['at'])}{' · ' + a['venue'] if a['venue'] else ''})" if a else ""
        out.append(f"{b['icon']} {b['kind']}: *{when(b['at'])}*{where(b)}{was}")
        at = b["at"]
    elif a and not b:
        out.append(f"The {a['kind'].lower()} time ({when(a['at'])}) isn't in the post any more: check it")
    if y and (not x or x["at"] != y["at"]):
        out.append(action_line(new, y, company))
        at, hours = (y["at"], ACTION_HOURS) if y["at"] else (at, hours)
    if set(re.findall(r"https?://\S+", new["text"])) - set(re.findall(r"https?://\S+", old["text"])):
        out.append("New link in the post")
    if not out:
        out.append("Details changed: see the post")
    return f"✏️ *{company}* post updated: {new['title']}\n" + "\n".join(out) + ai_note(b, y), at, hours


def check_reminders(state):
    """24 h, 12 h and 4 h before tests, PPTs, unsigned JAF deadlines and the portal login expiring."""
    sent = list(state.get("remindersSent") or [])
    sent_set = set(sent)
    due, urgent = [], []
    _, quiet_end = quiet_window()
    quiet = in_quiet()

    def consider(key, at, line, hours=REMIND_HOURS):
        left = (parse_iso(at) - now()).total_seconds() / 3600
        h = next((x for x in sorted(hours) if 0 < left <= x), None)  # tightest window we're in
        if h is None or f"{key}:{h}" in sent_set:
            return
        is_urgent = parse_iso(at) < quiet_end + timedelta(hours=1)  # due before you'd see the morning messages
        if quiet and not is_urgent:
            return  # wait: after quiet hours it's written fresh, with the right countdown
        for x in hours:
            if x >= h and f"{key}:{x}" not in sent_set:
                sent_set.add(f"{key}:{x}")
                sent.append(f"{key}:{x}")
        (urgent if is_urgent else due).append(line)

    for e in visible_events(state.get("events") or []):
        # Keyed by blog post: when a company posts again about a test, its reminders start again.
        if e.get("what"):  # a form / registration you must do
            consider(f"ev:{e['id']}:{e['at']}", e["at"], f"🚨 *{e['company']}*: due {until(e['at'])} ({when(e['at'])})"
                     + (" (🤖 read by AI)" if e.get("ai") else "") + f"\n{e['what']}\n{e['link']}", ACTION_HOURS)
            continue
        consider(f"ev:{e['id']}:{e['at']}", e["at"],
                 f"⏰ *{e['company']}*: {e['kind']} {until(e['at'])} ({when(e['at'])})" + where(e)
                 + (" (🤖 read by AI: check the post)" if e.get("ai") else ""))
    for j in state.get("openJobs") or []:
        if j["signed"] or not (j["eligible"] or j["bonus"]):
            continue
        consider(f"jaf:{j['id']}:{j['closesAt']}", j["closesAt"],
                 f"⏰ *{short_name(j['company'])}* JAF closes {until(j['closesAt'])} ({when(j['closesAt'])}): "
                 f"{j['title']} · {money(j['ctc'], j['currency'])} · not signed yet")
    jwt = decode_jwt(load(SESSION, {}).get("token", ""))
    if jwt and jwt.get("exp"):
        exp = iso(datetime.fromtimestamp(jwt["exp"], timezone.utc))
        consider(f"login:{jwt['exp']}", exp, f"🔑 Your placement portal login expires {until(exp)} ({when(exp)}). "
                 "Log in again in Chrome on your laptop to keep updates coming.")
    state["remindersSent"] = sent[-1000:]
    if urgent:
        send_digest("*Placement reminders*\n\n" + "\n\n".join(urgent), urgent, state=state, urgent=True)
    if due:
        send_digest("*Placement reminders*\n\n" + "\n\n".join(due), due, state=state)


def clock(s):
    return parse_iso(s).astimezone(IST).strftime("%-I:%M %p").lower()


def signable(state):
    """Open JAFs you can still sign (eligible, or bonus allowed), soonest deadline first."""
    return sorted((j for j in state.get("openJobs") or [] if not j["signed"] and (j["eligible"] or j["bonus"])
                   and parse_iso(j["closesAt"]) > now()), key=lambda j: j["closesAt"])


def today_text(state, title=None):
    """Today's tests/PPTs, JAFs closing today you haven't signed, and the login expiry if it's soon."""
    local = now().astimezone(IST)
    end = local.replace(hour=23, minute=59, second=59)
    events = [e for e in visible_events(state.get("events") or []) if now() < parse_iso(e["at"]) <= end]
    closing = [j for j in signable(state) if parse_iso(j["closesAt"]) <= end]
    lines = [title or f"📅 *Today, {local.strftime('%a %-d %b')}*"]
    if events:
        lines.append("*Tests, PPTs & deadlines*\n" + "\n".join(
            f"{e['icon']} {clock(e['at'])} · *{e['company']}*: {e['kind']}" + where(e)
            for e in events))
    if closing:
        lines.append("*JAFs closing today (not signed)*\n" + "\n".join(
            f"⏰ {clock(j['closesAt'])} · *{short_name(j['company'])}*: {j['title']} · {money(j['ctc'], j['currency'])}"
            for j in closing))
    jwt = decode_jwt(load(SESSION, {}).get("token", ""))
    if jwt and jwt.get("exp") and jwt["exp"] - now().timestamp() < 36 * 3600:
        lines.append(f"🔑 Portal login expires {when(iso(datetime.fromtimestamp(jwt['exp'], timezone.utc)))}: "
                     "log in on your laptop.")
    if len(lines) == 1:
        lines.append("Nothing left today. 🎉")
    return "\n\n".join(lines)


def open_text(state, limit=20):
    jafs = signable(state)
    if not jafs:
        return "📋 No open JAFs you can sign right now."
    lines = [f"📋 *JAFs you can still sign* ({len(jafs)})"]
    for j in jafs[:limit]:
        tag = "" if j["eligible"] else " (bonus)"
        lines.append(f"• *{short_name(j['company'])}*{tag}: {j['title']}\n  {money(j['ctc'], j['currency'])} · closes {when(j['closesAt'])} ({until(j['closesAt'])})")
    if len(jafs) > limit:
        lines.append(f"…and {len(jafs) - limit} more on the website.")
    return "\n".join(lines)


def ai_summary(job, fallback, state):
    """Claude's summary of a JAF for WhatsApp, or the plain one-liner if AI is off, failing or over budget."""
    try:
        import jaf_ai  # needs the anthropic SDK (in ~/jaf-sync/wa-venv)
    except ImportError:
        return fallback
    if not jaf_ai.settings()["summaries"]:
        return fallback
    try:
        return jaf_ai.summarize_jaf(job)
    except jaf_ai.BudgetExceeded as e:
        if state.get("aiBudgetAlert") != jaf_ai.month():  # say so once a month
            state["aiBudgetAlert"] = jaf_ai.month()
            return f"{fallback}\n\n🔒 {e}: AI summaries pause until the 1st (plain messages meanwhile)."
        return fallback
    except jaf_ai.AIUnavailable as e:
        state["aiError"] = str(e)
        return fallback


def ai_usage_line():
    try:
        import jaf_ai
        return jaf_ai.usage_text()
    except ImportError:
        return "AI: not installed"


def status_text(state):
    jwt = decode_jwt(load(SESSION, {}).get("token", ""))
    last = parse_iso(state.get("lastSync") or "")
    auth = state.get("auth") or {}
    hidden = sorted(hidden_companies())
    lines = ["🩺 *Tracker status*",
             f"Last sync: {when(state['lastSync']) if last else 'never'}" + (f" ({round((now() - last).total_seconds() / 60)} min ago)" if last else ""),
             "Portal login: " + ("OK" if auth.get("ok") else f"needs login ({auth.get('msg', '')})")
             + (f", expires {when(iso(datetime.fromtimestamp(jwt['exp'], timezone.utc)))}" if jwt and jwt.get("exp") else ""),
             "Blog: " + ("OK" if not state.get("blogError") else state["blogError"]),
             f"Your JAFs: {len(state.get('jobs') or {})} · open JAFs: {len(state.get('openJobs') or [])} · posts: {len(state.get('posts') or [])}",
             "Hidden companies: " + (", ".join(short_name(h) for h in hidden) if hidden else "none"),
             ai_usage_line()]
    if state.get("error"):
        lines.append(f"⚠️ {state['error']}")
    return "\n".join(lines)


def set_hidden(query, hide):
    """Hide/unhide one of your companies by (part of) its name. Returns the reply text."""
    jobs = load(STATE, {}).get("jobs") or {}
    state = load(STATE, {})
    # Your signed companies, plus companies whose PPTs you're reminded of
    names = sorted({j["company"] for j in jobs.values()} | {e["company"] for e in state.get("events") or [] if e["kind"] == "PPT"})
    q = query.strip().lower()
    matches = [n for n in names if q and (q in n.lower() or q in short_name(n).lower())]
    if not q or not matches:
        return "Which company? Your companies: " + ", ".join(short_name(n) for n in names)
    if len(matches) > 1 and not any(short_name(m).lower() == q for m in matches):
        return "More than one match: " + ", ".join(short_name(m) for m in matches) + ". Be more specific."
    name = next((m for m in matches if short_name(m).lower() == q), matches[0])
    hidden = hidden_companies()
    if hide:
        hidden.add(name)
    else:
        hidden.discard(name)
    save(HIDDEN, sorted(hidden), private=True)
    return (f"🙈 Hidden *{short_name(name)}*: no more updates or reminders for it (posts naming you still come through)."
            if hide else f"👀 *{short_name(name)}* is visible again.")


def catchup_parts(state):
    """(Telegram summary, WhatsApp messages, number of items) for the catch-up."""
    jobs = state.get("jobs") or {}
    t = now()
    events = [e for e in visible_events(state.get("events") or []) if timedelta(0) < parse_iso(e["at"]) - t < timedelta(hours=48)]
    closing = sorted((j for j in state.get("openJobs") or [] if not j["signed"] and (j["eligible"] or j["bonus"])
                      and parse_iso(j["closesAt"]) - t < timedelta(hours=24)), key=lambda j: j["closesAt"])
    recent = [p for p in relevant_posts(state.get("posts") or [], active_jobs(jobs))
              if p.get("date") and t - parse_iso(p["date"]) < timedelta(hours=24)]

    def section(title, lines):
        return f"*{title}*\n" + "\n".join(lines) if lines else ""

    text = "\n\n".join(filter(None, [
        "*IITB placement catch-up*",
        section("Coming up (next 48 h)", [f"{e['icon']} *{e['company']}*: {e['kind']} {when(e['at'])}" + where(e) for e in events]),
        section("JAFs you can still sign, closing within 24 h", [f"⏰ *{short_name(j['company'])}* closes {until(j['closesAt'])} ({when(j['closesAt'])}): {j['title']} · {money(j['ctc'], j['currency'])}" for j in closing]),
        section("Updates in the last 24 h", [describe_post(p, jobs, post_seat(state, p)) for p in recent]),
    ]))
    count = len(events) + len(closing) + len(recent)
    if not count:
        none = "*IITB placement catch-up*\n\nNothing coming up in the next 48 h. 🎉"
        return none, [none], 0
    # WhatsApp: the upcoming / closing summary as one message, then each recent update on its own.
    summary = text.split("\n\n*Updates in the last 24 h*")[0]
    items = ([summary] if events or closing else []) + [full_post(p, describe_post(p, jobs, post_seat(state, p))) for p in recent]
    return text, items, count


def catchup(state):
    text, items, count = catchup_parts(state)
    send_digest(text, items, state=state, urgent=True)  # you asked for it, so it goes out even in quiet hours
    return count


def morning_plan(state):
    """Once a day at config.json → morning (default 8, IST): today's tests/PPTs, JAFs closing today, login expiry."""
    cfg = load(CONFIG, {})
    local = now().astimezone(IST)
    today = local.date().isoformat()
    if local.hour < int(cfg.get("morning", 8)) or state.get("lastMorning") == today or in_quiet():
        return
    state["lastMorning"] = today
    text = today_text(state, f"☀️ *Good morning! Your placement day, {local.strftime('%a %-d %b')}*")
    send_digest(text, [text], state=state)


def evening_plan(state):
    """Once a day at config.json → evening (default 20 = 8 pm, IST): every deadline still ahead today,
    plus anything due early tomorrow (before 10 am), since quiet hours start at midnight."""
    cfg = load(CONFIG, {})
    local = now().astimezone(IST)
    today = local.date().isoformat()
    if local.hour < int(cfg.get("evening", 20)) or state.get("lastEvening") == today or in_quiet():
        return
    state["lastEvening"] = today
    tonight = local.replace(hour=23, minute=59, second=59)
    early = (local + timedelta(days=1)).replace(hour=10, minute=0, second=0)
    def items(until_dt):
        events = [(e["at"], f"{e['icon']} {clock(e['at'])} · *{e['company']}*: {e['kind']}" + where(e))
                  for e in visible_events(state.get("events") or []) if now() < parse_iso(e["at"]) <= until_dt]
        jafs = [(j["closesAt"], f"⏰ {clock(j['closesAt'])} · *{short_name(j['company'])}* JAF closes: {j['title']} · "
                 f"{money(j['ctc'], j['currency'])} · not signed yet")
                for j in state.get("openJobs") or [] if not j["signed"] and (j["eligible"] or j["bonus"])
                and now() < parse_iso(j["closesAt"]) <= until_dt]
        return [line for _, line in sorted(events + jafs)]

    lines = [f"🌙 *Today's remaining deadlines, {local.strftime('%a %-d %b')}*"]
    tonight_items = items(tonight)
    lines.append("*Tonight*\n" + "\n".join(tonight_items) if tonight_items else "Nothing left for tonight. 🎉")
    early_items = [i for i in items(early) if i not in tonight_items]
    if early_items:
        lines.append("*Early tomorrow (before 10 am)*\n" + "\n".join(early_items))
    text = "\n\n".join(lines)
    send_digest(text, [text], state=state)


def ping_health(ok, detail=""):
    """Tells healthchecks.io the tracker is alive (config.json → healthcheck); it alerts you if pings stop."""
    url = load(CONFIG, {}).get("healthcheck")
    if not url or not url.startswith("https://"):
        return
    try:
        req = urllib.request.Request(url if ok else url.rstrip("/") + "/fail", data=detail.encode()[:1000],
                                     headers={"User-Agent": "jaf-tracker"}, method="POST")
        urllib.request.urlopen(req, timeout=15).close()
    except Exception:  # noqa: BLE001 - monitoring must never break the sync
        pass


def write_web(state):
    save(WEB_DATA, {k: state.get(k) for k in ("me", "jobs", "posts", "events", "openJobs", "lastSync", "auth", "error",
                                              "ssoAlerted", "postEvents", "postSeats", "postActions")})


def main():
    DIR.mkdir(exist_ok=True)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd == "set-gemini-key":  # typed in, never shown, stored only in config.json (0600)
        key = getpass.getpass("Gemini API key from aistudio.google.com (input hidden): ").strip()
        if len(key) < 20:
            sys.exit("That doesn't look like a Gemini API key.")
        cfg = load(CONFIG, {})
        cfg.setdefault("gemini", {}).update({"apiKey": key})
        cfg.setdefault("ai", {}).update({"provider": "gemini"})
        cfg["ai"].setdefault("monthlyLimitInr", 250)
        save(CONFIG, cfg, private=True)
        print("Saved. AI now uses Gemini (free tier), with a ₹250 monthly cap if billing is ever turned on.")
        return
    if cmd == "set-openrouter-key":  # typed in, never shown, stored only in config.json (0600)
        key = getpass.getpass("OpenRouter API key from openrouter.ai/keys (input hidden): ").strip()
        if not key.startswith("sk-or-"):
            sys.exit("That doesn't look like an OpenRouter API key (it starts with sk-or-).")
        cfg = load(CONFIG, {})
        cfg.setdefault("openrouter", {}).update({"apiKey": key})
        cfg.setdefault("ai", {}).update({"provider": "openrouter"})
        cfg["ai"].setdefault("monthlyLimitInr", 250)
        save(CONFIG, cfg, private=True)
        print("Saved. AI now uses OpenRouter (anthropic/claude-haiku-5.5), with a ₹250 monthly limit.")
        return
    if cmd == "set-anthropic-key":  # typed in, never shown, stored only in config.json (0600)
        key = getpass.getpass("Anthropic API key (input hidden): ").strip()
        if not key.startswith("sk-ant-"):
            sys.exit("That doesn't look like an Anthropic API key (it starts with sk-ant-).")
        cfg = load(CONFIG, {})
        cfg.setdefault("anthropic", {}).update({"apiKey": key, "model": "claude-sonnet-5-5"})
        cfg.setdefault("ai", {}).update({"provider": "claude"})
        cfg["ai"].setdefault("monthlyLimitInr", 250)
        save(CONFIG, cfg, private=True)
        print("Saved. AI now uses Claude, with a ₹250 monthly limit.")
        return
    with open(DIR / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # one run at a time
        if cmd == "sync":
            try:
                state = sync()
            except Exception as e:  # noqa: BLE001 - record and keep the timer running
                state = load(STATE, {})
                state["error"] = f"Last sync failed: {e}"
            check_reminders(state)
            flush_outbox(state)
            morning_plan(state)
            evening_plan(state)
            save(STATE, state)
            write_web(state)
            ping_health(not state.get("error") and (state.get("auth") or {}).get("ok", False),
                        f"auth={state.get('auth')} error={state.get('error')}")
            print(f"synced: auth={state.get('auth')} jobs={len(state.get('jobs') or {})} "
                  f"open={len(state.get('openJobs') or [])} posts={len(state.get('posts') or [])} "
                  f"blogError={state.get('blogError')} error={state.get('error')}")
        elif cmd == "ai":
            print(ai_usage_line())
        elif cmd == "summarize-open":  # AI summaries of every JAF you can sign right now
            state = load(STATE, {})
            args = sys.argv[2:]
            limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
            jafs = signable(state)[:limit]
            items = [ai_summary(j, f"🆕 *{short_name(j['company'])}*: {j['title']}", state) for j in jafs]
            if "--dry-run" in args:
                print("\n\n---\n\n".join(items))
            else:
                send_digest(f"*JAFs you can sign* ({len(items)})", items, state=state, urgent=True)
                print(f"sent {len(items)} summaries")
            save(STATE, state)
        elif cmd == "morning":  # send today's plan now (for testing)
            state = load(STATE, {})
            state.pop("lastMorning", None)
            morning_plan(state)
            save(STATE, state)
        elif cmd == "evening":  # send tonight's deadlines now (for testing)
            state = load(STATE, {})
            state.pop("lastEvening", None)
            evening_plan(state)
            save(STATE, state)
        elif cmd == "catchup":
            state = load(STATE, {})
            print(f"catch-up sent: {catchup(state)} items")
            save(STATE, state)
        elif cmd == "status":
            state = load(STATE, {})
            jwt = decode_jwt(load(SESSION, {}).get("token", ""))
            exp = datetime.fromtimestamp(jwt["exp"], IST).strftime("%a %-d %b %-I:%M %p") if jwt else "no login"
            print(f"last sync: {state.get('lastSync')}  auth: {state.get('auth')}  login expires: {exp}\n"
                  f"jobs: {len(state.get('jobs') or {})}  open: {len(state.get('openJobs') or [])}  "
                  f"posts: {len(state.get('posts') or [])}  blogError: {state.get('blogError')}  error: {state.get('error')}")
        else:
            sys.exit(__doc__)


if __name__ == "__main__":
    main()
