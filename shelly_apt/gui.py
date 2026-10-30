"""Shelly-APT GTK4 / libadwaita front end.

Reads (package lists, searches, update checks) run in-process on worker
threads. Changes run the `shelly` CLI in --ui-mode, through pkexec for APT
operations, and stream its framed progress into a transaction dialog.
"""

import os
import shutil
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import APP_ID, VERSION, appimage_backend as appimg, flatpak_backend as fp, sources, system  # noqa: E402
from .config import CHOICES, Config  # noqa: E402
from .term import UI_PREFIX, Output, ShellyError  # noqa: E402

CSS = b"""
.pkg-name { font-weight: 600; }
.dim-label { opacity: 0.6; }
.installed-badge { color: @success_color; font-weight: 600; }
.update-count { font-size: 2.2em; font-weight: 800; }
.log-view { font-family: monospace; font-size: 0.9em; }
.nav-sidebar row { padding: 6px 8px; }
"""


# ---- helpers -------------------------------------------------------------------

def run_async(work, done=None, error=None):
    """Run work() on a thread and deliver its result on the GTK main loop."""
    def target():
        try:
            result = work()
        except Exception as exc:  # surfaced to the UI
            if error:
                GLib.idle_add(error, exc)
            else:
                GLib.idle_add(print, f"shelly-ui: {exc}", file=sys.stderr)
            return
        if done:
            GLib.idle_add(done, result)
    threading.Thread(target=target, daemon=True).start()


def shelly_command():
    """Path of the shelly CLI: installed copy first, then this source tree."""
    installed = shutil.which("shelly")
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "bin", "shelly")
    if os.path.exists(here) and not (installed and os.path.realpath(installed).startswith("/usr/")):
        return here
    return installed or here


def fmt_size(config, n):
    return Output(config).size(n) if n else ""


class Item(GObject.Object):
    """One row in a package table. `data` holds the backend dict."""

    def __init__(self, data, key):
        super().__init__()
        self.data = data
        self.key = key
        self.selected = False


# ---- generic table ----------------------------------------------------------------

class PackageTable(Gtk.Box):
    """A sortable, filterable ColumnView with a selection checkbox per row."""

    __gsignals__ = {
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, columns, key="name", search_keys=("name",), checkable=True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.key = key
        self.search_keys = search_keys
        self.query = ""
        self.extra_filter = None
        self.store = Gio.ListStore(item_type=Item)
        self.filter = Gtk.CustomFilter.new(self._match)
        filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.view = Gtk.ColumnView(show_row_separators=True, reorderable=False)
        self.view.add_css_class("data-table")
        self.sorted = Gtk.SortListModel(model=filtered, sorter=self.view.get_sorter())
        self.selection = Gtk.SingleSelection(model=self.sorted, autoselect=False, can_unselect=True)
        self.view.set_model(self.selection)
        self.view.connect("activate", self._on_activate)

        if checkable:
            self.view.append_column(self._check_column())
        first = None
        for title, field, expand, formatter, width in columns:
            col = self._text_column(title, field, formatter)
            col.set_expand(expand)
            col.set_resizable(True)
            if width:
                col.set_fixed_width(width)
            self.view.append_column(col)
            first = first or col
        if first:
            self.view.sort_by_column(first, Gtk.SortType.ASCENDING)

        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True, child=self.view)
        self.stack = Gtk.Stack()
        self.stack.add_named(scroller, "list")
        self.status = Adw.StatusPage(icon_name="system-search-symbolic")
        self.stack.add_named(self.status, "status")
        spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=12)
        spinner = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
        spinner.set_size_request(32, 32)
        spinner_box.append(spinner)
        self.loading_label = Gtk.Label(label="Loading…", css_classes=["dim-label"])
        spinner_box.append(self.loading_label)
        self.stack.add_named(spinner_box, "loading")
        self.append(self.stack)

    # columns
    def _check_column(self):
        factory = Gtk.SignalListItemFactory()

        def setup(_f, li):
            li.set_child(Gtk.CheckButton(halign=Gtk.Align.CENTER))

        def bind(_f, li):
            check, item = li.get_child(), li.get_item()
            check.set_active(item.selected)
            check._item = item
            check._hid = check.connect("toggled", self._on_toggle)

        def unbind(_f, li):
            check = li.get_child()
            if getattr(check, "_hid", None):
                check.disconnect(check._hid)
                check._hid = None

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        factory.connect("unbind", unbind)
        col = Gtk.ColumnViewColumn(title="", factory=factory)
        col.set_fixed_width(40)
        return col

    def _on_toggle(self, check):
        check._item.selected = check.get_active()
        self.emit("selection-changed")

    def _text_column(self, title, field, formatter):
        factory = Gtk.SignalListItemFactory()

        def setup(_f, li):
            li.set_child(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END))

        def bind(_f, li):
            item = li.get_item()
            label = li.get_child()
            value = formatter(item.data) if formatter else item.data.get(field, "")
            if isinstance(value, tuple):  # (text, css class)
                value, css = value
                label.set_css_classes([css] if css else [])
            label.set_text("" if value is None else str(value))
            label.set_tooltip_text(label.get_text() if len(label.get_text()) > 30 else None)

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        col = Gtk.ColumnViewColumn(title=title, factory=factory)

        def compare(a, b, _data):
            va, vb = a.data.get(field), b.data.get(field)
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                return (va > vb) - (va < vb)
            va, vb = str(va or "").lower(), str(vb or "").lower()
            return (va > vb) - (va < vb)

        col.set_sorter(Gtk.CustomSorter.new(compare, None))
        return col

    # data
    def _match(self, item):
        if self.extra_filter and not self.extra_filter(item.data):
            return False
        if not self.query:
            return True
        hay = " ".join(str(item.data.get(k) or "") for k in self.search_keys).lower()
        return all(t in hay for t in self.query.split())

    def set_items(self, rows):
        items = [Item(r, r.get(self.key)) for r in rows]
        self.store.splice(0, self.store.get_n_items(), items)
        if rows:
            self.stack.set_visible_child_name("list")
        else:
            self.show_status("Nothing here", "")
        self.emit("selection-changed")

    def set_query(self, text):
        self.query = text.lower().strip()
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def show_loading(self, text="Loading…"):
        self.loading_label.set_text(text)
        self.stack.set_visible_child_name("loading")

    def show_status(self, title, description, icon="system-search-symbolic"):
        self.status.set_title(title)
        self.status.set_description(description)
        self.status.set_icon_name(icon)
        self.stack.set_visible_child_name("status")

    def selected(self):
        return [self.store.get_item(i).data for i in range(self.store.get_n_items()) if self.store.get_item(i).selected]

    def clear_selection(self):
        for i in range(self.store.get_n_items()):
            self.store.get_item(i).selected = False
        self.sorted.items_changed(0, self.sorted.get_n_items(), self.sorted.get_n_items())
        self.emit("selection-changed")

    def _on_activate(self, _view, position):
        item = self.sorted.get_item(position)
        if item:
            self.emit("activated", item.data)


