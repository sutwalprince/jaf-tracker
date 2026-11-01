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
                     | summarize-open [--dry-run] [--limit N] | set-gemini-key | set-anthropic-key]
"""
import fcntl
import getpass
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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
WEB_DATA = HOME / "jaf-web-data.json"

ORIGIN = "https://campus.placements.iitb.ac.in"
BLOG_HOME = f"{ORIGIN}/blog/"
BLOG_URL = f"{ORIGIN}/blog/placement/"
BLOG_LOGIN = f"{ORIGIN}/blog/placement/authplacement/"
IST = timezone(timedelta(hours=5, minutes=30))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
MAX_PAGES = 40
TEXT_LIMIT = 8000
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
    tmp = path.with_suffix(".tmp")
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


def match_jobs(post, jobs):
    head = f"{post['title']} {post['text'][:300]}"
    return [j["id"] for j in jobs if any(p.search(head) for p in company_patterns(j["company"]))]


def relevant_posts(posts, jobs):
    out = []
    for p in posts:
        ids = match_jobs(p, jobs)
        if ids or p.get("mentionsMe"):
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


def company_of(post, jobs):
    j = jobs.get(str(post["jobIds"][0])) if post.get("jobIds") else None
    return short_name(j["company"]) if j else post["title"].split("|")[0].strip()


# ---------- blog parsing ----------

def html_to_text(s):
    s = re.sub(r"<(script|style)[\s\S]*?</\1>", "", s, flags=re.I)
    s = re.sub(r"<br\s*/?>|</(p|li|tr|h\d|div|ol|ul|table)>", "\n", s, flags=re.I)
    s = re.sub(r"</t[dh]>", "\t", s, flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    lines = (re.sub(r"[ \t ]+", " ", line).strip() for line in s.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def mention_test(me):
    pats = []
    if me.get("roll"):
        pats.append(re.compile(rf"\b{re.escape(me['roll'])}\b", re.I))
    name = (me.get("name") or "").strip()
    if " " in name:
        pats.append(re.compile(r"\s+".join(map(re.escape, name.split())), re.I))
    return lambda text: any(p.search(text) for p in pats)


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
        text = html_to_text(body.group(1) if body else "")
        posts.append({
            "id": pid.group(1),
            "date": date.group(1) if date else None,
            "link": html.unescape(title.group(1)) if title else BLOG_URL,
            "title": html_to_text(title.group(2)) if title else "(untitled)",
            "text": text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + "\n…",
            "mentionsMe": mentions(text),
        })
    return posts


# ---------- events (tests, PPTs, interviews) read from posts ----------

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MON = "|".join(MONTHS)
DATE_DM = re.compile(rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MON})[a-z]*\.?,?\s*(?:\(\w+\)\s*,?\s*)?(\d{{4}})?", re.I)
DATE_MD = re.compile(rf"\b({MON})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b,?\s*(\d{{4}})?", re.I)
TIME = re.compile(r"(\d{1,2})(?:[:.](\d{2}))?\s*([ap])\.?\s*m\b", re.I)
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
    return None


def find_time(s):
    m = TIME.search(s)
    if not m:
        return None
    return int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0), int(m.group(2) or 0)


def extract_event(post, company):
    kind = next((k for k in KINDS if k[0].search(post["title"])), None)
    if not kind:
        return None

    def seg(label):
        m = re.search(rf"\b{label}\s*[:\-–]\s*([^\n]{{0,90}})", post["text"], re.I)
        return m.group(1) if m else ""

    year = (parse_iso(post.get("date") or "") or now()).year
    date = find_date(seg("date") or seg("deadline"), year) or find_date(post["text"], year)
    time = find_time(seg("updated time") or seg("time") or seg("deadline")) or find_time(post["text"])
    if not date or not time:
        return None
    try:
        at = datetime(date[2], date[1] + 1, date[0], time[0], time[1], tzinfo=IST)
    except ValueError:
        return None
    venue = re.sub(r"\s+", " ", seg("venue")).strip()[:60]
    return {"id": post["id"], "company": company, "kind": kind[1], "icon": kind[2], "at": iso(at),
            "venue": venue, "title": post["title"], "link": post["link"]}


def extract_events(posts, jobs):
    seen = {}
    for p in posts:  # newest first: an "update" post replaces the earlier announcement
        e = extract_event(p, company_of(p, jobs))
        if e and (e["company"], e["kind"]) not in seen:
            seen[(e["company"], e["kind"])] = e
    return sorted(seen.values(), key=lambda e: e["at"])


def describe_post(p, jobs):
    """One short line built from the title and event time."""
    company = company_of(p, jobs)
    if p.get("mentionsMe"):
        if re.search(r"shortlist", p["title"], re.I):
            return f"🎉 You're on the *{company}* shortlist! ({p['title']})"
        return f"🎉 Your name is in: *{p['title']}*"
    e = extract_event(p, company)
    if e and parse_iso(e["at"]) > now():
        return f"{e['icon']} *{company}*: {e['kind']} {when(e['at'])}" + (f" · {e['venue']}" if e["venue"] else "")
    rest = "|".join(p["title"].split("|")[1:]).strip()
    return f"📢 *{company}*: {rest or p['title']}"


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


def read_blog(state, me, jar, first_run):
    """Returns {known, fresh} or {error, ssoLoggedOut?}."""
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
    fresh = []
    for page in range(1, MAX_PAGES + 1):
        res = first if page == 1 else get(f"{BLOG_URL}?paged={page}")
        if res[0] == 404 or not is_blog(res):
            break
        posts = parse_blog(res[2], me)
        for p in posts:
            if p["id"] not in known:
                fresh.append(p)
            known[p["id"]] = p
        if not first_run and posts and posts[-1]["id"] in old_ids:
            break
    return {"known": known, "fresh": fresh}


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
    result = read_blog(state, me, jar, first_run)
    session["cookies"] = jar_cookies(jar)  # keep any refreshed cookies
    save(SESSION, session, private=True)
    blog = result if "known" in result else None
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
    events = extract_events(relevant_posts(all_posts, list(jobs.values())), jobs)
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
    })

    # Messages about what's new
    new_posts = sorted(relevant_posts(fresh, job_list), key=lambda p: not p.get("mentionsMe"))
    lines = [describe_post(p, jobs) for p in new_posts]
    new_open = [j for j in open_jobs if had_seen and j["id"] not in seen_open and not j["signed"] and (j["eligible"] or j["bonus"])]
    open_lines = [f"🆕 New JAF you can sign{'' if j['eligible'] else ' (bonus)'}: *{short_name(j['company'])}* – "
                  f"{j['title']} · {money(j['ctc'], j['currency'])} · closes {when(j['closesAt'])}" for j in new_open]
    if not first_run and (lines or open_lines):
        open_items = [ai_summary(j, line, state) for j, line in zip(new_open, open_lines)]
        send_digest("*IITB placement updates*\n\n" + "\n\n".join(lines + open_lines),
                    [full_post(p, l) for p, l in zip(new_posts, lines)] + open_items, state=state)
    return state


def check_reminders(state):
    """24 h, 12 h and 4 h before tests, PPTs, unsigned JAF deadlines and the portal login expiring."""
    sent = list(state.get("remindersSent") or [])
    sent_set = set(sent)
    due, urgent = [], []
    _, quiet_end = quiet_window()
    quiet = in_quiet()

    def consider(key, at, line):
        left = (parse_iso(at) - now()).total_seconds() / 3600
        h = next((x for x in REMIND_HOURS if 0 < left <= x), None)  # tightest window we're in
        if h is None or f"{key}:{h}" in sent_set:
            return
        is_urgent = parse_iso(at) < quiet_end + timedelta(hours=1)  # due before you'd see the morning messages
        if quiet and not is_urgent:
            return  # wait: after quiet hours it's written fresh, with the right countdown
        for x in REMIND_HOURS:
            if x >= h and f"{key}:{x}" not in sent_set:
                sent_set.add(f"{key}:{x}")
                sent.append(f"{key}:{x}")
        (urgent if is_urgent else due).append(line)

    for e in visible_events(state.get("events") or []):
        # Keyed by blog post: when a company posts again about a test, its reminders start again.
        consider(f"ev:{e['id']}:{e['at']}", e["at"],
                 f"⏰ *{e['company']}*: {e['kind']} {until(e['at'])} ({when(e['at'])})" + (f" · {e['venue']}" if e["venue"] else ""))
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
            f"{e['icon']} {clock(e['at'])} · *{e['company']}*: {e['kind']}" + (f" · {e['venue']}" if e["venue"] else "")
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
    names = sorted({j["company"] for j in jobs.values()})
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
        section("Coming up (next 48 h)", [f"{e['icon']} *{e['company']}*: {e['kind']} {when(e['at'])}" + (f" · {e['venue']}" if e["venue"] else "") for e in events]),
        section("JAFs you can still sign, closing within 24 h", [f"⏰ *{short_name(j['company'])}* closes {until(j['closesAt'])} ({when(j['closesAt'])}): {j['title']} · {money(j['ctc'], j['currency'])}" for j in closing]),
        section("Updates in the last 24 h", [describe_post(p, jobs) for p in recent]),
    ]))
    count = len(events) + len(closing) + len(recent)
    if not count:
        none = "*IITB placement catch-up*\n\nNothing coming up in the next 48 h. 🎉"
        return none, [none], 0
    # WhatsApp: the upcoming / closing summary as one message, then each recent update on its own.
    summary = text.split("\n\n*Updates in the last 24 h*")[0]
    items = ([summary] if events or closing else []) + [full_post(p, describe_post(p, jobs)) for p in recent]
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
        events = [(e["at"], f"{e['icon']} {clock(e['at'])} · *{e['company']}*: {e['kind']}" + (f" · {e['venue']}" if e["venue"] else ""))
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
    save(WEB_DATA, {k: state.get(k) for k in ("me", "jobs", "posts", "events", "openJobs", "lastSync", "auth", "error")})


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
