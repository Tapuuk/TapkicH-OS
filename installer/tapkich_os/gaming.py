"""Gaming Mode: a separate login session that runs Steam's gamepad UI in gamescope, directly on the GPU, like SteamOS.
The desktop (Caelestia on Hyprland) stays the default; Gaming Mode is entered from the power-menu button and left with
Steam's own "Switch to Desktop".

The session itself is ChimeraOS's gamescope-session-plus + gamescope-session-steam (AUR, MIT, built into the ISO's
local repo by iso/build-aur.sh); gamescope is in Arch's extra repo. What we add is only the switching:

  desktop --(power menu: `tapkich gamemode`)--> session-switch gaming: SDDM's next automatic login = Gaming Mode,
          then the desktop session ends and SDDM logs straight into Gaming Mode (no password).
  Gaming Mode starts --> session-switch desktop: the next automatic login is the desktop again, whatever happens next.
          Steam's "Switch to Desktop" (steamos-session-select -> our os-session-select) closes Steam; a crash or a
          gamescope that cannot start ends the session too, and SDDM logs back into the desktop. Never a loop.
  desktop after Gaming Mode --> session-switch clear: logging out or rebooting shows the normal login screen again.

The automatic login lives in one SDDM file that only the root helper writes (sudo rule for exactly its three uses),
and it is removed at every boot (session-reset), which also puts SDDM's remembered session and the power profile
back, so the computer always boots to the normal login screen with the desktop preselected.
"""
from __future__ import annotations

OFFICIAL_PACKAGES = ["gamescope", "ttf-dejavu"]      # ttf-dejavu: Steam's first start in gamescope looks for DejaVuSans
LOCAL_PACKAGES = ["gamescope-session-git", "gamescope-session-steam-git"]

GAMING_SESSION = "tapkich-gaming.desktop"
RETURN_SESSION = "tapkich-desktop.desktop"           # hidden from the login screen; only the automatic return uses it
SESSIONS_DIR = "/usr/share/wayland-sessions"
# ChimeraOS's own "Steam Big Picture" session would skip our switching (no return to the desktop): never extracted.
HIDDEN_SESSIONS = ["usr/share/wayland-sessions/gamescope-session-steam.desktop"]

SWITCH = "/usr/lib/tapkich/session-switch"
GAMING_WRAPPER = "/usr/lib/tapkich/gaming-session"
RETURN_WRAPPER = "/usr/lib/tapkich/desktop-session"
OS_SESSION_SELECT = "/usr/lib/os-session-select"     # steamos-session-select (gamescope-session-steam) runs this
AUTOLOGIN_CONF = "/etc/sddm.conf.d/zz-tapkich-next-session.conf"
SUDOERS = "/etc/sudoers.d/50-tapkich-session"
RESET_UNIT = "tapkich-session-reset.service"
SESSION_RESET = "/usr/lib/tapkich/session-reset"
SDDM_STATE = "/var/lib/sddm/state.conf"               # SDDM's memory of the last login: [Last] User= and Session=
DESKTOP_SESSION_PATH = f"{SESSIONS_DIR}/hyprland.desktop"       # what the installer writes there (desktop.SESSION)

SWITCH_SCRIPT = f"""#!/bin/bash
# TapkicH-OS: which session SDDM logs into next, without a password (Gaming Mode <-> desktop).
# Run through sudo by the logged-in user (rule: {SUDOERS}); the user is taken from sudo, never from arguments.
set -eu
export PATH=/usr/bin
unset BASH_ENV ENV
conf={AUTOLOGIN_CONF}
case "${{1:-}}" in
  gaming)  session={GAMING_SESSION} ;;
  desktop) session={RETURN_SESSION} ;;
  clear)   rm -f "$conf"; exit 0 ;;
  *)       echo "usage: session-switch gaming|desktop|clear" >&2; exit 2 ;;
esac
user=${{SUDO_USER:-}}
if ! [[ $user =~ ^[a-z_][a-z0-9_-]*$ ]] || ! uid=$(id -u "$user" 2>/dev/null) || (( uid < 1000 )); then
  echo "session-switch: run it through sudo as a normal user" >&2
  exit 1
fi
tmp=$(mktemp /etc/sddm.conf.d/.tapkich-next.XXXXXX)
printf '[Autologin]\\nUser=%s\\nSession=%s\\nRelogin=true\\n' "$user" "$session" > "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" "$conf"
"""

SUDOERS_RULE = f"""# TapkicH-OS Gaming Mode: switching between the desktop and Gaming Mode needs no password (only these three uses)
Defaults!{SWITCH} env_reset,secure_path="/usr/bin"
%wheel ALL=(root) NOPASSWD: {SWITCH} gaming, {SWITCH} desktop, {SWITCH} clear
"""

POWER_SAVE = "${XDG_CACHE_HOME:-$HOME/.cache}/tapkich/power-profile"   # the desktop's power profile, while gaming
SESSION_ENV = "/etc/gamescope-session-plus/sessions.d/steam"          # read after ChimeraOS's own steam settings

