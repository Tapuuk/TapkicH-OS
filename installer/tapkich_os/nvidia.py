"""Graphics cards: what is in the machine (lspci) and, for NVIDIA, which driver to install.

NVIDIA's driver is proprietary in user space (nvidia-utils), so it is never on our ISO: the installer downloads it
from Arch's official repositories at install time, only on machines that have an NVIDIA card.

Choices (Arch repos checked 2026-10-19, driver 615.78):
  - Turing (GTX 16xx / RTX 20xx) and newer: NVIDIA's open kernel modules. Our kernel is linux-zen, and Arch's
    prebuilt `nvidia-open` is only for the stock `linux` kernel, so we use `nvidia-open-dkms` + `linux-zen-headers`
    (built once at install time, and again by DKMS after each kernel update). Plus nvidia-utils, lib32-nvidia-utils
    (32-bit games / Steam), egl-wayland (Wayland), libva-nvidia-driver (video decoding).
  - Maxwell, Pascal, Volta (GTX 750 ... GTX 10xx, Titan V): NVIDIA dropped them from the current driver; only the
    legacy 580 series supports them, and Arch moved it to the AUR (nvidia-580xx-dkms). We do not install that
    automatically (unofficial packaging of proprietary software); the open nouveau driver stays, with its Vulkan
    driver vulkan-nouveau (NVK) so games start (slower than NVIDIA's own driver), and `tapkich doctor` explains the AUR route.
  - Kepler and older: no current driver at all; nouveau.
  - Hybrid laptops (the screen on Intel/AMD graphics, boot_vga, + NVIDIA): PRIME render offload. The screen stays on the integrated GPU,
    `prime-run <app>` starts an app on NVIDIA, and switcheroo-control lets gamescope-session start Steam on NVIDIA
    (gamescope-session-plus runs `switcherooctl launch steam` when it is installed).
The architecture is decided by the PCI device id: Turing and later chips all have ids from 0x1E00 up, every earlier
chip is below (Volta GV100 0x1DB*, Pascal GP108 0x1D0*-0x1D1*). A VM has no NVIDIA card: this is unit-tested only.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

NVIDIA, AMD, INTEL = "10de", "1002", "8086"
DISPLAY_CLASSES = ("0300", "0302", "0380")          # VGA, 3D controller (laptop dGPUs), display controller
TURING_FIRST_ID = 0x1E00
MAXWELL_FIRST_ID = 0x1340

OPEN_PACKAGES = ["nvidia-open-dkms", "linux-zen-headers", "nvidia-utils", "lib32-nvidia-utils", "egl-wayland",
                 "libva-nvidia-driver"]
LEGACY_VULKAN = ["vulkan-nouveau", "lib32-vulkan-nouveau"]     # Vulkan for DXVK/Proton/gamescope on the open driver
HYBRID_PACKAGES = ["nvidia-prime", "switcheroo-control"]
EARLY_KMS_MODULES = ["nvidia", "nvidia_modeset", "nvidia_uvm", "nvidia_drm"]
CMDLINE = "nvidia_drm.modeset=1 nvidia_drm.fbdev=1"
MODPROBE_FILE = "/etc/modprobe.d/tapkich-nvidia.conf"
MODPROBE_CONF = """# TapkicH-OS (NVIDIA): kernel modesetting and the framebuffer console on NVIDIA's driver. Hyprland and gamescope
# (Gaming Mode) need modesetting; video memory is kept across suspend so a Wayland desktop survives sleep.
options nvidia_drm modeset=1 fbdev=1
options nvidia NVreg_PreserveVideoMemoryAllocations=1
"""
MKINITCPIO_FILE = "/etc/mkinitcpio.conf.d/tapkich-nvidia.conf"
MKINITCPIO_CONF = ("# TapkicH-OS (NVIDIA): load the driver early (early KMS), so the boot screen and the login screen "
                   "use it\nMODULES+=(" + " ".join(EARLY_KMS_MODULES) + ")\n")
SUSPEND_UNITS = ["nvidia-suspend.service", "nvidia-resume.service", "nvidia-hibernate.service"]
LEGACY_AUR = "nvidia-580xx-dkms nvidia-580xx-utils lib32-nvidia-580xx-utils"


@dataclass
class Gpu:
    slot: str
    vendor: str            # PCI vendor id, lower-case hex
    device: str            # PCI device id, lower-case hex
    name: str
    driver: str = ""       # kernel driver in use ("" = none)
    boot_vga: bool | None = None   # the card the firmware used for the boot screen (None = could not be read)

    @property
    def is_nvidia(self) -> bool:
        return self.vendor == NVIDIA

    @property
    def family(self) -> str:
        """'turing+' (open driver), 'legacy' (Maxwell/Pascal/Volta: 580xx only), 'old' (Kepler and older), or '' for
        other vendors."""
        if not self.is_nvidia:
            return ""
        dev = int(self.device, 16)
        if dev >= TURING_FIRST_ID:
            return "turing+"
        return "legacy" if dev >= MAXWELL_FIRST_ID else "old"


_LINE = re.compile(r"^(\S+)\s+(.+?)\s+\[(\w{4})\]:\s+(.*)\s+\[(\w{4}):(\w{4})\](?:\s+\(rev \w+\))?\s*$")


def parse_lspci(text: str) -> list:
    """`lspci -nnk` output -> the display devices (graphics cards), in order."""
    gpus, cur = [], None
    for line in text.splitlines():
        if not line.startswith(("\t", " ")):
            cur = None
            m = _LINE.match(line)
            if m and m.group(3) in DISPLAY_CLASSES:
                cur = Gpu(m.group(1), m.group(5).lower(), m.group(6).lower(), m.group(4).strip())
                gpus.append(cur)
        elif cur is not None and "Kernel driver in use:" in line:
            cur.driver = line.split(":", 1)[1].strip()
    return gpus


def read_boot_vga(slot: str, sysfs: str = "/sys/bus/pci/devices") -> bool | None:
    """True/False from the card's boot_vga file; None when it cannot be read. lspci -nnk prints the slot without the
    PCI domain, sysfs names it with one (0000:01:00.0)."""
    full = slot if slot.count(":") >= 2 else "0000:" + slot
    try:
        with open(f"{sysfs}/{full}/boot_vga") as f:
            return f.read().strip() == "1"
    except OSError:
        return None


def detect(run=subprocess.run, sysfs: str = "/sys/bus/pci/devices") -> list:
    try:
        p = run(["lspci", "-nnk"], capture_output=True, text=True)
        gpus = parse_lspci(p.stdout or "")
    except (OSError, AttributeError):
        return []
    for g in gpus:
        g.boot_vga = read_boot_vga(g.slot, sysfs)
    return gpus


def nvidia_gpus(gpus: list) -> list:
    return [g for g in gpus if g.is_nvidia]


def is_hybrid(gpus: list) -> bool:
    """An NVIDIA card next to Intel or AMD graphics that drives the screen (a laptop). A desktop whose monitor is on the
    NVIDIA card is not hybrid, even with the CPU's graphics enabled. When no card reports being the boot screen
    (boot_vga unreadable), any NVIDIA card next to Intel/AMD graphics counts as hybrid."""
    if not nvidia_gpus(gpus):
        return False
    if any(g.boot_vga for g in gpus):
        return any(g.boot_vga and g.vendor in (INTEL, AMD) for g in gpus)
    return any(g.vendor in (INTEL, AMD) for g in gpus)


def choose(gpus: list) -> dict:
    """What the installer does for these cards: {packages, files, cmdline, units, kind, note}."""
    nv = nvidia_gpus(gpus)
    plan = {"packages": [], "files": {}, "cmdline": "", "units": [], "kind": "none", "hybrid": False, "note": ""}
    if not nv:
        return plan
    best = "turing+" if any(g.family == "turing+" for g in nv) else ("legacy" if any(g.family == "legacy" for g in nv) else "old")
    plan["kind"] = best
    if best == "turing+":
        plan["packages"] = list(OPEN_PACKAGES)
        plan["files"] = {MODPROBE_FILE: MODPROBE_CONF, MKINITCPIO_FILE: MKINITCPIO_CONF}
        plan["cmdline"] = CMDLINE
        plan["units"] = list(SUSPEND_UNITS)
        if is_hybrid(gpus):
            plan["hybrid"] = True
            plan["packages"] += HYBRID_PACKAGES
            plan["units"].append("switcheroo-control.service")
        plan["note"] = "NVIDIA open driver (nvidia-open-dkms) for " + ", ".join(g.name for g in nv)
    elif best == "legacy":
        plan["packages"] = list(LEGACY_VULKAN)
        plan["note"] = (f"{nv[0].name}: NVIDIA's current driver no longer supports this card (Maxwell/Pascal/Volta). "
                        "The open nouveau driver is used, with its Vulkan driver (NVK), so games run, slower than on "
                        "NVIDIA's own driver. NVIDIA's legacy "
                        f"580 driver is only in the AUR: `paru -S {LEGACY_AUR}` (unofficial packaging).")
    else:
        plan["note"] = f"{nv[0].name}: too old for any current NVIDIA driver; the open nouveau driver is used."
    return plan


def hypr_env(plan: dict) -> str:
    """Lines for Caelestia's hypr-user.lua (Hyprland's NVIDIA advice). On hybrid laptops the desktop runs on the
    integrated GPU, so the NVIDIA-only variables are not set there (they would push every app onto NVIDIA)."""
    if plan.get("kind") != "turing+" or plan.get("hybrid"):
        return ""
    return ("-- NVIDIA (found by the installer): video decoding and OpenGL through NVIDIA's driver\n"
            'hl.env("LIBVA_DRIVER_NAME", "nvidia")\n'
            'hl.env("__GLX_VENDOR_LIBRARY_NAME", "nvidia")\n'
            'hl.env("NVD_BACKEND", "direct")\n')
