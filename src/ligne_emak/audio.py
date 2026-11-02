"""Appareils audio : liste, choix, branchement à chaud, test du haut-parleur et vumètre du micro.

La liste vient du serveur de son de la session (PulseAudio ou PipeWire) par « pactl ».
baresip ouvre l'appareil choisi par le module ALSA, au moyen du greffon ALSA « pulse »
(paquet libasound2-plugins : pulse:DEVICE=<nom>) ou, à défaut, du greffon « pipewire »
(paquet pipewire-alsa : pipewire:NODE=<nom>). Les noms sont les mêmes partout.

Les fonctions compute_route() et plan_changes() ne dépendent pas de GTK : elles décident
quels appareils utiliser et comment les appliquer au moteur, et sont testées seules.
"""

import array
import glob
import os
import re
import shutil
import subprocess

from gi.repository import Gio, GLib, GObject

DEFAULT = ""            # « appareil par défaut du système »
SAME_AS_OUTPUT = "@output"  # sonnerie : même appareil que le haut-parleur
SAFE_NAME = re.compile(r"^[A-Za-z0-9._:+@-]{1,110}$")


class Device:
    __slots__ = ("name", "description")

    def __init__(self, name, description):
        self.name = name
        self.description = description or name

    def __repr__(self):
        return "Device(%r, %r)" % (self.name, self.description)


class DeviceList:
    """Instantané des appareils : sorties (sinks), entrées (sources) et appareils par défaut."""

    def __init__(self, sinks=(), sources=(), default_sink="", default_source="", ok=False, error=""):
        self.sinks = list(sinks)
        self.sources = list(sources)
        self.default_sink = default_sink
        self.default_source = default_source
        self.ok = ok
        self.error = error

    def sink(self, name):
        return next((d for d in self.sinks if d.name == name), None)

    def source(self, name):
        return next((d for d in self.sources if d.name == name), None)

    def key(self):
        return (tuple(d.name for d in self.sinks), tuple(d.name for d in self.sources),
                self.default_sink, self.default_source)


# ---------------------------------------------------------------- greffon ALSA

def alsa_backend():
    """« pulse », « pipewire » ou None selon les greffons ALSA installés."""
    forced = os.environ.get("LIGNE_EMAK_ALSA_PLUGIN")
    if forced is not None:
        return forced if forced in ("pulse", "pipewire") else None
    dirs = glob.glob("/usr/lib/*/alsa-lib") + ["/usr/lib/alsa-lib", "/usr/lib64/alsa-lib"]
    for plugin in ("pulse", "pipewire"):
        if any(os.path.exists(os.path.join(d, "libasound_module_pcm_%s.so" % plugin)) for d in dirs):
            return plugin
    return None


def alsa_device(name, backend):
    """Nom d'appareil ALSA à donner à baresip (« module,appareil »)."""
    if not name or not backend or not SAFE_NAME.match(name):
        return "alsa,default"
    if backend == "pipewire":
        return "alsa,pipewire:NODE=%s" % name
    return "alsa,pulse:DEVICE=%s" % name


# ---------------------------------------------------------------- liste des appareils (pactl)

