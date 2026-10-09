"""Test de bout en bout du moteur, sans serveur EMAK ni carte son.

Un faux PBX (fakepbx.py) joue le rôle d'EMAK : authentification Digest et messagerie (MWI).
Un second baresip, « Bob », joue l'interlocuteur. On vérifie : enregistrement, refus du mauvais
mot de passe, appels sortant et entrant, DTMF, muet, attente, refus, et que le mot de passe ne
reste pas sur le disque.

    sudo apt install baresip-core python3-gi
    python3 tests/test_engine.py

Variables facultatives : LIGNE_EMAK_BARESIP (binaire), LIGNE_EMAK_MODULE_DIR (modules).
Ports utilisés sur 127.0.0.1 : 5090 (PBX), 5072 (Bob), 4455 (contrôle de Bob).
"""
import atexit
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from gi.repository import GLib  # noqa: E402

from ligne_emak.engine import MODULE_DIRS, Account, Engine, find_baresip  # noqa: E402

WORK = tempfile.mkdtemp(prefix="ligne-emak-test-")
atexit.register(shutil.rmtree, WORK, True)

BARESIP = find_baresip()
MODDIR = os.environ.get("LIGNE_EMAK_MODULE_DIR") or next((d for d in MODULE_DIRS if os.path.isdir(d)), None)
if not BARESIP or not MODDIR:
    sys.exit("baresip introuvable : sudo apt install baresip-core")


def tone(path, freq):
    w = wave.open(path, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / 8000)))
                           for i in range(8000 * 20)))
    w.close()


tone(os.path.join(WORK, "a.wav"), 440)
tone(os.path.join(WORK, "b.wav"), 660)
os.makedirs(os.path.join(WORK, "run"), mode=0o700)
os.environ.update({
    "LIGNE_EMAK_BARESIP": BARESIP,
    "LIGNE_EMAK_MODULE_DIR": MODDIR,
    "LIGNE_EMAK_AUDIO_SOURCE": "aufile," + os.path.join(WORK, "a.wav"),
    "LIGNE_EMAK_AUDIO_PLAYER": "aufile," + os.path.join(WORK, "alice_out.wav"),
    "LIGNE_EMAK_EXTRA_MODULES": "aufile.so",
    "LIGNE_EMAK_REGINT": "10",          # ré-enregistrement rapide : la messagerie s'abonne plus tôt
    "XDG_RUNTIME_DIR": os.path.join(WORK, "run"),
})

results = []


def check(name, ok, info=""):
    results.append((name, ok))
    print(("OK    " if ok else "ÉCHEC ") + name + (("  — " + info) if info else ""), flush=True)


# ---------------------------------------------------------------- faux PBX + Bob
pbx_log_path = os.path.join(WORK, "pbx.log")
pbx = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "fakepbx.py"), "5090", "101", "Secret123"],
                       stdout=open(pbx_log_path, "w"), stderr=subprocess.STDOUT)
bobdir = os.path.join(WORK, "bob")
os.makedirs(bobdir)
with open(os.path.join(bobdir, "config"), "w") as f:
    f.write("\n".join([
        "module_path " + MODDIR,
        "sip_listen 127.0.0.1:5072",
        "audio_player aufile," + os.path.join(WORK, "bob_out.wav"),
        "audio_source aufile," + os.path.join(WORK, "b.wav"),
        "audio_alert aufile," + os.path.join(WORK, "bob_ring.wav"),
        "ctrl_tcp_listen 127.0.0.1:4455",
        "module g711.so", "module aufile.so",
        "module_tmp account.so", "module_app menu.so", "module_app ctrl_tcp.so", "",
    ]))
with open(os.path.join(bobdir, "accounts"), "w") as f:
    f.write("<sip:bob@127.0.0.1:5072>;regint=0;answermode=manual;mwi=no\n")
