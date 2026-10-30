"""Command handlers. Each takes (ctx, opts, args) and returns an exit status."""

import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from . import VERSION, appimage_backend as appimg, flatpak_backend as fp, sources, spec, system
from .config import CHOICES, DEFAULTS
from .term import ShellyError

_apt = None


def apt():
    """Import the APT backend on first use (python-apt import takes a moment)."""
    global _apt
    if _apt is None:
        from . import apt_backend
        _apt = apt_backend
    return _apt


# ---- shared helpers ----------------------------------------------------------

def need_args(args, what):
    if not args:
        raise ShellyError(f"Missing {what}")


def paginate(rows, opts):
    limit = int(opts.get("--limit") or 0)
    page = int(opts.get("--page") or 1)
    if limit:
        start = (max(page, 1) - 1) * limit
        return rows[start:start + limit]
    return rows


def elevated_child(ctx, args):
    """Run another shelly command as root in a child process and return its status."""
    script = os.path.realpath(sys.argv[0])
    cmd = [sys.executable, script, *ctx.global_args(), *args]
    if not system.is_root():
        tool = system.elevation_tool(ctx.config, ctx.out.ui_mode)
        cmd = [tool] + cmd
    sys.stdout.flush()
    return subprocess.call(cmd)


def print_plan(out, plan):
    sections = [("install", "Install", out.green), ("upgrade", "Upgrade", out.cyan),
                ("downgrade", "Downgrade", out.yellow), ("reinstall", "Reinstall", out.magenta),
                ("remove", "Remove", out.red), ("purge", "Purge", out.red)]
    if out.json_mode:
        out.emit_json(plan)
        return
    for key, title, color in sections:
        rows = plan.get(key) or []
        if not rows:
            continue
        print()
        print(color(out.bold(f"{title} ({len(rows)})")))
        out.table(["Package", "Old version", "New version", "Repository", "Download"],
                  [[r["name"] + (out.dim(" (dep)") if r.get("auto") else ""), r["old"] or "", r["new"] or "",
                    r["repository"] if key not in ("remove", "purge") else "",
                    out.size(r["download"]) if r["download"] else ""] for r in rows])
    if plan.get("held_back"):
        print()
        out.warn("Held back (not upgraded): " + ", ".join(plan["held_back"]))
    print()
    print(f"{out.bold('Total download:')} {out.size(plan['download_size'])}    "
          f"{out.bold('Disk space change:')} {out.size(plan['space_change'])}")


def report_reboot(out):
    needed, pkgs = system.restart_report()
    if needed:
        out.event("reboot-required", packages=pkgs)
        out.warn("A reboot is required" + (f" (because of: {', '.join(pkgs)})" if pkgs else ""))


def downloads_dir():
    path = "/var/cache/shelly-apt/downloads"
    os.makedirs(path, exist_ok=True)
    return path