GAMING_WRAPPER_SCRIPT = f"""#!/bin/bash
# TapkicH-OS Gaming Mode session: Steam's gamepad UI in gamescope (ChimeraOS's gamescope-session-plus).
# First make the desktop the next automatic login: however this session ends (Steam's "Switch to Desktop",
# a crash, gamescope unable to start), SDDM goes back to the desktop instead of starting Gaming Mode again.
sudo -n {SWITCH} desktop || echo "tapkich: could not set the return to the desktop" >&2
# Power: "performance" while gaming on mains power (laptops on battery keep their profile); the desktop's profile is
# saved and put back when the desktop starts again (desktop-session).
on_mains() {{
  local s found=0
  for s in /sys/class/power_supply/*; do
    [[ -r $s/type ]] || continue
    case $(<"$s/type") in
      Mains) found=1; [[ $(<"$s/online") == 1 ]] && return 0 ;;
      Battery) found=1 ;;
    esac
  done
  ((found == 0))                                      # no battery and no charger reported: a desktop PC
}}
if command -v powerprofilesctl >/dev/null && on_mains; then
  current=$(powerprofilesctl get 2>/dev/null)
  if [[ -n $current && $current != performance ]] && powerprofilesctl set performance 2>/dev/null; then
    mkdir -p "$(dirname "{POWER_SAVE}")" && echo "$current" > "{POWER_SAVE}"
  fi
fi
# The user's service manager outlives a quick session change and still holds the desktop's display variables
# (Hyprland exports them). gamescope runs as a user service: with WAYLAND_DISPLAY set it tries to run nested inside a
# desktop that is gone ("Failed to connect to wayland socket", found in the VM) instead of taking the screen.
systemctl --user unset-environment WAYLAND_DISPLAY DISPLAY XAUTHORITY HYPRLAND_INSTANCE_SIGNATURE HYPRLAND_CMD \\
    XDG_CURRENT_DESKTOP XDG_SESSION_DESKTOP QT_QPA_PLATFORM GDK_BACKEND SDL_VIDEODRIVER 2>/dev/null
unset WAYLAND_DISPLAY DISPLAY HYPRLAND_INSTANCE_SIGNATURE
exec /usr/bin/gamescope-session-plus steam
"""

RETURN_WRAPPER_SCRIPT = f"""#!/bin/bash
# TapkicH-OS: the desktop after Gaming Mode. Next time it is the normal login screen again, and what the gaming
# session left in the user's service manager (it can outlive a quick session change) is cleared, so the desktop's
# portals (screen sharing, file dialogs) start fresh. Then the usual Hyprland start.
sudo -n {SWITCH} clear || true
saved="{POWER_SAVE}"
if [[ -f $saved ]]; then                              # the power profile from before Gaming Mode
  powerprofilesctl set "$(<"$saved")" 2>/dev/null
  rm -f "$saved"
fi
systemctl --user stop gamescope-session-plus@steam.service 2>/dev/null
systemctl --user unset-environment XDG_DESKTOP_PORTAL_DIR DISPLAY XAUTHORITY XDG_SESSION_TYPE XDG_CURRENT_DESKTOP DESKTOP_SESSION 2>/dev/null
systemctl --user stop xdg-desktop-portal.service xdg-desktop-portal-hyprland.service xdg-desktop-portal-gtk.service 2>/dev/null
exec /usr/bin/start-hyprland
"""

OS_SESSION_SELECT_SCRIPT = f"""#!/bin/bash
# TapkicH-OS: what Steam's session switching runs (steamos-session-select <session>, from gamescope-session-steam).
# Valve's own script treats no argument as "gamescope". Steam passes:
#   plasma, plasma-wayland, plasma-x11, plasma-wayland-persistent, plasma-x11-persistent, desktop
#       -> "Switch to Desktop": the desktop is the next automatic login, Steam closes, the session ends.
#   gamescope (or nothing)  -> back to Gaming Mode: from the desktop like the power-menu button; inside Gaming
#       Mode there is nothing to do.
# Anything else is treated as "to the desktop" (the safe direction: never a loop into Gaming Mode).
in_gaming_mode() {{ pgrep -u "$(id -u)" -x gamescope >/dev/null || pgrep -u "$(id -u)" -f "gamescope-session-plus" >/dev/null; }}
case "${{1:-gamescope}}" in
  gamescope|gamescope-wayland)
    in_gaming_mode && exit 0
    exec tapkich gamemode ;;
esac
# already on the desktop: nothing to switch (re-arming the automatic login here would log the user straight back in
# after a later logout; seen in the VM test)
in_gaming_mode || exit 0
sudo -n {SWITCH} desktop || true
# only a running Steam: `steam -shutdown` would START Steam when it is not running (seen in the VM test)
if pgrep -u "$(id -u)" -x steam >/dev/null; then
  exec steam -shutdown
fi
"""

