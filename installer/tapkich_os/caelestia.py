"""The desktop: Caelestia shell and its dotfiles (github.com/caelestia-dots, GPL-3.0), customised only through its
own override files, never by editing or forking its code (user decision, 2026-10-18).

What TapkicH-OS adds on top:
  - our keybinds (incl. SUPER + E = Files, Windows-style), Waterfox as the browser and Nautilus as the file manager
    (hypr-vars.lua: Caelestia's own variables),
  - focus follows the mouse and the keyboard layouts chosen in the installer (hypr-user.lua, loaded last),
  - Gaming Mode in Caelestia's power menu: the "hibernate" button runs `tapkich gamemode` (shell.json). Our installs
    cannot hibernate anyway (zram only, no swap partition), so the slot is free.

The Caelestia packages are built from the AUR into the local repo on the ISO (iso/build-aur.sh); the installer
installs them from there, then runs Caelestia's own installer (`caelestia install`) as the new user, which fetches
the dotfiles and puts its configs in place (its package list is already installed by pacstrap).
"""
from __future__ import annotations

import json
import re

# From the ISO's local repo (iso/build-aur.sh). caelestia-shell pulls quickshell-git, qt6-m3shapes-git, libcava,
# ttf-rubik-vf and caelestia-cli (with python-materialyoucolor); the others are in Caelestia's default package list.
PACKAGES = ["caelestia-shell", "caelestia-cli", "darkly-bin", "qtengine", "papirus-folders", "pwvucontrol"]

# The official-repo part of Caelestia's package list (its installer's "Installing packages" step, read from a real run
# on 2026-10-19; all in Arch core/extra). Installed by pacstrap up front: faster than the install-time paru step, and
# it decides the "jack" provider early. Left to Caelestia's installer, pacman refuses to swap the jack2 that ffmpeg/mpv
# pulled in for Caelestia's pipewire-jack under --noconfirm, and the whole Caelestia step stopped with no configs.
OFFICIAL_PACKAGES = [
    "hyprland", "xdg-desktop-portal-hyprland", "xdg-desktop-portal-gtk", "ttf-jetbrains-mono-nerd", "fish", "eza",
    "zoxide", "direnv", "foot", "fastfetch", "btop", "micro", "thunar", "starship", "adw-gtk-theme",
    "papirus-icon-theme", "frameworkintegration", "gnome-keyring", "polkit-gnome", "networkmanager", "bluez",
    "bluez-utils", "pipewire", "pipewire-pulse", "pipewire-audio", "pipewire-alsa", "pipewire-jack", "wireplumber",
    "wl-clipboard", "cliphist", "noto-fonts", "noto-fonts-cjk", "noto-fonts-emoji", "curl", "git", "trash-cli", "jq",
    "lazygit", "bat", "ripgrep", "ydotool", "hyprpicker", "xdg-user-dirs",
]

# Waterfox is the browser, so Caelestia's Firefox component is left out.
INSTALL_COMMAND = "caelestia install --noconfirm --aur-helper paru --disable-components firefox"
STOCK_WALLPAPER = "/etc/xdg/quickshell/caelestia/assets/wallpaper.webp"   # in the caelestia-shell package
BROWSER_PACKAGE = "waterfox-bin"               # AUR, prebuilt into the ISO's local repo
BROWSER = "waterfox"
FILE_MANAGER = "nautilus"                      # the Files app (user's choice); SUPER + E opens it, like on Windows

# Our keys, as overrides of Caelestia's variables (hypr/variables.lua in caelestia-dots/caelestia). Two of its own
# binds move out of the way: its to-do workspace (was SUPER + R) and the browser (was SUPER + W).
KEYBINDS = [
    ("kbLauncher", "SUPER + R", "app launcher (search); Caelestia: tap SUPER"),
    ("kbTerminal", "SUPER + Q", "terminal; Caelestia: SUPER + T"),
    ("kbCloseWindow", "SUPER + W", "close the window; Caelestia: SUPER + Q"),
    ("kbGoToWs", "SUPER", "SUPER + 1 ... 0: go to desktop 1 ... 10"),
    ("kbMoveWinToWs", "SUPER + SHIFT", "SUPER + SHIFT + 1 ... 0: move the window to desktop 1 ... 10; Caelestia: SUPER + ALT"),
    ("kbMoveWinToWsSilent", "SUPER + ALT", "SUPER + ALT + 1 ... 0: move the window, stay here; Caelestia: SUPER + SHIFT"),
    ("kbTodoWs", "SUPER + SHIFT + T", "to-do workspace; Caelestia: SUPER + R"),
    ("kbBrowser", "SUPER + B", "web browser; Caelestia: SUPER + W"),
    ("kbFileExplorer", "SUPER + E", "file manager (Files); Windows-style"),
]
GAMING_ICON = "sports_esports"                 # Material Symbols name (Caelestia's icon font)


