"""AppImage backend: install into a user directory, integrate with the desktop,
and check GitHub releases or plain URLs for updates."""

import configparser
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request

from . import VERSION
from .config import invoking_user, user_home, xdg_dir
from .term import ShellyError

USER_AGENT = f"shelly-apt/{VERSION}"


def _chown(path):
    user = invoking_user()
    if os.getuid() == 0 and user.pw_uid != 0:
        try:
            os.chown(path, user.pw_uid, user.pw_gid)
        except OSError:
            pass


def db_path():
    return os.path.join(xdg_dir("data"), "appimages.json")


def load_db():
    try:
        with open(db_path()) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_db(db):
    path = db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as fh:
        json.dump(db, fh, indent=2, sort_keys=True)
    os.replace(path + ".tmp", path)
    _chown(path)


def slugify(text):
    text = re.sub(r"\.AppImage$", "", text, flags=re.I)
    # Drop version / arch suffixes like -1.2.3-x86_64
    text = re.sub(r"[-_ ]v?\d+(\.\d+)+.*$", "", text)
    text = re.sub(r"[-_](x86[-_]64|amd64|aarch64|arm64|i386|i686)$", "", text, flags=re.I)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-").lower() or "appimage"


def applications_dir():
    return os.path.join(user_home(), ".local/share/applications")


def icons_dir():
    return os.path.join(xdg_dir("data"), "icons")


def _extract_metadata(appimage):
    """Extract the embedded .desktop file and icon. Runs the AppImage's own extractor."""
    meta = {"name": None, "version": None, "icon": None, "comment": None, "categories": None}
    tmp = tempfile.mkdtemp(prefix="shelly-appimage-")
    try:
        for pattern in ("*.desktop", ".DirIcon", "*.png", "*.svg"):
            subprocess.run([appimage, "--appimage-extract", pattern], cwd=tmp,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        root = os.path.join(tmp, "squashfs-root")
        desktops = glob.glob(os.path.join(root, "*.desktop"))
        if desktops:
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            cp.optionxform = str
            try:
                cp.read(desktops[0], encoding="utf-8")
                entry = cp["Desktop Entry"]
                meta["name"] = entry.get("Name")
                meta["version"] = entry.get("X-AppImage-Version")
                meta["comment"] = entry.get("Comment")
                meta["categories"] = entry.get("Categories")
                meta["icon_name"] = entry.get("Icon")
            except (configparser.Error, KeyError):
                pass
        icon = None
        icon_name = meta.get("icon_name")
        candidates = []
        if icon_name:
            candidates += glob.glob(os.path.join(root, icon_name + ".*"))
        candidates += [os.path.join(root, ".DirIcon")] + glob.glob(os.path.join(root, "*.png")) + glob.glob(os.path.join(root, "*.svg"))
        for c in candidates:
            real = os.path.realpath(c)
            if os.path.isfile(real) and real.startswith(os.path.realpath(root)):
                icon = real
                break
        if icon:
            meta["icon_tmp"] = icon
            meta["tmp"] = tmp
            return meta
    except (OSError, subprocess.SubprocessError):
        pass
    shutil.rmtree(tmp, ignore_errors=True)
    return meta


def _write_desktop(slug, entry):
    os.makedirs(applications_dir(), exist_ok=True)
    path = os.path.join(applications_dir(), f"shelly-appimage-{slug}.desktop")
    env = " ".join(f"{k}={v}" for k, v in entry.get("env", {}).items())
    exec_line = (f"env {env} " if env else "") + f'"{entry["path"]}" %U'
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={entry.get('name') or slug}",
        f"Exec={exec_line}",
        f"Icon={entry.get('icon') or 'application-x-executable'}",
        f"Comment={entry.get('comment') or ''}",
        f"Categories={entry.get('categories') or 'Utility;'}",
        "Terminal=false",
        f"X-Shelly-AppImage={slug}",
    ]
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    _chown(path)
    return path


