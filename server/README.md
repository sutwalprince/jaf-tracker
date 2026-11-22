# JAF Tracker: server

Runs the tracker 24/7 without a browser: a Python sync every 15–30 minutes, an always-on WhatsApp
service, and a private website. A free Oracle Cloud VM (1 GB RAM is enough) works well; any Ubuntu
24.04 machine will do. See the [main README](../README.md) for what it does.

| Piece | Runs as | What it does |
|---|---|---|
| `jaf_sync.py` | `jaf-sync.timer` (every 15–30 min, random) | Portal API, blog via IITB SSO, events, reminders, 8 am / 8 pm messages, Telegram, queues WhatsApp, website data |
| `jaf_whatsapp.py serve` | `jaf-whatsapp.service` (always on) | Sends queued WhatsApp messages (typing indicator; batches paced 2–5 s apart, replies to you quick), answers your commands, and passes other questions (text or voice) to the AI |
| `jaf_ai.py` | used by the two above | Gemini (default, free tier) or Claude: summaries of new JAFs you can sign, answers to your questions; hard monthly ₹ cap |
| `jaf_transcribe.py` | short-lived, per voice note | Transcribes voice notes on the server (faster-whisper `base`), then the audio is deleted |
| `web.py` | `jaf-web.service` | The private website on `127.0.0.1:8787`, published only to your Tailscale devices; receives your login from the laptop |

All files live in `~/jaf-sync/` (private, `700`) and `~/jaf-web/`.

## 1. Create the server

On Oracle Cloud (free tier): **Compute → Instances → Create instance**.

- **Image**: Canonical Ubuntu 24.04. **Shape**: `VM.Standard.E2.1.Micro` (AMD, always available) or
  `VM.Standard.A1.Flex` (ARM, faster, often out of capacity).
- **Networking**: first create a network with **Networking → Virtual cloud networks → Actions →
  Start VCN Wizard → Create VCN with Internet Connectivity**, then pick its *public* subnet and turn on
  **Automatically assign public IPv4 address**.
- **SSH keys**: **Paste public key**: the contents of your laptop's `~/.ssh/id_ed25519.pub`.

Keep the default firewall (only SSH, port 22, is open).

## 2. Install

From your laptop, in the project folder:

```bash
scp -r jaf-tracker server/*.py server/web-shim.js server/device-session.js server/sw.js server/setup.sh ubuntu@<server-ip>:~/
ssh ubuntu@<server-ip> 'bash setup.sh'
echo 'JAF_SERVER=ubuntu@<server-ip>' > server/local.env   # used by deploy.sh; not in git
```

`setup.sh` installs the Python virtualenv with neonize (the WhatsApp library), creates the secret key
and a settings file with all alerts off, installs and starts the services, and hardens SSH.

## 3. After setup

**Tailscale (private website + login endpoint).** On the server:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=jaf-tracker     # open the link it prints and sign in
sudo tailscale serve --bg 8787               # approve "Serve" when it asks
```

Install Tailscale on your laptop and phone with the same account. The site is then at
`https://jaf-tracker.<your-tailnet>.ts.net`, reachable only from your devices (never use `funnel`).

**Your login.** Copy the key to the laptop, then set up the extension's **Send login to server** panel
(see [`jaf-tracker/README.md`](../jaf-tracker/README.md)):

```bash
mkdir -p ~/.config/jaf-tracker && scp ubuntu@<server-ip>:jaf-sync/key ~/.config/jaf-tracker/server-key
chmod 600 ~/.config/jaf-tracker/server-key
```

Log in to the portal in laptop Chrome and open the placement blog once; the server syncs right away.

## 4. Settings (`~/jaf-sync/config.json`)

Edit on the server (`nano ~/jaf-sync/config.json`); keep it private (`chmod 600`).

```json
{
  "telegram":    {"enabled": true, "token": "<bot token from @BotFather>", "chatId": "<your chat id>"},
  "whatsapp":    {"enabled": true, "to": "<your main number, country code + digits>"},
  "healthcheck": "https://hc-ping.com/<your check id>",
  "quiet":       {"start": 0, "end": 7},
  "morning":     8,
  "evening":     20
}
```

- **Telegram**: create a bot with @BotFather, send it "hi", then get your chat id from
  `https://api.telegram.org/bot<token>/getUpdates` (`message.chat.id`).
