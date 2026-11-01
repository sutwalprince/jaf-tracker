#!/usr/bin/env bash
# Sets up the JAF Tracker server (browser-free) on a fresh Ubuntu machine, e.g. Oracle Cloud's free VM.
# Installs: the sync job (every 15–30 min), the private website, and the always-on WhatsApp service.
#
# Run from your laptop, in this project folder:
#   scp -r jaf-tracker server/*.py server/web-shim.js server/setup.sh ubuntu@<server-ip>:~/
#   ssh ubuntu@<server-ip> 'bash setup.sh'
# Then follow the "After setup" steps in server/README.md (Tailscale, login, Telegram/WhatsApp).
set -euo pipefail

SYNC="$HOME/jaf-sync"
WEB="$HOME/jaf-web"
UNITS="$HOME/.config/systemd/user"

for f in jaf_sync.py jaf_whatsapp.py jaf_ai.py jaf_transcribe.py web.py web-shim.js; do
  [ -f "$HOME/$f" ] || { echo "Missing ~/$f. Copy the project files first (see the top of this script)."; exit 1; }
done
[ -f "$HOME/jaf-tracker/dashboard.html" ] || { echo "Missing ~/jaf-tracker (the website reuses its pages)."; exit 1; }

echo "==> Packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv >/dev/null
sudo timedatectl set-timezone Asia/Kolkata

echo "==> Files (private to your user)"
mkdir -p "$SYNC" "$WEB" "$UNITS"
chmod 700 "$SYNC"
mv "$HOME/jaf_sync.py" "$HOME/jaf_whatsapp.py" "$HOME/jaf_ai.py" "$HOME/jaf_transcribe.py" "$SYNC/"
mv "$HOME/web.py" "$HOME/web-shim.js" "$WEB/"
# The secret key the laptop extension uses to send your login (copy it to the laptop afterwards).
[ -f "$SYNC/key" ] || (umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$SYNC/key")
# Settings: alert channels stay off until you set them up (see server/README.md → Settings).
if [ ! -f "$SYNC/config.json" ]; then
  (umask 077; cat > "$SYNC/config.json" <<'EOF'
{"telegram": {"enabled": false}, "whatsapp": {"enabled": false}, "quiet": {"start": 0, "end": 7}, "morning": 8, "evening": 20}
EOF
)
fi

echo "==> Python libraries in a private virtualenv: WhatsApp (neonize), Claude (anthropic), speech-to-text (faster-whisper)"
python3 -m venv "$SYNC/wa-venv"
"$SYNC/wa-venv/bin/pip" install -q --upgrade pip >/dev/null
"$SYNC/wa-venv/bin/pip" install -q neonize anthropic faster-whisper
chmod 700 "$SYNC/wa-venv"

echo "==> Services"
cat > "$UNITS/jaf-sync.service" <<'EOF'
[Unit]
Description=JAF Tracker sync (portal + blog + reminders, no browser)

[Service]
Type=oneshot
ExecStart=%h/jaf-sync/wa-venv/bin/python -I %h/jaf-sync/jaf_sync.py sync
EOF

cat > "$UNITS/jaf-sync.timer" <<'EOF'
[Unit]
Description=Run the JAF Tracker sync every 15–30 minutes (random)

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
RandomizedDelaySec=15min
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > "$UNITS/jaf-web.service" <<'EOF'
[Unit]
Description=JAF Tracker website (localhost only; published privately with Tailscale Serve)

[Service]
ExecStart=/usr/bin/python3 -I %h/jaf-web/web.py
Restart=always

[Install]
WantedBy=default.target
EOF

cat > "$UNITS/jaf-whatsapp.service" <<'EOF'
[Unit]
Description=JAF Tracker WhatsApp (spare number to your main number; answers your commands)
After=network-online.target

[Service]
ExecStart=%h/jaf-sync/wa-venv/bin/python -I %h/jaf-sync/jaf_whatsapp.py serve
Restart=always
RestartSec=30
MemoryMax=900M

[Install]
WantedBy=default.target
EOF

sudo loginctl enable-linger "$USER"   # keep your services running without an SSH login
systemctl --user daemon-reload
systemctl --user enable --now jaf-web jaf-sync.timer
# jaf-whatsapp is enabled after you link the spare number (server/README.md → WhatsApp).

echo "==> SSH hardening (key-only login was already the default on Oracle images)"
sudo tee /etc/ssh/sshd_config.d/99-jaf-hardening.conf >/dev/null <<'EOF'
PermitRootLogin no
PasswordAuthentication no
X11Forwarding no
MaxAuthTries 3
LoginGraceTime 30
EOF
sudo sshd -t && sudo systemctl reload ssh

cat <<'EOF'

Done. Next (see server/README.md → After setup):
  1. Install Tailscale and publish the website:  tailscale up  ·  tailscale serve --bg 8787
  2. Copy ~/jaf-sync/key to the laptop and set up "Send login to server" in the extension.
  3. Optional: Telegram, WhatsApp (pair the spare number), healthchecks.io, AI (set-anthropic-key).
EOF
