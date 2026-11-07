#!/usr/bin/env python3
"""Serves the JAF Tracker dashboard as a website from the server, on two local ports:
127.0.0.1:8787 for Tailscale Serve (your own devices, no password) and
127.0.0.1:8788 for Nginx (the internet, passkey required; the password only adds a passkey to a new device).
The port decides, not a request header, so a visitor from the internet can't pretend to be on Tailscale.

It serves the extension's own pages plus the data jaf_sync.py writes, keeps the shared read/hidden lists,
and accepts your login from the laptop extension at POST /api/session (secret key required).
"""
import base64
import getpass
import gzip
import hashlib
import hmac
import html
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.serialization import load_der_public_key

EXT = Path.home() / "jaf-tracker"
DATA = Path.home() / "jaf-web-data.json"
SHIM = Path(__file__).resolve().parent / "web-shim.js"
DEVICE_SESSION = Path(__file__).resolve().parent / "device-session.js"
SERVICE_WORKER = Path(__file__).resolve().parent / "sw.js"
SITE_PORT = 8787    # Tailscale Serve connects here. Never point anything public at this port.
PUBLIC_PORT = 8788  # Nginx connects here. Everything private needs the password.
SESSION =Path.home() / "jaf-sync" / "session.json"
KEY_FILE = Path.home() / "jaf-sync" / "key"
ALLOWED_COOKIE_HOSTS = {"campus.placements.iitb.ac.in", "sso.iitb.ac.in"}
READ_FILE = Path.home() / "jaf-sync" / "read.json"
READ_LOCK = threading.Lock()
MAX_READ = 5000
HIDDEN_FILE = Path.home() / "jaf-sync" / "hidden.json"
HIDDEN_LOCK = threading.Lock()

# Only these files are served; nothing else from the home folder is reachable.
FILES = {"dashboard.html", "jafs.html", "dashboard.css", "dashboard.js", "jafs.js", "common.js",
         "icons/icon16.png", "icons/icon48.png", "icons/icon128.png",
         "icons/icon192.png", "icons/icon512.png", "site.webmanifest"}
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".png": "image/png", ".json": "application/json",
         ".webmanifest": "application/manifest+json"}


# The website's password. Only a salted hash is stored; no password file means nobody can log in.
PASSWORD_FILE = Path.home() / "jaf-sync" / "web-password"
LOGIN_SECRET_FILE = Path.home() / "jaf-sync" / "web-login-secret"
INITIAL_PASSWORD_FILE = Path.home() / "jaf-sync" / "web-password.initial"
COOKIE = "__Host-jaf-login"
COOKIE_SECONDS = 15 * 60  # short, because the device key renews it and a copied cookie can't
PASSKEY_FILE = Path.home() / "jaf-sync" / "web-passkeys.json"
PASSKEY_LOCK = threading.Lock()
# Your own site settings, kept out of git: ~/jaf-sync/site.json, e.g.
#   {"domain": "jaf.example.com", "user": "me", "parentOrigin": "https://other.example.com"}
# domain: where the public site lives (passkeys are tied to it); user: the name passkeys are saved under;
# parentOrigin: a page of yours that keeps the login fresh in a hidden frame (optional).
SITE_FILE = Path.home() / "jaf-sync" / "site.json"
SITE = json.loads(SITE_FILE.read_text()) if SITE_FILE.exists() else {}
RP_ID = os.environ.get("JAF_RP_ID") or SITE.get("domain") or "jaf.example.com"
ORIGIN = os.environ.get("JAF_ORIGIN") or f"https://{RP_ID}"
SITE_USER = str(SITE.get("user") or "me")
PARENT_ORIGIN = str(SITE.get("parentOrigin") or "")
CHALLENGE_SECONDS = 5 * 60
MAX_CHALLENGES = 500
MAX_DEVICES_PER_PASSKEY = 10
MAX_PASSKEY_BODY = 16 * 1024
FAIL_WINDOW = 15 * 60    # failed logins are counted over this many seconds
MAX_FAILS_PER_CLIENT = 5
MAX_FAILS_TOTAL = 50     # across all clients, in case an attacker keeps changing address
MAX_PASSWORD_LENGTH = 1024
# Harmless and needed before login: browsers fetch the install manifest without cookies.
PUBLIC_FILES = {"device-session.js", "site.webmanifest", "icons/icon16.png", "icons/icon48.png", "icons/icon128.png",
                "icons/icon192.png", "icons/icon512.png"}
