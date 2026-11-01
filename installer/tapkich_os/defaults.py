"""`tapkich default <app>`: make an app the default browser, terminal or file manager. One registry (DEFAULTS) feeds
the command and the help text; to add an app, add a line to it.

Where each kind is set (read from caelestia-shell 2.5.0 and its dotfiles, 2026-11-01):
  - Caelestia's settings (its Apps page) keep `general.apps.terminal` and `general.apps.explorer` in
    ~/.config/caelestia/shell.json: the launcher opens terminal apps with the first, the shell opens folders with the
    second. It has no browser setting.
  - The keys (SUPER + Q/T, SUPER + B/W, SUPER + E) run the `terminal`, `browser` and `fileExplorer` variables of
    Caelestia's hypr/variables.lua, overridden in ~/.config/caelestia/hypr-vars.lua.
  - Links, web pages and folders opened by other apps follow the xdg defaults (~/.config/mimeapps.list).
Each file is copied to <file>.before-tapkich-default before its first change.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass


@dataclass(frozen=True)
class Default:
    name: str            # `tapkich default <name>`
    label: str           # shown in messages and the help
    kind: str            # "browser", "terminal" or "files"
    command: str         # the program that starts it
    desktop: str = ""    # its .desktop file, for the xdg default (browsers and file managers)


DEFAULTS = [
    Default("chromium", "Chromium", "browser", "chromium", "chromium.desktop"),
    Default("waterfox", "Waterfox", "browser", "waterfox", "waterfox.desktop"),
    Default("firefox", "Firefox", "browser", "firefox", "firefox.desktop"),
    Default("chrome", "Google Chrome", "browser", "google-chrome-stable", "google-chrome.desktop"),
    Default("brave", "Brave", "browser", "brave", "brave-browser.desktop"),
    Default("foot", "foot", "terminal", "foot"),
    Default("alacritty", "Alacritty", "terminal", "alacritty"),
    Default("kitty", "kitty", "terminal", "kitty"),
    Default("nautilus", "Files (Nautilus)", "files", "nautilus", "org.gnome.Nautilus.desktop"),
    Default("thunar", "Thunar", "files", "thunar", "thunar.desktop"),
]
BY_NAME = {d.name: d for d in DEFAULTS}
ALIASES = {"google-chrome": "chrome", "chrome-debloated": "chrome", "brave-debloated": "brave", "files": "nautilus"}

KIND_WORDS = {"browser": "browser", "terminal": "terminal", "files": "file manager"}
SHELL_KEYS = {"terminal": "terminal", "files": "explorer"}                  # shell.json general.apps.<key>
HYPR_VARS = {"browser": "browser", "terminal": "terminal", "files": "fileExplorer"}
KEY_WORDS = {"browser": "the browser key (SUPER + B)", "terminal": "the terminal key (SUPER + Q)",
             "files": "the file manager key (SUPER + E)"}

SHELL_JSON = ".config/caelestia/shell.json"
HYPR_VARS_FILE = ".config/caelestia/hypr-vars.lua"
BACKUP_SUFFIX = ".before-tapkich-default"

HELP_WIDTH = 78


def help_lines(width: int = HELP_WIDTH) -> str:
    """The 'Default apps' lines of `tapkich help`, made from DEFAULTS, wrapped to `width` columns."""
    out = []
    for kind, word in KIND_WORDS.items():
        names = ", ".join(d.name for d in DEFAULTS if d.kind == kind)
        head = f"  {word + 's:':<16}"
        lines = textwrap.wrap(names, width - len(head), break_on_hyphens=False)
        out.append(head + lines[0])
        out += [" " * len(head) + line for line in lines[1:]]
    return "\n".join(out)


def usage_text() -> str:
    return "Default apps (tapkich default <name>):\n" + help_lines()


def _backup(path: str) -> None:
    if os.path.exists(path) and not os.path.exists(path + BACKUP_SUFFIX):
        shutil.copy2(path, path + BACKUP_SUFFIX)


def _write(path: str, text: str) -> None:
    _backup(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tapkich-new"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def set_shell_app(path: str, key: str, command: str) -> bool:
    """general.apps.<key> = [command] in Caelestia's shell.json, everything else kept. False if it already was."""
    data = {}
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)                       # a broken file raises ValueError: it is not overwritten
    if not isinstance(data, dict) or not isinstance(data.get("general", {}), dict) \
            or not isinstance(data.get("general", {}).get("apps", {}), dict):
        raise ValueError("general.apps is not an object")
    apps = data.setdefault("general", {}).setdefault("apps", {})
    if apps.get(key) == [command]:
        return False
    apps[key] = [command]
    _write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")   # Caelestia writes it with 2 spaces
    return True


