"""Pilotage du moteur SIP (baresip) par son interface de contrôle TCP locale.

baresip gère tout le SIP, l'audio et les codecs. Ce module :
  * écrit une configuration minimale dans un dossier privé en mémoire
    ($XDG_RUNTIME_DIR), y compris le mot de passe, puis efface le fichier
    des comptes dès que baresip l'a lu ;
  * lance baresip, se connecte à son port de contrôle (127.0.0.1 seulement) ;
  * traduit les événements JSON de baresip en signaux GObject.

Protocole ctrl_tcp : messages JSON encadrés en « netstring » (longueur:données,).
"""

import errno
import json
import re
import os
import shutil
import signal
import socket
import time

from gi.repository import Gio, GLib, GObject

from . import DEFAULT_DOMAIN_SUFFIX

MODULE_DIRS = [
    "/usr/lib/baresip/modules",
    "/usr/lib/x86_64-linux-gnu/baresip/modules",
    "/usr/lib/aarch64-linux-gnu/baresip/modules",
    "/usr/local/lib/baresip/modules",
]
SOUND_DIRS = ["/usr/share/baresip", "/usr/local/share/baresip"]

# Ordre = préférence des codecs (G.722 « HD » d'abord, puis G.711, puis Opus).
CODEC_MODULES = ["g722.so", "g711.so", "opus.so"]
AUDIO_MODULES = ["alsa.so"]
NAT_MODULES = ["stun.so"]
TMP_MODULES = ["uuid.so", "account.so"]
# ctrl_tcp en dernier : quand son port répond, les comptes sont déjà chargés.
APP_MODULES = ["menu.so", "ctrl_tcp.so"]
# Le module de messagerie (mwi) de baresip 1.0.0 peut boucler à l'infini si
# l'enregistrement réussit avant son minuteur de démarrage ; on ne le charge
# donc qu'APRÈS le premier enregistrement réussi (commande « insmod »).
LATE_MODULES = ["mwi"]
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def normalize_domain(text):
    """« macompagnie » -> « macompagnie.emaktalk.com » ; garde un domaine complet tel quel."""
    d = (text or "").strip().lower()
    for prefix in ("sip:", "sips:", "https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/")[0].strip().strip(".")
    d = "".join(d.split())
    if d and "." not in d:
        d += DEFAULT_DOMAIN_SUFFIX
    return d


def normalize_number(text):
    """Nettoie un numéro saisi ou reçu d'un lien tel: pour le composer sur un PBX nord-américain."""
    t = (text or "").strip()
    for prefix in ("tel:", "callto:", "sip:", "sips:"):
        if t.lower().startswith(prefix):
            t = t[len(prefix):]
            break
    t = t.split(";")[0].split("?")[0]
    if "@" in t:  # adresse SIP complète : on ne touche à rien
        return t
    keep = "".join(c for c in t if c.isdigit() or c in "*#+")
    if keep.startswith("+1") and len(keep) == 12:
        return keep[1:]                      # +1 514... -> 1514...
    if keep.startswith("+"):
        return "011" + keep[1:].replace("+", "")  # international depuis l'Amérique du Nord
    return keep