failed_logins = {}
failed_logins_lock = threading.Lock()
password_checks = threading.BoundedSemaphore(2)  # each check takes 16 MB, and this server has 1 GB


def hash_password(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, maxmem=64 * 1024 * 1024)


def write_private(path, data):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.replace(tmp, path)


def save_password(password):
    salt = secrets.token_bytes(16)
    write_private(PASSWORD_FILE, f"{salt.hex()}:{hash_password(password, salt).hex()}\n")
    LOGIN_SECRET_FILE.unlink(missing_ok=True)  # logs every device out


def password_matches(password):
    try:
        salt, expected = PASSWORD_FILE.read_text().strip().split(":")
        with password_checks:
            return hmac.compare_digest(hash_password(password, bytes.fromhex(salt)).hex(), expected)
    except (OSError, ValueError):
        return False  # no usable password set, so nobody gets in


def login_secret():
    if not LOGIN_SECRET_FILE.exists():
        write_private(LOGIN_SECRET_FILE, secrets.token_hex(32))
    return LOGIN_SECRET_FILE.read_text().encode()


def device_class(agent):
    """A cookie only works in the kind of browser and system that created it. It ignores versions, so updates
    don't matter. Browsers report this themselves, so it can be faked: the cookie's short life and the device
    key are the stronger protections."""
    system = next((n for n in ("Android", "iPhone", "iPad", "Windows", "Mac OS", "CrOS", "Linux") if n in agent), "other")
    browser = next((n for n in ("CriOS", "FxiOS", "Edg", "OPR", "SamsungBrowser", "Firefox", "Chrome", "Safari") if n in agent), "other")
    return f"{browser}|{system}"


def login_signature(expires, device):
    # "v2" means cookies from before passkeys, which lasted 400 days, no longer work.
    return hmac.new(login_secret(), f"v2|{expires}|{device}".encode(), hashlib.sha256).hexdigest()


# Passkeys. Each browser that logs in with one also gets a device key, which renews its short cookie without a
# fingerprint prompt. Attestation isn't requested, so the browser reports the new passkey's public key directly,
# and only ES256 (P-256) keys are accepted.
challenges = {}
challenges_lock = threading.Lock()


def from_base64(value):
    if not isinstance(value, str):
        raise ValueError
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def new_challenge(purpose):
    now = time.monotonic()
    with challenges_lock:
        for key in [k for k, (_, at) in challenges.items() if now - at >= CHALLENGE_SECONDS]:
            del challenges[key]
        if len(challenges) >= MAX_CHALLENGES:
            del challenges[next(iter(challenges))]
        value = secrets.token_urlsafe(32)
        challenges[value] = (purpose, now)
        return value


def take_challenge(value, purpose):
    """A challenge works once, for one purpose."""
    with challenges_lock:
        entry = challenges.pop(value, None) if isinstance(value, str) else None
    return bool(entry) and entry[0] == purpose and time.monotonic() - entry[1] < CHALLENGE_SECONDS


def load_passkeys():
    # A damaged file raises an error, which stops logins instead of overwriting it with an empty list.
    return json.loads(PASSKEY_FILE.read_text()) if PASSKEY_FILE.exists() else {"passkeys": []}


def p256_key(der):
    key = load_der_public_key(der)
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError
    return key


def check_ceremony(client_data_text, authenticator_data_text, kind, purpose):
    client_data = from_base64(client_data_text)
    authenticator_data = from_base64(authenticator_data_text)
    parsed = json.loads(client_data)
    if (parsed.get("type") != kind or parsed.get("crossOrigin") is True or parsed.get("origin") != ORIGIN
            or not take_challenge(parsed.get("challenge"), purpose) or len(authenticator_data) < 37
            or not hmac.compare_digest(authenticator_data[:32], hashlib.sha256(RP_ID.encode()).digest())
            # The person must be present (bit 0) and have passed the fingerprint, face or PIN check (bit 2).
            or authenticator_data[32] & 0x05 != 0x05):
        raise ValueError
    return client_data, authenticator_data, int.from_bytes(authenticator_data[33:37], "big")