def set_hypr_var(text: str, var: str, command: str, name: str) -> str:
    """hypr-vars.lua with `var = "command"`: its line replaced, or added at the end of the table."""
    line = f'    {var:<14} = "{command}",{" " * max(1, 22 - len(command))}-- set by `tapkich default {name}`'
    entry = re.compile(rf'^[ \t]*{re.escape(var)}[ \t]*=[ \t]*("[^"\n]*"|\'[^\'\n]*\')[^\n]*$', re.M)
    if entry.search(text):
        return entry.sub(lambda m: line, text, count=1)
    end = text.rfind("}")
    if not text.strip():
        return "return {\n" + line + "\n}\n"
    if end < 0:
        raise ValueError("no table to add to")
    before = re.sub(r"--[^\n]*", "", text[:end]).rstrip()
    if not before.endswith(("{", ",", ";")):          # the last entry has no comma: adding one after it is guesswork
        raise ValueError("its last entry has no comma after it")
    head = text[:end] if text[:end].endswith("\n") else text[:end] + "\n"
    return head + line + "\n" + text[end:]


def default(args: list, home: str | None = None, run=subprocess.run, out=print, euid: int | None = None,
            which=shutil.which, env=None) -> int:
    """`tapkich default [name]`. No name: the list. Unknown name: the list, exit 2."""
    from . import appinstall
    env = os.environ if env is None else env
    if not args or args[0] in ("-h", "--help", "help"):
        out(usage_text())
        return 0
    if len(args) > 1:
        out("tapkich default: one app at a time, with no options: tapkich default <name>\n\n" + usage_text())
        return 2
    name = ALIASES.get(args[0], args[0])
    if name not in BY_NAME:
        out(f"tapkich default: unknown app {args[0]!r}\n\n" + usage_text())
        return 2
    app = BY_NAME[name]
    if (os.geteuid() if euid is None else euid) == 0:     # the settings are the user's, not root's
        out("tapkich default: run it as your user, not with sudo.")
        return 1
    if not which(app.command):
        install = appinstall.ALIASES.get(app.name, app.name)
        hint = (f"tapkich install {install}" if install in appinstall.BY_NAME
                else f"sudo pacman -S {app.command}")
        out(f"{app.label} is not installed. Install it first: {hint}")
        return 1
    home = home or os.path.expanduser("~")
    word = KIND_WORDS[app.kind]
    done, rc = [], 0
    if app.kind in SHELL_KEYS:
        path = os.path.join(home, SHELL_JSON)
        try:
            set_shell_app(path, SHELL_KEYS[app.kind], app.command)
            done.append(f"Caelestia's {word} setting (~/{SHELL_JSON})")
        except (OSError, ValueError) as e:
            out(f"Could not change ~/{SHELL_JSON}: {e}")
            rc = 1
    path = os.path.join(home, HYPR_VARS_FILE)
    try:
        old = ""
        if os.path.exists(path):
            with open(path) as f:
                old = f.read()
        new = set_hypr_var(old, HYPR_VARS[app.kind], app.command, app.name)
        if new != old:
            _write(path, new)
            if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
                run(["hyprctl", "reload"], stdout=subprocess.DEVNULL, check=False)
        done.append(f"{KEY_WORDS[app.kind]} (~/{HYPR_VARS_FILE})")
    except (OSError, ValueError) as e:
        out(f"Could not change ~/{HYPR_VARS_FILE}: {e}")
        rc = 1
    if app.desktop:
        cmd = (["xdg-settings", "set", "default-web-browser", app.desktop] if app.kind == "browser"
               else ["xdg-mime", "default", app.desktop, "inode/directory"])
        try:
            ok = run(cmd, check=False).returncode == 0
        except OSError:
            ok = False
        if ok:
            done.append("links and web pages from other apps" if app.kind == "browser" else "folders opened by other apps")
        else:
            out(f"Could not set the system default: {' '.join(cmd)}")
            rc = 1
    if done:
        out(f"{app.label} is now the default {word} for:\n" + "\n".join(f"  - {d}" for d in done))
    return rc
