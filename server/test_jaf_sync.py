#!/usr/bin/env python3
"""Every blog post that once fooled the tracker, as a test (made-up text and roll numbers, never real students').
Runs with no network and no server files:  python3 -I server/test_jaf_sync.py   (server/deploy.sh runs it first)
Add a case here whenever a post is read wrong, before fixing it."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jaf_sync as J  # noqa: E402

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=J.IST)
J.now = lambda: NOW
J.HIDDEN = Path(tempfile.mkdtemp()) / "hidden.json"
SENT = []
J.send_digest = lambda text, items=None, state=None, urgent=False: SENT.append((text, urgent))

JOBS = {"1": {"id": 1, "company": "NVIDIA Graphics Pvt Ltd", "title": "System Software Engineer", "code": 3},
        "2": {"id": 2, "company": "Celonis", "title": "Software Engineer", "code": 1},
        "3": {"id": 3, "company": "Agratas Energy", "title": "Cathode Scientist", "code": 3},
        "4": {"id": 4, "company": "Acme Corp", "title": "Analyst", "code": 1},
        "5": {"id": 5, "company": "Qualcomm India Pvt Ltd", "title": "Engineer- HW and Perf System", "code": 8}}
ME = {"roll": "99X9999", "name": "Test Student"}


def post(pid, title, text, date="2026-11-22T10:00:00+05:30"):
    return {"id": str(pid), "date": date, "link": f"https://blog/{pid}", "title": title, "text": text, "mentionsMe": False}


def rel(p):
    return J.relevant_posts([p], list(JOBS.values()))[0]


def test_product_names_arent_companies():
    # "Google Form" / meet.google.com made posts count as Google's.
    jobs = [{"id": 9, "company": "Google"}, *JOBS.values()]
    p = post(1, "Acme | Registration", "Please fill the Google Form (https://forms.gle/x) and join on "
             "https://meet.google.com/abc by 5 pm.")
    assert J.match_jobs(p, jobs) == [4], J.match_jobs(p, jobs)


def test_two_job_codes_two_times():
    # NVIDIA: one post, Job Code 2 at 10 am and Job Code 3 (yours) at 11:30 am.
    p = rel(post(2, "NVIDIA | Test Schedule", "Date: 25th Nov 2026\n\nJob Code 2: ASIC Engineer\nTest Time: 10:00 AM\n"
                 "Venue: LH 101\n\nJob Code 3: System Software Engineer\nTest Time: 11:30 AM\nVenue: LH 302"))
    e = J.extract_event(p, "NVIDIA", J.post_jobs(p, JOBS))
    assert e["at"] == "2026-11-25T06:00:00.000Z" and e["venue"] == "LH 302", e


def test_shortlist_only_as_a_link():
    # Celonis: "Test Announcement" with "Test Shortlist: Link (sheet)", your roll not in the sheet.
    p = post(3, "Celonis | Test Announcement", "Celonis will be conducting a window test.\n\n"
             "Test Shortlist: Link (https://bighome.iitb.ac.in/s/AbC123)\n\nWindow Test Deadline: 22nd Nov 11:59 PM.")
    J.fetch_rows = lambda url: [(1, [f"99X{n:04d}" for n in range(10)])]
    state = {"me": ME}
    J.check_lists(state, [p], JOBS, notify=False)
    assert state["lists"]["3"]["inList"] is False and "Celonis" in J.hidden_companies(), state["lists"]
    J.HIDDEN.unlink()


def test_edited_post_is_reported():
    # Agratas: posted as an untitled draft with one job, edited later to the real title and seven jobs.
    old = post(4, "(untitled)", "Job Code 1: Graduate Software Engineer\n\nDeadline: Friday, 20th Nov 2026, 11:59 PM")
    new = rel(post(4, "Agratas | Test", "Job Code 3: Cathode Scientist\n\nTest on 26th Nov 2026 at 2 pm, Venue: LH 101"))
    line, at, hours = J.edit_line(old, new, JOBS)
    assert "Title: (untitled) → *Agratas | Test*" in line and "Test: *on Thu, 26 Nov at 2:00 pm* · LH 101" in line, line
    assert at == "2026-11-26T08:30:00.000Z" and hours == J.REMIND_HOURS
    # A test moved: the message says from when to when.
    a = rel(post(5, "Acme | Test", "Test on 26th Nov 2026 at 10 am"))
    b = rel(post(5, "Acme | Test", "Test on 26th Nov 2026 at 2 pm"))
    line, at, _ = J.edit_line(a, b, JOBS)
    assert "*on Thu, 26 Nov at 2:00 pm* (was on Thu, 26 Nov at 10:00 am)" in line, line


def test_unreadable_post_is_flagged():
    p = rel(post(6, "Acme | Update", "The online test is moved to 26th Nov 2026. Timings will be shared soon."))
    assert "⚠️" in J.describe_post(p, JOBS)
    # A JAF post listing its rounds isn't a test announcement.
    p = rel(post(7, "Acme | JAF Open", "Selection process: Online Test, Interview.\nDeadline: 27th Nov 2026, 11:59 PM"))
    assert "⚠️" not in J.describe_post(p, JOBS)
    # A test whose time was read gets no warning.
    p = rel(post(8, "Acme | Test", "Test on 26th Nov 2026 at 2 pm"))
    assert "⚠️" not in J.describe_post(p, JOBS)


def test_test_time_not_the_form_deadline():
    # Qualcomm: the registration deadline (5:30 PM, 22/11/2026) came first; the test was "Date & Time of Test: 9:30 PM".
    p = rel(post(10, "Qualcomm | Test Update", "Students who have filled the JAF for Qualcomm, please register on the "
                 "following link:\n\nLink: https://a.example.com/x\n\nDeadline of the Registration – 5:30 PM, 22/11/2026\n\n"
                 "Venue & Seating Arrangements: Click Here (https://bighome.iitb.ac.in/s/x)\n\n"
                 "Date & Time of Test: 9:30 PM – 11:00 PM, 22 November", date="2026-11-22T12:03:00+05:30"))
    e = J.extract_event(p, "Qualcomm", [])
    assert e["at"] == "2026-11-22T16:00:00.000Z" and e["venue"] == "", e
    assert J.action_of(p)["at"] == "2026-11-22T12:00:00.000Z", J.action_of(p)


def test_times_and_deadlines_written_oddly():
    # NVIDIA: "Time: 9: 30 AM" (a space after the colon), and a 9 am reporting time further down.
    p = rel(post(13, "Acme | Test Update", "Date: 22 November, 2026\n\nTime: 9: 30 AM to 10:30 AM\n\nVenue: LA 201\n\n"
                 "Students must report to the examination hall by 9 am, before the start time of 9:30 am.",
                 date="2026-11-21T15:09:00+05:30"))
    assert J.extract_event(p, "Acme", [])["at"] == "2026-11-22T04:00:00.000Z"
    # Bajaj: "Deadline ( 11:59 pm 1st november)": the colon inside 11:59 isn't the label's.
    p = rel(post(14, "Acme | Test Announcement", "Date: Monday, 2nd November 2026\n\nTimings: 9:00 PM – 10:00 PM\n\n"
                 "Please fill the Test preference along with the JAF\n\nWithout the response in the preference form, the "
                 "link will not be generated. Deadline ( 11:59 pm 1st november)", date="2026-11-01T11:18:00+05:30"))
    assert J.extract_event(p, "Acme", [])["at"] == "2026-11-02T15:30:00.000Z"
    assert J.action_of(p)["at"] == "2026-11-01T18:29:00.000Z", J.action_of(p)
    # Texas Instruments: "before the deadline." then "– Carefully verify…" on the next line isn't a label.
    p = rel(post(15, "Acme | Test Registration & Mock Assessment", "– Complete your registration before the deadline.\n\n"
                 "– Carefully verify all details before submission.\n\nAll candidates must complete the registration "
                 "before the deadline expires.\n\nDeadline : 6:00 PM, 31st October 2026.", date="2026-10-28T14:43:00+05:30"))
    assert J.action_of(p)["at"] == "2026-10-31T12:30:00.000Z", J.action_of(p)


def test_ai_second_reader():
    import json
    import jaf_ai
    p = rel(post(9, "Acme | Test", "The online test will be held on 26th Nov 2026.\n\nReporting time: 13:30 hrs, LH 101."))
    reply = {"event": {"kind": "Test", "date": "2026-11-26", "time": "13:30", "venue": "LH 101",
                       "quote": "Reporting time: 13:30 hrs"}, "deadline": None}
    jaf_ai.ask = lambda *a, **k: "```json\n" + json.dumps(reply) + "\n```"
    r = jaf_ai.read_post(p, J.post_jobs(p, JOBS))
    assert r["event"]["at"] == "2026-11-26T08:00:00.000Z" and r["deadline"] is None, r
    # A quote that isn't in the post, or doesn't hold the time: thrown away (the AI made it up).
    for quote in ("Reporting time: 14:30 hrs", "Report at 13:30 sharp", "held on 26th Nov 2026"):
        reply["event"]["quote"] = quote
        assert jaf_ai.read_post(p, [])["event"] is None, quote
    # The rules couldn't read "13:30 hrs": the AI's time is used (reminders, website) and marked.
    p["ai"] = r
    e = J.extract_event(p, "Acme", [])
    assert e["at"] == r["event"]["at"] and e["ai"] == "read", e
    assert "🤖 The time was read by AI from “Reporting time: 13:30 hrs”" in J.describe_post(p, JOBS)
    # The rules read 10 am; the AI 2 pm from a line about the test: the AI's time wins, and the message says so.
    q = rel(post(11, "Acme | Test", "Test on 26th Nov 2026 at 10 am. Updated: the test starts at 2 pm."))
    q["ai"] = {"event": {"kind": "Test", "at": "2026-11-26T08:30:00.000Z", "venue": "",
                         "quote": "the test starts at 2 pm"}}
    e = J.extract_event(q, "Acme", [])
    assert e["at"] == "2026-11-26T08:30:00.000Z" and e["ai"] == "fixed", e
    assert "Time corrected by AI (I'd read on Thu, 26 Nov at 10:00 am)" in J.describe_post(q, JOBS)
    # ... but from a quote that isn't clearly about the test, it's only flagged.
    q["ai"]["event"]["quote"] = "2 pm"
    e = J.extract_event(q, "Acme", [])
    assert e["at"] == "2026-11-26T04:30:00.000Z" and e["aiAt"] == "2026-11-26T08:30:00.000Z", e
    # A form whose deadline the rules couldn't read: the AI's is used.
    f = rel(post(12, "Acme | Registration", "Please fill the form (https://forms.gle/x). Last date is coming Friday."))
    f["ai"] = {"deadline": {"at": "2026-11-27T12:30:00.000Z", "what": "fill the form", "quote": "Last date is coming Friday"}}
    a = J.action_of(f, [])
    assert a["at"] == "2026-11-27T12:30:00.000Z" and a["ai"] == "read", a


class FakeBlog:
    """The blog as pages of 10 posts, newest first; `pages` is what read_blog's urllib sees."""
    def __init__(self, posts):
        self.posts = posts
        self.asked = []

    def open(self, req, timeout=None):
        url = req.full_url
        self.asked.append(url)
        page = int(url.split("paged=")[1]) if "paged=" in url else 1
        chunk = self.posts[(page - 1) * 10: page * 10]
        body = "".join(f'<article id="post-{p["id"]}"><time datetime="{p["date"]}"></time><h2 class="entry-title">'
                       f'<a href="{p["link"]}">{p["title"]}</a></h2><div class="entry-content"><p>{p["text"]}</p></div>'
                       "</article>" for p in chunk)
        status = 200 if chunk else 404

        class R:
            def __enter__(s): return s
            def __exit__(s, *a): pass
            def geturl(s): return url
            def read(s): return (body or "nothing").encode()
        r = R()
        r.status = status
        if not chunk:
            raise J.urllib.error.HTTPError(url, 404, "", {}, None)
        return r


