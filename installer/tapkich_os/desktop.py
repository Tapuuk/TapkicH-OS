"""The TapkicH-OS desktop = stock Caelestia shell (caelestia.py) plus the user's keybinds, Waterfox as the browser and
the Gaming Mode button. This file only holds what Caelestia does not do: the packages a Hyprland desktop needs,
the login manager (SDDM, its own default look), and the per-user files (Caelestia overrides, default apps).
"""
from __future__ import annotations

from . import apps, caelestia, gaming

# Official Arch packages (extra). Caelestia's own installer adds the rest of its package list (terminal, fonts,
# file manager, theming ...); see caelestia.py.
DESKTOP_PACKAGES = [
    # compositor and portals
    "hyprland", "xorg-xwayland", "xdg-desktop-portal-hyprland", "xdg-desktop-portal-gtk", "polkit", "xdg-utils",
    # services Caelestia's shell talks to
    "upower", "power-profiles-daemon", "libnotify",
    # login screen
    "sddm", "xorg-server", "qt6-declarative", "qt6-svg",
    # sound, Bluetooth, brightness
    "pipewire", "pipewire-pulse", "pipewire-alsa", "wireplumber", "bluez", "bluez-utils", "brightnessctl",
    # graphics (open drivers; NVIDIA is a later, separate step)
    "mesa", "vulkan-icd-loader", "vulkan-radeon", "vulkan-intel",
    # fonts
    "noto-fonts", "noto-fonts-emoji",
]

# ---------------------------------------------------------------- login screen (SDDM, its default look)
# The hyprland package ships two login sessions. "Hyprland (uwsm-managed)" needs uwsm, which we do not install, and it
# sorts first, so picking it would loop back to the login screen: it is never extracted.
SESSION = "/usr/share/wayland-sessions/hyprland.desktop"
BROKEN_SESSIONS = ["usr/share/wayland-sessions/hyprland-uwsm.desktop", *gaming.HIDDEN_SESSIONS]


def hide_broken_sessions(pacman_conf: str) -> str:
    """pacman.conf that never installs the session files we cannot run."""
    lines = pacman_conf.split("\n")
    if any(line.startswith("NoExtract") and BROKEN_SESSIONS[0] in line for line in lines):
        return pacman_conf
    i = next((n for n, line in enumerate(lines) if line.strip() == "[options]"), None)
    if i is None:
        raise ValueError("pacman.conf has no [options] section")
    lines.insert(i + 1, "NoExtract = " + " ".join(BROKEN_SESSIONS) + "   # TapkicH-OS: login sessions we never offer (uwsm; ChimeraOS Steam without our return to the desktop)")
    return "\n".join(lines)


def render_sddm_state(user: str) -> str:
    """The login screen's memory, so the first login already has the right user and the working session selected."""
    return f"[Last]\nUser={user}\nSession={SESSION}\n"


SDDM_CONF = """# TapkicH-OS login screen (SDDM's own default theme)
[General]
InputMethod=

[Users]
RememberLastUser=true
RememberLastSession=true
"""


# ---------------------------------------------------------------- everything to install
def skel_files(layouts: str, keybinds: str = "tapkich", graphics_lua: str = "") -> dict:
    """Per-user files, relative to the home directory (written into /etc/skel before the user is created)."""
    return {
        **caelestia.skel_files(layouts, keybinds, graphics_lua),
        ".config/mimeapps.list": apps.render_mimeapps(),            # Waterfox opens web links
    }


def system_files() -> dict:
    """System files: absolute path -> (content, mode)."""
    return {"/etc/sddm.conf.d/10-tapkich.conf": (SDDM_CONF, 0o644), **gaming.system_files()}
