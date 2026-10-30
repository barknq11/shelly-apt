"""Shelly configuration stored as JSON in the invoking user's XDG config dir."""

import json
import os
import pwd

DEFAULTS = {
    # Auto | Bytes | Megabytes | Gigabytes
    "FileSizeDisplay": "Megabytes",
    # full-upgrade (dist-upgrade) instead of plain upgrade for `upgrade standard`
    "UseFullUpgrade": True,
    # Install Recommends: like apt's default
    "InstallRecommends": True,
    "DisableFlatpakUpdateCheck": False,
    "DisableAppImageUpdateCheck": False,
    # Run `apt-get autoclean` after `upgrade all`
    "CleanCacheAfterUpgrade": False,
    # Remove orphaned dependencies after `upgrade all`
    "AutoRemoveAfterUpgrade": False,
    "AppImageInstallPath": "~/Applications",
    # auto | sudo | pkexec | run0
    "Elevation": "auto",
    "Colors": True,
    "FlatpakInstallation": "system",  # system | user
    "UpdateCheckIntervalHours": 6,
    "NotifyOnUpdates": True,
}

TYPES = {k: type(v) for k, v in DEFAULTS.items()}
CHOICES = {
    "FileSizeDisplay": ("Auto", "Bytes", "Megabytes", "Gigabytes"),
    "Elevation": ("auto", "sudo", "pkexec", "run0"),
    "FlatpakInstallation": ("system", "user"),
}


def invoking_user():
    """Return the pwd entry of the human user, even when running elevated."""
    for var in ("PKEXEC_UID", "SUDO_UID", "RUN0_UID"):
        uid = os.environ.get(var)
        if uid and uid.isdigit():
            try:
                return pwd.getpwuid(int(uid))
            except KeyError:
                pass
    return pwd.getpwuid(os.getuid())


def user_home():
    user = invoking_user()
    # Elevated: $HOME is root's, so use the human user's passwd entry.
    if os.getuid() == 0 and user.pw_uid != 0:
        return user.pw_dir
    return os.environ.get("HOME") or user.pw_dir


def xdg_dir(kind):
    home = user_home()
    elevated = os.getuid() == 0 and invoking_user().pw_uid != 0
    env = {
        "config": ("XDG_CONFIG_HOME", ".config"),
        "data": ("XDG_DATA_HOME", ".local/share"),
        "cache": ("XDG_CACHE_HOME", ".cache"),
    }[kind]
    base = None if elevated else os.environ.get(env[0])
    return os.path.join(base or os.path.join(home, env[1]), "shelly-apt")


def config_path():
    return os.path.join(xdg_dir("config"), "config.json")


def coerce(key, value):
    if key not in DEFAULTS:
        raise KeyError(f"Unknown configuration key '{key}'. Known keys: {', '.join(DEFAULTS)}")
    kind = TYPES[key]
    if kind is bool:
        if isinstance(value, bool):
            return value
        low = str(value).strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"'{value}' is not a boolean (use true or false)")
    if kind is int:
        return int(value)
    value = str(value)
    if key in CHOICES:
        for choice in CHOICES[key]:
            if choice.lower() == value.lower():
                return choice
        raise ValueError(f"'{value}' is not valid for {key}. Choices: {', '.join(CHOICES[key])}")
    return value


class Config:
    def __init__(self, path=None):
        self.path = path or config_path()
        self.values = dict(DEFAULTS)
        try:
            with open(self.path) as fh:
                stored = json.load(fh)
            for key, value in stored.items():
                if key in DEFAULTS:
                    try:
                        self.values[key] = coerce(key, value)
                    except (ValueError, TypeError):
                        pass
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.values[key]

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = coerce(key, value)
        self.save()

    def reset(self):
        self.values = dict(DEFAULTS)
        self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.values, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)
        # Keep files owned by the human user when written from an elevated process.
        user = invoking_user()
        if os.getuid() == 0 and user.pw_uid != 0:
            for p in (os.path.dirname(self.path), self.path):
                try:
                    os.chown(p, user.pw_uid, user.pw_gid)
                except OSError:
                    pass

    @property
    def appimage_dir(self):
        path = self.values["AppImageInstallPath"]
        if path.startswith("~"):
            path = user_home() + path[1:]
        return path
