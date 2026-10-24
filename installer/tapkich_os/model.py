"""Data model shared by the detector and the planner.

Everything here is plain data (JSON-serialisable) so the planner can be tested
without touching a disk: detect() produces a Snapshot, plan() consumes it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Optional

SECTOR = 512
MIB = 1024 * 1024
GIB = 1024 * MIB

# Partition roles, derived from GPT type GUID / MBR type.
ROLE_ESP = "esp"
ROLE_MSR = "msr"
ROLE_WINRE = "windows-re"
ROLE_DATA = "data"
ROLE_LINUX = "linux"
ROLE_OTHER = "other"

# Names of partitions this installer creates (GPT partition names).
OUR_ESP_NAME = "TAPKICH_EFI"
OUR_ROOT_NAME = "TAPKICH_ROOT"


@dataclass
class Partition:
    disk: str                      # disk id (device path or image name)
    number: int
    start: int                     # bytes
    size: int                      # bytes
    role: str = ROLE_OTHER
    name: str = ""                 # GPT partition name
    partuuid: str = ""
    fs: str = ""                   # vfat, ntfs, bitlocker, ext4, btrfs, ""
    # ESP / FAT contents
    efi_dirs: list = field(default_factory=list)   # directory names under /EFI
    has_bootmgfw: bool = False     # EFI/Microsoft/Boot/bootmgfw.efi present
    # NTFS contents
    has_windows: bool = False      # Windows/System32 present
    ntfs_dirty: bool = False
    unreadable: bool = False       # an NTFS volume we could not open at all: it might be Windows, so treat it as such

    @property
    def end(self) -> int:
        return self.start + self.size

    @property
    def is_ours(self) -> bool:
        # TAPKICH_* are ours; HYPRARCHIK_* is the old name (earlier test installs are still recognised for reinstall)
        return self.name.startswith(("TAPKICH_", "HYPRARCHIK_"))

    @property
    def ref(self) -> str:
        return f"{self.disk}#{self.number}"


@dataclass
class Disk:
    id: str
    size: int
    model: str = ""
    serial: str = ""
    table: str = "none"            # gpt | mbr | hybrid | none
    removable: bool = False
    installer_media: bool = False
    first_usable: int = 1 * MIB
    last_usable: int = 0
    raw_fs: str = ""               # a file system written straight onto the disk, with no partition table
    partitions: list = field(default_factory=list)

    def free_regions(self, min_size: int = 1 * MIB):
        """Unallocated byte ranges [(start, size)], aligned to 1 MiB."""
        regions = []
        cursor = self.first_usable
        end = self.last_usable or self.size
        for p in sorted(self.partitions, key=lambda p: p.start):
            if p.start - cursor >= min_size:
                regions.append((_align_up(cursor), p.start - _align_up(cursor)))
            cursor = max(cursor, p.end)
        if end - cursor >= min_size:
            regions.append((_align_up(cursor), end - _align_up(cursor)))
        return [(s, z) for s, z in regions if z >= min_size]


def _align_up(x: int, to: int = MIB) -> int:
    return (x + to - 1) // to * to


@dataclass
class NvramEntry:
    bootnum: str
    label: str
    partuuid: str = ""             # partition GUID the entry points at, if any
    path: str = ""


@dataclass
class Firmware:
    uefi: bool = True
    secure_boot: bool = False
    rst_raid: bool = False


@dataclass
class Snapshot:
    disks: list
    nvram: list = field(default_factory=list)
    firmware: Firmware = field(default_factory=Firmware)

    def disk(self, disk_id: str) -> Disk:
        for d in self.disks:
            if d.id == disk_id:
                return d
        raise KeyError(disk_id)

    def partitions(self):
        for d in self.disks:
            yield from d.partitions

    def find_partuuid(self, partuuid: str) -> Optional[Partition]:
        if not partuuid:
            return None
        for p in self.partitions():
            if p.partuuid and p.partuuid.lower() == partuuid.lower():
                return p
        return None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @staticmethod
    def from_json(text: str) -> "Snapshot":
        raw = json.loads(text)
        disks = []
        for d in raw["disks"]:
            parts = [Partition(**p) for p in d.pop("partitions")]
            disks.append(Disk(partitions=parts, **d))
        return Snapshot(
            disks=disks,
            nvram=[NvramEntry(**n) for n in raw.get("nvram", [])],
            firmware=Firmware(**raw.get("firmware", {})),
        )
