"""`tapkich install <name>`: the optional apps. One registry (APPS) feeds the install code and the help text.

Where each one comes from (checked 2026-10-24): Arch's extra repo with pacman (firefox, telegram), the AUR with paru
(1password, chrome, claude-desktop, chatgpt-desktop, linear, brave), or, for Decky Loader, the upstream GitHub release,
installed the way Decky's own installer does it (decky-installer cli/install_release.sh) but as plain code. Paru is
run as the user WITH its review prompt (no --skipreview, no --noconfirm): the user is at the terminal.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class App:
    name: str            # `tapkich install <name>`
    desc: str            # one line in the help
    source: str          # "pacman", "aur" or "decky" (own code)
    package: str = ""    # pacman / AUR package
    note: str = ""       # printed before installing


APPS = [
    App("1password", "1Password, the password manager (proprietary, AUR)", "aur", "1password"),
    App("claude-desktop", "Claude Desktop, the Claude app (proprietary, AUR)", "aur",
        "claude-desktop-extra"),
    App("chatgpt-desktop", "ChatGPT Desktop, OpenAI's own Linux app (proprietary, AUR)", "aur",
        "chatgpt-desktop"),
    App("firefox", "Firefox (Arch repos)", "pacman", "firefox"),
    App("chrome", "Google Chrome (proprietary, AUR)", "aur", "google-chrome"),
    App("decky-loader", "Decky Loader, plugins for Steam's Gaming Mode (from Decky's GitHub release)", "decky"),
    App("telegram", "Telegram Desktop (Arch repos)", "pacman", "telegram-desktop"),
    App("linear", "Linear (AUR, an unofficial Linux build of Linear's app)", "aur", "linear-bin",
        "Note: this is an unofficial build of Linear's app, packaged by an AUR user, not by Linear."),
    App("brave-debloated", "Brave browser with Leo AI, Rewards, Wallet, VPN, News and telemetry switched off (AUR)", "aur",
        "brave-bin"),
]
BY_NAME = {a.name: a for a in APPS}


def help_lines() -> str:
    """The 'Optional apps' lines of `tapkich help`, made from APPS."""
    return "\n".join(f"  tapkich install {a.name:<17} {a.desc}" for a in APPS)


# ---- Brave: our own managed policy (not the brave-debloater package) -----------------------------------------------
# Every key is in Brave's policy definitions (brave/brave-core, components/policy/resources/templates/policy_definitions/
# BraveSoftware/*.yaml, read 2026-10-24) except MetricsReportingEnabled, which is Chromium's (Miscellaneous/). The first
# nine match what the AUR package brave-debloater 1.2.0 sets for the same features; we write only these, none of its
# other choices (password manager, translate, DNS ...).
BRAVE_POLICY_FILE = "/etc/brave/policies/managed/tapkich-debloat.json"
BRAVE_POLICY = {
    "BraveAIChatEnabled": False,            # Leo, the AI chat
    "BraveRewardsDisabled": True,
    "BraveWalletDisabled": True,
    "BraveVPNDisabled": True,
    "BraveNewsDisabled": True,
    "BraveTalkDisabled": True,
    "BraveP3AEnabled": False,               # product analytics
    "BraveStatsPingEnabled": False,         # the usage ping
    "BraveWebDiscoveryEnabled": False,
    "MetricsReportingEnabled": False,       # Chromium's usage and crash reports
}


def brave_policy_json() -> str:
    return json.dumps(BRAVE_POLICY, indent=2) + "\n"


# ---- Decky Loader ---------------------------------------------------------------------------------------------------
DECKY_RELEASES = "https://api.github.com/repos/SteamDeckHomebrew/decky-loader/releases?per_page=30"
DECKY_UNIT = "/etc/systemd/system/plugin_loader.service"
DECKY_FLAG = ".steam/steam/.cef-enable-remote-debugging"                  # Steam's CEF remote debugging, relative to home
DECKY_FLAG_FLATPAK = ".var/app/com.valvesoftware.Steam/data/Steam/.cef-enable-remote-debugging"

# The service file of decky-loader's dist/plugin_loader-release.service (as of v3.3.0); {home} is the homebrew folder.
# It runs as root, exactly like upstream's: the loader needs it to manage plugins.
DECKY_SERVICE = """[Unit]
Description=SteamDeck Plugin Loader
After=network.target
[Service]
Type=simple
User=root
Restart=always
KillMode=process
TimeoutStopSec=15
ExecStart={home}/services/PluginLoader
WorkingDirectory={home}/services
Environment=UNPRIVILEGED_PATH={home}
Environment=PRIVILEGED_PATH={home}
Environment=LOG_LEVEL=INFO
[Install]
WantedBy=multi-user.target
"""


def decky_asset_name(machine: str | None = None) -> str:
    return "PluginLoader-arm64" if (machine or platform.machine()) in ("aarch64", "arm64") else "PluginLoader"


def pick_decky_release(releases: list, asset_name: str):
    """(tag, download url, sha256 or None) of the newest stable release that has the asset; None if there is none."""
    for rel in releases:
        if rel.get("prerelease") or rel.get("draft"):
            continue
        for asset in rel.get("assets", []):
            if asset.get("name") == asset_name:
                digest = asset.get("digest") or ""
                return rel["tag_name"], asset["browser_download_url"], (digest[7:] if digest.startswith("sha256:") else None)
    return None


def _fetch(url: str, urlopen, headers=None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "tapkich-os", **(headers or {})})
    with urlopen(req, timeout=120) as r:
        return r.read()


def install_decky(home: str, run=subprocess.run, urlopen=urllib.request.urlopen, machine: str | None = None,
                  out=print) -> int:
    services = os.path.join(home, "homebrew", "services")
    binary = os.path.join(services, "PluginLoader")
    if os.path.exists(binary) and os.path.exists(DECKY_UNIT):
        out("Decky Loader is already installed. It runs in Gaming Mode (Steam's gamepad interface).")
        return 0
    if not os.path.isdir(os.path.dirname(os.path.join(home, DECKY_FLAG))):
        out("Decky Loader needs Steam: start Steam once so that ~/.steam/steam exists, then run this again.")
        return 1
    try:
        picked = pick_decky_release(json.loads(_fetch(DECKY_RELEASES, urlopen, {"Accept": "application/vnd.github+json"})),
                                    decky_asset_name(machine))
        if not picked:
            out("No stable Decky Loader release with a build for this machine was found.")
            return 1
        tag, url, digest = picked
        out(f"Downloading Decky Loader {tag} ...")
        data = _fetch(url, urlopen)
    except (OSError, ValueError, KeyError, TypeError) as e:
        out(f"Could not download Decky Loader from GitHub: {e}")
        return 1
    if digest:
        if hashlib.sha256(data).hexdigest() != digest:
            out("The download does not match the checksum GitHub lists for it. Nothing was installed.")
            return 1
        out("Checksum (sha256) matches the one GitHub lists for the release file.")
    else:
        out("GitHub lists no checksum for this file, so it could not be verified.")
    homebrew = os.path.join(home, "homebrew")
    os.makedirs(os.path.join(homebrew, "plugins"), exist_ok=True)
    os.makedirs(services, exist_ok=True)
    run(["sudo", "systemctl", "stop", "plugin_loader"], check=False)                   # a running one blocks the new file
    tmp = binary + ".new"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o755)
    os.replace(tmp, binary)
    with open(os.path.join(services, ".loader.version"), "w") as f:
        f.write(tag + "\n")
    with tempfile.TemporaryDirectory() as d:
        unit = os.path.join(d, "plugin_loader.service")
        with open(unit, "w") as f:
            f.write(DECKY_SERVICE.format(home=homebrew))
        for cmd in (["sudo", "install", "-m", "644", unit, DECKY_UNIT], ["sudo", "systemctl", "daemon-reload"],
                    ["sudo", "systemctl", "enable", "--now", "plugin_loader"]):
            if run(cmd).returncode != 0:
                out(f"Failed: {' '.join(cmd)}")
                return 1
    for rel in (DECKY_FLAG, DECKY_FLAG_FLATPAK):                       # the flatpak Steam's folder only if it exists
        path = os.path.join(home, rel)
        if rel == DECKY_FLAG or os.path.isdir(os.path.dirname(path)):
            open(path, "a").close()
    out(f"Decky Loader {tag} is installed. It runs in Gaming Mode (Steam's gamepad interface): "
        "switch with `tapkich gamemode`, then open Decky from the quick access menu (the ... button).")
    return 0


# ---- the command ---------------------------------------------------------------------------------------------------
def is_installed(pkg: str, run=subprocess.run) -> bool:
    return run(["pacman", "-Q", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _install_package(app: App, run, out) -> int:
    if is_installed(app.package, run):
        out(f"{app.name} is already installed.")
        return 0
    if app.note:
        out(app.note)
    cmd = (["sudo", "pacman", "-S", "--needed", app.package] if app.source == "pacman"
           else ["paru", "-S", "--needed", app.package])
    return run(cmd).returncode


def install_brave_policy(run, out) -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "tapkich-debloat.json")
        with open(tmp, "w") as f:
            f.write(brave_policy_json())
        rc = run(["sudo", "install", "-Dm644", tmp, BRAVE_POLICY_FILE]).returncode
    out(f"Brave's AI chat, Rewards, Wallet, VPN, News, Talk and telemetry are switched off ({BRAVE_POLICY_FILE})."
        if rc == 0 else f"Could not write {BRAVE_POLICY_FILE}.")
    return rc


def usage_text() -> str:
    return "Optional apps (tapkich install <name>):\n" + help_lines()


def install(args: list, home: str | None = None, run=subprocess.run, urlopen=urllib.request.urlopen, out=print) -> int:
    """`tapkich install [name]`. No name: the list. Unknown name: the list, exit 2."""
    if not args:
        out(usage_text())
        return 0
    if len(args) != 1 or args[0] not in BY_NAME:
        out(f"tapkich install: unknown app {' '.join(args)!r}\n\n" + usage_text())
        return 2
    app = BY_NAME[args[0]]
    if app.source == "decky":
        return install_decky(home or os.path.expanduser("~"), run, urlopen, out=out)
    rc = _install_package(app, run, out)
    if rc != 0:
        out(f"{app.name} could not be installed.")
        return rc
    if app.name == "brave-debloated":                 # also when Brave was there already: the policy is ours to keep
        rc = install_brave_policy(run, out)
    return rc
