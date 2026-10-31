#!/usr/bin/env python3
"""WhatsApp for the JAF Tracker, from your spare number (linked to this server) to your main number.

  jaf_whatsapp.py serve                 the always-on service (systemd: jaf-whatsapp.service):
                                          - sends what jaf_sync.py queues in ~/jaf-sync/wa-outbox, in order
                                          - answers commands texted from your main number (and nobody else):
                                            today · open · sync · hide <company> · unhide <company> · status · catchup
  jaf_whatsapp.py pair <spare number>   links the spare number: prints a pairing code to type on the spare phone
                                        (stop the service first: systemctl --user stop jaf-whatsapp)

Runs with ~/jaf-sync/wa-venv/bin/python (neonize, a Python wrapper of the whatsmeow library).
Incoming messages are never stored: a command is read, answered and forgotten.
"""
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

logging.disable(logging.WARNING)  # neonize logs a lot

from neonize.client import NewClient  # noqa: E402
from neonize.events import ConnectedEv, DisconnectedEv, LoggedOutEv, MessageEv, PairStatusEv  # noqa: E402
from neonize.utils import build_jid  # noqa: E402
from neonize.utils.enum import ChatPresence, ChatPresenceMedia, ReceiptType  # noqa: E402

DIR = Path.home() / "jaf-sync"
sys.path.insert(0, str(DIR))
import jaf_sync as J  # noqa: E402  (shares the tracker's data and message builders)

DB = str(DIR / "whatsapp.db")
OUTBOX = DIR / "wa-outbox"
STATUS = DIR / "wa-status.json"
MAX_COMMAND_AGE = 600  # seconds; older commands (delivered after a restart) are ignored
GAP_SECONDS = (2.0, 5.0)  # random pause between messages
TYPING_CHARS_PER_SECOND = 60  # how long "typing…" shows, from the message length…
TYPING_SECONDS = (1.5, 6.0)  # …kept within these bounds

HELP = ("🤖 *JAF Tracker commands*\n"
        "• *today*: today's tests, PPTs and JAF deadlines\n"
        "• *open*: JAFs you can still sign\n"
        "• *sync*: check the portal and blog right now\n"
        "• *hide <company>* / *unhide <company>*: stop or restart updates for a company\n"
        "• *status*: is everything working?\n"
        "• *catchup*: what's coming up and the last day's updates")


def digits(number):
    return re.sub(r"\D", "", number or "")


def main_number():
    return digits((J.load(J.CONFIG, {}).get("whatsapp") or {}).get("to"))


def write_status(connected, error=None):
    J.save(STATUS, {"at": time.time(), "connected": connected, "error": error}, private=True)


# ---------- commands ----------

def answer(text):
    """The reply (one or more messages) to a command; anything unrecognised gets the command list."""
    words = text.strip().split(None, 1)
    if not words:
        return None
    cmd, arg = words[0].lower().strip("/!.,"), (words[1] if len(words) > 1 else "")
    state = J.load(J.STATE, {})
    if cmd == "today":
        return [J.today_text(state)]
    if cmd == "open":
        return [J.open_text(state)]
    if cmd in ("hide", "unhide"):
        return [J.set_hidden(arg, cmd == "hide")]
    if cmd == "status":
        return [J.status_text(state) + "\n✅ WhatsApp service: connected"]
    if cmd in ("catchup", "catch-up"):
        _, items, _ = J.catchup_parts(state)
        return items
    return [HELP]


def run_sync():
    """A full sync now (portal + blog), then a short report. New updates arrive as their own messages."""
    before = J.load(J.STATE, {})
    old_posts = {p["id"] for p in before.get("posts") or []}
    r = subprocess.run(["systemctl", "--user", "start", "jaf-sync.service"], capture_output=True, text=True, timeout=600)
    state = J.load(J.STATE, {})
    if r.returncode:
        return f"⚠️ Sync didn't run: {(r.stderr or r.stdout).strip()[-200:]}"
    auth = state.get("auth") or {}
    if not auth.get("ok"):
        return f"🔑 Sync stopped: {auth.get('msg', 'login needed')} Log in on your laptop."
    new = [p for p in J.relevant_posts(state.get("posts") or [], J.active_jobs(state.get("jobs") or {}))
           if p["id"] not in old_posts]
    lines = [f"✅ *Synced* {J.clock(state['lastSync'])}",
             f"New updates for your companies: {len(new)}" + (" (sent above)" if new else ""),
             f"Open JAFs you can sign: {len(J.signable(state))}",
             "Blog: " + ("OK" if not state.get("blogError") else state["blogError"])]
    if state.get("error"):
        lines.append(f"⚠️ {state['error']}")
    if new and J.in_quiet():
        lines.append("(It's quiet hours, so the new updates will arrive at 7 am.)")
    return "\n".join(lines)


def respond(send, chat, text):
    """Works out the reply off WhatsApp's event thread (a sync takes a while) and sends it."""
    words = text.strip().split(None, 1)
    if words and words[0].lower().strip("/!.,") == "sync":
        send(chat, ["🔄 Syncing with the placement portal and blog…"])
        send(chat, [run_sync()])
        return
    reply = answer(text)
    if reply:
        send(chat, reply)