def remember_device(passkey, device_key):
    der = from_base64(device_key)
    p256_key(der)
    device_id = base64.urlsafe_b64encode(hashlib.sha256(der).digest()).decode().rstrip("=")
    others = [d for d in passkey.get("devices", []) if d["id"] != device_id]
    passkey["devices"] = (others + [{"id": device_id, "publicKey": device_key}])[-MAX_DEVICES_PER_PASSKEY:]
    return device_id


def add_passkey(body):
    _, _, sign_count = check_ceremony(body.get("clientData"), body.get("authenticatorData"), "webauthn.create", "register")
    credential_id, public_key = body.get("credentialId"), body.get("publicKey")
    if not isinstance(credential_id, str) or not credential_id:
        raise ValueError
    p256_key(from_base64(public_key))
    with PASSKEY_LOCK:
        store = load_passkeys()
        if any(p["id"] == credential_id for p in store["passkeys"]):
            raise ValueError
        passkey = {"id": credential_id, "publicKey": public_key, "signCount": sign_count,
                   "addedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        device_id = remember_device(passkey, body.get("deviceKey"))
        store["passkeys"].append(passkey)
        write_private(PASSKEY_FILE, json.dumps(store))
    return device_id


def login_with_passkey(body):
    client_data, authenticator_data, sign_count = check_ceremony(
        body.get("clientData"), body.get("authenticatorData"), "webauthn.get", "session")
    with PASSKEY_LOCK:
        store = load_passkeys()
        passkey = next(p for p in store["passkeys"] if p["id"] == body.get("credentialId"))
        p256_key(from_base64(passkey["publicKey"])).verify(
            from_base64(body.get("signature")), authenticator_data + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()))
        # Synced passkeys always report 0. Otherwise the count must rise, or the passkey was cloned.
        if (sign_count or passkey["signCount"]) and sign_count <= passkey["signCount"]:
            raise ValueError
        passkey["signCount"] = sign_count
        device_id = remember_device(passkey, body.get("deviceKey"))
        write_private(PASSKEY_FILE, json.dumps(store))
    return device_id


def device_proves_itself(body):
    challenge = body.get("challenge")
    if not take_challenge(challenge, "session"):
        raise ValueError
    with PASSKEY_LOCK:
        devices = [d for p in load_passkeys()["passkeys"] for d in p.get("devices", [])]
    device = next(d for d in devices if d["id"] == body.get("deviceId"))
    raw = from_base64(body.get("signature"))
    if len(raw) != 64:
        raise ValueError
    signature = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    p256_key(from_base64(device["publicKey"])).verify(signature, challenge.encode(), ec.ECDSA(hashes.SHA256()))


def too_many_fails(client):
    now = time.monotonic()
    with failed_logins_lock:
        for key in list(failed_logins):
            failed_logins[key] = [t for t in failed_logins[key] if now - t < FAIL_WINDOW]
            if not failed_logins[key]:
                del failed_logins[key]
        total = sum(len(times) for times in failed_logins.values())
        return len(failed_logins.get(client, [])) >= MAX_FAILS_PER_CLIENT or total >= MAX_FAILS_TOTAL


def record_fail(client):
    with failed_logins_lock:
        failed_logins.setdefault(client, []).append(time.monotonic())


PAGE_STYLE = """
  :root { color-scheme: light dark; }
  body { margin: 0; min-height: 100svh; display: grid; place-items: center; padding: 16px; box-sizing: border-box;
         font: 16px/1.5 system-ui, sans-serif; background: Canvas; color: CanvasText; }
  main { width: 100%; max-width: 22rem; }
  h1 { margin: 0 0 1.25rem; font-size: 1.5rem; }
  label { display: block; font-weight: 600; }
  input { width: 100%; box-sizing: border-box; margin: .375rem 0 1rem; padding: .625rem .75rem; font: inherit;
          border: 1px solid GrayText; border-radius: .375rem; background: Field; color: FieldText; }
  button { width: 100%; min-height: 2.75rem; font: inherit; font-weight: 600; cursor: pointer; border: 0;
           border-radius: .375rem; background: CanvasText; color: Canvas; }
  button.secondary { border: 1px solid GrayText; background: transparent; color: CanvasText; }
  h2 { margin: 2rem 0 .75rem; padding-top: 1.5rem; border-top: 1px solid GrayText; font-size: 1rem; }
  p { margin: 1rem 0 0; }
  p:empty { display: none; }
"""


