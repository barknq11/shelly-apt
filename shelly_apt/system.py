"""Privilege elevation and running system tools (apt-get, dpkg, flatpak)."""

import os
import shutil
import subprocess
import sys
import threading

from .term import ShellyError


def is_root():
    return os.geteuid() == 0


def have(tool):
    return shutil.which(tool) is not None


def elevation_tool(config, ui_mode):
    choice = config["Elevation"] if config else "auto"
    if choice != "auto":
        if not have(choice):
            raise ShellyError(f"Elevation tool '{choice}' is not installed")
        return choice
    # The GUI has no terminal to type a password into, so it always uses polkit.
    order = ["pkexec"] if ui_mode or not sys.stdin.isatty() else ["sudo", "run0", "pkexec"]
    for tool in order:
        if have(tool):
            return tool
    raise ShellyError("Root privileges are required, but none of sudo, run0 or pkexec is available")


def ensure_root(out, config, argv):
    """Re-execute this command as root if needed. Never returns when it re-execs."""
    if is_root():
        return
    if os.environ.get("SHELLY_ELEVATED") == "1":
        raise ShellyError("Elevation failed: still not running as root")
    tool = elevation_tool(config, out.ui_mode)
    script = os.path.realpath(sys.argv[0])
    cmd = [tool]
    if tool == "sudo":
        cmd += ["--preserve-env=SHELLY_ELEVATED,NO_COLOR"]
    cmd += [sys.executable, script, *argv]
    os.environ["SHELLY_ELEVATED"] = "1"
    if not out.ui_mode and not out.json_mode:
        print(out.dim(f"Requesting root privileges via {tool}…"), file=sys.stderr, flush=True)
    sys.stdout.flush()
    try:
        os.execvp(tool, cmd)
    except OSError as exc:
        raise ShellyError(f"Could not run {tool}: {exc}")


def noninteractive_env():
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env.setdefault("APT_LISTCHANGES_FRONTEND", "none")
    env.setdefault("NEEDRESTART_MODE", "l")
    return env


DPKG_SAFE_OPTS = [
    "-o", "Dpkg::Options::=--force-confdef",
    "-o", "Dpkg::Options::=--force-confold",
]


def run_apt(out, args, assume_yes=True):
    """Run apt-get with the given arguments, streaming progress in UI mode."""
    # Our output is block-buffered when piped; flush so it stays ahead of apt's.
    sys.stdout.flush()
    sys.stderr.flush()
    cmd = ["apt-get"]
    if assume_yes:
        cmd.append("-y")
    if out.ui_mode or out.no_confirm:
        cmd += DPKG_SAFE_OPTS
        env = noninteractive_env()
    else:
        env = None
    if not out.ui_mode:
        cmd += args
        rc = subprocess.call(cmd, env=env)
        if rc != 0:
            raise ShellyError(f"apt-get exited with status {rc}", rc)
        return

    read_fd, write_fd = os.pipe()
    cmd += ["-o", f"APT::Status-Fd={write_fd}", "-o", "Dpkg::Use-Pty=0", *args]
    proc = subprocess.Popen(
        cmd, env=env, pass_fds=(write_fd,), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    os.close(write_fd)

    def pump_status():
        with os.fdopen(read_fd, "r", errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").split(":", 3)
                if len(parts) < 4:
                    continue
                kind, item, pct, message = parts
                phase = {"dlstatus": "download", "pmstatus": "install",
                         "pmerror": "error", "pmconffile": "conffile"}.get(kind, kind)
                try:
                    percent = float(pct)
                except ValueError:
                    percent = None
                out.event("progress", phase=phase, item=item, percent=percent, message=message)

    t = threading.Thread(target=pump_status, daemon=True)
    t.start()
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
    rc = proc.wait()
    t.join(timeout=2)
    if rc != 0:
        raise ShellyError(f"apt-get exited with status {rc}", rc)


def run(out, cmd, check=True, env=None, user=False):
    """Run a command and stream its output. With user=True, drop root to the invoking user."""
    if user and is_root():
        from .config import invoking_user
        u = invoking_user()
        if u.pw_uid != 0:
            cmd = ["runuser", "-u", u.pw_name, "--", *cmd]
    sys.stdout.flush()
    sys.stderr.flush()
    rc = subprocess.call(cmd, env=env)
    if check and rc != 0:
        raise ShellyError(f"{cmd[0]} exited with status {rc}", rc)
    return rc


def capture(cmd, check=False, env=None):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    except FileNotFoundError:
        if check:
            raise ShellyError(f"{cmd[0]} is not installed")
        return 127, "", f"{cmd[0]}: not found"
    if check and res.returncode != 0:
        raise ShellyError(res.stderr.strip() or f"{cmd[0]} exited with status {res.returncode}")
    return res.returncode, res.stdout, res.stderr


def restart_report():
    """Return (reboot_needed, [packages]) based on Debian's reboot-required markers."""
    needed = os.path.exists("/run/reboot-required") or os.path.exists("/var/run/reboot-required")
    pkgs = []
    for path in ("/run/reboot-required.pkgs", "/var/run/reboot-required.pkgs"):
        try:
            with open(path) as fh:
                pkgs = sorted({ln.strip() for ln in fh if ln.strip()})
            break
        except OSError:
            continue
    return needed, pkgs
