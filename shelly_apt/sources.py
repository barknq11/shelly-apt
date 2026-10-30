"""APT repository (sources.list / deb822) and signing-key management, plus the
dpkg equivalents of pacnew/pacsave files."""

import glob
import os
import re
import subprocess
import urllib.request

from . import VERSION, system
from .term import ShellyError

SOURCES_DIR = "/etc/apt/sources.list.d"
KEYRING_DIR = "/etc/apt/keyrings"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# ---- reading -------------------------------------------------------------

def _parse_one_line(path):
    entries = []
    try:
        with open(path) as fh:
            for line in fh:
                raw = line.strip()
                enabled = True
                if raw.startswith("#"):
                    raw = raw.lstrip("#").strip()
                    enabled = False
                    if not raw.startswith(("deb ", "deb-src ", "deb [", "deb-src [")):
                        continue
                if not raw or not raw.startswith("deb"):
                    continue
                options = ""
                m = re.match(r"^(deb(?:-src)?)\s+\[([^\]]*)\]\s+(.*)$", raw)
                if m:
                    kind, options, rest = m.groups()
                else:
                    kind, _, rest = raw.partition(" ")
                parts = rest.split()
                if len(parts) < 2:
                    continue
                entries.append({
                    "file": path, "format": "one-line", "enabled": enabled,
                    "types": kind, "uris": parts[0], "suites": parts[1],
                    "components": " ".join(parts[2:]), "options": options,
                })
    except OSError:
        pass
    return entries


def _parse_deb822(path):
    entries = []
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return entries
    for stanza in re.split(r"\n\s*\n", text):
        fields, last = {}, None
        for line in stanza.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line[0] in " \t" and last:
                fields[last] += "\n" + line.strip()
                continue
            key, sep, value = line.partition(":")
            if sep:
                last = key.strip().lower()
                fields[last] = value.strip()
        if "uris" not in fields:
            continue
        entries.append({
            "file": path, "format": "deb822",
            "enabled": fields.get("enabled", "yes").lower() != "no",
            "types": fields.get("types", "deb"), "uris": fields.get("uris", ""),
            "suites": fields.get("suites", ""), "components": fields.get("components", ""),
            "options": f"signed-by={fields['signed-by'].splitlines()[0]}" if "signed-by" in fields else "",
        })
    return entries


def list_repositories():
    entries = _parse_one_line("/etc/apt/sources.list")
    for path in sorted(glob.glob(os.path.join(SOURCES_DIR, "*.list"))):
        entries += _parse_one_line(path)
    for path in sorted(glob.glob(os.path.join(SOURCES_DIR, "*.sources"))):
        entries += _parse_deb822(path)
    return entries


# ---- writing -------------------------------------------------------------

def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": f"shelly-apt/{VERSION}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def add_key(out, name, source):
    """Store a signing key in /etc/apt/keyrings and return its path."""
    if not NAME_RE.match(name):
        raise ShellyError(f"Invalid key name '{name}'")
    if source.startswith(("http://", "https://")):
        if source.startswith("http://"):
            out.warn("Fetching a signing key over plain HTTP; verify its fingerprint")
        data = _fetch(source)
    else:
        with open(source, "rb") as fh:
            data = fh.read()
    armored = b"-----BEGIN PGP PUBLIC KEY BLOCK-----" in data
    os.makedirs(KEYRING_DIR, mode=0o755, exist_ok=True)
    path = os.path.join(KEYRING_DIR, f"{name}.{'asc' if armored else 'gpg'}")
    with open(path, "wb") as fh:
        fh.write(data)
    os.chmod(path, 0o644)
    for k in describe_key(path):
        out.info(f"Key {k['fingerprint']}  {k['uid']}")
    out.ok(f"Saved signing key to {path}")
    return path


def describe_key(path):
    rc, stdout, _ = system.capture(["gpg", "--batch", "--with-colons", "--show-keys", path])
    keys, cur = [], None
    for line in stdout.splitlines():
        f = line.split(":")
        if f[0] == "pub":
            cur = {"file": path, "fingerprint": "", "uid": "", "expires": f[6] or "", "created": f[5]}
            keys.append(cur)
        elif f[0] == "fpr" and cur is not None and not cur["fingerprint"]:
            cur["fingerprint"] = f[9]
        elif f[0] == "uid" and cur is not None and not cur["uid"]:
            cur["uid"] = f[9]
    return keys