# The installer asks which keys to use (user request, 2026-10-19): ours (default) or Caelestia's own, untouched.
KEYBIND_CHOICES = ("tapkich", "caelestia")


def hypr_vars(keybinds: str = "tapkich") -> str:
    if keybinds == "caelestia":                  # Caelestia's own keys; only the apps change (Waterfox, Files)
        return ("-- Caelestia's own keybinds (chosen in the installer); only the apps are changed. Yours to edit.\n"
                f'return {{\n    {"browser":<14} = "{BROWSER}",\n    {"fileExplorer":<14} = "{FILE_MANAGER}",\n}}\n')
    lines = [f'    {name:<14} = "{key}",{" " * max(1, 22 - len(key))}-- {what}' for name, key, what in KEYBINDS]
    lines.append(f'    {"browser":<14} = "{BROWSER}",{" " * max(1, 22 - len(BROWSER))}-- the browser key opens Waterfox')
    lines.append(f'    {"fileExplorer":<14} = "{FILE_MANAGER}",{" " * max(1, 22 - len(FILE_MANAGER))}-- the file manager key opens Files')
    return ("-- TapkicH-OS keybinds on top of Caelestia's (its hypr/variables.lua). Yours to edit.\n"
            "return {\n" + "\n".join(lines) + "\n}\n")


def hypr_user(layouts: str = "us", keybinds: str = "tapkich") -> str:
    """Loaded last by Caelestia's hyprland.lua: the keyboard layouts from the installer, and (with our keys) focus
    follows the mouse."""
    if not re.fullmatch(r"[a-z]{2,8}(,[a-z]{2,8})*", layouts):
        raise ValueError(f"unsafe keyboard layout list {layouts!r}")
    options = "grp:alt_shift_toggle" if "," in layouts else ""
    if keybinds == "caelestia":
        return f"""-- TapkicH-OS: only the keyboard layouts from the installer (loaded last). Yours to edit.
hl.config({{
    input = {{
        kb_layout  = "{layouts}",
        kb_options = "{options}",
    }},
}})
"""
    return f"""-- TapkicH-OS additions on top of Caelestia (loaded last). Yours to edit.
hl.config({{
    input = {{
        kb_layout    = "{layouts}",
        kb_options   = "{options}",
        follow_mouse = 1,                -- focus follows the mouse pointer: no click needed
    }},
}})
"""


def shell_json() -> str:
    """Only what we change; Caelestia fills in its defaults for everything else."""
    return json.dumps({"session": {"icons": {"hibernate": GAMING_ICON},
                                   "commands": {"hibernate": ["tapkich", "gamemode"]}}}, indent=4) + "\n"


def skel_files(layouts: str = "us", keybinds: str = "tapkich", graphics_lua: str = "") -> dict:
    """Per-user files, relative to the home directory (written into /etc/skel before the user is created).
    graphics_lua: extra lines for hypr-user.lua from the installer's graphics step (NVIDIA environment, nvidia.py)."""
    if keybinds not in KEYBIND_CHOICES:
        raise ValueError(f"unknown keybind choice {keybinds!r}")
    return {
        ".config/caelestia/hypr-vars.lua": hypr_vars(keybinds),
        ".config/caelestia/hypr-user.lua": hypr_user(layouts, keybinds) + ("\n" + graphics_lua if graphics_lua else ""),
        ".config/caelestia/shell.json": shell_json(),
    }


# ---------------------------------------------------------------- the key list (`tapkich keys`)
# Read from the real config every time, so it always matches what Hyprland binds: Caelestia's own defaults
# (~/.config/hypr/variables.lua, put there by `caelestia install`) with the user's overrides on top
# (~/.config/caelestia/hypr-vars.lua: our keys, Caelestia's own, or whatever the user edited since). The same merge
# Caelestia's hyprland.lua does.
VARIABLES_FILE = ".config/hypr/variables.lua"
OVERRIDES_FILE = ".config/caelestia/hypr-vars.lua"