- **healthchecks.io**: create a check with period 30 min and grace 30 min; it alerts you if syncs stop.
- **Quiet hours** and the **morning / evening** messages use hours in IST.

### WhatsApp (spare number → your main number)

Use a spare WhatsApp number as the sender, so your main account is never automated. Set
`whatsapp.to` above to your main number, then link the spare number:

```bash
systemctl --user stop jaf-whatsapp
~/jaf-sync/wa-venv/bin/python -I ~/jaf-sync/jaf_whatsapp.py pair <spare number with country code>
```

It prints a pairing code. On the spare phone: **WhatsApp → Linked devices → Link a device → Link with
phone number instead**, and type the code. Then start the service:

```bash
systemctl --user enable --now jaf-whatsapp
```

It only replies to your main number. Incoming messages are never stored.

### AI: JAF summaries and answers (Gemini, or Claude)

**Gemini (default, free tier):** create a key at aistudio.google.com → **Get API key**, then store it on the
server. It's typed in hidden and saved only in `config.json`:

```bash
~/jaf-sync/wa-venv/bin/python -I ~/jaf-sync/jaf_sync.py set-gemini-key
~/jaf-sync/wa-venv/bin/python -I ~/jaf-sync/jaf_sync.py summarize-open --dry-run --limit 1   # try one
```

It uses `gemini-3.6-flash`, and `gemini-3.5-flash-lite` when the first is busy, slow or out of free quota. On the
free tier **Google may use your prompts to improve its products**.

**Claude instead (paid):** `jaf_sync.py set-anthropic-key` with a key from console.anthropic.com
(uses `claude-sonnet-5-5`). Switch back any time with `set-gemini-key`.

**OpenRouter instead (paid from your credits):** `jaf_sync.py set-openrouter-key` with a key from openrouter.ai/keys.
It uses `anthropic/claude-haiku-5.5` (about ₹0.02 a post), with `openai/gpt-6-luna` if that's down, and asks
only providers that don't keep or train on prompts. Change `openrouter.model` in `config.json` for another model.

**Second reader for posts** (`ai.postChecks`, on by default): the AI reads each recent post about your tests,
interviews, PPTs and forms once more. It must quote the exact words with the time, and the code checks that quote is
in the post, or the answer is dropped. If it disagrees with the tracker's own reading, the message says
"⚠️ The AI read the time as …"; when only the AI could read a time, it becomes a reminder marked "🤖 read by AI".
Roll numbers are removed before posts are sent. Shortlists and seating sheets never go to the AI.

Settings in `config.json`:

```json
"ai":        {"provider": "gemini", "monthlyLimitInr": 250, "usdToInr": 90, "summaries": true, "answers": true},
"gemini":    {"apiKey": "<key>", "model": "gemini-3.6-flash", "fallbackModel": "gemini-3.5-flash-lite"},
"anthropic": {"apiKey": "<sk-ant-…>", "model": "claude-sonnet-5-5"}
```

- **Budget**: every call is priced at the provider's paid rates from its token counts (+15% margin) into
  `~/jaf-sync/ai-usage.json`; a call that could cross `monthlyLimitInr` isn't made, so even if billing is ever
  turned on, the month can't go past ₹250. On the Gemini free tier you actually pay nothing.
  `jaf_sync.py ai` (or the WhatsApp `status` command) shows this month's count.
- **Sent to the AI provider**: JAF details, blog posts about your companies (shortlists can name other
  students), your questions. **Not sent**: your login, cookies, tokens, numbers, or voice audio: voice notes
  are transcribed here and deleted right away.

## Day-to-day commands (on the server)

```bash
PY=~/jaf-sync/wa-venv/bin/python
$PY -I ~/jaf-sync/jaf_sync.py status          # last sync, login expiry, counts, errors, AI spend
$PY -I ~/jaf-sync/jaf_sync.py sync            # sync now
$PY -I ~/jaf-sync/jaf_sync.py catchup         # send the catch-up now
$PY -I ~/jaf-sync/jaf_sync.py morning         # send today's plan now
$PY -I ~/jaf-sync/jaf_sync.py evening         # send tonight's deadlines now
$PY -I ~/jaf-sync/jaf_sync.py summarize-open  # AI summaries of every JAF you can sign now
$PY -I ~/jaf-sync/jaf_sync.py ai              # this month's AI spend
systemctl --user list-timers jaf-sync.timer   # when the next sync runs
journalctl --user -u jaf-sync -n 20           # recent syncs
journalctl --user -u jaf-whatsapp -n 20       # WhatsApp service log
cat ~/jaf-sync/wa-status.json                 # WhatsApp connected? last heartbeat
```

