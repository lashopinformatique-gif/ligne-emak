"""Widgets de l'interface : formulaire de compte, clavier, écrans d'appel, historique."""

import time

from gi.repository import GLib, GObject, Gtk, Pango

from .engine import normalize_domain

KEYS = [
    ("1", ""), ("2", "ABC"), ("3", "DEF"),
    ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
    ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"),
    ("*", ""), ("0", "+"), ("#", ""),
]

MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
DAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]


# ---------------------------------------------------------------- utilitaires d'affichage

def icon(name, px=16):
    img = Gtk.Image.new_from_icon_name("le-%s-symbolic" % name, Gtk.IconSize.BUTTON)
    img.set_pixel_size(px)
    return img


def label(text="", css=None, xalign=None, wrap=False, ellipsize=False, selectable=False):
    lb = Gtk.Label(label=text)
    if css:
        for c in css.split():
            lb.get_style_context().add_class(c)
    if xalign is not None:
        lb.set_xalign(xalign)
    if wrap:
        lb.set_line_wrap(True)
        lb.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if ellipsize:
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    lb.set_selectable(selectable)
    return lb


def add_class(widget, *classes):
    ctx = widget.get_style_context()
    for c in classes:
        ctx.add_class(c)
    return widget


def format_number(n):
    """5145550123 -> (514) 555-0123 ; les postes courts restent tels quels."""
    d = (n or "").strip()
    if d.startswith("+1") and len(d) == 12:
        d = d[2:]
    if len(d) == 11 and d.startswith("1") and d.isdigit():
        d = d[1:]
    if len(d) == 10 and d.isdigit():
        return "(%s) %s-%s" % (d[:3], d[3:6], d[6:])
    return n or ""


def number_from_uri(uri):
    u = (uri or "").strip().strip("<>")
    for p in ("sip:", "sips:", "tel:"):
        if u.lower().startswith(p):
            u = u[len(p):]
    user = u.split("@")[0].split(";")[0]
    if user.startswith("+1") and len(user) == 12:
        user = user[2:]
    return user or u


def initials(name, number):
    words = [w for w in (name or "").replace("-", " ").split() if w[:1].isalpha()]
    if words:
        return (words[0][0] + (words[1][0] if len(words) > 1 else "")).upper()
    return ""