# ---- transaction dialog ------------------------------------------------------------

class TransactionDialog(Adw.Dialog):
    """Runs a shelly CLI command and shows its progress and log."""

    def __init__(self, title, argv, on_done=None):
        super().__init__(title=title, content_width=640, content_height=460, can_close=False)
        self.on_done = on_done
        self.success = False
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False)
        toolbar.add_top_bar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12,
                      margin_start=18, margin_end=18)
        self.status = Gtk.Label(label="Starting…", xalign=0, wrap=True, css_classes=["heading"])
        box.append(self.status)
        self.progress = Gtk.ProgressBar(show_text=True)
        box.append(self.progress)
        self.buffer = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.buffer, editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            css_classes=["log-view"], top_margin=6, bottom_margin=6, left_margin=6, right_margin=6)
        self.scroller = Gtk.ScrolledWindow(vexpand=True, child=view, css_classes=["card"])
        box.append(self.scroller)
        self.close_btn = Gtk.Button(label="Close", sensitive=False, halign=Gtk.Align.END, css_classes=["pill", "suggested-action"])
        self.close_btn.connect("clicked", lambda *_: self.force_close())
        box.append(self.close_btn)
        toolbar.set_content(box)
        self.set_child(toolbar)
        self.pulse_id = GLib.timeout_add(120, self._pulse)
        self._start(argv)

    def _pulse(self):
        if self.progress.get_fraction() == 0:
            self.progress.pulse()
        return True

    def _append(self, text):
        self.buffer.insert(self.buffer.get_end_iter(), text)
        adj = self.scroller.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper()) or False)

    def _start(self, argv):
        self._append("$ " + " ".join(argv) + "\n")
        try:
            self.proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as exc:
            self._finish(False, str(exc))
            return
        self.stream = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        self.stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)
        self.proc.wait_async(None, self._on_exit)

    def _on_line(self, stream, result):
        try:
            line, _len = stream.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
        if line is None:
            return
        if line.startswith(UI_PREFIX):
            self._event(line[len(UI_PREFIX):])
        else:
            self._append(line + "\n")
            if line.strip() and not line.startswith((" ", "─")):
                self.status.set_text(line.strip()[:120])
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)

    def _event(self, payload):
        import json
        try:
            ev = json.loads(payload)
        except ValueError:
            return
        if ev.get("event") == "progress":
            pct = ev.get("percent")
            if pct is not None:
                self.progress.set_fraction(max(0.001, min(1.0, pct / 100.0)))
                self.progress.set_text(f"{ev.get('phase', '').capitalize()} {pct:.0f}%")
            if ev.get("message"):
                self.status.set_text(ev["message"])
        elif ev.get("event") == "reboot-required":
            self._append("\n⚠ A reboot is required to finish this update.\n")

    def _on_exit(self, proc, result):
        try:
            proc.wait_finish(result)
            ok = proc.get_successful()
        except GLib.Error:
            ok = False
        code = proc.get_exit_status() if proc.get_if_exited() else -1
        if code in (126, 127) and not ok:
            msg = "Authorization was cancelled or denied"
        else:
            msg = "Finished successfully" if ok else f"Failed (exit status {code})"
        self._finish(ok, msg)

    def _finish(self, ok, message):
        self.success = ok
        GLib.source_remove(self.pulse_id)
        self.progress.set_fraction(1.0 if ok else self.progress.get_fraction())
        self.progress.set_text("Done" if ok else "Failed")
        self.status.set_text(message)
        self._append(f"\n{message}\n")
        self.set_can_close(True)
        self.close_btn.set_sensitive(True)
        if self.on_done:
            self.on_done(ok)
        if ok:
            GLib.timeout_add(900, lambda: self.force_close() or False)


# ---- pages ------------------------------------------------------------------------

class Page(Gtk.Box):
    title = ""
    icon = ""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.loaded = False

    @property
    def config(self):
        return self.win.config

    def on_shown(self):
        if not self.loaded:
            self.loaded = True
            self.refresh()

    def refresh(self):
        pass

    def search_bar(self, placeholder, on_change, extra=None):
        bar = Gtk.Box(spacing=8, margin_top=10, margin_bottom=6, margin_start=12, margin_end=12)
        entry = Gtk.SearchEntry(placeholder_text=placeholder, hexpand=True)
        entry.connect("search-changed", lambda e: on_change(e.get_text()))
        bar.append(entry)
        for widget in extra or []:
            bar.append(widget)
        self.append(bar)
        return entry

    def action_bar(self, buttons):
        bar = Gtk.ActionBar()
        self.sel_label = Gtk.Label(label="", css_classes=["dim-label"])
        bar.pack_start(self.sel_label)
        for btn in reversed(buttons):
            bar.pack_end(btn)
        self.append(bar)
        return bar


def segmented(labels, on_change):
    box = Gtk.Box(css_classes=["linked"])
    buttons = []
    for i, label in enumerate(labels):
        btn = Gtk.ToggleButton(label=label, active=(i == 0))
        if buttons:
            btn.set_group(buttons[0])
        buttons.append(btn)
        box.append(btn)
    # Connect after the initial state is set so building the page doesn't fire it.
    for i, btn in enumerate(buttons):
        btn.connect("toggled", lambda b, i=i: b.get_active() and on_change(i))
    return box, buttons


