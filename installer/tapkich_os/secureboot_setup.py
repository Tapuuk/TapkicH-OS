"""`tapkich secureboot setup`: Secure Boot with our own keys, Windows-compatible.

How it works (decided 2026-10-19):
  - sbctl creates our own Secure Boot keys and enrolls them together WITH Microsoft's (`sbctl enroll-keys -m`), so
    Windows' boot manager and graphics-card firmware (signed by Microsoft) keep starting with Secure Boot on.
  - The firmware checks Limine's EFI file: we sign it (both copies on our ESP) with our key.
  - Limine checks everything it loads: the BLAKE2b hash of limine.conf is enrolled into the signed Limine binary
    (`limine enroll-config`), and every file in limine.conf carries its own hash (`path#hash`). So a changed kernel,
    initramfs or config is refused. The kernel is also signed with our key (useful if it is ever started directly).
  - Windows is chainloaded through the firmware (protocol efi), which checks Microsoft's signature itself.
  - Updates stay signed: a pacman hook runs `tapkich secureboot refresh` after every transaction (after sbctl's own
    hook). It does nothing until Secure Boot was set up, and only rewrites what changed.
Keys can only be enrolled while the firmware is in Setup Mode; firmware never allows that silently from the OS.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys

STATE_FILE = "/etc/tapkich-os/secureboot.conf"
BOOT = "/boot"
LIMINE_SRC = "/usr/share/limine/BOOTX64.EFI"
LIMINE_EFIS = ["EFI/limine/limine_x64.efi", "EFI/BOOT/BOOTX64.EFI"]          # relative to /boot (our ESP)
LIMINE_CONFS = ["EFI/limine/limine.conf", "EFI/BOOT/limine.conf"]
KERNELS = ["vmlinuz-linux-zen"]
HOOK_NAME = "zzzz-tapkich-secureboot.hook"                 # sorts after sbctl's zz-sbctl.hook and mkinitcpio's 90-*
PACMAN_HOOK = """[Trigger]
Operation = Install
Operation = Upgrade
Operation = Remove
Type = Package
Target = *