def clock(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%02d:%02d" % (m, s)


def human_duration(sec):
    sec = int(sec)
    if sec < 60:
        return "%d s" % sec
    m, s = divmod(sec, 60)
    if m < 60:
        return "%d min %02d s" % (m, s)
    h, m = divmod(m, 60)
    return "%d h %02d min" % (h, m)


def when(ts):
    t = time.localtime(ts)
    now = time.localtime()
    hhmm = "%d h %02d" % (t.tm_hour, t.tm_min)
    days = (time.mktime(now[:3] + (0, 0, 0) + now[6:]) - time.mktime(t[:3] + (0, 0, 0) + t[6:])) / 86400
    if days < 0.5:
        return "Aujourd'hui, " + hhmm
    if days < 1.5:
        return "Hier, " + hhmm
    if days < 6.5:
        return "%s %d %s, %s" % (DAYS[t.tm_wday], t.tm_mday, MONTHS[t.tm_mon - 1], hhmm)
    return "%d %s %d, %s" % (t.tm_mday, MONTHS[t.tm_mon - 1], t.tm_year, hhmm)


# ---------------------------------------------------------------- formulaire de compte

class AccountForm(Gtk.Box):
    """Poste, mot de passe, compagnie (domaine) et options. Émet « submit »."""

    __gsignals__ = {"submit": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, settings, password="", show_autostart=False, autostart=True, button_text=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        add_class(self, "le-form")

        self.ext = Gtk.Entry(text=settings.get("extension", ""), placeholder_text="ex. 101")
        self.ext.set_input_purpose(Gtk.InputPurpose.DIGITS)
        self.pw = Gtk.Entry(text=password, visibility=False, placeholder_text="Mot de passe du poste")
        self.pw.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.pw.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, "view-reveal-symbolic")
        self.pw.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Afficher le mot de passe")
        self.pw.connect("icon-press", self._toggle_pw)

        dom = settings.get("domain", "")
        if dom.endswith(".emaktalk.com"):
            dom = dom[: -len(".emaktalk.com")]
        self.dom = Gtk.Entry(text=dom, placeholder_text="votrecompagnie", hexpand=True)
        self.suffix = label(".emaktalk.com", "dim-label")
        self.suffix.set_no_show_all(True)
        self.dom.connect("changed", self._dom_changed)
        dom_box = Gtk.Box(spacing=6)
        dom_box.pack_start(self.dom, True, True, 0)
        dom_box.pack_start(self.suffix, False, False, 0)

        self.name = Gtk.Entry(text=settings.get("display_name", ""), placeholder_text="Facultatif")

        self.pack_start(self._field("Numéro de poste", self.ext), False, False, 0)
        self.pack_start(self._field("Mot de passe du poste", self.pw), False, False, 0)
        self.pack_start(self._field("Domaine de la compagnie", dom_box,
                                    "Le début de l'adresse fournie par EMAK, par exemple "
                                    "« acme » pour acme.emaktalk.com."), False, False, 0)

        help_lb = label("Ces trois informations se trouvent dans l'application Web EMAK, sous "
                        "<b>Comptes → Postes</b>. Sinon, le soutien EMAK (*611 ou 514 400-0226) "
                        "peut vous les donner.", "le-help dim-label", xalign=0, wrap=True)
        help_lb.set_use_markup(True)
        self.pack_start(help_lb, False, False, 0)

        exp = Gtk.Expander(label="Options avancées")
        adv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=10)
        adv.pack_start(self._field("Nom affiché aux autres postes", self.name), False, False, 0)
        self.udp = Gtk.RadioButton.new_with_label(None, "UDP (recommandé)")
        self.tcp = Gtk.RadioButton.new_with_label_from_widget(self.udp, "TCP")
        (self.tcp if settings.get("transport") == "tcp" else self.udp).set_active(True)
        tr = Gtk.Box(spacing=16)
        tr.pack_start(self.udp, False, False, 0)
        tr.pack_start(self.tcp, False, False, 0)
        adv.pack_start(self._field("Transport SIP", tr, "Essayez TCP si les appels entrants "
                                   "cessent de sonner après quelques minutes."), False, False, 0)
        exp.add(adv)
        exp.set_expanded(settings.get("transport") == "tcp" or bool(settings.get("display_name")))
        self.pack_start(exp, False, False, 0)

        self.autostart = None
        if show_autostart:
            self.autostart = Gtk.CheckButton(label="Démarrer avec l'ordinateur pour toujours recevoir les appels")
            self.autostart.set_active(autostart)
            self.pack_start(self.autostart, False, False, 0)

        self.error = label("", "le-form-error", xalign=0, wrap=True)
        self.error.set_no_show_all(True)
        self.pack_start(self.error, False, False, 0)

        if button_text:
            btn = add_class(Gtk.Button(label=button_text), "suggested-action", "le-wide-btn")
            btn.connect("clicked", lambda *_: self.emit("submit"))
            self.pack_start(btn, False, False, 4)
        for e in (self.ext, self.pw, self.dom):
            e.connect("activate", lambda *_: self.emit("submit"))
        self._dom_changed(self.dom)

    def _field(self, title, widget, hint=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.pack_start(label(title, "le-field-title", xalign=0), False, False, 0)
        box.pack_start(widget, False, False, 0)
        if hint:
            box.pack_start(label(hint, "le-hint dim-label", xalign=0, wrap=True), False, False, 0)
        return box

    def _toggle_pw(self, entry, pos, event):
        vis = not entry.get_visibility()
        entry.set_visibility(vis)
        entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY,
                                      "view-conceal-symbolic" if vis else "view-reveal-symbolic")

    def _dom_changed(self, entry):
        self.suffix.set_visible("." not in entry.get_text())

    def values(self):
        return {
            "extension": self.ext.get_text().strip(),
            "password": self.pw.get_text(),
            "domain": normalize_domain(self.dom.get_text()),
            "transport": "tcp" if self.tcp.get_active() else "udp",
            "display_name": self.name.get_text().strip(),
            "autostart": self.autostart.get_active() if self.autostart else None,
        }

    def show_error(self, text):
        self.error.set_text(text or "")
        self.error.set_visible(bool(text))

    def focus_first_empty(self):
        for e in (self.ext, self.pw, self.dom):
            if not e.get_text():
                e.grab_focus()
                return
        self.ext.grab_focus()


# ---------------------------------------------------------------- clavier

