"""`tapkich bootloader timeout [<seconds>]`: how long Limine's boot menu counts down before it starts the default entry.

The installer writes the same limine.conf twice (EFI/limine/ and EFI/BOOT/; Limine reads the config beside itself).
Only the `timeout:` line changes here. With Secure Boot set up (`tapkich secureboot setup`) the config's hash is
enrolled into Limine, so the pacman hook's refresh runs afterwards; otherwise the system boots as it is.
"""
from __future__ import annotations

import os
import re
import sys

from . import secureboot_setup as sb

USAGE = ("usage: tapkich bootloader timeout [<seconds>]\n"
         "  timeout            print the boot menu countdown\n"
         "  timeout <seconds>  set it: 0 to 60 seconds (0 = start the default entry at once, without showing the menu),\n"
         "                     or `no` to wait until you choose")
_LINE = re.compile(r"^timeout\s*:\s*(\S*)[ \t]*$", re.M)


def parse(value: str) -> str | None:
    """The text for the `timeout:` line, or None when the value is not allowed."""
    if value == "no":
        return "no"
    if re.fullmatch(r"[0-9]{1,2}", value) and int(value) <= 60:
        return str(int(value))
    return None


def current(boot: str = sb.BOOT) -> str | None:
    """The timeout in the first limine.conf found, or None."""
    for c in sb.LIMINE_CONFS:
        try:
            with open(f"{boot}/{c}") as f:
                m = _LINE.search(f.read())
        except OSError:
            continue
        if m:
            return m.group(1)
    return None


def set_timeout(value: str, boot: str = sb.BOOT) -> list:
    """Rewrite the timeout line in every limine.conf that exists; returns the files written."""
    done = []
    for c in sb.LIMINE_CONFS:
        path = f"{boot}/{c}"
        try:
            with open(path, newline="") as f:
                text = f.read()
        except OSError:
            continue
        new, n = _LINE.subn(f"timeout: {value}", text, count=1)
        if n == 0:                                    # no line yet: Limine's default is 5 s, so say it explicitly
            new = f"timeout: {value}\n{text}"
        with open(path, "w", newline="") as f:
            f.write(new)
        done.append(path)
    return done


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
    before = {}
    for c in sb.LIMINE_CONFS:                          # kept, so a failed re-signing can put the old configs back
        try:
            with open(f"{boot}/{c}", newline="") as f:
                before[f"{boot}/{c}"] = f.read()
        except OSError:
            pass
    if not set_timeout(value, boot):
        print("tapkich: no boot menu config found on /boot")
        return 1
    if os.path.isfile(state_path):                     # Secure Boot set up: the new config must be hashed and enrolled
        try:
            sb.refresh(boot, state_path)
        except (RuntimeError, OSError) as e:
            restored = True
            for path, text in before.items():          # the enrolled hash belongs to the old config: put it back
                try:
                    with open(path, "w", newline="") as f:
                        f.write(text)
                except OSError:
                    restored = False
            print(f"tapkich: the countdown was not changed, the boot files could not be re-signed: {e}\n"
                  + ("The old boot files were kept." if restored else
                     "Do not restart with Secure Boot on before this is fixed (run `sudo tapkich secureboot refresh`)."))
            return 1
    print(f"Boot menu countdown: {value if value == 'no' else value + ' s'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
