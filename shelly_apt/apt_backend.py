"""APT/dpkg backend built on python-apt. Read-only queries and transaction planning.

Transactions themselves are executed by apt-get (see system.run_apt) so that dpkg
prompts, triggers, hooks and conffile handling behave exactly like stock Debian.
"""

import fnmatch
import os
import re
import subprocess

from .term import ShellyError

try:
    import apt
    import apt_pkg
except ImportError as exc:  # pragma: no cover - checked at runtime
    raise ShellyError("python3-apt is required (sudo apt install python3-apt)") from exc


def open_cache():
    try:
        return apt.Cache()
    except Exception as exc:  # apt raises a zoo of SystemError/LockFailedException
        raise ShellyError(f"Could not open the APT cache: {exc}")


def _origin(ver):
    if ver is None:
        return ""
    for o in ver.origins:
        if o.archive and o.archive != "now":
            # suite/component is the closest thing Debian has to an Arch repo name.
            return f"{o.archive}/{o.component}" if o.component else o.archive
    return "local"


def is_held(pkg):
    return pkg._pkg.selected_state == apt_pkg.SELSTATE_HOLD


def is_residual(pkg):
    return pkg._pkg.current_state == apt_pkg.CURSTATE_CONFIG_FILES


def is_foreign(pkg):
    """Installed, but the installed version can't be downloaded from any repository."""
    return pkg.is_installed and not pkg.installed.downloadable


def pkg_info(pkg, detail=False):
    inst, cand = pkg.installed, pkg.candidate
    ver = inst or cand
    def rel(deps):
        return [" | ".join(b.name + (f" ({b.relation} {b.version})" if b.relation else "") for b in dep.or_dependencies)
                for dep in deps if dep.or_dependencies[0].name != pkg.shortname]

    info = {
        "name": pkg.shortname if pkg.architecture() == apt_pkg.config.find("APT::Architecture") else pkg.name,
        "architecture": pkg.architecture(),
        "installed_version": inst.version if inst else None,
        "candidate_version": cand.version if cand else None,
        "summary": ver.summary if ver else "",
        "section": ver.section if ver else "",
        "repository": _origin(cand or inst),
        "installed_size": ver.installed_size if ver else 0,
        "download_size": cand.size if cand else 0,
        "installed": pkg.is_installed,
        "upgradable": pkg.is_upgradable,
        "auto": pkg.is_installed and pkg.is_auto_installed,
        "held": is_held(pkg),
        "foreign": is_foreign(pkg),
    }
    if detail and ver:
        info.update({
            "description": ver.description,
            "homepage": ver.homepage,
            "maintainer": ver.record.get("Maintainer", ""),
            "priority": ver.priority,
            "source": ver.source_name,
            "depends": rel(ver.get_dependencies("Depends", "PreDepends")),
            "recommends": rel(ver.get_dependencies("Recommends")),
            "suggests": rel(ver.get_dependencies("Suggests")),
            "conflicts": rel(ver.get_dependencies("Conflicts", "Breaks")),
            "provides": [p for p in ver.provides if p != pkg.shortname],
            "versions": [{"version": v.version, "repository": _origin(v)} for v in pkg.versions],
            "essential": pkg.essential,
        })
        if pkg.is_installed:
            try:
                files = pkg.installed_files
            except Exception:
                files = []
            info["file_count"] = len(files)
    return info


def find_package(cache, name):
    if name in cache:
        return cache[name]
    # Virtual package: pick the single provider if unambiguous.
    try:
        providers = cache.get_providing_packages(name)
    except Exception:
        providers = []
    if len(providers) == 1:
        return providers[0]
    if providers:
        raise ShellyError(f"'{name}' is a virtual package provided by: "
                          + ", ".join(sorted(p.name for p in providers)) + ". Choose one.")
    raise ShellyError(f"Package '{name}' was not found. Try 'shelly sync standard' first.")


