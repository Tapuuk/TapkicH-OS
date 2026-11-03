"""`tapkich secureboot setup`: Secure Boot with our own keys, Windows-compatible.

How it works (decided 2026-10-19):
  - sbctl creates our own Secure Boot keys and enrolls them together WITH Microsoft's (`sbctl enroll-keys -m`), so
    Windows' boot manager and graphics-card firmware (signed by Microsoft) keep starting with Secure Boot on.
  - The firmware checks Limine's EFI file: we sign it (both copies on our ESP) with our key.
  - Limine checks everything it loads: the BLAKE2b hash of limine.conf is enrolled into the signed Limine binary
    (`limine enroll-config`), and every file in limine.conf carries its own hash (`path#hash`). So a changed kernel,
    initramfs or config is refused. The kernel is also signed with our key (useful if it is ever started directly).
  - Windows is chainloaded through the firmware (protocol efi), which checks Microsoft's signature itself.
  - Updates stay signed: tapkich-boot-update (bootupdate.py) redoes the hashes, the enrolled config hash and the
    signatures after every pacman transaction, every mkinitcpio run and kernel-install, and at shutdown.
Keys can only be enrolled while the firmware is in Setup Mode; firmware never allows that silently from the OS.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from . import bootupdate

STATE_FILE = "/etc/tapkich-os/secureboot.conf"
BOOT = "/boot"
LIMINE_CONFS = bootupdate.LIMINE_CONFS                     # relative to /boot (our ESP)
LIMINE_EFIS = bootupdate.LIMINE_EFIS
with_hashes, without_hashes, blake2b_file = bootupdate.with_hashes, bootupdate.without_hashes, bootupdate.blake2b_file


def missing_hashes(conf: str) -> list:
    """Files in the config that have no hash (Limine would refuse to boot them under Secure Boot)."""
    return ["/" + rel for key, rel, h in bootupdate.file_lines(conf) if not h and key not in ("wallpaper", "term_font")]


def read_state_file(path: str = STATE_FILE) -> dict:
    try:
        with open(path) as f:
            return dict(l.strip().split("=", 1) for l in f if "=" in l and not l.startswith("#"))
    except OSError:
        return {}


def render_state_file(st: dict) -> str:
    return ("# TapkicH-OS Secure Boot (written by `tapkich secureboot setup`; `tapkich secureboot refresh` keeps it)\n"
            + "".join(f"{k}={v}\n" for k, v in sorted(st.items())))


# ---------------------------------------------------------------- doing it (root only)

def _run(cmd: list, check: bool = True, **kw) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed (exit {p.returncode}): {(p.stderr or p.stdout).strip()[-400:]}")
    return p


def sbctl_has_keys() -> bool:
    return bootupdate.has_keys("/")


def refresh(boot: str = BOOT, state_path: str = STATE_FILE, force: bool = False, log=print) -> bool:
    """Hash, enroll and sign everything Limine starts: tapkich-boot-update's job (bootupdate.update), kept under this
    name for `tapkich secureboot setup` and `refresh`. `boot` is <root>/boot. Raises RuntimeError on failure."""
    root = os.path.dirname(boot.rstrip("/")) or "/"
    bootupdate.update(bootupdate.Machine(root, log=log, sign=None if root == "/" else False), force=force)
    return True


def _ask(question: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"  {question} {hint} ").strip().lower()
    except EOFError:
        return False                    # nobody answered (piped or scripted run): never change the firmware unasked
    return default if not answer else answer in ("y", "yes")


# Linux's efivarfs marks most firmware variables immutable (chattr +i); writing the key databases needs -i first.
EFIVARS = "/sys/firmware/efi/efivars"
KEY_VARIABLES = ("PK-8be4df61-93ca-11d2-aa0d-00e098032b8c", "KEK-8be4df61-93ca-11d2-aa0d-00e098032b8c",
                 "db-d719b2cb-3d3a-4596-a3bc-dad00e67656f", "dbx-d719b2cb-3d3a-4596-a3bc-dad00e67656f")


_firmware_touched = False          # set once setup starts writing firmware variables (for an accurate Ctrl-C message)


class EnrollError(RuntimeError):
    """sbctl could not enroll the keys (the firmware keys are unchanged)."""


def _unlock_key_variables(efivars: str | None = None, log=print) -> list:
    """`chattr -i` on the existing PK, KEK, db and dbx variables. Returns the files that were unlocked."""
    efivars = efivars or EFIVARS
    files = [os.path.join(efivars, v) for v in KEY_VARIABLES if os.path.exists(os.path.join(efivars, v))]
    for f in files:
        _run(["chattr", "-i", f], check=False)
    if files:
        log(f"  unlocked {len(files)} firmware key variables for writing (chattr -i)")
    return files


def _lock_key_variables(files: list, log=print) -> None:
    """Put `chattr +i` back on the unlocked variables that still exist (the firmware default)."""
    again = [f for f in files if os.path.exists(f)]
    for f in again:
        _run(["chattr", "+i", f], check=False)
    if again:
        log("  locked the firmware key variables again (chattr +i)")


RESET_STEPS = """  What to do:
    1. Restart into the firmware setup. On a Lenovo ThinkPad press F1 at power-on, then open
       Security > Secure Boot and choose "Reset to Setup Mode" or "Clear All Secure Boot Keys".
       On other computers look for "Clear keys", "Setup Mode" or "Custom mode" in the Secure Boot settings.
       Do NOT choose "Restore factory keys".
    2. Save and exit, boot TapkicH-OS, then run `sudo tapkich secureboot setup` again.
  Clearing the keys also removes Microsoft's from the firmware for a moment; this command puts them back together
  with ours, so Windows keeps starting with Secure Boot on."""

NOT_SETUP_MODE = """  Your firmware is not in Setup Mode, so new Secure Boot keys cannot be added from here (by design: firmware never
  lets the running system change its keys silently).

