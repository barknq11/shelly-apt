"""shelly-notifications: check for updates and post a desktop notification.

Run periodically by shelly-notifications.timer (systemd --user). It relies on
the system's own apt-daily timer to refresh package lists, so it never needs root.
"""

import json
import os
import subprocess
import sys

from . import appimage_backend as appimg, flatpak_backend as fp
from .config import Config, xdg_dir


def count_updates(config):
    counts = {"APT": 0, "Flatpak": 0, "AppImage": 0}
    try:
        from . import apt_backend
        rows, _ = apt_backend.upgradable(apt_backend.open_cache(), full=config["UseFullUpgrade"])
        counts["APT"] = len(rows)
    except Exception as exc:
        print(f"APT check failed: {exc}", file=sys.stderr)
    if fp.available() and not config["DisableFlatpakUpdateCheck"]:
        try:
            counts["Flatpak"] = len(fp.list_updates())
        except Exception as exc:
            print(f"Flatpak check failed: {exc}", file=sys.stderr)
    if not config["DisableAppImageUpdateCheck"]:
        try:
            counts["AppImage"] = len(appimg.list_updates())
        except Exception as exc:
            print(f"AppImage check failed: {exc}", file=sys.stderr)
    return counts


def notify(summary, body):
    """Send a notification with an 'Open Shelly' action and wait briefly for a click."""
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    proxy = Gio.DBusProxy.new_sync(bus, Gio.DBusProxyFlags.NONE, None, "org.freedesktop.Notifications",
                                   "/org/freedesktop/Notifications", "org.freedesktop.Notifications", None)
    hints = {"desktop-entry": GLib.Variant("s", "org.shellyapt.Shelly"),
             "urgency": GLib.Variant("y", 1)}
    nid = proxy.call_sync("Notify", GLib.Variant("(susssasa{sv}i)", (
        "Shelly", 0, "org.shellyapt.Shelly", summary, body,
        ["default", "Open Shelly", "open", "Open Shelly"], hints, -1)),
        Gio.DBusCallFlags.NONE, -1, None).unpack()[0]

    loop = GLib.MainLoop()

    def on_signal(_proxy, _sender, signal, params):
        args = params.unpack()
        if args[0] != nid:
            return
        if signal == "ActionInvoked":
            subprocess.Popen(["shelly-ui", "--page", "updates"], start_new_session=True)
            loop.quit()
        elif signal == "NotificationClosed":
            loop.quit()

    proxy.connect("g-signal", on_signal)
    GLib.timeout_add_seconds(300, loop.quit)
    loop.run()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    config = Config()
    force = "--force" in argv
    if not config["NotifyOnUpdates"] and not force:
        return 0
    counts = count_updates(config)
    total = sum(counts.values())
    state_path = os.path.join(xdg_dir("cache"), "notified.json")
    try:
        with open(state_path) as fh:
            last = json.load(fh)
    except (OSError, ValueError):
        last = {}
    if "--print" in argv:
        print(json.dumps(counts))
        return 0
    if total == 0 or (counts == last and not force):
        return 0
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump(counts, fh)
    parts = [f"{n} {kind}" for kind, n in counts.items() if n]
    try:
        notify(f"{total} update{'s' if total != 1 else ''} available", ", ".join(parts))
    except Exception as exc:
        print(f"Could not send notification: {exc}", file=sys.stderr)
        return 1
    return 0