class DialPad(Gtk.Grid):
    __gsignals__ = {"digit": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, compact=False):
        super().__init__(row_spacing=10 if not compact else 6, column_spacing=22 if not compact else 14,
                         halign=Gtk.Align.CENTER)
        add_class(self, "le-dialpad")
        if compact:
            add_class(self, "compact")
        for i, (digit, letters) in enumerate(KEYS):
            b = Gtk.Button()
            add_class(b, "le-key")
            b.set_can_focus(False)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, valign=Gtk.Align.CENTER)
            box.pack_start(label(digit, "le-digit"), False, False, 0)
            if not compact:
                box.pack_start(label(letters or " ", "le-letters"), False, False, 0)
            b.add(box)
            b.connect("clicked", lambda _b, d=digit: self.emit("digit", d))
            if digit == "0":
                # appui long sur 0 = « + »
                b.connect("button-press-event", self._press)
                b.connect("button-release-event", self._release)
            self.attach(b, i % 3, i // 3, 1, 1)
        self._long_src = 0
        self._long_fired = False

    def _press(self, button, event):
        self._long_fired = False

        def fire():
            self._long_src = 0
            self._long_fired = True
            self.emit("digit", "+")
            return False

        self._long_src = GLib.timeout_add(600, fire)
        return False

    def _release(self, button, event):
        if self._long_src:
            GLib.source_remove(self._long_src)
            self._long_src = 0
        if self._long_fired:
            self._long_fired = False
            return True  # empêche le « 0 » après le « + »
        return False


def round_button(icon_name, css, tooltip, px=26, size=64):
    b = Gtk.Button()
    add_class(b, "le-round", css)
    b.set_size_request(size, size)
    b.set_halign(Gtk.Align.CENTER)
    b.set_valign(Gtk.Align.CENTER)
    b.add(icon(icon_name, px))
    b.set_tooltip_text(tooltip)
    b.set_can_focus(False)
    return b


class ActionToggle(Gtk.Box):
    """Bouton rond + libellé (Muet, Attente, Clavier, Transférer)."""

    def __init__(self, icon_name, text, toggle=True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.CENTER)
        self.button = Gtk.ToggleButton() if toggle else Gtk.Button()
        add_class(self.button, "le-action")
        self.button.set_size_request(58, 58)
        self.button.set_halign(Gtk.Align.CENTER)
        self.button.set_can_focus(False)
        self.image = icon(icon_name, 22)
        self.button.add(self.image)
        self.text = label(text, "le-action-label")
        self.pack_start(self.button, False, False, 0)
        self.pack_start(self.text, False, False, 0)

    def set_icon(self, icon_name):
        self.image.set_from_icon_name("le-%s-symbolic" % icon_name, Gtk.IconSize.BUTTON)
        self.image.set_pixel_size(22)


# ---------------------------------------------------------------- écran d'appel entrant

class Avatar(Gtk.Stack):
    """Cercle avec les initiales de l'appelant, ou une silhouette si le nom est inconnu."""

    def __init__(self, size=104):
        super().__init__(halign=Gtk.Align.CENTER)
        self.initials = add_class(Gtk.Label(), "le-avatar")
        self.initials.set_size_request(size, size)
        self.glyph = add_class(icon("person", int(size * 0.42)), "le-avatar")
        self.glyph.set_size_request(size, size)
        self.add_named(self.initials, "initials")
        self.add_named(self.glyph, "glyph")

    def update(self, name, number):
        ini = initials(name, number)
        self.initials.set_text(ini)
        self.set_visible_child_name("initials" if ini else "glyph")


class IncomingView(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        add_class(self, "le-incoming")
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, valign=Gtk.Align.CENTER, vexpand=True)
        self.state = label("Appel entrant", "le-call-state le-ringing")
        self.avatar = Avatar()
        self.name = label("", "le-peer-name", ellipsize=True, selectable=False)
        self.number = label("", "le-peer-number", selectable=True)
        self.note = label("", "le-hint dim-label", wrap=True)
        self.note.set_justify(Gtk.Justification.CENTER)
        self.note.set_no_show_all(True)
        for w in (self.state, self.avatar, self.name, self.number, self.note):
            top.pack_start(w, False, False, 0)
        top.set_margin_start(24)
        top.set_margin_end(24)
        self.pack_start(top, True, True, 0)

        row = Gtk.Box(spacing=0, homogeneous=True, margin_bottom=44, margin_start=28, margin_end=28)
        self.decline = round_button("hangup", "le-hangup", "Refuser l'appel", px=30, size=72)
        self.answer = round_button("call", "le-answer", "Répondre", px=30, size=72)
        for btn, txt in ((self.decline, "Refuser"), (self.answer, "Répondre")):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            col.pack_start(btn, False, False, 0)
            col.pack_start(label(txt, "le-action-label"), False, False, 0)
            row.pack_start(col, True, True, 0)
        self.pack_start(row, False, False, 0)

    def show_call(self, name, number, waiting):
        self.avatar.update(name, number)
        self.name.set_text(name or format_number(number) or "Numéro masqué")
        self.number.set_text(format_number(number) if name else "")
        self.number.set_visible(bool(name))
        self.note.set_text("Votre appel en cours sera mis en attente si vous répondez." if waiting else "")
        self.note.set_visible(waiting)


# ---------------------------------------------------------------- écran d'appel en cours

class CallView(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        add_class(self, "le-callview")

        self.held_bar = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        hb = add_class(Gtk.Box(spacing=8), "le-heldbar")
        hb.pack_start(icon("pause", 14), False, False, 0)
        self.held_label = label("", ellipsize=True, xalign=0)
        hb.pack_start(self.held_label, True, True, 0)
        self.held_bar.add(hb)
        self.pack_start(self.held_bar, False, False, 0)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=30)
        self.avatar = Avatar(92)
        self.name = label("", "le-peer-name", ellipsize=True)
        self.number = label("", "le-peer-number", selectable=True)
        self.state = label("", "le-call-state")
        for w in (self.avatar, self.name, self.number, self.state):
            info.pack_start(w, False, False, 0)
        info.set_margin_start(24)
        info.set_margin_end(24)
        self.pack_start(info, False, False, 0)

        self.middle = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True,
                                valign=Gtk.Align.CENTER, vhomogeneous=False, hhomogeneous=False,
                                interpolate_size=True)
        grid = Gtk.Grid(column_homogeneous=True, row_spacing=26, column_spacing=56,
                        halign=Gtk.Align.CENTER)
        self.mute = ActionToggle("mic-off", "Muet")
        self.hold = ActionToggle("pause", "Attente")
        self.keypad = ActionToggle("dialpad", "Clavier", toggle=False)
        self.transfer = ActionToggle("transfer", "Transférer", toggle=False)
        grid.attach(self.mute, 0, 0, 1, 1)
        grid.attach(self.keypad, 1, 0, 1, 1)
        grid.attach(self.hold, 0, 1, 1, 1)
        grid.attach(self.transfer, 1, 1, 1, 1)
        self.middle.add_named(grid, "actions")

        pad_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.dtmf_label = label("", "le-dtmf", ellipsize=True)
        self.dtmf_pad = DialPad(compact=True)
        hide = Gtk.Button(halign=Gtk.Align.CENTER, relief=Gtk.ReliefStyle.NONE)
        hide_box = Gtk.Box(spacing=6)
        hide_box.pack_start(icon("keyboard-hide", 14), False, False, 0)
        hide_box.pack_start(label("Masquer le clavier"), False, False, 0)
        hide.add(hide_box)
        hide.connect("clicked", lambda *_: self.show_actions())
        pad_box.pack_start(self.dtmf_label, False, False, 0)
        pad_box.pack_start(self.dtmf_pad, False, False, 0)
        pad_box.pack_start(hide, False, False, 0)
        self.middle.add_named(pad_box, "dtmf")

        tr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=32, margin_end=32)
        tr_box.pack_start(label("Transférer l'appel à…", "le-field-title", xalign=0), False, False, 0)
        self.transfer_entry = Gtk.Entry(placeholder_text="Poste ou numéro")
        self.transfer_entry.set_input_purpose(Gtk.InputPurpose.PHONE)
        tr_box.pack_start(self.transfer_entry, False, False, 0)
        tr_box.pack_start(label("Transfert immédiat (sans annonce) : votre appel se termine dès que "
                                "l'autre poste prend le relais.", "le-hint dim-label", xalign=0, wrap=True),
                          False, False, 0)
        tr_btns = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.transfer_cancel = Gtk.Button(label="Annuler")
        self.transfer_go = add_class(Gtk.Button(label="Transférer"), "suggested-action")
        tr_btns.pack_start(self.transfer_cancel, False, False, 0)
        tr_btns.pack_start(self.transfer_go, False, False, 0)
        tr_box.pack_start(tr_btns, False, False, 0)
        self.transfer_cancel.connect("clicked", lambda *_: self.show_actions())
        self.middle.add_named(tr_box, "transfer")
        self.pack_start(self.middle, True, True, 0)

        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_bottom=40)
        self.hangup = round_button("hangup", "le-hangup", "Raccrocher", px=30, size=72)
        bottom.pack_start(self.hangup, False, False, 0)
        self.pack_start(bottom, False, False, 0)

        self.keypad.button.connect("clicked", lambda *_: self.middle.set_visible_child_name("dtmf"))
        self.transfer.button.connect("clicked", self._show_transfer)

    def _show_transfer(self, *_):
        self.middle.set_visible_child_name("transfer")
        self.transfer_entry.set_text("")
        self.transfer_entry.grab_focus()

    def show_actions(self):
        self.middle.set_visible_child_name("actions")

    def reset(self):
        self.show_actions()
        self.dtmf_label.set_text("")


