#!/usr/bin/env bash
# Copies your latest changes to the server and restarts what's affected, then runs one sync.
# Usage (from anywhere):  server/deploy.sh
set -euo pipefail
# Your server's address lives in server/local.env (not in git), e.g.  JAF_SERVER=ubuntu@203.0.113.10
[ -f "$(dirname "$0")/local.env" ] && . "$(dirname "$0")/local.env"
SERVER="${JAF_SERVER:?Set JAF_SERVER in server/local.env (e.g. JAF_SERVER=ubuntu@<server-ip>)}"
cd "$(dirname "$0")/.."

scp -q server/jaf_sync.py server/jaf_whatsapp.py server/jaf_ai.py server/jaf_transcribe.py "$SERVER":~/jaf-sync/
scp -q -r jaf-tracker "$SERVER":~/                 # the website reuses the extension's pages
scp -q server/web.py server/web-shim.js "$SERVER":~/jaf-web/
ssh "$SERVER" '
  systemctl --user restart jaf-web
  systemctl --user is-enabled --quiet jaf-whatsapp && systemctl --user restart jaf-whatsapp
  ~/jaf-sync/wa-venv/bin/python -I ~/jaf-sync/jaf_sync.py sync
'
