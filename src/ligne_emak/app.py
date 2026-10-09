"""Application GTK : fenêtre principale, logique d'appel, notifications, icône de barre."""

import os
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, VERSION, VOICEMAIL_NUMBER, store  # noqa: E402
from .engine import Account, Engine, normalize_number  # noqa: E402
from .ui import (AccountForm, CallView, DialPad, HistoryList, IncomingView, add_class, clock,  # noqa: E402
                 format_number, human_duration, icon, label, number_from_uri)


def data_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.environ.get("LIGNE_EMAK_DATA"), os.path.join(here, "..", "..", "data"), "/usr/share/ligne-emak"):
        if d and os.path.exists(os.path.join(d, "style.css")):
            return os.path.abspath(d)
    return "/usr/share/ligne-emak"


def friendly_failure(detail):
    d = (detail or "").lower()
    if any(k in d for k in ("401", "403", "refusé", "forbidden", "unauthorized")):
        return "EMAK refuse le poste ou le mot de passe. Vérifiez-les dans les paramètres."
    if any(k in d for k in ("timed out", "timeout", "unreachable", "resolve", "dns", "no such",
                            "network", "connection refused", "503", "408")):
        return "Le serveur EMAK ne répond pas. Vérifiez la connexion Internet et le domaine de la compagnie."
    return detail or "Connexion impossible."


CLOSE_REASONS = {
    "486": "Occupé", "600": "Occupé", "603": "Refusé", "480": "Indisponible", "404": "Numéro introuvable",
    "484": "Numéro incomplet", "408": "Pas de réponse", "487": "Annulé", "503": "Service indisponible",
}