def blog_posts(n, days_apart=0.25):
    out = []
    for i in range(n):
        d = datetime.fromtimestamp(NOW.timestamp() - (i + 1) * days_apart * 86400, J.IST).isoformat()
        out.append(post(1000 - i, f"Acme | Note {i}", f"Text {i}", date=d))
    return out


def run_blog(blog, saved, full=False):
    J.urllib.request.build_opener = lambda *a: type("O", (), {"open": blog.open, "addheaders": []})()
    return J.read_blog({"posts": saved}, ME, None, False, full)


def test_edits_two_days_back_and_daily_full_read():
    live = blog_posts(50, days_apart=0.1)       # 10 posts a day: one page a day
    saved = [dict(p) for p in live]
    live[15] = {**live[15], "text": "edited"}   # 1.6 days old, on page 2: found by a normal sync
    live[40] = {**live[40], "text": "edited"}   # 4.1 days old: only the daily full read finds it
    blog = FakeBlog(live)
    r = run_blog(blog, saved)
    assert [n["id"] for _, n in r["edited"]] == [live[15]["id"]] and len(blog.asked) == 3, (r["edited"], blog.asked)
    blog = FakeBlog(live)
    r = run_blog(blog, saved, full=True)
    assert sorted(n["id"] for _, n in r["edited"]) == sorted([live[15]["id"], live[40]["id"]]) and not r["gone"]