def list_keys():
    files = []
    for d in (KEYRING_DIR, "/etc/apt/trusted.gpg.d", "/usr/share/keyrings"):
        files += sorted(glob.glob(os.path.join(d, "*.gpg")) + glob.glob(os.path.join(d, "*.asc")))
    if os.path.exists("/etc/apt/trusted.gpg"):
        files.append("/etc/apt/trusted.gpg")
    keys = []
    for f in files:
        found = describe_key(f)
        keys += found or [{"file": f, "fingerprint": "", "uid": "(unreadable)", "expires": "", "created": ""}]
    return keys


def remove_key(out, name):
    removed = False
    for path in glob.glob(os.path.join(KEYRING_DIR, f"{name}.*")):
        os.remove(path)
        out.ok(f"Removed {path}")
        removed = True
    if not removed:
        raise ShellyError(f"No key named '{name}' in {KEYRING_DIR}")


def add_repository(out, name, uri, suite=None, components=(), key=None, arch=None, src=False):
    if uri.startswith("ppa:"):
        if not system.have("add-apt-repository"):
            raise ShellyError("PPAs need add-apt-repository (sudo apt install software-properties-common)")
        system.run(out, ["add-apt-repository", "-y", uri])
        return
    if not NAME_RE.match(name):
        raise ShellyError(f"Invalid repository name '{name}'")
    if not suite:
        raise ShellyError("A suite is required (for example: stable, bookworm, ./)")
    path = os.path.join(SOURCES_DIR, f"{name}.sources")
    if os.path.exists(path):
        raise ShellyError(f"{path} already exists; remove it first")
    lines = [f"Types: deb{' deb-src' if src else ''}", f"URIs: {uri}", f"Suites: {suite}"]
    if components:
        lines.append("Components: " + " ".join(components))
    elif not suite.endswith("/"):
        lines.append("Components: main")
    if arch:
        lines.append(f"Architectures: {arch}")
    if key:
        key_path = add_key(out, name, key) if not key.startswith(KEYRING_DIR) else key
        lines.append(f"Signed-By: {key_path}")
    else:
        out.warn("No signing key given; APT will refuse unsigned repositories unless the key is already trusted")
    with open(path, "w") as fh:
        fh.write("# Added by shelly-apt\n" + "\n".join(lines) + "\n")
    os.chmod(path, 0o644)
    out.ok(f"Added repository {name} ({path})")


def _repo_files(name):
    hits = [p for p in glob.glob(os.path.join(SOURCES_DIR, "*"))
            if os.path.splitext(os.path.basename(p))[0] == name]
    if not hits:
        raise ShellyError(f"No repository file named '{name}' in {SOURCES_DIR}")
    return hits


def remove_repository(out, name, keep_key=False):
    for path in _repo_files(name):
        os.remove(path)
        out.ok(f"Removed {path}")
    if not keep_key:
        for path in glob.glob(os.path.join(KEYRING_DIR, f"{name}.*")):
            os.remove(path)
            out.ok(f"Removed key {path}")


def set_repository_enabled(out, name, enabled):
    for path in _repo_files(name):
        with open(path) as fh:
            text = fh.read()
        if path.endswith(".sources"):
            text = re.sub(r"(?im)^Enabled:.*\n?", "", text)
            if not enabled:
                text = re.sub(r"(?m)^(Types:)", "Enabled: no\n\\1", text)
        else:
            if enabled:
                text = re.sub(r"(?m)^#\s*(deb(-src)?\s)", r"\1", text)
            else:
                text = re.sub(r"(?m)^(deb(-src)?\s)", r"# \1", text)
        with open(path, "w") as fh:
            fh.write(text)
        out.ok(f"{'Enabled' if enabled else 'Disabled'} {path}")


# ---- conffiles (pacnew/pacsave equivalents) --------------------------------

CONFFILE_SUFFIXES = (".dpkg-new", ".dpkg-dist", ".dpkg-old", ".dpkg-bak", ".ucf-dist", ".ucf-old", ".ucf-new")


def find_conffiles(root="/etc"):
    found = []
    for dirpath, _dirs, files in os.walk(root, onerror=lambda e: None):
        for f in files:
            if f.endswith(CONFFILE_SUFFIXES):
                path = os.path.join(dirpath, f)
                base, suffix = os.path.splitext(path)
                found.append({"path": path, "kind": suffix.lstrip("."), "original": base,
                              "original_exists": os.path.exists(base)})
    return sorted(found, key=lambda r: r["path"])


def conffile_diff(entry):
    if not entry["original_exists"]:
        return f"(original {entry['original']} does not exist)\n"
    res = subprocess.run(["diff", "-u", "--color=auto", entry["original"], entry["path"]],
                         capture_output=True, text=True)
    return res.stdout or "(identical)\n"
