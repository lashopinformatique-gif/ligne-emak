"""Interface du choix des appareils audio : fenêtre « Micro et haut-parleur » et bouton d'appel."""

import os

from gi.repository import GLib, Gtk, Pango

from . import audio
from .ui import add_class, icon, label

OUTPUT, INPUT, RING = "audio_output", "audio_input", "audio_ring"


def device_icon(name):
    n = (name or "").lower()
    if any(k in n for k in ("headset", "headphone", "bluez", "usb", "casque")):
        return "headset"
    return "speaker"


def describe(app, key, name):
    """Libellé d'un choix (appareil branché, débranché ou par défaut)."""
    devs = app.devices
    if key == RING and name == audio.SAME_AS_OUTPUT:
        return "Même appareil que la voix"
    if not name:
        default = devs.sink(devs.default_sink) if key != INPUT else devs.source(devs.default_source)
        return "Par défaut du système" + (" (%s)" % default.description if default else "")
    dev = devs.source(name) if key == INPUT else devs.sink(name)
    if dev:
        return dev.description
    known = app.settings.get("audio_names", {}).get(name, name)
    return "%s — débranché" % known if devs.ok else known


def choices(app, key):
    """[(nom, libellé)] pour une liste déroulante, en gardant visible un choix débranché."""
    devs = app.devices
    items = []
    if key == RING:
        items.append((audio.SAME_AS_OUTPUT, describe(app, key, audio.SAME_AS_OUTPUT)))
    items.append(("", describe(app, key, "")))
    for d in (devs.sources if key == INPUT else devs.sinks):
        items.append((d.name, d.description))
    current = app.settings.get(key, audio.SAME_AS_OUTPUT if key == RING else "")
    if current not in [n for n, _ in items]:
        items.append((current, describe(app, key, current)))
    return items, current