def _pactl(args, timeout=4):
    exe = shutil.which("pactl")
    if not exe:
        return None, "pactl introuvable (paquet pulseaudio-utils)"
    env = dict(os.environ, LC_ALL="C", LANG="C", LANGUAGE="C")
    try:
        r = subprocess.run([exe] + list(args), capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip() or "pactl a échoué"
    return r.stdout, ""


def parse_pactl_list(text):
    """Analyse « LC_ALL=C pactl list sinks|sources » ; ignore les moniteurs et les noms bizarres."""
    devices, cur = [], None
    for line in (text or "").splitlines():
        if re.match(r"^(Sink|Source) #\d+", line):
            cur = {}
            devices.append(cur)
            continue
        if cur is None or not line.startswith("\t") or line.startswith("\t\t"):
            continue
        key, sep, val = line.strip().partition(":")
        if sep and key in ("Name", "Description", "Monitor of Sink"):
            cur[key] = val.strip()
    out = []
    for d in devices:
        name = d.get("Name", "")
        monitor = d.get("Monitor of Sink", "n/a")
        if not name or name.endswith(".monitor") or (monitor and monitor != "n/a"):
            continue
        if not SAFE_NAME.match(name):
            continue
        out.append(Device(name, d.get("Description", "")))
    return out


def parse_pactl_info(text):
    sink = source = ""
    for line in (text or "").splitlines():
        key, _, val = line.partition(":")
        if key.strip() == "Default Sink":
            sink = val.strip()
        elif key.strip() == "Default Source":
            source = val.strip()
    return sink, source


def list_devices():
    sinks_txt, err = _pactl(["list", "sinks"])
    if sinks_txt is None:
        return DeviceList(error=err)
    sources_txt, err = _pactl(["list", "sources"])
    if sources_txt is None:
        return DeviceList(error=err)
    info_txt, _ = _pactl(["info"])
    ds, dsrc = parse_pactl_info(info_txt)
    return DeviceList(parse_pactl_list(sinks_txt), parse_pactl_list(sources_txt), ds, dsrc, ok=True)


# ---------------------------------------------------------------- décisions (sans GTK)

def effective(pref, present, trust):
    """Appareil réellement utilisé pour une préférence : elle-même si branchée, sinon le défaut."""
    if not pref:
        return DEFAULT
    return pref if (trust or present(pref)) else DEFAULT


def compute_route(prefs, devices, backend):
    """Préférences -> appareils ALSA {player, source, alert} + noms effectifs.

    prefs : {"output": nom|"", "input": nom|"", "ring": nom|""|SAME_AS_OUTPUT}
    Si la liste n'a pas pu être lue, on fait confiance aux préférences enregistrées.
    """
    trust = not devices.ok
    out = effective(prefs.get("output", ""), lambda n: devices.sink(n) is not None, trust)
    inp = effective(prefs.get("input", ""), lambda n: devices.source(n) is not None, trust)
    ring_pref = prefs.get("ring", SAME_AS_OUTPUT)
    ring = out if ring_pref == SAME_AS_OUTPUT else effective(ring_pref, lambda n: devices.sink(n) is not None,
                                                             trust)
    return {
        "player": alsa_device(out, backend),
        "source": alsa_device(inp, backend),
        "alert": alsa_device(ring, backend),
        "names": {"output": out, "input": inp, "ring": ring},
    }


def plan_changes(route, applied, calls_active, calls_ringing):
    """Ce qu'il faut faire pour passer de l'état du moteur (applied) à la route voulue.

    Renvoie une liste d'actions : ("ausrc", dev), ("auplay", dev), ("restart",) ou ("defer",).
    - Jamais de changement pendant qu'un appel sonne (baresip 1.0 ne sait pas encore ouvrir
      l'appareil d'un appel non établi) : on attend qu'il soit établi ou terminé.
    - « auplay » règle à la fois la voix et la sonnerie : en appel, on bascule la voix et on
      laisse la sonnerie se corriger à la fin de l'appel ; hors appel, si la voix et la sonnerie
      doivent aller sur deux appareils différents, il faut relancer le moteur (configuration).
    """
    want = (route["player"], route["source"], route["alert"])
    have = (applied.get("player"), applied.get("source"), applied.get("alert"))
    if want == have:
        return []
    if calls_ringing:
        return [("defer",)]
    actions = []
    if route["source"] != applied.get("source"):
        actions.append(("ausrc", route["source"]))
    if calls_active:
        if route["player"] != applied.get("player"):
            actions.append(("auplay", route["player"]))
        return actions
    if route["player"] == applied.get("player") and route["alert"] == applied.get("alert"):
        return actions
    if route["player"] == route["alert"]:
        actions.append(("auplay", route["player"]))
        return actions
    return [("restart",)]


# ---------------------------------------------------------------- surveillance (branchement à chaud)

class DeviceMonitor(GObject.Object):
    """Suit « pactl subscribe » et émet « changed » quand un appareil apparaît ou disparaît."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}
    EVENT = re.compile(r"Event '(new|remove|change)' on (sink|source|server)\b")

    def __init__(self):
        super().__init__()
        self._proc = None
        self._stream = None
        self._cancel = None
        self._debounce = 0
        self._retry = 0
        self._delay = 3
        self._stopped = False

    def start(self):
        self._stopped = False
        if self._proc is not None or not shutil.which("pactl"):
            return
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
        launcher.setenv("LC_ALL", "C", True)
        launcher.setenv("LANG", "C", True)
        try:
            self._proc = launcher.spawnv([shutil.which("pactl"), "subscribe"])
        except GLib.Error:
            self._schedule_retry()
            return
        self._cancel = Gio.Cancellable()
        self._stream = Gio.DataInputStream.new(self._proc.get_stdout_pipe())
        self._proc.wait_async(None, self._on_exit, self._proc)
        self._read()

    def stop(self):
        self._stopped = True
        if self._retry:
            GLib.source_remove(self._retry)
            self._retry = 0
        if self._cancel:
            self._cancel.cancel()
        if self._proc is not None:
            self._proc.force_exit()
        self._proc = None

    def _read(self):
        self._stream.read_line_async(GLib.PRIORITY_DEFAULT, self._cancel, self._on_line)

    def _on_line(self, stream, result):
        try:
            line, _ = stream.read_line_finish_utf8(result)
        except GLib.Error:
            return
        if line is None:
            return  # fin du flux ; _on_exit relancera
        m = self.EVENT.search(line)
        if m and (m.group(1) != "change" or m.group(2) == "server"):
            self._delay = 3
            if self._debounce:
                GLib.source_remove(self._debounce)
            self._debounce = GLib.timeout_add(600, self._fire)
        self._read()

    def _fire(self):
        self._debounce = 0
        self.emit("changed")
        return False

    def _on_exit(self, proc, result, which):
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        if which is self._proc:
            self._proc = None
        if not self._stopped:
            self._schedule_retry()

    def _schedule_retry(self):
        if self._retry or self._stopped:
            return

        def again():
            self._retry = 0
            self.start()
            self.emit("changed")  # le serveur de son a pu redémarrer
            return False

        self._retry = GLib.timeout_add_seconds(self._delay, again)
        self._delay = min(self._delay * 2, 60)


# ---------------------------------------------------------------- vumètre du micro et test du haut-parleur

class LevelMeter(GObject.Object):
    """Lit le micro choisi avec « parec » et émet son niveau (0 à 1) environ 25 fois par seconde."""

    __gsignals__ = {"level": (GObject.SignalFlags.RUN_FIRST, None, (float,))}

    def __init__(self):
        super().__init__()
        self._proc = None
        self._cancel = None

    @staticmethod
    def available():
        return shutil.which("parec") is not None

    def start(self, source_name):
        self.stop()
        exe = shutil.which("parec")
        if not exe:
            return False
        args = [exe, "--raw", "--format=s16le", "--rate=16000", "--channels=1", "--latency-msec=40",
                "--client-name=Ligne EMAK", "--stream-name=Test du micro"]
        if source_name:
            args.append("--device=" + source_name)
        try:
            self._proc = Gio.Subprocess.new(args, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error:
            self._proc = None
            return False
        self._cancel = Gio.Cancellable()
        self._read(self._proc)
        return True

    def stop(self):
        if self._cancel:
            self._cancel.cancel()
            self._cancel = None
        if self._proc is not None:
            self._proc.force_exit()
            self._proc = None

    def _read(self, proc):
        proc.get_stdout_pipe().read_bytes_async(1280, GLib.PRIORITY_DEFAULT, self._cancel, self._on_data, proc)

    def _on_data(self, stream, result, proc):
        try:
            data = stream.read_bytes_finish(result).get_data()
        except GLib.Error:
            return
        if not data or proc is not self._proc:
            return
        samples = array.array("h")
        samples.frombytes(data[: len(data) // 2 * 2])
        peak = max((abs(s) for s in samples), default=0) / 32768.0
        self.emit("level", min(1.0, peak))
        self._read(proc)


class SoundTest:
    """Joue un son court sur une sortie précise avec « paplay » (bouton « Tester »)."""

    def __init__(self):
        self._proc = None
        self._timer = 0

    @staticmethod
    def available():
        return shutil.which("paplay") is not None

    def play(self, path, sink_name, seconds=4, on_done=None):
        self.stop()
        exe = shutil.which("paplay")
        if not exe or not path or not os.path.exists(path):
            return False
        args = [exe, "--client-name=Ligne EMAK"]
        if sink_name:
            args.append("--device=" + sink_name)
        args.append(path)
        try:
            proc = Gio.Subprocess.new(args, Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error:
            return False
        self._proc = proc

        def finished(p, res):
            try:
                p.wait_finish(res)
            except GLib.Error:
                pass
            if p is self._proc:
                self._proc = None
                if self._timer:
                    GLib.source_remove(self._timer)
                    self._timer = 0
                if on_done:
                    on_done()

        proc.wait_async(None, finished)
        self._timer = GLib.timeout_add_seconds(seconds, self._timeout)
        return True

    def _timeout(self):
        self._timer = 0
        if self._proc is not None:
            self._proc.force_exit()
        return False

    def stop(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if self._proc is not None:
            self._proc.force_exit()

    @property
    def playing(self):
        return self._proc is not None


def test_sound_path(sound_dir):
    for p in ("/usr/share/sounds/freedesktop/stereo/audio-test-signal.oga",
              "/usr/share/sounds/freedesktop/stereo/complete.oga",
              os.path.join(sound_dir or "", "ringback.wav")):
        if os.path.exists(p):
            return p
    return None
