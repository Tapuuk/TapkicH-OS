"""`tapkich restore windows`: put the firmware's "Windows Boot Manager" boot entry back when it has gone missing.

Some firmware (a Lenovo ThinkPad, for one) drops boot entries made by an operating system when Secure Boot is switched.
Windows adds its entry again only when it starts, which it cannot if the firmware no longer offers it. We add just that
one entry, at the END of the boot order (TapkicH-OS stays first), and touch nothing else: not Windows' files, not its
boot partition, not any other entry.

Pure helpers (find the target, decide, build the commands) are unit-tested with made-up data; `main(argv)` runs as root.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys

from . import detect, planner

LABEL = "Windows Boot Manager"        # Limine's Windows menu item finds the entry by exactly this name
LOADER = "\\EFI\\Microsoft\\Boot\\bootmgfw.efi"
GIB = 1024 ** 3

USAGE = "usage: tapkich restore windows [--check] [--yes] [--disk /dev/xxx --part N]"

NOTE_LIMINE = ("If TapkicH-OS's boot menu was set up while this entry was missing, its Windows item starts Windows "
               "directly. That works. With BitLocker, start Windows from the firmware boot menu instead.")


class RestoreError(Exception):
    pass


def node_name(disk: str, number: int) -> str:
    sep = "p" if disk[-1:].isdigit() else ""
    return f"{disk}{sep}{number}"


def parse_bootorder(text: str) -> list:
    m = re.search(r"^BootOrder:\s*([0-9A-Fa-f,]*)\s*$", text, re.M)
    return [x.upper() for x in m.group(1).split(",") if x] if m else []


def windows_partitions(snap) -> list:
    return sorted(planner.windows_boot_esps(snap), key=lambda p: (p.disk, p.number))


def describe(p) -> str:
    return f"{node_name(p.disk, p.number)} (disk {p.disk}, partition {p.number}, {p.size / GIB:.1f} GiB)"


def pick_target(parts: list, disk: str = "", part: int = 0):
    """The one partition to work on. Never guesses: several candidates need --disk and --part."""
    if not parts:
        raise RestoreError("No Windows boot files found on any disk; nothing to restore.")
    if disk or part:
        chosen = [p for p in parts if p.disk == disk and p.number == part]
        if not chosen:
            raise RestoreError("No Windows boot files on that disk and partition. Found:\n" +
                               "\n".join("  " + describe(p) for p in parts))
        return chosen[0]
    if len(parts) > 1:
        raise RestoreError("More than one Windows boot partition was found:\n" +
                           "\n".join("  " + describe(p) for p in parts) +
                           "\nChoose one with `--disk /dev/xxx --part N`.")
    return parts[0]


def has_entry(nvram: list, part) -> bool:
    return any(e.label == LABEL and part.partuuid and e.partuuid == part.partuuid.lower() for e in nvram)


def create_command(part) -> list:
    return ["efibootmgr", "--create-only", "--disk", part.disk, "--part", str(part.number),
            "--label", LABEL, "--loader", LOADER]


def order_command(old_order: list, new_num: str) -> list:
    return ["efibootmgr", "--bootorder", ",".join([*old_order, new_num])]


def check_result(before: list, after: list, order_after: list, old_order: list, new_part) -> str:
    """The new boot list must hold exactly one new entry (the one we made) and lose none. Returns its number."""
    b, a = {e.bootnum.upper() for e in before}, {e.bootnum.upper() for e in after}
    if b - a:
        raise RestoreError("A boot entry disappeared while adding ours (" + ", ".join(sorted(b - a)) + "). Nothing "
                           "was deleted by this tool; check `efibootmgr -v`.")
    new = sorted(a - b)
    if len(new) != 1:
        raise RestoreError(f"Expected exactly one new boot entry, found {len(new)}. Check `efibootmgr -v`.")
    entry = next(e for e in after if e.bootnum.upper() == new[0])
    if entry.label != LABEL or not has_entry([entry], new_part):
        raise RestoreError(f"The new entry Boot{new[0]} does not look right. Check `efibootmgr -v`.")
    if order_after and order_after[:len(old_order)] != old_order:
        raise RestoreError("The boot order changed in an unexpected way. Check `efibootmgr -v`.")
    return new[0]


def _read_list(run):
    text = run(["efibootmgr", "-v"])
    return detect.parse_efibootmgr(text), parse_bootorder(text)


def _run(cmd: list) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RestoreError(f"`{' '.join(cmd)}` failed: {(p.stderr or p.stdout).strip()}")
    return p.stdout


def restore(snap, *, disk="", part=0, check=False, yes=False, run=_run, ask=None, say=print) -> int:
    """Returns the exit code. `run(cmd) -> stdout` and `ask(question) -> bool` are replaced in tests."""
    try:
        target = pick_target(windows_partitions(snap), disk, part)
        nvram, order = _read_list(run)
        if has_entry(nvram, target):
            say(f'The "{LABEL}" boot entry is there (disk {target.disk}, partition {target.number}). Nothing to do.')
            return 0
        say(f'The firmware has no "{LABEL}" boot entry for Windows on {describe(target)}.')
        if check:
            say("Run `sudo tapkich restore windows` to add it.")
            return 1
        say(f'Adding it creates one new boot entry "{LABEL}" at the end of the boot order; TapkicH-OS stays first. '
            "No other entry and no file on any disk is changed.")
        if not yes and not (ask and ask("Add it? [y/N] ")):
            say("Nothing changed.")
            return 1
        run(create_command(target))
        mid, _ = _read_list(run)
        new = check_result(nvram, mid, [], order, target)
        run(order_command(order, new))
        after, order_after = _read_list(run)
        check_result(nvram, after, order_after, order, target)
        if not order_after or order_after[-1] != new:
            raise RestoreError(f"Boot{new} is not at the end of the boot order. Check `efibootmgr -v`.")
        say(f'Done: Boot{new} "{LABEL}" is added after the existing entries.')
        say(NOTE_LIMINE)
        return 0
    except RestoreError as e:
        say(f"tapkich restore windows: {e}")
        return 1


def parse_args(argv: list) -> dict:
    opts = {"check": False, "yes": False, "disk": "", "part": 0}
    it = iter(argv)
    for a in it:
        if a == "--check":
            opts["check"] = True
        elif a in ("--yes", "-y"):
            opts["yes"] = True
        elif a in ("--disk", "--part"):
            v = next(it, "")
            if a == "--part":
                if not v.isdigit():
                    raise ValueError(a)
                opts["part"] = int(v)
            else:
                opts["disk"] = v
        else:
            raise ValueError(a)
    if bool(opts["disk"]) != bool(opts["part"]):
        raise ValueError("--disk and --part")
    return opts


def _ask(question: str) -> bool:
    try:
        return input("  " + question).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main(argv=None) -> int:
    """`argv` is what follows `restore windows`. Needs root (reads the disks, writes the boot list)."""
    try:
        opts = parse_args(list(argv or []))
    except ValueError:
        print(USAGE, file=sys.stderr)
        return 2
    missing = [tool for tool in ("mdir", "ntfsls") if not shutil.which(tool)]
    if missing:                                    # without them every disk with an EFI or Windows partition is skipped
        print("tapkich restore windows: needs mtools and ntfsprogs to read the disks: sudo pacman -S mtools ntfsprogs",
              file=sys.stderr)
        return 1
    snap = detect.detect_system()
    return restore(snap, **opts, ask=_ask)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
