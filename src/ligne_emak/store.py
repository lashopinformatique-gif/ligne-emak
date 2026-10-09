"""Réglages, mot de passe (trousseau GNOME), historique d'appels et démarrage automatique."""

import json
import os
import time

from gi.repository import GLib

from . import APP_ID, APP_NAME

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "ligne-emak")
DATA_DIR = os.path.join(GLib.get_user_data_dir(), "ligne-emak")
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "ligne-emak")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
FALLBACK_PW_FILE = os.path.join(CONFIG_DIR, ".motdepasse")
HISTORY_FILE = os.path.join(DATA_DIR, "historique.json")
AUTOSTART_FILE = os.path.join(GLib.get_user_config_dir(), "autostart", APP_ID + ".desktop")
MAX_HISTORY = 300


def _ensure(path):
    os.makedirs(path, mode=0o700, exist_ok=True)


def _atomic_write(path, text, mode=0o600):
    _ensure(os.path.dirname(path))
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- réglages

DEFAULTS = {
    "extension": "",
    "domain": "",
    "transport": "udp",
    "display_name": "",
    "width": 380,
    "height": 660,
    "close_hint_shown": False,
}


def load_settings():
    data = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data.update(json.load(f))
    except (OSError, ValueError):
        pass
    return data


def save_settings(data):
    _atomic_write(SETTINGS_FILE, json.dumps(data, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- mot de passe

_SECRET = None


def _secret():
    """Module libsecret (trousseau GNOME), ou None s'il n'est pas disponible."""
    global _SECRET
    if _SECRET is None:
        try:
            import gi
            gi.require_version("Secret", "1")
            from gi.repository import Secret
            schema = Secret.Schema.new(APP_ID, Secret.SchemaFlags.NONE,
                                       {"account": Secret.SchemaAttributeType.STRING})
            _SECRET = (Secret, schema)
        except (ImportError, ValueError, AttributeError):
            _SECRET = False
    return _SECRET or None


def _key(extension, domain):
    return "%s@%s" % (extension, domain)


def store_password(extension, domain, password):
    """Enregistre le mot de passe ; renvoie 'keyring' ou 'file' selon l'endroit."""
    sec = _secret()
    if sec:
        Secret, schema = sec
        try:
            ok = Secret.password_store_sync(schema, {"account": _key(extension, domain)},
                                            Secret.COLLECTION_DEFAULT,
                                            "%s — poste %s" % (APP_NAME, extension), password, None)
            if ok:
                try:
                    os.unlink(FALLBACK_PW_FILE)
                except OSError:
                    pass
                return "keyring"
        except GLib.Error:
            pass
    _atomic_write(FALLBACK_PW_FILE, json.dumps({_key(extension, domain): password}))
    return "file"


def load_password(extension, domain):
    sec = _secret()
    if sec:
        Secret, schema = sec
        try:
            pw = Secret.password_lookup_sync(schema, {"account": _key(extension, domain)}, None)
            if pw:
                return pw
        except GLib.Error:
            pass
    try:
        with open(FALLBACK_PW_FILE, encoding="utf-8") as f:
            return json.load(f).get(_key(extension, domain), "")
    except (OSError, ValueError):
        return ""


def forget_password(extension, domain):
    sec = _secret()
    if sec:
        Secret, schema = sec
        try:
            Secret.password_clear_sync(schema, {"account": _key(extension, domain)}, None)
        except GLib.Error:
            pass
    try:
        os.unlink(FALLBACK_PW_FILE)
    except OSError:
        pass


def keyring_available():
    return _secret() is not None


# ---------------------------------------------------------------- historique

class History:
    def __init__(self):
        self.items = []
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                self.items = [i for i in json.load(f) if isinstance(i, dict)]
        except (OSError, ValueError):
            self.items = []

    def add(self, number, name, direction, status, started, duration):
        """direction: in|out ; status: answered|missed|declined|failed|cancelled"""
        self.items.insert(0, {
            "number": number, "name": name or "", "direction": direction, "status": status,
            "time": int(started or time.time()), "duration": int(duration or 0),
        })
        del self.items[MAX_HISTORY:]
        self.save()

    def clear(self):
        self.items = []
        self.save()

    def save(self):
        _atomic_write(HISTORY_FILE, json.dumps(self.items, ensure_ascii=False))

    def missed_since(self, ts):
        return sum(1 for i in self.items if i["status"] == "missed" and i["time"] > ts)


# ---------------------------------------------------------------- démarrage automatique

def autostart_enabled():
    try:
        with open(AUTOSTART_FILE, encoding="utf-8") as f:
            return "X-GNOME-Autostart-enabled=false" not in f.read()
    except OSError:
        return False


def set_autostart(enabled):
    if enabled:
        _atomic_write(AUTOSTART_FILE, "\n".join([
            "[Desktop Entry]",
            "Type=Application",
            "Name=%s" % APP_NAME,
            "Comment=Recevoir les appels de votre poste EMAK",
            "Exec=ligne-emak --background",
            "TryExec=ligne-emak",
            "Icon=%s" % APP_ID,
            "Terminal=false",
            "X-GNOME-Autostart-enabled=true",
            "X-GNOME-Autostart-Delay=5",
            "",
        ]), mode=0o644)
    else:
        try:
            os.unlink(AUTOSTART_FILE)
        except OSError:
            pass