class PackagesPage(Page):
    title = "Packages"
    icon = "package-x-generic-symbolic"
    FILTERS = [("All", None), ("Manually installed", lambda d: not d["auto"]),
               ("Automatically installed", lambda d: d["auto"]), ("Held", lambda d: d["held"]),
               ("Not from a repository", lambda d: d["foreign"])]

    def __init__(self, win):
        super().__init__(win)
        self.mode = 0  # 0 installed, 1 available
        self.installed_rows = []
        self.search_serial = 0
        seg, self.mode_buttons = segmented(["Installed", "Available"], self.set_mode)
        self.filter_drop = Gtk.DropDown.new_from_strings([f for f, _ in self.FILTERS])
        self.filter_drop.connect("notify::selected", self._on_filter)
        self.entry = self.search_bar("Search packages…", self._on_search, [seg, self.filter_drop])
        cfg = win.config
        self.table = PackageTable([
            ("Name", "name", False, lambda d: (d["name"], "pkg-name"), 240),
            ("Version", "installed_version", False,
             lambda d: (d["installed_version"], "installed-badge") if d["installed"] and self.mode == 1
             else (d["installed_version"] or d["candidate_version"], None), 180),
            ("Repository", "repository", False, None, 140),
            ("Size", "installed_size", False, lambda d: fmt_size(cfg, d["installed_size"]), 90),
            ("Description", "summary", True, None, 0),
        ], search_keys=("name", "summary"))
        self.table.connect("selection-changed", self._on_selection)
        self.table.connect("activated", lambda _t, d: self.win.show_package_details(d["name"]))
        self.append(self.table)
        self.install_btn = Gtk.Button(label="Install", css_classes=["suggested-action"], sensitive=False)
        self.install_btn.connect("clicked", lambda *_: self._apply("install"))
        self.remove_btn = Gtk.Button(label="Remove", css_classes=["destructive-action"], sensitive=False)
        self.remove_btn.connect("clicked", lambda *_: self._apply("remove"))
        self.purge_check = Gtk.CheckButton(label="Also remove configuration")
        self.cascade_check = Gtk.CheckButton(label="Remove unneeded dependencies")
        self.action_bar([self.cascade_check, self.purge_check, self.remove_btn, self.install_btn])

    def set_mode(self, mode):
        self.mode = mode
        self.filter_drop.set_visible(mode == 0)
        self.table.extra_filter = None
        if mode == 0:
            self._on_filter()
            self.table.set_items(self.installed_rows)
            self.table.set_query(self.entry.get_text())
            if not self.installed_rows:
                self.refresh()
        else:
            self.table.set_items([])
            self.table.show_status("Search Debian repositories", "Type at least two characters to search.")
            self._on_search(self.entry.get_text())

    def refresh(self):
        if self.mode == 1:
            self._on_search(self.entry.get_text())
            return
        self.table.show_loading("Reading installed packages…")

        def work():
            with self.win.apt_lock:
                ab = self.win.apt()
                cache = self.win.cache()
                return [ab.pkg_info(p) for p in ab.list_installed(cache)]

        def done(rows):
            self.installed_rows = rows
            if self.mode == 0:
                self.table.set_items(rows)
                self.table.set_query(self.entry.get_text())

        run_async(work, done, self.win.report_error)

    def _on_filter(self, *_):
        fn = self.FILTERS[self.filter_drop.get_selected()][1]
        self.table.extra_filter = fn
        self.table.refilter()

    def _on_search(self, text):
        if self.mode == 0:
            self.table.set_query(text)
            return
        self.search_serial += 1
        serial = self.search_serial
        terms = text.split()
        if len(text.strip()) < 2:
            self.table.set_items([])
            self.table.show_status("Search Debian repositories", "Type at least two characters to search.")
            return

        def start():
            if serial != self.search_serial:
                return False
            self.table.show_loading(f"Searching for “{text}”…")

            def work():
                with self.win.apt_lock:
                    ab = self.win.apt()
                    cache = self.win.cache()
                    return [ab.pkg_info(p) for p in ab.search(cache, terms, limit=400)]

            def done(rows):
                if serial == self.search_serial:
                    self.table.query = ""
                    self.table.set_items(rows)
                    if not rows:
                        self.table.show_status("No packages found", f"Nothing matches “{text}”.")

            run_async(work, done, self.win.report_error)
            return False

        GLib.timeout_add(350, start)

    def _on_selection(self, *_):
        sel = self.table.selected()
        n = len(sel)
        self.sel_label.set_text(f"{n} selected" if n else "")
        self.install_btn.set_sensitive(any(not d["installed"] or d["upgradable"] for d in sel))
        self.remove_btn.set_sensitive(any(d["installed"] for d in sel))

    def _apply(self, kind):
        sel = self.table.selected()
        if kind == "install":
            names = [d["name"] for d in sel if not d["installed"] or d["upgradable"]]
        else:
            names = [d["name"] for d in sel if d["installed"]]
        purge, cascade = self.purge_check.get_active(), self.cascade_check.get_active()
        self.win.confirm_apt(kind, names, purge=purge, cascade=cascade,
                             on_done=lambda ok: ok and self.table.clear_selection())