[Action]
Description = Keeping the TapkicH-OS boot files signed for Secure Boot (does nothing if it is not set up)...
When = PostTransaction
Exec = /usr/bin/tapkich secureboot refresh --hook
"""

# Limine config keys whose value is a file Limine loads and must be hash-checked under Secure Boot.
HASHED_KEYS = ("path", "module_path", "kernel_path", "dtb_path", "wallpaper", "term_font")
_FILE_LINE = re.compile(r"^(\s*)(" + "|".join(HASHED_KEYS) + r")(\s*:\s*)(boot\(\):)(/[^#\s]+)(#[0-9a-fA-F]*)?\s*$")


def blake2b_file(path: str) -> str:
    h = hashlib.blake2b()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def blake2b_text(text: str) -> str:
    return hashlib.blake2b(text.encode()).hexdigest()


def with_hashes(conf: str, hash_of) -> str:
    """limine.conf with a fresh `#<blake2b>` on every file on our ESP (`boot():/...`). `hash_of(relpath)` returns the
    hex hash, or None when the file is missing (the line is then left without a hash and Limine will refuse it; the
    caller reports it). EFI chainload entries (`guid(...)` paths, Windows) are left alone: the firmware checks those."""
    out = []
    for line in conf.splitlines():
        m = _FILE_LINE.match(line)
        if m:
            indent, key, sep, dev, rel, _old = m.groups()
            h = hash_of(rel.lstrip("/"))
            line = f"{indent}{key}{sep}{dev}{rel}" + (f"#{h}" if h else "")
        out.append(line)
    return "\n".join(out) + ("\n" if conf.endswith("\n") else "")


def without_hashes(conf: str) -> str:
    return with_hashes(conf, lambda rel: None)


def missing_hashes(conf: str) -> list:
    """Files in the config that have no hash (Limine would refuse to boot them under Secure Boot)."""
    out = []
    for line in conf.splitlines():
        m = _FILE_LINE.match(line)
        if m and not (m.group(6) or "").lstrip("#") and m.group(2) not in ("wallpaper", "term_font"):
            out.append(m.group(5))
    return out


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
    return any(os.path.isdir(d) and os.listdir(d) for d in ("/var/lib/sbctl/keys", "/usr/share/secureboot/keys"))


def refresh(boot: str = BOOT, state_path: str = STATE_FILE, force: bool = False, log=print) -> bool:
    """Sign the kernel, hash every boot file into limine.conf, enroll the config hash into Limine and sign Limine.
    Only does work when something changed (or force). Returns True when everything is signed."""
    st = read_state_file(state_path)
    kernels = [k for k in KERNELS if os.path.isfile(os.path.join(boot, k))]
    for k in kernels:                                  # signing changes the file: hash it afterwards
        if st.get(f"signed_{k}") != blake2b_file(os.path.join(boot, k)) or force:
            _run(["sbctl", "sign", "-s", os.path.join(boot, k)])
            st[f"signed_{k}"] = blake2b_file(os.path.join(boot, k))
            log(f"  signed {k}")
    confs = [c for c in LIMINE_CONFS if os.path.isfile(os.path.join(boot, c))]
    if not confs:
        raise RuntimeError(f"no limine.conf found on {boot}")
    with open(os.path.join(boot, confs[0])) as f:
        conf = f.read()
    new = with_hashes(conf, lambda rel: blake2b_file(os.path.join(boot, rel)) if os.path.isfile(os.path.join(boot, rel)) else None)
    missing = missing_hashes(new)
    if missing:
        raise RuntimeError("these boot files are missing, Limine would refuse to start: " + ", ".join(missing))
    conf_hash = blake2b_text(new)
    src_hash = blake2b_file(LIMINE_SRC) if os.path.isfile(LIMINE_SRC) else ""
    # A Limine reinstall copies the unsigned binary over both ESP files without changing any hash above, so the files
    # are also compared with what was recorded right after signing them (no record yet: sign once).
    efis_ok = all(os.path.isfile(os.path.join(boot, e)) and st.get(f"signed_{e}") == blake2b_file(os.path.join(boot, e))
                  for e in LIMINE_EFIS)
    changed = force or not efis_ok or conf_hash != st.get("config_hash") or src_hash != st.get("limine_source_hash")
    if changed:
        _install_limine(boot, new, conf_hash, st, log)
        st.update(config_hash=conf_hash, limine_source_hash=src_hash)
        log("  boot menu config hashed and enrolled; Limine signed")
    st["enrolled"] = st.get("enrolled", "no")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    tmp = state_path + ".new"
    with open(tmp, "w") as f:
        f.write(render_state_file(st))
    os.replace(tmp, state_path)
    return True


SIGN_MARGIN = 64 * 1024            # signing adds a signature to the binary; room for it per file


def _install_limine(boot: str, conf: str, conf_hash: str, st: dict, log=print) -> None:
    """Replace both Limine copies and both configs without ever leaving a half-written file.
    Everything is first built as `<file>.new` beside its target (copy, enroll the config hash, sign, check). Only when
    all of them are ready are they moved into place, one directory at a time (config, then its Limine binary), so at
    most one of the two boot paths is inconsistent for a moment and the other one stays complete. On any error the
    new files are deleted and the old ones are untouched."""
    pairs = list(zip(LIMINE_CONFS, LIMINE_EFIS))
    targets = [os.path.join(boot, x) for pair in pairs for x in pair]
    staged = [t + ".new" for t in targets]
    src_size = os.path.getsize(LIMINE_SRC)                    # missing source: OSError, nothing written yet
    need = len(LIMINE_EFIS) * (src_size + SIGN_MARGIN) + len(LIMINE_CONFS) * len(conf.encode())
    free = shutil.disk_usage(boot).free
    if free < need:
        raise RuntimeError(f"not enough free space on the boot partition: {need // 1024} KiB needed, "
                           f"{free // 1024} KiB free. Nothing was changed.")
    try:
        for path in staged:                                   # leftovers of an earlier interrupted run
            if os.path.lexists(path):
                os.remove(path)
        for conf_rel, efi_rel in pairs:
            conf_new = os.path.join(boot, conf_rel) + ".new"
            efi_new = os.path.join(boot, efi_rel) + ".new"
            os.makedirs(os.path.dirname(conf_new), exist_ok=True)
            os.makedirs(os.path.dirname(efi_new), exist_ok=True)
            with open(conf_new, "w") as f:
                f.write(conf)
                f.flush()
                os.fsync(f.fileno())
            shutil.copyfile(LIMINE_SRC, efi_new)              # a clean binary each time (enroll-config writes into it)
            _run(["limine", "enroll-config", "--quiet", efi_new, conf_hash])
            _run(["sbctl", "sign", efi_new])
            if not os.path.isfile(efi_new) or os.path.getsize(efi_new) < src_size:
                raise RuntimeError(f"the new {efi_rel} is incomplete; the old files were kept")
        hashes = {}
        for conf_rel, efi_rel in pairs:
            os.replace(os.path.join(boot, conf_rel) + ".new", os.path.join(boot, conf_rel))
            os.replace(os.path.join(boot, efi_rel) + ".new", os.path.join(boot, efi_rel))
            _run(["sbctl", "sign", "-s", os.path.join(boot, efi_rel)], check=False)    # remember it for sbctl's own hook
            hashes[efi_rel] = blake2b_file(os.path.join(boot, efi_rel))
        for efi_rel, h in hashes.items():
            st[f"signed_{efi_rel}"] = h
    finally:
        for path in staged:
            try:
                os.remove(path)
            except OSError:
                pass


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


def main(args: list) -> int:
    if args[:1] == ["refresh"]:
        hook = "--hook" in args
        if not os.path.isfile(STATE_FILE):
            return 0                                          # not set up: nothing to keep signed
        if os.geteuid() != 0:
            print("tapkich secureboot refresh must run as root")
            return 1
        try:
            refresh(force="--force" in args, log=(lambda *_: None) if hook else print)
        except (RuntimeError, OSError) as e:
            print(f"TapkicH-OS Secure Boot: could not re-sign the boot files: {e}\n"
                  "The old boot files were kept. Do not restart with Secure Boot on before this is fixed "
                  "(run `sudo tapkich secureboot refresh`).")
            return 1
        except KeyboardInterrupt:
            print("Stopped. The old boot files were kept.")
            return 130
        return 0
    try:
        return setup(assume_yes="--yes" in args, from_installer="--from-installer" in args)
    except KeyboardInterrupt:
        print("Stopped. " + ("The firmware keys may have been changed: run `sudo tapkich secureboot status` to see."
                             if _firmware_touched else "Nothing was changed in the firmware."))
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