class Account:
    """Identifiants d'un poste EMAK."""

    def __init__(self, extension="", password="", domain="", transport="udp", display_name=""):
        self.extension = (extension or "").strip()
        self.password = password or ""
        self.domain = normalize_domain(domain)
        self.transport = "tcp" if transport == "tcp" else "udp"
        self.display_name = (display_name or "").replace('"', "").replace("<", "").replace(">", "").strip()

    @property
    def aor(self):
        return "sip:%s@%s" % (self.extension, self.domain)

    def problems(self):
        """Liste des erreurs empêchant la connexion (en français, pour l'utilisateur)."""
        out = []
        if not self.extension:
            out.append("Le numéro de poste est vide.")
        elif any(c in self.extension for c in " ;<>@:\"'"):
            out.append("Le numéro de poste contient des caractères invalides.")
        if not self.password:
            out.append("Le mot de passe est vide.")
        elif any(c in self.password for c in " ;<>\"\t\r\n"):
            out.append("Le mot de passe contient une espace ou l'un de ces caractères : ; < > \" "
                       "— baresip ne peut pas les transmettre. Demandez un nouveau mot de passe à EMAK.")
        if not self.domain or any(c in self.domain for c in ";<>@\" "):
            out.append("Le domaine de la compagnie est invalide.")
        return out

    def is_complete(self):
        return not self.problems()

    def baresip_line(self):
        name = '"%s" ' % self.display_name if self.display_name else ""
        params = [
            "auth_user=%s" % self.extension,
            "auth_pass=%s" % self.password,
            "answermode=manual",
            "rwait=80",
        ]
        regint = os.environ.get("LIGNE_EMAK_REGINT")  # réservé aux tests
        if self.transport == "tcp":
            # TCP : connexion persistante avec keep-alive (RFC 5626), comme les mobiles EMAK.
            params += ["regint=%s" % (regint or 240), 'outbound="sip:%s;transport=tcp"' % self.domain,
                       "sipnat=outbound"]
        else:
            # UDP : ré-enregistrement fréquent pour garder ouverte la porte du routeur (NAT).
            params += ["regint=%s" % (regint or 60)]
        return "%s<%s>;%s\n" % (name, self.aor, ";".join(params))


def _first_existing(paths):
    for p in paths:
        if os.path.isdir(p):
            return p
    return None


def find_baresip():
    exe = os.environ.get("LIGNE_EMAK_BARESIP") or shutil.which("baresip")
    return exe if exe and os.access(exe, os.X_OK) else None


def _free_local_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def parse_mwi(body):
    """'Messages-Waiting: yes / Voice-Message: 2/5' -> (nouveaux, anciens)."""
    new = old = 0
    waiting = False
    for line in (body or "").replace("\\r\\n", "\n").splitlines():
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "messages-waiting":
            waiting = val.lower().startswith("yes")
        elif key == "voice-message":
            counts = val.split()[0] if val else ""
            n, _, o = counts.partition("/")
            try:
                new, old = int(n), int(o or 0)
            except ValueError:
                pass
    if waiting and new == 0:
        new = 1
    return new, old


