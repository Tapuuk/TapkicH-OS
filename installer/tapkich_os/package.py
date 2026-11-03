"""The `tapkich-os` package: every file TapkicH-OS itself puts on an installed system that is the same on every
machine (the `tapkich` command, Gaming Mode's session files, our pacman hooks, tuning, the update channel, our
package-signing key). User- and machine-specific files (home folder, Limine config, fstab, ...) stay in the installer.

One list, two users:
  - packaging/tapkich-os/PKGBUILD runs `python3 -m tapkich_os.package <pkgdir>` to build the package;
  - the installer installs that package from the ISO's local repo, or, when it is not there (development), writes
    the same files straight into the new system (system.py).
Updates then come through our own repository [tapkich] (update.py).
"""
from __future__ import annotations

import os
import shutil
import sys

from . import bootupdate, desktop, tuning, update

NAME = "tapkich-os"
HERE = os.path.dirname(os.path.abspath(__file__))
KEYRING_DIR = "/usr/share/pacman/keyrings"
KEY_SOURCE = os.path.join(HERE, "data", "keyring")       # tapkich.gpg (public key) + tapkich-trusted (tools/make-signing-key.sh)
HOOKS = "/usr/share/libalpm/hooks"
DOCS = os.path.join(HERE, "data", "docs")               # the boot docs, also shipped on the system


def _doc(name: str) -> str:
    with open(os.path.join(DOCS, name)) as f:
        return f.read()

TAPKICH_WRAPPER = """#!/bin/sh
# tapkich: the one command for tweaking this system (see `tapkich help`)
exec env PYTHONPATH=/opt/tapkich-os PYTHONDONTWRITEBYTECODE=1 python3 -P -m tapkich_os.command "$@"
"""

OS_RELEASE = """NAME="TapkicH-OS"
PRETTY_NAME="TapkicH-OS"
ID=tapkich
ID_LIKE=arch
BUILD_ID=rolling
ANSI_COLOR="38;2;124;58;237"
"""
# /etc/os-release is a symlink owned by Arch's `filesystem` package: put our file back after each update of it.
OS_RELEASE_HOOK = """[Trigger]
Operation = Install
Operation = Upgrade
Type = Package
Target = filesystem
Target = tapkich-os

[Action]
Description = Keeping the TapkicH-OS name in /etc/os-release
When = PostTransaction
Exec = /bin/sh -c 'rm -f /etc/os-release && cp /usr/share/tapkich-os/os-release /etc/os-release'
"""

UNITS = "/usr/lib/systemd/system"


def system_files() -> dict:
    """Absolute path -> (content, mode) for every generated file of the package."""
    files = {
        "/usr/bin/tapkich": (TAPKICH_WRAPPER, 0o755),
        "/usr/share/tapkich-os/os-release": (OS_RELEASE, 0o644),
        f"{HOOKS}/tapkich-os-release.hook": (OS_RELEASE_HOOK, 0o644),
        # the boot menu: hashes, enrolled config hash and signatures kept in step (bootupdate.py)
        "/usr/bin/tapkich-boot-update": (bootupdate.COMMAND, 0o755),
        "/usr/bin/tapkich-secureboot-check": (bootupdate.SB_CHECK_COMMAND, 0o755),
        f"{HOOKS}/{bootupdate.HOOK_NAME}": (bootupdate.PACMAN_HOOK, 0o644),
        bootupdate.MKINITCPIO_POST_PATH: (bootupdate.MKINITCPIO_POST, 0o755),
        bootupdate.KERNEL_INSTALL_PATH: (bootupdate.KERNEL_INSTALL, 0o755),
        f"{UNITS}/tapkich-boot-update-shutdown.service": (bootupdate.SHUTDOWN_UNIT, 0o644),
        f"{UNITS}/tapkich-boot-good.service": (bootupdate.GOOD_UNIT, 0o644),
        f"{UNITS}/tapkich-boot-good.timer": (bootupdate.GOOD_TIMER, 0o644),
        "/" + bootupdate.SETTINGS: (bootupdate.BOOT_CONF, 0o644),
        "/usr/share/doc/tapkich-os/boot-locks.md": (_doc("boot-locks.md"), 0o644),
        "/usr/share/doc/tapkich-os/boot-recovery.md": (_doc("boot-recovery.md"), 0o644),
        update.CONF: (update.render_conf(), 0o644),
        **{p: (c, 0o644) for p, c in tuning.files().items()},
        **desktop.system_files(),
    }
    return files


# Always on (as if `systemctl enable`d): the shutdown check and the "last known good" copy of the boot files
SYMLINKS = {
    f"{UNITS}/multi-user.target.wants/tapkich-boot-update-shutdown.service": "../tapkich-boot-update-shutdown.service",
    f"{UNITS}/timers.target.wants/tapkich-boot-good.timer": "../tapkich-boot-good.timer",
}


# /etc files the user may edit: pacman keeps their version on upgrades (PKGBUILD backup=())
def backup_files() -> list:
    return sorted(p.lstrip("/") for p in system_files() if p.startswith("/etc/") and not p.startswith("/etc/sudoers.d/"))


def keyring_files() -> dict:
    """Our package-signing public key for pacman's keyring (pacman-key --populate tapkich). Empty if not created."""
    out = {}
    for name in ("tapkich.gpg", "tapkich-trusted"):
        src = os.path.join(KEY_SOURCE, name)
        if os.path.isfile(src):
            with open(src) as f:
                out[f"{KEYRING_DIR}/{name}"] = (f.read(), 0o644)
    return out


def write_tree(dest: str) -> list:
    """Write the whole package content under `dest` (a pkgdir, or the new system's root). Returns the paths."""
    written = []
    code = os.path.join(dest, "opt/tapkich-os/tapkich_os")
    os.makedirs(os.path.dirname(code), exist_ok=True)
    shutil.copytree(HERE, code, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    written.append("/opt/tapkich-os/tapkich_os")
    for path, (content, mode) in {**system_files(), **keyring_files()}.items():
        full = os.path.join(dest, path.lstrip("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        os.chmod(full, mode)
        written.append(path)
    for path, target in SYMLINKS.items():
        full = os.path.join(dest, path.lstrip("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if os.path.lexists(full):
            os.remove(full)
        os.symlink(target, full)
        written.append(path)
    if os.path.isdir(os.path.join(dest, "etc/sudoers.d")):
        os.chmod(os.path.join(dest, "etc/sudoers.d"), 0o750)
    return written


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--backup":
        print(" ".join(backup_files()))
    elif len(sys.argv) == 2:
        for p in write_tree(sys.argv[1]):
            print(p)
    else:
        print("usage: python3 -m tapkich_os.package <destdir> | --backup x", file=sys.stderr)
        sys.exit(2)