class AudioDialog(Gtk.Dialog):
    """Choix du haut-parleur (voix), du micro et de la sonnerie, avec test et vumètre."""

    def __init__(self, app):
        super().__init__(title="Micro et haut-parleur", transient_for=app.window, modal=False,
                         use_header_bar=True, destroy_with_parent=True)
        self.app = app
        self.set_default_size(440, -1)
        self.meter = audio.LevelMeter()
        self.meter.connect("level", self._on_level)
        self.player = audio.SoundTest()
        self._peak = 0.0
        self._updating = False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_start=22, margin_end=22,
                      margin_top=18, margin_bottom=20)
        self.warning = label("", "le-audio-warning", xalign=0, wrap=True)
        self.warning.set_no_show_all(True)
        box.pack_start(self.warning, False, False, 0)

        self.combos = {}
        self.out_test = self._test_button("Tester le haut-parleur", self._test_output)
        box.pack_start(self._section("speaker", "Voix de l'appel (haut-parleur ou casque)", OUTPUT,
                                     self.out_test), False, False, 0)

        self.level = Gtk.LevelBar(min_value=0, max_value=1, hexpand=True, valign=Gtk.Align.CENTER)
        self.level.set_mode(Gtk.LevelBarMode.CONTINUOUS)
        for name in ("low", "high", "full"):
            self.level.remove_offset_value(name)
        add_class(self.level, "le-meter")
        self.level.set_size_request(-1, 8)
        mic_extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        mic_extra.pack_start(self.level, False, False, 0)
        self.level_hint = label("Parlez : la barre doit bouger.", "le-hint dim-label", xalign=0)
        mic_extra.pack_start(self.level_hint, False, False, 0)
        box.pack_start(self._section("mic", "Micro", INPUT, None, below=mic_extra), False, False, 0)

        self.ring_test = self._test_button("Tester la sonnerie", self._test_ring)
        box.pack_start(self._section("bell", "Sonnerie des appels entrants", RING, self.ring_test,
                                     hint="Avec un casque, choisissez les haut-parleurs de l'ordinateur "
                                          "pour entendre sonner même sans le casque sur les oreilles."),
                     False, False, 0)

        box.pack_start(label("Un casque branché plus tard (USB ou Bluetooth) apparaît ici tout seul. "
                             "S'il est débranché, l'appareil par défaut prend le relais.",
                             "le-hint dim-label", xalign=0, wrap=True), False, False, 0)
        self.get_content_area().add(box)
        self.connect("response", lambda *_: self.destroy())
        self.connect("destroy", self._on_destroy)
        self.connect("map", lambda *_: self._start_meter())
        self.refresh()

    # ---------------------------------------------------------------- construction

    def _section(self, icon_name, title, key, button, below=None, hint=None):
        sec = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        head = Gtk.Box(spacing=8)
        head.pack_start(add_class(icon(icon_name, 16), "dim-label"), False, False, 0)
        head.pack_start(label(title, "le-field-title", xalign=0), True, True, 0)
        sec.pack_start(head, False, False, 0)
        row = Gtk.Box(spacing=8)
        combo = Gtk.ComboBoxText(hexpand=True)
        combo.set_size_request(120, -1)
        for cell in combo.get_cells():
            cell.set_property("ellipsize", Pango.EllipsizeMode.END)
        combo.connect("changed", self._on_combo, key)
        self.combos[key] = combo
        row.pack_start(combo, True, True, 0)
        if button:
            row.pack_start(button, False, False, 0)
        sec.pack_start(row, False, False, 0)
        if below:
            sec.pack_start(below, False, False, 0)
        if hint:
            sec.pack_start(label(hint, "le-hint dim-label", xalign=0, wrap=True), False, False, 0)
        return sec

    def _test_button(self, tooltip, cb):
        b = Gtk.Button(valign=Gtk.Align.CENTER)
        b.add(icon("play", 14))
        b.set_tooltip_text(tooltip)
        b.connect("clicked", cb)
        return b

    # ---------------------------------------------------------------- données

    def refresh(self):
        """Recharge les listes (ouverture, branchement d'un appareil)."""
        self._updating = True
        for key, combo in self.combos.items():
            combo.remove_all()
            items, current = choices(self.app, key)
            for name, text in items:
                combo.append(name or "-", text)
            combo.set_active_id(current or "-")
        self._updating = False
        devs = self.app.devices
        problem = ""
        if not self.app.audio_backend:
            problem = ("Le greffon audio ALSA est absent : seul l'appareil par défaut est utilisable. "
                       "Installez-le avec : sudo apt install libasound2-plugins")
        elif not devs.ok:
            problem = ("Impossible de lister les appareils (%s). Seul l'appareil par défaut est proposé." %
                       (devs.error or "serveur de son injoignable"))
        self.warning.set_text(problem)
        self.warning.set_visible(bool(problem))
        usable = bool(self.app.audio_backend)
        for combo in self.combos.values():
            combo.set_sensitive(usable)
        self.out_test.set_sensitive(audio.SoundTest.available())
        self.ring_test.set_sensitive(audio.SoundTest.available())
        if self.get_mapped():
            self._start_meter()

    def _on_combo(self, combo, key):
        if self._updating:
            return
        cid = combo.get_active_id()
        if cid is None:
            return
        value = "" if cid == "-" else cid
        self.app.set_audio_pref(key, value)
        if key == INPUT:
            self._start_meter()

    # ---------------------------------------------------------------- vumètre et tests

    def _start_meter(self):
        name = self.app.current_route()["names"]["input"]
        ok = self.meter.start(name)
        self.level.set_sensitive(ok)
        self.level_hint.set_text("Parlez : la barre doit bouger." if ok else
                                 "Vumètre indisponible (paquet pulseaudio-utils).")

    def _on_level(self, meter, value):
        self._peak = max(value, self._peak * 0.85)
        self.level.set_value(min(1.0, self._peak * 1.6))

    def _test(self, button, path, sink):
        if self.player.playing:
            self.player.stop()
            return

        def done():
            for b in (self.out_test, self.ring_test):
                img = b.get_child()
                if img is not None:          # la fenêtre a pu être fermée entre-temps
                    img.set_from_icon_name("le-play-symbolic", Gtk.IconSize.BUTTON)
                    img.set_pixel_size(14)

        if self.player.play(path, sink, on_done=done):
            button.get_child().set_from_icon_name("le-stop-symbolic", Gtk.IconSize.BUTTON)
            button.get_child().set_pixel_size(14)

    def _test_output(self, button):
        self._test(button, audio.test_sound_path(self.app.engine.sound_dir()),
                   self.app.current_route()["names"]["output"])

    def _test_ring(self, button):
        self._test(button, os.path.join(self.app.engine.sound_dir(), "ring.wav"),
                   self.app.current_route()["names"]["ring"])

    def _on_destroy(self, *_):
        self.meter.stop()
        self.player.stop()
        if self.app.audio_dialog is self:
            self.app.audio_dialog = None


