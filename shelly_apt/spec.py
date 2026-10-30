"""Command catalog: actions, types, options and shortcodes.

Mirrors Shelly's grammar:  shelly <action> <type> [options] [args]
and its shortcodes:        shelly -<ActionCode><typecode><modifiers> [args]
e.g. `shelly -Is htop` == `shelly install standard htop`,
     `shelly -Rscp foo` == `shelly remove standard --cascade --remove-config foo`.
"""

from dataclasses import dataclass, field


@dataclass
class Opt:
    name: str
    short: str = None
    help: str = ""
    value: str = None  # metavar when the option takes a value


@dataclass
class Variant:
    name: str
    code: str
    help: str
    usage: str = ""
    opts: list = field(default_factory=list)
    root: bool = False  # needs root (re-executes through sudo/pkexec)


@dataclass
class Action:
    name: str
    code: str
    help: str
    variants: dict
    bare: bool = False  # `shelly <action>` works without a type


TYPES = {
    "standard": ("s", "APT/dpkg packages from configured Debian repositories, local .deb files and URLs"),
    "flatpak": ("f", "Flatpak applications and runtimes"),
    "appimage": ("i", "AppImage applications"),
    "all": ("x", "Every supported backend"),
}

GLOBAL_OPTS = [
    Opt("--help", "-h", "Show help for the command"),
    Opt("--version", "-V", "Show version information"),
    Opt("--no-confirm", "-n", "Use safe automatic answers instead of prompting"),
    Opt("--ui-mode", "-U", "Emit framed progress output for the Shelly UI"),
    Opt("--json", "-j", "Output structured JSON where supported"),
]


def V(name, help, usage="", opts=(), root=False, code=None):
    if code is None:
        code = TYPES[name][0] if name in TYPES else name[:1]
    return Variant(name, code, help, usage, list(opts), root)


LIMIT = [Opt("--limit", "-l", "Maximum number of results", "N"), Opt("--page", "-p", "Results page (with --limit)", "N")]

