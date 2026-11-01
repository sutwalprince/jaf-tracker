# AGENTS.md: setting up the IITB JAF Tracker for someone

Instructions for an AI coding assistant (Claude Code, Codex, Copilot, …) helping a student set up their
own copy. Read [`README.md`](README.md), [`server/README.md`](server/README.md) and
[`jaf-tracker/README.md`](jaf-tracker/README.md) first; this file adds the order of work, the rules, what
to ask the student for, and the pitfalls that cost time.

## Rules (non-negotiable)

1. **Read-only on the placement portal.** Only HTTP GET. Never sign, unsign, apply, upload or change
   anything on `campus.placements.iitb.ac.in`, through code or through a browser you control.
   If a step would need a click that changes state, stop and ask.
2. **Never automate the portal login.** It's IITB SSO with a bundled (invisible) reCAPTCHA. The student
   logs in themselves in their own Chrome; the extension forwards the resulting session.
   Don't store their LDAP password anywhere, and don't try to get around CAPTCHAs or bot detection.
3. **Secrets never enter the repo or the chat.** API keys, bot tokens, the server key, cookies, phone
   numbers and the server's address live only in `~/jaf-sync/config.json` / `session.json` / `key` on the
   server (0600), in the browser's extension storage, or in `server/local.env` (gitignored). Have the
   student type secrets themselves (`set-gemini-key`, `set-anthropic-key` use hidden input).
   Before any commit, scan for keys, IPs, phone numbers, roll numbers and tailnet names.
4. **Use the student's own accounts** (cloud VM, Tailscale, Google AI Studio, Telegram, WhatsApp). Ask before
   anything that publishes, purchases, sends messages, or deletes data, and before force-pushing.
5. **WhatsApp: a spare number sends, the student's main number receives.** Never automate their main
   account. The service answers only the configured main number and stores no incoming messages.
6. **Privacy:** blog posts can contain other students' names and roll numbers (shortlists). They go to
   the AI provider in prompts (tell the student; on Gemini's free tier Google may use prompts). Don't
   copy them anywhere else.

## Ask the student for

- Which cloud they'll use (Oracle Cloud free tier works) and that they can create a VM there.
- Their main WhatsApp number (receives) and a spare WhatsApp number on a phone (sends); optional.
- Whether they want Telegram (they create the bot with @BotFather), healthchecks.io, and AI
  (Gemini key from aistudio.google.com is free; Claude is paid).
- Quiet hours and the morning/evening message times, if different from 00:00–07:00 / 8 am / 8 pm IST.

## Order of work

1. **Server.** Create an Ubuntu 24.04 VM (see `server/README.md` §1: on Oracle, create the VCN with the
   *VCN Wizard → Internet Connectivity* first, then the instance on its public subnet with a public IPv4;
   paste the student's SSH public key). Free ARM shapes are often out of capacity in India regions: use
   `VM.Standard.E2.1.Micro` (1 GB RAM) if needed; everything here fits in it.
2. **Install.** From the repo: copy files and run `server/setup.sh` (`server/README.md` §2). Create
   `server/local.env` with `JAF_SERVER=ubuntu@<ip>` so `server/deploy.sh` works.
3. **Tailscale** on server, laptop and phone, same account; `tailscale serve --bg 8787` (never `funnel`).
4. **Extension** on the student's laptop Chrome (`jaf-tracker/README.md`): load unpacked, then in
   *Send login to server* enter `https://<host>.<tailnet>.ts.net` and the key from `~/jaf-sync/key`
   (copied with `scp`, pasted by the student). The student logs in to the portal and opens the placement
   blog once; the server should log `synced: auth={'ok': True} …`.
5. **Messaging** (optional, any order): Telegram in `config.json`; WhatsApp pairing
   (`jaf_whatsapp.py pair <spare number>` with the service stopped, then enable `jaf-whatsapp`);
   healthchecks.io URL; AI key via `jaf_sync.py set-gemini-key`.
6. **Verify** (below), then show the student the day-to-day steps in `README.md`.

## Verify

```bash
PY=~/jaf-sync/wa-venv/bin/python
$PY -I ~/jaf-sync/jaf_sync.py status                    # auth OK, counts, AI line
$PY -I ~/jaf-sync/jaf_sync.py summarize-open --dry-run --limit 1   # one AI summary printed, not sent
systemctl --user is-active jaf-web jaf-whatsapp jaf-sync.timer
cat ~/jaf-sync/wa-status.json                           # connected: true
curl -s -o /dev/null -w "%{http_code}\n" https://<host>.<tailnet>.ts.net/   # 200 (from a tailnet device)
```

Then have the student text `status` and `today` to the spare number from their main number, and ask a
question by text and by voice note.

## Pitfalls (each of these cost real time)

- **Run Python with the venv and `-I`**: `~/jaf-sync/wa-venv/bin/python -I …`. `-I` drops the script's
  folder from `sys.path`; `jaf_sync.py` and `jaf_whatsapp.py` add it back themselves, keep that.
- **Blog session**: the blog is `mod_auth_openidc` behind IITB SSO. A GET to
  `/blog/placement/authplacement/` with the SSO cookies signs in automatically, **but only with
  `Accept: text/html`**; without it the server answers 401. The SSO session (`connect.sid`) lasts ~2 weeks,
  the portal token (JWT in `localStorage.token`) ~5 days.
- **Portal API**: `Authorization: Bearer <token>`; JWT payload has `id`, `username` (roll), `batch`,
  `service`, `season`. Open JAFs + eligibility: `GET /api/v1/job?sendEligibility=true&service=&year=&season=`;
  round durations are free text in each job's `processes[].duration`.
- **One WhatsApp connection per linked device**: only `jaf-whatsapp.service` connects; everything else
  queues files in `~/jaf-sync/wa-outbox/`. Stop the service before running `pair`.
- **neonize/whatsmeow can hang on disconnect**: the scripts end with `os._exit()`; keep that.
- **Never `pkill -f <pattern>` over SSH** when the pattern also appears in your SSH command line: it kills
  your own session. Use `pgrep` and kill by PID.
- **faster-whisper + PyAV 19**: `decode_audio` passes an argument PyAV removed, so `jaf_transcribe.py`
  decodes audio itself. Don't switch back to passing a file path.
- **Gemini models change and get busy.** If a model times out or returns 404/503, try another free-tier
  model and set `config.json → gemini.model` / `fallbackModel`. Check the current list on
  ai.google.dev/gemini-api/docs/pricing.
- **The website is the extension's own pages** (`dashboard.html` with `#jafs` / `#updates` / `#open`
  views), served by `web.py` with `web-shim.js` standing in for `chrome.*`. Change the pages once, test
  both in the extension and on the site, then `server/deploy.sh`. Keep it one page: separate pages cost a
  full reload per tab through Tailscale's relay. New files must be added to `FILES` in `web.py`.
- **Chrome caches extension background code by version**: bump `version` in `manifest.json` whenever you
  change the extension, then reload it in `chrome://extensions`.
- **Reminders**: they're keyed by blog post (a company's repeat post restarts them, by design) and are not
  queued during quiet hours, so their countdown is always fresh.
- **Oracle free VMs can be reclaimed when idle**; the healthchecks.io ping is how the student finds out.
