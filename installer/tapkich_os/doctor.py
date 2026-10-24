"""`tapkich doctor`: a read-only health check in plain language.

Graphics card and driver, Vulkan, Gaming Mode readiness, Secure Boot, Flathub, disk space and the update channel.
`gather()` collects the facts (files, commands); `evaluate(facts)` turns them into verdicts and is unit-tested with
made-up facts.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from . import gaming, nvidia, restore, secureboot, secureboot_setup, update

OK, NOTE, PROBLEM = "ok", "note", "problem"


@dataclass
class Verdict:
    area: str
    level: str          # ok / note / problem
    text: str
    fix: str = ""


def _out(cmd: list, timeout: float = 20) -> tuple:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def parse_vulkan_summary(text: str) -> list:
    """`vulkaninfo --summary` -> [(deviceName, driverName)]."""
    names = re.findall(r"^\s*deviceName\s*=\s*(.+)$", text, re.M)
    drivers = re.findall(r"^\s*driverName\s*=\s*(.+)$", text, re.M)
    return [(n.strip(), (drivers[i].strip() if i < len(drivers) else "")) for i, n in enumerate(names)]


def windows_boot_state():
    """"ok" / "missing" / None. Best-effort and silent: reading the disks needs root, so as a normal user this finds no
    Windows and says nothing (`sudo tapkich restore windows --check` gives the full answer)."""
    try:
        import contextlib
        import io
        from . import detect
        with contextlib.redirect_stderr(io.StringIO()):
            parts = restore.windows_partitions(detect.detect_system())
        if len(parts) != 1:
            return None
        nvram = detect.parse_efibootmgr(_out(["efibootmgr", "-v"])[1])
        return "ok" if restore.has_entry(nvram, parts[0]) else "missing"
    except Exception:
        return None


def gather(root: str = "/") -> dict:
    f: dict = {}
    f["gpus"] = nvidia.detect()
    rc, pkgs = _out(["pacman", "-Qq"])
    f["packages"] = set(pkgs.split()) if rc == 0 else set()
    f["nvidia_loaded"] = os.path.isdir("/sys/module/nvidia")
    f["nvidia_modeset"] = _read("/sys/module/nvidia_drm/parameters/modeset")
    f["nvidia_fbdev"] = _read("/sys/module/nvidia_drm/parameters/fbdev")
    if shutil.which("vulkaninfo"):
        rc, out = _out(["vulkaninfo", "--summary"])
        f["vulkan"] = parse_vulkan_summary(out) if rc == 0 else []
        f["vulkan_tool"] = True
    else:
        f["vulkan"], f["vulkan_tool"] = [], False
    f["vulkan_icds"] = sorted(os.listdir("/usr/share/vulkan/icd.d")) if os.path.isdir("/usr/share/vulkan/icd.d") else []
    f["gaming_files"] = {p: os.path.exists(p) for p in (
        "/usr/bin/gamescope", "/usr/bin/gamescope-session-plus", f"{gaming.SESSIONS_DIR}/{gaming.GAMING_SESSION}",
        f"{gaming.SESSIONS_DIR}/{gaming.RETURN_SESSION}", gaming.SWITCH, gaming.OS_SESSION_SELECT, "/usr/bin/steam")}
    f["sudo_rule"] = True                     # /etc/sudoers.d is root-only: what counts is whether sudo allows it
    rc, _ = _out(["sudo", "-n", "-l", gaming.SWITCH, "gaming"], timeout=5)
    f["sudo_rule_works"] = rc == 0
    rc, out = _out(["systemctl", "is-enabled", gaming.RESET_UNIT], timeout=5)
    f["reset_unit"] = out.strip()
    f["switcheroo"] = _out(["systemctl", "is-active", "switcheroo-control"], timeout=5)[1].strip()
    f["sb"] = secureboot.read_state(root)
    f["sb_ours"] = secureboot_setup.read_state_file().get("enrolled") == "yes"
    rc, out = _out(["flatpak", "remotes", "--system", "--columns=name"], timeout=10)
    f["flatpak"] = shutil.which("flatpak") is not None
    f["flathub"] = "flathub" in out.split()
    f["disk"] = {}
    for mount in ("/", "/boot"):
        try:
            u = shutil.disk_usage(mount)
            f["disk"][mount] = (u.free, u.total)
        except OSError:
            pass
    f["windows_boot"] = windows_boot_state()
    conf = update.read_conf()
    f["update_enabled"] = conf["enabled"]
    f["update_channel"] = update.probe(conf["url"], timeout=8) if conf["enabled"] != "no" else "off"
    return f


GIB = 1024 ** 3
MIB = 1024 ** 2


def evaluate(f: dict) -> list:
    v: list[Verdict] = []
    gpus = f.get("gpus", [])
    pkgs = f.get("packages", set())
    plan = nvidia.choose(gpus)

    # ---- graphics
    if not gpus:
        v.append(Verdict("Graphics", NOTE, "no graphics card found by lspci (a VM, or pciutils is missing)"))
    for g in gpus:
        v.append(Verdict("Graphics", OK, f"{g.name}: driver {g.driver or 'none loaded'}"))
    nv = nvidia.nvidia_gpus(gpus)
    if nv:
        if plan["kind"] == "turing+":
            missing = [p for p in nvidia.OPEN_PACKAGES if p not in pkgs] + \
                      ([p for p in nvidia.HYBRID_PACKAGES if p not in pkgs] if plan["hybrid"] else [])
            if missing:
                v.append(Verdict("NVIDIA", PROBLEM, "NVIDIA's driver packages are missing: " + ", ".join(missing),
                                 "sudo pacman -S --needed " + " ".join(missing) + "   (then restart)"))
            elif not f.get("nvidia_loaded"):
                v.append(Verdict("NVIDIA", PROBLEM, "the NVIDIA driver is installed but not loaded",
                                 "Restart. If it stays like this: `sudo dkms autoinstall` and check `journalctl -b | grep -i nvidia`."))
            else:
                v.append(Verdict("NVIDIA", OK, "NVIDIA open driver loaded"))
                if f.get("nvidia_modeset") != "Y":
                    v.append(Verdict("NVIDIA", PROBLEM, "kernel modesetting is off (Hyprland and Gaming Mode need it)",
                                     f"Add `{nvidia.CMDLINE}` to the kernel command line in /boot/EFI/limine/limine.conf" +
                                     (", then run `sudo tapkich secureboot refresh` (Secure Boot checks the config)."
                                      if f.get("sb_ours") else ".")))
                else:
                    v.append(Verdict("NVIDIA", OK, "kernel modesetting on" +
                                     (", framebuffer console on" if f.get("nvidia_fbdev") == "Y" else "")))
            if plan["hybrid"]:
                if f.get("switcheroo") == "active":
                    v.append(Verdict("NVIDIA", OK, "hybrid graphics: the screen runs on the built-in GPU; Gaming Mode "
                                     "starts Steam on NVIDIA (switcheroo-control); `prime-run <app>` for other apps"))
                else:
                    v.append(Verdict("NVIDIA", PROBLEM, "hybrid graphics, but switcheroo-control is not running "
                                     "(Gaming Mode would start Steam on the built-in GPU)",
                                     "sudo systemctl enable --now switcheroo-control"))
        else:
            v.append(Verdict("NVIDIA", NOTE, plan["note"]))

    # ---- Vulkan (games and gamescope need a real Vulkan device)
    devices = f.get("vulkan", [])
    if not f.get("vulkan_tool"):
        v.append(Verdict("Vulkan", NOTE, "drivers: " + (", ".join(f.get("vulkan_icds", [])) or "none found")
                         + " (install vulkan-tools for a full check)"))
    elif not devices:
        v.append(Verdict("Vulkan", PROBLEM, "no Vulkan device: games and Gaming Mode will not work (normal in a "
                         "virtual machine without 3D acceleration)",
                         "Check the graphics driver above."))
    else:
        hw = [d for d in devices if "llvmpipe" not in d[0].lower() and "lavapipe" not in d[1].lower()]
        if hw:
            v.append(Verdict("Vulkan", OK, "; ".join(f"{n} ({d})" if d else n for n, d in hw)))
        else:
            v.append(Verdict("Vulkan", PROBLEM, "only software Vulkan (llvmpipe): games will be very slow and "
                             "Gaming Mode cannot start (normal in a virtual machine)", "Check the graphics driver above."))

    # ---- Gaming Mode
    files = f.get("gaming_files", {})
    missing = [p for p, there in files.items() if not there]
    if missing:
        v.append(Verdict("Gaming Mode", PROBLEM, "missing: " + ", ".join(missing),
                         "sudo pacman -S --needed steam gamescope (the session files come with TapkicH-OS: `tapkich update`)"))
    else:
        v.append(Verdict("Gaming Mode", OK, "gamescope, Steam and the Gaming Mode session are installed"))
    if not f.get("sudo_rule") or not f.get("sudo_rule_works"):
        v.append(Verdict("Gaming Mode", PROBLEM, "the password-free session switch is not allowed (the button would fail)",
                         f"{gaming.SUDOERS} must exist (it comes with TapkicH-OS)"))
    else:
        v.append(Verdict("Gaming Mode", OK, "the Gaming Mode button may switch sessions without a password"))
    if f.get("reset_unit") != "enabled":
        v.append(Verdict("Gaming Mode", NOTE, "the boot reset of a pending Gaming Mode switch is not enabled",
                         f"sudo systemctl enable {gaming.RESET_UNIT}"))
    if plan["kind"] in ("legacy", "old"):
        v.append(Verdict("Gaming Mode", NOTE, "gamescope needs a current GPU driver; on this NVIDIA card it may not start"))

    # ---- Secure Boot
    sb = f.get("sb", {})
    if not sb.get("uefi"):
        v.append(Verdict("Secure Boot", NOTE, "started in legacy BIOS mode: no Secure Boot"))
    elif sb.get("secure_boot"):
        ms = sb.get("ms_in_db") or sb.get("ms_in_kek")
        v.append(Verdict("Secure Boot", OK, "on" + (", with TapkicH-OS keys" if sb.get("tapkich_key_in_db") or f.get("sb_ours") else "")
                         + (" and Microsoft's keys (Windows keeps starting)" if ms else "")))
        if not ms:
            v.append(Verdict("Secure Boot", PROBLEM, "Microsoft's keys are not in the firmware: Windows and some graphics "
                             "cards may refuse to start"))
    elif sb.get("setup_mode"):
        v.append(Verdict("Secure Boot", NOTE, "off; the firmware is in Setup Mode, ready for our keys",
                         "sudo tapkich secureboot setup"))
    elif f.get("sb_ours"):
        v.append(Verdict("Secure Boot", NOTE, "our keys are enrolled, but Secure Boot is off",
                         "Turn Secure Boot on in the firmware settings."))
    else:
        v.append(Verdict("Secure Boot", NOTE, "off (fine; needed only for some anti-cheat and policies)",
                         "sudo tapkich secureboot setup   (explains how)"))

    # ---- Windows boot entry
    wb = f.get("windows_boot")
    if wb == "ok":
        v.append(Verdict("Windows", OK, 'the firmware has its "Windows Boot Manager" boot entry'))
    elif wb == "missing":
        v.append(Verdict("Windows", PROBLEM, 'the firmware lost its "Windows Boot Manager" boot entry (some firmware '
                         "does this when Secure Boot is switched), so Windows is not in its boot menu",
                         "sudo tapkich restore windows"))

    # ---- Flathub
    if not f.get("flatpak"):
        v.append(Verdict("Apps", NOTE, "Flatpak is not installed (Bazaar needs it)", "sudo pacman -S flatpak bazaar"))
    elif f.get("flathub"):
        v.append(Verdict("Apps", OK, "Flathub is set up for Bazaar"))
    else:
        v.append(Verdict("Apps", PROBLEM, "Flathub is missing (Bazaar shows no apps)",
                         "sudo flatpak remote-add --system --if-not-exists flathub "
                         "https://dl.flathub.org/repo/flathub.flatpakrepo"))

    # ---- disk space
    disk = f.get("disk", {})
    if "/" in disk:
        free, total = disk["/"]
        if free < 5 * GIB:
            v.append(Verdict("Disk", PROBLEM, f"only {free / GIB:.1f} GiB free on the system disk: updates may fail",
                             "Free some space (Steam games, ~/Downloads; `paru -Sc` clears the package cache)."))
        elif free < 15 * GIB:
            v.append(Verdict("Disk", NOTE, f"{free / GIB:.1f} GiB free on the system disk (games need a lot more)"))
        else:
            v.append(Verdict("Disk", OK, f"{free / GIB:.0f} GiB free of {total / GIB:.0f} GiB"))
    if "/boot" in disk:
        free, _ = disk["/boot"]
        if free < 120 * MIB:
            v.append(Verdict("Disk", PROBLEM, f"the boot partition has only {free / MIB:.0f} MiB free: a kernel update "
                             "may not fit", "Remove old files from /boot that no boot entry uses."))
        else:
            v.append(Verdict("Disk", OK, f"boot partition: {free / MIB:.0f} MiB free"))

    # ---- updates
    ch = f.get("update_channel")
    if ch == "published":
        v.append(Verdict("Updates", OK, "the TapkicH-OS update channel is reachable (`tapkich update`)"))
    elif ch == "not-published":
        v.append(Verdict("Updates", NOTE, update.NOT_PUBLISHED))
    elif ch == "off":
        v.append(Verdict("Updates", NOTE, f"TapkicH-OS updates are switched off in {update.CONF}"))
    else:
        v.append(Verdict("Updates", NOTE, "could not reach the TapkicH-OS update server (offline?)"))
    return v


def render(verdicts: list) -> str:
    mark = {OK: "[ ok ]", NOTE: "[note]", PROBLEM: "[FIX ]"}
    lines, area = ["TapkicH-OS doctor", ""], None
    for x in verdicts:
        if x.area != area:
            area = x.area
            lines.append(area)
        lines.append(f"  {mark[x.level]} {x.text}")
        if x.fix and x.level != OK:
            lines.append(f"         -> {x.fix}")
    problems = sum(1 for x in verdicts if x.level == PROBLEM)
    lines += ["", "Everything looks fine." if not problems else
              f"{problems} thing(s) to fix: the lines marked [FIX ] say how."]
    return "\n".join(lines)


def main() -> int:
    verdicts = evaluate(gather())
    print(render(verdicts))
    return 1 if any(x.level == PROBLEM for x in verdicts) else 0
