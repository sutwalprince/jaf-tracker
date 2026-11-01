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
scp -r jaf-tracker server/*.py server/web-shim.js server/setup.sh ubuntu@<server-ip>:~/
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

The website's **Sync now** button calls `POST /api/sync` on `web.py`, which starts `jaf-sync.service`
(at most once a minute, and never while a sync is running); `GET /api/sync` says whether one is running.
`web.py` gzips responses and answers unchanged files with `304`, and the site keeps the last data in the
browser's `localStorage`, so it opens instantly over Tailscale's relays.

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

## Security

- SSH: key only, no passwords, no root login, 3 tries (`/etc/ssh/sshd_config.d/99-jaf-hardening.conf`).
- Only port 22 is open to the internet; the website listens on `127.0.0.1` and is published only on
  your tailnet. `POST /api/session` also requires the key from `~/jaf-sync/key`.
- `~/jaf-sync/` is `700`; `session.json` (portal login + cookies), `config.json` (tokens, number),
  `key` and `whatsapp.db` are `600`. Security updates install automatically (unattended-upgrades).