# Gaming Mode settings on top of ChimeraOS's steam session (VRR, scaling/FSR, tearing and the frame limiter are
# already Steam's own switches there). HDR: gamescope offers it, Steam shows its HDR switch only on HDR displays.
SESSION_ENV_FILE = """# TapkicH-OS Gaming Mode settings (read after /usr/share/gamescope-session-plus/sessions.d/steam).
# VRR, FSR / scaling, the frame limiter, refresh rate and tearing are switched in Steam's own Quick Access menu.
# HDR: let gamescope offer HDR; Steam's HDR switch appears only on an HDR display.
ENABLE_GAMESCOPE_HDR=1
# Hybrid graphics (laptops): leave VULKAN_ADAPTER unset, gamescope draws on the GPU wired to the screen and
# switcherooctl (switcheroo-control) starts Steam on the NVIDIA GPU. Set e.g. VULKAN_ADAPTER=10de:2786 to force one.
"""

GAMING_SESSION_FILE = f"""[Desktop Entry]
Name=Gaming Mode (Steam)
Comment=Steam's console-style interface in gamescope, like SteamOS. "Switch to Desktop" in Steam's power menu returns.
Exec={GAMING_WRAPPER}
Type=Application
DesktopNames=gamescope
"""

RETURN_SESSION_FILE = f"""[Desktop Entry]
Name=Hyprland (back from Gaming Mode)
Comment=Used only for the automatic return from Gaming Mode
Exec={RETURN_WRAPPER}
Type=Application
DesktopNames=Hyprland
NoDisplay=true
"""

SESSION_RESET_SCRIPT = f"""#!/bin/bash
# TapkicH-OS, at every boot before the login screen: start from the normal state again.
#  1. no pending automatic login (a one-shot Gaming Mode switch that never got used);
#  2. SDDM's remembered session is the desktop: it records the session after every login, so a shutdown from
#     Gaming Mode would otherwise preselect Gaming Mode at the next login. Only our two session names are replaced;
#     any other remembered session stays;
#  3. the power profile saved by Gaming Mode is put back (power-profiles-daemon keeps "performance" across a reboot).
set -u
export PATH=/usr/bin
unset BASH_ENV ENV
rm -f {AUTOLOGIN_CONF}
state={SDDM_STATE}
if [[ -f $state ]]; then
  # same file, same owner: the content is rewritten in place
  new=$(sed -E 's#^(Session=)(.*/)?({GAMING_SESSION}|{RETURN_SESSION})[[:space:]]*$#\\1{DESKTOP_SESSION_PATH}#' "$state") &&
    [[ -n $new ]] && printf '%s\\n' "$new" > "$state"
fi
# the profile goes back only while the daemon runs; otherwise the file stays for the desktop-session wrapper
if systemctl is-active --quiet power-profiles-daemon.service; then
  for saved in /home/*/.cache/tapkich/power-profile; do
    [[ -f $saved && ! -L $saved ]] || continue
    uid=$(stat -c %u "$saved") && (( uid >= 1000 )) || continue
    profile=$(head -c 32 "$saved")
    case $profile in
      power-saver|balanced|performance) powerprofilesctl set "$profile" 2>/dev/null && rm -f "$saved" ;;
      *) rm -f "$saved" ;;
    esac
  done
fi
exit 0
"""

RESET_UNIT_FILE = f"""[Unit]
Description=TapkicH-OS: boot to the normal login screen (forget a pending Gaming Mode switch and its leftovers)
Before=display-manager.service
Wants=power-profiles-daemon.service
After=power-profiles-daemon.service

[Service]
Type=oneshot
ExecStart={SESSION_RESET}

[Install]
WantedBy=graphical.target
"""


def system_files() -> dict:
    """Absolute path -> (content, mode)."""
    return {
        SWITCH: (SWITCH_SCRIPT, 0o755),
        GAMING_WRAPPER: (GAMING_WRAPPER_SCRIPT, 0o755),
        RETURN_WRAPPER: (RETURN_WRAPPER_SCRIPT, 0o755),
        OS_SESSION_SELECT: (OS_SESSION_SELECT_SCRIPT, 0o755),
        f"{SESSIONS_DIR}/{GAMING_SESSION}": (GAMING_SESSION_FILE, 0o644),
        f"{SESSIONS_DIR}/{RETURN_SESSION}": (RETURN_SESSION_FILE, 0o644),
        SUDOERS: (SUDOERS_RULE, 0o440),
        SESSION_ENV: (SESSION_ENV_FILE, 0o644),
        SESSION_RESET: (SESSION_RESET_SCRIPT, 0o755),
        f"/etc/systemd/system/{RESET_UNIT}": (RESET_UNIT_FILE, 0o644),
    }


def enter_commands(in_hyprland: bool, session_id: str | None) -> list:
    """What `tapkich gamemode` runs after the switch: end this desktop session the normal way (like logging out), so
    SDDM sees the session finish and logs into Gaming Mode. Caelestia's Hyprland uses a Lua config, where dispatchers
    are Lua (`hl.dsp.exit()`, as in Hyprland's own default config). Not `loginctl terminate-session`: that kills SDDM's
    helper too, SDDM records a crash and then shows nothing at all (found in the VM, 2026-10-19)."""
    if in_hyprland:
        return [["hyprctl", "dispatch", "hl.dsp.exit()"]]
    if session_id:                                    # not inside Hyprland (should not happen): last resort
        return [["loginctl", "terminate-session", session_id]]
    return []