def test_post_flipping_between_versions_is_reported_once():
    # Microsoft's test post was "updated" 13 times in 4 hours: each version change sent a message again.
    a = blog_posts(3)
    b = [{**a[0], "text": "Venue: LA201"}, *a[1:]]
    state = {"posts": [dict(p) for p in a]}
    reported = []
    for live in (b, a, b, a):
        J.urllib.request.build_opener = lambda *x, live=live: type("O", (), {"open": FakeBlog(live).open, "addheaders": []})()
        r = J.read_blog(state, ME, None, False)
        reported += [n["id"] for _, n in r["edited"]]
        state["posts"] = list(r["known"].values())
    assert reported == [a[0]["id"]], reported
    s = {"remindersSent": []}
    J.mark_sent(s, "1", "2026-11-23T10:00:00.000Z", J.REMIND_HOURS)
    J.mark_sent(s, "1", "2026-11-23T10:00:00.000Z", J.REMIND_HOURS)
    assert len(s["remindersSent"]) == 2, s  # 6.5 h ahead: the 12 h and 24 h windows, once each


def test_room_from_venue_heading_above_your_row():
    # Amazon's PDF: "Venue: LA 202" heads each room's list; the rows only have a seat ("Seat No: G13" was shown as the room).
    head = ["Sr No", "Roll No", "Name", "Seat No"]
    rows = [(None, ["Seating Arrangement"]), (None, ["Venue: LA 201"]), (head, ["1", "99X0001", "A B", "G1"]),
            (head, ["Page 1"]), (head, ["Venue: LA 202"]), (head, ["139", "99X9999", "Test Student", "G13"])]
    assert J.my_seat(J.with_sections(rows), ME["roll"], ME["name"]) == "LA 202 · Seat No: G13", J.my_seat(J.with_sections(rows), ME["roll"], ME["name"])
    plain = [(["roll", "Room No"], ["99X9999", "LC102"])]  # Microsoft's: room in the row
    assert J.my_seat(J.with_sections(plain), ME["roll"], ME["name"]) == "LC102"