def start_text_search(terms):
    """Start `apt-cache search` early so it runs while python-apt opens its cache.

    Reading every package's summary through python-apt costs seconds; apt-cache
    does the same scan in C++ and prints `name - summary` lines we can filter.
    """
    patterns = [re.escape(t) for t in terms if t]
    if not patterns or any(ch in t for t in terms for ch in "*?["):
        return None
    try:
        return subprocess.Popen(["apt-cache", "search", "--", *patterns],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return None


def search(cache, terms, installed_only=False, names_only=False, limit=None, proc=None):
    """Match all terms (case-insensitive) against name, then summary."""
    terms = [t.lower() for t in terms if t]
    globbing = any(ch in t for t in terms for ch in "*?[")
    name_hits, desc_hits = [], []
    if not installed_only and not names_only and not globbing:
        proc = proc or start_text_search(terms)
    else:
        if proc:
            proc.kill()
        proc = None
    if proc is not None:
        output, _ = proc.communicate()
        for line in output.splitlines():
            name, _, summary = line.partition(" - ")
            low_name, low_sum = name.lower(), summary.lower()
            if not all(t in low_name or t in low_sum for t in terms) or name not in cache:
                continue
            pkg = cache[name]
            (name_hits if all(t in low_name for t in terms) else desc_hits).append(pkg)
        # apt-cache lists only the native architecture; add foreign-arch name matches.
        for pkg in cache:
            if ":" in pkg.name and pkg.candidate is not None and all(t in pkg.name.lower() for t in terms):
                name_hits.append(pkg)
    for pkg in (cache if proc is None else ()):
        if installed_only and not pkg.is_installed:
            continue
        if not installed_only and pkg.candidate is None and not pkg.is_installed:
            continue
        name = pkg.name.lower()
        if globbing:
            if all(fnmatch.fnmatchcase(name, t) for t in terms):
                name_hits.append(pkg)
            continue
        if all(t in name for t in terms):
            name_hits.append(pkg)
            continue
        if names_only:
            continue
        ver = pkg.candidate or pkg.installed
        if ver is None:
            continue
        summary = (ver.summary or "").lower()
        if all(t in name or t in summary for t in terms):
            desc_hits.append(pkg)

    def rank(pkg):
        n = pkg.shortname.lower()
        exact = n in terms
        prefix = any(n.startswith(t) for t in terms)
        return (not exact, not prefix, len(n), n)

    name_hits.sort(key=rank)
    desc_hits.sort(key=lambda p: p.name)
    hits = name_hits + desc_hits
    return hits[:limit] if limit else hits


def list_installed(cache, mode="all"):
    pkgs = []
    for pkg in cache:
        if mode == "residual":
            if is_residual(pkg):
                pkgs.append(pkg)
            continue
        if not pkg.is_installed:
            continue
        if mode == "explicit" and pkg.is_auto_installed:
            continue
        if mode == "dependency" and not pkg.is_auto_installed:
            continue
        if mode == "held" and not is_held(pkg):
            continue
        if mode == "foreign" and not is_foreign(pkg):
            continue
        if mode == "orphans" and not pkg.is_auto_removable:
            continue
        pkgs.append(pkg)
    pkgs.sort(key=lambda p: p.name)
    return pkgs


# ---- transaction planning ------------------------------------------------

def changes_plan(cache):
    plan = {"install": [], "upgrade": [], "downgrade": [], "reinstall": [], "remove": [], "purge": []}
    for pkg in cache.get_changes():
        old = pkg.installed.version if pkg.installed else None
        new = pkg.candidate.version if pkg.candidate else None
        row = {"name": pkg.name, "old": old, "new": new,
               "repository": _origin(pkg.candidate),
               "download": pkg.candidate.size if pkg.candidate and not pkg.marked_delete else 0,
               "auto": pkg.marked_install and pkg.is_auto_installed}
        if pkg.marked_delete:
            row["new"] = None
            plan["remove"].append(row)
        elif pkg.marked_install:
            plan["install"].append(row)
        elif pkg.marked_upgrade:
            plan["upgrade"].append(row)
        elif pkg.marked_downgrade:
            plan["downgrade"].append(row)
        elif pkg.marked_reinstall:
            plan["reinstall"].append(row)
    for rows in plan.values():
        rows.sort(key=lambda r: r["name"])
    plan["download_size"] = cache.required_download
    plan["space_change"] = cache.required_space
    plan["held_back"] = []
    return plan


def plan_is_empty(plan):
    return not any(plan[k] for k in ("install", "upgrade", "downgrade", "reinstall", "remove", "purge"))


def resolve_or_fail(cache):
    if cache.broken_count:
        resolver = apt.cache.ProblemResolver(cache)
        try:
            resolver.resolve()
        except SystemError as exc:
            broken = [p.name for p in cache if p.is_inst_broken or p.is_now_broken][:10]
            raise ShellyError("Unable to resolve dependencies: " + str(exc)
                              + (f" (broken: {', '.join(broken)})" if broken else ""))


def plan_upgrade(cache, full=True):
    try:
        cache.upgrade(dist_upgrade=full)
    except SystemError as exc:
        raise ShellyError(f"Unable to compute upgrade: {exc}")
    plan = changes_plan(cache)
    plan["held_back"] = sorted(p.name for p in cache
                               if p.is_installed and p.is_upgradable and not p.marked_upgrade
                               and not p.marked_delete)
    return plan


def plan_install(cache, names, recommends=True, reinstall=False, only_upgrade=False):
    with cache.actiongroup():
        for name in names:
            pkg = find_package(cache, name)
            if only_upgrade:
                if not pkg.is_installed:
                    raise ShellyError(f"'{name}' is not installed")
                if pkg.is_upgradable:
                    pkg.mark_upgrade(from_user=True)
                continue
            if pkg.is_installed and reinstall:
                pkg.mark_reinstall()
            elif not pkg.is_installed or pkg.is_upgradable:
                if pkg.candidate is None:
                    raise ShellyError(f"'{name}' has no installation candidate")
                if not recommends:
                    apt_pkg.config.set("APT::Install-Recommends", "false")
                pkg.mark_install(auto_fix=False, from_user=True)
    resolve_or_fail(cache)
    return changes_plan(cache)


def plan_remove(cache, names, purge=False, cascade=False):
    with cache.actiongroup():
        for name in names:
            if name not in cache:
                raise ShellyError(f"Package '{name}' was not found")
            pkg = cache[name]
            if not pkg.is_installed and not (purge and is_residual(pkg)):
                raise ShellyError(f"'{name}' is not installed")
            if pkg.essential:
                raise ShellyError(f"'{name}' is an Essential package; refusing to remove it")
            pkg.mark_delete(auto_fix=False, purge=purge)
        if cascade:
            for pkg in cache:
                if pkg.is_auto_removable and not pkg.marked_delete:
                    pkg.mark_delete(purge=purge)
    resolve_or_fail(cache)
    plan = changes_plan(cache)
    if purge:
        plan["purge"], plan["remove"] = plan["remove"], []
    return plan


def upgradable(cache, full=True):
    plan = plan_upgrade(cache, full)
    rows = plan["upgrade"] + plan["install"] + plan["downgrade"]
    return rows, plan


def candidate_versions(pkg):
    return [{"version": v.version, "repository": _origin(v),
             "installed": pkg.installed is not None and v.version == pkg.installed.version,
             "downloadable": v.downloadable}
            for v in pkg.versions]


def cached_debs(name):
    """Older versions of a package still present in /var/cache/apt/archives."""
    found = []
    archive = "/var/cache/apt/archives"
    try:
        for fn in os.listdir(archive):
            if fn.startswith(name + "_") and fn.endswith(".deb"):
                ver = fn[len(name) + 1:].rsplit("_", 1)[0].replace("%3a", ":")
                found.append({"version": ver, "repository": "cache", "path": os.path.join(archive, fn)})
    except OSError:
        pass
    return found
