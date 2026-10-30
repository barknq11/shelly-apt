<p align="center"><img src="data/icons/hicolor/scalable/apps/org.shellyapt.Shelly.svg" width="128" alt="Shelly-APT icon"></p>

# Shelly-APT

A Debian/Ubuntu/PikaOS port of [Shelly](https://github.com/Seafoam-Labs/Shelly-ALPM), the
user-friendly Arch Linux package manager. It manages **APT packages, local `.deb` files,
Flatpaks and AppImages** from one CLI (`shelly`) and one GTK4/libadwaita app (`shelly-ui`).

The original is ~258k lines of Zig built on libalpm. This port is written in Python on
top of `python3-apt`, and keeps Shelly's command grammar, shortcodes, pages and
workflows. Arch-only features are replaced with their Debian equivalents (see the table
at the end).

## Install

```bash
sudo apt install git debhelper python3-apt python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 flatpak
git clone https://github.com/barknq11/shelly-apt.git
cd shelly-apt
make deb                      # builds ../shelly-apt_0.1.0_all.deb
sudo apt install ../shelly-apt_0.1.0_all.deb
```

Or run it straight from the source tree: `./bin/shelly …` and `./bin/shelly-ui`.
Background update notifications: `systemctl --user enable --now shelly-notifications.timer`
(or flip the switch in Settings).

## CLI

```
shelly <action> <type> [options] [args]
shelly -<Action><type>[modifiers] [args]
shelly <search words>          # search APT + Flatpak, pick numbers to install
```

| Code | Action | Types |
|---|---|---|
| `S` | search | `s` standard, `f` flatpak, `x` all |
| `I` | install | `s`, `f`, `i` appimage |
| `R` | remove | `s`, `f`, `i` |
| `U` | upgrade | `s`, `f`, `i`, `x` all |
| `E` | update (named packages only) | `s`, `f`, `i` |
| `L` | list | `s`, `f`, `i` |
| `P` | list-updates | `s`, `f`, `i`, `x` |
| `Y` | sync | `s`, `f`, `i` |
| `D` | downgrade | `s` |
| `M` | mark | `o` hold, `u` unhold, `e` explicit, `d` dependency, `l` list |
| `Z` | purify | `s`, `f` |
| `B` | backup / restore | — |
| `T` | utility | `r` repository, `k` keyring, `c` conffiles, `m` completions, `b` reboot |
| `C` | config | `l` list, `g` get, `s` set, `r` reset |
| `X` | run | `f`, `i` |

Examples:

```bash
shelly -Ss htop                 # search
shelly -Ss -d bash              # full package details
shelly -Is htop ./foo.deb       # install packages and a local .deb
shelly -Is https://…/app.deb    # download and install a .deb
shelly -Rscp foo                # remove + autoremove + purge config
shelly -Us                      # apt update + full-upgrade with a plan preview
shelly -Ux                      # upgrade APT, Flatpaks and AppImages
shelly -Px                      # list every pending update
shelly -Ds firefox              # pick an older version (repos + /var/cache/apt)
shelly -Mo firefox              # hold it
shelly -Lsf                     # packages not from any repository
shelly -Zs -r -c                # orphans, leftover configs, clean cache
shelly -Tr add vscode https://packages.microsoft.com/repos/code stable main \
      --key https://packages.microsoft.com/keys/microsoft.asc --refresh
shelly -Tc --diff               # review .dpkg-new / .dpkg-dist / .ucf-* files
shelly -If org.mozilla.firefox  # Flatpak
shelly -Ii ~/Downloads/App.AppImage
shelly sync appimage -c app github owner/repo   # AppImage updates from GitHub releases
shelly backup -o pkgs.toml && shelly backup --restore pkgs.toml
shelly config set FileSizeDisplay Auto
```

Global flags: `-n/--no-confirm`, `-j/--json`, `-U/--ui-mode` (framed progress for the GUI).
Commands that change the system re-run themselves through `sudo`, `run0` or `pkexec`
(configurable with `Elevation`). Flatpak and AppImage commands never need root.

## GUI

`shelly-ui` has the same pages as Shelly: **Packages** (installed/available, filters,
details, hold/remove/install with a dependency preview), **Flatpak** (installed and
discover), **AppImage**, **Updates** (APT + Flatpak + AppImage, Upgrade all, reboot banner),
**Utilities** (cleanup, backup/restore, repositories, pending conffiles) and **Settings**.
APT changes run through `pkexec shelly --ui-mode` and stream progress from apt's status fd.

## Arch → Debian mapping

| Shelly (Arch) | Shelly-APT (Debian) |
|---|---|
| libalpm / RLPM | python3-apt for queries and planning, `apt-get` for transactions |
| pacman repositories | APT suites/components (`bookworm/main`, `pika/nest`) |
| AUR (`-Ia`, `build`, `repo-db`) | not applicable; install `.deb` files/URLs, PPAs via `utility repository add ppa:…` |
| foreign packages (`-Qm`) | `list standard --foreign` (installed version not in any repo) |
| IgnorePkg / HoldPkg | `apt-mark hold` (`mark hold`) |
| pacnew / pacsave | `.dpkg-new`, `.dpkg-dist`, `.dpkg-old`, `.ucf-*` (`utility conffiles`) |
| pacman-key | `/etc/apt/keyrings` (`utility keyring`) |
| Arch news | not applicable |
| downgrade from cache/ALA | older repo versions + `/var/cache/apt/archives` |

## Built with AI assistance

This project was built with the help of AI (Anthropic's Claude), working from the
original Shelly source and tested on a real PikaOS system. Review the code like any
other third-party package manager before relying on it, and please report bugs as
GitHub issues.

## License

GPL-3.0, same as upstream Shelly.