def login_page():
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Log in | JAF Tracker</title><style>{PAGE_STYLE}</style></head>
<body><main><h1>JAF Tracker</h1>
<button id="passkey" type="button">Log in with passkey</button>
<p id="message" role="alert"></p>
<h2>New device?</h2>
<form id="add-device">
  <input type="text" name="username" value="{html.escape(SITE_USER)}" autocomplete="username" hidden>
  <label for="password">Password</label>
  <input id="password" type="password" name="password" autocomplete="current-password" required>
  <button class="secondary" type="submit">Create a passkey here</button>
</form></main>
<script src="/device-session.js"></script>
<script>
  const message = document.getElementById('message');
  const buttons = document.querySelectorAll('button');
  const setBusy = busy => buttons.forEach(button => {{ button.disabled = busy; }});
  const explain = error => error?.name === 'InvalidStateError' ? 'This device already has a passkey. Use the button above.'
    : error?.name === 'NotAllowedError' ? "The passkey step didn't finish. Please try again." : "That didn't work. Please try again.";
  async function run(action) {{
    message.textContent = '';
    setBusy(true);
    try {{
      const status = await action();
      if (status === 200) location.replace('/');
      else if (status === 429) message.textContent = 'Too many failed tries. Please wait 15 minutes.';
      else if (status === 401) message.textContent = "That didn't work. Please check and try again.";
      else message.textContent = "That didn't work. Please try again.";
    }} catch (error) {{
      message.textContent = explain(error);
    }} finally {{
      setBusy(false);
    }}
  }}
  document.getElementById('passkey').addEventListener('click', () => run(deviceSession.login));
  document.getElementById('add-device').addEventListener('submit', event => {{
    event.preventDefault();
    run(() => deviceSession.addDevice(document.getElementById('password').value));
  }});
  if (!deviceSession.supported) {{
    setBusy(true);
    message.textContent = "This browser can't use passkeys, so it can't log in here.";
  }}
  // This device still holds its key? Then go straight to the site.
  window.addEventListener('pageshow', async () => {{ if (await deviceSession.refresh()) location.replace('/'); }});
</script></body></html>""".encode()


SYNC_GAP = 60  # seconds between syncs started from the website, so the portal is never hammered
last_sync_start = 0.0


STALE_AFTER = 5 * 60     # opening the site starts a sync if the last one is older than this
PUSH_POLL = 2            # seconds between checks for changed data, per open page
PUSH_PING = 25           # keep-alive comment, so proxies don't drop an idle stream
PUSH_MAX_AGE = 30 * 60   # end a stream after 30 min; the browser reconnects by itself
MAX_STREAMS = 20
streams = threading.BoundedSemaphore(MAX_STREAMS)


def data_stamp():
    """Changes whenever the website's data, the shared read list or the hidden list changes."""
    return tuple(f.stat().st_mtime_ns if f.exists() else 0 for f in (DATA, READ_FILE, HIDDEN_FILE))