# (variable, what it does), grouped, in the order shown.
KEY_ACTIONS = [
    ("Apps", [("kbLauncher", "app launcher (search)"), ("kbTerminal", "terminal"), ("kbBrowser", "web browser"),
              ("kbFileExplorer", "file manager"), ("kbEditor", "text editor"), ("kbAudioSettings", "sound settings")]),
    ("Windows", [("kbCloseWindow", "close the window"), ("kbWindowFullscreen", "fullscreen"),
                 ("kbToggleWindowFloating", "float / tile the window"), ("kbPinWindow", "pin the window"),
                 ("kbCenterWindow", "centre the window"), ("kbWindowCycleNext", "next window")]),
    ("Desktops", [("kbGoToWs", "go to desktop 1 ... 10"), ("kbMoveWinToWs", "move the window to desktop 1 ... 10"),
                  ("kbNextWs", "next desktop"), ("kbPrevWs", "previous desktop"), ("kbSpecialWs", "scratchpad"),
                  ("kbTodoWs", "to-do workspace"), ("kbMusicWs", "music workspace"),
                  ("kbCommunicationWs", "chat workspace"), ("kbSystemMonitorWs", "system monitor")]),
    ("Shell", [("kbSession", "power menu (the Gaming Mode button is here)"), ("kbShowSidebar", "notifications"),
               ("kbShowPanels", "all panels"), ("kbLock", "lock the screen"), ("kbSleep", "sleep"),
               ("kbClipboard", "clipboard history"), ("kbEmoji", "emoji picker")]),
    ("Screen", [("kbScreenshotRegion", "screenshot of an area"), ("kbScreenshotFreeze", "screenshot (frozen screen)"),
                ("kbScreenshot", "screenshot of everything"), ("kbRecord", "record the screen"),
                ("kbColorPicker", "colour picker")]),
    ("Media", [("kbMediaToggle", "play / pause"), ("kbMediaNext", "next track"), ("kbVolumeMute", "mute")]),
]
MODIFIER_ONLY = {"kbGoToWs", "kbMoveWinToWs", "kbGoToWsGroup", "kbMoveWinToWsGroup"}   # + the number keys

_LUA_ENTRY = re.compile(r"""^\s*(\w+)\s*=\s*("[^"\n]*"|'[^'\n]*'|\{[^}\n]*\})""", re.M)
_LUA_STRING = re.compile(r"""\"([^"]*)\"|'([^']*)'""")
_LUA_BLOCK_COMMENT = re.compile(r"--\[(=*)\[.*?\]\1\]", re.S)


def parse_lua_keys(text: str) -> dict:
    """`name = "KEY"` and `name = { "KEY", "KEY" }` entries of a Caelestia variables file -> {name: [keys]}.
    Comments are dropped first; anything that is not a string or a list of strings is ignored."""
    text = _LUA_BLOCK_COMMENT.sub("", text)
    text = re.sub(r"--[^\n]*", "", text)
    return {name: [a or b for a, b in _LUA_STRING.findall(value)] for name, value in _LUA_ENTRY.findall(text)}


def pretty_key(key: str) -> str:
    key = re.sub(r"\s*\+\s*", " + ", key.strip())
    if key.upper() in ("SUPER + SUPER_L", "SUPER_L"):
        return "SUPER (tap)"
    return key.replace("mouse_down", "scroll down").replace("mouse_up", "scroll up")


def read_keys(home: str) -> tuple:
    """(merged keys, names the override file changes, whether Caelestia's own key file was found)."""
    import os

    def load(rel):
        try:
            with open(os.path.join(home, rel)) as f:
                return parse_lua_keys(f.read())
        except OSError:
            return {}
    base, over = load(VARIABLES_FILE), load(OVERRIDES_FILE)
    changed = {k for k, v in over.items() if k.startswith("kb") and base.get(k) != v}     # keys only, not apps
    return {**base, **over}, changed, bool(base)


def user_home() -> str:
    """The home whose keys to show: as root (`sudo tapkich keys`) that is the user who called sudo, not /root."""
    import os
    user = os.environ.get("SUDO_USER", "")
    if os.geteuid() == 0 and user and user != "root":
        try:
            import pwd
            return pwd.getpwnam(user).pw_dir
        except (KeyError, ImportError):
            pass
    return os.path.expanduser("~")


def key_list(home: str | None = None) -> str:
    """The keys as they are really set right now, grouped; the ones changed from Caelestia's default marked *."""
    import os
    keys, changed, found = read_keys(home or user_home())
    rows = []
    for group, actions in KEY_ACTIONS:
        lines = []
        for name, what in actions:
            values = [v for v in keys.get(name, []) if v.strip()]
            if not values:
                continue
            if name in MODIFIER_ONLY:
                shown = pretty_key(values[0]) + " + 1 ... 0"
            else:
                shown = "  or  ".join(pretty_key(v) for v in values[:2])
                if len(shown) > 40:                       # two keys do not fit: the first one is enough
                    shown = pretty_key(values[0])
            lines.append(f"  {'*' if name in changed else ' '} {shown:<40} {what}")
        if lines:
            rows += [f"  {group}", *lines]
    if not found:
        rows.insert(0, "  (Caelestia's key file ~/.config/hypr/variables.lua was not found: only the changed keys are shown)")
    rows.append("\n  * changed from Caelestia's default (in ~/.config/caelestia/hypr-vars.lua)" if changed
                else "\n  These are Caelestia's own keys, unchanged.")
    return "\n".join(rows) + "\n"
