"""`tapkich update`: one command for everything on the system.

  1. TapkicH-OS itself: our own pacman repository [tapkich] (the `tapkich-os` package with our files, plus the AUR
     packages we build and ship), hosted as assets of the GitHub release "repo" of Tapuuk/TapkicH-OS.
  2. The system: pacman -Syu (official Arch repos, and [tapkich] once it is published).
  3. The shell's Qt-linked AUR packages (quickshell-git, caelestia-shell, ...): rebuilt when pacman just moved Qt to
     another version than they were built against (Quickshell uses Qt's private API and crashes otherwise).
  4. AUR packages: paru -Sua (only what is not in a repository).
  5. Flatpak apps (Bazaar / Flathub).

When the release files cannot be downloaded (GitHub is down, or the files are missing for a moment), step 1 says so
calmly and the rest still runs: if [tapkich] is already in pacman.conf, pacman runs with a temporary configuration
that leaves it out. Once the files are reachable again, the next `tapkich update` uses the repository again. The one
switch is `enabled` in /etc/tapkich-os/update.conf (auto = use it as soon as it is reachable; no = never, and the
[tapkich] block is removed from pacman.conf).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
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

NOT_PUBLISHED = "TapkicH-OS update files could not be found (try again later)"
OFFLINE = "could not reach the TapkicH-OS update server (try again later)"
USAGE = "usage: tapkich update [--yes]"
YES_FLAGS = ("--yes", "-y", "--noconfirm")


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


def without_repo(pacman_conf: str, include: str = REPO_INCLUDE) -> str:
    """pacman.conf without the [tapkich] section, whatever it holds (a hand-edited SigLevel or Server line too): from
    its header up to the next [section] header or the end. Idempotent; the other sections and comments stay."""
    out, skipping = [], False
    for line in pacman_conf.split("\n"):
        if re.match(rf"^\[{REPO_NAME}\]", line):
            skipping = True
            while out and not out[-1].strip():          # the blank lines that set the section apart
                out.pop()
            continue
        if skipping and re.match(r"^\[", line):
            skipping = False
            out.append("")
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip("\n") + "\n"


def has_repo(pacman_conf: str) -> bool:
    return re.search(rf"^\[{REPO_NAME}\]", pacman_conf, re.M) is not None


def key_fingerprint(path: str = "") -> str:
    """The fingerprint of our package-signing key, from the keyring files the package ships ('' if there is none)."""
    from . import package
    try:
        with open(path or os.path.join(package.KEY_SOURCE, "tapkich-trusted")) as f:
            for line in f:
                fp = line.split(":", 1)[0].strip()
                if re.fullmatch(r"[0-9A-Fa-f]{40}", fp):
                    return fp.upper()
    except OSError:
        pass
    return ""


def _quiet(cmd: list) -> int:
    try:
        return subprocess.run(cmd, capture_output=True).returncode
    except OSError:
        return 127


def key_in_keyring(fingerprint: str, run=_quiet) -> bool:
    """Is our signing key in pacman's keyring? If not, populate it from the shipped keyring files once."""
    if not fingerprint:
        return False
    if run(_sudo(["pacman-key", "--list-keys", fingerprint])) == 0:
        return True
    run(_sudo(["pacman-key", "--populate", REPO_NAME]))
    return run(_sudo(["pacman-key", "--list-keys", fingerprint])) == 0


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
        self.skip_repo = False        # [tapkich] is in pacman.conf but cannot be used this time


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
    # --aur: every target comes from the AUR, so paru builds it instead of reusing a same-named [tapkich] binary
    return ["paru", "-S", "--aur", "--rebuild", *pkgs] + (["--noconfirm"] if yes else [])


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


def channel_step(conf: dict, pacman_conf_path: str = PACMAN_CONF, probe_fn=None, fingerprint=key_fingerprint,
                 run=_quiet, write=None) -> Step:
    """Make [tapkich] part of pacman's repositories when it is published and signed with a key pacman knows. When it
    cannot be used while it is already configured, `skip_repo` tells main to run pacman without it. Never an error wall."""
    write = write or _write_as_root
    probe_fn = probe_fn or probe
    s = Step("TapkicH-OS")
    try:
        with open(pacman_conf_path) as f:
            current = f.read()
    except OSError:
        s.result, s.note = "failed", f"cannot read {pacman_conf_path}"
        return s
    configured = has_repo(current)
    if conf["enabled"] == "no":
        s.note = f"switched off in {CONF}"
        if configured:
            new = without_repo(current)
            if has_repo(new):
                s.result, s.skip_repo = "failed", True
                s.note += "; the [tapkich] section could not be removed from pacman.conf"
            elif write(pacman_conf_path, new):
                s.note += "; the repository was removed from pacman.conf"
            else:
                s.skip_repo = True
        return s
    state = probe_fn(conf["url"])
    reason = ""
    if state == "not-published":
        reason = NOT_PUBLISHED
    elif state == "offline":
        reason = OFFLINE
    elif not key_in_keyring(fingerprint(), run):
        reason = "the TapkicH-OS signing key is not in pacman's keyring"
    if reason:
        s.note, s.skip_repo = reason, configured
        return s
    ok = write(REPO_INCLUDE, render_include(conf["url"]))
    new = with_repo(current)
    if ok and new != current:
        ok = write(pacman_conf_path, new)
    s.result = "ok" if ok else "failed"
    s.note = "the TapkicH-OS repository is on" if ok else "could not add the TapkicH-OS repository"
    return s