def test_posts_only_for_another_programme():
    # Amazon's MS(R)-only test (today 11 am, LCOO1) replaced an M.Tech student's own test (yesterday 2:30 pm).
    me = {**ME, "programName": "M.Tech."}
    for text in ("Amazon will conduct test for MS(R) students.\nNOTE: Test is only for MS(R) Students and test link will be shared.",
                 "The JAF is now open for MS(R) also.\nOnly MS(R) Students can apply.",
                 "This test is for B.Tech and Dual Degree students only."):
        assert J.for_other_programme(text, me), text
    for text in ("Only for M.Tech and MS(R) students.", "Only students who filled the JAF can apply.",
                 "Test for all students. Report only after 10 am.",
                 "Students MUST take the test only from designated labs/classrooms under STRICT invigilation/ proctoring.",
                 "The role is open only to eligible Dual Degree students and PG students."):
        assert not J.for_other_programme(text, me), text
    assert not J.for_other_programme("Only MS(R) Students can apply.", {**ME})  # programme unknown: keep the post


def test_ai_finds_posts_of_any_date_or_company():
    import jaf_ai
    posts = [post(1, "Acme | Test Update", "Venue: LA 202"), post(2, "Zeta Labs | PPT", "Online"),
             post(3, "Kalam Labs | JAF Open", "CTC 20 LPA", date="2026-10-01T10:00:00+05:30")]
    assert [p["id"] for p in jaf_ai.search_posts("when was the kalam labs JAF?", posts)] == ["3"]
    assert jaf_ai.search_posts("what is up", posts) == []


def test_taken_down_posts_but_never_old_ones():
    live = blog_posts(30)
    saved = [dict(p) for p in live] + blog_posts(60)[30:]  # 30 older posts beyond the pages the blog still shows
    del live[3]                                             # taken down
    r = run_blog(FakeBlog(live), saved, full=True)
    assert [p["id"] for p in r["gone"]] == ["997"] and len(r["known"]) == 59, (r["gone"], len(r["known"]))
    # A misread blog (most posts "missing") deletes nothing.
    r = run_blog(FakeBlog([live[0], live[1], live[12]]), saved, full=True)
    assert r["gone"] == [] and len(r["known"]) == 60, r["gone"]


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok", name)
    print(f"{len(tests)} passed")