# ---------------------------------------------------------------- historique

class HistoryList(Gtk.Box):
    __gsignals__ = {"call-number": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, history):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.history = history
        self.stack = Gtk.Stack()
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, valign=Gtk.Align.CENTER)
        empty.pack_start(add_class(icon("history", 48), "dim-label"), False, False, 0)
        empty.pack_start(label("Aucun appel pour l'instant", "le-empty-title"), False, False, 0)
        empty.pack_start(label("Vos appels entrants, sortants et manqués\napparaîtront ici.",
                               "dim-label"), False, False, 0)
        self.stack.add_named(empty, "empty")

        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        add_class(self.listbox, "le-history")
        self.listbox.connect("row-activated", self._activated)
        sw.add(self.listbox)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.pack_start(sw, True, True, 0)
        clear = Gtk.Button(label="Effacer l'historique", relief=Gtk.ReliefStyle.NONE, halign=Gtk.Align.CENTER,
                           margin_top=6, margin_bottom=8)
        add_class(clear, "le-clear")
        clear.connect("clicked", self._clear)
        box.pack_start(clear, False, False, 0)
        self.stack.add_named(box, "list")
        self.pack_start(self.stack, True, True, 0)
        self.refresh()

    def _clear(self, *_):
        dlg = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                                message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE,
                                text="Effacer tout l'historique des appels ?")
        dlg.add_button("Annuler", Gtk.ResponseType.CANCEL)
        add_class(dlg.add_button("Effacer", Gtk.ResponseType.OK), "destructive-action")
        if dlg.run() == Gtk.ResponseType.OK:
            self.history.clear()
            self.refresh()
        dlg.destroy()

    def _activated(self, lb, row):
        if getattr(row, "number", None):
            self.emit("call-number", row.number)

    def refresh(self):
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        for item in self.history.items[:200]:
            self.listbox.add(self._row(item))
        self.listbox.show_all()
        self.stack.set_visible_child_name("list" if self.history.items else "empty")

    def _row(self, item):
        row = Gtk.ListBoxRow()
        row.number = item.get("number", "")
        add_class(row, "le-history-row")
        status = item.get("status")
        if status in ("missed", "declined"):
            add_class(row, "missed")
        box = Gtk.Box(spacing=12, margin_start=14, margin_end=8, margin_top=8, margin_bottom=8)
        ic = "missed" if status in ("missed", "declined") else ("incoming" if item["direction"] == "in"
                                                                  else "outgoing")
        box.pack_start(add_class(icon(ic, 16), "le-dir-icon"), False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name = item.get("name") or format_number(item.get("number")) or "Numéro masqué"
        text.pack_start(label(name, "le-history-name", xalign=0, ellipsize=True), False, False, 0)
        detail = when(item.get("time", 0))
        if status == "answered" and item.get("duration"):
            detail += " · " + human_duration(item["duration"])
        elif status == "missed":
            detail += " · Manqué"
        elif status == "declined":
            detail += " · Refusé"
        elif status == "failed":
            detail += " · Échec"
        elif status == "cancelled":
            detail += " · Annulé"
        if item.get("name"):
            detail = format_number(item.get("number")) + " · " + detail
        text.pack_start(label(detail, "le-history-detail dim-label", xalign=0, ellipsize=True), False, False, 0)
        box.pack_start(text, True, True, 0)
        if row.number:
            b = Gtk.Button(relief=Gtk.ReliefStyle.NONE, valign=Gtk.Align.CENTER)
            add_class(b, "le-history-call")
            b.add(icon("call", 16))
            b.set_tooltip_text("Rappeler")
            b.connect("clicked", lambda *_: self.emit("call-number", row.number))
            box.pack_start(b, False, False, 0)
        row.add(box)
        return row
