"""Flatpak backend: a thin, structured wrapper around the flatpak CLI.

Flatpak authorizes system-wide changes itself through polkit, so these
operations never need Shelly to elevate.
"""

import re

from . import system
from .term import ShellyError


def available():
    return system.have("flatpak")


def require():
    if not available():
        raise ShellyError("Flatpak is not installed (sudo apt install flatpak)")


def _rows(args, columns):
    rc, out, err = system.capture(["flatpak", *args, "--columns=" + ",".join(columns)])
    if rc != 0:
        if "No remote refs found" in err or not err.strip():
            return []
        raise ShellyError(err.strip())
    rows = []
    for line in out.splitlines():
        if not line.strip() or "\t" not in line and line.strip().startswith(("No matches", "No remote")):
            continue
        parts = line.split("\t")
        parts += [""] * (len(columns) - len(parts))
        rows.append(dict(zip(columns, (p.strip() for p in parts))))
    return rows


SIZE_RE = re.compile(r"([\d.,]+)\s*([kMGT]?B)")


def parse_size(text):
    m = SIZE_RE.search(text or "")
    if not m:
        return 0
    num = float(m.group(1).replace(",", "."))
    mult = {"B": 1, "kB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4}[m.group(2)]
    return int(num * mult)


def list_installed(include_runtimes=False):
    require()
    cols = ["application", "name", "version", "branch", "arch", "origin", "installation", "size", "ref"]
    rows = _rows(["list"] + ([] if include_runtimes else ["--app"]), cols)
    for r in rows:
        r["installed_size"] = parse_size(r.pop("size"))
    return sorted(rows, key=lambda r: r["name"].lower())


def search(terms):
    require()
    cols = ["application", "name", "description", "version", "branch", "remotes"]
    rows = _rows(["search", " ".join(terms)], cols)
    installed = {r["application"] for r in list_installed(include_runtimes=True)}
    for r in rows:
        r["installed"] = r["application"] in installed
    return rows


def list_updates():
    require()
    cols = ["application", "name", "version", "branch", "origin"]
    rows = []
    for scope in ("--system", "--user"):
        try:
            for r in _rows(["remote-ls", scope, "--updates"], cols):
                r["installation"] = scope[2:]
                rows.append(r)
        except ShellyError:
            continue
    return rows


def remotes():
    require()
    return _rows(["remotes", "--show-disabled"], ["name", "title", "url", "options"])


def ps():
    require()
    return _rows(["ps"], ["instance", "application", "pid"])


def _scope(config, user_flag):
    if user_flag:
        return ["--user"]
    return ["--user"] if config["FlatpakInstallation"] == "user" else ["--system"]


def _y(out):
    return ["-y", "--noninteractive"] if (out.no_confirm or out.ui_mode) else []


def install(out, config, refs, remote=None, user=False):
    require()
    for ref in refs:
        if ref.endswith(".flatpakref") or ref.startswith(("http://", "https://")):
            cmd = ["flatpak", "install", *_scope(config, user), *_y(out), "--from", ref]
        elif ref.endswith(".flatpak"):
            cmd = ["flatpak", "install", *_scope(config, user), *_y(out), "--bundle", ref]
        else:
            cmd = ["flatpak", "install", *_scope(config, user), *_y(out)] + ([remote] if remote else []) + [ref]
        out.info(f"Installing Flatpak {ref}")
        system.run(out, cmd, user=True)


def remove(out, refs, delete_data=False, unused=False):
    require()
    cmd = ["flatpak", "uninstall", *_y(out)] + (["--delete-data"] if delete_data else []) + list(refs)
    system.run(out, cmd, user=True)
    if unused:
        system.run(out, ["flatpak", "uninstall", "--unused", *_y(out)], user=True)


def update(out, refs=()):
    require()
    system.run(out, ["flatpak", "update", *_y(out), *refs], user=True)


def purify(out):
    require()
    system.run(out, ["flatpak", "uninstall", "--unused", *_y(out)], user=True)


def repair(out, user=False):
    require()
    system.run(out, ["flatpak", "repair"] + (["--user"] if user else []), user=True)


def remote_add(out, name, url, user=False):
    require()
    flag = "--user" if user else "--system"
    system.run(out, ["flatpak", "remote-add", flag, "--if-not-exists", name, url], user=True)


def remote_delete(out, name, user=False):
    require()
    flag = "--user" if user else "--system"
    system.run(out, ["flatpak", "remote-delete", flag, name], user=True)


def sync(out):
    """Refresh AppStream metadata for every remote."""
    require()
    system.run(out, ["flatpak", "update", "--appstream", *_y(out)], user=True)


def run_app(out, app_id, kill=False):
    require()
    if kill:
        system.run(out, ["flatpak", "kill", app_id], user=True)
    else:
        import subprocess
        subprocess.Popen(["flatpak", "run", app_id], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        out.ok(f"Launched {app_id}")
