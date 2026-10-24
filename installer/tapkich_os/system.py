"""System install stage: runs after the partitions exist, as Applier's `bootloader` hook.

  mount (ext4, or btrfs subvolumes; our ESP at /boot) -> pacstrap (Arch's official linux-zen kernel) -> configure in chroot
  -> Limine on OUR ESP -> UEFI entry -> Windows chainload entry -> unmount

Only official Arch repositories are used (plus a local repo on the ISO holding paru, built from the AUR): no CachyOS
repo, keyring or mirrorlist, because their repository policy forbids shipping them in another distribution.

Safety: this module only ever writes to the partitions apply created (mounted under /mnt). The one thing
it does outside them is add ONE UEFI boot entry with efibootmgr; it never deletes or reorders entries
that are not its own, and aborts (-> rollback) if a Windows entry disappears.
"""
from __future__ import annotations

import glob
import json
import math
import os
import time
import re
import tomllib
from dataclasses import dataclass
from typing import Optional

import subprocess
from collections import deque

from .apply import ApplyError, ApplyResult, _run
from .model import OUR_ESP_NAME, OUR_ROOT_NAME, Snapshot
from .planner import Plan, windows_boot_esps
from .progress import NULL, Progress, Span

# Where each install step sits on the bar (percent). Packages are most of the work; the AUR apps (built one by one,
# minutes each) get their own real slice instead of one frozen number.
STAGES = {"download": (14, 38), "install": (38, 70), "fallback": 71, "configure": 73, "refresh": (73.5, 74.9), "desktop": (75, 89),
          "aur": (89, 93),
          "encrypt": 93, "limine": 94, "entry": 95, "secureboot": 96, "unmount": 97}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def short_line(line: str, width: int = 60) -> str:
    """One output line, tidied for the sub-text under the bar."""
    line = " ".join(line.replace("==>", "").replace("->", "").replace("::", "").split())
    return line if len(line) <= width else line[:width - 1] + "\u2026"


def aur_fraction(line: str) -> float:
    """How far one AUR package is, judged from paru / makepkg output (only ever used to move forward)."""
    low = line.lower()
    for marker, frac in (("installing package", 0.85), ("loading packages", 0.85), ("checking keys", 0.8),
                         ("finished making", 0.75), ("tidying install", 0.7), ("entering fakeroot", 0.6),
                         ("starting build", 0.45), ("starting prepare", 0.35), ("extracting sources", 0.3),
                         ("validating source", 0.25), ("retrieving sources", 0.15), ("downloading", 0.1),
                         ("cloning", 0.05)):
        if marker in low:
            return frac
    return 0.0
from . import apps, caelestia, desktop, gaming, nvidia, package, shell, tuning

MNT = "/mnt"
KERNEL = "linux-zen"                       # Arch's official kernel package: vmlinuz-linux-zen, initramfs-linux-zen*.img
LIMINE_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "limine")
WALLPAPER_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "wallpapers")
FLATHUB_REPO_FILE = "/usr/share/flatpak/remotes.d/flathub.flatpakrepo"   # in the flatpak package
LOCAL_REPO = "/opt/tapkich-repo"           # on the ISO: paru built from the AUR (see iso/build-aur.sh)
ZONEINFO = "/usr/share/zoneinfo"
OS_RELEASE, OS_RELEASE_HOOK, LIMINE_HOOK, TAPKICH_WRAPPER = (package.OS_RELEASE, package.OS_RELEASE_HOOK,
                                                  package.LIMINE_HOOK, package.TAPKICH_WRAPPER)
PACKAGES = [
    "base", "e2fsprogs", KERNEL, "linux-firmware", "amd-ucode", "intel-ucode",
    "limine", "efibootmgr", "networkmanager", "sudo", "nano", "git", "github-cli", "python",
    "sbctl", "pciutils", "vulkan-tools",       # Secure Boot keys (`tapkich secureboot setup`); `tapkich doctor`
    "sof-firmware", "wireless-regdb",          # sound on recent Intel/AMD laptops; the right Wi-Fi channels for the country
    "mtools", "ntfsprogs",                     # disk detection reads EFI and Windows partitions (`tapkich restore windows`, doctor)
    *tuning.TUNING_PACKAGES,
    *desktop.DESKTOP_PACKAGES,
    *caelestia.OFFICIAL_PACKAGES,
    *apps.APP_PACKAGES,
    *gaming.OFFICIAL_PACKAGES,
    *shell.SHELL_PACKAGES,
]
PACKAGES = list(dict.fromkeys(PACKAGES))      # a package named in two lists is installed once


def packages_for(root_fs: str, with_paru: bool = True, gpu=(), with_tapkich_package: bool = False) -> list:
    """ext4 needs nothing extra (e2fsprogs is in base); btrfs needs btrfs-progs. paru, Caelestia shell (with its
    AUR dependencies), the user's AUR apps and our own tapkich-os package come from the local repo on the ISO.
    `gpu`: the graphics driver packages for this machine (nvidia.choose), from the official repos."""
    local = ["paru", *caelestia.PACKAGES, *apps.LOCAL_APPS, *gaming.LOCAL_PACKAGES] if with_paru else []
    if with_tapkich_package:
        local.append(package.NAME)
    extra = (["btrfs-progs"] if root_fs == "btrfs" else []) + [p for p in gpu if p not in PACKAGES]
    return PACKAGES + extra + local


def with_local_repo(conf: str, path: str = LOCAL_REPO) -> str:
    """pacman.conf with the local ISO repo (holding paru) above the official repos. Used for pacstrap only: the
    installed system's pacman.conf stays the plain official one."""
    block = f"[tapkich-local]\nSigLevel = Optional TrustAll\nServer = file://{path}\n\n"
    new, n = re.subn(r"^\[core\]", block + "[core]", conf, count=1, flags=re.M)
    if n != 1:
        raise ApplyError("PACMAN_CONF", "cannot find [core] in /etc/pacman.conf")
    return new


_PKG_VERSION = re.compile(r"-[0-9][^-\s]*-[0-9]+(?:-(?:x86_64|any|aarch64))?$")


def strip_version(token: str) -> str:
    """'ntfs-3g-2022.10.3-1-x86_64' -> 'ntfs-3g' (only the trailing -pkgver-pkgrel[-arch] goes)."""
    return _PKG_VERSION.sub("", token)


def parse_pacman_line(line: str, st: dict):
    """Feed one line of pacstrap/pacman output; update st {total, downloaded, installed}; return a short
    (kind, name) event or None. Works on the plain (non-tty) output pacman prints into a pipe."""
    m = re.match(r"^Packages \((\d+)\)", line)
    if m:
        st["total"] = int(m.group(1))
        return ("plan", "")
    m = re.match(r"^\s*(\S+)\s+downloading\.\.\.", line) or re.match(r"^\s*downloading (\S+?)\.\.\.", line)
    if m:
        st["downloaded"] = st.get("downloaded", 0) + 1
        return ("download", strip_version(m.group(1)))
    m = re.match(r"^(?:\(\s*\d+/\d+\)\s+)?installing (\S+?)\.\.\.", line)
    if m:
        st["installed"] = st.get("installed", 0) + 1
        return ("install", m.group(1))
    return None