def last_sync_age():
    try:
        last = json.loads(DATA.read_text()).get("lastSync")
        from datetime import datetime, timezone
        return (datetime.now(timezone.utc) - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds()
    except Exception:  # noqa: BLE001 - no data yet, or unreadable: treat as stale
        return float("inf")


def sync_running():
    r = subprocess.run(["systemctl", "--user", "is-active", "jaf-sync.service"], capture_output=True, text=True)
    return r.stdout.strip() in ("active", "activating", "reloading")


def service_worker():
    """sw.js with the list of app files and a fingerprint of them, so any change reinstalls the offline copy."""
    fingerprint = hashlib.sha1(SHIM.read_bytes())
    for name in sorted(FILES):
        fingerprint.update((EXT / name).read_bytes())
    return (SERVICE_WORKER.read_text()
            .replace("__VERSION__", fingerprint.hexdigest()[:16])
            .replace('"__FILES__"', json.dumps(sorted(FILES) + ["web-shim.js"]))
            .encode())


class Site(BaseHTTPRequestHandler):
    def on_tailscale(self):
        return self.server.server_address[1] == SITE_PORT

    def logged_in(self):
        if self.on_tailscale():
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                expires, _, signature = value.partition(".")
                return (expires.isdigit() and int(expires) > time.time()
                        and hmac.compare_digest(signature.encode(), login_signature(int(expires), self.device()).encode()))
        return False

    def device(self):
        return device_class(self.headers.get("User-Agent", ""))

    def login_cookie(self):
        expires = int(time.time()) + COOKIE_SECONDS
        return (f"{COOKIE}={expires}.{login_signature(expires, self.device())}; Path=/; Secure; HttpOnly; "
                f"SameSite=Lax; Max-Age={COOKIE_SECONDS}")

    def client(self):
        # Nginx always overwrites X-Real-IP, so a visitor can't fake it. Tailscale Serve doesn't set it.
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def deny(self):
        if "text/html" in self.headers.get("Accept", ""):
            self.redirect("/login")
            return
        body = b'{"error":"login required"}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location, cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def send_page(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_status(self, status, cookie=None, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def handle_passkey(self, route):
        # Set only by Nginx. A password must never travel unencrypted, even before HTTPS is switched on.
        if self.on_tailscale() or self.headers.get("X-Forwarded-Proto") == "http":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not 0 <= length <= MAX_PASSKEY_BODY:
            self.send_error(413)
            return
        client = self.client()
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
            if route == "challenge":
                self.send_status(200, obj={"challenge": new_challenge("session"), "rpId": RP_ID})
            elif route == "refresh":
                device_proves_itself(body)
                self.send_status(204, self.login_cookie())
            elif route == "login":
                # No lockout: signatures can't be guessed, and it would only let strangers block real logins.
                self.send_status(200, self.login_cookie(), {"deviceId": login_with_passkey(body)})
            elif route == "register":
                self.send_status(200, self.login_cookie(), {"deviceId": add_passkey(body)})
            elif route != "register-options":
                self.send_error(404)
            elif too_many_fails(client):
                self.send_status(429)
            else:
                password = body.get("password")
                if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LENGTH or not password_matches(password):
                    raise ValueError
                with PASSKEY_LOCK:
                    existing = [p["id"] for p in load_passkeys()["passkeys"]]
                self.send_status(200, obj={"challenge": new_challenge("register"), "rpId": RP_ID, "existing": existing})
        except (ValueError, KeyError, TypeError, StopIteration, InvalidSignature):
            if route == "register-options":
                record_fail(client)
            self.send_status(401)

    def do_GET(self):
        path = self.path.split("?", 1)[0].lstrip("/") or "dashboard.html"
        if path == "login" and self.on_tailscale():
            self.redirect("/")  # no password here, so there's nothing to log in to
            return
        if path == "login":
            self.send_page(login_page())
            return
        if path not in PUBLIC_FILES and not self.logged_in():
            self.deny()
            return
        if path == "api/read":
            self.send_json({"readIds": self.read_ids()})
            return
        if path == "api/hidden":
            self.send_json({"hidden": self.hidden()})
            return
        if path == "api/sync":
            self.send_json({"running": sync_running()})
            return
        if path == "api/events":
            self.push_stream()
            return
        if path == "device-session.js":
            body = (DEVICE_SESSION.read_text().replace('"__JAF_USER__"', json.dumps(SITE_USER))
                    .replace('"__JAF_PARENT_ORIGIN__"', json.dumps(PARENT_ORIGIN)).encode())
        elif path == "data.json":
            body = DATA.read_bytes() if DATA.exists() else b"{}"
        elif path == "web-shim.js":
            body = SHIM.read_bytes()
        elif path == "sw.js":
            body = service_worker()
        elif path in FILES:
            body = (EXT / path).read_bytes()
        else:
            self.send_error(404)
            return
        if path.endswith(".html"):
            # Load the shim (stands in for the chrome.* APIs) before the page's own module.
            body = body.replace(b'<script type="module"', b'<link rel="modulepreload" href="common.js">\n  '
                                b'<script src="device-session.js" data-keep-alive></script>\n  '
                                b'<script src="web-shim.js"></script>\n  <script type="module"', 1)
        # Browsers keep a copy and ask "changed?" each time; unchanged files come back as a tiny 304.
        etag = '"%s"' % hashlib.sha1(body).hexdigest()[:20]
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(Path(path).suffix, "application/octet-stream"))
        if path.endswith(".html"):
            # Portal data is shown on these pages, so only the site's own script files may ever run.
            self.send_header("Content-Security-Policy", "script-src 'self'; object-src 'none'; base-uri 'none'; "
                             "frame-ancestors 'none'")
        # The app itself can come from this device's copy, but data.json always reaches the server.
        if len(body) > 1024 and not path.endswith(".png") and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, 6)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
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

    def push_stream(self):
        """Server-Sent Events: tells an open page "update" the moment new data lands (a sync finished, or a post
        was read / a company hidden on another device). Nothing but that one word is sent."""
        if not streams.acquire(blocking=False):
            self.send_error(503)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 5000\n\n")
            stamp, started, pinged = data_stamp(), time.monotonic(), time.monotonic()
            while time.monotonic() - started < PUSH_MAX_AGE:
                time.sleep(PUSH_POLL)
                now_stamp = data_stamp()
                if now_stamp != stamp:
                    stamp = now_stamp
                    self.wfile.write(b"data: update\n\n")
                    pinged = time.monotonic()
                elif time.monotonic() - pinged > PUSH_PING:
                    self.wfile.write(b": ping\n\n")
                    pinged = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the page was closed
        finally:
            streams.release()

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        if route.startswith("/passkey/"):
            self.handle_passkey(route.removeprefix("/passkey/"))
            return
        # The extension's login endpoint has its own secret key, checked below, and is for Tailscale only.
        if route == "/api/session" and not self.on_tailscale():
            self.send_error(404)
            return
        if route != "/api/session" and not self.logged_in():
            self.deny()
            return
        if route == "/api/sync":
            # "Sync now" on the website, or ?stale=1 when the site is opened: the same read-only sync the timer runs.
            # With ?stale=1 it only starts if the last sync is over 5 min old. Never while one runs, at most once a minute.
            global last_sync_start
            wanted = "stale=1" not in self.path or last_sync_age() > STALE_AFTER
            started = False
            if wanted and not sync_running() and time.monotonic() - last_sync_start > SYNC_GAP:
                last_sync_start = time.monotonic()
                subprocess.Popen(["systemctl", "--user", "start", "--no-block", "jaf-sync.service"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                started = True
            self.send_json({"running": started or sync_running(), "started": started})
            return
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


def set_password():
    """Lets you choose the password. It's typed in hidden and never saved as plain text."""
    password = getpass.getpass("New website password: ")
    if len(password) < 12:
        sys.exit("Please use at least 12 characters.")
    if password != getpass.getpass("Type it again: "):
        sys.exit("Those didn't match.")
    save_password(password)
    INITIAL_PASSWORD_FILE.unlink(missing_ok=True)
    print("Password changed. All devices are logged out; restarting isn't needed.")


def init_password():
    """Creates a random first password, kept in a private file so it's never printed."""
    if PASSWORD_FILE.exists():
        print("A password is already set; nothing changed.")
        return
    password = secrets.token_urlsafe(18)
    save_password(password)
    write_private(INITIAL_PASSWORD_FILE, password + "\n")
    print(f"Password created and saved in {INITIAL_PASSWORD_FILE}")


if __name__ == "__main__":
    if sys.argv[1:] == ["set-password"]:
        set_password()
        sys.exit()
    if sys.argv[1:] == ["init-password"]:
        init_password()
        sys.exit()
    print(f"JAF Tracker website on 127.0.0.1:{SITE_PORT} (Tailscale) and :{PUBLIC_PORT} (Nginx)", flush=True)
    public = ThreadingHTTPServer(("127.0.0.1", PUBLIC_PORT), Site)
    threading.Thread(target=public.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", SITE_PORT), Site).serve_forever()