## Updating

From the laptop, after editing files in this project:

```bash
server/deploy.sh
```

It copies the code, restarts the website and WhatsApp service, and runs one sync.

How the website stays current (`web.py` + `web-shim.js`):
- **Push:** `GET /api/events` is a Server-Sent Events stream. `web.py` checks every 2 s whether
  `~/jaf-web-data.json`, `read.json` or `hidden.json` changed and, if so, sends `update`; the page then
  reloads `data.json` and redraws. At most 20 streams; each ends after 30 min and the browser reconnects.
- **Sync now:** `POST /api/sync` starts `jaf-sync.service` (at most once a minute, never while one runs);
  `POST /api/sync?stale=1`, sent when the site is opened, only starts one if the last sync is over 5 min
  old. `GET /api/sync` says whether a sync is running.
- **Fast over Tailscale's relays:** gzip, `ETag` / `304` for unchanged files, and the last data kept in the
  browser's `localStorage`, so pages draw at once and refresh in the background.
- **Installable:** `site.webmanifest` + `icons/icon192.png` / `icon512.png`.
- **Offline:** `sw.js` (a service worker) keeps the site's files on the device and serves them without
  touching the network; `web.py` stamps it with a fingerprint of those files, so a deploy installs a whole
  new copy and open pages reload once. `data.json` and `/api/*` never go through its cache: the last data
  stays in `localStorage`. Posts read and companies hidden while offline are queued there and sent when the
  server is reachable again. Service workers need HTTPS, which `tailscale serve` provides.

**Shortlists.** For posts about your companies from the last 10 days whose title says shortlist, waitlist,
selected, result or offer, the post text and any linked sheet (same downloaders as below) are checked for your
roll number. Found: the post counts as naming you (🎉 message, Mentions). Not found, but the list has at least 5
roll numbers and every link was read: you're out, so the company is added to `hidden.json` once per list (unhiding
sticks) and you get a message. Only that yes/no is kept (`state.json → lists`), never the list.

**Your room from seating sheets.** Posts keep their links as `Click Here (https://…)`. For posts about your
companies from the last 4 days, links on "seating / seat allotment / venue" lines are downloaded (IITB
bighome shares as PDF via `pdftotext` from `poppler-utils`, Google Sheets as xlsx), your row is found by
roll number, and only your room/seat is kept (`state.json → seats`); the file is never saved. While that
company's test is still ahead, the sheet is re-read every 30 min. Changing `POST_FORMAT` in `jaf_sync.py`
makes the next sync re-read the whole blog once (GET only, no repeat messages).

## Backups

`~/jaf-sync` (settings, logins, the WhatsApp link, website password and passkeys, read/hidden lists, reminder
history) exists only on the server, and none of it is in git. If Oracle reclaims the VM, a backup saves you
re-linking WhatsApp and re-entering everything.

```bash
server/backup-pull.sh set-password   # on your laptop, once: choose the backup password (typed hidden, sent to the server)
server/backup-pull.sh install        # on your laptop, once: pull the newest backup 4 times a day
```

- Every night at 04:30 the server writes `~/jaf-backups/jaf-sync-<date>.tar.gz.gpg` (`jaf-backup.timer`):
  encrypted with gpg AES-256 and your password, WhatsApp's database copied safely while in use, about 70 KB.
  The server keeps 3; your laptop keeps the last 14 in `~/jaf-backups`.
- Not included: the code (in git), the Python venv and the voice model (`setup.sh` recreates them).
- **Keep the password in a password manager.** Without it a backup can't be opened; with it, the file is safe
  to keep anywhere (Google Drive, a USB stick).
- Check one: `scp` it to the server, then `~/jaf-sync/jaf_backup.py verify <file>` (lists names and sizes).

**Restore on a new server:** run §2 (setup) with the code from git, then