""" + RESET_STEPS + """

  Until then nothing is wrong: TapkicH-OS keeps starting normally with Secure Boot off.
"""

ENROLL_FAILED = """  The keys could not be enrolled. If the firmware is not in Setup Mode (or does not allow it from the running
  system), do this:

""" + RESET_STEPS + """

  Until then nothing is wrong: TapkicH-OS keeps starting normally with Secure Boot off.
"""

BITLOCKER_WARNING = """  If Windows on this computer uses BitLocker (device encryption), changing Secure Boot keys makes Windows ask for
  the BitLocker RECOVERY KEY at its next start. Have it ready first (https://account.microsoft.com/devices/recoverykey)
  or suspend BitLocker in Windows before you change anything in the firmware.
"""


def setup(assume_yes: bool = False, from_installer: bool = False, log=print) -> int:
    global _firmware_touched
    _firmware_touched = False
    from .secureboot import read_state
    if os.geteuid() != 0:
        log("tapkich secureboot setup must run as root (sudo tapkich secureboot setup).")
        return 1
    st = read_state("/")
    if not st["uefi"]:
        log("  This computer started in legacy BIOS mode: Secure Boot needs UEFI.")
        return 1
    if not shutil.which("sbctl"):
        log("  Installing sbctl (the Secure Boot key tool) ...")
        if subprocess.run(["pacman", "-S", "--needed", "--noconfirm", "sbctl"]).returncode != 0:
            log("  Could not install sbctl.")
            return 1
    ours = st["tapkich_key_in_db"] or read_state_file().get("enrolled") == "yes"
    if not st["setup_mode"] and not ours:
        log("Secure Boot setup\n")
        log(NOT_SETUP_MODE)
        log(BITLOCKER_WARNING)
        if not assume_yes and not from_installer and _ask("Restart into the firmware settings now?"):
            subprocess.run(["systemctl", "reboot", "--firmware-setup"])
        return 1
    log("Secure Boot setup\n")
    if st["setup_mode"]:
        log(BITLOCKER_WARNING)
        if not assume_yes and not _ask("Create our keys and enroll them together with Microsoft's now?", default=True):
            log("  Nothing was changed.")
            return 1
    try:
        if not sbctl_has_keys():
            log("  creating our Secure Boot keys (kept in /var/lib/sbctl, readable by root only) ...")
            _run(["sbctl", "create-keys"])
        log("  signing the boot files (before touching the firmware) ...")
        refresh(force=True, log=log)
        if st["setup_mode"]:
            log("  enrolling our keys together with Microsoft's ...")
            _firmware_touched = True
            unlocked = _unlock_key_variables(log=log)
            try:
                p = _run(["sbctl", "enroll-keys", "--microsoft"], check=False)
                if p.returncode != 0 and "brick" in (p.stdout + p.stderr).lower():
                    # sbctl asks for this when it cannot read the TPM event log to check option ROMs. With Microsoft's
                    # keys enrolled, option ROMs (graphics cards) signed by Microsoft keep working, which is what it checks.
                    p = _run(["sbctl", "enroll-keys", "--microsoft", "--yes-this-might-brick-my-machine"], check=False)
            finally:
                _lock_key_variables(unlocked, log=log)
            if p.returncode != 0:
                raise EnrollError("sbctl enroll-keys failed: " + (p.stderr or p.stdout).strip()[-400:])
        s = read_state_file()
        s["enrolled"] = "yes"
        with open(STATE_FILE, "w") as f:
            f.write(render_state_file(s))
    except (RuntimeError, OSError) as e:
        log(f"\n  Secure Boot setup stopped: {e}\n  The firmware keys were not changed unless the last step said so.")
        if isinstance(e, EnrollError):
            log("\n" + ENROLL_FAILED)
        return 1
    log("""
  Done. Our keys and Microsoft's are enrolled, and the boot files are signed.
  Restart. Most firmware switches Secure Boot on by itself once keys are enrolled; if `tapkich secureboot status`
  still says it is off, turn "Secure Boot" on in the firmware settings. Windows keeps starting (Microsoft's keys
  stay trusted). Kernel and driver updates are signed automatically from now on.""")
    return 0


USAGE = "usage: tapkich secureboot setup | refresh | status"
SETUP_FLAGS = ("--yes", "--from-installer")
REFRESH_FLAGS = ("--hook", "--force")


def main(args: list) -> int:
    allowed = REFRESH_FLAGS if args[:1] == ["refresh"] else SETUP_FLAGS
    if args[:1] not in (["setup"], ["refresh"], []) or any(a not in allowed for a in args[1:]):
        print(USAGE, file=sys.stderr)                         # an unknown option must never start the real setup
        return 2
    if args[:1] == ["refresh"]:                           # the same as tapkich-boot-update (older pacman hooks call it)
        return bootupdate.main(["--hook"] * ("--hook" in args) + ["--force"] * ("--force" in args))
    try:
        return setup(assume_yes="--yes" in args, from_installer="--from-installer" in args)
    except KeyboardInterrupt:
        print("Stopped. " + ("The firmware keys may have been changed: run `sudo tapkich secureboot status` to see."
                             if _firmware_touched else "Nothing was changed in the firmware."))
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