class Engine(GObject.Object):
    """Un processus baresip pour un compte, piloté de façon asynchrone dans la boucle GLib."""

    __gsignals__ = {
        # état : stopped | starting | registering | online | failed | error | missing
        "status": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # événement d'appel brut de baresip (dict)
        "call-event": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # messagerie vocale : nouveaux, anciens
        "mwi": (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
    }

    def __init__(self, config_dir, cache_dir):
        super().__init__()
        self.config_dir = _private_dir(config_dir)
        self.cache_dir = _private_dir(cache_dir)
        runtime = GLib.get_user_runtime_dir() or os.path.join(cache_dir, "run")
        self.run_dir = os.path.join(runtime, "ligne-emak")
        self.log_path = os.path.join(self.cache_dir, "baresip.log")
        self.account = None
        self.state = "stopped"
        self.detail = ""
        self._proc = None
        self._generation = 0
        self._sock = None
        self._watch = 0
        self._buf = b""
        self._port = 0
        self._token = 0
        self._pending = {}
        self._connect_deadline = 0
        self._stopping = False
        self._restart_delay = 3
        self._restart_src = 0
        self._after_exit = None
        self._queue = []
        self._late_loaded = False
        self._moddir = None
        GLib.timeout_add_seconds(30, self._health_check)

    # ---------------------------------------------------------------- cycle de vie

    def start(self, account):
        """(Re)démarre le moteur avec ce compte."""
        self.account = account
        if self._restart_src:
            GLib.source_remove(self._restart_src)
            self._restart_src = 0
        if self._proc is not None:
            self._after_exit = lambda: self._spawn()
            self._terminate()
            return
        self._spawn()

    def stop(self, then=None):
        self.account = None
        if self._restart_src:
            GLib.source_remove(self._restart_src)
            self._restart_src = 0
        if self._proc is None:
            self._set_state("stopped", "")
            if then:
                then()
            return
        self._after_exit = then
        self._terminate()

    def stop_blocking(self, timeout=3.0):
        """Arrêt propre à la fermeture de l'application (désenregistrement SIP)."""
        proc = self._proc
        self.account = None
        if proc is None:
            return
        self._stopping = True
        try:
            proc.send_signal(signal.SIGTERM)
        except GLib.Error:
            pass
        end = time.monotonic() + timeout
        ctx = GLib.MainContext.default()
        while self._proc is not None and time.monotonic() < end:
            ctx.iteration(False)
            time.sleep(0.02)
        if self._proc is not None:
            try:
                proc.force_exit()
            except GLib.Error:
                pass

    @property
    def running(self):
        return self._proc is not None

    @property
    def connected(self):
        return self._sock is not None

    def _spawn(self):
        self._stopping = False
        self._after_exit = None
        acc = self.account
        if acc is None:
            return
        problems = acc.problems()
        if problems:
            self._set_state("failed", problems[0])
            return
        exe = find_baresip()
        if not exe:
            self._set_state("missing", "Le moteur « baresip » n'est pas installé (sudo apt install baresip).")
            return
        moddir = os.environ.get("LIGNE_EMAK_MODULE_DIR") or _first_existing(MODULE_DIRS)
        if not moddir:
            self._set_state("missing", "Modules baresip introuvables (paquet baresip-core).")
            return

        _private_dir(self.run_dir)
        self._kill_stale()
        self._moddir = moddir
        self._late_loaded = False
        self._port = _free_local_port()
        _write_private(os.path.join(self.run_dir, "config"), self._config_text(moddir))
        _write_private(os.path.join(self.run_dir, "accounts"), acc.baresip_line())
        uuid_src = os.path.join(self.config_dir, "uuid")
        if os.path.exists(uuid_src):
            shutil.copyfile(uuid_src, os.path.join(self.run_dir, "uuid"))

        self._rotate_log()
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDERR_MERGE)
        launcher.set_stdout_file_path(self.log_path)
        try:
            proc = launcher.spawnv([exe, "-f", self.run_dir])
        except GLib.Error as e:
            self._wipe_secrets()
            self._set_state("error", "Impossible de lancer baresip : %s" % e.message)
            return
        self._proc = proc
        try:
            _write_private(self._pidfile, proc.get_identifier() or "")
        except OSError:
            pass
        self._generation += 1
        gen = self._generation
        proc.wait_async(None, self._on_exit, gen)
        self._set_state("starting", "")
        self._connect_deadline = time.monotonic() + 15
        GLib.timeout_add(150, self._try_connect, gen)

    def _config_text(self, moddir):
        env = os.environ
        src = env.get("LIGNE_EMAK_AUDIO_SOURCE", "alsa,default")
        play = env.get("LIGNE_EMAK_AUDIO_PLAYER", "alsa,default")
        alert = env.get("LIGNE_EMAK_AUDIO_ALERT", play)
        sounds = env.get("LIGNE_EMAK_SOUND_DIR") or _first_existing(SOUND_DIRS) or "/usr/share/baresip"
        lines = [
            "# Généré par Ligne EMAK — ne pas modifier, réécrit à chaque démarrage",
            "poll_method epoll",
            "module_path %s" % moddir,
            "audio_path %s" % sounds,
            "call_local_timeout 120",
            "call_max_calls 4",
            "audio_player %s" % play,
            "audio_source %s" % src,
            "audio_alert %s" % alert,
            "audio_level no",
            "jitter_buffer_delay 5-10",
            "rtp_tos 184",
            "rtp_ports 16384-32768",
            "rtcp_mux no",
            "opus_bitrate 28000",
            "ctrl_tcp_listen 127.0.0.1:%d" % self._port,
        ]
        extra_mods = env.get("LIGNE_EMAK_EXTRA_MODULES", "").split()
        for m in CODEC_MODULES + AUDIO_MODULES + NAT_MODULES + extra_mods:
            if os.path.exists(os.path.join(moddir, m)):
                lines.append("module %s" % m)
        for m in TMP_MODULES:
            lines.append("module_tmp %s" % m)
        for m in APP_MODULES:
            lines.append("module_app %s" % m)
        return "\n".join(lines) + "\n"

    @property
    def _pidfile(self):
        return os.path.join(self.run_dir, "baresip.pid")

    def _kill_stale(self):
        """Arrête un baresip orphelin laissé par une session précédente qui aurait planté."""
        try:
            with open(self._pidfile) as f:
                pid = int(f.read().strip() or 0)
        except (OSError, ValueError):
            return
        if pid <= 1:
            return
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            return
        if "baresip" not in cmd or self.run_dir not in cmd:
            return
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(30):
                time.sleep(0.05)
                os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def _rotate_log(self):
        try:
            if os.path.getsize(self.log_path) > 512 * 1024:
                os.replace(self.log_path, self.log_path + ".1")
        except OSError:
            pass

    def _wipe_secrets(self):
        try:
            os.unlink(os.path.join(self.run_dir, "accounts"))
        except OSError:
            pass

    def _terminate(self):
        self._stopping = True
        self._disconnect()
        if self._proc is not None:
            try:
                self._proc.send_signal(signal.SIGTERM)
            except GLib.Error:
                pass
            proc, gen = self._proc, self._generation

            def force():
                if self._proc is proc and self._generation == gen:
                    try:
                        proc.force_exit()
                    except GLib.Error:
                        pass
                return False

            GLib.timeout_add_seconds(4, force)

    def _on_exit(self, proc, result, gen):
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        if gen != self._generation:
            return
        self._proc = None
        self._disconnect()
        self._wipe_secrets()
        try:
            os.unlink(self._pidfile)
        except OSError:
            pass
        then, self._after_exit = self._after_exit, None
        if self._stopping:
            self._stopping = False
            if self.account is None:
                self._set_state("stopped", "")
            if then:
                then()
            return
        # Arrêt inattendu : on relance avec un délai croissant.
        self._set_state("error", "Le moteur SIP s'est arrêté ; nouvelle tentative dans %d s." % self._restart_delay)
        delay = self._restart_delay
        self._restart_delay = min(self._restart_delay * 2, 60)

        def again():
            self._restart_src = 0
            if self.account is not None and self._proc is None:
                self._spawn()
            return False

        self._restart_src = GLib.timeout_add_seconds(delay, again)

    # ---------------------------------------------------------------- connexion de contrôle

    def _try_connect(self, gen):
        if gen != self._generation or self._proc is None or self._stopping:
            return False
        try:
            s = socket.create_connection(("127.0.0.1", self._port), timeout=0.5)
        except OSError:
            if time.monotonic() > self._connect_deadline:
                self._set_state("error", "baresip ne répond pas (voir le journal).")
                self._terminate()
                return False
            return True  # on réessaie
        s.setblocking(False)
        self._sock = s
        self._buf = b""
        self._watch = GLib.io_add_watch(s.fileno(), GLib.PRIORITY_DEFAULT,
                                        GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._on_readable)
        # Les comptes sont chargés : le mot de passe ne reste pas sur le disque.
        self._wipe_secrets()
        self._persist_uuid()
        self._restart_delay = 3
        if self.state == "starting":
            self._set_state("registering", "")
        # L'enregistrement a pu aboutir avant notre connexion : on demande l'état.
        self._poll_registration()
        queued, self._queue = self._queue, []
        for args in queued:
            self.command(*args)
        return False

    def _poll_registration(self):
        def done(ok, data):
            if not ok:
                return
            text = ANSI.sub("", data)
            if " OK " in text or text.rstrip().endswith(" OK"):
                self._set_state("online", "")
            elif " ERR " in text and self.state != "failed":
                self._set_state("failed", "Enregistrement refusé par le serveur")
        self.command("reginfo", None, done)

    def _health_check(self):
        if self._sock is not None and self.state in ("registering", "online", "failed"):
            self._poll_registration()
        return True

    def _load_late_modules(self):
        if self._late_loaded or not self._moddir:
            return
        self._late_loaded = True
        for m in LATE_MODULES:
            if os.path.exists(os.path.join(self._moddir, m + ".so")):
                self.command("insmod", m)

    def _persist_uuid(self):
        src = os.path.join(self.run_dir, "uuid")
        dst = os.path.join(self.config_dir, "uuid")
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                shutil.copyfile(src, dst)
            except OSError:
                pass

    def _disconnect(self):
        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = 0
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        for cb in self._pending.values():
            if cb:
                try:
                    cb(False, "déconnecté")
                except Exception:  # noqa: BLE001 - un rappel ne doit pas casser l'arrêt
                    pass
        self._pending.clear()

    def _on_readable(self, fd, cond):
        if cond & (GLib.IO_HUP | GLib.IO_ERR):
            self._watch = 0
            self._disconnect()
            return False
        try:
            chunk = self._sock.recv(65536)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return True
            chunk = b""
        if not chunk:
            self._watch = 0
            self._disconnect()
            return False
        self._buf += chunk
        while True:
            colon = self._buf.find(b":")
            if colon < 0:
                break
            try:
                n = int(self._buf[:colon])
            except ValueError:
                self._buf = b""  # flux corrompu : on repart à zéro
                break
            end = colon + 1 + n
            if len(self._buf) < end + 1:
                break
            payload = self._buf[colon + 1:end]
            self._buf = self._buf[end + 1:]
            try:
                msg = json.loads(payload.decode("utf-8", "replace"))
            except ValueError:
                continue
            self._dispatch(msg)
        return True

    def _dispatch(self, msg):
        if msg.get("response"):
            cb = self._pending.pop(msg.get("token"), None)
            if cb:
                cb(bool(msg.get("ok")), msg.get("data") or "")
            return
        if not msg.get("event"):
            return
        cls = msg.get("class")
        typ = msg.get("type", "")
        if cls == "register":
            if typ == "REGISTER_OK":
                self._set_state("online", "")
            elif typ == "REGISTER_FAIL":
                self._set_state("failed", msg.get("param") or "")
            elif typ == "REGISTERING" and self.state not in ("online",):
                self._set_state("registering", "")
        elif cls == "mwi" or typ == "MWI_NOTIFY":
            new, old = parse_mwi(msg.get("param", ""))
            self.emit("mwi", new, old)
        elif cls == "call":
            self.emit("call-event", msg)

    def _set_state(self, state, detail):
        if state == "online" and self._sock is not None:
            self._load_late_modules()
        if state == self.state and detail == self.detail:
            return
        self.state, self.detail = state, detail
        self.emit("status", state, detail)

    # ---------------------------------------------------------------- commandes

    def command(self, cmd, params=None, callback=None):
        """Envoie une commande baresip (dial, accept, hangup, mute, hold, resume, sndcode, transfer…)."""
        if self._sock is None:
            if self._proc is not None:
                self._queue.append((cmd, params, callback))
            elif callback:
                callback(False, "moteur arrêté")
            return
        self._token += 1
        tok = "t%d" % self._token
        body = {"command": cmd, "token": tok}
        if params:
            body["params"] = str(params)
        data = json.dumps(body).encode("utf-8")
        self._pending[tok] = callback
        try:
            self._sock.sendall(b"%d:%s," % (len(data), data))
        except OSError:
            self._pending.pop(tok, None)
            if callback:
                callback(False, "envoi impossible")

    # raccourcis
    def dial(self, number, cb=None):
        self.command("dial", number, cb)

    def accept(self, cb=None):
        self.command("accept", None, cb)

    def hangup(self, cb=None):
        self.command("hangup", None, cb)

    def toggle_mute(self, cb=None):
        self.command("mute", None, cb)

    def hold(self, cb=None):
        self.command("hold", None, cb)

    def resume(self, cb=None):
        self.command("resume", None, cb)

    def send_dtmf(self, digits, cb=None):
        self.command("sndcode", digits, cb)

    def transfer(self, number, cb=None):
        self.command("transfer", number, cb)