def from_main(client, src, main):
    """True only for a private message from your main number."""
    if src.IsFromMe or src.IsGroup or not main:
        return False
    if main in (src.Sender.User, src.SenderAlt.User):
        return True
    try:  # WhatsApp may identify the sender by a privacy ID instead of the number
        pn = client.get_pn_from_lid(src.Sender)
        return bool(pn) and pn.User == main
    except Exception:  # noqa: BLE001
        return False


def message_text(msg):
    m = msg.Message
    return m.conversation or m.extendedTextMessage.text or ""


# ---------- the service ----------

def serve():
    main = main_number()
    client = NewClient(DB)
    connected = threading.Event()
    send_lock = threading.Lock()

    def presence(jid, state):
        try:
            client.send_chat_presence(jid, state, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
        except Exception:  # noqa: BLE001 - cosmetic only, never block a message
            pass

    def send(jid, texts):
        """One message at a time, in order: shows "typing…" for a bit, sends, then pauses a random 2–5 s."""
        with send_lock:
            for t in texts:
                for part in J.chunks(t, J.WA_CHUNK):
                    presence(jid, ChatPresence.CHAT_PRESENCE_COMPOSING)
                    lo, hi = TYPING_SECONDS
                    time.sleep(min(hi, max(lo, len(part) / TYPING_CHARS_PER_SECOND)) + random.uniform(0, 1))
                    client.send_message(jid, part)
                    presence(jid, ChatPresence.CHAT_PRESENCE_PAUSED)
                    time.sleep(random.uniform(*GAP_SECONDS))

    def mark_read(ev):
        """Blue ticks on your command, a moment after it arrives."""
        time.sleep(random.uniform(0.5, 2.0))
        try:
            src = ev.Info.MessageSource
            client.mark_read(ev.Info.ID, chat=src.Chat, sender=src.Sender, receipt=ReceiptType.READ)
        except Exception:  # noqa: BLE001
            pass

    @client.event(ConnectedEv)
    def _connected(_c, _ev):
        connected.set()
        write_status(True)

    @client.event(DisconnectedEv)
    def _disconnected(_c, _ev):
        connected.clear()

    @client.event(LoggedOutEv)
    def _logged_out(_c, _ev):
        write_status(False, "the spare WhatsApp number was logged out: link it again")
        os._exit(3)

    def _not_linked(_c, _data):
        write_status(False, "the spare WhatsApp number isn't linked: run 'jaf_whatsapp.py pair <number>'")
        os._exit(3)

    client.event.qr(_not_linked)

    @client.event(MessageEv)
    def _message(c, ev):
        src = ev.Info.MessageSource
        if not from_main(c, src, main):
            return  # not you: ignore, store nothing
        ts = ev.Info.Timestamp / 1000 if ev.Info.Timestamp > 1e12 else ev.Info.Timestamp
        if time.time() - ts > MAX_COMMAND_AGE:
            return
        text = message_text(ev)

        def handle():
            mark_read(ev)
            respond(send, src.Chat, text)

        threading.Thread(target=handle, daemon=True).start()

    threading.Thread(target=client.connect, daemon=True).start()
    OUTBOX.mkdir(mode=0o700, exist_ok=True)
    last_beat = 0
    while True:
        time.sleep(2)
        if not connected.is_set():
            continue
        if time.time() - last_beat > 60:
            write_status(True)
            last_beat = time.time()
        for f in sorted(OUTBOX.glob("*.json")):  # oldest first
            try:
                messages = json.loads(f.read_text()).get("messages", [])
            except (OSError, ValueError):
                f.unlink(missing_ok=True)
                continue
            jid = build_jid(main_number())
            while messages:  # drop each message once WhatsApp has accepted it, so nothing is sent twice
                send(jid, [messages[0]])
                messages = messages[1:]
                if messages:
                    J.save(f, {"messages": messages}, private=True)
            f.unlink(missing_ok=True)


# ---------- linking ----------

def pair(number):
    client = NewClient(DB)
    linked, needs_link = threading.Event(), threading.Event()
    client.event(PairStatusEv)(lambda _c, _ev: print("Linked. Finishing setup…", flush=True))
    client.event(ConnectedEv)(lambda _c, _ev: linked.set())
    client.event.qr(lambda _c, _data: needs_link.set())
    threading.Thread(target=client.connect, daemon=True).start()
    while not (linked.is_set() or needs_link.is_set()):
        time.sleep(0.5)
    if not linked.is_set():
        print(f"PAIRING CODE: {client.PairPhone(digits(number), True)}", flush=True)
        if not linked.wait(300):
            raise SystemExit("the code wasn't entered in time; run pair again")
    print("Done: the spare number is linked to this server.", flush=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    try:
        if args == ["serve"]:
            serve()
        elif len(args) == 2 and args[0] == "pair":
            pair(args[1])
        else:
            raise SystemExit(__doc__)
    except SystemExit as e:
        if e.code not in (None, 0):
            print(e.code if isinstance(e.code, str) else "failed", file=sys.stderr, flush=True)
        os._exit(e.code if isinstance(e.code, int) else 1)
    os._exit(0)  # don't wait for WhatsApp's background threads to wind down
