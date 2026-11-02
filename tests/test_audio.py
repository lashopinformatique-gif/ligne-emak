"""Choix des appareils audio : tests des décisions, puis routage réel des sons dans baresip.

    sudo apt install baresip-core python3-gi
    python3 tests/test_audio.py

1) Sans baresip : lecture de la sortie de pactl, choix des appareils, plan d'application.
2) Avec baresip : de faux appareils ALSA (voir fakeaudio.py) permettent de vérifier que la
   sonnerie, la voix et le micro passent bien par les appareils choisis, y compris quand on
   change d'appareil en plein appel.
"""
import atexit
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from ligne_emak import audio  # noqa: E402

results = []


def check(name, ok, info=""):
    results.append((name, ok))
    print(("OK    " if ok else "ÉCHEC ") + name + (("  — " + str(info)) if info != "" else ""), flush=True)


# =============================================================== 1) décisions

SINKS = """Sink #47
\tState: SUSPENDED
\tName: alsa_output.pci-0000_00_1f.3.analog-stereo
\tDescription: Audio interne Stéréo analogique
\tDriver: PipeWire
\tProperties:
\t\tdevice.description = "Audio interne"
\t\tnode.name = "alsa_output.pci-0000_00_1f.3.analog-stereo"
Sink #63
\tState: RUNNING
\tName: alsa_output.usb-Logitech_USB_Headset-00.analog-stereo
\tDescription: Casque USB Logitech
\tProperties:
\t\tNamed: thing
Sink #70
\tName: bad name;with spaces
\tDescription: Ignoré
"""
SOURCES = """Source #48
\tName: alsa_output.pci-0000_00_1f.3.analog-stereo.monitor
\tDescription: Monitor of Audio interne Stéréo analogique
\tMonitor of Sink: alsa_output.pci-0000_00_1f.3.analog-stereo
Source #49
\tName: alsa_input.pci-0000_00_1f.3.analog-stereo
\tDescription: Audio interne Micro
\tMonitor of Sink: n/a
Source #64
\tName: alsa_input.usb-Logitech_USB_Headset-00.mono-fallback
\tDescription: Casque USB Logitech Mono
\tMonitor of Sink: n/a
"""
INFO = """Server String: /run/user/1000/pulse/native
Server Name: PulseAudio (on PipeWire 1.0.5)
Default Sink: alsa_output.pci-0000_00_1f.3.analog-stereo
Default Source: alsa_input.pci-0000_00_1f.3.analog-stereo
"""

sinks = audio.parse_pactl_list(SINKS)
sources = audio.parse_pactl_list(SOURCES)
check("pactl : 2 sorties (nom invalide écarté)", [d.name for d in sinks] == [
    "alsa_output.pci-0000_00_1f.3.analog-stereo", "alsa_output.usb-Logitech_USB_Headset-00.analog-stereo"],
    [d.name for d in sinks])
check("pactl : descriptions accentuées conservées", sinks[0].description == "Audio interne Stéréo analogique")
check("pactl : moniteurs écartés, 2 micros", [d.description for d in sources] == [
    "Audio interne Micro", "Casque USB Logitech Mono"], [d.description for d in sources])
check("pactl info : appareils par défaut", audio.parse_pactl_info(INFO) == (
    "alsa_output.pci-0000_00_1f.3.analog-stereo", "alsa_input.pci-0000_00_1f.3.analog-stereo"))

HEADSET = "alsa_output.usb-Logitech_USB_Headset-00.analog-stereo"
HEADMIC = "alsa_input.usb-Logitech_USB_Headset-00.mono-fallback"
devs = audio.DeviceList(sinks, sources, *audio.parse_pactl_info(INFO), ok=True)
check("appareil ALSA (greffon pulse)", audio.alsa_device(HEADSET, "pulse") == "alsa,pulse:DEVICE=" + HEADSET)
check("appareil ALSA (greffon pipewire)", audio.alsa_device(HEADSET, "pipewire") == "alsa,pipewire:NODE=" + HEADSET)
check("appareil ALSA par défaut", audio.alsa_device("", "pulse") == "alsa,default")
check("sans greffon : toujours le défaut", audio.alsa_device(HEADSET, None) == "alsa,default")

r = audio.compute_route({"output": HEADSET, "input": HEADMIC, "ring": audio.SAME_AS_OUTPUT}, devs, "pulse")
check("route : casque pour voix, micro et sonnerie",
      r["player"] == r["alert"] == "alsa,pulse:DEVICE=" + HEADSET and r["source"] == "alsa,pulse:DEVICE=" + HEADMIC)
