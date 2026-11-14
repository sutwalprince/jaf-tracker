#!/usr/bin/env python3
"""Encrypted backups of ~/jaf-sync: your settings, logins, WhatsApp link, read/hidden lists and reminder history.

    jaf_backup.py set-password      choose the backup password (asked twice, hidden). Keep it in a password manager:
                                    without it a backup can't be opened.
    jaf_backup.py backup            write ~/jaf-backups/jaf-sync-<date>.tar.gz.gpg (the jaf-backup.timer runs this daily)
    jaf_backup.py verify FILE       decrypt FILE and list what's inside (names and sizes only)
    jaf_backup.py restore FILE      put FILE back into ~/jaf-sync on a (new) server, then restart the services

Encryption: gpg, AES-256, with your password. The code itself isn't backed up (it's in git), nor the Python venv or
the voice model (setup.sh recreates them). Only the newest KEEP backups stay on the server; your laptop pulls copies
(server/backup-pull.sh).
"""
import fcntl
import getpass
import io
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

DIR = Path(os.environ.get("JAF_SYNC_DIR", Path.home() / "jaf-sync"))
OUT = Path(os.environ.get("JAF_BACKUP_DIR", Path.home() / "jaf-backups"))
PASSWORD = DIR / "backup-password"
KEEP = 3
SKIP_DIRS = {"wa-venv", "whisper-models", "__pycache__", "voice"}
SKIP_FILES = {".lock", "wa-status.json", ".ai-usage.lock"}
SERVICES = ["jaf-whatsapp.service", "jaf-web.service", "jaf-sync.timer", "jaf-backup.timer"]


def ask(prompt):
    """Hidden input from a terminal; a plain line when piped (e.g. backup-pull.sh set-password)."""
    return getpass.getpass(prompt) if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")


def wanted(path):
    rel = path.relative_to(DIR)
    return (not SKIP_DIRS & set(rel.parts) and rel.name not in SKIP_FILES
            and path.suffix not in (".py", ".pyc", ".tmp") and not path.name.endswith(("-journal", "-wal", "-shm"))
            and path.is_file())


def gpg(args, data, password):
    """Runs gpg with the password on a pipe (never on the command line or in a file other than PASSWORD)."""
    r, w = os.pipe()
    os.write(w, password.encode() + b"\n")
    os.close(w)
    try:
        p = subprocess.run(["gpg", "--batch", "--yes", "--quiet", "--pinentry-mode", "loopback",
                            "--passphrase-fd", str(r), *args], input=data, capture_output=True, pass_fds=(r,))
    finally:
        os.close(r)
    if p.returncode:
        sys.exit("gpg failed: " + p.stderr.decode(errors="replace").strip().splitlines()[-1]
                 if p.stderr else "gpg failed")
    return p.stdout


def set_password():
    # From a terminal: asked twice, hidden. Piped (backup-pull.sh set-password types it on your laptop): two lines.
    a = ask("New backup password (at least 12 characters): ")
    if len(a) < 12:
        sys.exit("Too short: use at least 12 characters.")
    if ask("Again: ") != a:
        sys.exit("The two didn't match.")
    old = os.umask(0o077)
    PASSWORD.write_text(a)
    os.umask(old)
    os.chmod(PASSWORD, 0o600)
    print(f"Saved in {PASSWORD} (readable only by you). Keep a copy in your password manager: "
          "a backup can't be opened without it.")


def backup():
    if not PASSWORD.exists():
        sys.exit("No backup password yet: run  jaf_backup.py set-password")
    buf = io.BytesIO()
    with open(DIR / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # never in the middle of a sync writing state.json
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path in sorted(DIR.rglob("*")):
                if not wanted(path) or path == PASSWORD:
                    continue
                if path.name == "whatsapp.db":
                    # The WhatsApp service keeps this database open: copy it with SQLite's own backup.
                    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
                        src, dst = sqlite3.connect(f"file:{path}?mode=ro", uri=True), sqlite3.connect(tmp.name)
                        src.backup(dst)
                        src.close()
                        dst.close()
                        tar.add(tmp.name, arcname=str(path.relative_to(DIR)))
                    continue
                tar.add(path, arcname=str(path.relative_to(DIR)))
    OUT.mkdir(mode=0o700, exist_ok=True)
    name = OUT / time.strftime("jaf-sync-%Y%m%d-%H%M%S.tar.gz.gpg")
    name.write_bytes(gpg(["--symmetric", "--cipher-algo", "AES256", "--output", "-"], buf.getvalue(),
                         PASSWORD.read_text().strip()))
    os.chmod(name, 0o600)
    latest = OUT / "latest.tar.gz.gpg"
    latest.unlink(missing_ok=True)
    latest.symlink_to(name.name)
    for old in sorted(OUT.glob("jaf-sync-*.tar.gz.gpg"))[:-KEEP]:
        old.unlink()
    print(f"backup: {name.name} ({name.stat().st_size // 1024} KB)")


def open_backup(file):
    password = PASSWORD.read_text().strip() if PASSWORD.exists() else ask("Backup password: ")
    data = gpg(["--decrypt"], Path(file).read_bytes(), password)
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz"), password


def verify(file):
    tar, _ = open_backup(file)
    for m in tar.getmembers():
        print(f"{m.size:>9}  {m.name}")
    print(f"OK: {len(tar.getmembers())} files, opened with the password.")


def restore(file):
    tar, password = open_backup(file)
    names = [m.name for m in tar.getmembers()]
    print("Restoring:", ", ".join(names))
    for s in SERVICES:
        subprocess.run(["systemctl", "--user", "stop", s], capture_output=True)
    DIR.mkdir(mode=0o700, exist_ok=True)
    tar.extractall(DIR, filter="data")  # refuses absolute paths, "..", links outside DIR
    for n in names:
        os.chmod(DIR / n, 0o600)
    if not PASSWORD.exists():
        PASSWORD.write_text(password)
        os.chmod(PASSWORD, 0o600)
    for s in SERVICES:
        subprocess.run(["systemctl", "--user", "start", s], capture_output=True)
    print("Restored. Check:  jaf_sync.py status   and   systemctl --user is-active jaf-whatsapp jaf-web")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "set-password":
        set_password()
    elif cmd == "backup":
        backup()
    elif cmd in ("verify", "restore") and len(sys.argv) == 3:
        (verify if cmd == "verify" else restore)(sys.argv[2])
    else:
        sys.exit(__doc__)
