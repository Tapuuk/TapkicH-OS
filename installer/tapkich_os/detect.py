"""Read-only disk detection. Produces a model.Snapshot.

Works on raw block devices and on plain image files (used by the tests): all
probing is done by reading bytes at an offset plus read-only helper tools
(sfdisk -J, mtools, ntfsls). Nothing here writes to the disk.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from .model import (
    MIB, SECTOR, ROLE_DATA, ROLE_ESP, ROLE_LINUX, ROLE_MSR, ROLE_OTHER, ROLE_WINRE,
    Disk, Firmware, NvramEntry, Partition, Snapshot,
)

GPT_ROLES = {
    "C12A7328-F81F-11D2-BA4B-00A0C93EC93B": ROLE_ESP,
    "E3C9E316-0B5C-4DB8-817D-F92DF00215AE": ROLE_MSR,
    "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC": ROLE_WINRE,
    "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7": ROLE_DATA,
    "0FC63DAF-8483-4772-8E79-3D69D8477DE4": ROLE_LINUX,
}
DOS_ROLES = {"ef": ROLE_ESP, "07": ROLE_DATA, "7": ROLE_DATA, "83": ROLE_LINUX, "27": ROLE_WINRE}


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def read_at(path: str, offset: int, n: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(n)


def device_size(path: str) -> int:
    with open(path, "rb") as f:
        return f.seek(0, os.SEEK_END)


def sector_size(path: str) -> int:
    """Logical sector size of a block device (512 or 4096); 512 for plain image files."""
    try:
        if stat.S_ISBLK(os.stat(path).st_mode):
            import fcntl
            import struct as _struct
            with open(path, "rb") as f:
                buf = fcntl.ioctl(f.fileno(), 0x1268, b"\0\0\0\0")      # BLKSSZGET
            return _struct.unpack("I", buf)[0] or SECTOR
    except OSError:
        pass
    return SECTOR


def disk_table_kind(path: str) -> str:
    """gpt | hybrid | mbr | none, from raw bytes (sfdisk hides hybrid MBRs)."""
    mbr = read_at(path, 0, 512)
    if len(mbr) < 512:
        return "none"
    # the GPT header sits in LBA 1: byte 512 on normal disks, byte 4096 on 4K-sector disks
    has_gpt = read_at(path, sector_size(path), 8) == b"EFI PART"
    types = []
    for i in range(4):
        e = mbr[446 + 16 * i: 446 + 16 * (i + 1)]
        types.append(e[4])
    if has_gpt:
        return "hybrid" if any(t not in (0x00, 0xEE) for t in types) else "gpt"
    if mbr[510:512] == b"\x55\xaa" and any(types):
        return "mbr"
    return "none"


def probe_fs(path: str, offset: int) -> str:
    boot = read_at(path, offset, 512)
    if len(boot) < 512:
        return ""
    if boot[3:11] == b"-FVE-FS-":
        return "bitlocker"
    if boot[0:6] == b"LUKS\xba\xbe":
        return "luks"
    if boot[3:11] == b"NTFS    ":
        return "ntfs"
    if boot[54:59] in (b"FAT12", b"FAT16") or boot[82:87] == b"FAT32":
        return "vfat"
    if read_at(path, offset + 0x10040, 8) == b"_BHRfS_M":
        return "btrfs"
    sb = read_at(path, offset + 1080, 2)
    if sb == b"\x53\xef":
        return "ext4"
    return ""


def _mtools_env():
    env = dict(os.environ)
    env["MTOOLS_SKIP_CHECK"] = "1"
    return env


def probe_fat(path: str, offset: int):
    """Return (efi_dirs, has_bootmgfw) by listing /EFI read-only with mtools."""
    spec = f"{path}@@{offset}"
    r = _run(["mdir", "-b", "-i", spec, "::/EFI"], env=_mtools_env())
    dirs = []
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            name = line.strip().rstrip("/").split("/")[-1]
            if name and name not in (".", ".."):
                dirs.append(name)
    r2 = _run(["mdir", "-b", "-i", spec, "::/EFI/Microsoft/Boot/bootmgfw.efi"], env=_mtools_env())
    return dirs, r2.returncode == 0


def hibernated(header: bytes) -> bool:
    """The first bytes of hiberfil.sys: "hibr"/"wake" (any case) = Windows hibernated (classic hibernation);
    zeros = no hibernation image."""
    return header[:4].lower() in (b"hibr", b"wake")


RESTART_VOLUME_IS_CLEAN = 0x0002


def ntfs_head(dev: str, path: str, n: int, timeout: float = 30) -> bytes:
    """The first n bytes of a file on an NTFS volume, read-only, without reading the rest (hiberfil.sys can be many
    GB: reading it all took over a minute in a slow VM and the timeout crashed detection, 2026-10-20). Anything that
    goes wrong gives b"" (= unknown): a warning check must never stop the installer."""
    try:
        p = subprocess.Popen(["ntfscat", dev, path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return b""
    data = b""
    try:
        import select
        end = time.monotonic() + timeout
        while len(data) < n and time.monotonic() < end:
            ready, _, _ = select.select([p.stdout], [], [], max(0.0, end - time.monotonic()))
            if not ready:
                break
            chunk = os.read(p.stdout.fileno(), n - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        data = b""
    finally:
        p.kill()
        p.wait()
        p.stdout.close()
    return data


def logfile_unclean(page: bytes):
    """NTFS's journal ($LogFile), first restart page: True when the volume was NOT cleanly unmounted. That is how
    Windows 8+ leaves its disk after a Fast Startup shutdown (ntfs-3g: "Metadata kept in Windows cache"), and after a
    crash; the hiberfil.sys header stays zero then (seen with Windows 11 in the VM, 2026-10-20). None when the page
    cannot be read. Layout: "RSTR" + ... + restart_area_offset (u16 at 0x18); restart area flags (u16 at +0x0E)."""
    if len(page) < 0x40 or page[:4] not in (b"RSTR", b"CHKD"):
        return None
    if page[:4] == b"CHKD":                      # chkdsk was interrupted: certainly not clean
        return True
    ra = int.from_bytes(page[0x18:0x1A], "little")
    if ra + 0x10 > len(page):
        return None
    flags = int.from_bytes(page[ra + 0x0E:ra + 0x10], "little")
    return not (flags & RESTART_VOLUME_IS_CLEAN)


def probe_ntfs(path: str, offset: int, size: int, node: str | None):
    """Return (has_windows, dirty, unreadable). Uses the partition node when we have one, else a temp copy."""
    tmp = None
    dev = node
    try:
        if dev is None:
            tmp = tempfile.NamedTemporaryFile(delete=False)
            # Copy only the filesystem itself (boot sector says how big it is),
            # not the whole partition: Windows partitions can be hundreds of GB.
            boot = read_at(path, offset, 512)
            bps = int.from_bytes(boot[0x0B:0x0D], "little") or SECTOR
            fs_bytes = int.from_bytes(boot[0x28:0x30], "little") * bps + bps
            with open(path, "rb") as src:
                src.seek(offset)
                left = min(size, fs_bytes)
                while left:
                    chunk = src.read(min(left, 4 * MIB))
                    if not chunk:
                        break
                    tmp.write(chunk)
                    left -= len(chunk)
            tmp.close()
            dev = tmp.name
        r = _run(["ntfsls", "-p", "/Windows/System32", dev])
        has_windows = r.returncode == 0
        unreadable = False
        if not has_windows:
            # "no Windows folder" and "could not open the volume at all" are different: a volume we cannot read
            # (damaged, needs chkdsk, unsupported feature) might still be a Windows install, so it is treated as one
            unreadable = _run(["ntfsls", dev]).returncode != 0
        info = _run(["ntfsinfo", "-m", dev])
        dirty = "DIRTY" in info.stdout.upper() if info.returncode == 0 else False
        # Fast Startup / hibernation does not set the dirty flag: it leaves a hibernation image whose header starts
        # with "hibr" or "wake" (ntfs-3g's own check). We never write to Windows, but the user is warned.
        if has_windows:
            if hibernated(ntfs_head(dev, "/hiberfil.sys", 4)):
                dirty = True
            if logfile_unclean(ntfs_head(dev, "$LogFile", 4096)):
                dirty = True                       # Fast Startup (Windows 8+) or not shut down cleanly
        return has_windows, dirty, unreadable
    finally:
        if tmp is not None:
            os.unlink(tmp.name)


def read_disk(path: str, disk_id: str | None = None, *, node_for_partition=None) -> Disk:
    disk_id = disk_id or path
    size = device_size(path)
    table = disk_table_kind(path)
    disk = Disk(id=disk_id, size=size, table=table, last_usable=size)
    if table == "none":
        disk.raw_fs = probe_fs(path, 0)          # e.g. a disk formatted without any partition table
        return disk
    r = _run(["sfdisk", "-J", path])
    if r.returncode != 0:
        raise RuntimeError(f"sfdisk failed on {path}: {r.stderr.strip()}")
    pt = json.loads(r.stdout)["partitiontable"]
    sector = pt.get("sectorsize", SECTOR)
    if pt["label"] == "gpt":
        disk.first_usable = pt.get("firstlba", 34) * sector
        disk.last_usable = (pt.get("lastlba", size // sector - 34) + 1) * sector
    else:
        disk.first_usable = 1 * MIB
        disk.last_usable = size
    for entry in pt.get("partitions", []):
        number = int(re.search(r"(\d+)$", entry["node"]).group(1))
        start, psize = entry["start"] * sector, entry["size"] * sector
        ptype = entry["type"].upper()
        role = GPT_ROLES.get(ptype) if pt["label"] == "gpt" else DOS_ROLES.get(ptype.lower(), ROLE_OTHER)
        part = Partition(
            disk=disk_id, number=number, start=start, size=psize,
            role=role or ROLE_OTHER, name=entry.get("name", ""),
            partuuid=entry.get("uuid", "").lower(),
        )
        part.fs = probe_fs(path, start)
        if part.fs == "vfat":
            part.efi_dirs, part.has_bootmgfw = probe_fat(path, start)
        elif part.fs == "ntfs":
            node = node_for_partition(number) if node_for_partition else None
            part.has_windows, part.ntfs_dirty, part.unreadable = probe_ntfs(path, start, psize, node)
        disk.partitions.append(part)
    return disk


BOOT_ENTRY = re.compile(r"^Boot([0-9A-Fa-f]{4})\*?\s+(.*)$")
HD_GUID = re.compile(r"HD\(\d+,GPT,([0-9a-fA-F-]{36})", re.I)


def parse_efibootmgr(text: str):
    """Parse `efibootmgr -v` output into NvramEntry objects."""
    out = []
    for line in text.splitlines():
        m = BOOT_ENTRY.match(line.strip())
        if not m:
            continue
        rest = m.group(2)
        label, _, dev = rest.partition("\t")
        g = HD_GUID.search(dev or rest)
        pm = re.search(r"File\(([^)]*)\)", dev or rest)
        out.append(NvramEntry(bootnum=m.group(1), label=label.strip(),
                              partuuid=g.group(1).lower() if g else "",
                              path=pm.group(1) if pm else ""))
    return out


def detect_images(images: dict, *, nvram_text: str = "", firmware: Firmware | None = None,
                  removable=(), installer_media=()) -> Snapshot:
    """Snapshot of disk *image files* (tests): {disk_id: path}."""
    disks = []
    for disk_id, path in images.items():
        d = read_disk(path, disk_id)
        d.removable = disk_id in removable
        d.installer_media = disk_id in installer_media
        disks.append(d)
    return Snapshot(disks=disks, nvram=parse_efibootmgr(nvram_text), firmware=firmware or Firmware())


def detect_devices(devices: dict, *, nvram_text: str = "", firmware: Firmware | None = None) -> Snapshot:
    """Snapshot of block devices under chosen ids: {disk_id: "/dev/xyz"} (also loop devices in tests)."""
    disks = []
    for disk_id, path in devices.items():
        sep = "p" if path[-1].isdigit() else ""
        disks.append(read_disk(path, disk_id, node_for_partition=lambda n, p=path, s=sep: f"{p}{s}{n}"))
    return Snapshot(disks=disks, nvram=parse_efibootmgr(nvram_text), firmware=firmware or Firmware())


def _installer_disks() -> set:
    """Disks the live ISO is on, so they are never offered as a target. Found two ways: where /run/archiso/bootmnt is
    mounted from, and any ISO 9660 volume labelled like our ISO (also covers copy-to-RAM boots)."""
    found = set()
    src = _run(["findmnt", "-n", "-o", "SOURCE", "/run/archiso/bootmnt"]).stdout.strip()
    if src:
        found.update(_disk_ancestors(src) or {src})
    for line in _run(["lsblk", "-rno", "PATH,LABEL,FSTYPE"]).stdout.splitlines():
        parts = line.split(" ")
        if len(parts) >= 3 and parts[2] == "iso9660" and parts[1].startswith(("TAPKICH_", "ARCH_", "HYPRARCHIK_")):
            found.update(_disk_ancestors(parts[0]) or {parts[0]})
    return found


def _disk_ancestors(node: str) -> set:
    """The physical disk(s) under a block device, however many layers sit between (partition, device-mapper such as a
    Ventoy stick's /dev/mapper/ventoy). `lsblk -s` lists the device followed by everything below it."""
    out = _run(["lsblk", "-snrpo", "PATH,TYPE", node]).stdout
    return {line.split(" ")[0] for line in out.splitlines() if line.endswith(" disk")}


# not install targets: optical, RAM/zram, floppy, network block, device-mapper, eMMC boot/rpmb hardware partitions
_SKIP_DEVICE = re.compile(r"^(loop|ram|zram|sr|fd|nbd|dm-)|^mmcblk\d+(boot\d+|rpmb)$")


def raid_member_reason(name: str, sysblock: str = "/sys/block", run=_run) -> str:
    """Why the whole disk `name` (e.g. "sda") is a member of a RAID set, or "" for a normal disk. Writing one member
    can break the array (Intel RST / mdraid, which often holds Windows). Two signs: an md device holds the disk in
    /sys/block/<d>/holders, or the RAID metadata on it (blkid TYPE ending in _raid_member). Other holders (LUKS / LVM
    opened on the whole disk) are not RAID: see `in_use_reason`."""
    md = [h for h in disk_holders(name, sysblock) if h.startswith("md")]
    if md:
        return f"in use by {', '.join(md)}"
    try:
        fstype = run(["blkid", "-p", "-s", "TYPE", "-o", "value", f"/dev/{name}"]).stdout.strip()
    except OSError:
        return ""
    return f"{fstype} metadata" if fstype.endswith("_raid_member") else ""


def disk_holders(name: str, sysblock: str = "/sys/block") -> list:
    """What holds the whole disk `name` open (/sys/block/<d>/holders): md0, dm-0, ..."""
    try:
        return sorted(os.listdir(f"{sysblock}/{name}/holders"))
    except OSError:
        return []


def in_use_reason(name: str, sysblock: str = "/sys/block") -> str:
    """"in use by dm-0" when something other than RAID (md) holds the whole disk, else ""."""
    other = [h for h in disk_holders(name, sysblock) if not h.startswith("md")]
    return f"in use by {', '.join(other)}" if other else ""


def detect_system() -> Snapshot:
    """Snapshot of the running machine (live ISO). Needs root; read-only. A device that cannot be read (empty
    card-reader slot, broken disk) is skipped and logged instead of stopping detection of all the others."""
    installer = _installer_disks()
    disks = []
    skipped = []
    for name in sorted(os.listdir("/sys/block")):
        if _SKIP_DEVICE.match(name):
            continue
        path = f"/dev/{name}"
        sep = "p" if name[-1].isdigit() else ""
        try:
            d = read_disk(path, path, node_for_partition=lambda n, p=path, s=sep: f"{p}{s}{n}")
            d.removable = open(f"/sys/block/{name}/removable").read().strip() == "1"
        except (OSError, RuntimeError, ValueError, KeyError) as e:
            skipped.append(f"{path}: {e}")
            continue
        d.installer_media = path in installer
        d.raid_member = raid_member_reason(name)
        d.in_use = "" if d.raid_member else in_use_reason(name)
        for attr in ("model", "serial"):
            f = f"/sys/block/{name}/device/{attr}"
            if os.path.exists(f):
                try:
                    setattr(d, attr, open(f).read().strip())
                except OSError:
                    pass
        disks.append(d)
    if skipped:
        print("detect: skipped unreadable devices: " + "; ".join(skipped), file=sys.stderr)
    nv = _run(["efibootmgr", "-v"]).stdout if shutil.which("efibootmgr") else ""
    fw = Firmware(uefi=os.path.isdir("/sys/firmware/efi"), secure_boot=_secure_boot(), rst_raid=rst_raid_mode())
    return Snapshot(disks=disks, nvram=parse_efibootmgr(nv), firmware=fw, skipped=skipped)


# Intel storage controllers in RAID / "Intel RST" mode: PCI class 01:04 (RAID bus controller), and Intel VMD (Volume
# Management Device, the RST mode of newer laptops; Linux handles it with the `vmd` driver). Windows installed in this
# mode only starts in it, so the firmware's SATA/NVMe mode must not simply be switched to AHCI.
INTEL = "0x8086"
VMD_DEVICE_IDS = {"0x201d", "0x28c0", "0x467f", "0x4c3d", "0x7d0b", "0x9a0b", "0xa77f", "0xad0b", "0xb60b", "0xb06f"}


def rst_raid_from_pci(devices: list) -> bool:
    """devices: [(vendor, device, class)] as in /sys/bus/pci/devices/*/{vendor,device,class} ("0x8086", "0x9a0b",
    "0x010400")."""
    for vendor, device, cls in devices:
        if vendor.lower() != INTEL:
            continue
        if cls.lower().startswith("0x0104") or device.lower() in VMD_DEVICE_IDS:
            return True
    return False


def rst_raid_mode(root: str = "/sys/bus/pci/devices") -> bool:
    """Best effort, for a warning only: whatever goes wrong while reading PCI data, detection must go on."""
    found = []
    try:
        for dev in os.listdir(root):
            vals = []
            for attr in ("vendor", "device", "class"):
                try:
                    with open(os.path.join(root, dev, attr)) as f:
                        vals.append(f.read().strip())
                except Exception:                       # noqa: BLE001
                    vals.append("")
            found.append(tuple(vals))
    except Exception:                                   # noqa: BLE001 (a hint must never stop the installer)
        return False
    return rst_raid_from_pci(found)


def _secure_boot() -> bool:
    try:
        for f in os.listdir("/sys/firmware/efi/efivars"):
            if f.startswith("SecureBoot-"):
                data = open(f"/sys/firmware/efi/efivars/{f}", "rb").read()
                return len(data) > 4 and data[-1] == 1
    except OSError:
        pass
    return False