def fetch_deb(out, url):
    name = os.path.basename(urllib.parse.urlparse(url).path) or "download.deb"
    if not name.endswith(".deb"):
        name += ".deb"
    target = os.path.join(downloads_dir(), name)
    out.info(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": f"shelly-apt/{VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(target, "wb") as fh:
        while chunk := resp.read(1 << 16):
            fh.write(chunk)
    os.chmod(target, 0o644)
    return target


def info_table(out, info, keys):
    width = max(len(k) for k, _ in keys)
    for label, key in keys:
        value = info.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, bool):
            value = "yes" if value else "no"
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        print(f"{out.bold(label.ljust(width))}  {value}")


# ---- search ------------------------------------------------------------------

def search_standard(ctx, opts, args):
    out = ctx.out
    need_args(args, "search terms")
    proc = None
    if not opts.get("--detail") and not opts.get("--installed") and not opts.get("--names-only"):
        proc = apt().start_text_search(args)
    cache = apt().open_cache()
    if opts.get("--detail"):
        pkg = apt().find_package(cache, args[0])
        info = apt().pkg_info(pkg, detail=True)
        if out.json_mode:
            out.emit_json(info)
            return 0
        info["versions_text"] = [f"{v['version']} [{v['repository']}]" for v in info.get("versions", [])]
        info["installed_size_h"] = out.size(info["installed_size"])
        info["download_size_h"] = out.size(info["download_size"])
        info["reason"] = ("automatic" if info["auto"] else "manual") if info["installed"] else None
        info_table(out, info, [
            ("Name", "name"), ("Installed", "installed_version"), ("Candidate", "candidate_version"),
            ("Repository", "repository"), ("Section", "section"), ("Summary", "summary"),
            ("Homepage", "homepage"), ("Maintainer", "maintainer"), ("Source", "source"),
            ("Architecture", "architecture"), ("Installed size", "installed_size_h"),
            ("Download size", "download_size_h"), ("Install reason", "reason"), ("Held", "held"),
            ("Essential", "essential"), ("Depends", "depends"), ("Recommends", "recommends"),
            ("Suggests", "suggests"), ("Conflicts", "conflicts"), ("Provides", "provides"),
            ("Versions", "versions_text"), ("Files", "file_count"),
        ])
        if info.get("description"):
            print()
            print(info["description"])
        return 0
    hits = apt().search(cache, args, installed_only=bool(opts.get("--installed")),
                        names_only=bool(opts.get("--names-only")), proc=proc)
    hits = paginate(hits, opts)
    infos = [apt().pkg_info(p) for p in hits]
    if out.json_mode:
        out.emit_json(infos)
        return 0
    if not infos:
        out.info("No packages found")
        return 1
    out.table(["Name", "Version", "Repository", "Size", "Description"], [
        [out.bold(i["name"]),
         (out.green(i["installed_version"] + " ✓") if i["installed"] else (i["candidate_version"] or "")),
         i["repository"], out.size(i["download_size"]), i["summary"]] for i in infos],
        max_widths=[36, 30, 24, 12, None])
    print(out.dim(f"{len(infos)} result(s)"))
    return 0


def search_flatpak(ctx, opts, args):
    out = ctx.out
    need_args(args, "search terms")
    rows = paginate(fp.search(args), opts)
    if out.json_mode:
        out.emit_json(rows)
        return 0
    if not rows:
        out.info("No Flatpaks found")
        return 1
    out.table(["Name", "Application ID", "Version", "Remote", "Description"], [
        [out.bold(r["name"]) + (out.green(" ✓") if r["installed"] else ""), r["application"], r["version"],
         r["remotes"], r["description"]] for r in rows], max_widths=[30, 40, 16, 12, None])
    return 0


def search_all(ctx, opts, args):
    out = ctx.out
    need_args(args, "search terms")
    if out.json_mode:
        cache = apt().open_cache()
        std = [apt().pkg_info(p) for p in paginate(apt().search(cache, args), opts)]
        flat = paginate(fp.search(args), opts) if fp.available() else []
        out.emit_json({"standard": std, "flatpak": flat})
        return 0
    print(out.bold("APT packages"))
    search_standard(ctx, opts, args)
    if fp.available():
        print()
        print(out.bold("Flatpaks"))
        search_flatpak(ctx, opts, args)
    return 0


def interactive_search(ctx, terms):
    """`shelly <words>`: search APT and Flatpak, then install what the user picks."""
    out = ctx.out
    cache = apt().open_cache()
    std = [apt().pkg_info(p) for p in apt().search(cache, terms, limit=20)]
    flat = []
    if fp.available():
        try:
            flat = fp.search(terms)[:10]
        except ShellyError as exc:
            out.warn(f"Flatpak search failed: {exc}")
    choices = [("standard", i) for i in std] + [("flatpak", r) for r in flat]
    if not choices:
        out.info("Nothing found")
        return 1
    rows = []
    for n, (kind, item) in enumerate(choices, 1):
        if kind == "standard":
            rows.append([out.cyan(str(n)), out.dim("apt"), out.bold(item["name"]),
                         out.green("installed") if item["installed"] else (item["candidate_version"] or ""),
                         item["summary"]])
        else:
            rows.append([out.cyan(str(n)), out.dim("flatpak"), out.bold(item["name"]) + out.dim(f" ({item['application']})"),
                         out.green("installed") if item["installed"] else item["version"], item["description"]])
    out.table(["#", "Type", "Package", "Version", "Description"], rows, max_widths=[3, 7, 48, 28, None])
    picked = out.choose("Packages to install (e.g. 1 3 5-7):", len(choices))
    if not picked:
        return 0
    std_names = [choices[i][1]["name"] for i in picked if choices[i][0] == "standard"]
    flat_items = [choices[i][1] for i in picked if choices[i][0] == "flatpak"]
    rc = 0
    if std_names:
        rc = elevated_child(ctx, ["install", "standard", *std_names])
    for item in flat_items:
        remote = (item.get("remotes") or "").split(",")[0] or None
        try:
            fp.install(out, ctx.config, [item["application"]], remote=remote)
        except ShellyError as exc:
            out.error(str(exc))
            rc = 1
    return rc


# ---- install / remove / update ------------------------------------------------

def install_standard(ctx, opts, args):
    out = ctx.out
    need_args(args, "package names, .deb files or URLs")
    if opts.get("--build-deps"):
        system.run_apt(out, ["build-dep", *args])
        return 0
    debs, names = [], []
    for a in args:
        if a.startswith(("http://", "https://")):
            debs.append(fetch_deb(out, a))
        elif a.endswith(".deb"):
            if not os.path.isfile(a):
                raise ShellyError(f"{a}: no such file")
            debs.append(os.path.abspath(a))
        else:
            names.append(a)
    if opts.get("--upgrade"):
        out.info("Synchronizing package lists")
        system.run_apt(out, ["update"])

    recommends = ctx.config["InstallRecommends"] and not opts.get("--no-recommends")
    if names:
        cache = apt().open_cache()
        if opts.get("--upgrade"):
            cache.upgrade(dist_upgrade=ctx.config["UseFullUpgrade"])
        plan = apt().plan_install(cache, names, recommends=recommends, reinstall=bool(opts.get("--reinstall")))
        if apt().plan_is_empty(plan) and not debs:
            out.ok("Everything requested is already installed and up to date")
            return 0
        print_plan(out, plan)
    if debs:
        print()
        print(out.bold("Local packages:"))
        for d in debs:
            print(f"  {d}")
    if not out.confirm("Proceed with installation?"):
        out.info("Cancelled")
        return 1
    if opts.get("--upgrade"):
        system.run_apt(out, ["full-upgrade" if ctx.config["UseFullUpgrade"] else "upgrade"])
    flags = []
    if not recommends:
        flags.append("--no-install-recommends")
    if opts.get("--reinstall"):
        flags.append("--reinstall")
    if opts.get("--download-only"):
        flags.append("--download-only")
    system.run_apt(out, ["install", *flags, *names, *debs])
    out.ok("Installation complete")
    report_reboot(out)
    return 0


def remove_standard(ctx, opts, args):
    out = ctx.out
    need_args(args, "package names")
    cache = apt().open_cache()
    purge = bool(opts.get("--remove-config"))
    plan = apt().plan_remove(cache, args, purge=purge, cascade=bool(opts.get("--cascade")))
    print_plan(out, plan)
    if not out.confirm("Remove these packages?"):
        out.info("Cancelled")
        return 1
    cmd = ["purge" if purge else "remove"]
    if opts.get("--cascade"):
        cmd.append("--autoremove")
    system.run_apt(out, cmd + args)
    out.ok("Removal complete")
    return 0


def update_standard(ctx, opts, args):
    out = ctx.out
    need_args(args, "package names")
    cache = apt().open_cache()
    plan = apt().plan_install(cache, args, only_upgrade=True)
    if apt().plan_is_empty(plan):
        out.ok("The named packages are already up to date")
        return 0
    out.warn("Partial upgrades are supported by APT, but a full 'shelly upgrade standard' keeps the system consistent")
    print_plan(out, plan)
    if not out.confirm("Update these packages?"):
        return 1
    system.run_apt(out, ["install", "--only-upgrade", *args])
    out.ok("Update complete")
    return 0


def upgrade_standard(ctx, opts, args):
    out = ctx.out
    if not opts.get("--skip-sync"):
        out.info("Synchronizing package lists")
        system.run_apt(out, ["update"])
    full = ctx.config["UseFullUpgrade"] and not opts.get("--safe")
    cache = apt().open_cache()
    plan = apt().plan_upgrade(cache, full=full)
    if apt().plan_is_empty(plan):
        out.ok("The system is up to date")
        if plan["held_back"]:
            out.warn("Held back: " + ", ".join(plan["held_back"]))
    else:
        print_plan(out, plan)
        if not out.confirm("Proceed with the upgrade?"):
            out.info("Cancelled")
            return 1
        system.run_apt(out, ["full-upgrade" if full else "upgrade"])
        out.ok("Upgrade complete")
    if ctx.config["AutoRemoveAfterUpgrade"]:
        system.run_apt(out, ["autoremove"])
    if ctx.config["CleanCacheAfterUpgrade"]:
        system.run_apt(out, ["autoclean"])
    report_reboot(out)
    return 0


def downgrade_standard(ctx, opts, args):
    out = ctx.out
    need_args(args, "a package name")
    name = args[0]
    cache = apt().open_cache()
    pkg = apt().find_package(cache, name)
    if not pkg.is_installed:
        raise ShellyError(f"'{name}' is not installed")
    current = pkg.installed.version
    import functools
    import apt_pkg
    versions = [v for v in apt().candidate_versions(pkg) if v["downloadable"]]
    seen = {v["version"] for v in versions}
    versions += [v for v in apt().cached_debs(pkg.shortname) if v["version"] not in seen]
    older = [v for v in versions if apt_pkg.version_compare(v["version"], current) < 0]
    older.sort(key=functools.cmp_to_key(lambda a, b: apt_pkg.version_compare(b["version"], a["version"])))
    if len(args) > 1:
        chosen = next((v for v in versions if v["version"] == args[1]), None)
        if chosen is None:
            raise ShellyError(f"Version {args[1]} of {name} is not available")
    else:
        if not older:
            raise ShellyError(f"No older versions of {name} are available from repositories or /var/cache/apt/archives")
        print(f"{out.bold(name)} installed: {current}")
        out.table(["#", "Version", "Source"], [[str(i), v["version"], v["repository"]] for i, v in enumerate(older, 1)])
        if out.no_confirm or out.ui_mode:
            chosen = older[0]
        else:
            picked = out.choose("Version to install:", len(older), allow_multiple=False)
            if not picked:
                return 1
            chosen = older[picked[0]]
    target = chosen.get("path") or f"{pkg.shortname}={chosen['version']}"
    if not out.confirm(f"Downgrade {name} {current} → {chosen['version']}?"):
        return 1
    system.run_apt(out, ["install", "--allow-downgrades", target])
    if out.confirm(f"Hold {name} at {chosen['version']} so upgrades don't replace it?", default=False):
        system.run(out, ["apt-mark", "hold", pkg.name])
    out.ok("Downgrade complete")
    return 0


def install_flatpak(ctx, opts, args):
    need_args(args, "Flatpak application IDs or files")
    fp.install(ctx.out, ctx.config, args, remote=opts.get("--remote"), user=bool(opts.get("--user")))
    return 0


def remove_flatpak(ctx, opts, args):
    need_args(args, "Flatpak application IDs")
    fp.remove(ctx.out, args, delete_data=bool(opts.get("--remove-config")), unused=bool(opts.get("--cascade")))
    return 0


def install_appimage(ctx, opts, args):
    need_args(args, "an .AppImage file")
    appimg.install(ctx.out, ctx.config, args[0], install_path=opts.get("--install-path"), name=opts.get("--name"))
    return 0


def remove_appimage(ctx, opts, args):
    need_args(args, "an AppImage name")
    for name in args:
        appimg.remove(ctx.out, name, remove_config=bool(opts.get("--remove-config")))
    return 0


def upgrade_flatpak(ctx, opts, args):
    fp.update(ctx.out)
    return 0


def update_flatpak(ctx, opts, args):
    need_args(args, "Flatpak application IDs")
    fp.update(ctx.out, args)
    return 0


def upgrade_appimage(ctx, opts, args):
    appimg.upgrade(ctx.out, ctx.config, args or None)
    return 0


def upgrade_all(ctx, opts, args):
    out = ctx.out
    results = []
    out.info("Upgrading APT packages")
    rc = elevated_child(ctx, ["upgrade", "standard"])
    results.append(("APT", rc == 0))
    if fp.available() and not ctx.config["DisableFlatpakUpdateCheck"]:
        out.info("Upgrading Flatpaks")
        try:
            fp.update(out)
            results.append(("Flatpak", True))
        except ShellyError as exc:
            out.error(str(exc))
            results.append(("Flatpak", False))
    if not ctx.config["DisableAppImageUpdateCheck"] and appimg.load_db():
        out.info("Upgrading AppImages")
        try:
            appimg.upgrade(out, ctx.config)
            results.append(("AppImage", True))
        except Exception as exc:
            out.error(str(exc))
            results.append(("AppImage", False))
    print()
    for name, ok in results:
        print(f"  {out.green('✓') if ok else out.red('✗')} {name}")
    return 0 if all(ok for _, ok in results) else 1


# ---- list / list-updates -------------------------------------------------------

def list_standard(ctx, opts, args):
    out = ctx.out
    mode = "all"
    for flag, m in (("--explicit", "explicit"), ("--dependency", "dependency"), ("--held", "held"),
                    ("--foreign", "foreign"), ("--orphans", "orphans"), ("--residual", "residual")):
        if opts.get(flag):
            mode = m
    cache = apt().open_cache()
    pkgs = apt().list_installed(cache, mode)
    if args:
        needle = args[0].lower()
        pkgs = [p for p in pkgs if needle in p.name.lower()]
    infos = [apt().pkg_info(p) for p in pkgs]
    if out.json_mode:
        out.emit_json(infos)
        return 0
    total = 0
    rows = []
    for i in infos:
        total += i["installed_size"] or 0
        flags = []
        if i["held"]:
            flags.append(out.yellow("held"))
        if i["foreign"] and mode != "residual":
            flags.append(out.magenta("local"))
        rows.append([out.bold(i["name"]), i["installed_version"] or out.dim("config only"),
                     "auto" if i["auto"] else "manual", out.size(i["installed_size"]),
                     " ".join(flags), i["summary"]])
    out.table(["Name", "Version", "Reason", "Size", "", "Description"], rows, max_widths=[36, 28, 6, 12, 10, None])
    print(out.dim(f"{len(infos)} package(s), {out.size(total)} installed"))
    return 0


def list_flatpak(ctx, opts, args):
    out = ctx.out
    if opts.get("--remotes"):
        rows = fp.remotes()
        if out.json_mode:
            out.emit_json(rows)
        else:
            out.table(["Name", "Title", "URL", "Options"], [[r["name"], r["title"], r["url"], r["options"]] for r in rows])
        return 0
    rows = fp.list_installed(include_runtimes=bool(opts.get("--runtimes")))
    if args:
        needle = args[0].lower()
        rows = [r for r in rows if needle in r["name"].lower() or needle in r["application"].lower()]
    if out.json_mode:
        out.emit_json(rows)
        return 0
    out.table(["Name", "Application ID", "Version", "Branch", "Origin", "Scope", "Size"], [
        [out.bold(r["name"]), r["application"], r["version"], r["branch"], r["origin"], r["installation"],
         out.size(r["installed_size"])] for r in rows])
    print(out.dim(f"{len(rows)} Flatpak(s)"))
    return 0


def list_appimage(ctx, opts, args):
    out = ctx.out
    db = appimg.load_db()
    rows = [dict(slug=k, **v) for k, v in sorted(db.items())]
    if args:
        rows = [r for r in rows if args[0].lower() in r["slug"]]
    if out.json_mode:
        out.emit_json(rows)
        return 0
    if not rows:
        out.info("No AppImages installed. Install one with 'shelly install appimage <file>'.")
        return 0
    out.table(["Name", "Version", "Size", "Update source", "Path"], [
        [out.bold(r["slug"]), r.get("version", ""), out.size(r.get("size")),
         f"{r['update']['type']}:{r['update']['source']}" if r.get("update") else out.dim("none"), r["path"]] for r in rows])
    return 0


def _updates_standard():
    cache = apt().open_cache()
    rows, _plan = apt().upgradable(cache)
    return rows


def list_updates_standard(ctx, opts, args):
    out = ctx.out
    rows = _updates_standard()
    if out.json_mode:
        out.emit_json(rows)
        return 0
    if not rows:
        out.ok("No APT updates available (run 'shelly sync standard' to refresh)")
        return 0
    out.table(["Package", "Installed", "Available", "Repository", "Download"],
              [[out.bold(r["name"]), r["old"] or out.dim("new"), out.green(r["new"]), r["repository"], out.size(r["download"])]
               for r in rows])
    print(out.dim(f"{len(rows)} update(s)"))
    return 0


def list_updates_flatpak(ctx, opts, args):
    out = ctx.out
    rows = fp.list_updates()
    if out.json_mode:
        out.emit_json(rows)
        return 0
    if not rows:
        out.ok("No Flatpak updates available")
        return 0
    out.table(["Name", "Application ID", "Version", "Branch", "Scope"],
              [[r["name"], r["application"], r["version"], r["branch"], r["installation"]] for r in rows])
    return 0


def list_updates_appimage(ctx, opts, args):
    out = ctx.out
    rows = appimg.list_updates(out)
    if out.json_mode:
        out.emit_json(rows)
        return 0
    if not rows:
        out.ok("No AppImage updates available")
        return 0
    out.table(["Name", "Installed", "Available"], [[r["name"], r["old"], out.green(r["new"])] for r in rows])
    return 0


def list_updates_all(ctx, opts, args):
    out = ctx.out
    result = {"standard": [], "flatpak": [], "appimage": []}
    try:
        result["standard"] = _updates_standard()
    except ShellyError as exc:
        out.warn(f"APT: {exc}")
    if fp.available() and not ctx.config["DisableFlatpakUpdateCheck"]:
        try:
            result["flatpak"] = fp.list_updates()
        except ShellyError as exc:
            out.warn(f"Flatpak: {exc}")
    if not ctx.config["DisableAppImageUpdateCheck"]:
        result["appimage"] = appimg.list_updates(out)
    if out.json_mode:
        out.emit_json(result)
        return 0
    rows = [["apt", r["name"], r["old"] or "", r["new"]] for r in result["standard"]]
    rows += [["flatpak", r["name"] or r["application"], "", r["version"]] for r in result["flatpak"]]
    rows += [["appimage", r["name"], r["old"], r["new"]] for r in result["appimage"]]
    if not rows:
        out.ok("Everything is up to date")
        return 0
    out.table(["Type", "Name", "Installed", "Available"], rows)
    print(out.dim(f"{len(rows)} update(s)"))
    return 0


# ---- sync --------------------------------------------------------------------

def sync_standard(ctx, opts, args):
    system.run_apt(ctx.out, ["update"], assume_yes=False)
    ctx.out.ok("Package lists synchronized")
    return 0


def sync_flatpak(ctx, opts, args):
    out = ctx.out
    if args and args[0] == "add":
        if len(args) < 3:
            raise ShellyError("Usage: shelly sync flatpak add <name> <url>")
        fp.remote_add(out, args[1], args[2], user=bool(opts.get("--user")))
    elif args and args[0] == "remove":
        need_args(args[1:], "a remote name")
        fp.remote_delete(out, args[1], user=bool(opts.get("--user")))
    else:
        fp.sync(out)
    return 0


def sync_appimage(ctx, opts, args):
    if opts.get("--configure-updates"):
        if len(args) < 3:
            raise ShellyError("Usage: shelly sync appimage --configure-updates <name> github|url <source>")
        appimg.configure_updates(ctx.out, args[0], args[1], args[2], opts.get("--pattern"))
    else:
        appimg.sync(ctx.out, ctx.config)
    return 0


# ---- mark --------------------------------------------------------------------

def _apt_mark(verb):
    def handler(ctx, opts, args):
        need_args(args, "package names")
        system.run(ctx.out, ["apt-mark", verb, *args])
        return 0
    return handler


def mark_list(ctx, opts, args):
    _rc, stdout, _ = system.capture(["apt-mark", "showhold"], check=True)
    held = stdout.split()
    if ctx.out.json_mode:
        ctx.out.emit_json(held)
    elif held:
        print("\n".join(held))
    else:
        ctx.out.info("No packages are held")
    return 0


# ---- purify ------------------------------------------------------------------

def purify_standard(ctx, opts, args):
    out = ctx.out
    _rc, audit, _ = system.capture(["dpkg", "--audit"])
    if audit.strip():
        print(audit)
        if out.confirm("dpkg reports problems. Try to repair them (dpkg --configure -a; apt-get install -f)?"):
            system.run(out, ["dpkg", "--configure", "-a"], check=False)
            system.run_apt(out, ["install", "-f"])
    else:
        out.ok("No broken or half-installed packages")

    cache = apt().open_cache()
    if not opts.get("--keep-orphans"):
        orphans = apt().list_installed(cache, "orphans")
        if orphans:
            out.info(f"{len(orphans)} orphaned dependenc{'y' if len(orphans) == 1 else 'ies'}")
            out.table(["Package", "Version", "Size"],
                      [[p.name, p.installed.version, out.size(p.installed.installed_size)] for p in orphans])
            if out.confirm("Remove orphaned dependencies?"):
                system.run_apt(out, ["autoremove"])
        else:
            out.ok("No orphaned dependencies")
    if opts.get("--residual"):
        residual = [p.name for p in apt().list_installed(cache, "residual")]
        if residual:
            out.info(f"Leftover configuration from {len(residual)} removed package(s): {', '.join(residual)}")
            if out.confirm("Purge leftover configuration?"):
                system.run_apt(out, ["purge", *residual])
        else:
            out.ok("No leftover configuration files")
    if opts.get("--cache") or opts.get("--autoclean"):
        size = 0
        for root, _d, files in os.walk("/var/cache/apt/archives"):
            size += sum(os.path.getsize(os.path.join(root, f)) for f in files if f.endswith(".deb"))
        verb = "clean" if opts.get("--cache") else "autoclean"
        if out.confirm(f"Run apt-get {verb}? (cache currently holds {out.size(size)})"):
            system.run_apt(out, [verb], assume_yes=False)
    return 0


def purify_flatpak(ctx, opts, args):
    fp.purify(ctx.out)
    return 0


# ---- backup ------------------------------------------------------------------

def _toml_str(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def backup(ctx, opts, args):
    out = ctx.out
    if opts.get("--restore"):
        return restore(ctx, opts["--restore"])
    cache = apt().open_cache()
    manual = [p.name for p in apt().list_installed(cache, "explicit")]
    lines = [f"# Shelly-APT backup, {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"# Restore with: shelly backup --restore <file>", "", "[standard]", "packages = ["]
    lines += [f"  {_toml_str(n)}," for n in manual] + ["]", ""]
    if fp.available():
        apps = fp.list_installed()
        lines += ["[flatpak]", "apps = ["]
        lines += [f"  {{ id = {_toml_str(a['application'])}, origin = {_toml_str(a['origin'])}, "
                  f"installation = {_toml_str(a['installation'])} }}," for a in apps]
        lines += ["]", ""]
    db = appimg.load_db()
    if db:
        lines += ["[appimage]", "apps = ["]
        for slug, e in sorted(db.items()):
            upd = e.get("update") or {}
            lines.append(f"  {{ name = {_toml_str(slug)}, update_type = {_toml_str(upd.get('type', ''))}, "
                         f"update_source = {_toml_str(upd.get('source', ''))} }},")
        lines += ["]", ""]
    text = "\n".join(lines)
    if opts.get("--output"):
        with open(opts["--output"], "w") as fh:
            fh.write(text)
        out.ok(f"Backed up {len(manual)} APT package(s) to {opts['--output']}")
    else:
        print(text)
    return 0


def restore(ctx, path):
    out = ctx.out
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        raise ShellyError("Restoring needs Python 3.11+ (tomllib)")
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    rc = 0
    wanted = data.get("standard", {}).get("packages", [])
    if wanted:
        cache = apt().open_cache()
        missing = [n for n in wanted if n in cache and not cache[n].is_installed]
        unknown = [n for n in wanted if n not in cache]
        if unknown:
            out.warn("Not available from current repositories: " + ", ".join(unknown))
        if missing:
            rc |= elevated_child(ctx, ["install", "standard", *missing])
        else:
            out.ok("All APT packages from the backup are installed")
    apps = data.get("flatpak", {}).get("apps", [])
    if apps and fp.available():
        installed = {a["application"] for a in fp.list_installed(include_runtimes=True)}
        for app in apps:
            if app["id"] not in installed:
                try:
                    fp.install(out, ctx.config, [app["id"]], remote=app.get("origin"),
                               user=app.get("installation") == "user")
                except ShellyError as exc:
                    out.error(str(exc))
                    rc = 1
    for app in data.get("appimage", {}).get("apps", []):
        if app.get("update_type") == "github" and app["name"] not in appimg.load_db():
            try:
                found = appimg.check_update({"version": "", "update": {"type": "github", "source": app["update_source"]}})
                if found:
                    import tempfile
                    tmp = os.path.join(tempfile.mkdtemp(), f"{app['name']}.AppImage")
                    urllib.request.urlretrieve(found["url"], tmp)
                    slug = appimg.install(out, ctx.config, tmp, name=app["name"])
                    appimg.configure_updates(out, slug, "github", app["update_source"])
            except Exception as exc:
                out.error(f"{app['name']}: {exc}")
                rc = 1
        elif app["name"] not in appimg.load_db():
            out.warn(f"AppImage {app['name']} has no GitHub update source; reinstall it manually")
    return rc


# ---- utility -------------------------------------------------------------------

def utility_repository(ctx, opts, args):
    out = ctx.out
    op = args[0] if args else "list"
    if op == "list":
        rows = sources.list_repositories()
        if out.json_mode:
            out.emit_json(rows)
            return 0
        out.table(["", "Type", "URI", "Suite", "Components", "File"], [
            [out.green("●") if r["enabled"] else out.dim("○"), r["types"], r["uris"], r["suites"], r["components"],
             os.path.basename(r["file"])] for r in rows])
        return 0
    if op == "add":
        if len(args) < 2:
            raise ShellyError("Usage: shelly utility repository add <name> <uri> <suite> [components…] | add ppa:user/name")
        if args[1].startswith("ppa:"):
            sources.add_repository(out, "", args[1])
        else:
            if len(args) < 4:
                raise ShellyError("Usage: shelly utility repository add <name> <uri> <suite> [components…]")
            sources.add_repository(out, args[1], args[2], args[3], args[4:], key=opts.get("--key"),
                                   arch=opts.get("--arch"), src=bool(opts.get("--source")))
    elif op == "remove":
        need_args(args[1:], "a repository name")
        sources.remove_repository(out, args[1])
    elif op in ("enable", "disable"):
        need_args(args[1:], "a repository name")
        sources.set_repository_enabled(out, args[1], op == "enable")
    else:
        raise ShellyError(f"Unknown repository operation '{op}' (list, add, remove, enable, disable)")
    if opts.get("--refresh"):
        system.run_apt(out, ["update"], assume_yes=False)
    return 0


def utility_keyring(ctx, opts, args):
    out = ctx.out
    op = args[0] if args else "list"
    if op == "list":
        keys = sources.list_keys()
        if out.json_mode:
            out.emit_json(keys)
            return 0
        out.table(["Fingerprint", "User ID", "Expires", "File"],
                  [[k["fingerprint"][-16:], k["uid"],
                    time.strftime("%Y-%m-%d", time.gmtime(int(k["expires"]))) if k["expires"].isdigit() else "never",
                    k["file"]] for k in keys])
    elif op == "add":
        if len(args) < 3:
            raise ShellyError("Usage: shelly utility keyring add <name> <file|url>")
        sources.add_key(out, args[1], args[2])
    elif op == "remove":
        need_args(args[1:], "a key name")
        sources.remove_key(out, args[1])
    else:
        raise ShellyError(f"Unknown keyring operation '{op}' (list, add, remove)")
    return 0


def utility_conffiles(ctx, opts, args):
    out = ctx.out
    found = sources.find_conffiles()
    if out.json_mode:
        out.emit_json(found)
        return 0
    if not found:
        out.ok("No pending configuration files")
        return 0
    out.table(["Kind", "File", "Active file"], [[f["kind"], f["path"], f["original"] if f["original_exists"] else out.dim("(missing)")]
                                                for f in found])
    if opts.get("--diff"):
        for f in found:
            print()
            print(out.bold(f["path"]))
            print(sources.conffile_diff(f), end="")
    return 0


def utility_reboot(ctx, opts, args):
    needed, pkgs = system.restart_report()
    if ctx.out.json_mode:
        ctx.out.emit_json({"reboot_required": needed, "packages": pkgs})
    elif needed:
        ctx.out.warn("Reboot required" + (f" by: {', '.join(pkgs)}" if pkgs else ""))
    else:
        ctx.out.ok("No reboot required")
    return 1 if needed else 0


def utility_completions(ctx, opts, args):
    shell = args[0] if args else "bash"
    actions = list(spec.ACTIONS)
    if shell == "bash":
        cases = "\n".join(
            f"        {name}) types=\"{' '.join(v for v in a.variants if v)}\" ;;" for name, a in spec.ACTIONS.items())
        print(f"""# bash completion for shelly (shelly-apt)
_shelly() {{
    local cur prev types
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    if [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "{' '.join(actions)} --help --version" -- "$cur") )
        return
    fi
    if [[ $COMP_CWORD -eq 2 ]]; then
        case "${{COMP_WORDS[1]}}" in
{cases}
        esac
        COMPREPLY=( $(compgen -W "$types" -- "$cur") )
        return
    fi
    if [[ "${{COMP_WORDS[2]}}" == standard && "$cur" != -* ]]; then
        COMPREPLY=( $(apt-cache --no-generate pkgnames "$cur" 2>/dev/null | head -200) )
    elif [[ "${{COMP_WORDS[2]}}" == appimage ]]; then
        COMPREPLY=( $(compgen -f -- "$cur") )
    fi
}}
complete -F _shelly shelly""")
    elif shell == "fish":
        print("# fish completion for shelly (shelly-apt)")
        print(f"complete -c shelly -f -n '__fish_use_subcommand' -a '{' '.join(actions)}'")
        for name, a in spec.ACTIONS.items():
            types = " ".join(v for v in a.variants if v)
            if types:
                print(f"complete -c shelly -f -n '__fish_seen_subcommand_from {name}' -a '{types}'")
    elif shell == "zsh":
        print("#compdef shelly")
        print("_shelly() {")
        print("  local -a actions")
        print(f"  actions=({' '.join(actions)})")
        print("  if (( CURRENT == 2 )); then _describe 'action' actions; return; fi")
        print("  case $words[2] in")
        for name, a in spec.ACTIONS.items():
            print(f"    {name}) (( CURRENT == 3 )) && compadd {' '.join(v for v in a.variants if v)} ;;")
        print("  esac")
        print("}")
        print("_shelly")
    else:
        raise ShellyError("Supported shells: bash, zsh, fish")
    return 0


# ---- config --------------------------------------------------------------------

def config_list(ctx, opts, args):
    out = ctx.out
    if out.json_mode:
        out.emit_json(ctx.config.values)
        return 0
    rows = []
    for key in DEFAULTS:
        val = ctx.config[key]
        rows.append([out.bold(key), str(val), out.dim("default") if val == DEFAULTS[key] else out.cyan("custom"),
                     "|".join(CHOICES[key]) if key in CHOICES else type(DEFAULTS[key]).__name__])
    out.table(["Key", "Value", "", "Type"], rows)
    print(out.dim(f"File: {ctx.config.path}"))
    return 0


def config_get(ctx, opts, args):
    need_args(args, "a key")
    key = _key(args[0])
    print(ctx.config[key])
    return 0


def config_set(ctx, opts, args):
    if len(args) < 2:
        raise ShellyError("Usage: shelly config set <key> <value>")
    key = _key(args[0])
    try:
        ctx.config.set(key, args[1])
    except ValueError as exc:
        raise ShellyError(str(exc))
    ctx.out.ok(f"{key} = {ctx.config[key]}")
    return 0


def config_reset(ctx, opts, args):
    if ctx.out.confirm("Reset every setting to its default?"):
        ctx.config.reset()
        ctx.out.ok("Configuration reset")
    return 0


def _key(name):
    for k in DEFAULTS:
        if k.lower() == name.lower():
            return k
    raise ShellyError(f"Unknown key '{name}'. Known keys: {', '.join(DEFAULTS)}")


# ---- run -----------------------------------------------------------------------

def run_flatpak(ctx, opts, args):
    need_args(args, "an application ID")
    if args[0] == "list":
        rows = fp.ps()
        if ctx.out.json_mode:
            ctx.out.emit_json(rows)
        else:
            ctx.out.table(["Instance", "Application", "PID"], [[r["instance"], r["application"], r["pid"]] for r in rows])
        return 0
    fp.run_app(ctx.out, args[0], kill=bool(opts.get("--kill")))
    return 0


def run_appimage(ctx, opts, args):
    need_args(args, "an AppImage name")
    appimg.run_app(ctx.out, args[0], kill=bool(opts.get("--kill")))
    return 0


# ---- dispatch ------------------------------------------------------------------

HANDLERS = {
    ("search", "standard"): search_standard,
    ("search", "flatpak"): search_flatpak,
    ("search", "all"): search_all,
    ("install", "standard"): install_standard,
    ("install", "flatpak"): install_flatpak,
    ("install", "appimage"): install_appimage,
    ("remove", "standard"): remove_standard,
    ("remove", "flatpak"): remove_flatpak,
    ("remove", "appimage"): remove_appimage,
    ("upgrade", "standard"): upgrade_standard,
    ("upgrade", "flatpak"): upgrade_flatpak,
    ("upgrade", "appimage"): upgrade_appimage,
    ("upgrade", "all"): upgrade_all,
    ("update", "standard"): update_standard,
    ("update", "flatpak"): update_flatpak,
    ("update", "appimage"): upgrade_appimage,
    ("list", "standard"): list_standard,
    ("list", "flatpak"): list_flatpak,
    ("list", "appimage"): list_appimage,
    ("list-updates", "standard"): list_updates_standard,
    ("list-updates", "flatpak"): list_updates_flatpak,
    ("list-updates", "appimage"): list_updates_appimage,
    ("list-updates", "all"): list_updates_all,
    ("sync", "standard"): sync_standard,
    ("sync", "flatpak"): sync_flatpak,
    ("sync", "appimage"): sync_appimage,
    ("downgrade", "standard"): downgrade_standard,
    ("mark", "hold"): _apt_mark("hold"),
    ("mark", "unhold"): _apt_mark("unhold"),
    ("mark", "explicit"): _apt_mark("manual"),
    ("mark", "dependency"): _apt_mark("auto"),
    ("mark", "list"): mark_list,
    ("purify", "standard"): purify_standard,
    ("purify", "flatpak"): purify_flatpak,
    ("backup", ""): backup,
    ("utility", "repository"): utility_repository,
    ("utility", "keyring"): utility_keyring,
    ("utility", "conffiles"): utility_conffiles,
    ("utility", "completions"): utility_completions,
    ("utility", "reboot"): utility_reboot,
    ("config", "list"): config_list,
    ("config", "get"): config_get,
    ("config", "set"): config_set,
    ("config", "reset"): config_reset,
    ("run", "flatpak"): run_flatpak,
    ("run", "appimage"): run_appimage,
}


def handler(action, variant):
    return HANDLERS[(action, variant)]


def needs_root(action, variant, opts, args):
    if action in ("utility",) and (not args or args[0] == "list"):
        return False
    return True