def _with_config(cmd: list, config: str) -> list:
    return [cmd[0], "--config", config, *cmd[1:]] if config else cmd


def pacman_command(adopt: bool, config: str = "") -> list:
    """The full system upgrade. On the first update after the repository is published, the tapkich-os package joins
    the same transaction (never a partial upgrade) and may replace the files the installer wrote."""
    if not adopt:
        return _with_config(["pacman", "-Syu"], config)
    return _with_config(["pacman", "-Syu", "--needed", PACKAGE,
                         *[a for p in adopt_paths() for a in ("--overwrite", p)]], config)


def keyring_command(config: str = "") -> list:
    """Refresh the signing keys first: an install that updates months later can fail on outdated keyring signatures."""
    return _with_config(["pacman", "-Sy", "--needed", "archlinux-keyring"], config)


def _refresh_sudo() -> None:
    """Builds can outlast sudo's password timestamp: renew it before the next root step."""
    if os.geteuid() != 0 and shutil.which("sudo"):
        _run(["sudo", "-v"])


def temp_pacman_conf() -> str:
    """A copy of pacman.conf without the [tapkich] block, for this run only. Returns its path ('' if it failed)."""
    path = ""
    try:
        with open(PACMAN_CONF) as f:
            text = without_repo(f.read())
        if has_repo(text):
            return ""
        fd, path = tempfile.mkstemp(prefix="tapkich-pacman-", suffix=".conf")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(path, 0o644)
        done, path = path, ""
        return done
    except OSError:
        return ""
    finally:
        if path:                                       # failed half way: do not leave the file behind
            try:
                os.unlink(path)
            except OSError:
                pass


def main(argv=None) -> int:
    args = list(argv or [])
    if any(a not in YES_FLAGS for a in args):
        print(USAGE, file=sys.stderr)
        return 2
    yes = bool(args)                                  # unattended: no questions from pacman or paru
    conf = read_conf()
    steps = []
    _say("Updating everything: TapkicH-OS, the system, AUR packages and Flatpak apps. You may be asked for your password.")

    _head("TapkicH-OS")
    ch = channel_step(conf)
    _say(f"  {ch.note}")
    steps.append(ch)

    _head("System packages (pacman)")
    s = Step("System packages")
    config = temp_pacman_conf() if ch.skip_repo else ""
    try:
        if ch.skip_repo and not config:
            s.result, s.note = "failed", "could not prepare pacman's configuration without the TapkicH-OS repository"
            _say("  " + s.note)
        else:
            # the keyring step and the full upgrade belong together (-Sy alone is a partial upgrade); a failed keyring
            # step does not stop the upgrade
            krc = _run(_sudo(keyring_command(config) + (["--noconfirm"] if yes else [])))
            if krc != 0:
                _say("  the signing keys could not be refreshed first; continuing with the upgrade")
            rc = _run(_sudo(pacman_command(adopt=ch.result == "ok" and not _installed(PACKAGE), config=config)
                            + (["--noconfirm"] if yes else [])))
            s.result = "ok" if rc == 0 else "failed"
    finally:
        if config:                                     # also after Ctrl-C
            try:
                os.unlink(config)
            except OSError:
                pass
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
        _refresh_sudo()
        rc1 = _run(_sudo(["flatpak", "update", "--system", "-y", "--noninteractive"]))
        rc2 = _run(["flatpak", "update", "--user", "-y", "--noninteractive"])
        s.result = "ok" if rc1 == 0 and rc2 == 0 else "failed"
    else:
        s.note = "flatpak is not installed"
    steps.append(s)

    _say("\nSummary")
    for st in steps:
        mark = {"ok": "done   ", "failed": "FAILED ", "skipped": "skipped"}[st.result]
        if st.skip_repo:
            _say(f"  {mark} {st.name}: TapkicH-OS updates skipped this time: {st.note}; the rest updated")
            continue
        _say(f"  {mark} {st.name}" + (f": {st.note}" if st.note and st.result != "ok" else ""))
    failed = [st for st in steps if st.result == "failed"]
    if failed:
        _say("\nSome steps did not finish. Their messages are above; running `tapkich update` again is safe.")
    else:
        _say("\nEverything is up to date. A restart is a good idea if the kernel or drivers were updated.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
