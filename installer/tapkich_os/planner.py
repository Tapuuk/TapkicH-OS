"""Pure installer planner: Snapshot + Choices -> Plan, or a stop.

No I/O happens here. All Windows-boot safety rules live in this file so they
can be unit- and property-tested without touching a disk.
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field

from .model import (
    GIB, MIB, OUR_ESP_NAME, OUR_ROOT_NAME, ROLE_ESP, ROLE_MSR, ROLE_WINRE,
    Disk, Partition, Snapshot, _align_up,
)

ESP_SIZE = 1 * GIB
MIN_ROOT = 32 * GIB

# Typed phrases that unlock destructive hard stops.
OVERRIDE_WINDOWS_BOOT = "ERASE WINDOWS BOOT FILES"
OVERRIDE_WINDOWS = "ERASE WINDOWS"
OVERRIDE_FOREIGN_BOOT = "ERASE OTHER BOOTLOADERS"
OVERRIDE_AMBIGUOUS = "I UNDERSTAND"
RST_WARNING = ("Intel RST / RAID (or VMD) storage mode is on. Linux may not see every disk in this mode. If a disk is "
               "missing, do NOT just switch the firmware to AHCI: Windows would stop starting. In Windows first run "
               "`bcdedit /set {current} safeboot minimal`, restart, switch the storage mode to AHCI in the firmware, let "
               "Windows start once (Safe Mode), run `bcdedit /deletevalue {current} safeboot`, restart, then install.")

MODE_FREE_SPACE = "free_space"
MODE_WHOLE_DISK = "whole_disk"
MODE_REINSTALL = "reinstall"


class PlanError(Exception):
    def __init__(self, code: str, message: str, alternatives=None, override: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.alternatives = alternatives or []
        self.override = override

    def __str__(self):
        text = f"[{self.code}] {self.message}"
        if self.alternatives:
            text += "\n  Safe alternatives:\n  - " + "\n  - ".join(self.alternatives)
        if self.override:
            text += f"\n  To override, type exactly: {self.override}"
        return text


class HardStop(PlanError):
    """The installer refuses. Only the named override phrase (if any) unlocks it."""


class NeedsConfirmation(PlanError):
    """Layout is ambiguous. Stop and ask rather than guess."""


@dataclass
class Choices:
    target_disk: str
    mode: str = MODE_FREE_SPACE
    root_size: int | None = None          # bytes; default = all of the chosen free region
    root_fs: str = "ext4"                 # ext4 (default, simple) or btrfs (snapshots, compression)
    encrypt: bool = False                 # LUKS2 on the system partition (off by default)
    overrides: frozenset = frozenset()


@dataclass
class Op:
    kind: str       # NEW_TABLE DELETE CREATE FORMAT WRITE NVRAM LIMINE_WINDOWS_ENTRY UNTOUCHED
    target: str
    detail: str = ""
    start: int = 0
    size: int = 0


@dataclass
class Plan:
    target_disk: str
    mode: str
    ops: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    boot_map: list = field(default_factory=list)
    root_fs: str = "ext4"
    encrypt: bool = False
    root_gb_min: int = MIN_ROOT // GIB
    root_gb_max: int = 0                                  # most space TapkicH-OS could get in the chosen region
    created: list = field(default_factory=list)          # [(name, GB)] for the review screen
    deleted: list = field(default_factory=list)          # partition refs removed (reinstall / whole disk)
    windows_boot_kept: list = field(default_factory=list)  # Windows EFI partitions left untouched

    def describe(self) -> str:
        lines = [f"Install plan: {self.mode} on {self.target_disk}"]
        if self.boot_map:
            lines.append("Windows boot map:")
            lines += [f"  {m}" for m in self.boot_map]
        for w in self.warnings:
            lines.append(f"WARNING: {w}")
        for op in self.ops:
            extra = f" {op.detail}" if op.detail else ""
            lines.append(f"  {op.kind:<21} {op.target}{extra}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Boot dependency map
# --------------------------------------------------------------------------

def maybe_windows(p: Partition) -> bool:
    """A Windows install, or a volume that might be one: BitLocker is opaque, and an NTFS volume we could not even
    open (damaged, needs chkdsk) is treated the same: when in doubt it is Windows."""
    return p.has_windows or p.fs == "bitlocker" or p.unreadable


def holds_windows_boot(p: Partition) -> bool:
    """An EFI partition with Windows boot data, even if bootmgfw.efi itself is missing or damaged (the rest of
    EFI/Microsoft, e.g. the BCD store, is still Windows' and must be protected)."""
    return p.has_bootmgfw or (p.fs == "vfat" and any(d.lower() == "microsoft" for d in p.efi_dirs))


def windows_installs(snap: Snapshot):
    """Partitions that are, or may be, a Windows install."""
    return [p for p in snap.partitions() if maybe_windows(p)]


def windows_boot_esps(snap: Snapshot):
    """ESPs we can chainload Windows from (bootmgfw.efi present). Protection uses holds_windows_boot instead."""
    return [p for p in snap.partitions() if p.has_bootmgfw]


def foreign_boot_esps(snap: Snapshot):
    """ESPs holding bootloaders that are neither Windows nor ours nor the UEFI fallback."""
    out = []
    for p in snap.partitions():
        if p.fs == "vfat" and not p.is_ours:
            foreign = [d for d in p.efi_dirs
                       if d.lower() not in ("microsoft", "boot", "limine")]
            if foreign:
                out.append(p)
    return out


def _is_windows_entry(label: str) -> bool:
    return "windows boot manager" in label.lower()


def boot_map(snap: Snapshot):
    """For every Windows install, which ESP boots it? Returns (lines, mapping, warnings).

    A disk's role is decided by what boots from it, not where Windows is installed.
    """
    esps = windows_boot_esps(snap)
    nv_esps = []
    for e in snap.nvram:
        if _is_windows_entry(e.label):
            p = snap.find_partuuid(e.partuuid)
            if p is not None:
                nv_esps.append(p)
    lines, mapping, warns = [], {}, []
    for w in windows_installs(snap):
        same_disk = [e for e in esps if e.disk == w.disk]
        if same_disk:
            chosen = same_disk[0]
        elif len(nv_esps) == 1 and nv_esps[0] in esps:
            chosen = nv_esps[0]
        elif len(esps) == 1:
            chosen = esps[0]
        else:
            chosen = None
        mapping[w.ref] = chosen
        what = ("Windows (BitLocker, unverified)" if w.fs == "bitlocker"
                else "Windows (volume could not be read, unverified)" if w.unreadable and not w.has_windows else "Windows")
        if chosen is None:
            lines.append(f"{what} on {w.disk} has no identifiable boot partition")
            warns.append(f"cannot determine which EFI partition boots {w.ref}")
        elif chosen.disk == w.disk:
            lines.append(f"{what} on {w.disk} starts from the EFI partition on the same disk ({chosen.ref})")
        else:
            lines.append(f"{what} on {w.disk} starts from the EFI partition on a DIFFERENT disk: {chosen.ref}")
    for e in esps:
        if not any(m is e for m in mapping.values()):
            lines.append(f"Windows Boot Manager found on {e.ref} (no matching Windows install seen)")
    return lines, mapping, warns


def disk_roles(snap: Snapshot, disk_id: str):
    d = snap.disk(disk_id)
    roles = set()
    for p in d.partitions:
        if holds_windows_boot(p):
            roles.add("HOLDS_WINDOWS_BOOT")
        if maybe_windows(p) or p.role in (ROLE_MSR, ROLE_WINRE):
            roles.add("HOLDS_WINDOWS")
        if p.fs == "vfat" and p in foreign_boot_esps(snap):
            roles.add("HOLDS_FOREIGN_BOOT")
    if d.raw_fs in ("bitlocker", "ntfs"):          # written straight on the disk, no partition table
        roles.add("HOLDS_WINDOWS")
    return roles


# --------------------------------------------------------------------------
# Planner
# --------------------------------------------------------------------------

def _dependents(snap: Snapshot, esp: Partition):
    _, mapping, _ = boot_map(snap)
    return sorted({ref.split("#")[0] for ref, e in mapping.items() if e is esp})


def plan(snap: Snapshot, choices: Choices) -> Plan:
    fw = snap.firmware
    if not fw.uefi:
        raise HardStop("NO_UEFI", "Booted in legacy BIOS mode. Windows boot preservation needs a UEFI install; boot the installer in UEFI mode.")

    try:
        target = snap.disk(choices.target_disk)
    except KeyError:
        raise HardStop("UNKNOWN_DISK", f"Disk {choices.target_disk} does not exist.")
    if target.installer_media:
        raise HardStop("INSTALLER_MEDIA", f"{target.id} is the installer's own USB stick.")
    is_md = re.match(r"^md\d", os.path.basename(target.id))
    if is_md or target.raid_member:
        what = (f"{target.id} is a RAID volume (Intel RST or software RAID)" if is_md else
                f"{target.id} is a member of a RAID set ({target.raid_member}); writing to it could break the array "
                "that may hold Windows")
        raise HardStop(
            "RAID_VOLUME",
            f"{what}, and TapkicH-OS cannot install onto it. If "
            "this computer's disks only show up as a RAID volume, do NOT just switch the firmware to AHCI: Windows "
            "would stop starting. In Windows first run `bcdedit /set {current} safeboot minimal`, restart, switch the "
            "storage mode to AHCI in the firmware, let Windows start once (Safe Mode), run `bcdedit /deletevalue "
            "{current} safeboot`, restart, then install.",
        )
    if target.table == "hybrid":
        raise HardStop(
            "HYBRID_MBR",
            f"{target.id} has a hybrid MBR (GPT plus real MBR entries). Windows setup and many firmwares misbehave with it. Fix it in Windows/gdisk first.",
        )

    ov = choices.overrides
    warnings: list[str] = []
    map_lines, _, map_warns = boot_map(snap)
    warnings += map_warns
    roles = disk_roles(snap, target.id)

    # ---- global warnings
    if fw.secure_boot:
        warnings.append("Secure Boot is on. TapkicH-OS can only set it up while the firmware is in Setup Mode; otherwise "
                        "turn Secure Boot off for now and run `sudo tapkich secureboot setup` later.")
    if fw.rst_raid:
        warnings.append(RST_WARNING)
    if target.removable:
        warnings.append(f"{target.id} is a removable disk.")
    if any(p.ntfs_dirty for p in windows_installs(snap)):
        warnings.append("Windows was not fully shut down (Fast Startup or hibernation). TapkicH-OS never writes to the "
                        "Windows partition, so this is safe, but Windows keeps part of its state on the disk: turn Fast "
                        "Startup off in Windows (Control Panel > Power Options > Choose what the power buttons do) and "
                        "shut down fully before changing partitions.")
    for e in snap.nvram:
        if _is_windows_entry(e.label) and e.partuuid and snap.find_partuuid(e.partuuid) is None:
            warnings.append(f"UEFI entry {e.bootnum} ({e.label}) points at a partition that no longer exists (stale).")
    if len(windows_installs(snap)) > 1:
        warnings.append("More than one Windows install detected; a Limine entry is added for each boot partition.")

    # ---- mode specific checks
    deleted: list[Partition] = []
    if choices.mode == MODE_WHOLE_DISK:
        _check_whole_disk(snap, target, roles, ov)
        deleted = list(target.partitions)
    elif choices.mode in (MODE_FREE_SPACE, MODE_REINSTALL):
        if target.table != "gpt":
            raise HardStop("NEED_GPT", f"{target.id} is not GPT ({target.table}); free-space installs require GPT. Use whole-disk on an empty disk instead.")
        if choices.mode == MODE_REINSTALL:
            deleted = [p for p in target.partitions if p.is_ours]
            if not deleted:
                raise HardStop("NOTHING_TO_REINSTALL", f"No previous TapkicH-OS partitions found on {target.id}.")
    else:
        raise HardStop("BAD_MODE", f"Unknown mode {choices.mode!r}")

    if choices.root_fs not in ("ext4", "btrfs"):
        raise HardStop("BAD_FILESYSTEM", f"Unknown filesystem {choices.root_fs!r}; choose ext4 or btrfs.")
    if choices.root_size is not None and choices.root_size < MIN_ROOT:
        raise HardStop("SIZE_TOO_SMALL", f"TapkicH-OS needs at least {MIN_ROOT // GIB} GB, you asked for {choices.root_size // GIB} GB.")

    # ---- where do new partitions go?
    work = copy.deepcopy(target)
    gone = {p.number for p in deleted}
    work.partitions = [p for p in work.partitions if p.number not in gone]
    if choices.mode == MODE_WHOLE_DISK:
        # A fresh GPT keeps its backup table at the end of the disk, so stop 1 MiB short.
        first = _align_up(max(work.first_usable, 1 * MIB))
        last = (work.size - 1 * MIB) // MIB * MIB
        regions = [(first, last - first)]
    else:
        regions = work.free_regions()
    need = ESP_SIZE + (choices.root_size or MIN_ROOT)
    fitting = [r for r in regions if r[1] >= need]
    if not fitting:
        biggest = max((r[1] for r in regions), default=0)
        min_need = ESP_SIZE + MIN_ROOT
        has_windows = any(p.has_windows or p.fs == "bitlocker" for p in target.partitions)
        if target.size - 2 * MIB < min_need:
            # The whole disk is smaller than the minimum: say that, and nothing about partitions or Windows.
            raise HardStop(
                "DISK_TOO_SMALL",
                f"{target.id} is too small: TapkicH-OS needs at least {min_need // GIB} GB and this disk is "
                f"{target.size // GIB} GB. Choose a bigger disk (in a virtual machine, make the virtual disk "
                f"at least {min_need // GIB + 7} GB).",
            )
        if choices.root_size is not None and biggest >= min_need:
            raise HardStop(
                "NOT_ENOUGH_SPACE",
                f"You asked for {choices.root_size // GIB} GB, but at most {(biggest - ESP_SIZE) // GIB} GB is "
                f"available on {target.id}. Choose a smaller amount.",
            )
        how = ("Shrink a partition from inside its own OS and run the installer again"
               + (" (turn BitLocker off first for Windows)." if has_windows else "."))
        raise HardStop(
            "NOT_ENOUGH_SPACE",
            f"Need {need // GIB} GB of unallocated space on {target.id}; the largest free region is "
            f"{biggest // GIB} GB. {how}",
        )
    start, size = max(fitting, key=lambda r: r[1])
    esp_start = start
    root_start = esp_start + ESP_SIZE
    root_size = choices.root_size if choices.root_size else (start + size - root_start)
    root_size = (root_size // MIB) * MIB

    # ---- ops
    ops: list[Op] = []
    if choices.mode == MODE_WHOLE_DISK:
        ops.append(Op("NEW_TABLE", target.id, "gpt"))
    for p in deleted:
        ops.append(Op("DELETE", p.ref, f"{p.name or p.fs or p.role}"))
    ops.append(Op("CREATE", f"new:{OUR_ESP_NAME}", f"on {target.id} (own EFI partition, never the Windows one)", esp_start, ESP_SIZE))
    ops.append(Op("CREATE", f"new:{OUR_ROOT_NAME}", f"on {target.id}", root_start, root_size))
    ops.append(Op("FORMAT", f"new:{OUR_ESP_NAME}", "vfat"))
    if choices.encrypt:
        ops.append(Op("ENCRYPT", f"new:{OUR_ROOT_NAME}", "luks2 (the file system goes inside)"))
    ops.append(Op("FORMAT", f"new:{OUR_ROOT_NAME}", choices.root_fs))
    ops.append(Op("WRITE", f"new:{OUR_ESP_NAME}", "Limine bootloader + limine.conf"))
    ops.append(Op("NVRAM", "efibootmgr", "add TapkicH-OS entry; existing entries kept"))
    deleted_refs = {p.ref for p in deleted}
    for esp in windows_boot_esps(snap):
        if esp.ref not in deleted_refs:
            ops.append(Op("LIMINE_WINDOWS_ENTRY", esp.ref,
                          f"chainload /EFI/Microsoft/Boot/bootmgfw.efi by PARTUUID {esp.partuuid or '?'}"))
    for p in snap.partitions():
        if p.ref not in deleted_refs:
            ops.append(Op("UNTOUCHED", p.ref, p.name or p.fs or p.role))
    for d in snap.disks:
        if d.id != target.id:
            ops.append(Op("UNTOUCHED", d.id, "entire disk"))

    result = Plan(target.id, choices.mode, ops, warnings, map_lines)
    result.root_fs = choices.root_fs
    result.encrypt = choices.encrypt
    result.root_gb_max = (size - ESP_SIZE) // GIB
    result.created = [(OUR_ESP_NAME, round(ESP_SIZE / GIB, 1)), (OUR_ROOT_NAME, round(root_size / GIB, 1))]
    result.deleted = [o.target for o in ops if o.kind == "DELETE"]
    result.windows_boot_kept = [o.target for o in ops if o.kind == "LIMINE_WINDOWS_ENTRY"]
    try:
        assert_safe(result, snap, ov)
    except AssertionError as e:
        raise HardStop("UNSAFE_PLAN", f"The installer refused its own plan to protect your computer: {e}")
    return result


def _check_whole_disk(snap: Snapshot, target: Disk, roles: set, ov: frozenset):
    boot_here = [p for p in target.partitions if holds_windows_boot(p)]
    if boot_here and OVERRIDE_WINDOWS_BOOT not in ov:
        deps = sorted({d for p in boot_here for d in _dependents(snap, p)})
        where = ", ".join(deps) if deps else "unknown"
        other = [d for d in deps if d != target.id]
        why = (f"Windows installed on {', '.join(other)} boots through this disk and would stop booting."
               if other else "Windows on this disk would stop booting.")
        raise HardStop(
            "WINDOWS_BOOT_ON_TARGET",
            f"{target.id} contains the Windows Boot Manager EFI files ({boot_here[0].ref}). {why} (Windows installs depending on it: {where})",
            alternatives=[
                f"Free-space install on {target.id} (keeps the Windows EFI partition untouched)",
                "Pick a different, empty disk",
            ],
            override=OVERRIDE_WINDOWS_BOOT,
        )
    if "HOLDS_WINDOWS" in roles and OVERRIDE_WINDOWS not in ov:
        raise HardStop(
            "WINDOWS_ON_TARGET",
            f"{target.id} contains Windows (or an encrypted volume that may be Windows). Whole-disk install would destroy it.",
            alternatives=[f"Free-space install on {target.id}", "Pick a different disk"],
            override=OVERRIDE_WINDOWS,
        )
    if "HOLDS_FOREIGN_BOOT" in roles and OVERRIDE_FOREIGN_BOOT not in ov:
        raise HardStop(
            "FOREIGN_BOOT_ON_TARGET",
            f"{target.id} has another bootloader's EFI files (e.g. GRUB/rEFInd/another Linux). Wiping it may break another OS.",
            alternatives=[f"Free-space install on {target.id}", "Pick a different disk"],
            override=OVERRIDE_FOREIGN_BOOT,
        )
    # Ambiguity: UEFI says Windows boots from a partition on the target, but we
    # cannot see Windows Boot Manager there. Do not guess.
    for e in snap.nvram:
        if _is_windows_entry(e.label):
            p = snap.find_partuuid(e.partuuid)
            if p is not None and p.disk == target.id and not p.has_bootmgfw and OVERRIDE_AMBIGUOUS not in ov:
                raise NeedsConfirmation(
                    "NVRAM_POINTS_AT_TARGET_UNVERIFIED",
                    f"UEFI boot entry {e.bootnum} ({e.label}) points at {p.ref} on the target disk, but Windows boot files were not found there. "
                    "The layout is ambiguous; refusing to guess.",
                    alternatives=["Choose a different disk", "Boot Windows and check it starts normally, then retry"],
                    override=OVERRIDE_AMBIGUOUS,
                )


# --------------------------------------------------------------------------
# Independent safety net: re-checks the finished plan against the snapshot.
# Even if plan() has a bug, destructive ops on Windows boot data are refused.
# --------------------------------------------------------------------------

def protection_needed(snap: Snapshot, p: Partition) -> str | None:
    """Override phrase required before this partition may be deleted/formatted, else None."""
    if p.is_ours:
        return None
    if holds_windows_boot(p):
        return OVERRIDE_WINDOWS_BOOT
    if maybe_windows(p) or p.role in (ROLE_MSR, ROLE_WINRE):
        return OVERRIDE_WINDOWS
    if p in foreign_boot_esps(snap):
        return OVERRIDE_FOREIGN_BOOT
    return None


def assert_safe(plan_: Plan, snap: Snapshot, overrides: frozenset = frozenset()):
    by_ref = {p.ref: p for p in snap.partitions()}
    destructive = ("DELETE", "FORMAT", "ENCRYPT", "NEW_TABLE", "WIPE")
    deleted = set()
    for op in plan_.ops:
        if op.kind not in destructive:
            continue
        if op.kind == "NEW_TABLE":
            if op.target != plan_.target_disk:
                raise AssertionError(f"NEW_TABLE outside target disk: {op.target}")
            if (snap.disk(op.target).raw_fs in ("bitlocker", "ntfs")) and OVERRIDE_WINDOWS not in overrides:
                raise AssertionError(f"NEW_TABLE on {op.target}, which holds a Windows-type volume with no partition table, "
                                     f"without override {OVERRIDE_WINDOWS!r}")
            continue
        if op.target.startswith("new:"):
            continue
        p = by_ref.get(op.target)
        if p is None:
            raise AssertionError(f"{op.kind} on unknown partition {op.target}")
        if p.disk != plan_.target_disk:
            raise AssertionError(f"{op.kind} {op.target} is outside target disk {plan_.target_disk}")
        need = protection_needed(snap, p)
        if need and need not in overrides:
            raise AssertionError(f"{op.kind} on protected partition {op.target} without override {need!r}")
        deleted.add(p.ref)
    # NEW_TABLE implicitly deletes everything on the disk: each partition must have a DELETE.
    if any(o.kind == "NEW_TABLE" for o in plan_.ops):
        for p in snap.disk(plan_.target_disk).partitions:
            if p.ref not in deleted:
                raise AssertionError(f"NEW_TABLE would silently destroy {p.ref}")
    # CREATE must not overlap any surviving partition.
    survivors = [p for p in snap.disk(plan_.target_disk).partitions if p.ref not in deleted]
    for op in plan_.ops:
        if op.kind != "CREATE":
            continue
        for p in survivors:
            if op.start < p.end and p.start < op.start + op.size:
                raise AssertionError(f"CREATE {op.target} overlaps surviving partition {p.ref}")