THEME_PATHS = ["/usr/share/tapkich-os/theme/colors.toml"]
# Exactly two brand colours (violet + orchid) and neutrals; mirrors theme/tapkich-purple/colors.toml.
DEFAULT_THEME = {
    "primary": "#7c3aed", "secondary": "#c084fc",
    "gradient_start": "#7c3aed", "gradient_end": "#c084fc",
    "background": "#0c0a12", "foreground": "#d4d2da", "dark_foreground": "#6e6b78", "accent": "#7c3aed",
    # the rest of the palette (the desktop configs need all of it if the theme file is missing)
    "dark_background": "#09070e", "darker_background": "#06050a", "lighter_background": "#17141f",
    "selection": "#241d33", "muted": "#3a3548", "light_foreground": "#b4b1bd", "bright_foreground": "#eeedf2",
    "danger": "#e5484d", "danger_bright": "#ff6b6b", "corner_radius": 20,
}


@dataclass
class SystemAnswers:
    username: str
    password: str
    hostname: str = "tapkich-os"
    timezone: str = "UTC"
    keyboard: str = "us"              # console keymap of the main layout
    xkb_layouts: str = ""             # all chosen layouts as XKB codes, main first, e.g. "us,de" ("" = just `keyboard`)
    locale: str = "en_US"             # system language, e.g. de_DE (generated as de_DE.UTF-8)
    font: str = ""                    # console font for non-Latin scripts, e.g. cyr-sun16
    selftest: bool = False            # test builds: serial console + power off after first boot report
    keybinds: str = "tapkich"         # "tapkich" (our keys) or "caelestia" (Caelestia's own), asked by the installer
    extra_apps: bool = True           # install the AUR apps (Waterfox, Claude, ...) at install time; tests turn it off
    disk_password: str = ""           # separate disk-encryption password ("" = the login password is also the disk password)
    wifi_ssid: str = ""               # Wi-Fi network used while installing; saved on the new system ("" = none)
    wifi_password: str = ""           # its password ("" = an open network)
    wifi_hidden: bool = False         # the network does not announce its name (joined as a hidden network)
    secure_boot: bool = False         # set up Secure Boot (our keys + Microsoft's); only possible in Setup Mode

    @staticmethod
    def from_file(path: str) -> "SystemAnswers":
        with open(path) as f:
            raw = json.load(f)
        keys = ("username", "password", "hostname", "timezone", "keyboard", "xkb_layouts", "locale", "font", "selftest",
                "disk_password", "wifi_ssid", "wifi_password", "wifi_hidden", "extra_apps", "keybinds", "secure_boot")
        return SystemAnswers(**{k: raw[k] for k in keys if k in raw})


def render_wifi_profile(ssid: str, password: str, uuid: str, hidden: bool = False) -> str:
    """NetworkManager keyfile for the Wi-Fi network chosen during the install, so the new system connects by itself.
    A 64-digit hex key goes in `psk=` as it is (NetworkManager accepts the raw key). Installed with mode 0600."""
    lines = ["[connection]", f"id={ssid}", f"uuid={uuid}", "type=wifi", "autoconnect=true", "",
             "[wifi]", "mode=infrastructure", f"ssid={ssid}", *(["hidden=true"] if hidden else []), ""]
    if password:
        lines += ["[wifi-security]", "key-mgmt=wpa-psk", f"psk={password}", ""]
    lines += ["[ipv4]", "method=auto", "", "[ipv6]", "addr-gen-mode=default", "method=auto", ""]
    return "\n".join(lines)


def theme_toml(theme: dict) -> str:
    """The theme as a colors.toml (strings quoted, numbers bare)."""
    lines = ["# TapkicH-OS theme (written by the installer; the theme switcher edits this file)"]
    for key, value in theme.items():
        lines.append(f"{key} = {value!r}".replace("'", '"') if isinstance(value, str) else f"{key} = {value}")
    return "\n".join(lines) + "\n"


def load_theme() -> dict:
    for p in THEME_PATHS:
        try:
            with open(p, "rb") as f:
                return {**DEFAULT_THEME, **tomllib.load(f)}
        except OSError:
            continue
    return dict(DEFAULT_THEME)


def render_xkb_conf(layouts: str) -> str:
    """X11/Wayland keyboard config for all chosen layouts (main first). Read by SDDM and compositors."""
    if not re.fullmatch(r"[a-z]{2,8}(,[a-z]{2,8})*", layouts):
        raise ApplyError("BAD_ANSWER", f"unsafe keyboard layout list {layouts!r}")
    return (
        'Section "InputClass"\n'
        '    Identifier "system-keyboard"\n'
        '    MatchIsKeyboard "on"\n'
        f'    Option "XkbLayout" "{layouts}"\n'
        '    Option "XkbOptions" "grp:alt_shift_toggle"\n'
        'EndSection\n'
    )


# System accounts and groups that exist on an Arch + TapkicH-OS install; a user may not take these names.
# Mirrored in the wizard (iso/overlay/usr/local/bin/tapkich-setup, username_screen): keep both lists the same.
RESERVED_USERS = frozenset("""root bin daemon mail ftp http nobody dbus uuidd dnsmasq rpc ntp avahi colord polkitd rtkit
sddm git geoclue usbmux flatpak nm-openconnect nm-openvpn alpm tss games saned brltty cups wheel adm sys users
gamemode""".split())