def install(out, config, location, install_path=None, name=None):
    if not location.lower().endswith(".appimage") or not os.path.isfile(location):
        raise ShellyError(f"'{location}' is not an existing .AppImage file")
    dest_dir = install_path or config.appimage_dir
    os.makedirs(dest_dir, exist_ok=True)
    _chown(dest_dir)
    os.chmod(location, os.stat(location).st_mode | 0o111)
    out.info(f"Reading metadata from {os.path.basename(location)}")
    meta = _extract_metadata(os.path.abspath(location))
    slug = slugify(name or meta.get("name") or os.path.basename(location))
    dest = os.path.join(dest_dir, f"{slug}.AppImage")
    if os.path.abspath(location) != os.path.abspath(dest):
        shutil.copy2(location, dest)
    os.chmod(dest, 0o755)
    _chown(dest)

    icon_path = None
    if meta.get("icon_tmp"):
        os.makedirs(icons_dir(), exist_ok=True)
        ext = os.path.splitext(meta["icon_tmp"])[1] or ".png"
        icon_path = os.path.join(icons_dir(), slug + ext)
        shutil.copyfile(meta["icon_tmp"], icon_path)
        _chown(icon_path)
    if meta.get("tmp"):
        shutil.rmtree(meta["tmp"], ignore_errors=True)

    db = load_db()
    previous = db.get(slug, {})
    entry = {
        "name": meta.get("name") or slug,
        "path": dest,
        "version": meta.get("version") or previous.get("version") or "unknown",
        "icon": icon_path,
        "comment": meta.get("comment"),
        "categories": meta.get("categories"),
        "size": os.path.getsize(dest),
        "installed_at": int(time.time()),
        "update": previous.get("update"),
        "env": previous.get("env", {}),
    }
    entry["desktop"] = _write_desktop(slug, entry)
    db[slug] = entry
    save_db(db)
    out.ok(f"Installed AppImage {entry['name']} → {dest}")
    return slug


def find(db, name):
    if name in db:
        return name
    low = name.lower()
    for slug, entry in db.items():
        if entry.get("name", "").lower() == low or slugify(name) == slug:
            return slug
    raise ShellyError(f"No installed AppImage named '{name}'. See 'shelly list appimage'.")


def remove(out, name, remove_config=False):
    db = load_db()
    slug = find(db, name)
    entry = db.pop(slug)
    for key in ("path", "desktop", "icon"):
        p = entry.get(key)
        if p and os.path.exists(p):
            os.remove(p)
    if remove_config:
        for d in (os.path.join(user_home(), ".config", entry.get("name", slug)),
                  os.path.join(user_home(), ".config", slug)):
            if os.path.isdir(d):
                shutil.rmtree(d)
                out.info(f"Removed {d}")
    save_db(db)
    out.ok(f"Removed AppImage {entry.get('name', slug)}")


def sync(out, config):
    """Reconcile the database with the AppImages actually present on disk."""
    db = load_db()
    changed = 0
    for slug in list(db):
        if not os.path.exists(db[slug].get("path", "")):
            out.warn(f"{slug}: file is missing; forgetting it")
            db.pop(slug)
            changed += 1
    save_db(db)
    known = {os.path.realpath(e["path"]) for e in db.values()}
    for f in sorted(glob.glob(os.path.join(config.appimage_dir, "*.AppImage"))
                    + glob.glob(os.path.join(config.appimage_dir, "*.appimage"))):
        if os.path.realpath(f) not in known:
            install(out, config, f)
            changed += 1
    out.ok(f"AppImage metadata synchronized ({changed} change{'s' if changed != 1 else ''})")


def configure_updates(out, name, kind, source, pattern=None):
    db = load_db()
    slug = find(db, name)
    if kind not in ("github", "url"):
        raise ShellyError("Update source type must be 'github' (owner/repo) or 'url'")
    if kind == "github" and not re.fullmatch(r"[\w.-]+/[\w.-]+", source):
        m = re.search(r"github\.com/([\w.-]+/[\w.-]+)", source)
        if not m:
            raise ShellyError("GitHub sources look like owner/repo")
        source = m.group(1)
    db[slug]["update"] = {"type": kind, "source": source, "pattern": pattern}
    save_db(db)
    out.ok(f"{slug}: updates will be checked from {kind} {source}")


