"""Terminal output: colors, tables, sizes, prompts and UI-mode framing."""

import json
import os
import shutil
import sys
import unicodedata

UI_PREFIX = "@@shelly "


class ShellyError(Exception):
    """A user-facing failure; printed without a traceback."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class Output:
    def __init__(self, config=None, json_mode=False, ui_mode=False, no_confirm=False):
        self.json_mode = json_mode
        self.ui_mode = ui_mode
        self.no_confirm = no_confirm
        self.size_mode = config["FileSizeDisplay"] if config else "Megabytes"
        colors = config["Colors"] if config else True
        self.color = colors and sys.stdout.isatty() and not ui_mode and "NO_COLOR" not in os.environ

    # ---- styling -------------------------------------------------------
    def c(self, text, code):
        return f"\033[{code}m{text}\033[0m" if self.color else str(text)

    def bold(self, t):
        return self.c(t, "1")

    def dim(self, t):
        return self.c(t, "2")

    def green(self, t):
        return self.c(t, "32")

    def red(self, t):
        return self.c(t, "31")

    def yellow(self, t):
        return self.c(t, "33")

    def cyan(self, t):
        return self.c(t, "36")

    def magenta(self, t):
        return self.c(t, "35")

    # ---- messages ------------------------------------------------------
    def info(self, msg):
        if self.json_mode:
            return
        print(self.cyan("::") + " " + self.bold(msg), flush=True)

    def ok(self, msg):
        if self.json_mode:
            return
        print(self.green("✓") + " " + msg, flush=True)

    def warn(self, msg):
        print(self.yellow("warning:") + " " + msg, file=sys.stderr, flush=True)

    def error(self, msg):
        print(self.red("error:") + " " + msg, file=sys.stderr, flush=True)

    def event(self, kind, **data):
        """Emit a framed machine-readable event for the GUI (only in --ui-mode)."""
        if self.ui_mode:
            data["event"] = kind
            print(UI_PREFIX + json.dumps(data), flush=True)

    def emit_json(self, data):
        json.dump(data, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")

    # ---- sizes ---------------------------------------------------------
    def size(self, num):
        if num is None:
            return "-"
        neg = num < 0
        n = abs(num)
        mode = self.size_mode
        if mode == "Bytes":
            text = f"{n} B"
        elif mode == "Gigabytes":
            text = f"{n / 1024 ** 3:.2f} GiB"
        elif mode == "Auto":
            units = ["B", "KiB", "MiB", "GiB", "TiB"]
            i = 0
            v = float(n)
            while i < len(units) - 1 and round(v, 2) >= 1024:
                v /= 1024
                i += 1
            text = f"{int(v)} B" if i == 0 else f"{v:.2f} {units[i]}"
        else:
            text = f"{n / 1024 ** 2:.2f} MiB"
        return ("-" if neg else "") + text

    # ---- tables --------------------------------------------------------
    def table(self, headers, rows, max_widths=None):
        if not rows:
            return
        width = shutil.get_terminal_size((120, 24)).columns
        cols = len(headers)
        str_rows = [[("" if v is None else str(v)) for v in r] for r in rows]
        widths = [display_width(h) for h in headers]
        for r in str_rows:
            for i, v in enumerate(r):
                widths[i] = max(widths[i], display_width(strip_ansi(v)))
        if max_widths:
            for i, mw in enumerate(max_widths):
                if mw:
                    widths[i] = min(widths[i], mw)
        # Shrink the last column to fit the terminal.
        used = sum(widths[:-1]) + 2 * (cols - 1)
        widths[-1] = max(10, min(widths[-1], width - used))
        line = "  ".join(self.bold(pad(h, widths[i])) for i, h in enumerate(headers))
        print(line)
        print(self.dim("  ".join("─" * w for w in widths)))
        for r in str_rows:
            print("  ".join(pad(truncate(v, widths[i]), widths[i]) for i, v in enumerate(r)).rstrip())

    # ---- prompts -------------------------------------------------------
    def confirm(self, question, default=True):
        if self.no_confirm or self.ui_mode:
            return default
        if not sys.stdin.isatty():
            return default
        hint = "[Y/n]" if default else "[y/N]"
        try:
            answer = input(f"{self.cyan('::')} {self.bold(question)} {hint} ").strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        return answer in ("y", "yes")

    def choose(self, question, count, allow_multiple=True):
        """Prompt for 1-based indices like '1 2 4-6'. Returns zero-based list."""
        if self.no_confirm or not sys.stdin.isatty():
            return []
        try:
            answer = input(f"{self.cyan('::')} {self.bold(question)} ").strip()
        except EOFError:
            return []
        picked = []
        for part in answer.replace(",", " ").split():
            if "-" in part:
                a, _, b = part.partition("-")
                if a.isdigit() and b.isdigit():
                    picked.extend(range(int(a), int(b) + 1))
            elif part.isdigit():
                picked.append(int(part))
        result = []
        for i in picked:
            if 1 <= i <= count and (i - 1) not in result:
                result.append(i - 1)
        return result if allow_multiple else result[:1]


def strip_ansi(text):
    out, skip = [], False
    for ch in text:
        if ch == "\033":
            skip = True
        elif skip and ch == "m":
            skip = False
        elif not skip:
            out.append(ch)
    return "".join(out)


def display_width(text):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def truncate(text, width):
    plain = strip_ansi(text)
    if display_width(plain) <= width:
        return text
    out, w = [], 0
    for ch in plain:
        cw = display_width(ch)
        if w + cw > width - 1:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "…"


def pad(text, width):
    return text + " " * max(0, width - display_width(strip_ansi(text)))
