"""Default applications for TapkicH-OS: what is installed, what opens what, and how GTK apps get our colours.

Choices made by the user (2026-10-18):
file manager Nautilus (+ Yazi in the terminal), images Loupe, video mpv + Celluloid, music Amberol, PDF Papers,
browser Waterfox (Firefox if Waterfox cannot be installed), archives Xarchiver, monitor Mission Center + btop,
calculator GNOME Calculator, screenshots omasnap (what Omarchy uses), recording gpu-screen-recorder,
games Steam (native, from Arch's multilib repo, NOT Flatpak, so Steam Input and Big Picture work) + Lutris,
ProtonUp-Qt and Proton-GE, plus the Codex CLI and Claude Code command-line tools.

Optional apps (Claude and ChatGPT desktop, 1Password, Chrome, ...) are not installed automatically: `tapkich install
<name>` does it (the registry in appinstall.py). Anything proprietary (Claude Code, Discord, the optional apps) is
installed ON THE USER'S MACHINE from the Arch repos or the AUR, which downloads it from the vendor. It is never copied
into our ISO or any repository of ours.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

# Official Arch repositories (`extra`, and `multilib` for the 32-bit parts of Steam).
APP_PACKAGES = [
    # files, media, documents
    "nautilus", "gvfs", "yazi", "loupe", "mpv", "celluloid", "amberol", "papers", "xarchiver", "zip", "unzip", "7zip",
    "gst-plugins-good", "gst-plugins-bad", "gst-libav",
    # system
    "mission-center", "btop", "gnome-calculator", "udiskie", "cliphist", "gpu-screen-recorder",
    # look: GTK3 apps look like libadwaita (dark), dconf stores the dark-mode setting
    "adw-gtk-theme", "papirus-icon-theme", "dconf",
    # chat and game launchers (Discord is proprietary: installed from the mirrors like Steam, never on the ISO)
    "discord", "prismlauncher",
    # games (multilib): Steam, Lutris, and the 32-bit graphics libraries Steam needs
    "steam", "steam-devices", "lutris", "lib32-mesa", "lib32-vulkan-icd-loader", "lib32-vulkan-radeon",
    "lib32-vulkan-intel", "lib32-gamemode", "lib32-mangohud",
    # app store: Bazaar (GPL-3.0) for Flathub apps; Arch's flatpak sets up the Flathub remote by itself
    "flatpak", "bazaar",
    # AI coding agent in the terminal (Apache-2.0)
    "openai-codex",
    # needed to build AUR packages
    "base-devel",
]

# The user's AUR apps, built into the local repo on the ISO (iso/build-aur.sh) and installed with pacstrap, so the
# install never builds or repackages them (Proton-GE alone is 1.5 GB). python-inputs/python-steam: ProtonUp-Qt's deps.
LOCAL_APPS = ["waterfox-bin", "omasnap-bin", "protonup-qt", "localsend-bin"]

# AUR packages installed at install time with paru, one by one, never fatal. (pkg, label, fallback official packages)
# Only what cannot go into our ISO: proprietary software is downloaded from its maker onto the user's machine.
AUR_APPS = [
    # Proton-GE comes from the AUR at install time: on the ISO it pushed the file over GitHub's 2 GiB release limit
    ("proton-ge-custom-bin", "Proton-GE, Proton with extra game fixes", []),
    ("claude-code", "Claude Code, the command-line tool (proprietary)", []),
]
# Built without compression: packing Proton-GE's 1.5 GB into a .zst took minutes with no visible progress.
UNCOMPRESSED = {"proton-ge-custom-bin"}
# Free space the new root needs before an app is built (Proton-GE: ~1.5 GB download, build tree and package); with
# less, the app is skipped and the finish screen says how to install it later.
AUR_SPACE_NEEDED = {"proton-ge-custom-bin": 4 * 1024 ** 3}
AUR_TIMEOUT = 20 * 60                  # seconds; an AUR step that takes longer is stopped and skipped, never hangs

# What opens what. Waterfox is the browser.
MIME_DEFAULTS = {
    "inode/directory": ["org.gnome.Nautilus.desktop"],
    "x-scheme-handler/http": ["waterfox.desktop"],
    "x-scheme-handler/https": ["waterfox.desktop"],
    "text/html": ["waterfox.desktop"],
    "application/pdf": ["org.gnome.Papers.desktop"],
    "application/zip": ["xarchiver.desktop"],
    "application/x-tar": ["xarchiver.desktop"],
    "application/gzip": ["xarchiver.desktop"],
    "application/x-7z-compressed": ["xarchiver.desktop"],
    "application/vnd.rar": ["xarchiver.desktop"],
}
for _kind, _app in (("image", "org.gnome.Loupe.desktop"), ("video", "io.github.celluloid_player.Celluloid.desktop"),
                    ("audio", "io.bassi.Amberol.desktop")):
    for _sub in {"image": ("png", "jpeg", "gif", "webp", "bmp", "svg+xml", "tiff"),
                 "video": ("mp4", "x-matroska", "webm", "x-msvideo", "quicktime", "mpeg"),
                 "audio": ("mpeg", "flac", "ogg", "x-wav", "mp4", "x-opus+ogg", "aac")}[_kind]:
        MIME_DEFAULTS[f"{_kind}/{_sub}"] = [_app]


def enable_multilib(conf: str) -> str:
    """pacman.conf with the [multilib] repository switched on (Steam and the lib32-* graphics libraries live there).
    Handles a commented-out block, an already enabled block, and a file with no multilib block at all."""
    if re.search(r"^\[multilib\]", conf, re.M):
        return conf
    new, n = re.subn(r"^#\[multilib\]\n#Include = (\S+)", r"[multilib]\nInclude = \1", conf, count=1, flags=re.M)
    if n:
        return new
    return conf.rstrip("\n") + "\n\n[multilib]\nInclude = /etc/pacman.d/mirrorlist\n"


def render_mimeapps() -> str:
    lines = ["[Default Applications]"]
    lines += [f"{mime}={';'.join(apps)};" for mime, apps in sorted(MIME_DEFAULTS.items())]
    return "\n".join(lines) + "\n"


SUDO_INSTALL_FILE = "/etc/sudoers.d/99-tapkich-install"
SUDO_INSTALL_RULE = "%wheel ALL=(ALL:ALL) NOPASSWD: ALL\n"


def retry_hint(pkg: str) -> str:
    """What to run later, on the installed system, for an AUR app that did not install."""
    if pkg == "proton-ge-custom-bin":
        return "paru -S proton-ge-custom-bin (or install Proton-GE from ProtonUp-Qt)"
    return f"paru -S {pkg}"


def paru_command(pkg: str, rebuild: bool = False) -> str:
    """paru as the new user. rebuild: build the package again even though it is installed (it was built against an
    older Qt); --needed would skip it."""
    # No --sudoloop: it runs `sudo -v`, which asks for a password even with a NOPASSWD rule (found in the VM test).
    env = "PKGEXT=.pkg.tar " if pkg in UNCOMPRESSED else ""
    return f"{env}paru -S --noconfirm --skipreview {'--rebuild' if rebuild else '--needed'} {pkg}"


# ---- AUR packages from the ISO: rebuilt only when needed ----------------------------------------------------------
# The ISO carries AUR packages built before the install, while the official packages (Qt 6 included) come fresh from
# the mirrors. Two things make a prebuilt package wrong, and only these are rebuilt (compiling is slow):
#  - its AUR version is newer than the installed one;
#  - it uses Qt's private API and was built against another Qt than the installed one. Quickshell itself warns "built
#    against Qt X but the system has updated to Qt Y ... likely to cause crashes, you must rebuild the package"
#    (src/launch/command.cpp), and Caelestia's plugins link Qt_6_PRIVATE_API too (readelf, caelestia-shell 2.5.0).
QT_PACKAGES = ["qt6-m3shapes-git", "quickshell-git", "caelestia-shell"]      # in build order; rebuilt together
AUR_RPC = "https://aur.archlinux.org/rpc/v5/info"
REBUILD_TIMEOUT = 60 * 60              # seconds per package: quickshell-git compiles for many minutes


def parse_pacman_q(text: str) -> dict:
    """`pacman -Q` / `pacman -Qm` output ("name version" per line) -> {name: version}."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def qt_mismatch(version_text: str) -> bool:
    """`quickshell --version --verbose` prints "Qt: 6.12.0 (built against 6.12.0)". True when the two differ, the same
    test Quickshell uses for its own warning. Output that cannot be read counts as no mismatch (nothing is rebuilt)."""
    m = re.search(r"^Qt:\s*(\S+) \(built against (\S+)\)", version_text, re.M)
    return bool(m) and m.group(1) != m.group(2)


