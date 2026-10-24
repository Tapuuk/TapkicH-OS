"""`tapkich update`: one command for everything on the system.

  1. TapkicH-OS itself: our own pacman repository [tapkich] (the `tapkich-os` package with our files, plus the AUR
     packages we build and ship), hosted as assets of the GitHub release "repo" of Tapuuk/TapkicH-OS.
  2. The system: pacman -Syu (official Arch repos, and [tapkich] once it is published).
  3. The shell's Qt-linked AUR packages (quickshell-git, caelestia-shell, ...): rebuilt when pacman just moved Qt to
     another version than they were built against (Quickshell uses Qt's private API and crashes otherwise).
  4. AUR packages: paru -Sua (only what is not in a repository).
  5. Flatpak apps (Bazaar / Flathub).

The GitHub repository is private for now, so the release files cannot be downloaded (GitHub answers 404). Until it is
public, step 1 says calmly that TapkicH-OS updates are not published yet and the rest still runs. Once the repository
is public, nothing else has to change: the next `tapkich update` finds the repository, adds [tapkich] to pacman.conf
and takes over our files as the `tapkich-os` package. The one switch is `enabled` in /etc/tapkich-os/update.conf
(auto = use it as soon as it is reachable; no = never).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

from . import apps

CONF = "/etc/tapkich-os/update.conf"
PACMAN_CONF = "/etc/pacman.conf"
REPO_NAME = "tapkich"
REPO_INCLUDE = "/etc/pacman.d/tapkich-repo"
DEFAULT_URL = "https://github.com/Tapuuk/TapkicH-OS/releases/download/repo"
PACKAGE = "tapkich-os"
# Files the installer wrote before they belonged to the tapkich-os package: pacman may replace them once. Derived from
# the package's own file list (package.py), so the two cannot drift apart (a hand-written list missed the pacman hooks
# and the keyring files: found in the VM test, 2026-10-19).
def adopt_paths() -> list:
    from . import package
    return sorted({"/opt/tapkich-os/*", "/usr/share/tapkich-os/*", *package.system_files(), *package.keyring_files()})


CONF_TEMPLATE = """# TapkicH-OS updates (`tapkich update`)
# enabled = auto: use the TapkicH-OS package repository as soon as it is published (reachable); no: never use it
enabled = auto
url = {url}
"""

NOT_PUBLISHED = ("TapkicH-OS updates are not published yet (the project's repository is still private). "
                 "Everything else is updated as usual.")


def render_conf(url: str = DEFAULT_URL) -> str:
    return CONF_TEMPLATE.format(url=url)


def parse_conf(text: str) -> dict:
    out = {"enabled": "auto", "url": DEFAULT_URL}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = (s.strip() for s in line.split("=", 1))
            if k in out and v:
                out[k] = v
    if out["enabled"] not in ("auto", "no"):
        out["enabled"] = "auto"
    return out


def read_conf(path: str = CONF) -> dict:
    try:
        with open(path) as f:
            return parse_conf(f.read())
    except OSError:
        return parse_conf("")


def render_include(url: str) -> str:
    """The [tapkich] repository's server line. Packages and the database are signed with our key (tapkich-os
    installs it into pacman's keyring)."""
    return ("# TapkicH-OS package repository (written by `tapkich update`)\n"
            "SigLevel = Required DatabaseRequired\n"
            f"Server = {url.rstrip('/')}\n")


def with_repo(pacman_conf: str, include: str = REPO_INCLUDE) -> str:
    """pacman.conf with the [tapkich] repository (after the official ones). Idempotent."""
    if re.search(rf"^\[{REPO_NAME}\]", pacman_conf, re.M):
        return pacman_conf
    return pacman_conf.rstrip("\n") + f"\n\n[{REPO_NAME}]\nInclude = {include}\n"


