"""`tapkich`: the one command for tweaking a TapkicH-OS system (like Omarchy's `omarchy`).

New areas are added as subcommands here. Run `tapkich help`.
"""
from __future__ import annotations

import os
import subprocess
import sys

from . import appinstall, caelestia

HELP_HEAD = """tapkich: tweak your TapkicH-OS system

  tapkich update [--yes]        Update everything: TapkicH-OS, system packages (pacman), AUR packages (paru), Flatpak
  tapkich doctor                Check the graphics driver, Vulkan, Gaming Mode, Secure Boot, Flathub and disk space
  tapkich gamemode              Switch to Gaming Mode: Steam's console-style session, like SteamOS (also the gamepad
                                button in Caelestia's power menu). Steam's "Switch to Desktop" comes back.
  tapkich keys                  Your keyboard shortcuts, read from your current config
  tapkich install [<app>]       Install an optional app (the list is below); without a name it prints the list
  tapkich secureboot status     Secure Boot, and what anti-cheat (Epic EAC for Linux) needs: TPM, kernel settings
  tapkich secureboot setup      Turn Secure Boot on with our own keys, keeping Microsoft's (Windows keeps starting)
  tapkich restore windows [--check|--yes]   Put the firmware's "Windows Boot Manager" boot entry back if it went missing
  tapkich bootloader timeout [<seconds>]   Show or set the boot menu countdown (0 to 60 seconds, or `no` to wait for you)
  tapkich help                  This help (also: tapkich --help, tapkich -h, or just tapkich)

Optional apps:
"""

HELP_TAIL = """
The desktop is Caelestia shell: its settings, wallpapers and colours are in its own menus and the `caelestia` command.
"""

HELP = HELP_HEAD + appinstall.help_lines() + "\n" + HELP_TAIL


def _notify(text: str) -> None:
    subprocess.run(["notify-send", "-a", "TapkicH-OS", "Gaming Mode", text], check=False)


def gamemode() -> int:
    """Switch to the Gaming Mode session (gaming.py): the next automatic login becomes Gaming Mode, then this desktop
    session ends. Open windows close as with logging out."""
    import shutil
    from . import gaming
    if not shutil.which("steam"):
        _notify("Steam is not installed.")
        return 1
    if not (os.path.isfile(f"{gaming.SESSIONS_DIR}/{gaming.GAMING_SESSION}") and shutil.which("gamescope-session-plus")):
        _notify("The Gaming Mode session is not installed.")
        return 1
    if subprocess.run(["sudo", "-n", gaming.SWITCH, "gaming"]).returncode != 0:
        _notify("Could not switch to Gaming Mode (session switch refused).")
        return 1
    for cmd in gaming.enter_commands(bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")), os.environ.get("XDG_SESSION_ID")):
        subprocess.run(cmd, check=False)
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("help", "-h", "--help"):
        print(HELP)
        return 0
    cmd = args[0]
    if cmd == "install":
        return appinstall.install(args[1:])
    if cmd == "gamemode":
        return gamemode()
    if cmd == "keys":
        print(caelestia.key_list())
        return 0
    if cmd == "update":
        from . import update
        return update.main(args[1:])
    if cmd == "doctor":
        from . import doctor
        return doctor.main()
    if cmd == "secureboot_setup" or (cmd == "secureboot" and args[1:2] in (["setup"], ["refresh"])):
        sub = ["setup", *args[1:]] if cmd == "secureboot_setup" else args[1:]
        if os.geteuid() != 0:                         # the keys and the boot partition belong to root
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            os.execvp("sudo", ["sudo", "env", f"PYTHONPATH={here}", sys.executable, "-P", "-m", "tapkich_os.command",
                               "secureboot", *sub])
        from . import secureboot_setup
        return secureboot_setup.main(sub)
    if cmd == "restore":
        if args[1:2] != ["windows"]:
            print("usage: tapkich restore windows [--check] [--yes] [--disk /dev/xxx --part N]\n"
                  "  windows   put the firmware's \"Windows Boot Manager\" boot entry back if it went missing")
            return 2
        if os.geteuid() != 0:                         # reading the disks and writing the boot list need root
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            os.execvp("sudo", ["sudo", "env", f"PYTHONPATH={here}", sys.executable, "-P", "-m", "tapkich_os.command",
                               *args])
        from . import restore
        return restore.main(args[2:])
    if cmd == "bootloader":
        from . import bootloader
        if args[1:2] != ["timeout"]:
            return bootloader.main(args[1:])          # prints the usage
        if os.geteuid() != 0:                         # the boot partition is root-only, and so is the refresh
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            os.execvp("sudo", ["sudo", "env", f"PYTHONPATH={here}", sys.executable, "-P", "-m", "tapkich_os.command",
                               *args])
        return bootloader.main(args[1:])
    if cmd == "secureboot" and args[1:2] in (["status"], []):
        from .secureboot import evaluate, read_state, render
        print(render(evaluate(read_state("/"))))
        return 0
    print(f"tapkich: unknown command {' '.join(args)!r}\n")
    print(HELP)
    return 2


if __name__ == "__main__":
    sys.exit(main())
