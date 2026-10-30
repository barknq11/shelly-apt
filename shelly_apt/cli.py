"""The `shelly` command: argument parsing, shortcodes, help and dispatch."""

import os
import sys

from . import VERSION, spec
from .config import Config
from .term import Output, ShellyError

GLOBAL_FLAGS = {"--no-confirm": "no_confirm", "-n": "no_confirm",
                "--ui-mode": "ui_mode", "-U": "ui_mode",
                "--json": "json", "-j": "json"}


class Context:
    def __init__(self, out, config, argv, globals_):
        self.out = out
        self.config = config
        self.argv = argv  # canonical argv (shortcodes expanded), used for re-exec
        self.globals = globals_

    def global_args(self):
        flags = []
        if self.out.no_confirm:
            flags.append("--no-confirm")
        if self.out.ui_mode:
            flags.append("--ui-mode")
        if self.out.json_mode:
            flags.append("--json")
        return flags


# ---- shortcode translation -----------------------------------------------

def translate_shortcode(token):
    """'-Rscp' -> ['remove', 'standard', '--cascade', '--remove-config']; None if not a shortcode."""
    if len(token) < 2 or token[0] != "-" or token[1] == "-" or not token[1].isupper():
        return None
    action = spec.action_by_code(token[1])
    if action is None:
        return None
    if len(token) == 2:
        if action.bare:
            return [action.name]
        if token in ("-U", "-V"):
            return None  # global flags
        return [action.name, "--help"]
    type_code = token[2]
    variant = spec.variant_by_code(action, type_code)
    mods = token[3:]
    if variant is None and action.bare:
        variant, mods = action.variants[""], token[2:]
    if variant is None:
        valid = ", ".join(f"{v.code} ({v.name})" for v in action.variants.values() if v.code)
        raise ShellyError(f"Unknown type '{type_code}' for action code '{token[1]}' ({action.name}). Valid types: {valid}")
    result = [action.name] + ([variant.name] if variant.name else [])
    for m in mods:
        if m == "h":
            result.append("--help")
            continue
        opt = next((o for o in variant.opts if o.short == f"-{m}"), None)
        if opt is None:
            valid = ", ".join(o.short[1] for o in variant.opts if o.short) or "none"
            raise ShellyError(f"Unknown modifier '{m}' for '{action.name} {variant.name}'. Valid modifiers: {valid}")
        result.append(opt.name)
    return result


# ---- parsing ---------------------------------------------------------------

def parse(argv):
    globals_ = {"no_confirm": False, "ui_mode": False, "json": False, "help": False, "version": False}
    rest = []
    for tok in argv:
        if tok in GLOBAL_FLAGS:
            globals_[GLOBAL_FLAGS[tok]] = True
        elif tok in ("--version", "-V") and not rest:
            globals_["version"] = True
        else:
            rest.append(tok)
    if rest:
        expanded = translate_shortcode(rest[0])
        if expanded is not None:
            rest = expanded + rest[1:]
    return globals_, rest


def parse_options(variant, args):
    opts, positional = {}, []
    by_name = {o.name: o for o in variant.opts}
    by_short = {o.short: o for o in variant.opts if o.short}
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            positional += args[i + 1:]
            break
        if tok in ("--help", "-h"):
            opts["help"] = True
        elif tok.startswith("--"):
            name, eq, value = tok.partition("=")
            opt = by_name.get(name)
            if opt is None:
                raise ShellyError(f"Unknown option '{name}'. See 'shelly … --help'.")
            if opt.value:
                if not eq:
                    i += 1
                    if i >= len(args):
                        raise ShellyError(f"{name} requires a value ({opt.value})")
                    value = args[i]
                opts[name] = value
            else:
                opts[name] = True
        elif tok.startswith("-") and len(tok) > 1 and not tok[1].isdigit():
            # bundled short flags: -cp
            letters = tok[1:]
            for j, letter in enumerate(letters):
                opt = by_short.get(f"-{letter}")
                if opt is None:
                    raise ShellyError(f"Unknown option '-{letter}'")
                if opt.value:
                    value = letters[j + 1:] or (args[i + 1] if i + 1 < len(args) else None)
                    if value is None:
                        raise ShellyError(f"-{letter} requires a value ({opt.value})")
                    if not letters[j + 1:]:
                        i += 1
                    opts[opt.name] = value
                    break
                opts[opt.name] = True
        else:
            positional.append(tok)
        i += 1
    return opts, positional


