"""Faux appareils audio pour tester le routage sans carte son ni serveur de son.

Un fichier ~/.asoundrc de test définit un appareil ALSA « pulse:DEVICE=<nom> » (le même nom
que le vrai greffon PulseAudio) et l'appareil « default ». Chaque sortie écrit dans un tube
nommé out-<nom>.raw, lu ici au rythme du temps réel ; chaque entrée lit un tube in-<nom>.raw,
alimenté ici par une tonalité propre à ce micro. On peut donc vérifier quel appareil reçoit
la sonnerie ou la voix, et quel micro l'interlocuteur entend.

Format : PCM 16 bits, mono, 8000 Hz (celui des appels G.711 et de la sonnerie de baresip).

Avec ces appareils artificiels, la tampon de gigue de baresip 1.0 insère de courts silences :
une tonalité reçue paraît plus grave (environ 2/3 de sa fréquence). On identifie donc chaque
micro par une bande de fréquences (tone_matches) plutôt que par une valeur exacte.
"""
import array
import collections
import fcntl
import math
import os
import threading
import time

RATE = 8000
CHUNK = 320                 # 20 ms (une période audio)
F_SETPIPE_SZ = 1031


ASOUNDRC = """
pcm.pulse {{
	@args [ DEVICE ]
	@args.DEVICE {{ type string default "systeme" }}
	type asym
	playback.pcm {{
		type file
		slave.pcm "null"
		file {{ @func concat strings [ "{d}/out-" $DEVICE ".raw" ] }}
		format "raw"
	}}
	capture.pcm {{
		type file
		slave.pcm "null"
		file "/dev/null"
		infile {{ @func concat strings [ "{d}/in-" $DEVICE ".raw" ] }}
		format "raw"
	}}
}}
pcm.!default {{
	type asym
	playback.pcm {{ type file slave.pcm "null" file "{d}/out-systeme.raw" format "raw" }}
	capture.pcm {{ type file slave.pcm "null" file "/dev/null" infile "{d}/in-systeme.raw" format "raw" }}
}}
"""


class _Sink(threading.Thread):
    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = path
        self.recent = collections.deque()   # (horodatage, échantillons)
        self.lock = threading.Lock()
        self.bytes = 0
        self.start()

    def run(self):
        # Ouvert en lecture-écriture : le tube existe en permanence (baresip ne bloque jamais à
        # l'ouverture) et garde un petit tampon (~0,25 s), comme une vraie carte son.
        fd = os.open(self.path, os.O_RDWR)
        try:
            fcntl.fcntl(fd, F_SETPIPE_SZ, 4096)
        except OSError:
            pass
        nxt = time.monotonic()
        while True:
            data = os.read(fd, CHUNK)
            s = array.array("h")
            s.frombytes(data[: len(data) // 2 * 2])
            now = time.monotonic()
            with self.lock:
                self.bytes += len(data)
                self.recent.append((now, s))
                while self.recent and self.recent[0][0] < now - 1.0:
                    self.recent.popleft()
            nxt = max(nxt + len(data) / (2.0 * RATE), now - 0.1)
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    def window(self, seconds=0.5):
        now = time.monotonic()
        with self.lock:
            out = array.array("h")
            for t, s in self.recent:
                if t >= now - seconds:
                    out.extend(s)
        return out


class _Source(threading.Thread):
    def __init__(self, path, freq):
        super().__init__(daemon=True)
        self.path = path
        n = RATE // 10 * 10
        self.tone = array.array("h", (int(9000 * math.sin(2 * math.pi * freq * i / RATE)) for i in range(n)))
        self.start()

    def run(self):
        raw = self.tone.tobytes()
        fd = os.open(self.path, os.O_RDWR)
        try:
            fcntl.fcntl(fd, F_SETPIPE_SZ, 4096)
        except OSError:
            pass
        pos, nxt = 0, time.monotonic()
        while True:
            os.write(fd, raw[pos:pos + CHUNK])      # bloque si personne ne lit ce micro
            pos = (pos + CHUNK) % len(raw)
            now = time.monotonic()
            nxt = max(nxt + CHUNK / (2.0 * RATE), now - 0.1)
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)


def rms(samples):
    if not samples:
        return 0.0
    return math.sqrt(sum(x * x for x in samples) / len(samples))


def frequency(samples):
    """Fréquence dominante approximative (passages par zéro), en Hz ; 0 si silence."""
    if rms(samples) < 300:
        return 0
    crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
    return round(crossings * RATE / (2.0 * len(samples)))


def tone_matches(measured, nominal):
    """La fréquence mesurée correspond-elle à cette tonalité (voir la note en tête du fichier) ?"""
    return 0.55 * nominal <= measured <= 1.08 * nominal


class FakeAudio:
    """sinks : noms des sorties ; sources : {nom du micro: fréquence de sa tonalité en Hz}."""

    def __init__(self, workdir, sinks, sources):
        self.dir = workdir
        os.makedirs(os.path.join(workdir, "home"), exist_ok=True)
        with open(os.path.join(workdir, "home", ".asoundrc"), "w") as f:
            f.write(ASOUNDRC.format(d=workdir))
        self.sinks, self.sources = {}, {}
        for name in list(sinks) + ["systeme"]:
            path = os.path.join(workdir, "out-%s.raw" % name)
            os.mkfifo(path)
            self.sinks[name] = _Sink(path)
        sources = dict(sources)
        sources.setdefault("systeme", 2400)
        for name, freq in sources.items():
            path = os.path.join(workdir, "in-%s.raw" % name)
            os.mkfifo(path)
            self.sources[name] = _Source(path, freq)

    @property
    def home(self):
        return os.path.join(self.dir, "home")

    def level(self, sink, seconds=0.5):
        return rms(self.sinks[sink].window(seconds))

    def freq(self, sink, seconds=0.5):
        return frequency(self.sinks[sink].window(seconds))

    def loudest(self, seconds=0.5):
        """Sortie qui joue en ce moment (ou None)."""
        levels = {n: self.level(n, seconds) for n in self.sinks}
        name = max(levels, key=levels.get)
        return name if levels[name] > 300 else None