class Call:
    def __init__(self, direction, number="", name=""):
        self.id = None
        self.direction = direction        # in | out
        self.number = number
        self.name = name
        self.state = "ringing" if direction == "in" else "dialing"
        self.created = time.time()
        self.answered = None
        self.muted = False
        self.held = False
        self.user_hangup = False
        self.closed = False

    @property
    def display(self):
        return self.name or format_number(self.number) or "Numéro masqué"


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        s = app.settings
        self.set_default_size(int(s.get("width", 380)), int(s.get("height", 660)))
        self.set_size_request(340, 560)
        self.set_icon_name(APP_ID)
        add_class(self, "le-window")

        # -- barre de titre
        hb = Gtk.HeaderBar(show_close_button=True)
        tbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        tbox.pack_start(label(APP_NAME, "title"), False, False, 0)
        srow = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.dot = add_class(Gtk.Box(valign=Gtk.Align.CENTER), "le-dot")
        self.dot.set_size_request(8, 8)
        self.status_lb = label("Hors ligne", "le-subtitle")
        srow.pack_start(self.dot, False, False, 0)
        srow.pack_start(self.status_lb, False, False, 0)
        tbox.pack_start(srow, False, False, 0)
        hb.set_custom_title(tbox)
        menu = Gio.Menu()
        sec1 = Gio.Menu()
        sec1.append("Paramètres du poste…", "app.settings")
        sec1.append("Démarrer avec l'ordinateur", "app.autostart")
        menu.append_section(None, sec1)
        sec2 = Gio.Menu()
        sec2.append("Journal technique", "app.log")
        sec2.append("À propos de Ligne EMAK", "app.about")
        sec2.append("Quitter", "app.quit")
        menu.append_section(None, sec2)
        mb = Gtk.MenuButton(menu_model=menu, valign=Gtk.Align.CENTER)
        mb.add(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        mb.set_tooltip_text("Menu")
        hb.pack_end(mb)
        self.set_titlebar(hb)

        # -- pages
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=180)
        self.add(self.stack)
        self.stack.add_named(self._build_setup(), "setup")
        self.stack.add_named(self._build_phone(), "phone")
        self.incoming = IncomingView()
        self.stack.add_named(self.incoming, "incoming")
        self.callview = CallView()
        self.stack.add_named(self.callview, "call")

        self.connect("key-press-event", self._on_key)
        self.connect("delete-event", self._on_delete)
        self.connect("configure-event", self._on_configure)

    # ---------------------------------------------------------------- construction

    def _build_setup(self):
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=28, margin_end=28,
                      margin_top=26, margin_bottom=24)
        img = Gtk.Image.new_from_icon_name(APP_ID, Gtk.IconSize.DIALOG)
        img.set_pixel_size(72)
        box.pack_start(img, False, False, 0)
        box.pack_start(label("Connectez votre ligne EMAK", "le-setup-title"), False, False, 6)
        intro = label("Entrez les informations de votre poste. Votre ordinateur sonnera comme "
                      "votre téléphone de bureau.", "dim-label", wrap=True)
        intro.set_justify(Gtk.Justification.CENTER)
        box.pack_start(intro, False, False, 0)
        self.setup_form = AccountForm(self.app.settings, show_autostart=True, autostart=True,
                                      button_text="Connecter mon poste")
        self.setup_form.set_margin_top(14)
        self.setup_form.connect("submit", lambda f: self.app.save_account(f, from_setup=True))
        box.pack_start(self.setup_form, False, False, 0)
        sw.add(box)
        return sw

    def _build_phone(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.banner = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        bb = add_class(Gtk.Box(spacing=10), "le-banner")
        self.banner_lb = label("", xalign=0, wrap=True)
        bb.pack_start(self.banner_lb, True, True, 0)
        self.banner_btn = Gtk.Button(label="Modifier", valign=Gtk.Align.CENTER)
        self.banner_btn.connect("clicked", lambda *_: self.app.activate_action("settings", None))
        bb.pack_start(self.banner_btn, False, False, 0)
        self.banner.add(bb)
        outer.pack_start(self.banner, False, False, 0)

        self.inner = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT, transition_duration=160)
        sw = Gtk.StackSwitcher(stack=self.inner, halign=Gtk.Align.CENTER, margin_top=10, margin_bottom=2)
        add_class(sw, "le-switcher")
        outer.pack_start(sw, False, False, 0)
        outer.pack_start(self.inner, True, True, 0)

        # -- clavier
        kp = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.number = Gtk.Entry(placeholder_text="Numéro ou poste", xalign=0.5, margin_start=24,
                                margin_end=24, margin_top=18)
        add_class(self.number, "le-number")
        self.number.set_input_purpose(Gtk.InputPurpose.PHONE)
        self.number.connect("activate", lambda *_: self.app.dial_from_entry())
        self.number.connect("changed", self._number_changed)
        kp.pack_start(self.number, False, False, 0)
        self.number_hint = label(" ", "le-number-hint dim-label")
        kp.pack_start(self.number_hint, False, False, 0)

        pad = DialPad()
        pad.connect("digit", self._pad_digit)
        pad.set_valign(Gtk.Align.CENTER)
        pad.set_vexpand(True)
        kp.pack_start(pad, True, True, 0)

        row = Gtk.Grid(column_homogeneous=True, halign=Gtk.Align.CENTER, column_spacing=22,
                       margin_bottom=28, margin_top=6)
        vm_overlay = Gtk.Overlay(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.vm_btn = Gtk.Button()
        add_class(self.vm_btn, "le-round", "le-flat-round")
        self.vm_btn.set_size_request(64, 64)
        self.vm_btn.add(icon("voicemail", 24))
        self.vm_btn.set_tooltip_text("Messagerie vocale (%s)" % VOICEMAIL_NUMBER)
        self.vm_btn.set_can_focus(False)
        self.vm_btn.connect("clicked", lambda *_: self.app.dial(VOICEMAIL_NUMBER))
        vm_overlay.add(self.vm_btn)
        self.vm_badge = label("", "le-badge")
        self.vm_badge.set_halign(Gtk.Align.END)
        self.vm_badge.set_valign(Gtk.Align.START)
        self.vm_badge.set_no_show_all(True)
        vm_overlay.add_overlay(self.vm_badge)
        self.call_btn = Gtk.Button()
        add_class(self.call_btn, "le-round", "le-answer")
        self.call_btn.set_size_request(72, 72)
        self.call_btn.add(icon("call", 28))
        self.call_btn.set_tooltip_text("Appeler (Entrée)")
        self.call_btn.set_can_focus(False)
        self.call_btn.set_halign(Gtk.Align.CENTER)
        self.call_btn.connect("clicked", lambda *_: self.app.dial_from_entry())
        self.back_btn = Gtk.Button()
        add_class(self.back_btn, "le-round", "le-flat-round")
        self.back_btn.set_size_request(64, 64)
        self.back_btn.add(icon("backspace", 24))
        self.back_btn.set_tooltip_text("Effacer (appui long : tout effacer)")
        self.back_btn.set_can_focus(False)
        self.back_btn.set_halign(Gtk.Align.CENTER)
        self.back_btn.set_valign(Gtk.Align.CENTER)
        self.back_btn.connect("clicked", lambda *_: self._backspace())
        self.back_btn.connect("button-press-event", self._back_press)
        self.back_btn.connect("button-release-event", self._back_release)
        self._back_src = 0
        row.attach(vm_overlay, 0, 0, 1, 1)
        row.attach(self.call_btn, 1, 0, 1, 1)
        row.attach(self.back_btn, 2, 0, 1, 1)
        kp.pack_start(row, False, False, 0)
        self.inner.add_titled(kp, "keypad", "Clavier")

        # -- historique
        self.history_list = HistoryList(self.app.history)
        self.history_list.connect("call-number", lambda _w, n: self.app.dial(n))
        self.inner.add_titled(self.history_list, "history", "Historique")
        self.inner.connect("notify::visible-child-name", self._inner_changed)
        self._number_changed(self.number)
        return outer

    # ---------------------------------------------------------------- comportements

    def _inner_changed(self, *_):
        if self.inner.get_visible_child_name() == "history":
            self.app.mark_missed_seen()
        else:
            self.number.grab_focus_without_selecting()

    def _pad_digit(self, pad, d):
        pos = self.number.get_position()
        self.number.insert_text(d, pos)
        self.number.set_position(pos + len(d))

    def _backspace(self):
        if self._back_long:
            self._back_long = False
            return
        text = self.number.get_text()
        pos = self.number.get_position() or len(text)
        if pos > 0:
            self.number.delete_text(pos - 1, pos)

    _back_long = False

    def _back_press(self, *_):
        def clear():
            self._back_src = 0
            self._back_long = True
            self.number.set_text("")
            return False
        self._back_long = False
        self._back_src = GLib.timeout_add(600, clear)
        return False

    def _back_release(self, *_):
        if self._back_src:
            GLib.source_remove(self._back_src)
            self._back_src = 0
        return False

    def _number_changed(self, entry):
        text = entry.get_text()
        n = normalize_number(text)
        hint = format_number(n) if n and format_number(n) != n else " "
        self.number_hint.set_text(hint)
        self.back_btn.set_opacity(1 if text else 0.35)

    def _on_key(self, win, event):
        page = self.stack.get_visible_child_name()
        ch = chr(Gdk.keyval_to_unicode(event.keyval)) if Gdk.keyval_to_unicode(event.keyval) else ""
        mods = event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK)
        if page == "call" and not mods and ch and ch in "0123456789*#":
            focus = self.get_focus()
            if not isinstance(focus, Gtk.Entry):
                self.app.send_dtmf(ch)
                return True
        if page == "incoming" and event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.app.answer()
            return True
        if page == "phone" and self.inner.get_visible_child_name() == "keypad" and not mods:
            if not self.number.has_focus() and ch and ch in "0123456789*#+":
                self.number.grab_focus_without_selecting()
                return False
        return False

    def _on_delete(self, *_):
        self.app.hide_window()
        return True

    def _on_configure(self, *_):
        if not self.is_maximized():
            w, h = self.get_size()
            self.app.settings["width"], self.app.settings["height"] = w, h
        return False

    # ---------------------------------------------------------------- état

    def set_status(self, state, text, banner=None, banner_button=True):
        ctx = self.dot.get_style_context()
        for c in ("online", "busy", "error"):
            ctx.remove_class(c)
        if state:
            ctx.add_class(state)
        self.status_lb.set_text(text)
        self.banner_lb.set_text(banner or "")
        self.banner_btn.set_visible(banner_button)
        self.banner.set_reveal_child(bool(banner))

    def set_voicemail(self, new):
        self.vm_badge.set_text(str(new) if new < 100 else "99+")
        self.vm_badge.set_visible(new > 0)
        self.vm_btn.set_tooltip_text(
            ("%d nouveau%s message%s — " % (new, "x" if new > 1 else "", "s" if new > 1 else "") if new else "")
            + "Messagerie vocale (%s)" % VOICEMAIL_NUMBER)

    def set_missed(self, n):
        child = self.history_list
        self.inner.child_set_property(child, "needs-attention", n > 0)
        self.inner.child_set_property(child, "title", "Historique (%d)" % n if n else "Historique")


class LigneEmakApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        GLib.set_application_name(APP_NAME)
        GLib.set_prgname(APP_ID)
        self.window = None
        self.engine = None
        self.calls = []            # dans l'ordre de création ; le dernier non terminé = appel courant
        self.pending_out = None
        self.settings = store.load_settings()
        self.history = None
        self.indicator = None
        self.mwi_new = 0
        self._tick = 0
        self._seen_missed = self.settings.get("missed_seen", 0)
        self._net_src = 0
        self._started_hidden = False
        self._end_src = 0

    # ---------------------------------------------------------------- démarrage

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.history = store.History()
        ddir = data_dir()
        Gtk.IconTheme.get_default().append_search_path(os.path.join(ddir, "icons"))
        Gtk.IconTheme.get_default().append_search_path(ddir)
        css = Gtk.CssProvider()
        try:
            css.load_from_path(os.path.join(ddir, "style.css"))
            Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                                     Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        except GLib.Error as e:
            print("CSS:", e.message, file=sys.stderr)

        for name, cb in (("settings", self.on_settings), ("log", self.on_log), ("about", self.on_about),
                         ("quit", self.on_quit), ("show", lambda *_: self.show_window()),
                         ("answer", lambda *_: self.answer()), ("decline", lambda *_: self.decline()),
                         ("history", self.on_show_history)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        auto = Gio.SimpleAction.new_stateful("autostart", None, GLib.Variant.new_boolean(store.autostart_enabled()))
        auto.connect("change-state", self.on_autostart)
        self.add_action(auto)
        self.set_accels_for_action("app.quit", ["<Primary>q"])
        self.set_accels_for_action("app.settings", ["<Primary>comma"])

        self.engine = Engine(store.CONFIG_DIR, store.CACHE_DIR)
        self.engine.connect("status", self.on_engine_status)
        self.engine.connect("call-event", self.on_call_event)
        self.engine.connect("mwi", self.on_mwi)

        self.window = MainWindow(self)
        self.window.show_all()
        self.window.hide()
        self.wire_call_view()
        self._setup_indicator()
        self._watch_network()
        self.hold()  # l'application reste active fenêtre fermée, pour recevoir les appels

        acc = self.current_account()
        if acc and acc.is_complete():
            self.window.stack.set_visible_child_name("phone")
            self.engine.start(acc)
        else:
            self.window.stack.set_visible_child_name("setup")
        self.on_engine_status(self.engine, self.engine.state, self.engine.detail)
        self.window.set_missed(self.unseen_missed())
        self._tick = GLib.timeout_add_seconds(1, self._on_tick)

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        background = "--background" in args
        first = not getattr(self, "_activated_once", False)
        self._activated_once = True
        uri = next((a for a in args if a.lower().startswith(("tel:", "callto:", "sip:", "sips:"))), None)
        acc = self.current_account()
        configured = acc is not None and acc.is_complete()
        if uri:
            self.show_window()
            if configured:
                self.dial(normalize_number(uri))
        elif not (background and first and configured):
            self.show_window()
        return 0

    def current_account(self):
        s = self.settings
        if not s.get("extension") or not s.get("domain"):
            return None
        pw = store.load_password(s["extension"], s["domain"])
        return Account(s["extension"], pw, s["domain"], s.get("transport", "udp"), s.get("display_name", ""))

    # ---------------------------------------------------------------- fenêtre

    def show_window(self):
        if self.window:
            self.window.show()
            t = Gtk.get_current_event_time()
            if t:
                self.window.present_with_time(t)
            else:
                self.window.present()

    def hide_window(self):
        self.window.hide()
        store.save_settings(self.settings)
        if not self.settings.get("close_hint_shown"):
            self.settings["close_hint_shown"] = True
            store.save_settings(self.settings)
            n = Gio.Notification.new("Ligne EMAK reste active")
            n.set_body("Vous continuerez à recevoir vos appels. Pour quitter complètement, "
                       "utilisez le menu ☰ → Quitter.")
            n.set_icon(Gio.ThemedIcon.new(APP_ID))
            n.set_default_action("app.show")
            self.send_notification("hint", n)

    # ---------------------------------------------------------------- icône de barre (facultative)

    def _setup_indicator(self):
        AppIndicator = None
        for ns, ver in (("AyatanaAppIndicator3", "0.1"), ("AppIndicator3", "0.1")):
            try:
                gi.require_version(ns, ver)
                AppIndicator = getattr(__import__("gi.repository", fromlist=[ns]), ns)
                break
            except (ValueError, ImportError, AttributeError):
                continue
        if AppIndicator is None:
            return
        try:
            ind = AppIndicator.Indicator.new("ligne-emak", APP_ID,
                                             AppIndicator.IndicatorCategory.COMMUNICATIONS)
            ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            ind.set_title(APP_NAME)
            m = Gtk.Menu()
            self._ind_status = Gtk.MenuItem(label="Hors ligne")
            self._ind_status.set_sensitive(False)
            show = Gtk.MenuItem(label="Ouvrir Ligne EMAK")
            show.connect("activate", lambda *_: self.show_window())
            vm = Gtk.MenuItem(label="Écouter la messagerie")
            vm.connect("activate", lambda *_: (self.show_window(), self.dial(VOICEMAIL_NUMBER)))
            quit_ = Gtk.MenuItem(label="Quitter")
            quit_.connect("activate", lambda *_: self.on_quit())
            for it in (self._ind_status, Gtk.SeparatorMenuItem(), show, vm, Gtk.SeparatorMenuItem(), quit_):
                m.append(it)
            m.show_all()
            ind.set_menu(m)
            ind.set_secondary_activate_target(show)
            self.indicator = ind
        except Exception as e:  # noqa: BLE001 - l'icône de barre est un bonus
            print("Indicateur indisponible :", e, file=sys.stderr)

    # ---------------------------------------------------------------- réseau / mise en veille

    def _watch_network(self):
        try:
            mon = Gio.NetworkMonitor.get_default()
            self._last_ip = self._local_ip()
            mon.connect("network-changed", self._on_network_changed)
        except Exception:  # noqa: BLE001
            pass
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            bus.signal_subscribe("org.freedesktop.login1", "org.freedesktop.login1.Manager",
                                 "PrepareForSleep", "/org/freedesktop/login1", None,
                                 Gio.DBusSignalFlags.NONE, self._on_sleep, None)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _local_ip():
        """Adresse IP source de la route par défaut (aucun paquet n'est envoyé)."""
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("198.51.100.1", 9))
            return s.getsockname()[0]
        except OSError:
            return None
        finally:
            s.close()

    def _on_network_changed(self, mon, available):
        ip = self._local_ip() if available else None
        changed = ip != getattr(self, "_last_ip", ip)
        self._last_ip = ip
        if available and (changed or self.engine.state != "online"):
            self._schedule_reconnect(4)

    def _on_sleep(self, conn, sender, path, iface, signal, params, data):
        going_to_sleep = params.unpack()[0]
        if not going_to_sleep:
            self._schedule_reconnect(3)

    def _schedule_reconnect(self, delay):
        """Après un changement de réseau ou un réveil, on relance le moteur (hors appel)."""
        if self._net_src:
            GLib.source_remove(self._net_src)

        def go():
            self._net_src = 0
            if self.active_calls():
                self._net_src = GLib.timeout_add_seconds(10, go)
                return False
            acc = self.current_account()
            if acc and acc.is_complete() and Gio.NetworkMonitor.get_default().get_network_available():
                self.engine.start(acc)
            return False

        self._net_src = GLib.timeout_add_seconds(delay, go)

    # ---------------------------------------------------------------- état du moteur

    def on_engine_status(self, eng, state, detail):
        s = self.settings
        ext = s.get("extension", "")
        w = self.window
        if state == "online":
            w.set_status("online", "En ligne · poste %s" % ext)
        elif state in ("starting", "registering"):
            w.set_status("busy", "Connexion…")
        elif state == "failed":
            w.set_status("error", "Non connecté", friendly_failure(detail))
        elif state == "error":
            w.set_status("error", "Moteur arrêté", detail or "Le moteur SIP s'est arrêté.", banner_button=False)
        elif state == "missing":
            w.set_status("error", "Moteur manquant", detail, banner_button=False)
        else:
            w.set_status(None, "Hors ligne")
        if self.indicator is not None:
            self._ind_status.set_label(w.status_lb.get_text())

    def on_mwi(self, eng, new, old):
        prev, self.mwi_new = self.mwi_new, new
        self.window.set_voicemail(new)
        if new > prev:
            n = Gio.Notification.new("Nouveau message vocal" if new - prev == 1 else
                                     "%d nouveaux messages vocaux" % (new - prev))
            n.set_body("Vous avez %d message%s non écouté%s." % (new, "s" if new > 1 else "",
                                                                    "s" if new > 1 else ""))
            n.set_icon(Gio.ThemedIcon.new(APP_ID))
            n.set_default_action("app.show")
            self.send_notification("mwi", n)
        elif new == 0:
            self.withdraw_notification("mwi")

    # ---------------------------------------------------------------- appels : actions

    def active_calls(self):
        return [c for c in self.calls if not c.closed]

    def current_call(self):
        act = self.active_calls()
        return act[-1] if act else None

    def dial_from_entry(self):
        text = self.window.number.get_text()
        if not text.strip():
            # Entrée sur un champ vide = rappeler le dernier numéro composé
            last = next((i for i in self.history.items if i["direction"] == "out"), None)
            if last:
                self.window.number.set_text(last["number"])
            return
        self.dial(text)

    def dial(self, raw):
        number = normalize_number(raw)
        if not number:
            return
        if self.engine.state != "online":
            self.toast("Votre poste n'est pas connecté. Vérifiez l'état en haut de la fenêtre.")
            return
        if self.active_calls():
            self.toast("Un appel est déjà en cours.")
            return
        call = Call("out", number)
        self.calls.append(call)
        self.pending_out = call
        self.window.number.set_text("")
        self.show_call_screen(call)

        def done(ok, data):
            if not ok and not call.closed:
                call.closed = True
                self.pending_out = None
                self.history.add(call.number, "", "out", "failed", call.created, 0)
                self.window.history_list.refresh()
                self.toast("Impossible de lancer l'appel : %s" % (data.strip() or "erreur"))
                self.after_call_ended(call, "Échec de l'appel")

        self.engine.dial(number, done)

    def answer(self):
        call = next((c for c in reversed(self.active_calls()) if c.direction == "in" and c.state == "ringing"),
                    None)
        if not call:
            return
        others = [c for c in self.active_calls() if c is not call and c.state == "active"]
        for o in others:
            o.held = True            # baresip met l'appel en cours en attente
        self.engine.accept()
        self.withdraw_notification("incoming")
        self.show_window()

    def decline(self):
        call = next((c for c in reversed(self.active_calls()) if c.direction == "in" and c.state == "ringing"),
                    None)
        if not call:
            return
        call.user_hangup = True
        self.engine.hangup()
        self.withdraw_notification("incoming")

    def hangup(self):
        call = self.current_call()
        if not call:
            return
        call.user_hangup = True
        self.engine.hangup()

    def toggle_mute(self):
        call = self.current_call()
        if not call or call.state != "active":
            self.window.callview.mute.button.set_active(call.muted if call else False)
            return

        def done(ok, data):
            if ok:
                call.muted = "un-muted" not in data
            self.refresh_call_screen()

        self.engine.toggle_mute(done)

    def toggle_hold(self):
        call = self.current_call()
        if not call or call.state != "active":
            self.refresh_call_screen()
            return
        want = not call.held

        def done(ok, data):
            if ok:
                call.held = want
            self.refresh_call_screen()

        (self.engine.hold if want else self.engine.resume)(done)

    def send_dtmf(self, digit):
        call = self.current_call()
        if call and call.state == "active":
            self.engine.send_dtmf(digit)
            lb = self.window.callview.dtmf_label
            lb.set_text((lb.get_text() + digit)[-24:])

    def transfer(self):
        cv = self.window.callview
        target = normalize_number(cv.transfer_entry.get_text())
        call = self.current_call()
        if not target or not call:
            return

        def done(ok, data):
            if not ok:
                self.toast("Le transfert a échoué.")
            else:
                self.toast("Transfert vers %s…" % format_number(target))
            cv.show_actions()

        self.engine.transfer(target, done)

    # ---------------------------------------------------------------- appels : événements

    def _find(self, ev):
        cid = ev.get("id")
        for c in self.calls:
            if cid and c.id == cid:
                return c
        if ev.get("direction") == "outgoing" and self.pending_out and self.pending_out.id is None:
            c = self.pending_out
            c.id = cid
            self.pending_out = None
            return c
        return None

    def on_call_event(self, eng, ev):
        typ = ev.get("type")
        call = self._find(ev)
        if typ == "CALL_INCOMING":
            if call is None:
                call = Call("in", number_from_uri(ev.get("peeruri") or ev.get("param")),
                            ev.get("peerdisplayname", ""))
                call.id = ev.get("id")
                self.calls.append(call)
            waiting = any(c is not call and c.state == "active" for c in self.active_calls())
            self.window.incoming.show_call(call.name, call.number, waiting)
            self.window.stack.set_visible_child_name("incoming")
            self.notify_incoming(call)
            self.show_window()
            return
        if call is None:
            return
        if typ in ("CALL_RINGING", "CALL_PROGRESS"):
            call.state = "ringing-out"
            if ev.get("peerdisplayname") and not call.name:
                call.name = ev["peerdisplayname"]
        elif typ == "CALL_ESTABLISHED":
            call.state = "active"
            call.answered = time.time()
            if ev.get("peerdisplayname") and not call.name:
                call.name = ev["peerdisplayname"]
            self.withdraw_notification("incoming")
            self.show_call_screen(call)
        elif typ == "CALL_CLOSED":
            self.on_call_closed(call, ev.get("param", ""))
            return
        elif typ == "TRANSFER_FAILED":
            self.toast("Le transfert a échoué.")
        self.refresh_call_screen()

    def on_call_closed(self, call, reason):
        call.closed = True
        if self.pending_out is call:
            self.pending_out = None
        duration = time.time() - call.answered if call.answered else 0
        if call.direction == "in":
            status = "answered" if call.answered else ("declined" if call.user_hangup else "missed")
        else:
            status = "answered" if call.answered else ("cancelled" if call.user_hangup else "failed")
        self.history.add(call.number, call.name, call.direction, status, call.created, duration)
        self.window.history_list.refresh()
        self.withdraw_notification("incoming")
        if status == "missed":
            n = Gio.Notification.new("Appel manqué")
            n.set_body(call.display)
            n.set_icon(Gio.ThemedIcon.new(APP_ID))
            n.set_default_action("app.history")
            self.send_notification("missed", n)
            self.window.set_missed(self.unseen_missed())
        code = (reason or "").split(" ")[0]
        if call.answered:
            msg = "Appel terminé · %s" % clock(duration)
        elif call.direction == "out" and not call.user_hangup:
            msg = CLOSE_REASONS.get(code, "Appel non abouti")
        else:
            msg = "Appel terminé"
        self.after_call_ended(call, msg)

    def after_call_ended(self, call, msg):
        remaining = self.current_call()
        w = self.window
        if remaining:
            if remaining.direction == "in" and remaining.state == "ringing":
                w.incoming.show_call(remaining.name, remaining.number, False)
                w.stack.set_visible_child_name("incoming")
            else:
                self.show_call_screen(remaining)
            return
        if w.stack.get_visible_child_name() in ("call", "incoming"):
            cv = w.callview
            cv.state.set_text(msg)
            cv.hangup.set_sensitive(False)
            if self._end_src:
                GLib.source_remove(self._end_src)

            def back():
                self._end_src = 0
                if not self.active_calls():
                    cv.hangup.set_sensitive(True)
                    w.stack.set_visible_child_name("phone")
                    w.number.grab_focus_without_selecting()
                return False

            if w.stack.get_visible_child_name() == "incoming":
                back()
            else:
                self._end_src = GLib.timeout_add(1600, back)

    # ---------------------------------------------------------------- écran d'appel

    def show_call_screen(self, call):
        cv = self.window.callview
        if self._end_src:
            GLib.source_remove(self._end_src)
            self._end_src = 0
        cv.hangup.set_sensitive(True)
        cv.reset()
        self.window.stack.set_visible_child_name("call")
        self.refresh_call_screen()

    def refresh_call_screen(self):
        call = self.current_call()
        if not call or call.direction == "in" and call.state == "ringing":
            return
        cv = self.window.callview
        cv.avatar.update(call.name, call.number)
        cv.name.set_text(call.display)
        cv.number.set_text(format_number(call.number) if call.name else "")
        cv.number.set_visible(bool(call.name))
        active = call.state == "active"
        for t in (cv.mute, cv.hold, cv.keypad, cv.transfer):
            t.button.set_sensitive(active)
        self._syncing = True
        cv.mute.button.set_active(call.muted)
        cv.hold.button.set_active(call.held)
        self._syncing = False
        cv.mute.text.set_text("Micro coupé" if call.muted else "Muet")
        cv.hold.text.set_text("En attente" if call.held else "Attente")
        others = [c for c in self.active_calls() if c is not call]
        cv.held_label.set_text("%s · en attente" % others[0].display if others else "")
        cv.held_bar.set_reveal_child(bool(others))
        self._update_call_state_label()

    def _update_call_state_label(self):
        call = self.current_call()
        if not call:
            return
        cv = self.window.callview
        if call.state == "dialing":
            text = "Appel en cours…"
        elif call.state == "ringing-out":
            text = "Sonnerie…"
        elif call.state == "active":
            text = clock(time.time() - call.answered)
            if call.held:
                text = "En attente · " + text
            elif call.muted:
                text = "Micro coupé · " + text
        else:
            text = ""
        cv.state.set_text(text)

    _syncing = False

    def _mute_clicked(self, *_):
        if not self._syncing:
            self.toggle_mute()

    def _hold_clicked(self, *_):
        if not self._syncing:
            self.toggle_hold()

    def wire_call_view(self):
        cv = self.window.callview
        cv.mute.button.connect("clicked", self._mute_clicked)
        cv.hold.button.connect("clicked", self._hold_clicked)
        cv.hangup.connect("clicked", lambda *_: self.hangup())
        cv.dtmf_pad.connect("digit", lambda _p, d: self.send_dtmf(d))
        cv.transfer_go.connect("clicked", lambda *_: self.transfer())
        cv.transfer_entry.connect("activate", lambda *_: self.transfer())
        inc = self.window.incoming
        inc.answer.connect("clicked", lambda *_: self.answer())
        inc.decline.connect("clicked", lambda *_: self.decline())

    def _on_tick(self):
        if self.window.stack.get_visible_child_name() == "call":
            self._update_call_state_label()
        return True

    # ---------------------------------------------------------------- notifications

    def notify_incoming(self, call):
        n = Gio.Notification.new("Appel entrant")
        n.set_body(call.display if not call.name else "%s\n%s" % (call.name, format_number(call.number)))
        n.set_icon(Gio.ThemedIcon.new(APP_ID))
        n.set_priority(Gio.NotificationPriority.URGENT)
        n.add_button("Refuser", "app.decline")
        n.add_button("Répondre", "app.answer")
        n.set_default_action("app.show")
        self.send_notification("incoming", n)

    def toast(self, text):
        w = self.window
        w.set_status(w.dot.get_style_context().has_class("online") and "online" or None,
                     w.status_lb.get_text(), text, banner_button=False)

        def hide():
            self.on_engine_status(self.engine, self.engine.state, self.engine.detail)
            return False

        GLib.timeout_add_seconds(5, hide)

    def unseen_missed(self):
        return self.history.missed_since(self._seen_missed)

    def mark_missed_seen(self):
        self._seen_missed = int(time.time())
        self.settings["missed_seen"] = self._seen_missed
        store.save_settings(self.settings)
        self.window.set_missed(0)
        self.withdraw_notification("missed")

    # ---------------------------------------------------------------- compte

    def save_account(self, form, from_setup=False, dialog=None):
        v = form.values()
        acc = Account(v["extension"], v["password"], v["domain"], v["transport"], v["display_name"])
        problems = acc.problems()
        if problems:
            form.show_error(problems[0])
            return False
        form.show_error("")
        old = (self.settings.get("extension"), self.settings.get("domain"))
        if old[0] and old != (acc.extension, acc.domain):
            store.forget_password(*old)
        where = store.store_password(acc.extension, acc.domain, acc.password)
        self.settings.update({"extension": acc.extension, "domain": acc.domain, "transport": acc.transport,
                              "display_name": acc.display_name})
        store.save_settings(self.settings)
        if v.get("autostart") is not None:
            store.set_autostart(v["autostart"])
            self.lookup_action("autostart").set_state(GLib.Variant.new_boolean(v["autostart"]))
        if where == "file":
            print("Trousseau indisponible : mot de passe gardé dans un fichier protégé (600).", file=sys.stderr)
        self.window.stack.set_visible_child_name("phone")
        self.window.inner.set_visible_child_name("keypad")
        self.engine.start(acc)
        return True

    def on_settings(self, *_):
        if self.active_calls():
            self.toast("Terminez l'appel avant de modifier les paramètres.")
            return
        s = self.settings
        pw = store.load_password(s.get("extension", ""), s.get("domain", "")) if s.get("extension") else ""
        dlg = Gtk.Dialog(title="Paramètres du poste", transient_for=self.window, modal=True,
                         use_header_bar=True)
        dlg.add_button("Annuler", Gtk.ResponseType.CANCEL)
        save = dlg.add_button("Enregistrer", Gtk.ResponseType.OK)
        add_class(save, "suggested-action")
        dlg.set_default_size(400, -1)
        form = AccountForm(s, password=pw)
        form.set_margin_start(22)
        form.set_margin_end(22)
        form.set_margin_top(18)
        form.set_margin_bottom(18)
        form.connect("submit", lambda f: dlg.response(Gtk.ResponseType.OK))
        dlg.get_content_area().add(form)
        dlg.show_all()
        form.focus_first_empty()
        while True:
            r = dlg.run()
            if r != Gtk.ResponseType.OK or self.save_account(form, dialog=dlg):
                break
        dlg.destroy()

    def on_autostart(self, action, value):
        action.set_state(value)
        store.set_autostart(value.get_boolean())

    def on_show_history(self, *_):
        self.show_window()
        if self.window.stack.get_visible_child_name() == "phone":
            self.window.inner.set_visible_child_name("history")

    def on_log(self, *_):
        path = self.engine.log_path
        if not os.path.exists(path):
            self.toast("Aucun journal pour l'instant.")
            return
        try:
            Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(path), None)
        except GLib.Error:
            self.toast("Journal : %s" % path)

    def on_about(self, *_):
        dlg = Gtk.AboutDialog(transient_for=self.window, modal=True)
        dlg.set_program_name(APP_NAME)
        dlg.set_version(VERSION)
        dlg.set_logo_icon_name(APP_ID)
        dlg.set_comments("Votre poste EMAK Telecom sur votre ordinateur Linux.\n"
                         "Application indépendante, non affiliée à EMAK Telecom.\n"
                         "Moteur SIP : baresip.")
        dlg.set_license_type(Gtk.License.MIT_X11)
        dlg.run()
        dlg.destroy()

    def on_quit(self, *_):
        if self.active_calls():
            dlg = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
                                    buttons=Gtk.ButtonsType.NONE, text="Un appel est en cours")
            dlg.format_secondary_text("Quitter mettra fin à l'appel.")
            dlg.add_button("Annuler", Gtk.ResponseType.CANCEL)
            add_class(dlg.add_button("Quitter", Gtk.ResponseType.OK), "destructive-action")
            r = dlg.run()
            dlg.destroy()
            if r != Gtk.ResponseType.OK:
                return
        store.save_settings(self.settings)
        self.withdraw_notification("incoming")
        self.engine.stop_blocking()
        self.release()
        self.quit()


def main(argv=None):
    app = LigneEmakApp()
    return app.run(argv if argv is not None else sys.argv)
