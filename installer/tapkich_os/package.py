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

from . import desktop, secureboot_setup, tuning, update

NAME = "tapkich-os"
HERE = os.path.dirname(os.path.abspath(__file__))
KEYRING_DIR = "/usr/share/pacman/keyrings"
KEY_SOURCE = os.path.join(HERE, "data", "keyring")       # tapkich.gpg (public key) + tapkich-trusted (tools/make-signing-key.sh)
HOOKS = "/usr/share/libalpm/hooks"

TAPKICH_WRAPPER = """#!/bin/sh
# tapkich: the one command for tweaking this system (see `tapkich help`)
exec env PYTHONPATH=/opt/tapkich-os python3 -P -m tapkich_os.command "$@"
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

# Does nothing under Secure Boot (the Secure Boot refresh hook installs, enrolls and signs Limine itself; a stock copy
# here would replace the signed one first) and unless /boot really is the mounted EFI partition.
LIMINE_HOOK = """[Trigger]
Operation = Install
Operation = Upgrade
Type = Package
Target = limine

[Action]
Description = Updating Limine on the TapkicH-OS EFI partition (only /boot, never another disk's ESP)...
When = PostTransaction
Exec = /bin/sh -c 'if [ ! -e /etc/tapkich-os/secureboot.conf ] && mountpoint -q /boot && [ -d /boot/EFI/limine ]; then install -D /usr/share/limine/BOOTX64.EFI /boot/EFI/limine/limine_x64.efi && install -D /usr/share/limine/BOOTX64.EFI /boot/EFI/BOOT/BOOTX64.EFI; fi'
"""


def system_files() -> dict:
    """Absolute path -> (content, mode) for every generated file of the package."""
    files = {
        "/usr/bin/tapkich": (TAPKICH_WRAPPER, 0o755),
        "/usr/share/tapkich-os/os-release": (OS_RELEASE, 0o644),
        f"{HOOKS}/tapkich-os-release.hook": (OS_RELEASE_HOOK, 0o644),
        f"{HOOKS}/90-limine-deploy.hook": (LIMINE_HOOK, 0o644),
        f"{HOOKS}/{secureboot_setup.HOOK_NAME}": (secureboot_setup.PACMAN_HOOK, 0o644),
        update.CONF: (update.render_conf(), 0o644),
        **{p: (c, 0o644) for p, c in tuning.files().items()},
        **desktop.system_files(),
    }
    return files


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
