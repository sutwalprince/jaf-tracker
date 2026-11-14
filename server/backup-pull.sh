#!/usr/bin/env bash
# Runs on your laptop. Copies the server's newest encrypted backup to ~/jaf-backups (keeps the last 14).
#   server/backup-pull.sh                 pull now
#   server/backup-pull.sh install         pull automatically (10 min after login, and at 5/11 am, 5/11 pm)
#   server/backup-pull.sh set-password    choose the backup password on the server (typed here, hidden)
# Restore on a new server:  scp the file there, then  ~/jaf-sync/jaf_backup.py restore <file>
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/local.env" ] && . "$HERE/local.env"
SERVER="${JAF_SERVER:?Set JAF_SERVER in server/local.env}"
DEST="${JAF_BACKUPS:-$HOME/jaf-backups}"
KEEP=14

case "${1:-pull}" in
  install)
    UNITS="$HOME/.config/systemd/user"
    mkdir -p "$UNITS"
    cat > "$UNITS/jaf-backup-pull.service" <<EOF
[Unit]
Description=Copy the JAF Tracker server's encrypted backup to this laptop
[Service]
Type=oneshot
ExecStart=$HERE/backup-pull.sh pull
EOF
    cat > "$UNITS/jaf-backup-pull.timer" <<'EOF'
[Unit]
Description=Copy the JAF Tracker backup 4 times a day (and after login)
[Timer]
OnStartupSec=10min
OnCalendar=*-*-* 05,11,17,23:00
Persistent=true
[Install]
WantedBy=timers.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now jaf-backup-pull.timer
    echo "Installed: the laptop copies the newest backup at 5 and 11, am and pm (and 10 min after you log in)."
    ;;
  set-password)
    read -rsp "New backup password (at least 12 characters): " a; echo
    read -rsp "Again: " b; echo
    [ "$a" = "$b" ] || { echo "The two didn't match." >&2; exit 1; }
    [ "${#a}" -ge 12 ] || { echo "Too short: use at least 12 characters." >&2; exit 1; }
    printf '%s\n%s\n' "$a" "$a" | ssh "$SERVER" 'python3 -I ~/jaf-sync/jaf_backup.py set-password'
    echo "Keep this password in your password manager: a backup can't be opened without it."
    ;;
  pull)
    mkdir -p -m 700 "$DEST"
    name=$(ssh -o BatchMode=yes -o ConnectTimeout=20 "$SERVER" 'readlink ~/jaf-backups/latest.tar.gz.gpg') \
      || { echo "No backup on the server yet (or it's unreachable)." >&2; exit 1; }
    if [ ! -f "$DEST/$name" ]; then
      scp -q -o BatchMode=yes "$SERVER:jaf-backups/$name" "$DEST/$name.part" && mv "$DEST/$name.part" "$DEST/$name"
      chmod 600 "$DEST/$name"
      echo "Copied $name"
    else
      echo "Already have $name"
    fi
    ls -1 "$DEST"/jaf-sync-*.tar.gz.gpg 2>/dev/null | head -n -"$KEEP" | xargs -r rm --
    ;;
  *)
    sed -n '2,6p' "$0"; exit 1 ;;
esac