def set_env(out, name, assignments):
    db = load_db()
    slug = find(db, name)
    env = db[slug].setdefault("env", {})
    for a in assignments:
        key, sep, value = a.partition("=")
        if not sep:
            raise ShellyError(f"'{a}' is not KEY=VALUE")
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    _write_desktop(slug, db[slug])
    save_db(db)
    out.ok(f"{slug}: environment = {env or '{}'}")


def _http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def _arch_words():
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return ("x86_64", "amd64", "x64")
    if m in ("aarch64", "arm64"):
        return ("aarch64", "arm64")
    return (m,)


def check_update(entry):
    """Return {'version', 'url'} when a newer build is available, else None."""
    upd = entry.get("update")
    if not upd:
        return None
    if upd["type"] == "github":
        data = _http_json(f"https://api.github.com/repos/{upd['source']}/releases/latest")
        tag = data.get("tag_name", "")
        assets = [a for a in data.get("assets", []) if a["name"].lower().endswith(".appimage")]
        if upd.get("pattern"):
            assets = [a for a in assets if re.search(upd["pattern"], a["name"])]
        arch = [a for a in assets if any(w in a["name"].lower() for w in _arch_words())]
        asset = (arch or assets or [None])[0]
        if not asset:
            return None
        current = str(entry.get("version", "")).lstrip("v")
        if tag.lstrip("v") == current:
            return None
        return {"version": tag.lstrip("v"), "url": asset["browser_download_url"]}
    req = urllib.request.Request(upd["source"], method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        stamp = resp.headers.get("ETag") or resp.headers.get("Last-Modified") or ""
    if stamp and stamp != upd.get("stamp"):
        return {"version": stamp.strip('"')[:24], "url": upd["source"], "stamp": stamp}
    return None


def list_updates(out=None):
    rows = []
    for slug, entry in sorted(load_db().items()):
        try:
            found = check_update(entry)
        except Exception as exc:
            if out:
                out.warn(f"{slug}: update check failed: {exc}")
            continue
        if found:
            rows.append({"name": slug, "old": entry.get("version"), "new": found["version"],
                         "url": found["url"], "stamp": found.get("stamp")})
    return rows


def upgrade(out, config, names=None):
    updates = list_updates(out)
    if names:
        updates = [u for u in updates if u["name"] in names]
    if not updates:
        out.ok("All AppImages are up to date")
        return 0
    for u in updates:
        out.info(f"Downloading {u['name']} {u['new']}")
        tmpdir = tempfile.mkdtemp(prefix="shelly-appimage-dl-")
        try:
            target = os.path.join(tmpdir, f"{u['name']}.AppImage")
            req = urllib.request.Request(u["url"], headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp, open(target, "wb") as fh:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        out.event("progress", phase="download", item=u["name"],
                                  percent=100.0 * done / total, message=f"Downloading {u['name']}")
            db = load_db()
            entry = db[u["name"]]
            install(out, config, target, install_path=os.path.dirname(entry["path"]), name=u["name"])
            db = load_db()
            db[u["name"]]["version"] = u["new"]
            if u.get("stamp"):
                db[u["name"]]["update"]["stamp"] = u["stamp"]
            save_db(db)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return len(updates)


def run_app(out, name, kill=False):
    db = load_db()
    slug = find(db, name)
    entry = db[slug]
    if kill:
        subprocess.call(["pkill", "-f", entry["path"]])
        out.ok(f"Stopped {slug}")
        return
    env = dict(os.environ)
    env.update(entry.get("env", {}))
    subprocess.Popen([entry["path"]], env=env, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out.ok(f"Launched {entry.get('name', slug)}")