```bash
scp ~/jaf-backups/<newest>.tar.gz.gpg ubuntu@<new-ip>:~/
ssh -t ubuntu@<new-ip> '~/jaf-sync/jaf_backup.py restore ~/<newest>.tar.gz.gpg'   # asks for the password
```

It stops the services, puts everything back and starts them again. Then update `server/local.env` with the new
address, set up Tailscale (§3; name the machine as before to keep the same website address) and log in to the
portal once so the extension sends your login.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "🔑 Login needed" / login expiring | Log in to the portal in laptop Chrome (Tailscale on). The extension sends it automatically. |
| "IITB SSO login needed" | Open the placement blog in laptop Chrome and log in with SSO (Placement). |
| "WhatsApp sender needs linking again" | Re-link the spare number (WhatsApp section above). |
| No WhatsApp messages | `systemctl --user status jaf-whatsapp`, `cat ~/jaf-sync/wa-status.json`; queued messages wait in `~/jaf-sync/wa-outbox/`. |
| Website doesn't open | Is Tailscale connected on that device? `sudo tailscale serve status` on the server. |
| healthchecks.io says "down" | `journalctl --user -u jaf-sync -n 30`; `~/jaf-sync/wa-venv/bin/python -I ~/jaf-sync/jaf_sync.py status`. |
| AI replies "couldn't answer" | `$PY -I ~/jaf-sync/jaf_sync.py ai`; re-enter the key with `set-gemini-key`; on the free tier, wait if the daily quota is used up. |

When killing a process over SSH, use its PID (`pgrep`, then `kill <pid>`): `pkill -f <pattern>` also
matches the SSH command itself and ends your session.

## Website password

On the public address, every page, `data.json` and `/api/*` require a password. Only the install manifest and
icons don't. Without a password file, nobody can log in there. A device stays logged in for up to 400 days, each visit extends that, and failed tries are limited.

Your Tailscale address doesn't ask for it. `web.py` listens on two local ports, `8787` for Tailscale Serve and
`8788` for Nginx, and the port decides. **Never point Nginx or anything public at `8787`.**

```bash
~/jaf-sync/wa-venv/bin/python -I ~/jaf-web/web.py set-password   # choose a new one, typed in hidden
```

Changing it logs out every device. There's no logout button or page, so to remove a single device, clear this site's data in that device's browser.

## Publishing on the internet (optional)

This makes the site reachable from any device at your own domain (`jaf.example.com` below). **It's less private than Tailscale
alone:** anyone can reach the login page, so only the password protects your data.

First put your settings in `~/jaf-sync/site.json` (not in git): `{"domain": "jaf.example.com", "user": "me"}`
(`user` is the name passkeys are saved under; an optional `parentOrigin` is a page of yours that keeps the login
fresh in a hidden frame). `enable-public-site.sh` puts Nginx in front of `web.py`. It blocks `/api/session` publicly, so your portal
login is still sent over Tailscale only. Then allow TCP 80 and 443 in Oracle's security list, point the DNS
name at the server, and run `sudo certbot --nginx -d jaf.example.com`.

Tailscale Serve holds port 443 on its own interface, so Nginx can't listen on 443 everywhere. After Certbot runs,
in `/etc/nginx/sites-available/jaf-tracker` change `listen 443 ssl` to `listen <the server's private IP>:443 ssl`,
remove the `listen [::]:443` line, then run `sudo nginx -t && sudo systemctl restart nginx`.

To go back to Tailscale only, remove both Oracle rules and run `sudo systemctl disable --now nginx`.

## Security

- SSH: key only, no passwords, no root login, 3 tries (`/etc/ssh/sshd_config.d/99-jaf-hardening.conf`).
- The website listens only on `127.0.0.1`. It's published on your tailnet, plus the internet through Nginx if you
  enabled that. `POST /api/session` also requires the key from `~/jaf-sync/key`.
- `~/jaf-sync/` is `700`; `session.json` (portal login + cookies), `config.json` (tokens, number),
  `key` and `whatsapp.db` are `600`. Security updates install automatically (unattended-upgrades).

## Website login (public address)

The public address requires a passkey. The password only creates a passkey on a new device and gives no login by itself. The login cookie lasts 15 minutes and is renewed silently by a device key that scripts can't read. Passkeys are stored in `~/jaf-sync/web-passkeys.json`; delete that file to remove every passkey and device. The Tailscale address still needs no login.