bob = subprocess.Popen([BARESIP, "-f", bobdir], stdout=open(os.path.join(WORK, "bob.log"), "w"),
                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
atexit.register(lambda: [p.kill() for p in (pbx, bob)])


class Ctl:
    """Client minimal de la console de contrôle de baresip (pour piloter Bob)."""

    def __init__(self, port):
        for _ in range(50):
            try:
                self.s = socket.create_connection(("127.0.0.1", port))
                break
            except OSError:
                time.sleep(0.1)
        else:
            sys.exit("Bob (baresip) ne démarre pas — voir %s" % os.path.join(WORK, "bob.log"))
        self.s.setblocking(False)
        self.buf = b""
        self.events = []
        GLib.io_add_watch(self.s.fileno(), GLib.IO_IN, self._read)

    def _read(self, *a):
        self.buf += self.s.recv(65536)
        while b":" in self.buf:
            i = self.buf.index(b":")
            n = int(self.buf[:i])
            if len(self.buf) < i + 2 + n:
                break
            m = json.loads(self.buf[i + 1:i + 1 + n])
            self.buf = self.buf[i + 2 + n:]
            if m.get("event"):
                self.events.append(m)
        return True

    def cmd(self, c, p=None):
        d = {"command": c, "token": "bob"}
        if p:
            d["params"] = p
        d = json.dumps(d).encode()
        self.s.sendall(b"%d:%s," % (len(d), d))


ctx = GLib.MainContext.default()


def pump(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ctx.iteration(False)
        if cond():
            return True
        time.sleep(0.01)
    return False


bobc = Ctl(4455)
has = lambda evs, t: any(e["type"] == t for e in evs)  # noqa: E731

# ---------------------------------------------------------------- Alice = le moteur de l'application
eng = Engine(os.path.join(WORK, "config"), os.path.join(WORK, "cache"))
states, calls, mwis = [], [], []
eng.connect("status", lambda e, s, d: states.append((s, d)))
eng.connect("call-event", lambda e, ev: calls.append(ev))
eng.connect("mwi", lambda e, n, o: mwis.append((n, o)))

eng.start(Account("101", "Mauvais", "127.0.0.1:5090", "udp"))
check("mauvais mot de passe refusé", pump(lambda: eng.state == "failed", 15), str(states))

eng.start(Account("101", "Secret123", "127.0.0.1:5090", "udp", "Réception"))
check("enregistrement (Digest) réussi", pump(lambda: eng.state == "online", 15), str(states[-3:]))
check("mot de passe effacé du disque après lecture",
      not os.path.exists(os.path.join(eng.run_dir, "accounts")))
check("dossier d'exécution privé (0700)", oct(os.stat(eng.run_dir).st_mode & 0o777) == "0o700")
check("messagerie : 2 nouveaux, 1 ancien", pump(lambda: (2, 1) in mwis, 30), str(mwis))

# Appel sortant
eng.dial("sip:bob@127.0.0.1:5072")
check("Bob sonne", pump(lambda: has(bobc.events, "CALL_INCOMING")))
bobc.cmd("accept")
check("appel sortant établi", pump(lambda: has(calls, "CALL_ESTABLISHED")))
resp = {}
eng.send_dtmf("5", lambda ok, d: resp.setdefault("dtmf", ok))
check("DTMF « 5 » reçu par Bob",
      pump(lambda: any(e["type"] == "CALL_DTMF_START" and e.get("param") == "5" for e in bobc.events), 5))
eng.toggle_mute(lambda ok, d: resp.setdefault("mute", d))
pump(lambda: "mute" in resp, 3)
check("muet", "call muted" in resp.get("mute", ""), repr(resp.get("mute")))
eng.toggle_mute(lambda ok, d: resp.setdefault("unmute", d))
pump(lambda: "unmute" in resp, 3)
eng.hold(lambda ok, d: resp.setdefault("hold", ok))
pump(lambda: "hold" in resp, 3)
eng.resume(lambda ok, d: resp.setdefault("resume", ok))
pump(lambda: "resume" in resp, 3)
check("attente puis reprise", resp.get("hold") is True and resp.get("resume") is True, str(resp))
eng.hangup()
check("raccrocher", pump(lambda: has(calls, "CALL_CLOSED")))

# Appel entrant (Bob appelle le contact enregistré d'Alice)
m = re.findall(r"contact=<(sip:[^>]+)>", open(pbx_log_path).read())
contact = m[-1] if m else None
calls.clear()
bobc.cmd("dial", contact)
check("appel entrant reçu", pump(lambda: has(calls, "CALL_INCOMING")), str(contact))
eng.accept()
check("répondre", pump(lambda: has(calls, "CALL_ESTABLISHED")))
bobc.cmd("hangup")
check("l'appelant raccroche", pump(lambda: has(calls, "CALL_CLOSED")))

calls.clear()
bobc.events.clear()
bobc.cmd("dial", contact)
pump(lambda: has(calls, "CALL_INCOMING"))
eng.hangup()
check("refuser un appel", pump(lambda: has(bobc.events, "CALL_CLOSED")))

eng.stop()
check("arrêt propre", pump(lambda: eng.state == "stopped" and not eng.running, 8))

print("\n%d/%d vérifications réussies" % (sum(ok for _, ok in results), len(results)))
sys.exit(0 if all(ok for _, ok in results) else 1)
