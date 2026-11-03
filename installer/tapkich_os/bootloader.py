"""`tapkich bootloader timeout [<seconds>]`: how long Limine's boot menu counts down before it starts the default entry.

Only the `timeout:` line of the boot menu's source (/etc/tapkich-os/limine.conf) changes here; tapkich-boot-update
(bootupdate.py) then writes both configs on the ESP from it, with their hashes, enrollment and signatures.
"""
from __future__ import annotations

import os
import re
import sys

from . import bootupdate
from . import secureboot_setup as sb

USAGE = ("usage: tapkich bootloader timeout [<seconds>]\n"
         "  timeout            print the boot menu countdown\n"
         "  timeout <seconds>  set it: 0 to 60 seconds (0 = start the default entry at once, without\n"
         "                     showing the menu), or `no` to wait until you choose")
_LINE = re.compile(r"^[ \t]*timeout[ \t]*:[ \t]*(\S*)[ \t]*(\r?)$", re.M | re.I)    # \r: a config with Windows line ends
LIMINE_DEFAULT = "5"                              # Limine's own countdown when limine.conf has no timeout line
ZERO_WARNING = ("Warning: with 0 the boot menu is not shown, so recovery and Windows cannot be reached from it.\n"
                "To get it back, run `tapkich bootloader timeout 3` from TapkicH-OS.")


def parse(value: str) -> str | None:
    """The text for the `timeout:` line, or None when the value is not allowed."""
    if value == "no":
        return "no"
    if re.fullmatch(r"[0-9]{1,2}", value) and int(value) <= 60:
        return str(int(value))
    return None


def _machine(boot: str):
    root = os.path.dirname(boot.rstrip("/")) or "/"
    return bootupdate.Machine(root, sign=None if root == "/" else False, log=lambda *_: None)


def current(boot: str = sb.BOOT) -> str | None:
    """The timeout in the boot menu's source; Limine's default when it has no timeout line; None when there is no
    boot menu at all."""
    try:
        text = bootupdate.read_source(_machine(boot), create=False)
    except (bootupdate.NotSetUp, OSError):
        return None
    m = _LINE.search(text)
    return m.group(1) if m else LIMINE_DEFAULT


def set_timeout_text(text: str, value: str) -> str:
    new, n = _LINE.subn(lambda m: f"timeout: {value}{m.group(2)}", text, count=1)
    if n == 0:                                        # no line yet: Limine's default is 5 s, so say it explicitly
        eol = "\r\n" if "\r\n" in text else "\n"
        new = f"timeout: {value}{eol}{text}"
    return new


def rewrite(change, boot: str = sb.BOOT, state_path: str = sb.STATE_FILE) -> tuple:
    """Write `change(text)` into the boot menu's source and regenerate both configs on the ESP (hashes, enrolled
    config hash, signatures: tapkich-boot-update). If that fails the old source is put back and the ESP is unchanged.
    Returns (the ESP configs, error text or ""); no configs when there is no boot menu."""
    m = _machine(boot)
    try:
        before = bootupdate.read_source(m)
    except bootupdate.NotSetUp:
        return [], ""
    bootupdate.write_source(m, change(before))
    try:
        bootupdate.update(m)
    except (RuntimeError, OSError) as e:
        try:
            bootupdate.write_source(m, before)
            kept = "The old boot files were kept."
        except OSError:
            kept = "Run `sudo tapkich-boot-update` before restarting."
        return [], f"the boot menu could not be updated: {e}\n{kept}"
    return [m.esp(c) for c in bootupdate.LIMINE_CONFS], ""


def main(args: list, boot: str = sb.BOOT, state_path: str = sb.STATE_FILE) -> int:
    if args[:1] != ["timeout"] or len(args) > 2:
        print(USAGE)
        return 2
    if len(args) == 1:
        cur = current(boot)
        if cur is None:
            print("tapkich: no boot menu config found on /boot")
            return 1
        print("no (waits until you choose)" if cur == "no" else f"{cur} seconds" if cur != "1" else "1 second")
        return 0
    value = parse(args[1])
    if value is None:
        print(f"tapkich: {args[1]!r} is not a number of seconds from 0 to 60 (or `no`)\n{USAGE}")
        return 2
    written, error = rewrite(lambda text: set_timeout_text(text, value), boot, state_path)
    if error:
        print(f"tapkich: the countdown was not changed, {error}")
        return 1
    if not written:
        print("tapkich: no boot menu config found on /boot")
        return 1
    print(f"Boot menu countdown: {value if value == 'no' else value + ' s'}")
    if value == "0":
        print(ZERO_WARNING)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
