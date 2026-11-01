# IITB JAF Tracker

Keeps track of your IIT Bombay placement season for you. It watches the JAFs you've signed, the JAFs
you can still sign, and every placement-blog post about your companies (PPTs, tests, JAF updates,
shortlists). It messages you on WhatsApp and Telegram with updates, reminders and a daily plan,
and shows everything on a private website.

**It is read-only on the placement portal.** It only reads (HTTP GET). It never signs, unsigns,
applies, uploads or changes anything.

## How it fits together

```
 Your laptop (Chrome)                         Your server (e.g. Oracle Cloud free VM, Ubuntu)
 ┌──────────────────────────┐                 ┌──────────────────────────────────────────────┐
 │ JAF Tracker extension    │  your login     │ jaf_sync.py  (every 15–30 min, no browser)   │
 │ · dashboard, Open JAFs   │ ──────────────▶ │  · portal API: your JAFs, open JAFs, CTC     │
 │ · sends your portal      │  (Tailscale,    │  · blog via IITB SSO: new posts, events      │
 │   login + IITB SSO       │   secret key)   │  · reminders, 8 am plan, 8 pm deadlines      │
 │   cookies to the server  │                 │  · quiet hours 00:00–07:00                   │
 └──────────────────────────┘                 │ jaf_whatsapp.py (always on)                  │
                                              │  · spare number → your main number           │
 Your phone                                   │  · answers your commands                     │
 ┌──────────────────────────┐  WhatsApp       │ web.py  (private website, Tailscale only)    │
 │ WhatsApp · Telegram      │ ◀────────────── │ healthchecks.io ping on every run            │
 │ Website (via Tailscale)  │ ◀────────────── │                                              │
 └──────────────────────────┘                 └──────────────────────────────────────────────┘
```

## What you get

**Messages** (WhatsApp: one message per item, with the full post text; Telegram: short summaries):

| When | What |
|---|---|
| As it happens | New blog posts about your companies, posts naming your roll number (shortlists), new JAFs you're eligible for |
| 24 h, 12 h, 4 h before | Tests, PPTs, registration deadlines, JAFs you can sign but haven't, your portal login expiring (a company's new post about a test starts its reminders again) |
| 8 am | Today's plan: tests/PPTs with times and venues, JAFs closing today |
| 8 pm | Today's remaining deadlines, plus anything due early tomorrow |
| 00:00–07:00 | Quiet: updates are held and sent at 7 am; reminders wait and are written fresh after 7 am, except ones due before then |
| If something breaks | Login needed, IITB SSO login needed, blog unreachable; healthchecks.io alerts you if syncs stop |

**WhatsApp commands.** Text the spare number from your main number (it ignores everyone else):

| Command | Reply |
|---|---|
| `today` | Today's tests, PPTs and JAF deadlines |
| `open` | JAFs you can still sign, with CTC and deadline |
| `sync` | Checks the portal and blog right now, then reports |
| `hide <company>` / `unhide <company>` | Stop or restart updates and reminders for a company |
| `status` | Last sync, login expiry, blog, WhatsApp |
| `catchup` | What's coming up, then each update from the last day |

**AI (Google Gemini free tier by default, or Claude; hard limit ₹250 a month).** Every new JAF you're eligible for (or bonus) arrives
on WhatsApp as a proper summary: pay, location, eligibility, cutoff, bond, rounds with durations, key
requirements, deadline and a one-line takeaway. Anything you send the spare number that isn't a command,
typed or as a **voice note**, is answered from the tracker's data: "how many software engineer jobs are
open?", "which JAFs close today?", "what does Juspay's JD ask for?". Voice notes are transcribed on the
server itself. When the month's budget is used up, summaries fall back to the plain format and questions get
"budget used up" until the 1st.

**Website** (private: opens only on your own Tailscale devices; works well on a phone, with a bottom
tab bar). Three tabs that switch instantly:
- *My JAFs* (`…/#jafs`): your signed JAFs with CTC, rounds and durations, and the next test or PPT on
  each card. Companies with something coming up are listed first. Hide/Unhide companies you're done with.
- *Updates* (`…/#updates`, the default): blog posts about your companies, grouped by day and labelled
  (Test, PPT, Interview, Deadline, JAF open, Shortlist), upcoming events, and **Show unread updates (N)**.
  The tab shows a red count of unread posts.
- *Open JAFs* (`…/#open`): eligibility, bonus, CTC for your program, countdowns, link to sign on the portal.

**Sync now** (top right) asks the server to sync with the portal and blog straight away (read-only, at
most once a minute). Read/unread and hidden companies are shared across all your devices; each device
keeps a copy of the last data so pages open instantly.

## Day to day

- **About every 5 days**, when you get "🔑 Your portal login expires…": log in to the placement
  portal in Chrome on your laptop, with Tailscale connected. The extension sends the new login to
  the server, which syncs straight away.
- **About every 2 weeks at most**, if you get "IITB SSO login needed": open the placement blog in
  laptop Chrome and click the SSO login.
- **Every week or two**, open WhatsApp on the spare phone so its linked devices stay active.

## Setting it up

1. Set up the server: [`server/README.md`](server/README.md).
2. Install the extension on your laptop: [`jaf-tracker/README.md`](jaf-tracker/README.md).

Setting this up for someone else with an AI coding assistant (Claude Code, Codex, Copilot, …)?
Point it at [`AGENTS.md`](AGENTS.md): the step-by-step guide, rules and known pitfalls written for it.

## Project layout

| Path | What |
|---|---|
| `jaf-tracker/` | The Chrome extension (its pages are also served as the website) |
| `server/jaf_sync.py` | The sync: portal, blog, events, reminders, daily messages, Telegram; queues WhatsApp |
| `server/jaf_whatsapp.py` | The always-on WhatsApp service: sends the queue, answers commands and questions, links the spare number |
| `server/jaf_ai.py` | The AI (Gemini or Claude): JAF summaries and answers, with the monthly budget |
| `server/jaf_transcribe.py` | Voice-note transcription on the server (faster-whisper) |
| `server/web.py`, `server/web-shim.js` | The private website, and the shim that lets the extension's pages run as a site |
| `server/setup.sh` | One-time setup of a fresh server |
| `server/deploy.sh` | Copies your changes to the server and restarts what's affected |

Nothing personal is in the code. Your server's address goes in `server/local.env` and your settings
and logins live only on the server, all outside git (see `.gitignore`).

## Security, in short

- Server: SSH with your key only (no passwords, no root login); only port 22 is open to the internet.
- The website and the login endpoint are reachable only through your Tailscale network, never
  publicly. The login endpoint also needs the secret key.
- Your portal login, IITB cookies, Telegram token and WhatsApp link live in `~/jaf-sync/` on the
  server, readable only by your user. Your password is never stored anywhere.
- Incoming WhatsApp messages are never stored; only your main number's commands get a reply.
- Turn on two-factor login for your cloud account and the account you use for Tailscale.