r = audio.compute_route({"output": HEADSET, "input": "", "ring": ""}, devs, "pulse")
check("route : sonnerie sur l'appareil par défaut", r["alert"] == "alsa,default" and r["source"] == "alsa,default")
unplugged = audio.DeviceList(sinks[:1], sources[:1], *audio.parse_pactl_info(INFO), ok=True)
r = audio.compute_route({"output": HEADSET, "input": HEADMIC, "ring": audio.SAME_AS_OUTPUT}, unplugged, "pulse")
check("casque débranché : repli sur le défaut", r["player"] == r["source"] == r["alert"] == "alsa,default"
      and r["names"]["output"] == "")
r = audio.compute_route({"output": HEADSET, "input": HEADMIC}, audio.DeviceList(), "pulse")
check("liste illisible : on garde le choix enregistré", r["player"] == "alsa,pulse:DEVICE=" + HEADSET)

A = {"player": "alsa,pulse:DEVICE=a", "source": "alsa,pulse:DEVICE=m", "alert": "alsa,pulse:DEVICE=a"}
plan = audio.plan_changes
check("plan : rien à faire", plan(dict(A), A, False, False) == [])
check("plan : appel qui sonne -> attendre",
      plan(dict(A, player="alsa,default", alert="alsa,default"), A, True, True) == [("defer",)])
check("plan : micro en plein appel", plan(dict(A, source="alsa,default"), A, True, False) == [("ausrc", "alsa,default")])
check("plan : sortie en plein appel (sonnerie corrigée après)",
      plan(dict(A, player="alsa,default"), A, True, False) == [("auplay", "alsa,default")])
check("plan : hors appel, voix et sonnerie ensemble -> auplay",
      plan(dict(A, player="alsa,default", alert="alsa,default"), A, False, False) == [("auplay", "alsa,default")])
check("plan : hors appel, voix et sonnerie séparées -> relance",
      plan(dict(A, alert="alsa,default"), A, False, False) == [("restart",)])
after = {"player": "alsa,default", "source": A["source"], "alert": "alsa,default"}   # après auplay en appel
check("plan : fin d'appel, sonnerie à rétablir -> relance",
      plan(dict(A, player="alsa,default"), after, False, False) == [("restart",)])

# =============================================================== 2) routage réel dans baresip

from gi.repository import GLib  # noqa: E402

from fakeaudio import FakeAudio, tone_matches  # noqa: E402
from ligne_emak.engine import MODULE_DIRS, Account, Engine, find_baresip  # noqa: E402

BARESIP = find_baresip()
MODDIR = os.environ.get("LIGNE_EMAK_MODULE_DIR") or next((d for d in MODULE_DIRS if os.path.isdir(d)), None)
if not BARESIP or not MODDIR or not os.path.exists(os.path.join(MODDIR, "alsa.so")):
    print("\nbaresip (module alsa) absent : routage réel non testé.")
    print("%d/%d vérifications réussies" % (sum(ok for _, ok in results), len(results)))
    sys.exit(0 if all(ok for _, ok in results) else 1)

WORK = tempfile.mkdtemp(prefix="ligne-emak-audio-")
if not os.environ.get("LIGNE_EMAK_KEEP"):
    atexit.register(shutil.rmtree, WORK, True)
else:
    print("dossier de test conservé :", WORK)
fake = FakeAudio(WORK, ["casque", "hautparleurs", "bob"], {"micro-casque": 300, "webcam": 900, "micro-bob": 660})
ALICE = ("casque", "hautparleurs", "systeme")
os.environ["HOME"] = fake.home          # baresip lit ~/.asoundrc
os.makedirs(os.path.join(WORK, "run"), mode=0o700)
os.environ.update({"LIGNE_EMAK_BARESIP": BARESIP, "LIGNE_EMAK_MODULE_DIR": MODDIR,
                   "XDG_RUNTIME_DIR": os.path.join(WORK, "run"), "LIGNE_EMAK_REGINT": "10"})
for k in ("LIGNE_EMAK_AUDIO_SOURCE", "LIGNE_EMAK_AUDIO_PLAYER", "LIGNE_EMAK_AUDIO_ALERT"):
    os.environ.pop(k, None)

pbx_log = os.path.join(WORK, "pbx.log")
pbx = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "fakepbx.py"), "5090", "101", "Secret123"],
                       stdout=open(pbx_log, "w"), stderr=subprocess.STDOUT)
bobdir = os.path.join(WORK, "bob")
os.makedirs(bobdir)
with open(os.path.join(bobdir, "config"), "w") as f:
    f.write("\n".join(["module_path " + MODDIR, "sip_listen 127.0.0.1:5072",
                       "audio_player alsa,pulse:DEVICE=bob", "audio_source alsa,pulse:DEVICE=micro-bob",
                       "audio_alert alsa,pulse:DEVICE=bob", "ctrl_tcp_listen 127.0.0.1:4455",
                       "module g711.so", "module alsa.so", "module_tmp account.so",
                       "module_app menu.so", "module_app ctrl_tcp.so", ""]))
