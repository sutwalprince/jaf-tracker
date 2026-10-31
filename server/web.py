#!/usr/bin/env python3
"""Serves the JAF Tracker dashboard as a website from the server, on 127.0.0.1:8787 only
(reachable just from your own devices, through Tailscale Serve).

It serves the extension's own pages plus the data jaf_sync.py writes, keeps the shared read/hidden lists,
and accepts your login from the laptop extension at POST /api/session (secret key required).
"""
import hmac
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

EXT = Path.home() / "jaf-tracker"
DATA = Path.home() / "jaf-web-data.json"
SHIM = Path(__file__).resolve().parent / "web-shim.js"
SITE_PORT = 8787
SESSION = Path.home() / "jaf-sync" / "session.json"
KEY_FILE = Path.home() / "jaf-sync" / "key"
ALLOWED_COOKIE_HOSTS = {"campus.placements.iitb.ac.in", "sso.iitb.ac.in"}
READ_FILE = Path.home() / "jaf-sync" / "read.json"
READ_LOCK = threading.Lock()
MAX_READ = 5000
HIDDEN_FILE = Path.home() / "jaf-sync" / "hidden.json"
HIDDEN_LOCK = threading.Lock()

# Only these files are served; nothing else from the home folder is reachable.
FILES = {"dashboard.html", "jafs.html", "dashboard.css", "dashboard.js", "jafs.js", "common.js",
         "icons/icon16.png", "icons/icon48.png", "icons/icon128.png"}
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".png": "image/png", ".json": "application/json"}


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0].lstrip("/") or "dashboard.html"
        if path == "api/read":
            self.send_json({"readIds": self.read_ids()})
            return
        if path == "api/hidden":
            self.send_json({"hidden": self.hidden()})
            return
        if path == "data.json":
            body = DATA.read_bytes() if DATA.exists() else b"{}"
        elif path == "web-shim.js":
            body = SHIM.read_bytes()
        elif path in FILES:
            body = (EXT / path).read_bytes()
        else:
            self.send_error(404)
            return
        if path.endswith(".html"):
            # Load the shim (stands in for the chrome.* APIs) before the page's own module.
            body = body.replace(b'<script type="module"', b'<script src="web-shim.js"></script>\n  <script type="module"', 1)
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(Path(path).suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_ids(self, add=()):
        """The shared list of posts you've read, on any device. Only grows (posts are never un-read)."""
        with READ_LOCK:
            ids = set(json.loads(READ_FILE.read_text())) if READ_FILE.exists() else set()
            new = {str(i) for i in add if str(i).isdigit()} - ids
            if new:
                ids |= new
                tmp = READ_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(sorted(ids, key=int)[-MAX_READ:]))
                os.replace(tmp, READ_FILE)
            return sorted(ids, key=int)

    def hidden(self, hide=(), unhide=()):
        """Companies whose updates you've hidden, shared by all your devices and jaf_sync.py."""
        with HIDDEN_LOCK:
            names = set(json.loads(HIDDEN_FILE.read_text())) if HIDDEN_FILE.exists() else set()
            new = (names | {str(n)[:200] for n in hide}) - {str(n) for n in unhide}
            if new != names:
                tmp = HIDDEN_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(sorted(new)))
                os.replace(tmp, HIDDEN_FILE)
            return sorted(new)

    def do_POST(self):
        if self.path == "/api/hidden":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if 0 < length <= 64 * 1024 else {}
            except ValueError:
                body = {}
            lists = [body.get(k) if isinstance(body.get(k), list) else [] for k in ("hide", "unhide")]
            self.send_json({"hidden": self.hidden(*lists)})
            return
        if self.path == "/api/read":
            length = int(self.headers.get("Content-Length", 0))
            try:
                add = json.loads(self.rfile.read(length)).get("add", []) if 0 < length <= 512 * 1024 else []
            except ValueError:
                add = []
            self.send_json({"readIds": self.read_ids(add if isinstance(add, list) else [])})
            return
        # Your login (portal token + IITB cookies) from the laptop extension, for jaf_sync.py.
        if self.path != "/api/session":
            self.send_error(404)
            return
        expected = KEY_FILE.read_text().strip() if KEY_FILE.exists() else ""
        given = self.headers.get("X-JAF-Key", "")
        if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
            self.send_error(403)
            return
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= 1024 * 1024:
            self.send_error(413)
            return
        try:
            body = json.loads(self.rfile.read(length))
            token, cookies = body["token"], body["cookies"]
            if not isinstance(token, str) or not isinstance(cookies, list):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            self.send_error(400)
            return
        # Only cookies for the portal and IITB SSO are kept.
        cookies = [c for c in cookies if str(c.get("domain", "")).lstrip(".") in ALLOWED_COOKIE_HOSTS]
        tmp = SESSION.with_suffix(".tmp")
        tmp.write_text(json.dumps({"token": token, "cookies": cookies, "receivedAt": body.get("sentAt")}))
        os.chmod(tmp, 0o600)
        os.replace(tmp, SESSION)
        # Sync right away with the fresh login.
        subprocess.Popen(["systemctl", "--user", "start", "--no-block", "jaf-sync.service"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"JAF Tracker website on http://127.0.0.1:{SITE_PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", SITE_PORT), Site).serve_forever()