ACTIONS = {a.name: a for a in [
    Action("search", "S", "Search Debian repositories or Flatpak remotes.", {
        "standard": V("standard", "Search available APT packages by name and summary. Use --installed for installed packages or --detail for full metadata of one package.",
                      "<terms…>", [Opt("--installed", "-i", "Only search installed packages"),
                                   Opt("--names-only", "-o", "Match package names only"),
                                   Opt("--detail", "-d", "Show complete metadata for one exact package")] + LIMIT),
        "flatpak": V("flatpak", "Search the AppStream catalogs of every configured Flatpak remote.", "<terms…>", LIMIT),
        "all": V("all", "Search APT repositories and Flatpak remotes together.", "<terms…>", LIMIT),
    }),
    Action("install", "I", "Install packages, .deb files, Flatpaks or AppImages.", {
        "standard": V("standard", "Install APT packages, local .deb files or .deb files downloaded from HTTP(S) URLs.",
                      "<packages|file.deb|url…>",
                      [Opt("--no-recommends", "-r", "Do not install recommended packages"),
                       Opt("--reinstall", "-e", "Reinstall packages that are already installed"),
                       Opt("--upgrade", "-u", "Synchronize and fully upgrade the system in the same run"),
                       Opt("--build-deps", "-b", "Install the build dependencies of the named source packages"),
                       Opt("--download-only", "-w", "Only download the packages into the APT cache")], root=True),
        "flatpak": V("flatpak", "Install a Flatpak by application ID, a .flatpakref file/URL or a .flatpak bundle.",
                     "<app-id|ref|file…>", [Opt("--remote", "-r", "Remote to install from (default: ask Flatpak)", "NAME"),
                                            Opt("--user", "-u", "Install into the per-user installation")]),
        "appimage": V("appimage", "Install a local AppImage into the AppImage directory and create a desktop entry.",
                      "<file.AppImage>", [Opt("--install-path", None, "Directory to install into", "DIR"),
                                         Opt("--name", None, "Name to register the AppImage under", "NAME")]),
    }),
    Action("remove", "R", "Remove packages, Flatpaks or AppImages.", {
        "standard": V("standard", "Remove installed APT packages.", "<packages…>",
                      [Opt("--cascade", "-c", "Also remove dependencies that are no longer needed (autoremove)"),
                       Opt("--remove-config", "-p", "Purge configuration files as well")], root=True),
        "flatpak": V("flatpak", "Remove an installed Flatpak application or runtime.", "<app-id…>",
                     [Opt("--remove-config", "-p", "Delete the application's data in ~/.var/app"),
                      Opt("--cascade", "-c", "Also remove runtimes that are no longer used")]),
        "appimage": V("appimage", "Remove an installed AppImage and its desktop entry.", "<name>",
                      [Opt("--remove-config", "-p", "Also delete ~/.config/<name>")]),
    }),
    Action("upgrade", "U", "Upgrade everything from one backend, or all of them together.", {
        "standard": V("standard", "Synchronize APT lists, show the upgrade plan, perform a full upgrade and report required restarts.",
                      "", [Opt("--skip-sync", "-k", "Do not run apt update first"),
                           Opt("--safe", "-s", "Use 'apt upgrade' (never removes packages) instead of full-upgrade")], root=True),
        "flatpak": V("flatpak", "Upgrade every Flatpak application and runtime.", ""),
        "appimage": V("appimage", "Check configured update sources and replace outdated AppImages.", ""),
        "all": V("all", "Upgrade APT packages, Flatpaks and AppImages in one coordinated run, continuing through backend failures.", ""),
    }),
    Action("update", "E", "Update only the named packages.", {
        "standard": V("standard", "Upgrade only the named installed APT packages (apt install --only-upgrade).", "<packages…>", root=True),
        "flatpak": V("flatpak", "Update only the named Flatpaks.", "<app-id…>"),
        "appimage": V("appimage", "Update only the named AppImages.", "<name…>"),
    }),
    Action("list", "L", "List installed packages.", {
        "standard": V("standard", "List installed APT packages, optionally filtered by install reason or state.", "[filter]",
                      [Opt("--explicit", "-e", "Only manually installed packages"),
                       Opt("--dependency", "-d", "Only automatically installed packages"),
                       Opt("--held", "-o", "Only held packages"),
                       Opt("--foreign", "-f", "Only packages not available from any repository (local/obsolete)"),
                       Opt("--orphans", "-a", "Only automatically installed packages nothing depends on"),
                       Opt("--residual", "-r", "Removed packages that still have configuration files")]),
        "flatpak": V("flatpak", "List installed Flatpaks, or remotes with --remotes.", "[filter]",
                     [Opt("--runtimes", "-r", "Include runtimes"), Opt("--remotes", "-m", "List configured remotes")]),
        "appimage": V("appimage", "List installed AppImages.", "[filter]"),
    }),
    Action("list-updates", "P", "List available updates.", {
        "standard": V("standard", "List upgradable APT packages (from the last 'sync standard').", ""),
        "flatpak": V("flatpak", "List Flatpaks with available updates.", ""),
        "appimage": V("appimage", "List AppImages whose update source has a newer build.", ""),
        "all": V("all", "List updates from every backend.", ""),
    }),
    Action("sync", "Y", "Refresh package metadata.", {
        "standard": V("standard", "Refresh APT package lists (apt update).", "", root=True),
        "flatpak": V("flatpak", "Refresh Flatpak AppStream data, or add/remove remotes.", "[add <name> <url> | remove <name>]",
                     [Opt("--user", "-u", "Act on the per-user installation")]),
        "appimage": V("appimage", "Reconcile AppImage metadata with the AppImage directory, or configure an update source.",
                      "[<name> github <owner/repo> | <name> url <url>]",
                      [Opt("--configure-updates", "-c", "Configure an AppImage's update source"),
                       Opt("--pattern", None, "Regex selecting the right release asset", "REGEX")]),
    }),
    Action("downgrade", "D", "Install an older version of a package.", {
        "standard": V("standard", "Pick an older version from the repositories or the APT cache and install it.", "<package> [version]", root=True),
    }),
    Action("mark", "M", "Hold packages or change their install reason.", {
        "hold": V("hold", "Hold packages at their current version (apt-mark hold).", "<packages…>", root=True, code="o"),
        "unhold": V("unhold", "Release held packages.", "<packages…>", root=True, code="u"),
        "explicit": V("explicit", "Mark packages as manually installed.", "<packages…>", root=True, code="e"),
        "dependency": V("dependency", "Mark packages as automatically installed.", "<packages…>", root=True, code="d"),
        "list": V("list", "List held packages.", "", code="l"),
    }),
    Action("purify", "Z", "Clean up orphans, caches, leftover configs and broken state.", {
        "standard": V("standard", "Fix broken installs and remove orphaned dependencies; optionally clean caches and leftover configs.", "",
                      [Opt("--cache", "-c", "Delete every cached .deb (apt clean)"),
                       Opt("--autoclean", "-a", "Delete cached .debs that can no longer be downloaded"),
                       Opt("--residual", "-r", "Purge configuration left by removed packages"),
                       Opt("--keep-orphans", "-k", "Do not remove orphaned dependencies")], root=True),
        "flatpak": V("flatpak", "Remove unused Flatpak runtimes and extensions.", ""),
    }),
    Action("backup", "B", "Back up (or restore) explicitly installed packages as TOML.", {
        "": V("", "Write manually installed APT packages, Flatpaks and AppImage sources as TOML.", "",
              [Opt("--output", "-o", "Write to a file instead of standard output", "FILE"),
               Opt("--restore", "-r", "Install everything listed in a backup file", "FILE")], code=""),
    }, bare=True),
    Action("utility", "T", "Repositories, signing keys, conffiles, completions and restart checks.", {
        "repository": V("repository", "Manage APT repositories.",
                        "list | add <name> <uri> <suite> [components…] | add ppa:<user>/<ppa> | remove <name> | enable <name> | disable <name>",
                        [Opt("--key", "-k", "Signing key file or URL for 'add'", "KEY"),
                         Opt("--arch", None, "Restrict 'add' to an architecture", "ARCH"),
                         Opt("--source", "-s", "Also enable deb-src for 'add'"),
                         Opt("--refresh", "-y", "Run apt update afterwards")], root=True, code="r"),
        "keyring": V("keyring", "Manage APT signing keys.", "list | add <name> <file|url> | remove <name>", root=True, code="k"),
        "conffiles": V("conffiles", "Find .dpkg-new/.dpkg-dist/.dpkg-old/.ucf-* files left by upgrades (the pacnew equivalent).", "",
                       [Opt("--diff", "-d", "Show a diff against the active file")], code="c"),
        "completions": V("completions", "Print a shell completion script.", "bash|zsh|fish", code="m"),
        "reboot": V("reboot", "Report whether a reboot is required and which packages caused it.", "", code="b"),
    }),
    Action("config", "C", "Read and modify Shelly configuration.", {
        "list": V("list", "List every configuration value.", "", code="l"),
        "get": V("get", "Read a configuration value.", "<key>", code="g"),
        "set": V("set", "Set a configuration value.", "<key> <value>", code="s"),
        "reset": V("reset", "Reset configuration to defaults.", "", code="r"),
    }),
    Action("run", "X", "Launch or stop a Flatpak or AppImage.", {
        "flatpak": V("flatpak", "Launch a Flatpak, stop it with --kill, or show running instances with `run flatpak list`.", "<app-id|list>",
                     [Opt("--kill", "-k", "Stop the application")]),
        "appimage": V("appimage", "Launch an AppImage or stop it with --kill.", "<name>", [Opt("--kill", "-k", "Stop the application")]),
    }),
]}


def action_by_code(code):
    for a in ACTIONS.values():
        if a.code == code:
            return a
    return None


def variant_by_code(action, code):
    for v in action.variants.values():
        if v.code == code:
            return v
    return None