class FlatpakPage(Page):
    title = "Flatpak"
    icon = "application-x-addon-symbolic"

    def __init__(self, win):
        super().__init__(win)
        self.mode = 0
        self.serial = 0
        self.installed = []
        seg, _ = segmented(["Installed", "Discover"], self.set_mode)
        self.runtimes = Gtk.CheckButton(label="Runtimes")
        self.runtimes.connect("toggled", lambda *_: self.refresh())
        self.entry = self.search_bar("Search Flatpaks…", self._on_search, [seg, self.runtimes])
        cfg = win.config
        self.table = PackageTable([
            ("Name", "name", False, lambda d: (d["name"], "pkg-name"), 220),
            ("Application ID", "application", False, None, 260),
            ("Version", "version", False,
             lambda d: (("installed" if d.get("installed") and self.mode == 1 else d.get("version")),
                        "installed-badge" if d.get("installed") and self.mode == 1 else None), 110),
            ("Origin", "origin", False, lambda d: d.get("origin") or d.get("remotes"), 100),
            ("Size / Description", "installed_size", True,
             lambda d: fmt_size(cfg, d.get("installed_size")) or d.get("description", ""), 0),
        ], key="application", search_keys=("name", "application", "description"))
        self.table.connect("selection-changed", self._on_selection)
        self.table.connect("activated", lambda _t, d: self.mode == 0 and self._run(d))
        self.append(self.table)
        self.install_btn = Gtk.Button(label="Install", css_classes=["suggested-action"], sensitive=False)
        self.install_btn.connect("clicked", lambda *_: self._install())
        self.remove_btn = Gtk.Button(label="Remove", css_classes=["destructive-action"], sensitive=False)
        self.remove_btn.connect("clicked", lambda *_: self._remove())
        self.run_btn = Gtk.Button(label="Launch", sensitive=False)
        self.run_btn.connect("clicked", lambda *_: [self._run(d) for d in self.table.selected()])
        self.data_check = Gtk.CheckButton(label="Delete app data")
        self.action_bar([self.data_check, self.run_btn, self.remove_btn, self.install_btn])

    def on_shown(self):
        if not fp.available():
            self.table.show_status("Flatpak is not installed", "Install it with: sudo apt install flatpak",
                                   "dialog-warning-symbolic")
            return
        super().on_shown()

    def set_mode(self, mode):
        self.mode = mode
        self.runtimes.set_visible(mode == 0)
        if mode == 0:
            self.table.set_items(self.installed)
            self.table.set_query(self.entry.get_text())
        else:
            self.table.set_items([])
            self.table.show_status("Discover Flatpaks", "Search every configured remote, such as Flathub.")
            self._on_search(self.entry.get_text())

    def refresh(self):
        if not fp.available():
            return
        self.table.show_loading("Reading installed Flatpaks…")

        def done(rows):
            self.installed = rows
            if self.mode == 0:
                self.table.set_items(rows)

        run_async(lambda: fp.list_installed(self.runtimes.get_active()), done, self.win.report_error)

    def _on_search(self, text):
        if self.mode == 0:
            self.table.set_query(text)
            return
        self.serial += 1
        serial = self.serial
        if len(text.strip()) < 2:
            return

        def start():
            if serial != self.serial:
                return False
            self.table.show_loading(f"Searching Flatpak remotes for “{text}”…")

            def done(rows):
                if serial == self.serial:
                    self.table.query = ""
                    self.table.set_items(rows)
                    if not rows:
                        self.table.show_status("No Flatpaks found", f"Nothing matches “{text}”.")

            run_async(lambda: fp.search(text.split()), done, self.win.report_error)
            return False

        GLib.timeout_add(450, start)

    def _on_selection(self, *_):
        sel = self.table.selected()
        self.sel_label.set_text(f"{len(sel)} selected" if sel else "")
        installed = self.mode == 0 or any(d.get("installed") for d in sel)
        self.install_btn.set_sensitive(self.mode == 1 and any(not d.get("installed") for d in sel))
        self.remove_btn.set_sensitive(bool(sel) and installed)
        self.run_btn.set_sensitive(bool(sel) and installed)

    def _install(self):
        sel = [d for d in self.table.selected() if not d.get("installed")]
        for d in sel:
            remote = (d.get("remotes") or "").split(",")[0]
            args = ["install", "flatpak", d["application"]] + (["--remote", remote] if remote else [])
            self.win.run_cli(f"Installing {d['name']}", args, root=False, refresh=[self])

    def _remove(self):
        ids = [d["application"] for d in self.table.selected()]
        args = ["remove", "flatpak", *ids] + (["--remove-config"] if self.data_check.get_active() else [])
        self.win.ask(f"Remove {len(ids)} Flatpak(s)?", "\n".join(ids), "Remove",
                     lambda: self.win.run_cli("Removing Flatpaks", args, root=False, refresh=[self]), destructive=True)

    def _run(self, d):
        try:
            fp.run_app(Output(self.config), d["application"])
            self.win.toast(f"Launched {d['name']}")
        except ShellyError as exc:
            self.win.report_error(exc)