def fetch_aur_versions(names: list, urlopen=urllib.request.urlopen, timeout: float = 20) -> dict | None:
    """{name: version} for the names the AUR knows (AUR RPC v5). None when the AUR cannot be asked (offline, bad
    answer): the caller keeps the prebuilt packages."""
    if not names:
        return {}
    url = AUR_RPC + "?" + "&".join("arg[]=" + urllib.parse.quote(n) for n in names)
    try:
        with urlopen(urllib.request.Request(url, headers={"User-Agent": "tapkich-os"}), timeout=timeout) as r:
            data = json.load(r)
        if data.get("type") == "error":
            return None
        return {x["Name"]: x["Version"] for x in data["results"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def packages_to_refresh(installed: dict, aur: dict | None, qt_changed: bool, vercmp) -> list:
    """[(package, rebuild, reason)] to build at install time. installed: foreign packages with their versions (pacman
    -Qm); aur: AUR versions (None = unknown); vercmp(a, b) -> -1/0/1 like pacman's vercmp. Qt-linked packages come
    first, in QT_PACKAGES order, when the Qt differs; the others only when the AUR has a newer version."""
    out, seen = [], set()
    if qt_changed:
        for p in QT_PACKAGES:
            if p in installed:
                out.append((p, True, "built against another Qt"))
                seen.add(p)
    for p in sorted(installed):
        if p not in seen and aur and p in aur and vercmp(installed[p], aur[p]) < 0:
            out.append((p, False, f"newer on the AUR ({installed[p]} -> {aur[p]})"))
    return out