# ---- help ------------------------------------------------------------------

def root_help(out):
    print(f"{out.bold('Shelly-APT')} {VERSION} — a unified package manager for Debian: APT packages, Flatpaks and AppImages.")
    print()
    print(f"{out.bold('Usage:')} shelly <action> <type> [options] [arguments]")
    print("       shelly -<Action><type>[modifiers] [arguments]     e.g. shelly -Is htop, shelly -Rscp foo")
    print("       shelly <search words>                             search and pick packages to install")
    print()
    print(out.bold("Actions:"))
    for a in spec.ACTIONS.values():
        print(f"  {out.cyan(a.code)}  {a.name:<13} {a.help}")
    print()
    print(out.bold("Types:"))
    for name, (code, desc) in spec.TYPES.items():
        print(f"  {out.cyan(code)}  {name:<13} {desc}")
    print()
    print(out.bold("Global options:"))
    for o in spec.GLOBAL_OPTS:
        print(f"  {o.short or '  '}, {o.name:<14} {o.help}")
    print()
    print("Run 'shelly <action> --help' or 'shelly <action> <type> --help' for details.")


def action_help(out, action):
    print(f"{out.bold('shelly ' + action.name)} ({out.cyan('-' + action.code)}) — {action.help}")
    print()
    for v in action.variants.values():
        code = f"-{action.code}{v.code}" if v.code else f"-{action.code}"
        label = v.name or "(no type)"
        print(f"  {out.cyan(code):<6} {label:<13} {v.help}")


def variant_help(out, action, variant):
    code = f"-{action.code}{variant.code}"
    words = " ".join(w for w in ("shelly", action.name, variant.name) if w)
    print(f"{out.bold(words)} {variant.usage}".rstrip())
    print(f"Shortcode: {out.cyan(code)}" + (f"[{''.join(o.short[1] for o in variant.opts if o.short)}]" if any(o.short for o in variant.opts) else ""))
    print()
    print(variant.help)
    if variant.root:
        print(out.dim("Requires root; Shelly elevates through sudo, run0 or pkexec automatically."))
    if variant.opts:
        print()
        print(out.bold("Options:"))
        for o in variant.opts:
            flag = (f"{o.short}, " if o.short else "    ") + o.name + (f" <{o.value}>" if o.value else "")
            print(f"  {flag:<30} {o.help}")


# ---- entry point -------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    config = Config()
    out = Output(config)
    try:
        g, rest = parse(argv)
        out = Output(config, json_mode=g["json"], ui_mode=g["ui_mode"], no_confirm=g["no_confirm"])
        if g["version"]:
            print(f"shelly-apt {VERSION}")
            return 0
        if not rest or rest[0] in ("--help", "-h", "help"):
            root_help(out)
            return 0

        from . import commands  # imported lazily: python-apt is slow to import

        action = spec.ACTIONS.get(rest[0])
        if action is None:
            if rest[0].startswith("-"):
                raise ShellyError(f"Unknown option or shortcode '{rest[0]}'. See 'shelly --help'.")
            ctx = Context(out, config, rest, g)
            return commands.interactive_search(ctx, rest) or 0

        args = rest[1:]
        if action.bare:
            variant = action.variants[""]
        else:
            if not args or args[0] in ("--help", "-h"):
                action_help(out, action)
                return 0
            variant = action.variants.get(args[0])
            if variant is None:
                valid = ", ".join(action.variants)
                raise ShellyError(f"Unknown type '{args[0]}' for '{action.name}'. Valid types: {valid}")
            args = args[1:]
        opts, positional = parse_options(variant, args)
        if opts.pop("help", False):
            variant_help(out, action, variant)
            return 0
        canonical = []
        for flag, key in (("--no-confirm", "no_confirm"), ("--ui-mode", "ui_mode"), ("--json", "json")):
            if g[key]:
                canonical.append(flag)
        canonical += rest
        ctx = Context(out, config, canonical, g)
        handler = commands.handler(action.name, variant.name)
        if variant.root and commands.needs_root(action.name, variant.name, opts, positional):
            from .system import ensure_root
            ensure_root(out, config, canonical)
        return handler(ctx, opts, positional) or 0
    except ShellyError as exc:
        out.error(str(exc))
        return exc.code or 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        out.error("Interrupted")
        return 130
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