class AppImagePage(Page):
    title = "AppImage"
    icon = "application-x-executable-symbolic"

    def __init__(self, win):
        super().__init__(win)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.prefs = Adw.PreferencesPage()
        scroller.set_child(self.prefs)
        self.group = None
        self.append(scroller)
        add = Gtk.Button(label="Install AppImage…", css_classes=["suggested-action"])
        add.connect("clicked", self._pick_file)
        sync = Gtk.Button(label="Rescan folder")
        sync.connect("clicked", lambda *_: self.win.run_cli("Synchronizing AppImages", ["sync", "appimage"],
                                                             root=False, refresh=[self]))
        check = Gtk.Button(label="Check for updates")
        check.connect("clicked", lambda *_: self.win.run_cli("Upgrading AppImages", ["upgrade", "appimage"],
                                                              root=False, refresh=[self]))
        self.action_bar([check, sync, add])

    def refresh(self):
        if self.group:
            self.prefs.remove(self.group)
        self.group = Adw.PreferencesGroup(title="Installed AppImages",
                                          description=f"Stored in {self.config.appimage_dir}")
        self.prefs.add(self.group)
        db = appimg.load_db()
        if not db:
            self.group.add(Adw.ActionRow(title="No AppImages installed",
                                         subtitle="Use “Install AppImage…” to add one."))
            return
        for slug, e in sorted(db.items()):
            upd = e.get("update")
            row = Adw.ActionRow(title=GLib.markup_escape_text(e.get("name", slug)),
                                subtitle=GLib.markup_escape_text(
                                    f"{e.get('version', '?')} · {fmt_size(self.config, e.get('size'))} · "
                                    + (f"updates from {upd['type']} {upd['source']}" if upd else "no update source")))
            icon = Gtk.Image(pixel_size=32)
            if e.get("icon") and os.path.exists(e["icon"]):
                icon.set_from_file(e["icon"])
            else:
                icon.set_from_icon_name("application-x-executable")
            row.add_prefix(icon)
            for label, cb, css in (("Launch", self._launch, None), ("Updates…", self._configure, None),
                                   ("Remove", self._remove, "destructive-action")):
                b = Gtk.Button(label=label, valign=Gtk.Align.CENTER, css_classes=["flat"] + ([css] if css else []))
                b.connect("clicked", lambda _b, s=slug, cb=cb: cb(s))
                row.add_suffix(b)
            self.group.add(row)

    def _pick_file(self, *_):
        dialog = Gtk.FileDialog(title="Choose an AppImage")
        flt = Gtk.FileFilter(name="AppImages")
        flt.add_pattern("*.AppImage")
        flt.add_pattern("*.appimage")
        store = Gio.ListStore(item_type=Gtk.FileFilter)
        store.append(flt)
        dialog.set_filters(store)

        def done(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            self.win.run_cli("Installing AppImage", ["install", "appimage", f.get_path()], root=False, refresh=[self])

        dialog.open(self.win, None, done)

    def _launch(self, slug):
        try:
            appimg.run_app(Output(self.config), slug)
            self.win.toast(f"Launched {slug}")
        except ShellyError as exc:
            self.win.report_error(exc)

    def _remove(self, slug):
        self.win.ask(f"Remove {slug}?", "The AppImage file, its icon and desktop entry are deleted.", "Remove",
                     lambda: self.win.run_cli(f"Removing {slug}", ["remove", "appimage", slug], root=False, refresh=[self]),
                     destructive=True)

    def _configure(self, slug):
        entry = appimg.load_db().get(slug, {})
        upd = entry.get("update") or {}
        dialog = Adw.AlertDialog(heading=f"Update source for {slug}",
                                 body="GitHub: owner/repo (latest release asset). URL: a direct download link.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        kind = Gtk.DropDown.new_from_strings(["github", "url"])
        kind.set_selected(1 if upd.get("type") == "url" else 0)
        src = Gtk.Entry(placeholder_text="owner/repo or https://…", text=upd.get("source", ""))
        box.append(kind)
        box.append(src)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            if response == "save" and src.get_text().strip():
                try:
                    appimg.configure_updates(Output(self.config), slug, ["github", "url"][kind.get_selected()],
                                             src.get_text().strip())
                    self.refresh()
                except ShellyError as exc:
                    self.win.report_error(exc)

        dialog.connect("response", on_response)
        dialog.present(self.win)


class UpdatesPage(Page):
    title = "Updates"
    icon = "software-update-available-symbolic"

    def __init__(self, win):
        super().__init__(win)
        self.banner = Adw.Banner(title="A reboot is required to finish installing updates")
        self.append(self.banner)
        header = Gtk.Box(spacing=12, margin_top=12, margin_start=18, margin_end=18, margin_bottom=6)
        self.count = Gtk.Label(label="–", css_classes=["update-count"])
        header.append(self.count)
        self.summary = Gtk.Label(label="Checking for updates…", xalign=0, hexpand=True, wrap=True)
        header.append(self.summary)
        sync = Gtk.Button(label="Refresh lists", tooltip_text="Run apt update (asks for your password)")
        sync.connect("clicked", lambda *_: self.win.run_cli("Synchronizing package lists", ["sync", "standard"],
                                                             root=True, refresh=[self]))
        header.append(sync)
        self.upgrade_btn = Gtk.Button(label="Upgrade all", css_classes=["suggested-action", "pill"])
        self.upgrade_btn.connect("clicked", self._upgrade_all)
        header.append(self.upgrade_btn)
        self.append(header)
        self.table = PackageTable([
            ("Type", "type", False, None, 90),
            ("Name", "name", False, lambda d: (d["name"], "pkg-name"), 260),
            ("Installed", "old", False, None, 200),
            ("Available", "new", False, lambda d: (d["new"], "installed-badge"), 200),
            ("Repository", "repository", True, None, 0),
        ], key="name", checkable=False)
        self.append(self.table)

    def refresh(self):
        self.table.show_loading("Checking for updates…")
        self.summary.set_text("Checking for updates…")
        self.banner.set_revealed(system.restart_report()[0])
        cfg = self.config

        def work():
            rows = []
            with self.win.apt_lock:
                cache = self.win.cache()
                for r in self.win.apt().upgradable(cache)[0]:
                    rows.append({"type": "APT", "name": r["name"], "old": r["old"] or "(new)", "new": r["new"],
                                 "repository": r["repository"]})
                cache.clear()
            if fp.available() and not cfg["DisableFlatpakUpdateCheck"]:
                try:
                    for r in fp.list_updates():
                        rows.append({"type": "Flatpak", "name": r["name"] or r["application"], "old": "",
                                     "new": r["version"] or r["branch"], "repository": r["origin"]})
                except ShellyError:
                    pass
            if not cfg["DisableAppImageUpdateCheck"]:
                for r in appimg.list_updates():
                    rows.append({"type": "AppImage", "name": r["name"], "old": r["old"], "new": r["new"],
                                 "repository": ""})
            return rows

        def done(rows):
            self.count.set_text(str(len(rows)))
            self.upgrade_btn.set_sensitive(bool(rows))
            kinds = {}
            for r in rows:
                kinds[r["type"]] = kinds.get(r["type"], 0) + 1
            self.summary.set_text(", ".join(f"{n} {k}" for k, n in kinds.items()) + " update(s) available"
                                  if rows else "Your system is up to date")
            self.table.set_items(rows)
            if not rows:
                self.table.show_status("Everything is up to date", "Use “Refresh lists” to check the repositories again.",
                                       "emblem-ok-symbolic")
            self.win.set_update_badge(len(rows))

        run_async(work, done, self.win.report_error)

    def _upgrade_all(self, *_):
        # APT goes through pkexec; Flatpak and AppImage upgrades run as the user.
        def after_apt(ok):
            steps = []
            if fp.available() and not self.config["DisableFlatpakUpdateCheck"]:
                steps.append(("Upgrading Flatpaks", ["upgrade", "flatpak"]))
            if appimg.load_db() and not self.config["DisableAppImageUpdateCheck"]:
                steps.append(("Upgrading AppImages", ["upgrade", "appimage"]))
            self._chain(steps)

        self.win.run_cli("Upgrading APT packages", ["upgrade", "standard"], root=True, on_done=after_apt)

    def _chain(self, steps):
        if not steps:
            self.win.refresh_all()
            return
        title, args = steps[0]
        self.win.run_cli(title, args, root=False, on_done=lambda ok: self._chain(steps[1:]))


class UtilitiesPage(Page):
    title = "Utilities"
    icon = "applications-utilities-symbolic"

    def __init__(self, win):
        super().__init__(win)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.prefs = Adw.PreferencesPage()
        scroller.set_child(self.prefs)
        self.append(scroller)

        clean = Adw.PreferencesGroup(title="Maintenance")
        for title, subtitle, args, root in [
            ("Remove orphaned packages", "Dependencies nothing needs any more (apt autoremove)", ["purify", "standard"], True),
            ("Purge leftover configuration", "Config files of removed packages", ["purify", "standard", "--residual", "--keep-orphans"], True),
            ("Clear package cache", "Delete downloaded .deb files (apt clean)", ["purify", "standard", "--cache", "--keep-orphans"], True),
            ("Remove unused Flatpak runtimes", "flatpak uninstall --unused", ["purify", "flatpak"], False),
            ("Repair package database", "dpkg --configure -a and apt install -f", ["purify", "standard", "--keep-orphans"], True),
        ]:
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            b = Gtk.Button(label="Run", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda _b, a=args, r=root, t=title: self.win.run_cli(t, a, root=r))
            row.add_suffix(b)
            clean.add(row)
        self.prefs.add(clean)

        backup = Adw.PreferencesGroup(title="Backup")
        row = Adw.ActionRow(title="Export package list", subtitle="Manually installed APT packages, Flatpaks and AppImage sources (TOML)")
        b = Gtk.Button(label="Export…", valign=Gtk.Align.CENTER)
        b.connect("clicked", self._export)
        row.add_suffix(b)
        backup.add(row)
        row = Adw.ActionRow(title="Restore from backup", subtitle="Install everything listed in a backup file")
        b = Gtk.Button(label="Restore…", valign=Gtk.Align.CENTER)
        b.connect("clicked", self._restore)
        row.add_suffix(b)
        backup.add(row)
        self.prefs.add(backup)

        self.repo_group = None
        self.conf_group = None

    def refresh(self):
        for g in (self.repo_group, self.conf_group):
            if g:
                self.prefs.remove(g)
        self.repo_group = Adw.PreferencesGroup(title="Repositories", description="Entries in /etc/apt/sources.list.d")
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add repository", css_classes=["flat"])
        add.connect("clicked", self._add_repo)
        self.repo_group.set_header_suffix(add)
        for r in sources.list_repositories():
            name = os.path.splitext(os.path.basename(r["file"]))[0]
            row = Adw.ActionRow(title=GLib.markup_escape_text(r["uris"]),
                                subtitle=GLib.markup_escape_text(f"{r['types']} · {r['suites']} {r['components']} · {os.path.basename(r['file'])}"))
            if r["file"].startswith("/etc/apt/sources.list.d/"):
                sw = Gtk.Switch(active=r["enabled"], valign=Gtk.Align.CENTER)
                sw.connect("state-set", lambda _s, state, n=name: self._toggle_repo(n, state))
                row.add_suffix(sw)
                rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
                rm.connect("clicked", lambda _b, n=name: self.win.ask(
                    f"Remove repository {n}?", "Its signing key in /etc/apt/keyrings is removed too.", "Remove",
                    lambda: self.win.run_cli(f"Removing {n}", ["utility", "repository", "remove", n], root=True, refresh=[self]),
                    destructive=True))
                row.add_suffix(rm)
            self.repo_group.add(row)
        self.prefs.add(self.repo_group)

        self.conf_group = Adw.PreferencesGroup(title="Pending configuration files",
                                               description="New or old versions of config files left by upgrades (.dpkg-new, .dpkg-dist, .dpkg-old, .ucf-*)")
        found = sources.find_conffiles()
        if not found:
            self.conf_group.add(Adw.ActionRow(title="Nothing to review"))
        for f in found:
            row = Adw.ExpanderRow(title=GLib.markup_escape_text(f["path"]), subtitle=f["kind"])
            diff = Gtk.Label(label=sources.conffile_diff(f)[:6000], xalign=0, selectable=True, wrap=True,
                             css_classes=["log-view"], margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
            row.add_row(diff)
            self.conf_group.add(row)
        self.prefs.add(self.conf_group)

    def _toggle_repo(self, name, state):
        self.win.run_cli(f"{'Enabling' if state else 'Disabling'} {name}",
                         ["utility", "repository", "enable" if state else "disable", name], root=True, refresh=[self])
        return False

    def _add_repo(self, *_):
        dialog = Adw.AlertDialog(heading="Add repository",
                                 body="Enter a deb line's parts, or a PPA such as ppa:user/name in the URI field.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        fields = {}
        for key, ph in (("name", "Name (e.g. vscode)"), ("uri", "URI (https://…) or ppa:user/name"),
                        ("suite", "Suite (e.g. stable, trixie)"), ("components", "Components (default: main)"),
                        ("key", "Signing key URL or file (recommended)")):
            fields[key] = Gtk.Entry(placeholder_text=ph)
            box.append(fields[key])
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("add", "Add")
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            if response != "add":
                return
            v = {k: e.get_text().strip() for k, e in fields.items()}
            if v["uri"].startswith("ppa:"):
                args = ["utility", "repository", "add", v["uri"], "--refresh"]
            else:
                if not (v["name"] and v["uri"] and v["suite"]):
                    self.win.toast("Name, URI and suite are required")
                    return
                args = ["utility", "repository", "add", v["name"], v["uri"], v["suite"], *v["components"].split()]
                if v["key"]:
                    args += ["--key", v["key"]]
                args.append("--refresh")
            self.win.run_cli("Adding repository", args, root=True, refresh=[self])

        dialog.connect("response", on_response)
        dialog.present(self.win)

    def _export(self, *_):
        dialog = Gtk.FileDialog(title="Export backup", initial_name="shelly-backup.toml")

        def done(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            self.win.run_cli("Exporting backup", ["backup", "--output", f.get_path()], root=False)

        dialog.save(self.win, None, done)

    def _restore(self, *_):
        dialog = Gtk.FileDialog(title="Restore backup")

        def done(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            self.win.run_cli("Restoring backup", ["backup", "--restore", f.get_path()], root=False,
                             refresh=self.win.pages)

        dialog.open(self.win, None, done)


class SettingsPage(Page):
    title = "Settings"
    icon = "emblem-system-symbolic"

    def __init__(self, win):
        super().__init__(win)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        prefs = Adw.PreferencesPage()
        scroller.set_child(prefs)
        self.append(scroller)
        cfg = win.config

        def switch(group, key, title, subtitle=""):
            row = Adw.SwitchRow(title=title, subtitle=subtitle, active=cfg[key])
            row.connect("notify::active", lambda r, _p: self._set(key, r.get_active()))
            group.add(row)

        def combo(group, key, title):
            row = Adw.ComboRow(title=title, model=Gtk.StringList.new(list(CHOICES[key])))
            row.set_selected(list(CHOICES[key]).index(cfg[key]))
            row.connect("notify::selected", lambda r, _p: self._set(key, CHOICES[key][r.get_selected()]))
            group.add(row)

        g = Adw.PreferencesGroup(title="APT")
        switch(g, "UseFullUpgrade", "Full upgrade", "Allow upgrades to install or remove packages (apt full-upgrade)")
        switch(g, "InstallRecommends", "Install recommended packages")
        switch(g, "AutoRemoveAfterUpgrade", "Remove orphans after upgrading")
        switch(g, "CleanCacheAfterUpgrade", "Clean obsolete downloads after upgrading (apt autoclean)")
        prefs.add(g)

        g = Adw.PreferencesGroup(title="Flatpak and AppImage")
        combo(g, "FlatpakInstallation", "Install Flatpaks for")
        switch(g, "DisableFlatpakUpdateCheck", "Skip Flatpaks when upgrading everything")
        switch(g, "DisableAppImageUpdateCheck", "Skip AppImages when upgrading everything")
        row = Adw.EntryRow(title="AppImage folder", text=cfg["AppImageInstallPath"], show_apply_button=True)
        row.connect("apply", lambda r: self._set("AppImageInstallPath", r.get_text()))
        g.add(row)
        prefs.add(g)

        g = Adw.PreferencesGroup(title="General")
        combo(g, "FileSizeDisplay", "File size units")
        combo(g, "Elevation", "Privilege elevation (CLI)")
        prefs.add(g)

        g = Adw.PreferencesGroup(title="Update notifications")
        self.timer_row = Adw.SwitchRow(title="Check for updates in the background",
                                       subtitle="Runs shelly-notifications every few hours via a systemd user timer")
        rc, out_, _ = system.capture(["systemctl", "--user", "is-enabled", "shelly-notifications.timer"])
        self.timer_row.set_active(out_.strip() == "enabled")
        self.timer_row.connect("notify::active", self._toggle_timer)
        g.add(self.timer_row)
        prefs.add(g)

        about = Adw.PreferencesGroup(title="About")
        about.add(Adw.ActionRow(title="Shelly-APT", subtitle=f"Version {VERSION} · GPL-3.0 · Debian port of Shelly by Seafoam Labs"))
        about.add(Adw.ActionRow(title="Built with AI assistance",
                                subtitle="Developed with the help of AI (Anthropic's Claude)"))
        about.add(Adw.ActionRow(title="Configuration file", subtitle=cfg.path))
        prefs.add(about)

    def _set(self, key, value):
        try:
            self.config.set(key, value)
        except (ValueError, KeyError) as exc:
            self.win.report_error(exc)

    def _toggle_timer(self, row, _p):
        verb = ["enable", "--now"] if row.get_active() else ["disable", "--now"]
        rc, _o, err = system.capture(["systemctl", "--user", *verb, "shelly-notifications.timer"])
        if rc != 0:
            self.win.toast(err.strip() or "Could not change the timer (is shelly-apt installed system-wide?)")


# ---- window ---------------------------------------------------------------------

class ShellyWindow(Adw.ApplicationWindow):
    def __init__(self, app, start_page=None):
        super().__init__(application=app, title="Shelly", default_width=1180, default_height=760)
        self.config = Config()
        self.apt_lock = threading.Lock()
        self._apt_backend = None
        self._cache = None

        self.toasts = Adw.ToastOverlay()
        split = Adw.NavigationSplitView(min_sidebar_width=200, max_sidebar_width=240)
        self.toasts.set_child(split)
        self.set_content(self.toasts)

        self.pages = [PackagesPage(self), FlatpakPage(self), AppImagePage(self), UpdatesPage(self),
                      UtilitiesPage(self), SettingsPage(self)]
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        for p in self.pages:
            self.stack.add_named(p, p.title)

        # sidebar
        self.nav = Gtk.ListBox(css_classes=["navigation-sidebar", "nav-sidebar"])
        self.badges = {}
        for p in self.pages:
            row_box = Gtk.Box(spacing=12)
            row_box.append(Gtk.Image(icon_name=p.icon))
            row_box.append(Gtk.Label(label=p.title, xalign=0, hexpand=True))
            badge = Gtk.Label(css_classes=["dim-label", "caption"], visible=False)
            row_box.append(badge)
            self.badges[p.title] = badge
            row = Gtk.ListBoxRow(child=row_box)
            row.page = p
            self.nav.append(row)
        self.nav.connect("row-selected", self._on_nav)
        side_toolbar = Adw.ToolbarView()
        side_header = Adw.HeaderBar()
        brand = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        brand.append(Gtk.Image(icon_name=APP_ID, pixel_size=28))
        brand.append(Adw.WindowTitle(title="Shelly", subtitle="Debian"))
        side_header.set_title_widget(brand)
        side_toolbar.add_top_bar(side_header)
        side_toolbar.set_content(Gtk.ScrolledWindow(child=self.nav, vexpand=True))
        split.set_sidebar(Adw.NavigationPage(title="Shelly", child=side_toolbar))

        content_toolbar = Adw.ToolbarView()
        self.header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title="Packages")
        self.header.set_title_widget(self.title_widget)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload")
        refresh.connect("clicked", lambda *_: self.current_page().refresh())
        self.header.pack_end(refresh)
        content_toolbar.add_top_bar(self.header)
        content_toolbar.set_content(self.stack)
        self.content_page = Adw.NavigationPage(title="Packages", child=content_toolbar)
        split.set_content(self.content_page)

        index = next((i for i, p in enumerate(self.pages) if p.title.lower() == (start_page or "").lower()), 0)
        self.nav.select_row(self.nav.get_row_at_index(index))
        # Check for updates in the background so the sidebar badge is filled in.
        updates = self.pages[3]
        if index != 3:
            GLib.timeout_add_seconds(2, lambda: updates.on_shown() or False)

    # navigation
    def _on_nav(self, _lb, row):
        if row is None:
            return
        page = row.page
        self.stack.set_visible_child(page)
        self.title_widget.set_title(page.title)
        self.content_page.set_title(page.title)
        page.on_shown()

    def current_page(self):
        return self.stack.get_visible_child()

    def set_update_badge(self, n):
        badge = self.badges["Updates"]
        badge.set_text(str(n))
        badge.set_visible(n > 0)

    # apt access (worker threads only, under apt_lock)
    def apt(self):
        if self._apt_backend is None:
            from . import apt_backend
            self._apt_backend = apt_backend
        return self._apt_backend

    def cache(self):
        if self._cache is None:
            self._cache = self.apt().open_cache()
        else:
            self._cache.clear()
        return self._cache

    def invalidate_cache(self):
        with self.apt_lock:
            self._cache = None

    # feedback
    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(text)), timeout=4))

    def report_error(self, exc):
        self.toast(f"Error: {exc}")
        return False

    def ask(self, heading, body, confirm_label, on_yes, destructive=False):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", confirm_label)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                       else Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok" if not destructive else "cancel")
        dialog.connect("response", lambda _d, r: r == "ok" and on_yes())
        dialog.present(self)

    # running the CLI
    def run_cli(self, title, args, root=False, refresh=None, on_done=None):
        argv = [shelly_command(), "--ui-mode", "--no-confirm", *args]
        if root and not system.is_root():
            argv = ["pkexec", *argv]

        def finished(ok):
            if root:
                self.invalidate_cache()
            for page in (refresh or []):
                page.loaded = False
                if page is self.current_page():
                    page.on_shown()
            if not ok:
                self.toast(f"{title} failed — see the log for details")
            if on_done:
                on_done(ok)

        TransactionDialog(title, argv, finished).present(self)

    def refresh_all(self):
        self.invalidate_cache()
        for p in self.pages:
            p.loaded = False
        self.current_page().on_shown()

    def confirm_apt(self, kind, names, purge=False, cascade=False, on_done=None):
        """Preview an APT transaction in-process, then run it through pkexec."""
        if not names:
            return

        def work():
            with self.apt_lock:
                ab = self.apt()
                cache = self.cache()
                if kind == "install":
                    plan = ab.plan_install(cache, names, recommends=self.config["InstallRecommends"])
                else:
                    plan = ab.plan_remove(cache, names, purge=purge, cascade=cascade)
                cache.clear()
                return plan

        def done(plan):
            out = Output(self.config)
            lines = []
            for key, label in (("install", "Install"), ("upgrade", "Upgrade"), ("reinstall", "Reinstall"),
                               ("downgrade", "Downgrade"), ("remove", "Remove"), ("purge", "Purge")):
                rows = plan.get(key) or []
                if rows:
                    shown = ", ".join(r["name"] for r in rows[:25]) + (f" and {len(rows) - 25} more" if len(rows) > 25 else "")
                    lines.append(f"{label} ({len(rows)}): {shown}")
            lines.append(f"\nDownload: {out.size(plan['download_size'])} · Disk space change: {out.size(plan['space_change'])}")
            extra = [r["name"] for k in ("remove", "purge") for r in plan.get(k, []) if r["name"] not in names]
            if kind == "install" and extra:
                lines.insert(0, "⚠ Installing these packages will REMOVE other packages.\n")
            if kind == "install":
                args = ["install", "standard", *names]
            else:
                args = ["remove", "standard", *names] + (["--remove-config"] if purge else []) + (["--cascade"] if cascade else [])
            self.ask("Install packages?" if kind == "install" else "Remove packages?", "\n".join(lines),
                     "Install" if kind == "install" else "Remove",
                     lambda: self.run_cli("Installing packages" if kind == "install" else "Removing packages", args,
                                          root=True, refresh=[self.pages[0], self.pages[3]], on_done=on_done),
                     destructive=(kind == "remove" or bool(extra)))

        run_async(work, done, self.report_error)

    def show_package_details(self, name):
        def work():
            with self.apt_lock:
                ab = self.apt()
                return ab.pkg_info(ab.find_package(self.cache(), name), detail=True)

        def done(info):
            out = Output(self.config)
            dialog = Adw.Dialog(title=info["name"], content_width=560, content_height=620)
            tv = Adw.ToolbarView()
            tv.add_top_bar(Adw.HeaderBar())
            page = Adw.PreferencesPage()
            head = Adw.PreferencesGroup(title=GLib.markup_escape_text(info["name"]),
                                        description=GLib.markup_escape_text(info.get("summary") or ""))
            page.add(head)
            facts = Adw.PreferencesGroup()
            for label, value in (
                ("Installed", info.get("installed_version")), ("Available", info.get("candidate_version")),
                ("Repository", info.get("repository")), ("Section", info.get("section")),
                ("Installed size", out.size(info.get("installed_size"))), ("Download size", out.size(info.get("download_size"))),
                ("Install reason", ("automatic" if info["auto"] else "manual") if info["installed"] else None),
                ("Held", "yes" if info.get("held") else None), ("Homepage", info.get("homepage")),
                ("Maintainer", info.get("maintainer")), ("Source package", info.get("source")),
                ("Depends", ", ".join(info.get("depends", []))), ("Recommends", ", ".join(info.get("recommends", []))),
                ("Versions", ", ".join(v["version"] for v in info.get("versions", []))),
            ):
                if value:
                    row = Adw.ActionRow(title=label, subtitle=GLib.markup_escape_text(str(value)), subtitle_selectable=True)
                    facts.add(row)
            page.add(facts)
            if info.get("description"):
                desc = Adw.PreferencesGroup(title="Description")
                desc.add(Gtk.Label(label=info["description"], wrap=True, xalign=0, selectable=True))
                page.add(desc)
            tv.set_content(page)
            buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=8, margin_bottom=12, margin_end=12)
            if info["installed"]:
                if info.get("held"):
                    b = Gtk.Button(label="Unhold")
                    b.connect("clicked", lambda *_: (dialog.close(), self.run_cli("Unholding", ["mark", "unhold", info["name"]], root=True, refresh=[self.pages[0]])))
                else:
                    b = Gtk.Button(label="Hold version")
                    b.connect("clicked", lambda *_: (dialog.close(), self.run_cli("Holding", ["mark", "hold", info["name"]], root=True, refresh=[self.pages[0]])))
                buttons.append(b)
                rm = Gtk.Button(label="Remove", css_classes=["destructive-action"])
                rm.connect("clicked", lambda *_: (dialog.close(), self.confirm_apt("remove", [info["name"]])))
                buttons.append(rm)
            if not info["installed"] or info["upgradable"]:
                ins = Gtk.Button(label="Update" if info["installed"] else "Install", css_classes=["suggested-action"])
                ins.connect("clicked", lambda *_: (dialog.close(), self.confirm_apt("install", [info["name"]])))
                buttons.append(ins)
            tv.add_bottom_bar(buttons)
            dialog.set_child(tv)
            dialog.present(self)

        run_async(work, done, self.report_error)


class ShellyApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option("page", ord("p"), GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Open on a page (packages, flatpak, appimage, updates, utilities, settings)", "PAGE")

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        display = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # Running from a source checkout: let GTK find the bundled icon.
        source_icons = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "data", "icons")
        if os.path.isdir(source_icons):
            Gtk.IconTheme.get_for_display(display).add_search_path(source_icons)
        Gtk.Window.set_default_icon_name(APP_ID)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_command_line(self, command_line):
        opts = command_line.get_options_dict().end().unpack()
        win = self.get_active_window()
        if win is None:
            win = ShellyWindow(self, opts.get("page"))
        elif opts.get("page"):
            for i, p in enumerate(win.pages):
                if p.title.lower() == opts["page"].lower():
                    win.nav.select_row(win.nav.get_row_at_index(i))
        win.present()
        return 0


def main():
    return ShellyApp().run(sys.argv)