def probe(url: str, timeout: float = 15) -> str:
    """Is the repository published? 'published', 'not-published' (404 / 401 / 403: private or not there yet),
    or 'offline' (cannot reach the server)."""
    req = urllib.request.Request(url.rstrip("/") + f"/{REPO_NAME}.db", method="HEAD",
                                 headers={"User-Agent": "tapkich-update"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "published" if 200 <= r.status < 300 else "not-published"
    except urllib.error.HTTPError as e:
        return "not-published" if e.code in (401, 403, 404) else "offline"
    except (urllib.error.URLError, OSError, ValueError):
        return "offline"


class Step:
    def __init__(self, name: str):
        self.name, self.result, self.note = name, "skipped", ""


def _say(text: str = "") -> None:
    print(text, flush=True)


def _head(text: str) -> None:
    _say(f"\n\033[1m== {text}\033[0m")


def _run(cmd: list) -> int:
    try:
        return subprocess.run(cmd).returncode
    except OSError as e:
        _say(f"  could not run {cmd[0]}: {e}")
        return 127


def _sudo(cmd: list) -> list:
    return cmd if os.geteuid() == 0 else ["sudo", *cmd]


def _installed(pkg: str) -> bool:
    return subprocess.run(["pacman", "-Q", pkg], capture_output=True).returncode == 0


def _write_as_root(path: str, text: str) -> bool:
    if os.geteuid() == 0:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return True
    return subprocess.run(["sudo", "install", "-Dm644", "/dev/stdin", path], input=text, text=True).returncode == 0


def qt_rebuild_packages(version_text: str, installed) -> list:
    """The Qt-linked shell packages to rebuild: all of them when `quickshell --version --verbose` says the running Qt
    is not the one Quickshell was built against (`installed(pkg)` tells which exist). Otherwise none."""
    if not apps.qt_mismatch(version_text):
        return []
    return [p for p in apps.QT_PACKAGES if installed(p)]


def rebuild_command(pkgs: list, yes: bool) -> list:
    return ["paru", "-S", "--rebuild", *pkgs] + (["--noconfirm"] if yes else [])


def qt_step(yes: bool, run=_run) -> Step:
    """After pacman: rebuild the shell packages when Qt changed. Skipped quietly when nothing is out of step."""
    s = Step("Shell packages (Qt)")
    if os.geteuid() == 0 or not shutil.which("paru") or not shutil.which("quickshell"):
        s.note = "nothing to rebuild here"
        return s
    try:
        out = subprocess.run(["quickshell", "--version", "--verbose"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        s.note = "could not ask Quickshell for its Qt version"
        return s
    pkgs = qt_rebuild_packages(out, _installed)
    if not pkgs:
        s.note = "Qt did not change since they were built"
        _say("  " + s.note)
        return s
    _say(f"  Qt changed since these were built; rebuilding them (this takes a while): {', '.join(pkgs)}")
    s.result = "ok" if run(rebuild_command(pkgs, yes)) == 0 else "failed"
    return s


def channel_step(conf: dict, pacman_conf_path: str = PACMAN_CONF) -> Step:
    """Make [tapkich] part of pacman's repositories when it is published. Never an error wall."""
    s = Step("TapkicH-OS")
    if conf["enabled"] == "no":
        s.note = f"switched off in {CONF}"
        return s
    state = probe(conf["url"])
    if state == "not-published":
        s.note = NOT_PUBLISHED
        return s
    if state == "offline":
        s.note = "could not reach the TapkicH-OS update server; try again later"
        return s
    try:
        with open(pacman_conf_path) as f:
            current = f.read()
    except OSError:
        s.result, s.note = "failed", f"cannot read {pacman_conf_path}"
        return s
    ok = _write_as_root(REPO_INCLUDE, render_include(conf["url"]))
    new = with_repo(current)
    if ok and new != current:
        ok = _write_as_root(pacman_conf_path, new)
    s.result = "ok" if ok else "failed"
    s.note = "the TapkicH-OS repository is on" if ok else "could not add the TapkicH-OS repository"
    return s


def pacman_command(adopt: bool) -> list:
    """The full system upgrade. On the first update after the repository is published, the tapkich-os package joins
    the same transaction (never a partial upgrade) and may replace the files the installer wrote."""
    if not adopt:
        return ["pacman", "-Syu"]
    return ["pacman", "-Syu", "--needed", PACKAGE, *[a for p in adopt_paths() for a in ("--overwrite", p)]]


def keyring_command() -> list:
    """Refresh the signing keys first: an install that updates months later can fail on outdated keyring signatures."""
    return ["pacman", "-Sy", "--needed", "archlinux-keyring"]


def main(argv=None) -> int:
    yes = "--yes" in (argv or [])                     # unattended: no questions from pacman or paru
    conf = read_conf()
    steps = []
    _say("Updating everything: TapkicH-OS, the system, AUR packages and Flatpak apps. You may be asked for your password.")

    _head("TapkicH-OS")
    ch = channel_step(conf)
    _say(f"  {ch.note}")
    steps.append(ch)

    _head("System packages (pacman)")
    s = Step("System packages")
    # the keyring step and the full upgrade belong together (-Sy alone is a partial upgrade); a failed keyring step
    # does not stop the upgrade
    krc = _run(_sudo(keyring_command() + (["--noconfirm"] if yes else [])))
    if krc != 0:
        _say("  the signing keys could not be refreshed first; continuing with the upgrade")
    rc = _run(_sudo(pacman_command(adopt=ch.result == "ok" and not _installed(PACKAGE)) + (["--noconfirm"] if yes else [])))
    s.result = "ok" if rc == 0 else "failed"
    steps.append(s)

    _head("Shell packages (rebuilt after a Qt update)")
    steps.append(qt_step(yes))

    _head("AUR packages (paru)")
    s = Step("AUR packages")
    if os.geteuid() == 0:
        s.note = "paru does not run as root: run `tapkich update` without sudo to update AUR packages"
        _say("  " + s.note)
    elif shutil.which("paru"):
        rc = _run(["paru", "-Sua"] + (["--noconfirm"] if yes else []))
        s.result = "ok" if rc == 0 else "failed"
    else:
        s.note = "paru is not installed"
        _say("  paru is not installed: skipped")
    steps.append(s)

    _head("Flatpak apps")
    s = Step("Flatpak apps")
    if shutil.which("flatpak"):
        rc1 = _run(_sudo(["flatpak", "update", "--system", "-y", "--noninteractive"]))
        rc2 = _run(["flatpak", "update", "--user", "-y", "--noninteractive"])
        s.result = "ok" if rc1 == 0 and rc2 == 0 else "failed"
    else:
        s.note = "flatpak is not installed"
    steps.append(s)

    _say("\nSummary")
    for st in steps:
        mark = {"ok": "done   ", "failed": "FAILED ", "skipped": "skipped"}[st.result]
        _say(f"  {mark} {st.name}" + (f": {st.note}" if st.note and st.result != "ok" else ""))
    failed = [st for st in steps if st.result == "failed"]
    if failed:
        _say("\nSome steps did not finish. Their messages are above; running `tapkich update` again is safe.")
    else:
        _say("\nEverything is up to date. A restart is a good idea if the kernel or drivers were updated.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
