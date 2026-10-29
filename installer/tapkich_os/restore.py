"""`tapkich restore windows`: put the firmware's "Windows Boot Manager" boot entry back when it has gone missing.

Some firmware (a Lenovo ThinkPad, for one) drops boot entries made by an operating system when Secure Boot is switched.
Windows adds its entry again only when it starts, which it cannot if the firmware no longer offers it. We add just that
one entry, at the END of the boot order (TapkicH-OS stays first), and touch nothing else: not Windows' files, not its
boot partition, not any other entry.

Then the Windows items of TapkicH-OS's own boot menu (limine.conf on /boot) are brought up to date. The installer
writes them once, from the Windows boot partitions it saw; when those were missing or broken then and were repaired
later, the menu had no Windows item until this command adds it.

Pure helpers (find the target, decide, build the commands) are unit-tested with made-up data; `main(argv)` runs as root.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap

from . import bootloader, detect, planner, system
from . import secureboot_setup as sb

LABEL = "Windows Boot Manager"        # Limine's Windows menu item finds the entry by exactly this name
LOADER = "\\EFI\\Microsoft\\Boot\\bootmgfw.efi"
GIB = 1024 ** 3

USAGE = "usage: tapkich restore windows [--check] [--yes] [--remove-stale] [--disk /dev/xxx --part N]"



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


def other_entries(nvram: list, part) -> list:
    """Entries with exactly our label that do not point at `part`: Limine finds the entry by name, so they clash."""
    mine = part.partuuid.lower() if part is not None and part.partuuid else None
    return [e for e in nvram if e.label == LABEL and e.partuuid != mine]


def stale_entries(nvram: list, part, snap) -> list:
    """Of those, the ones whose partition no longer exists on any disk. Never one with an unknown target, never one
    whose partition is still there, never one with another name. `part` is None when no Windows partition is left."""
    return [e for e in other_entries(nvram, part)
            if e.label == LABEL and e.partuuid and snap.find_partuuid(e.partuuid) is None]


def describe_entry(e) -> str:
    """One stale entry, with everything the firmware knows about its target, so the user can judge it."""
    return f"Boot{e.bootnum}  {e.label}  partition {e.partuuid}  file {e.path or '(none)'}"


WIDTH = 100                  # no printed line is wider than this


def fit(say):
    """`say` that breaks every line longer than WIDTH at a space (continuation lines are indented two more)."""
    def out(text=""):
        for line in str(text).splitlines() or [""]:
            indent = " " * (len(line) - len(line.lstrip()) + 2)
            for part in textwrap.wrap(line, WIDTH, subsequent_indent=indent, break_long_words=False,
                                      break_on_hyphens=False) or [""]:
                say(part)
    return out


def skipped_refusal(snap) -> str:
    """Why nothing may be deleted: detection could not read some devices, so a partition that looks gone might sit
    on one of them. '' when the picture is complete."""
    skipped = list(getattr(snap, "skipped", None) or [])
    if not skipped:
        return ""
    return ("Some devices could not be read, so an entry that looks stale might point at one of them. Nothing was "
            "removed. Unreadable: " + "; ".join(skipped))


def remove_command(entry) -> list:
    return ["efibootmgr", "-b", entry.bootnum.upper(), "-B"]


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


def split_windows(text: str) -> tuple:
    """(limine.conf without its Windows items, the Windows items). The Windows items are the top-level entries whose
    name starts with "Windows", the ones the installer writes; sub-entries (`//`) stay with their parent."""
    keep, win, cur = [], [], None
    for line in text.split("\n"):
        if line.startswith("/") and not line.startswith("//"):
            cur = win if line[1:].startswith("Windows") else keep
        (keep if cur is None else cur).append(line)
    return "\n".join(keep), "\n".join(win)


def with_windows(text: str, items: list) -> str:
    """limine.conf with its Windows items replaced by `items` (system.windows_menu_lines), at the end."""
    crlf = "\r\n" in text
    rest, _ = split_windows(text.replace("\r\n", "\n"))
    new = rest.rstrip("\n") + "\n" + ("\n".join(items) + "\n" if items else "")
    return new.replace("\n", "\r\n") if crlf else new


def _guids(text: str) -> set:
    return {g.lower() for g in re.findall(r"guid\(([0-9A-Fa-f-]+)\)", text)}


def sync_menu(snap, nvram: list, *, boot: str, state_path: str, check: bool, say) -> int:
    """Make the Windows items of TapkicH-OS's boot menu match the Windows boot partitions on the disks. Returns the
    exit code."""
    items = system.windows_menu_items(windows_partitions(snap))
    want = system.windows_menu_lines(items, system.windows_boot_entry(snap, [g for _, g in items], nvram))
    text = None
    for c in sb.LIMINE_CONFS:
        try:
            with open(f"{boot}/{c}", newline="") as f:
                text = f.read()
            break
        except OSError:
            continue
    if text is None:
        say(f"Boot menu: no limine.conf found on {boot}, so the TapkicH-OS boot menu was not checked.")
        return 1
    _, have = split_windows(text.replace("\r\n", "\n"))
    if have.strip("\n") == "\n".join(want).strip("\n"):
        say("Boot menu: TapkicH-OS's boot menu lists Windows.")
        return 0
    if _guids(have) - _guids("\n".join(want)) and (why := skipped_refusal(snap)):
        say("Boot menu: not changed. " + why)
        return 1
    if check:
        say("Boot menu: TapkicH-OS's boot menu does not list Windows as it is now. Run `sudo tapkich restore windows` "
            "to update it.")
        return 1
    _, error = bootloader.rewrite(lambda t: with_windows(t, want), boot, state_path)
    if error:
        say(f"Boot menu: not changed, {error}")
        return 1
    say("Boot menu: " + ("Windows is added to TapkicH-OS's boot menu." if not have.strip() else
                         "the Windows items of TapkicH-OS's boot menu are updated.") + " It shows on the next start.")
    return 0


def restore(snap, *, disk="", part=0, check=False, yes=False, remove_stale=False, run=_run, ask=None, say=print,
            boot="", state_path=sb.STATE_FILE) -> int:
    """Returns the exit code. `run(cmd) -> stdout` and `ask(question) -> bool` are replaced in tests. With `boot`
    (where the installed system's ESP is mounted) the boot menu's Windows items are brought up to date afterwards."""
    say = fit(say)
    code = _restore_entry(snap, disk=disk, part=part, check=check, yes=yes, remove_stale=remove_stale, run=run,
                          ask=ask, say=say)
    if not boot or not windows_partitions(snap):
        return code
    try:
        nvram, _ = _read_list(run)                     # the entry may have just been added: the menu can then use it
    except RestoreError as e:
        say(f"tapkich restore windows: {e}")
        return 1
    return max(code, sync_menu(snap, nvram, boot=boot, state_path=state_path, check=check, say=say))


def _restore_entry(snap, *, disk, part, check, yes, remove_stale, run, ask, say) -> int:
    """The firmware's boot entry. Returns the exit code."""
    try:
        parts = windows_partitions(snap)
        if not parts and not remove_stale:
            pick_target(parts)                         # raises: nothing to restore
        target = pick_target(parts, disk, part) if parts else None
        nvram, order = _read_list(run)
        stale = stale_entries(nvram, target, snap)
        if target is None:                             # the Windows disk is gone: the only job is the stale entries
            if not stale:
                say(f'No Windows boot files found on any disk, and no "{LABEL}" entry points at a missing partition.')
                return 1
            if (why := skipped_refusal(snap)):
                say(why)
                return 1
            say(f'No Windows boot files found on any disk. These "{LABEL}" boot entries point at a partition that '
                "is gone:")
            for e in stale:
                say("  " + describe_entry(e))
            if check:
                return 1
            if not yes and not (ask and ask("Remove them? [y/N] ")):
                say("Nothing changed.")
                return 1
            for e in stale:
                run(remove_command(e))
            say("Removed.")
            return 0
        if remove_stale and stale:
            if (why := skipped_refusal(snap)):
                say(why)
                return 1
            say(f'These "{LABEL}" boot entries point at a partition that is gone:')
            for e in stale:
                say("  " + describe_entry(e))
            if check:
                return 1
            if not yes and not (ask and ask("Remove them? [y/N] ")):
                say("Nothing changed.")
                return 1
            for e in stale:
                run(remove_command(e))
            nvram, order = _read_list(run)
            say("Removed.")
        else:
            others = other_entries(nvram, target)
            if others:
                say(f'There is already another "{LABEL}" entry that points elsewhere (Limine finds the entry by '
                    "name, so two of them are ambiguous). To remove one:")
                for e in others:
                    gone = " (its partition is gone)" if e in stale else ""
                    say(f"  Boot{e.bootnum}{gone}: sudo efibootmgr -b {e.bootnum.upper()} -B")
                if stale:
                    say("Or remove those whose partition is gone: sudo tapkich restore windows --remove-stale")
        if has_entry(nvram, target):
            say(f'The "{LABEL}" boot entry is there (disk {target.disk}, partition {target.number}).')
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
        return 0
    except RestoreError as e:
        say(f"tapkich restore windows: {e}")
        return 1


def parse_args(argv: list) -> dict:
    opts = {"check": False, "yes": False, "remove_stale": False, "disk": "", "part": 0}
    it = iter(argv)
    for a in it:
        if a == "--check":
            opts["check"] = True
        elif a == "--remove-stale":
            opts["remove_stale"] = True
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
    if not os.path.isdir("/sys/firmware/efi"):    # efibootmgr has nothing to read or write without UEFI
        print("tapkich restore windows: this computer started in legacy BIOS mode, so there is no firmware boot "
              "list to restore.", file=sys.stderr)
        return 1
    snap = detect.detect_system()
    return restore(snap, **opts, ask=_ask, boot=sb.BOOT)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