def validate_answers(a: "SystemAnswers", zoneinfo: str = ZONEINFO) -> None:
    """Reject unsafe or impossible answers BEFORE any disk is touched (these values end up in shell commands and
    system files). Raises ApplyError(BAD_ANSWER)."""
    def bad(what, val=""):
        raise ApplyError("BAD_ANSWER", f"invalid {what}" + (f": {val!r}" if val else ""))

    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", a.username):
        bad("username (lowercase letters, digits, - and _, starting with a letter or _, at most 32)", a.username)
    if a.username in RESERVED_USERS or re.fullmatch(r"systemd-.*", a.username):
        bad("username (this name is used by the system, choose another)", a.username)
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", a.hostname):
        bad("hostname (lowercase letters, digits and -, at most 63, not starting or ending with -)", a.hostname)
    if (not re.fullmatch(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*", a.timezone)
            or not os.path.isfile(os.path.join(zoneinfo, a.timezone))):
        bad("timezone (not a known timezone name)", a.timezone)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", a.keyboard):
        bad("console keymap", a.keyboard)
    if not re.fullmatch(r"[a-z]{2,3}_[A-Z]{2}", a.locale):
        bad("language code", a.locale)
    if a.font and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", a.font):
        bad("console font", a.font)
    render_xkb_conf(resolve_xkb(a))                      # raises BAD_ANSWER for a bad layout list
    if a.keybinds not in caelestia.KEYBIND_CHOICES:
        bad("keybinds (tapkich or caelestia)", a.keybinds)
    if not a.password or any(c in a.password for c in "\n\r\0") or len(a.password) > 1000:
        bad("password (empty, too long, or containing a line break)")
    if a.wifi_ssid:
        if (any(ord(c) < 32 or ord(c) == 127 for c in a.wifi_ssid) or not 1 <= len(a.wifi_ssid.encode()) <= 32
                or a.wifi_ssid != a.wifi_ssid.strip()):
            bad("Wi-Fi network name")
        if a.wifi_password and (any(ord(c) < 32 or ord(c) == 127 for c in a.wifi_password)
                                or not 8 <= len(a.wifi_password) <= 64):
            bad("Wi-Fi password (8 to 63 characters, or 64 hex digits)")


def resolve_xkb(a: "SystemAnswers", tsv: str = "/usr/share/tapkich-os/keyboards.tsv") -> str:
    """XKB layout list for the installed system: the chosen list, else derived from the console keymap
    (a console keymap name like `de-latin1` is not an XKB layout code)."""
    if a.xkb_layouts:
        return a.xkb_layouts
    try:
        with open(tsv) as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 3 and parts[1] == a.keyboard:
                    return parts[2]
    except OSError:
        pass
    return a.keyboard if re.fullmatch(r"[a-z]{2,8}", a.keyboard) else "us"


def _hex(c: str) -> str:
    return c.lstrip("#").lower()


# ---------------------------------------------------------------- pure generators (unit-tested)

def render_limine_conf(root_uuid: str, windows_esps: list, theme: dict | None = None, *,
                       extra_cmdline: str = "", wallpaper: bool = True, serial: bool = False,
                       root_fs: str = "ext4", luks_uuid: str = "", luks_style: str = "systemd",
                       windows_boot_entry: str = "") -> str:
    """limine.conf for our ESP. windows_esps: [(label, partition GUID)] of Windows boot partitions.
    windows_boot_entry: the firmware's own boot entry for Windows ("Windows Boot Manager"), when there is exactly one
    Windows boot partition and its entry exists. The Windows menu entry then restarts straight into it (Limine's
    efi_boot_entry): Windows starts exactly as from the firmware's boot menu, so BitLocker does not ask for its
    recovery key. Chainloading bootmgfw.efi from Limine puts Limine into the measured boot chain, and a BitLocker
    (TPM) Windows then asks for the recovery key every time (seen with Windows 11 in QEMU, 2026-10-20). The direct
    chainload stays as a second entry, for when the firmware entry is gone.
    With luks_uuid the root is inside a LUKS container: the initramfs asks for the passphrase and opens it.
    luks_style: "systemd" (sd-encrypt hook, what the CachyOS/Arch default initramfs uses) or "udev" (encrypt hook)."""
    t = {**DEFAULT_THEME, **(theme or {})}
    flags = "rootflags=subvol=@ " if root_fs == "btrfs" else ""          # only btrfs uses subvolumes
    if luks_uuid and luks_style == "udev":
        root = f"cryptdevice=UUID={luks_uuid}:cryptroot root=/dev/mapper/cryptroot"
    elif luks_uuid:
        root = f"rd.luks.name={luks_uuid}=cryptroot root=/dev/mapper/cryptroot"
    else:
        root = f"root=UUID={root_uuid}"
    cmd = f"{root} {flags}rw quiet loglevel=3 {extra_cmdline}".strip()
    # The look (checked on screenshots in QEMU at 1280x720 and 1920x1080): the blurred dusk picture from the approved
    # mockups, a frosted dark panel 200 px in from every edge with a soft 24 px edge, Terminus font, a light selection
    # bar (Limine draws the selected entry in the text colour with the panel colour as text), grey help keys and an
    # orchid countdown. Only neutrals plus our two brand colours.
    lines = [
        "timeout: 3",
        "default_entry: 1",
        "interface_branding: TapkicH-OS",
        f"interface_branding_colour: {_hex(t['bright_foreground'])}",
        f"interface_help_colour: {_hex(t['dark_foreground'])}",
        f"interface_help_colour_bright: {_hex(t['secondary'])}",
        f"term_foreground: {_hex(t['bright_foreground'])}",
        f"term_foreground_bright: {_hex(t['bright_foreground'])}",
        # 8 palette slots: neutral, then only our two colours, then neutral text: no other hues anywhere
        "term_palette: " + ";".join(_hex(t[k]) for k in (
            "background", "primary", "secondary", "primary", "primary", "secondary", "secondary", "foreground")),
        "term_palette_bright: " + ";".join(_hex(t[k]) for k in (
            "dark_foreground", "secondary", "secondary", "bright_foreground", "secondary", "secondary", "secondary",
            "bright_foreground")),
    ]
    if serial:
        lines.append("serial: yes")
    if wallpaper:
        lines += [
            "wallpaper: boot():/EFI/limine/wallpaper.jpg",
            "wallpaper_style: stretched",
            f"term_background: 61{_hex(t['darker_background'])}",       # 62 % opaque (61 = how see-through)
            "term_margin: 200",
            "term_margin_gradient: 24",
            "term_font: boot():/EFI/limine/font.bin",
            "term_font_size: 8x16",
        ]
    else:
        lines.append(f"term_background: 00{_hex(t['background'])}")
    lines += [
        "",
        "/TapkicH-OS",
        "    comment: based on Arch Linux, with the linux-zen kernel",
        "    protocol: linux",
        f"    path: boot():/vmlinuz-{KERNEL}",
        f"    cmdline: {cmd}",
        f"    module_path: boot():/initramfs-{KERNEL}.img",
        "",
        "/TapkicH-OS (recovery)",
        "    comment: starts with every driver loaded, if the normal start fails",
        "    protocol: linux",
        f"    path: boot():/vmlinuz-{KERNEL}",
        f"    cmdline: {cmd}",
        f"    module_path: boot():/initramfs-{KERNEL}-fallback.img",
    ]
    if windows_boot_entry and len(windows_esps) == 1:
        lines += [
            "",
            "/Windows",
            "    comment: restarts straight into Windows Boot Manager (safe with BitLocker)",
            "    protocol: efi_boot_entry",
            f"    entry: {windows_boot_entry}",
        ]
        windows_esps = [(f"{windows_esps[0][0]} (direct)", windows_esps[0][1], True)]
    for label, guid, *direct in windows_esps:
        lines += [
            "",
            f"/{label}",
            "    comment: " + ("starts Windows Boot Manager from here; with BitLocker Windows asks for its recovery key"
                              if direct else "Windows Boot Manager"),
            "    protocol: efi",
            # by partition GUID, not by disk order: the Windows boot files may be on another disk
            f"    path: guid({guid}):/EFI/Microsoft/Boot/bootmgfw.efi",
        ]
    return "\n".join(lines) + "\n"


def windows_boot_entry(snap, esp_guids: list) -> str:
    """The firmware entry Limine may restart into for Windows: only with exactly one Windows boot partition, and only
    if an entry called "Windows Boot Manager" points at it (efi_boot_entry finds entries by name)."""
    if len(esp_guids) != 1:
        return ""
    names = [e for e in getattr(snap, "nvram", []) if e.label == "Windows Boot Manager"]
    if len(names) == 1 and names[0].partuuid.lower() == esp_guids[0].lower():
        return "Windows Boot Manager"
    return ""


def enable_fallback_preset(text: str) -> str:
    """Turn on mkinitcpio's `fallback` preset in a kernel preset file. Arch's current template only builds `default`
    (the fallback lines are commented out), but our recovery boot entry needs initramfs-<kernel>-fallback.img.
    Idempotent. Raises if the file does not look like a mkinitcpio preset."""
    if "PRESETS=" not in text:
        raise ApplyError("SYSTEM_INSTALL", "unexpected mkinitcpio preset file (no PRESETS line)")
    out, saw_presets = [], False
    for line in text.splitlines():
        if re.match(r"^PRESETS=\(.*\)\s*$", line):
            if not saw_presets:
                out.append("PRESETS=('default' 'fallback')")
                saw_presets = True
            continue                                          # drop the old PRESETS line(s)
        if re.match(r"^#PRESETS=\('default' 'fallback'\)\s*$", line):
            continue                                          # the commented example is replaced by the real line
        m = re.match(r"^#(fallback_(?:config|image|options)=.*)$", line)
        if m:                                                 # (no fallback_uki: we boot with Limine, not a UKI)
            line = m.group(1)
        out.append(line)
    if not saw_presets:
        out.insert(0, "PRESETS=('default' 'fallback')")
    return "\n".join(out) + "\n"


def luks_hook_for(mkinitcpio_conf: str):
    """Return (style, hook) for the initramfs in this mkinitcpio.conf: ("systemd","sd-encrypt") or ("udev","encrypt").
    Raises if there is no HOOKS line with `filesystems` to put the hook before."""
    m = re.search(r"^HOOKS=\((.*?)\)", mkinitcpio_conf, re.M)
    if not m or "filesystems" not in m.group(1).split():
        raise ApplyError("SYSTEM_INSTALL", "cannot find the HOOKS line (with `filesystems`) in /etc/mkinitcpio.conf")
    hooks = m.group(1).split()
    if "sd-encrypt" in hooks or "encrypt" in hooks:
        return ("systemd", "sd-encrypt") if "sd-encrypt" in hooks else ("udev", "encrypt")   # already there
    return ("systemd", "sd-encrypt") if "systemd" in hooks else ("udev", "encrypt")


def parse_disk_and_part(node: str):
    """'/dev/vda3' -> ('/dev/vda', 3); '/dev/nvme0n1p2' -> ('/dev/nvme0n1', 2)."""
    m = (re.fullmatch(r"(/dev/(?:nvme\d+n\d+|mmcblk\d+|loop\d+))p(\d+)", node)
         or re.fullmatch(r"(/dev/[a-z]+)(\d+)", node))
    if not m:
        raise ApplyError("UEFI_ENTRY", f"cannot split {node} into disk and partition number")
    return m.group(1), int(m.group(2))


SELFTEST_UNIT = """[Unit]
Description=TapkicH-OS installed-system self-test
After=multi-user.target
[Service]
Type=oneshot
StandardOutput=journal+console
ExecStart=/bin/sh -c 'echo TAPKICH-INSTALLED-BEGIN; uname -r; echo "root: $(findmnt -no SOURCE,FSTYPE /)"; echo "boot: $(findmnt -no SOURCE,FSTYPE /boot)"; echo "lang: $(cat /etc/locale.conf)"; echo "user: $(id %(user)s)"; echo "hostname: $(cat /etc/hostname)"; pacman -Q linux-zen limine github-cli paru python zram-generator gamemode mangohud hyprland caelestia-shell caelestia-cli quickshell-git sddm; ls ~%(user)s/.config/caelestia 2>/dev/null || ls /home/*/.config/caelestia; echo "sddm enabled: $(systemctl is-enabled sddm)"; echo "default target: $(systemctl get-default)"; ls /etc/skel/.config/hypr; echo "cachyos refs: $(grep -ci cachyos /etc/pacman.conf)"; echo "zram: $(swapon --show=NAME,SIZE --noheadings | head -1)"; echo "max_map_count: $(sysctl -n vm.max_map_count)"; echo "scheduler: $(cat /sys/block/vda/queue/scheduler 2>/dev/null)"; echo "sched_ext in kernel config: $(zcat /proc/config.gz 2>/dev/null | grep -c '^CONFIG_SCHED_CLASS_EXT=y')"; tapkich secureboot status; efibootmgr 2>/dev/null; grep -E "^/|protocol|path:" /boot/EFI/limine/limine.conf; echo TAPKICH-INSTALLED-END; systemctl poweroff'
[Install]
WantedBy=multi-user.target
"""


# ---------------------------------------------------------------- the installer hook

class SystemInstaller:
    def __init__(self, snap: Snapshot, plan: Plan, answers: SystemAnswers, *, mnt: str = MNT, run=_run,
                 theme: dict | None = None):
        self.snap, self.plan, self.a, self.mnt, self.run = snap, plan, answers, mnt, run
        validate_answers(answers)                 # unsafe/impossible answers stop here, before any disk is touched
        self.theme = theme or load_theme()
        self.our_boot_entry: Optional[str] = None
        self.mounted = False
        self.progress: Progress = NULL            # Applier replaces this with the real bar
        self.root_fs = getattr(plan, "root_fs", "ext4")
        self.luks_node = ""
        self.luks_style = "systemd"
        self.gpu = nvidia.choose([])              # graphics driver plan, decided from lspci when the install starts
        self.detect_gpus = nvidia.detect          # (tests replace it)
        self.tapkich_package = False              # our files come as the tapkich-os package (from the ISO's repo)
        self.notes: list = []                     # things the finish screen should mention
        self.windows_direct = False               # the Windows menu item chainloads directly (no firmware entry to use)

    # Applier calls the instance as the bootloader hook, and .undo() after a failure.
    def __call__(self, result: ApplyResult):
        esp = result.created[OUR_ESP_NAME]
        root = result.root_node or result.created[OUR_ROOT_NAME]          # the opened mapper when encrypted
        self.luks_node = result.luks_node
        self.progress.emit(12, "Mounting the new disk")
        self.gpu = nvidia.choose(self.detect_gpus())
        if self.gpu["note"]:
            self.progress.log("graphics: " + self.gpu["note"])
        self._mount(esp, root)
        self._pacstrap()
        self.progress.emit(STAGES["fallback"], "Preparing the recovery boot entry", "fallback initramfs")
        self._enable_fallback()
        self.progress.emit(STAGES["configure"], "Configuring the system", "language, keyboard, user, network")
        self._configure()
        self._refresh_aur()                                # only the AUR packages that are outdated
        self._caelestia()                                  # the desktop: always, not only with the extra apps
        if self.a.extra_apps:
            self._aur_apps()
        root_uuid = self.run(["blkid", "-s", "UUID", "-o", "value", root]).stdout.strip()
        if self.luks_node:
            self.progress.emit(STAGES["encrypt"], "Preparing the encrypted boot", "adding the disk-unlock step to the initramfs")
            self._enable_encrypt_hook()
        self.progress.emit(STAGES["limine"], "Installing the boot menu (Limine)")
        self._limine(root_uuid)
        self.progress.emit(STAGES["entry"], "Registering the boot entry")
        self._uefi_entry(esp, result)
        if self.a.secure_boot:
            self.progress.emit(STAGES["secureboot"], "Setting up Secure Boot", "our keys + Microsoft's, signing Limine and the kernel")
            self._secure_boot()
        self._boot_menu_notes(esp)
        self.progress.emit(STAGES["unmount"], "Unmounting")
        self._keep_install_log(result.backup_dir)
        self._unmount()
        self._cleanup_old_entries()

    def undo(self):
        """Failure path: unmount, and remove only the UEFI entry this run created."""
        self._unmount(check=False)
        if self.our_boot_entry:
            self.run(["efibootmgr", "--bootnum", self.our_boot_entry, "--delete-bootnum"], check=False)
            self.our_boot_entry = None

    def _keep_install_log(self, backup_dir: str = ""):
        """Copy the install log into the new system (root only), so problems can still be read after the reboot. Also
        the backup taken before anything was changed (partition tables, `efibootmgr -v`, a copy of Windows' boot
        files and their hashes): with it, a lost Windows boot entry or ESP can be put back from the installed system."""
        if backup_dir and os.path.isdir(backup_dir):
            import shutil
            dst = f"{self.mnt}/var/log/tapkich-os-install/backup"
            try:
                shutil.copytree(backup_dir, dst, dirs_exist_ok=True)
                os.chmod(os.path.dirname(dst), 0o700)
            except OSError as e:
                self.progress.log(f"could not keep the pre-install backup: {e}")
        src = getattr(self.progress, "log_path", None)
        if not src or not os.path.isfile(src):
            return
        dst = f"{self.mnt}/var/log/tapkich-os-install.log"
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil_copy(src, dst)
            os.chmod(dst, 0o600)
        except OSError as e:
            self.progress.log(f"could not keep the install log: {e}")

    # ---- steps
    def _mount(self, esp: str, root: str):
        m = self.mnt
        os.makedirs(m, exist_ok=True)
        if self.root_fs == "btrfs":
            self.run(["mount", "-t", "btrfs", root, m])
            self.mounted = True                    # from here on undo() must unmount, even if the next step fails
            for sv in ("@", "@home", "@log", "@pkg"):
                self.run(["btrfs", "subvolume", "create", f"{m}/{sv}"])
            self.run(["umount", m])
            self.mounted = False
            opts = "noatime,compress=zstd"
            self.run(["mount", "-t", "btrfs", "-o", f"{opts},subvol=@", root, m])
            self.mounted = True
            for sub, path in (("@home", "home"), ("@log", "var/log"), ("@pkg", "var/cache/pacman/pkg")):
                os.makedirs(f"{m}/{path}", exist_ok=True)
                self.run(["mount", "-t", "btrfs", "-o", f"{opts},subvol={sub}", root, f"{m}/{path}"])
        else:
            self.run(["mount", "-t", "ext4", "-o", "noatime", root, m])
            self.mounted = True
        os.makedirs(f"{m}/boot", exist_ok=True)
        self.run(["mount", "-t", "vfat", "-o", "umask=0077", esp, f"{m}/boot"])        # kernel + Limine live on OUR ESP

    def _pacstrap(self):
        """Install the base system, streaming pacman's output so the bar moves with real progress."""
        with open("/etc/pacman.conf") as f:
            self.official_conf = desktop.hide_broken_sessions(apps.enable_multilib(f.read()))   # official repos + multilib
        have_local = os.path.isfile(f"{LOCAL_REPO}/tapkich-local.db.tar.gz") or os.path.isfile(f"{LOCAL_REPO}/tapkich-local.db")
        self.pacman_conf = "/tmp/tapkich-pacman.conf"             # pacstrap's own copy (adds the local repo with paru)
        with open(self.pacman_conf, "w") as out:
            out.write(with_local_repo(self.official_conf) if have_local else self.official_conf)
        if not have_local:
            self.progress.log("WARNING: no local repo on this ISO, paru will NOT be installed")
        dl, inst = Span(self.progress, *STAGES["download"]), Span(self.progress, *STAGES["install"])
        self.progress.emit(14, "Downloading packages", "contacting the mirrors")
        self.tapkich_package = have_local and bool(glob.glob(f"{LOCAL_REPO}/{package.NAME}-[0-9r]*.pkg.tar.zst"))
        cmd = ["pacstrap", "-K", "-C", self.pacman_conf, self.mnt,
               *packages_for(self.root_fs, with_paru=have_local, gpu=self.gpu["packages"],
                             with_tapkich_package=self.tapkich_package)]
        st: dict = {}
        tail: deque = deque(maxlen=25)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        try:
            for raw in p.stdout:
                line = raw.rstrip("\n")
                tail.append(line)
                self.progress.log("pacstrap: " + line)
                ev = parse_pacman_line(line, st)
                total = st.get("total") or 0
                if ev and ev[0] == "download" and total:
                    dl.at(min(st["downloaded"], total) / total, "Downloading packages",
                          f"{min(st['downloaded'], total)} of {total}: {ev[1]}")
                elif ev and ev[0] == "install" and total:
                    inst.at(min(st["installed"], total) / total, "Installing packages",
                            f"{min(st['installed'], total)} of {total}: {ev[1]}")
                elif line.strip() and not line.startswith(" "):
                    self.progress.update(short_line(line))           # hooks, keyrings, ...: still show what happens
        except BaseException:                                        # interrupted: stop pacstrap before the rollback unmounts
            p.terminate()
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            raise
        rc = p.wait()
        if rc != 0:
            raise ApplyError("COMMAND_FAILED", f"{' '.join(cmd[:2])} -> exit {rc}: " + " | ".join(list(tail)[-8:]))
        for need in (f"vmlinuz-{KERNEL}", f"initramfs-{KERNEL}.img"):
            if not os.path.exists(f"{self.mnt}/boot/{need}"):
                raise ApplyError("SYSTEM_INSTALL", f"{need} missing from the new /boot after pacstrap")

    def _enable_fallback(self):
        """Arch only builds the `default` initramfs; switch on `fallback` so the recovery entry has an image."""
        path = f"{self.mnt}/etc/mkinitcpio.d/{KERNEL}.preset"
        with open(path) as f:
            text = f.read()
        with open(path, "w") as f:
            f.write(enable_fallback_preset(text))
        self._chroot(f"mkinitcpio -p {KERNEL}")
        need = f"initramfs-{KERNEL}-fallback.img"
        if not os.path.exists(f"{self.mnt}/boot/{need}"):
            raise ApplyError("SYSTEM_INSTALL", f"{need} was not created")

    def _chroot(self, script: str, stdin: str | None = None):
        return self.run(["arch-chroot", self.mnt, "/bin/bash", "-c", script], input=stdin)

    def _configure(self):
        a = self.a
        validate_answers(a)                                                      # (also checked in __init__; cheap)
        self.run(["bash", "-c", f"genfstab -U {self.mnt} >> {self.mnt}/etc/fstab"])
        with open(f"{self.mnt}/etc/pacman.conf", "w") as f:                       # official repos only (no local repo)
            f.write(self.official_conf)
        with open(f"{self.mnt}/etc/hostname", "w") as f:
            f.write(a.hostname + "\n")
        with open(f"{self.mnt}/etc/vconsole.conf", "w") as f:
            f.write(f"KEYMAP={a.keyboard}\n" + (f"FONT={a.font}\n" if a.font else ""))
        layouts = resolve_xkb(a)
        os.makedirs(f"{self.mnt}/etc/X11/xorg.conf.d", exist_ok=True)
        with open(f"{self.mnt}/etc/X11/xorg.conf.d/00-keyboard.conf", "w") as f:
            f.write(render_xkb_conf(layouts))
        self._install_tapkich()             # our files: tuning, Gaming Mode, hooks, `tapkich` (the tapkich-os package)
        self._graphics()
        self._install_desktop(layouts)
        os.makedirs(f"{self.mnt}/etc/tapkich-os", exist_ok=True)
        with open(f"{self.mnt}/etc/tapkich-os/keyboards", "w") as f:        # for the desktop config (Hyprland kb_layout)
            f.write(layouts + "\n")
        with open(f"{self.mnt}/etc/issue", "w") as f:                               # login prompt text, not "Arch Linux"
            f.write("TapkicH-OS \\r (\\l)\n\n")
        # The system calls itself TapkicH-OS (boot messages say "Welcome to <PRETTY_NAME>"). /etc/os-release is a
        # symlink owned by Arch's `filesystem` package; the package's pacman hook puts our file back after updates.
        if os.path.lexists(f"{self.mnt}/etc/os-release"):
            os.remove(f"{self.mnt}/etc/os-release")
        shutil_copy(f"{self.mnt}/usr/share/tapkich-os/os-release", f"{self.mnt}/etc/os-release")
        with open(f"{self.mnt}/etc/locale.conf", "w") as f:
            f.write(f"LANG={a.locale}.UTF-8\n")
        # Windows reads the hardware clock as local time, so with Windows on the computer we keep local time there too
        local_clock = self._windows_stays()
        if local_clock:
            self.notes.append("Windows is on this computer, so the hardware clock keeps local time (as Windows "
                              "expects); both systems show the same time.")
        self._chroot(
            "sed -i 's/^#en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen && "
            f"sed -i 's/^#{a.locale}.UTF-8 UTF-8/{a.locale}.UTF-8 UTF-8/' /etc/locale.gen && locale-gen && "
            f"ln -sf /usr/share/zoneinfo/{a.timezone} /etc/localtime && "
            f"hwclock --systohc{' --localtime' if local_clock else ''} && "
            f"useradd -m -G wheel -s {shell.LOGIN_SHELL} {a.username} && "
            # gamemode's limits.d file lets the group raise game priority
            f"{{ getent group gamemode >/dev/null && usermod -aG gamemode {a.username} || true; }} && "
            "echo '%wheel ALL=(ALL:ALL) ALL' > /etc/sudoers.d/10-wheel && chmod 440 /etc/sudoers.d/10-wheel && "
            f"dconf update && systemctl enable NetworkManager systemd-timesyncd sddm bluetooth power-profiles-daemon {gaming.RESET_UNIT} && systemctl set-default graphical.target")
        if a.wifi_ssid:                                                          # keep the install-time Wi-Fi
            import uuid
            nm = f"{self.mnt}/etc/NetworkManager/system-connections"
            os.makedirs(nm, exist_ok=True)
            path = f"{nm}/tapkich-wifi.nmconnection"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(render_wifi_profile(a.wifi_ssid, a.wifi_password, str(uuid.uuid4()), a.wifi_hidden))
            os.chmod(path, 0o600)
        # Flathub for Bazaar, ready from the first start (flatpak's own copy of Flathub's remote file; no network needed)
        self._chroot(f"flatpak remote-add --system --if-not-exists flathub {FLATHUB_REPO_FILE} "
                     "|| echo 'WARNING: could not add Flathub'")
        # password goes in on stdin, never on a command line or in a log
        self._chroot("chpasswd", stdin=f"{a.username}:{a.password}\n")
        self._chroot("passwd -l root")
        # (no git identity / GitHub questions here: `gh auth login` is a first-boot step)
        if a.selftest:
            os.makedirs(f"{self.mnt}/etc/systemd/system/multi-user.target.wants", exist_ok=True)
            with open(f"{self.mnt}/etc/systemd/system/tapkich-selftest.service", "w") as f:
                f.write(SELFTEST_UNIT % {"user": a.username})
            os.symlink("../tapkich-selftest.service", f"{self.mnt}/etc/systemd/system/multi-user.target.wants/tapkich-selftest.service")

    def _enable_encrypt_hook(self):
        """Add the disk-unlock hook before `filesystems` and rebuild the initramfs.
        The default Arch initramfs (also CachyOS') is systemd based (hooks: base systemd ... sd-vconsole ...), which needs
        `sd-encrypt`; the older busybox style needs `encrypt`. We read the real config instead of assuming.
        The keyboard/console-keymap hooks come earlier in the list, so the passphrase is typed on the chosen layout."""
        with open(f"{self.mnt}/etc/mkinitcpio.conf") as f:
            conf = f.read()
        self.luks_style, hook = luks_hook_for(conf)
        # `filesystems` may be followed by another hook or by the closing parenthesis
        self._chroot(
            f"sed -i -E '/^HOOKS=/ s/ filesystems([ )])/ {hook} filesystems\\1/' /etc/mkinitcpio.conf && "
            f"grep -q '^HOOKS=.* {hook} filesystems' /etc/mkinitcpio.conf && mkinitcpio -P")

    def _aur_apps(self):
        """Install the AUR apps with paru as the new user, one by one. A failure of one app never fails the install:
        it is logged and the rest go on (Waterfox falls back to Firefox). Proprietary apps (Claude, ChatGPT) are
        downloaded from their vendors onto THIS machine; they are never bundled in our ISO."""
        user, sudo_file = self.a.username, f"{self.mnt}{apps.SUDO_INSTALL_FILE}"
        with open(sudo_file, "w") as f:
            f.write(apps.SUDO_INSTALL_RULE)
        os.chmod(sudo_file, 0o440)
        failed = []
        lo, hi = STAGES["aur"]
        n = len(apps.AUR_APPS)
        try:
            for i, (pkg, label, fallback) in enumerate(apps.AUR_APPS):
                name = label.split(" (")[0]
                step = Span(self.progress, lo + (hi - lo) * i / n, lo + (hi - lo) * (i + 1) / n)
                msg = f"Installing extra apps ({i + 1} of {n}): {name}"
                step.at(0, msg, "starting")
                self.progress.log(f"aur: installing {pkg}")
                def show(line, step=step, msg=msg):
                    frac = aur_fraction(line)
                    if frac:
                        step.at(frac, msg, short_line(line))            # a new phase: the bar moves
                    else:
                        self.progress.update(short_line(line))          # any other output: the sub-text moves
                rc, tail = self._stream(["arch-chroot", self.mnt, "su", "-", user, "-c", apps.paru_command(pkg)],
                                        show, "aur: ", timeout=apps.AUR_TIMEOUT)
                if rc != 0:
                    failed.append(pkg)
                    self.progress.log(f"WARNING: {label} could not be installed ({pkg}); continuing. "
                                      f"Last output: {' | '.join(tail[-5:])!r}")
                    for fb in fallback:
                        step.at(0.9, msg, f"falling back to {fb}")
                        self.run(["arch-chroot", self.mnt, "pacman", "-S", "--noconfirm", "--needed", fb], check=False)
                step.at(1, msg, "failed, skipped" if rc != 0 else "done")
        finally:
            os.remove(sudo_file)                               # the passwordless sudo rule never stays on the system
        if failed:
            self.progress.log("AUR apps not installed: " + ", ".join(failed))

    def _refresh_aur(self):
        """The AUR packages on the ISO were built earlier, the official ones (Qt) just now. Rebuild only what needs it
        (apps.packages_to_refresh): packages with a newer AUR version, and the Qt-linked shell packages when the
        installed Qt differs from the one they were built against. Best effort: offline or an AUR error keeps the
        prebuilt packages. Nothing outdated means nothing compiles. Never fails the install."""
        try:
            qm = self.run(["arch-chroot", self.mnt, "pacman", "-Qm"], check=False)
            installed = apps.parse_pacman_q(qm.stdout or "")
            qs = self.run(["arch-chroot", self.mnt, "quickshell", "--version", "--verbose"], check=False)
            qt_changed = apps.qt_mismatch(qs.stdout or "")
            aur = apps.fetch_aur_versions(sorted(installed), self._urlopen)
            if aur is None:
                self.progress.log("aur: could not ask the AUR for newer versions; keeping the prebuilt packages")
            todo = apps.packages_to_refresh(installed, aur, qt_changed, self._vercmp)
        except (OSError, ApplyError, subprocess.CalledProcessError) as e:
            self.progress.log(f"aur: could not check the prebuilt packages ({e}); keeping them")
            return
        if not todo:
            self.progress.log("aur: the prebuilt packages are current")
            return
        user, sudo_file = self.a.username, f"{self.mnt}{apps.SUDO_INSTALL_FILE}"
        with open(sudo_file, "w") as f:
            f.write(apps.SUDO_INSTALL_RULE)
        os.chmod(sudo_file, 0o440)
        lo, hi = STAGES["refresh"]
        n = len(todo)
        try:
            for i, (pkg, rebuild, reason) in enumerate(todo):
                step = Span(self.progress, lo + (hi - lo) * i / n, lo + (hi - lo) * (i + 1) / n)
                msg = f"Updating {pkg} ({i + 1} of {n})"
                step.at(0, msg, reason)
                self.progress.log(f"aur: {pkg}: {reason}")

                def show(line, step=step, msg=msg):
                    frac = aur_fraction(line)
                    if frac:
                        step.at(frac, msg, short_line(line))
                    else:
                        self.progress.update(short_line(line))
                rc, tail = self._stream(["arch-chroot", self.mnt, "su", "-", user, "-c", apps.paru_command(pkg, rebuild)],
                                        show, "aur: ", timeout=apps.REBUILD_TIMEOUT)
                if rc != 0:
                    self.progress.log(f"WARNING: {pkg} could not be updated; the prebuilt one stays. "
                                      f"Last output: {' | '.join(list(tail)[-5:])!r}")
                step.at(1, msg, "failed, kept the prebuilt one" if rc != 0 else "done")
        finally:
            os.remove(sudo_file)

    @staticmethod
    def _urlopen(req, timeout=20):
        import urllib.request
        return urllib.request.urlopen(req, timeout=timeout)

    @staticmethod
    def _vercmp(a: str, b: str) -> int:
        return int(subprocess.run(["vercmp", a, b], capture_output=True, text=True, check=True).stdout.strip())

    def _caelestia(self):
        """Caelestia's own installer, as the new user: fetches its dotfiles, installs the rest of its package list
        (the AUR ones are already installed from the ISO's local repo) and puts its configs in place. Its hooks that
        need a running desktop only warn here. Waterfox (the browser) is installed first."""
        user, sudo_file = self.a.username, f"{self.mnt}{apps.SUDO_INSTALL_FILE}"
        with open(sudo_file, "w") as f:                    # paru needs sudo; the rule is removed right after
            f.write(apps.SUDO_INSTALL_RULE)
        os.chmod(sudo_file, 0o440)
        msg = "Setting up the desktop (Caelestia shell)"
        span, t0 = Span(self.progress, *STAGES["desktop"]), time.monotonic()
        span.at(0, msg, "fetching the dotfiles")
        done = [0.0]

        def show(line):
            # Caelestia's installer prints no overall progress: the bar follows the time spent (about 4 minutes in a
            # VM), never reaching the end before it is really done, and only ever moving forward
            frac = min(0.95, 1 - math.exp(-(time.monotonic() - t0) / 120))
            if frac - done[0] >= 0.01:
                done[0] = frac
                span.at(frac, msg, short_line(line))
            else:
                self.progress.update(short_line(line))
        try:
            self.progress.log("caelestia: " + caelestia.INSTALL_COMMAND)
            rc, tail = self._stream(["arch-chroot", self.mnt, "su", "-", user, "-c", caelestia.INSTALL_COMMAND],
                                    show, "caelestia: ", timeout=apps.AUR_TIMEOUT)
        finally:
            os.remove(sudo_file)
        span.at(1, msg, "done" if rc == 0 else "finished with warnings")
        if rc != 0:
            self.progress.log(f"WARNING: Caelestia's installer failed (exit {rc}). "
                              f"Last output: {' | '.join(list(tail)[-5:])!r}")

    def _stream(self, cmd: list, on_line, log_prefix: str = "", timeout: float | None = None) -> tuple:
        """Run a command, calling on_line(line) for every output line (ANSI colours removed). A carriage return ends a
        line too, so download progress (curl, pacman) shows live instead of the screen looking frozen. With a timeout
        the command and everything it started are stopped after that many seconds (exit code 124).
        Returns (exit code, last lines)."""
        import signal
        import threading
        tail: deque = deque(maxlen=25)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = threading.Event()

        def stop():
            timed_out.set()
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
        timer = threading.Timer(timeout, stop) if timeout else None
        if timer:
            timer.daemon = True
            timer.start()
        buf = b""
        try:
            while True:
                chunk = p.stdout.read1(4096)
                if not chunk:
                    break
                parts = re.split(rb"[\r\n]", buf + chunk)
                buf = parts.pop()
                for raw in parts:
                    self._stream_line(raw, tail, on_line, log_prefix)
            if buf:
                self._stream_line(buf, tail, on_line, log_prefix)
            rc = p.wait()
        finally:
            if timer:
                timer.cancel()
        if timed_out.is_set():
            tail.append(f"stopped after the time limit ({int(timeout)} s)")
            self.progress.log(f"{log_prefix}stopped after the time limit ({int(timeout)} s)")
            return 124, list(tail)
        return rc, list(tail)

    def _stream_line(self, raw: bytes, tail: deque, on_line, log_prefix: str):
        line = ANSI.sub("", raw.decode("utf-8", "replace"))
        if not line.strip():
            return
        tail.append(line)
        self.progress.log(log_prefix + line)
        on_line(line)

    def _wallpapers(self):
        """Caelestia's wallpaper folder (~/Pictures/Wallpapers, via /etc/skel): its own stock wallpaper (from the
        caelestia-shell package, which stays the one in use) plus two design-canvas pictures, night city and alpine dusk (user's choice)."""
        dst = f"{self.mnt}/etc/skel/Pictures/Wallpapers"
        os.makedirs(dst, exist_ok=True)
        stock = f"{self.mnt}{caelestia.STOCK_WALLPAPER}"
        if os.path.isfile(stock):
            shutil_copy(stock, f"{dst}/caelestia.webp")
        for name in sorted(os.listdir(WALLPAPER_DATA)):
            if name.endswith(".jpg"):
                shutil_copy(os.path.join(WALLPAPER_DATA, name), f"{dst}/{name}")

    def _install_desktop(self, layouts: str):
        """Desktop: the Caelestia override files and default apps into /etc/skel (copied into the new user's home by
        useradd -m), and the login manager setup. Caelestia itself comes later (_caelestia)."""
        for rel, text in desktop.skel_files(layouts, self.a.keybinds, nvidia.hypr_env(self.gpu)).items():
            path = f"{self.mnt}/etc/skel/{rel}"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        self._wallpapers()
        for rel in desktop.BROKEN_SESSIONS:                                       # in case it was extracted anyway
            if os.path.exists(f"{self.mnt}/{rel}"):
                os.remove(f"{self.mnt}/{rel}")
        os.makedirs(f"{self.mnt}/var/lib/sddm", exist_ok=True)
        with open(f"{self.mnt}/var/lib/sddm/state.conf", "w") as f:              # first login: this user, plain Hyprland
            f.write(desktop.render_sddm_state(self.a.username))
        self._chroot("chown -R sddm:sddm /var/lib/sddm 2>/dev/null || true")

    def _install_tapkich(self):
        """Our own files (package.py): from the ISO's tapkich-os package when pacstrap installed it, otherwise (a
        development ISO without it) written straight into the new system. Our package-signing key goes into pacman's
        keyring either way, for updates from the [tapkich] repository."""
        if not self.tapkich_package:
            package.write_tree(self.mnt)
            self.progress.log("tapkich-os package not on this ISO: wrote its files directly")
        if os.path.isfile(f"{self.mnt}/usr/share/pacman/keyrings/tapkich.gpg"):
            self._chroot("pacman-key --populate tapkich >/dev/null || echo 'WARNING: could not add the TapkicH-OS key'")

    def _graphics(self):
        """NVIDIA (nvidia.py): driver options, early modesetting, suspend services, the hybrid-GPU service. The
        packages came with pacstrap. Nothing to do for AMD/Intel (Mesa) or a VM."""
        g = self.gpu
        if not g["files"]:
            if g["note"]:
                self.notes.append(g["note"])
            return
        for path, text in g["files"].items():
            os.makedirs(os.path.dirname(f"{self.mnt}{path}"), exist_ok=True)
            with open(f"{self.mnt}{path}", "w") as f:
                f.write(text)
        self._chroot(f"systemctl enable {' '.join(g['units'])} && mkinitcpio -P")
        self.notes.append(g["note"])

    def _secure_boot(self):
        """`tapkich secureboot setup` inside the new system (sbctl keys + Microsoft's, Limine config hash, signatures).
        Only offered when the firmware is in Setup Mode. A failure never fails the install: the system still starts
        with Secure Boot off, and the finish screen says so."""
        p = self.run(["arch-chroot", self.mnt, "env", "PYTHONPATH=/opt/tapkich-os", "python3", "-m",
                      "tapkich_os.command", "secureboot", "setup", "--yes", "--from-installer"], check=False)
        self.progress.log("secureboot: " + (p.stdout or "") + (p.stderr or ""))
        if p.returncode == 0:
            self.notes.append("Secure Boot: our keys and Microsoft's are enrolled and the boot files are signed. "
                              "If it is still off after the restart, switch it on in the firmware settings.")
        else:
            self.notes.append("Secure Boot could not be set up (see /var/log/tapkich-os-install.log). The system "
                              "starts with Secure Boot off; try `sudo tapkich secureboot setup` later.")

    def _windows_stays(self) -> bool:
        """True when a Windows boot partition on this computer survives the install."""
        deleted = {o.target for o in self.plan.ops if o.kind == "DELETE"}
        return any(e.ref not in deleted for e in windows_boot_esps(self.snap))

    def _remove_stale_entries(self):
        """Delete the old "TapkicH-OS" boot entries that point at partitions this install deleted. Nothing else."""
        deleted = {o.target for o in self.plan.ops if o.kind == "DELETE"}
        gone = {p.partuuid.lower() for p in self.snap.partitions() if p.ref in deleted and p.partuuid}
        if not gone:
            return
        for e in self.snap.nvram:
            if e.label != "TapkicH-OS" or not e.partuuid or e.partuuid.lower() not in gone or e.bootnum == self.our_boot_entry:
                continue
            p = self.run(["efibootmgr", "--bootnum", e.bootnum, "--delete-bootnum"], check=False)
            self.progress.log(f"uefi entry: removed the old TapkicH-OS entry Boot{e.bootnum} (its partition was "
                              f"deleted): exit {p.returncode}")

    def _boot_menu_notes(self, esp_node: str = ""):
        """Finish-screen notes about starting TapkicH-OS and Windows again after the restart."""
        if not self.a.secure_boot:
            try:
                disk, part = parse_disk_and_part(esp_node)
                fix = (f"start this USB stick again and run: efibootmgr --create --disk {disk} --part {part} "
                       "--label TapkicH-OS --loader '\\EFI\\limine\\limine_x64.efi'")
            except ApplyError:
                fix = "start this USB stick again and add it back with efibootmgr"
            self.notes.append("Secure Boot has to stay off for TapkicH-OS to start, until you run `sudo tapkich "
                              "secureboot setup`. Changing the Secure Boot setting or loading the firmware defaults can "
                              "remove the TapkicH-OS boot entry on some laptops (seen on a Lenovo ThinkPad). If it is "
                              f"gone from the boot menu, {fix}")
        if self.windows_direct:
            self.notes.append("If Windows uses BitLocker, start it from the firmware's boot menu instead of the "
                              "TapkicH-OS menu: starting it from here asks for the BitLocker recovery key.")

    def _limine(self, root_uuid: str):
        boot = f"{self.mnt}/boot"
        t = self.theme
        windows = []
        deleted = {o.target for o in self.plan.ops if o.kind == "DELETE"}
        for i, esp in enumerate(e for e in windows_boot_esps(self.snap) if e.ref not in deleted):
            windows.append(("Windows" if i == 0 else f"Windows ({esp.disk})", esp.partuuid))
        boot_entry = windows_boot_entry(self.snap, [guid for _, guid in windows])
        self.windows_direct = bool(windows) and not boot_entry
        if self.windows_direct:
            self.progress.log("limine: no single \"Windows Boot Manager\" entry in the firmware's boot list for the "
                              "Windows boot partition, so the Windows menu item chainloads it directly")
        luks_uuid = ""
        if self.luks_node:
            luks_uuid = self.run(["blkid", "-s", "UUID", "-o", "value", self.luks_node]).stdout.strip()
            if not luks_uuid:
                raise ApplyError("SYSTEM_INSTALL", "cannot read the UUID of the encrypted partition")
        serial = ("console=tty0 console=ttyS0,115200 loglevel=6 rd.systemd.show_status=true systemd.show_status=true"
                  if self.a.selftest else "")
        extra = " ".join(x for x in (self.gpu["cmdline"], serial) if x)
        conf = render_limine_conf(root_uuid, windows, t, serial=self.a.selftest, root_fs=self.root_fs, luks_uuid=luks_uuid,
                                  luks_style=self.luks_style, extra_cmdline=extra, windows_boot_entry=boot_entry)
        for d in ("EFI/limine", "EFI/BOOT"):                                  # both: Limine reads the config beside itself
            os.makedirs(f"{boot}/{d}", exist_ok=True)
            with open(f"{boot}/{d}/limine.conf", "w") as f:
                f.write(conf)
        for name in ("wallpaper.jpg", "font.bin"):                           # made by tools/make-boot-assets.py
            shutil_copy(os.path.join(LIMINE_DATA, name), f"{boot}/EFI/limine/{name}")
        shutil_copy("/usr/share/limine/BOOTX64.EFI", f"{boot}/EFI/limine/limine_x64.efi")
        shutil_copy("/usr/share/limine/BOOTX64.EFI", f"{boot}/EFI/BOOT/BOOTX64.EFI")

    def _uefi_entry(self, esp_node: str, result: ApplyResult):
        if not os.path.isdir("/sys/firmware/efi/efivars"):
            result.skipped.append("UEFI entry (no efivars; fallback BOOTX64.EFI on our ESP is used)")
            self.progress.log("uefi entry: skipped, no efivars (the fallback BOOTX64.EFI on our ESP is used)")
            return
        before = self.run(["efibootmgr"], check=False).stdout
        self.progress.log("uefi entry: boot list before:\n" + (before or ""))
        disk, part = parse_disk_and_part(esp_node)
        p = self.run(["efibootmgr", "--create", "--disk", disk, "--part", str(part), "--label", "TapkicH-OS",
                      "--loader", "\\EFI\\limine\\limine_x64.efi"])
        self.progress.log("uefi entry: efibootmgr --create: " + (p.stdout or "") + (p.stderr or ""))
        after = self.run(["efibootmgr"], check=False).stdout
        self.progress.log("uefi entry: boot list after:\n" + (self.run(["efibootmgr", "-v"], check=False).stdout or ""))
        new = set(re.findall(r"^Boot([0-9A-F]{4})", after, re.M)) - set(re.findall(r"^Boot([0-9A-F]{4})", before, re.M))
        if len(new) != 1:
            raise ApplyError("UEFI_ENTRY", f"expected exactly one new UEFI entry, saw {sorted(new)}")
        self.our_boot_entry = new.pop()
        windows_before = [l for l in before.splitlines() if "Windows Boot Manager" in l]
        if windows_before and not [l for l in after.splitlines() if "Windows Boot Manager" in l]:
            raise ApplyError("UEFI_ENTRY", "Windows boot entry disappeared while adding ours; aborting")

    def _cleanup_old_entries(self):
        """The last step: a rollback cannot bring removed entries back, so nothing that can still fail comes after it."""
        try:
            self._remove_stale_entries()
        except Exception as e:                                     # a leftover entry is harmless: never fail the install
            self.progress.log(f"uefi entry: could not remove old TapkicH-OS entries: {e}")

    def _unmount(self, check: bool = True):
        if self.mounted:
            self.run(["umount", "-R", self.mnt], check=check)
            self.mounted = False


def shutil_copy(src: str, dst: str):
    import shutil
    shutil.copyfile(src, dst)
