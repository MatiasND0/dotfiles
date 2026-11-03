#!/usr/bin/python3
"""Waybar popovers using the desktop's existing GTK, NetworkManager and PipeWire."""
import calendar
import ctypes
import ctypes.util
import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import unquote
import urllib.request
import uuid
from zoneinfo import ZoneInfo
from panel_network import WiredConnections

os.environ["GDK_BACKEND"] = "wayland"
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk
from gi.repository import GdkPixbuf
import dbus
from dbus.mainloop.glib import DBusGMainLoop

DBusGMainLoop(set_as_default=True)
HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("America/Argentina/Buenos_Aires")
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
          "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
NM = "org.freedesktop.NetworkManager"
NMP = "/org/freedesktop/NetworkManager"


def run(args, timeout=5):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "No se pudo completar la acción.")
    return result.stdout.strip()


def data(args):
    return json.loads(run(args))


def now():
    return dt.datetime.now(TZ)


def clock_status():
    t = now()
    text = f"  {t:%H:%M}  ·  {DAYS[t.weekday()][:3]} {t.day} {MONTHS[t.month-1][:3]}  ⌄"
    tooltip = f"{DAYS[t.weekday()].capitalize()} {t.day} de {MONTHS[t.month-1]} de {t.year}\nAbrir calendario · Hora de Argentina"
    print(json.dumps({"text": text, "tooltip": tooltip}, ensure_ascii=False))


def label(text, cls=None, xalign=0):
    w = Gtk.Label(label=text, xalign=xalign)
    w.set_line_wrap(True)
    if cls:
        w.get_style_context().add_class(cls)
    return w


def box(vertical=True, spacing=10, cls=None):
    w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,
                spacing=spacing)
    if cls:
        w.get_style_context().add_class(cls)
    return w


def button(text, callback, cls=None):
    w = Gtk.Button(label=text)
    w.set_relief(Gtk.ReliefStyle.NONE)
    if cls:
        w.get_style_context().add_class(cls)
    w.connect("clicked", lambda *_: callback())
    return w


def props(bus, name, path, interface):
    return dbus.Interface(bus.get_object(name, path),
                          "org.freedesktop.DBus.Properties").GetAll(interface)


def layer_function(name, argtypes):
    f = getattr(LAYER, "gtk_layer_" + name)
    f.argtypes, f.restype = argtypes, None
    return f


# The shared library ships with Waybar. Use its public C API without extra packages.
LAYER = ctypes.CDLL(ctypes.util.find_library("gtk-layer-shell"))
capsule_pointer = ctypes.pythonapi.PyCapsule_GetPointer
capsule_pointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
capsule_pointer.restype = ctypes.c_void_p


def ptr(widget):
    return capsule_pointer(widget.__gpointer__, None)