with open(os.path.join(bobdir, "accounts"), "w") as f:
    f.write("<sip:bob@127.0.0.1:5072>;regint=0;answermode=manual;mwi=no\n")
bob = subprocess.Popen([BARESIP, "-f", bobdir], stdout=open(os.path.join(WORK, "bob.log"), "w"),
                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
atexit.register(lambda: [p.kill() for p in (pbx, bob)])

ctx = GLib.MainContext.default()


def pump(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ctx.iteration(False)
        if cond():
            return True
        time.sleep(0.01)
    return False


def settle(seconds):
    pump(lambda: False, seconds)


class Ctl:
    def __init__(self, port):
        for _ in range(50):
            try:
                self.s = socket.create_connection(("127.0.0.1", port))
                break
            except OSError:
                time.sleep(0.1)
        else:
            sys.exit("Bob ne démarre pas")
        self.s.setblocking(False)
        self.buf, self.events = b"", []
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
        d = json.dumps(dict({"command": c, "token": "bob"}, **({"params": p} if p else {}))).encode()
        self.s.sendall(b"%d:%s," % (len(d), d))


bobc = Ctl(4455)


def loudest(seconds=0.5):
    """Sortie d'Alice qui joue en ce moment (Bob a son propre appareil, ignoré ici)."""
    levels = {n: fake.level(n, seconds) for n in ALICE}
    name = max(levels, key=levels.get)
    return name if levels[name] > 300 else None


eng = Engine(os.path.join(WORK, "config"), os.path.join(WORK, "cache"))
calls = []
eng.connect("call-event", lambda e, ev: calls.append(ev))
eng.audio = {"player": "alsa,pulse:DEVICE=casque", "source": "alsa,pulse:DEVICE=micro-casque",
             "alert": "alsa,pulse:DEVICE=hautparleurs"}
eng.start(Account("101", "Secret123", "127.0.0.1:5090", "udp"))
check("moteur en ligne", pump(lambda: eng.state == "online", 15), eng.state)

m = re.findall(r"contact=<(sip:[^>]+)>", open(pbx_log).read())
bobc.cmd("dial", m[-1])
pump(lambda: any(e["type"] == "CALL_INCOMING" for e in calls), 8)
settle(1.2)
check("la sonnerie joue sur les haut-parleurs", loudest(1.0) == "hautparleurs",
      {n: round(fake.level(n, 1.0)) for n in ALICE})

eng.accept()
pump(lambda: any(e["type"] == "CALL_ESTABLISHED" for e in calls), 8)
settle(1.5)
check("la voix arrive dans le casque", loudest() == "casque" and tone_matches(fake.freq("casque"), 660),
      "%s, %s Hz" % (loudest(), fake.freq("casque")))
check("la sonnerie s'est arrêtée", fake.level("hautparleurs") < 300, round(fake.level("hautparleurs")))
check("l'interlocuteur entend le micro du casque", tone_matches(fake.freq("bob"), 300), "%s Hz" % fake.freq("bob"))

res = {}
eng.set_source("alsa,pulse:DEVICE=webcam", lambda ok, d: res.setdefault("src", ok))
pump(lambda: "src" in res, 3)
settle(1.5)
check("micro changé en plein appel -> l'interlocuteur entend la webcam", res.get("src") and tone_matches(fake.freq("bob"), 900),
      "%s Hz" % fake.freq("bob"))

eng.set_player("alsa,pulse:DEVICE=hautparleurs", lambda ok, d: res.setdefault("play", ok))
pump(lambda: "play" in res, 3)
settle(1.5)
check("sortie changée en plein appel -> la voix passe aux haut-parleurs",
      res.get("play") and loudest() == "hautparleurs" and tone_matches(fake.freq("hautparleurs"), 660),
      "%s, %s Hz" % (loudest(), fake.freq("hautparleurs")))
check("plus rien dans le casque", fake.level("casque") < 300, round(fake.level("casque")))

eng.set_player("alsa,default", lambda ok, d: res.setdefault("def", ok))
pump(lambda: "def" in res, 3)
settle(1.5)
check("retour à l'appareil par défaut du système", loudest() == "systeme", loudest())
check("état suivi par le moteur", eng.applied == {"player": "alsa,default", "source": "alsa,pulse:DEVICE=webcam",
                                                  "alert": "alsa,default"}, eng.applied)
eng.hangup()
pump(lambda: any(e["type"] == "CALL_CLOSED" for e in calls), 6)
eng.stop()
pump(lambda: not eng.running, 6)

print("\n%d/%d vérifications réussies" % (sum(ok for _, ok in results), len(results)))
sys.exit(0 if all(ok for _, ok in results) else 1)