class RouteButton(Gtk.MenuButton):
    """Pastille « haut-parleur actuel ▾ » de l'écran d'appel, avec le choix rapide des appareils."""

    def __init__(self, app):
        super().__init__(halign=Gtk.Align.CENTER, relief=Gtk.ReliefStyle.NONE)
        self.app = app
        add_class(self, "le-route")
        self.set_can_focus(False)
        inner = Gtk.Box(spacing=8)
        self.img = icon("speaker", 16)
        self.text = label("", ellipsize=True)
        self.text.set_max_width_chars(26)
        inner.pack_start(self.img, False, False, 0)
        inner.pack_start(self.text, False, False, 0)
        inner.pack_start(Gtk.Image.new_from_icon_name("pan-down-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        self.add(inner)
        self.set_tooltip_text("Choisir le haut-parleur et le micro")
        self.pop = Gtk.Popover(relative_to=self, position=Gtk.PositionType.TOP)
        add_class(self.pop, "le-route-popover")
        self.set_popover(self.pop)
        self.connect("toggled", self._on_toggled)
        self._building = False

    def update(self):
        route = self.app.current_route()
        name = route["names"]["output"]
        text = describe(self.app, OUTPUT, name) if name else "Appareil par défaut"
        self.text.set_text(text)
        self.img.set_from_icon_name("le-%s-symbolic" % device_icon(name), Gtk.IconSize.BUTTON)
        self.img.set_pixel_size(16)

    def _on_toggled(self, *_):
        if not self.get_active():
            return
        self.app.refresh_devices()
        self._build()

    def rebuild(self):
        self._build()

    def _build(self):
        self._building = True
        for child in self.pop.get_children():
            self.pop.remove(child)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=14, margin_end=14,
                      margin_top=12, margin_bottom=10)
        box.set_size_request(270, -1)
        for key, title in ((OUTPUT, "Voix de l'appel"), (INPUT, "Micro")):
            if key == INPUT:
                box.pack_start(Gtk.Separator(margin_top=6, margin_bottom=6), False, False, 0)
            box.pack_start(label(title, "le-pop-title dim-label", xalign=0), False, False, 2)
            items, current = choices(self.app, key)
            group = None
            for name, text in items:
                rb = Gtk.RadioButton.new_with_label_from_widget(group, text)
                group = group or rb
                rb.get_child().set_ellipsize(Pango.EllipsizeMode.END)
                rb.get_child().set_max_width_chars(34)
                rb.set_active(name == current)
                rb.set_sensitive(bool(self.app.audio_backend) or name == "")
                rb.connect("toggled", self._picked, key, name)
                box.pack_start(rb, False, False, 0)
        box.pack_start(Gtk.Separator(margin_top=6, margin_bottom=4), False, False, 0)
        more = Gtk.ModelButton(text="Sonnerie et tests…")
        more.connect("clicked", lambda *_: GLib.idle_add(self._open_dialog))
        box.pack_start(more, False, False, 0)
        box.show_all()
        self.pop.add(box)
        self._building = False

    def _picked(self, rb, key, name):
        if self._building or not rb.get_active():
            return
        self.app.set_audio_pref(key, name, refresh_dialog=True)
        self.update()

    def _open_dialog(self):
        self.pop.popdown()
        self.app.open_audio_dialog()
        return False