class Controls(Gtk.Application):
    def __init__(self):
        flags = Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        if "--smoke" in sys.argv:
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id="local.waybar.rose.controls", flags=flags)
        self.window = None
        self.kind = None
        self.timers = []
        self.bus = dbus.SystemBus()
        self.alive = False
        self.generation = 0
        self.audio_monitor = None
        self.audio_cancel = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_path(str(HERE / "panel.css"))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_command_line(self, cmdline):
        args = list(cmdline.get_arguments())[1:]
        kind = next((x for x in args if not x.startswith("--")), "calendar")
        if kind == "close":
            if self.window:
                self.window.destroy()
            return 0
        if self.window and kind == self.kind:
            self.window.destroy()
            return 0
        if self.window:
            self.window.destroy()
        self.kind = kind
        self.open_panel()
        if "--smoke" in args:
            self.later(1400, lambda: self.window.destroy() if self.window else None)
        return 0

    def later(self, ms, callback):
        def fire():
            if self.alive:
                callback()
            return False
        self.timers.append(GLib.timeout_add(ms, fire))

    def every(self, ms, callback):
        def fire():
            if not self.alive:
                return False
            callback()
            return True
        self.timers.append(GLib.timeout_add(ms, fire))

    def async_work(self, job, done=None):
        generation = self.generation
        def worker():
            try:
                result = job()
                def finish():
                    if self.alive and self.generation == generation and done:
                        done(result)
                    return False
                GLib.idle_add(finish)
            except Exception as exc:
                message = str(exc)
                GLib.idle_add(lambda: self.message(message) if self.alive and self.generation == generation else False)
        threading.Thread(target=worker, daemon=True).start()

    def message(self, text, error=True):
        if not self.alive:
            return False
        self.notice.set_text(str(text))
        self.notice.get_style_context().remove_class("error")
        if error:
            self.notice.get_style_context().add_class("error")
        self.notice.show()
        return False

    def launch(self, args):
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.window.destroy()

    def open_panel(self):
        self.alive = True
        self.generation += 1
        win = Gtk.ApplicationWindow(application=self)
        self.window = win
        win.set_name("rose-controls")
        win.set_title("Controles de Waybar")
        win.set_decorated(False)
        win.set_app_paintable(True)
        visual = win.get_screen().get_rgba_visual()
        if visual:
            win.set_visual(visual)
        layer_function("init_for_window", [ctypes.c_void_p])(ptr(win))
        layer_function("set_namespace", [ctypes.c_void_p, ctypes.c_char_p])(ptr(win), b"waybar-controls")
        layer_function("set_layer", [ctypes.c_void_p, ctypes.c_int])(ptr(win), 3)
        layer_function("set_keyboard_mode", [ctypes.c_void_p, ctypes.c_int])(ptr(win), 1)
        layer_function("set_exclusive_zone", [ctypes.c_void_p, ctypes.c_int])(ptr(win), -1)
        for edge in range(4):
            layer_function("set_anchor", [ctypes.c_void_p, ctypes.c_int, ctypes.c_int])(ptr(win), edge, 1)
        layer_function("set_margin", [ctypes.c_void_p, ctypes.c_int, ctypes.c_int])(ptr(win), 2, 58)
        width = 320 if self.kind == "network" else 380
        left = 12
        screen_height = 720
        try:
            monitors = data(["hyprctl", "-j", "monitors"])
            cursor = data(["hyprctl", "-j", "cursorpos"])
            monitor = next((m for m in monitors if m["x"] <= cursor["x"] < m["x"] + m["width"]/m["scale"]
                            and m["y"] <= cursor["y"] < m["y"] + m["height"]/m["scale"]),
                           next(m for m in monitors if m["focused"]))
            display = Gdk.Display.get_default()
            for i in range(display.get_n_monitors()):
                gm = display.get_monitor(i)
                geometry = gm.get_geometry()
                if gm.get_model() == monitor["name"] or (geometry.x, geometry.y) == (monitor["x"], monitor["y"]):
                    layer_function("set_monitor", [ctypes.c_void_p, ctypes.c_void_p])(ptr(win), ptr(gm))
                    break
            screen_width = int(monitor["width"]/monitor["scale"])
            screen_height = int(monitor["height"]/monitor["scale"])
            center = screen_width/2 if self.kind == "calendar" else cursor["x"]-monitor["x"]
            left = max(12, min(int(center-width/2), screen_width-width-12))
        except (RuntimeError, StopIteration, KeyError):
            pass
        overlay = Gtk.Overlay()
        background = Gtk.EventBox()
        background.connect("button-press-event", lambda *_: win.destroy())
        overlay.add(background)
        card = box(cls="card")
        card.set_size_request(width, -1)
        card.set_halign(Gtk.Align.START)
        card.set_valign(Gtk.Align.START)
        card.set_margin_start(left)
        card.set_margin_top(6)
        overlay.add_overlay(card)
        self.card = card
        titles = {"network": ("", "Conexiones"), "audio": ("", "Sonido"),
                  "calendar": ("", "Calendario"), "power": ("", "Pantalla y energía"),
                  "bluetooth": ("", "Bluetooth"), "system": ("", "Sesión"),
                  "stats": ("", "Tu equipo"), "media": ("", "Reproductor")}
        icon, title = titles.get(self.kind, titles["calendar"])
        header = box(False)
        header.pack_start(label(icon + "   " + title, "title"), True, True, 0)
        header.pack_end(button("×", win.destroy, "close"), False, False, 0)
        card.pack_start(header, False, False, 0)
        self.body = box()
        if self.kind == "network":
            body_scroll = Gtk.ScrolledWindow()
            body_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            body_scroll.set_overlay_scrolling(False)
            body_scroll.set_propagate_natural_height(True)
            body_scroll.set_max_content_height(max(160, screen_height - 240))
            body_scroll.add(self.body)
            card.pack_start(body_scroll, False, False, 0)
        else:
            card.pack_start(self.body, False, False, 0)
        self.notice = label("", "notice")
        self.notice.set_no_show_all(True)
        card.pack_start(self.notice, False, False, 0)
        try:
            getattr(self, "build_" + self.kind)()
        except Exception as exc:
            self.body.pack_start(label("No se pudo cargar este panel.", "muted"), False, False, 0)
            self.message(str(exc))
        win.add(overlay)
        win.connect("key-press-event", self.key_press)
        win.connect("destroy", self.closed)
        win.show_all()

    def key_press(self, _win, event):
        if event.keyval == Gdk.KEY_Escape:
            self.window.destroy()
            return True
        return False

    def closed(self, *_):
        self.alive = False
        if self.audio_cancel:
            self.audio_cancel.cancel()
            self.audio_cancel = None
        if self.audio_monitor:
            self.audio_monitor.force_exit()
            self.audio_monitor = None
        for timer in self.timers:
            if GLib.MainContext.default().find_source_by_id(timer):
                GLib.source_remove(timer)
        self.timers.clear()
        self.window = None

    def section(self, text):
        self.body.pack_start(label(text, "section"), False, False, 0)

    def build_calendar(self):
        t = now()
        self.year, self.month = t.year, t.month
        self.day_heading = label("", "date-heading")
        self.body.pack_start(self.day_heading, False, False, 0)
        self.calendar_box = box()
        self.body.pack_start(self.calendar_box, False, False, 0)
        self.calendar_grid()

    def calendar_grid(self):
        for child in self.calendar_box.get_children():
            child.destroy()
        title = f"{MONTHS[self.month-1].capitalize()} {self.year}"
        nav = box(False, 4)
        nav.pack_start(button("‹", lambda: self.move_month(-1), "nav"), False, False, 0)
        nav.pack_start(label(title, "month", .5), True, True, 0)
        nav.pack_end(button("›", lambda: self.move_month(1), "nav"), False, False, 0)
        self.calendar_box.pack_start(nav, False, False, 0)
        grid = Gtk.Grid(column_homogeneous=True, row_spacing=5, column_spacing=3)
        t = now()
        self.day_heading.set_text(f"{DAYS[t.weekday()].capitalize()}, {t.day} de {MONTHS[t.month-1]}")
        for i, day in enumerate(["L", "M", "X", "J", "V", "S", "D"]):
            grid.attach(label(day, "weekday", .5), i, 0, 1, 1)
        for row, week in enumerate(calendar.Calendar(0).monthdayscalendar(self.year, self.month), 1):
            for col, day in enumerate(week):
                if not day:
                    continue
                day_button = button(str(day), lambda d=day: self.select_day(d), "day")
                if (self.year, self.month, day) == (t.year, t.month, t.day):
                    day_button.get_style_context().add_class("today")
                if col >= 5:
                    day_button.get_style_context().add_class("weekend")
                grid.attach(day_button, col, row, 1, 1)
        self.calendar_box.pack_start(grid, False, False, 0)
        footer = box(False)
        footer.pack_start(label("Hora de Argentina", "muted"), True, True, 0)
        footer.pack_end(button("Hoy", self.go_today, "soft"), False, False, 0)
        self.calendar_box.pack_start(footer, False, False, 0)
        self.calendar_box.show_all()

    def move_month(self, delta):
        total = self.year*12+self.month-1+delta
        self.year, m = divmod(total, 12)
        self.month = m+1
        self.calendar_grid()

    def go_today(self):
        t = now()
        self.year, self.month = t.year, t.month
        self.calendar_grid()

    def select_day(self, day):
        t = dt.date(self.year, self.month, day)
        self.message(f"{DAYS[t.weekday()].capitalize()} {day} de {MONTHS[self.month-1]} de {self.year}", False)

    def audio_state(self):
        result = {}
        for kind, singular in [("sinks", "sink"), ("sources", "source")]:
            default = run(["pactl", "get-default-" + singular])
            devices = [d for d in data(["pactl", "--format=json", "list", kind])
                       if not d["name"].endswith(".monitor")]
            current = next((d for d in devices if d["name"] == default), None)
            result[singular] = (devices, current)
        return result

    def build_audio(self):
        self.audio_widgets = {}
        self.audio_updating = False
        self.audio_pending = {}
        self.audio_targets = {}
        self.audio_writing = set()
        self.audio_dragging = set()
        self.audio_busy = False
        self.audio_refresh_again = False
        for key, text, icon in [("sink", "Salida", ""), ("source", "Micrófono", "")]:
            self.section(text)
            combo = Gtk.ComboBoxText()
            combo.connect("changed", self.audio_device_changed, key)
            self.body.pack_start(combo, False, False, 0)
            row = box(False)
            mute = button(icon, lambda k=key: self.audio_mute(k), "icon-button")
            row.pack_start(mute, False, False, 0)
            slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
            slider.set_draw_value(False)
            slider.set_hexpand(True)
            slider.connect("value-changed", self.audio_volume_changed, key)
            slider.connect("button-press-event", self.audio_drag_started, key)
            slider.connect("button-release-event", self.audio_drag_ended, key)
            slider.connect("grab-broken-event", self.audio_drag_ended, key)
            row.pack_start(slider, True, True, 0)
            value = label("", "value")
            value.set_width_chars(5)
            row.pack_end(value, False, False, 0)
            self.body.pack_start(row, False, False, 0)
            self.audio_widgets[key] = (combo, slider, mute, value, ())
        self.body.pack_start(button("Abrir mezclador de aplicaciones  ↗",
                                   lambda: self.launch(["pavucontrol"]), "soft"), False, False, 0)
        self.watch_audio()

    def media_state(self):
        status = run(["playerctl", "status"])
        title = run(["playerctl", "metadata", "--format", "{{ artist }} · {{ title }}"])
        art = run(["playerctl", "metadata", "--format", "{{ mpris:artUrl }}"])
        return status, title, art

    def build_media(self):
        self.media_art_source = None
        self.media_art_file = Path(tempfile.gettempdir()) / "waybar-media-art"
        self.media_art = Gtk.Image.new_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
        self.media_art.set_pixel_size(180)
        self.body.pack_start(self.media_art, False, False, 0)
        self.media_status = label("", "date-heading", .5)
        self.media_title = label("", "value", .5)
        self.body.pack_start(self.media_status, False, False, 0)
        self.body.pack_start(self.media_title, False, False, 0)
        row = box(False, 4)
        for text, action in [("", "previous"), ("", "play-pause"),
                             ("", "next"), ("", "stop")]:
            row.pack_start(button(text, lambda a=action: self.media_action(a), "media-button"),
                           True, True, 0)
        self.body.pack_start(row, False, False, 0)
        self.refresh_media()
        self.every(2000, self.refresh_media)

    def refresh_media(self):
        try:
            status, title, art = self.media_state()
            self.media_status.set_text("Reproduciendo" if status == "Playing" else "En pausa")
            self.media_title.set_text(title or "Sin título")
            if art != self.media_art_source:
                self.media_art_source = art
                if art.startswith("file://"):
                    self.set_media_art(unquote(art[7:]))
                elif art.startswith(("http://", "https://")):
                    self.async_work(self.download_media_art,
                                    lambda path, source=art: self.set_media_art(path) if source == self.media_art_source else None)
                else:
                    self.set_media_art(None)
        except (RuntimeError, subprocess.TimeoutExpired):
            self.media_art_source = None
            self.set_media_art(None)
            self.media_status.set_text("No hay reproductor activo")
            self.media_title.set_text("Abrí una aplicación compatible con MPRIS")

    def download_media_art(self):
        url = self.media_art_source
        with urllib.request.urlopen(url, timeout=5) as response:
            self.media_art_file.write_bytes(response.read())
        return str(self.media_art_file)

    def set_media_art(self, path):
        if path:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 180, 180, True)
                self.media_art.set_from_pixbuf(pixbuf)
                return
            except (GLib.Error, OSError):
                pass
        self.media_art.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)

    def media_action(self, action):
        self.async_work(lambda: run(["playerctl", action]), lambda _: self.refresh_media())

    def watch_audio(self):
        generation = self.generation
        self.audio_cancel = Gio.Cancellable()
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE |
                                              Gio.SubprocessFlags.STDERR_SILENCE)
        launcher.setenv("LC_ALL", "C", True)
        try:
            monitor = launcher.spawnv(["pactl", "subscribe"])
        except GLib.Error:
            self.later(1000, self.watch_audio)
            self.refresh_audio()
            return
        self.audio_monitor = monitor
        stream = Gio.DataInputStream.new(monitor.get_stdout_pipe())
        cancel = self.audio_cancel

        def received(reader, result):
            try:
                line, _ = reader.read_line_finish_utf8(result)
            except GLib.Error:
                line = None
            if not self.alive or self.generation != generation:
                return
            if line is None:
                monitor.force_exit()
                self.audio_monitor = None
                self.later(1000, self.watch_audio)
                return
            if any(f" on {kind} " in line for kind in ("sink", "source", "server", "card")):
                self.refresh_audio()
            reader.read_line_async(GLib.PRIORITY_DEFAULT, cancel, received)

        stream.read_line_async(GLib.PRIORITY_DEFAULT, cancel, received)
        self.refresh_audio()

    def refresh_audio(self):
        if self.audio_busy:
            self.audio_refresh_again = True
            return
        self.audio_busy = True
        def read():
            try:
                return self.audio_state(), None
            except Exception as exc:
                return None, str(exc)
        def done(result):
            self.audio_busy = False
            # An event during the read may have made its snapshot obsolete.
            if self.audio_refresh_again:
                self.audio_refresh_again = False
                self.refresh_audio()
                return
            state, error = result
            if error:
                self.message(error)
            else:
                self.apply_audio(state)
        self.async_work(read, done)

    def apply_audio(self, state):
        self.audio_updating = True
        for key, (devices, current) in state.items():
            combo, slider, mute, value, previous = self.audio_widgets[key]
            names = tuple(d["name"] for d in devices)
            if names != previous:
                combo.remove_all()
                for d in devices:
                    combo.append(d["name"], d["description"])
                self.audio_widgets[key] = (combo, slider, mute, value, names)
            combo.set_sensitive(bool(current))
            slider.set_sensitive(bool(current))
            if current:
                combo.set_active_id(current["name"])
                channels = current.get("volume", {}).values()
                volume = round(sum(v["value"] for v in channels)/max(1, len(current.get("volume", {})))/65536*100)
                if (key not in self.audio_dragging and key not in self.audio_targets
                        and key not in self.audio_writing):
                    slider.set_value(min(volume, 100))
                    value.set_text(f"{volume}%")
                mute.set_label("" if key == "source" and current["mute"] else "" if current["mute"]
                               else "" if key == "source" else "")
                mute.set_tooltip_text("Activar" if current["mute"] else "Silenciar")
        self.audio_updating = False

    def audio_volume_changed(self, slider, key):
        if self.audio_updating:
            return
        self.audio_widgets[key][3].set_text(f"{round(slider.get_value())}%")
        self.audio_targets[key] = round(slider.get_value())
        self.queue_audio_volume(key)

    def queue_audio_volume(self, key):
        if key in self.audio_pending or key in self.audio_writing:
            return
        # Keep sending while dragging, with at most one write per device in flight.
        timer = GLib.timeout_add(40, self.flush_audio_volume, key)
        self.audio_pending[key] = timer
        self.timers.append(timer)

    def flush_audio_volume(self, key):
        timer = self.audio_pending.pop(key)
        self.timers.remove(timer)
        volume = self.audio_targets.pop(key)
        self.audio_writing.add(key)
        def write():
            try:
                run(["pactl", "set-" + key + "-volume", "@DEFAULT_" + key.upper() + "@", f"{volume}%"])
            except Exception as exc:
                return str(exc)
        def done(error):
            self.audio_writing.discard(key)
            if error:
                self.message(error)
            if key in self.audio_targets:
                self.queue_audio_volume(key)
            else:
                self.refresh_audio()
        self.async_work(write, done)
        return False

    def audio_drag_started(self, _slider, _event, key):
        self.audio_dragging.add(key)
        return False

    def audio_drag_ended(self, _slider, _event, key):
        self.audio_dragging.discard(key)
        self.refresh_audio()
        return False

    def audio_mute(self, key):
        self.async_work(lambda: run(["pactl", "set-" + key + "-mute", "@DEFAULT_" + key.upper() + "@", "toggle"]),
                        lambda _: self.refresh_audio())

    def audio_device_changed(self, combo, key):
        if self.audio_updating or not combo.get_active_id():
            return
        name = combo.get_active_id()
        def change():
            run(["pactl", "set-default-" + key, name])
            stream_kind = "sink-inputs" if key == "sink" else "source-outputs"
            for stream in data(["pactl", "--format=json", "list", stream_kind]):
                run(["pactl", "move-" + ("sink-input" if key == "sink" else "source-output"),
                     str(stream["index"]), name])
        self.async_work(change, lambda _: self.refresh_audio())

    def nm_interface(self, path=NMP, interface=NM):
        return dbus.Interface(self.bus.get_object(NM, path), interface)

    def build_network(self):
        self.net_manager = self.nm_interface()
        self.wired = WiredConnections(self.nm_interface,
                                      lambda path, interface: props(self.bus, NM, path, interface))
        self.eth_starting = set()
        self.eth_attempts = {}
        self.eth_snapshot = None
        self.section("Ethernet · Cable")
        self.eth_status = label("Buscando conexiones cableadas…", "muted")
        self.body.pack_start(self.eth_status, False, False, 0)
        self.eth_list = box(spacing=5)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_overlay_scrolling(False)
        scroll.set_max_content_height(160)
        scroll.set_propagate_natural_height(True)
        scroll.add(self.eth_list)
        self.body.pack_start(scroll, False, False, 0)
        self.build_wifi()
        footer = box(False)
        footer.pack_start(button("  Actualizar", self.refresh_network, "soft"), True, True, 0)
        footer.pack_end(button("Ajustes  ↗", lambda: self.launch(["nm-connection-editor"]), "soft"),
                        False, False, 0)
        self.body.pack_start(footer, False, False, 0)
        self.refresh_ethernet()
        self.every(2500, self.refresh_ethernet)

    def refresh_network(self):
        self.refresh_ethernet()
        if self.net_device:
            self.scan_wifi()

    def refresh_ethernet(self):
        if not self.alive:
            return
        try:
            for key, (path, name) in list(self.eth_attempts.items()):
                try:
                    state = int(props(self.bus, NM, path, NM + ".Connection.Active")["State"])
                except dbus.DBusException:
                    state = 4
                if state in (2, 4):
                    del self.eth_attempts[key]
                    self.message("Conectado por cable a " + name if state == 2 else
                                 "No se pudo conectar a " + name + ". Revisá el cable y los Ajustes.", state != 2)
            rows, status = self.wired.snapshot()
            pending = self.eth_starting | self.eth_attempts.keys()
            snapshot = (rows, status, frozenset(pending))
            if snapshot == self.eth_snapshot:
                return
            self.eth_snapshot = snapshot
            self.eth_status.set_text("Ethernet y Wi-Fi pueden estar conectados a la vez."
                                     if any(r["active"] for r in rows) else status)
            for child in self.eth_list.get_children():
                child.destroy()
            for item in rows:
                waiting = (item["path"], item["device"]) in pending
                group = box(spacing=4)
                row = Gtk.Button()
                row.get_style_context().add_class("network-row")
                if item["active"]:
                    row.get_style_context().add_class("connected")
                content = box(spacing=3)
                title = label(("✓  " if item["active"] else "  ") + item["name"])
                title.set_max_width_chars(28)
                title.set_ellipsize(3)
                content.pack_start(title, False, False, 0)
                content.pack_start(label("Conectando…" if waiting else item["status"], "muted"),
                                   False, False, 0)
                row.add(content)
                row.set_tooltip_text(item["name"] + (" · " + item["interface"] if item["interface"] else ""))
                row.set_sensitive(item["can_connect"] and not waiting)
                row.connect("clicked", lambda _b, a=item: self.ethernet_connect(a))
                group.pack_start(row, False, False, 0)
                if item["active"]:
                    group.pack_start(button("Desconectar cable", lambda a=item: self.ethernet_disconnect(a),
                                            "soft"), False, False, 0)
                self.eth_list.pack_start(group, False, False, 0)
            if not rows:
                self.eth_list.pack_start(label("No hay perfiles Ethernet guardados. Abrí Ajustes.", "muted"),
                                         False, False, 0)
            self.eth_list.show_all()
        except dbus.DBusException as exc:
            self.message("No se pudieron actualizar las conexiones cableadas: " + str(exc))

    def ethernet_connect(self, item):
        if not item["can_connect"]:
            return
        key = (item["path"], item["device"])
        if key in self.eth_starting or key in self.eth_attempts:
            return
        try:
            command = self.wired.connect_command(item)
        except (RuntimeError, dbus.DBusException) as exc:
            self.message(str(exc))
            self.refresh_ethernet()
            return
        generation = self.generation
        self.eth_starting.add(key)
        self.message("Conectando por cable a " + item["name"] + "…", False)
        self.refresh_ethernet()

        def failed(error):
            if self.alive and self.generation == generation:
                self.eth_starting.discard(key)
                self.message("No se pudo conectar a " + item["name"] + ": " + str(error))
                self.refresh_ethernet()

        def started(path):
            if self.alive and self.generation == generation:
                self.eth_starting.discard(key)
                self.eth_attempts[key] = (str(path), item["name"])
                self.refresh_ethernet()

        def activate(error=None):
            if error:
                failed(error)
                return
            self.net_manager.ActivateConnection(item["path"], item["device"], "/",
                                                reply_handler=started, error_handler=failed)

        if command:
            def configure():
                try:
                    run(command, timeout=20)
                except Exception as exc:
                    return str(exc)
                return None
            self.async_work(configure, activate)
        else:
            activate()

    def ethernet_disconnect(self, item):
        generation = self.generation
        def finished(error=None):
            if self.alive and self.generation == generation:
                if error:
                    self.message("No se pudo desconectar el cable: " + str(error))
                else:
                    self.later(300, self.refresh_ethernet)
        self.net_manager.DeactivateConnection(
            item["active_path"], reply_handler=finished, error_handler=finished)

    def build_wifi(self):
        self.net_device = None
        devices = []
        for path in self.net_manager.GetDevices():
            d = props(self.bus, NM, path, NM + ".Device")
            if d["DeviceType"] == 2:
                devices.append((path, d))
        if not devices:
            self.body.pack_start(label("No se encontró un adaptador Wi-Fi.", "muted"), False, False, 0)
            return
        self.net_device, device = devices[0]
        self.net_props = dbus.Interface(self.bus.get_object(NM, NMP), "org.freedesktop.DBus.Properties")
        radio = bool(self.net_props.Get(NM, "WirelessEnabled"))
        row = box(False)
        row.pack_start(label("Wi-Fi", "section"), True, True, 0)
        self.wifi_switch = Gtk.Switch(active=radio)
        self.wifi_switch.set_valign(Gtk.Align.CENTER)
        self.wifi_switch.connect("notify::active", self.wifi_toggle)
        row.pack_end(self.wifi_switch, False, False, 0)
        self.body.pack_start(row, False, False, 0)
        self.net_status = label("Buscando redes…", "muted")
        self.body.pack_start(self.net_status, False, False, 0)
        self.disconnect_button = button("Desconectar Wi-Fi", self.wifi_disconnect, "soft")
        self.disconnect_button.set_no_show_all(True)
        self.body.pack_start(self.disconnect_button, False, False, 0)
        self.net_list = box(spacing=5)
        self.net_list.set_margin_end(6)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_overlay_scrolling(False)
        scroll.set_min_content_height(140)
        scroll.set_max_content_height(250)
        scroll.set_propagate_natural_height(True)
        scroll.add(self.net_list)
        self.body.pack_start(scroll, False, False, 0)
        self.password_box = box(cls="password-box")
        self.password_box.set_no_show_all(True)
        self.password_title = label("", "section")
        self.password = Gtk.Entry()
        self.password.set_visibility(False)
        self.password.set_placeholder_text("Contraseña de la red")
        self.password.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.password.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, "view-reveal-symbolic")
        self.password.connect("icon-press", lambda entry, *_: entry.set_visibility(not entry.get_visibility()))
        self.password.connect("activate", lambda *_: self.wifi_connect_password())
        self.password_box.pack_start(self.password_title, False, False, 0)
        self.password_box.pack_start(self.password, False, False, 0)
        buttons = box(False)
        buttons.pack_start(button("Cancelar", self.cancel_password, "soft"), True, True, 0)
        buttons.pack_end(button("Conectar", self.wifi_connect_password, "primary"), True, True, 0)
        self.password_box.pack_start(buttons, False, False, 0)
        self.body.pack_start(self.password_box, False, False, 0)
        self.net_selected = None
        self.net_attempt = False
        self.refresh_wifi()
        self.scan_wifi()
        self.every(5000, self.refresh_wifi)

    def wifi_toggle(self, switch, _param):
        try:
            self.net_props.Set(NM, "WirelessEnabled", dbus.Boolean(switch.get_active()))
            self.later(500, self.refresh_wifi)
        except dbus.DBusException as exc:
            self.message(str(exc))

    def scan_wifi(self):
        wireless = props(self.bus, NM, self.net_device, NM + ".Device.Wireless")
        last_scan = int(wireless.get("LastScan", -1))
        if last_scan >= 0 and time.clock_gettime(time.CLOCK_BOOTTIME) - last_scan / 1000 < 12:
            self.refresh_wifi()
            return
        self.nm_interface(self.net_device, NM + ".Device.Wireless").RequestScan(
            dbus.Dictionary({}, signature="sv"),
            reply_handler=lambda: self.later(1600, self.refresh_wifi),
            error_handler=lambda e: self.message("No se pudo actualizar la lista. Probá de nuevo en unos segundos."))

    def saved_networks(self):
        saved = {}
        settings = self.nm_interface(NMP + "/Settings", NM + ".Settings")
        for path in settings.ListConnections():
            try:
                s = self.nm_interface(path, NM + ".Settings.Connection").GetSettings()
                wifi = s.get("802-11-wireless", {})
                ssid = bytes(wifi.get("ssid", [])).decode("utf-8", "replace")
                if ssid:
                    saved[ssid] = path
            except dbus.DBusException:
                pass
        return saved

    def refresh_wifi(self):
        if not self.alive:
            return
        try:
            radio = bool(self.net_props.Get(NM, "WirelessEnabled"))
            wireless = props(self.bus, NM, self.net_device, NM + ".Device.Wireless")
            active = str(wireless["ActiveAccessPoint"])
            self.net_saved = self.saved_networks()
            networks = {}
            for path in wireless["AccessPoints"]:
                a = props(self.bus, NM, path, NM + ".AccessPoint")
                ssid = bytes(a["Ssid"]).decode("utf-8", "replace")
                if not ssid:
                    continue
                item = {"ssid": ssid, "path": str(path), "strength": int(a["Strength"]),
                        "secure": bool(a["Flags"] & 1), "wpa": int(a["WpaFlags"]),
                        "rsn": int(a["RsnFlags"]), "active": str(path) == active}
                previous = networks.get(ssid)
                if previous is None or item["active"] or (not previous["active"] and
                                                         item["strength"] > previous["strength"]):
                    networks[ssid] = item
            for child in self.net_list.get_children():
                child.destroy()
            connected = next((a["ssid"] for a in networks.values() if a["active"]), None)
            if self.net_attempt:
                device_state = int(props(self.bus, NM, self.net_device, NM + ".Device")["State"])
                if connected == self.net_selected["ssid"] and device_state == 100:
                    self.net_attempt = False
                    self.message("Conectado a " + connected, False)
                elif device_state in (60, 120):
                    self.net_attempt = False
                    self.message("No se pudo conectar. Revisá la contraseña y la configuración en Ajustes.")
            self.net_status.set_text("Wi-Fi desactivado" if not radio else
                                     "Conectado · " + connected if connected else "Elegí una red para conectarte")
            self.disconnect_button.set_visible(bool(connected and radio))
            for item in sorted(networks.values(), key=lambda a: (not a["active"], -a["strength"], a["ssid"])) if radio else []:
                row = Gtk.Button()
                row.get_style_context().add_class("network-row")
                if item["active"]:
                    row.get_style_context().add_class("connected")
                content = box(False, 8)
                name = label(("  " if item["secure"] else "  ") + item["ssid"])
                name.set_max_width_chars(20)
                name.set_ellipsize(3)
                content.pack_start(name, True, True, 0)
                content.pack_end(label("✓" if item["active"] else str(item["strength"]) + "%", "muted"),
                                 False, False, 0)
                row.add(content)
                row.connect("clicked", lambda _b, a=item: self.wifi_choose(a))
                self.net_list.pack_start(row, False, False, 0)
            if radio and not networks:
                self.net_list.pack_start(label("Todavía no hay redes visibles. Tocá Actualizar.", "muted"),
                                         False, False, 0)
            self.net_list.show_all()
        except dbus.DBusException as exc:
            self.message(str(exc))

    def wifi_choose(self, item):
        self.cancel_password()
        if item["active"]:
            self.message("Esta es tu conexión actual.", False)
            return
        self.net_selected = item
        self.net_attempt = False
        if item["ssid"] in self.net_saved:
            self.net_attempt = True
            self.message("Conectando…", False)
            self.net_manager.ActivateConnection(self.net_saved[item["ssid"]], self.net_device, item["path"],
                                                reply_handler=lambda *_: self.wifi_started(),
                                                error_handler=lambda e: self.message("No se pudo conectar. Revisá la red en Ajustes."))
        elif item["rsn"] & 2048:
            self.wifi_add(item, None)
        elif item["secure"]:
            if (item["wpa"] | item["rsn"]) & (512 | 8192):
                self.message("Esta red necesita configuración de empresa. Abrí Ajustes para conectarte.")
                return
            if not item["wpa"] and not item["rsn"]:
                self.message("Esta red usa una seguridad antigua. Abrí Ajustes para configurarla.")
                return
            self.password_title.set_text("Conectar a " + item["ssid"])
            self.password_box.set_no_show_all(False)
            self.password_box.show_all()
            self.password.grab_focus()
        else:
            self.wifi_add(item, None)

    def cancel_password(self):
        self.password.set_text("")
        self.password_box.hide()
        self.password_box.set_no_show_all(True)

    def wifi_connect_password(self):
        secret = self.password.get_text()
        if not self.net_selected or not secret:
            return
        self.wifi_add(self.net_selected, secret)
        self.password.set_text("")

    def wifi_add(self, item, secret):
        config = {
            "connection": {"id": item["ssid"], "uuid": str(uuid.uuid4()), "type": "802-11-wireless"},
            "802-11-wireless": {"ssid": dbus.ByteArray(item["ssid"].encode()), "mode": "infrastructure"},
            "ipv4": {"method": "auto"}, "ipv6": {"method": "auto"},
        }
        if item["rsn"] & 2048:
            config["802-11-wireless-security"] = {"key-mgmt": "owe"}
        elif secret is not None:
            # WPA3-only networks need SAE; WPA2/WPA3 transition networks accept PSK.
            keymgmt = "sae" if item["rsn"] & 1024 and not item["rsn"] & 256 else "wpa-psk"
            config["802-11-wireless-security"] = {"key-mgmt": keymgmt, "psk": secret}
        config = dbus.Dictionary({k: dbus.Dictionary(v, signature="sv") for k, v in config.items()},
                                 signature="sa{sv}")
        self.net_attempt = True
        self.message("Conectando…", False)
        self.net_manager.AddAndActivateConnection(
            config, self.net_device, item["path"], reply_handler=lambda *_: self.wifi_started(),
            error_handler=lambda e: self.message("No se pudo conectar. Comprobá la contraseña o abrí Ajustes."))

    def wifi_started(self):
        self.cancel_password()
        self.message("Conexión solicitada. Esperando a la red…", False)
        self.later(2500, self.refresh_wifi)

    def wifi_disconnect(self):
        self.nm_interface(self.net_device, NM + ".Device").Disconnect(
            reply_handler=lambda: self.later(500, self.refresh_wifi),
            error_handler=lambda e: self.message("No se pudo desconectar la red."))

    def build_power(self):
        batteries = list(Path("/sys/class/power_supply").glob("BAT*"))
        if batteries:
            battery = batteries[0]
            capacity = (battery / "capacity").read_text().strip()
            status = (battery / "status").read_text().strip()
            statuses = {"Charging": "Cargando", "Discharging": "En uso", "Full": "Carga completa",
                        "Not charging": "Conectada"}
            self.body.pack_start(label(capacity + "%", "big-number"), False, False, 0)
            self.body.pack_start(label(statuses.get(status, status), "muted"), False, False, 0)
        self.section("Brillo de la pantalla")
        brightness = run(["brightnessctl", "-m"]).split(",")
        slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 5, 100, 1)
        slider.set_value(float(brightness[3].rstrip("%")))
        slider.set_digits(0)
        slider.set_value_pos(Gtk.PositionType.RIGHT)
        pending = [None]
        def changed(scale):
            if pending[0] and GLib.MainContext.default().find_source_by_id(pending[0]):
                GLib.source_remove(pending[0])
            value = round(scale.get_value())
            def apply():
                self.async_work(lambda: run(["brightnessctl", "set", f"{value}%"]))
                return False
            pending[0] = GLib.timeout_add(120, apply)
            self.timers.append(pending[0])
        slider.connect("value-changed", changed)
        self.body.pack_start(slider, False, False, 0)
        self.section("Modo de energía")
        current = run(["powerprofilesctl", "get"])
        combo = Gtk.ComboBoxText()
        available = run(["powerprofilesctl", "list"])
        for key, text in [("power-saver", "Ahorro de energía"), ("balanced", "Equilibrado"),
                          ("performance", "Rendimiento")]:
            if key + ":" in available:
                combo.append(key, text)
        combo.set_active_id(current)
        combo.connect("changed", lambda c: self.async_work(
            lambda: run(["powerprofilesctl", "set", c.get_active_id()])))
        self.body.pack_start(combo, False, False, 0)

    def build_bluetooth(self):
        self.bluez = dbus.Interface(self.bus.get_object("org.bluez", "/"),
                                    "org.freedesktop.DBus.ObjectManager")
        objects = self.bluez.GetManagedObjects()
        adapters = [(p, x["org.bluez.Adapter1"]) for p, x in objects.items() if "org.bluez.Adapter1" in x]
        if not adapters:
            self.body.pack_start(label("No se encontró un adaptador Bluetooth.", "muted"), False, False, 0)
            return
        path, adapter = adapters[0]
        self.bt_adapter = path
        row = box(False)
        row.pack_start(label("Bluetooth", "section"), True, True, 0)
        switch = Gtk.Switch(active=bool(adapter["Powered"]))
        switch.set_valign(Gtk.Align.CENTER)
        def toggle(s, *_):
            try:
                dbus.Interface(self.bus.get_object("org.bluez", path),
                               "org.freedesktop.DBus.Properties").Set("org.bluez.Adapter1", "Powered",
                                                                      dbus.Boolean(s.get_active()))
            except dbus.DBusException as exc:
                self.message(str(exc))
        switch.connect("notify::active", toggle)
        row.pack_end(switch, False, False, 0)
        self.body.pack_start(row, False, False, 0)
        self.section("Dispositivos guardados")
        self.bt_list = box(spacing=5)
        self.body.pack_start(self.bt_list, False, False, 0)
        self.body.pack_start(button("Añadir un dispositivo  ↗",
                                   lambda: self.launch(["bluedevil-wizard"]), "soft"), False, False, 0)
        self.refresh_bluetooth()
        self.every(3000, self.refresh_bluetooth)

    def refresh_bluetooth(self):
        for child in self.bt_list.get_children():
            child.destroy()
        objects = self.bluez.GetManagedObjects()
        devices = [(p, x["org.bluez.Device1"]) for p, x in objects.items()
                   if "org.bluez.Device1" in x and x["org.bluez.Device1"].get("Paired")]
        for path, d in sorted(devices, key=lambda a: (not a[1].get("Connected"), a[1].get("Alias", ""))):
            connected = bool(d.get("Connected"))
            text = d.get("Alias", "Dispositivo") + ("  ·  Conectado" if connected else "  ·  Conectar")
            def connect(p=path, active=connected):
                device = dbus.Interface(self.bus.get_object("org.bluez", p), "org.bluez.Device1")
                method = device.Disconnect if active else device.Connect
                method(reply_handler=lambda: self.refresh_bluetooth(),
                       error_handler=lambda e: self.message("No se pudo cambiar la conexión Bluetooth."))
            self.bt_list.pack_start(button(text, connect, "network-row"), False, False, 0)
        if not devices:
            self.bt_list.pack_start(label("No hay dispositivos emparejados.", "muted"), False, False, 0)
        self.bt_list.show_all()

    def build_stats(self):
        memory = {line.split(":")[0]: int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()}
        total = memory["MemTotal"]/1048576
        used = (memory["MemTotal"]-memory["MemAvailable"])/1048576
        uptime = int(float(Path("/proc/uptime").read_text().split()[0]))
        for title, value in [("Memoria", f"{used:.1f} / {total:.1f} GB"),
                             ("Procesador", f"{os.cpu_count()} hilos · carga {os.getloadavg()[0]:.2f}"),
                             ("Tiempo encendido", f"{uptime//3600} h {(uptime%3600)//60} min")]:
            row = box(False)
            row.pack_start(label(title, "muted"), True, True, 0)
            row.pack_end(label(value, "value"), False, False, 0)
            self.body.pack_start(row, False, False, 0)
        self.body.pack_start(button("Abrir monitor del sistema  ↗",
                                   lambda: self.launch(["plasma-systemmonitor"]), "soft"), False, False, 0)

    def build_system(self):
        for icon, text, command, confirm in [
            ("", "Bloquear pantalla", ["hyprlock"], False),
            ("", "Suspender", ["systemctl", "suspend"], True),
            ("", "Cerrar sesión", ["hyprctl", "dispatch", "exit"], True),
            ("", "Reiniciar", ["systemctl", "reboot"], True),
            ("", "Apagar", ["systemctl", "poweroff"], True),
        ]:
            self.body.pack_start(button(icon + "   " + text,
                lambda c=command, t=text, ask=confirm: self.confirm_action(t, c) if ask else self.launch(c),
                "action-row"), False, False, 0)

    def confirm_action(self, title, command):
        for child in self.body.get_children():
            child.destroy()
        self.body.pack_start(label("¿" + title + "?", "date-heading"), False, False, 0)
        self.body.pack_start(label("Confirmá para continuar.", "muted"), False, False, 0)
        row = box(False)
        row.pack_start(button("Cancelar", self.window.destroy, "soft"), True, True, 0)
        row.pack_end(button(title, lambda: self.launch(command), "primary"), True, True, 0)
        self.body.pack_start(row, False, False, 0)
        self.body.show_all()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "clock-status":
        clock_status()
    elif len(sys.argv) > 1 and sys.argv[1] == "brightness-down":
        current = int(run(["brightnessctl", "get"]))
        maximum = int(run(["brightnessctl", "max"]))
        run(["brightnessctl", "set", str(max(round(maximum * .05), current - round(maximum * .05)))])
    else:
        app = Controls()
        signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(app.quit))
        raise SystemExit(app.run(sys.argv))
