"""Apply a confirmed Plan to real block devices, with backup, verification and rollback.

Scope of this module: partition table changes + formatting of the partitions WE create, and proof that
nothing else changed. Installing the system, Limine and the UEFI entry are separate stages that plug in
via the `bootloader` hook (they run before verification, so a failure there is rolled back too).

Safety model:
  1. Refuse any plan that fails planner.assert_safe() (second, independent check).
  2. Re-detect the machine; refuse if it differs from the snapshot the user confirmed.
  3. Back up every partition table, the UEFI entries and every Windows EFI tree; hash those trees.
  4. Execute ops in order; new partitions are read back (never assumed) and must be genuinely new.
     Only partitions created in this run are ever formatted.
  5. Verify: Windows ESP files byte-identical, untouched partitions unchanged, other disks unchanged.
  6. On any failure: restore the target disk's partition table from backup and re-verify.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .detect import _mtools_env, detect_devices, sector_size
from .model import OUR_ESP_NAME, OUR_ROOT_NAME, SECTOR, Partition, Snapshot
from .progress import NULL, Progress
from .planner import Plan, assert_safe, windows_boot_esps

TYPE_ESP = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
TYPE_LINUX = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
NEW_PART = {OUR_ESP_NAME: (TYPE_ESP, "vfat"), OUR_ROOT_NAME: (TYPE_LINUX, "ext4")}   # root fs comes from the plan


class ApplyError(Exception):
    def __init__(self, code: str, message: str, *, rolled_back: bool | None = None, backup_dir: str = ""):
        super().__init__(f"[{code}] {message}")
        self.code, self.message, self.rolled_back, self.backup_dir = code, message, rolled_back, backup_dir


@dataclass
class ApplyResult:
    backup_dir: str
    created: dict = field(default_factory=dict)    # partition name -> device node
    windows_esps_verified: list = field(default_factory=list)
    skipped: list = field(default_factory=list)    # ops handled by later stages
    root_node: str = ""                            # what to mount as /: the partition, or the opened LUKS mapper
    luks_node: str = ""                            # the raw partition holding the LUKS container ("" = not encrypted)


CRYPT_NAME = "tapkichroot"


def _run(cmd, *, input=None, env=None, check=True):
    try:
        r = subprocess.run(cmd, input=input, text=True, capture_output=True, env=env)
    except FileNotFoundError:
        if check:
            raise ApplyError("COMMAND_MISSING", f"{cmd[0]} is not installed")
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")
    if check and r.returncode != 0:
        raise ApplyError("COMMAND_FAILED", f"{' '.join(cmd)} -> exit {r.returncode}: {r.stderr.strip() or r.stdout.strip()}")
    return r


def node_for(device: str, number: int) -> str:
    return f"{device}p{number}" if device[-1].isdigit() else f"{device}{number}"


def table_dump(device: str) -> str:
    """sfdisk -d text, or '' for a disk without a partition table."""
    r = _run(["sfdisk", "-d", device], check=False)
    return r.stdout if r.returncode == 0 else ""


def partition_numbers(device: str) -> set:
    r = _run(["sfdisk", "-J", device], check=False)
    if r.returncode != 0:
        return set()
    parts = json.loads(r.stdout)["partitiontable"].get("partitions", [])
    return {_num(p["node"]) for p in parts}


def _num(node: str) -> int:
    i = len(node)
    while i and node[i - 1].isdigit():
        i -= 1
    return int(node[i:])


def hash_efi_tree(device: str, offset: int, subdir: str = "EFI/Microsoft") -> dict:
    """sha256 of every file under ::/<subdir> on the FAT filesystem at device+offset (read-only)."""
    tmp = tempfile.mkdtemp()
    try:
        _run(["mcopy", "-s", "-n", "-i", f"{device}@@{offset}", f"::/{subdir}", tmp], env=_mtools_env(), check=False)
        out = {}
        for root, _, files in os.walk(tmp):
            for name in files:
                path = os.path.join(root, name)
                with open(path, "rb") as f:
                    out[os.path.relpath(path, tmp).replace(os.sep, "/")] = hashlib.sha256(f.read()).hexdigest()
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_passphrase(pw: str):
    """A passphrase must be typeable at the boot prompt: not empty, no line break/NUL, a sane length."""
    if not pw or any(c in pw for c in "\n\r\0") or len(pw) > 1000:
        raise ApplyError("BAD_PASSPHRASE", "The password cannot be used as a disk passphrase (empty, too long, or "
                                           "containing a line break). Nothing was written.")


def _wait_for(node: str, tries: int = 20):
    for _ in range(tries):
        if os.path.exists(node):
            return
        _run(["udevadm", "settle"], check=False)
        time.sleep(0.25)
    raise ApplyError("NODE_MISSING", f"device node {node} did not appear")


class Applier:
    def __init__(self, snap: Snapshot, plan: Plan, devices: dict, backup_dir: str, *,
                 overrides: frozenset = frozenset(),
                 redetect: Optional[Callable[[], Snapshot]] = None,
                 bootloader: Optional[Callable[["ApplyResult"], None]] = None,
                 before_op: Optional[Callable[[int, object], None]] = None,
                 after_op: Optional[Callable[[int, object], None]] = None,
                 progress: Optional[Progress] = None,
                 luks_passphrase: Optional[str] = None,
                 confirmed: Optional[Snapshot] = None):
        self.snap, self.plan, self.devices = snap, plan, devices
        self.confirmed = confirmed                         # the detection the user saw on the review screen, if known
        self.luks_passphrase = luks_passphrase
        self.backup_dir, self.overrides = backup_dir, overrides
        self.redetect = redetect or (lambda: detect_devices(devices))
        self.bootloader = bootloader
        self.before_op, self.after_op = before_op, after_op
        self.progress = progress or NULL
        if bootloader is not None and hasattr(bootloader, "progress"):
            bootloader.progress = self.progress            # the system stage reports on the same bar

    # ---- public ---------------------------------------------------------
    def run(self, *, i_have_confirmed: bool = False) -> ApplyResult:
        if not i_have_confirmed:
            raise ApplyError("NOT_CONFIRMED", "apply() needs i_have_confirmed=True after the user approved the plan")
        # 1. independent safety net
        try:
            assert_safe(self.plan, self.snap, self.overrides)
        except AssertionError as e:
            raise ApplyError("UNSAFE_PLAN", str(e))
        target = self.plan.target_disk
        if target not in self.devices:
            raise ApplyError("NO_DEVICE", f"no device mapped for disk {target}")
        if getattr(self.plan, "encrypt", False):
            if not self.luks_passphrase:
                raise ApplyError("NO_PASSPHRASE", "Encryption was chosen but no passphrase was supplied. Nothing was written.")
            check_passphrase(self.luks_passphrase)
        # 2. state must match what the user confirmed
        self.progress.emit(1, "Checking the disks")
        fresh = self.redetect()
        changed = ApplyError("STATE_CHANGED", "The disks changed after you confirmed the plan. Nothing was written; start again.")
        if layout_key(fresh) != layout_key(self.snap):
            raise changed
        if self.confirmed is not None and not same_as_confirmed(fresh, self.confirmed, target):
            raise changed
        # 3. backup + hashes
        result = ApplyResult(self.backup_dir)
        self.progress.emit(3, "Backing up partition tables" + (" and Windows boot files" if windows_boot_esps(self.snap) else ""))
        pre = self._backup()
        # 4-5. execute + verify; 6. rollback on any failure
        try:
            self._execute(result)
            if self.bootloader:
                self.bootloader(result)
            self._close_crypt()                          # the system stage has unmounted by now
            self.progress.emit(97, "Verifying that nothing else changed")
            self._verify(pre, result)
            self.progress.emit(100, "Done")
        except BaseException as e:                       # includes Ctrl-C / SIGTERM: never leave a half-installed disk
            err = e if isinstance(e, ApplyError) else ApplyError("UNEXPECTED", f"{type(e).__name__}: {e}")
            with _signals_ignored():                     # a second Ctrl-C must not cut the rollback short
                err.rolled_back = self._rollback(pre)
            err.backup_dir = self.backup_dir
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                e.rolled_back, e.backup_dir = err.rolled_back, err.backup_dir
                raise                                    # the caller still sees the interrupt, with the real rollback result
            raise err
        return result

    def _close_crypt(self):
        if os.path.exists(f"/dev/mapper/{CRYPT_NAME}"):
            _run(["cryptsetup", "close", CRYPT_NAME], check=False)

    # ---- steps ----------------------------------------------------------
    def _backup(self) -> dict:
        os.makedirs(self.backup_dir, exist_ok=True)
        pre = {"dumps": {}, "esp_hashes": {}}
        for disk_id, dev in self.devices.items():
            dump = table_dump(dev)
            # a disk that has a partition table must give a non-empty backup, otherwise a later rollback could not
            # restore it (and would have to wipe it): refuse BEFORE writing anything
            if not dump and disk_id in {d.id for d in self.snap.disks} and self.snap.disk(disk_id).table != "none":
                raise ApplyError("BACKUP_FAILED", f"could not back up the partition table of {disk_id}; nothing was written")
            pre["dumps"][disk_id] = dump
            with open(os.path.join(self.backup_dir, f"{_safe(disk_id)}.sfdisk"), "w") as f:
                f.write(dump)
        nv = _run(["efibootmgr", "-v"], check=False)
        with open(os.path.join(self.backup_dir, "efibootmgr.txt"), "w") as f:
            f.write(nv.stdout if nv.returncode == 0 else "")
        for esp in windows_boot_esps(self.snap):
            dev = self.devices[esp.disk]
            pre["esp_hashes"][esp.ref] = hash_efi_tree(dev, esp.start)
            if not pre["esp_hashes"][esp.ref]:           # an empty hash would make the later check pass vacuously
                raise ApplyError("BACKUP_FAILED", f"could not read the Windows boot files on {esp.ref}; nothing was written")
            dest = os.path.join(self.backup_dir, f"windows-esp-{_safe(esp.ref)}")
            os.makedirs(dest, exist_ok=True)
            _run(["mcopy", "-s", "-n", "-i", f"{dev}@@{esp.start}", "::/EFI/Microsoft", dest], env=_mtools_env(), check=False)
        with open(os.path.join(self.backup_dir, "pre-hashes.json"), "w") as f:
            json.dump(pre["esp_hashes"], f, indent=2)
        return pre

    def _execute(self, result: ApplyResult):
        target = self.plan.target_disk
        dev = self.devices[target]
        steps = [o for o in self.plan.ops if o.kind in ("NEW_TABLE", "DELETE", "CREATE", "ENCRYPT", "FORMAT")]
        done = 0
        labels = {"NEW_TABLE": "Creating a new partition table",
                  "DELETE": ("Removing the old TapkicH-OS partitions" if self.plan.mode == "reinstall" else
                             "Removing the old partitions"),
                  "CREATE": "Creating partitions", "ENCRYPT": "Encrypting the system partition (takes a moment)",
                  "FORMAT": "Formatting"}
        new_table = any(o.kind == "NEW_TABLE" for o in self.plan.ops)
        for i, op in enumerate(self.plan.ops):
            if self.before_op:
                self.before_op(i, op)
            if op.kind == "DELETE" and new_table:
                continue                       # a brand-new partition table already has none of the old partitions
            if op.kind in labels:
                self.progress.emit(4 + 8 * done / max(1, len(steps)), labels[op.kind])
                done += 1
            if op.kind == "NEW_TABLE":
                _run(["sfdisk", dev], input="label: gpt\n")
                _run(["udevadm", "settle"], check=False)
            elif op.kind == "DELETE":
                number = int(op.target.rsplit("#", 1)[1])
                _run(["sfdisk", "--delete", dev, str(number)])
                _run(["udevadm", "settle"], check=False)
            elif op.kind == "CREATE":
                self._create(dev, op, result)
            elif op.kind == "ENCRYPT":
                self._encrypt(op, result)
            elif op.kind == "FORMAT":
                self._format(op, result)
            elif op.kind in ("WRITE", "NVRAM", "LIMINE_WINDOWS_ENTRY"):
                result.skipped.append(f"{op.kind} {op.target}")      # handled by the bootloader stage
            elif op.kind == "UNTOUCHED":
                pass
            else:
                raise ApplyError("UNKNOWN_OP", f"unknown op {op.kind}")
            if self.after_op:
                self.after_op(i, op)

    def _create(self, dev: str, op, result: ApplyResult):
        name = op.target.split(":", 1)[1]
        ptype, _ = NEW_PART[name]
        before = partition_numbers(dev)
        ss = sector_size(dev)                  # sfdisk counts in the device's own sectors (4096 on 4Kn disks)
        line = f'start={op.start // ss}, size={op.size // ss}, type={ptype}, name="{name}"\n'
        _run(["sfdisk", "--append", dev], input=line)
        _run(["udevadm", "settle"], check=False)
        after = partition_numbers(dev)
        new = sorted(after - before)
        if len(new) != 1:
            raise ApplyError("CREATE_FAILED", f"expected exactly one new partition for {name}, saw {new}")
        number = new[0]
        # read back what was really created
        info = json.loads(_run(["sfdisk", "-J", dev]).stdout)["partitiontable"]["partitions"]
        mine = next(p for p in info if _num(p["node"]) == number)
        if abs(mine["start"] * ss - op.start) > 1 << 20 or abs(mine["size"] * ss - op.size) > 1 << 20:
            raise ApplyError("CREATE_MISMATCH", f"{name} was created at an unexpected place/size")
        node = node_for(dev, number)
        _wait_for(node)
        result.created[name] = node

    def _encrypt(self, op, result: ApplyResult):
        """LUKS2 on the new system partition, then open it; the file system is created inside (see _format)."""
        name = op.target.split(":", 1)[1]
        node = result.created.get(name)
        if node is None or name != OUR_ROOT_NAME:               # only ever encrypt the partition this run created
            raise ApplyError("ENCRYPT_REFUSED", f"{name} was not created by this run")
        key = self.luks_passphrase or ""
        _run(["wipefs", "-a", node])
        # the passphrase goes in on stdin (never on a command line); no trailing newline, so it matches what is typed at boot
        _run(["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "--key-file=-", node], input=key)
        _run(["cryptsetup", "open", "--key-file=-", node, CRYPT_NAME], input=key)
        mapper = f"/dev/mapper/{CRYPT_NAME}"
        _wait_for(mapper)
        result.luks_node = node
        result.root_node = mapper

    def _format(self, op, result: ApplyResult):
        name = op.target.split(":", 1)[1]
        node = result.created.get(name)
        if node is None:                       # only ever format partitions this run created
            raise ApplyError("FORMAT_REFUSED", f"{name} was not created by this run")
        if name == OUR_ROOT_NAME and result.luks_node:
            node = result.root_node                             # format INSIDE the encrypted container
        else:
            _run(["wipefs", "-a", node])
        if name == OUR_ESP_NAME:
            _run(["mkfs.fat", "-F", "32", "-n", "TAPKICH", node])
        elif op.detail == "btrfs":
            _run(["mkfs.btrfs", "-f", "-L", "TAPKICH", node])
        elif op.detail == "ext4":
            _run(["mkfs.ext4", "-F", "-q", "-L", "TAPKICH", node])
        else:
            raise ApplyError("FORMAT_REFUSED", f"unknown filesystem {op.detail!r} for {name}")

    def _verify(self, pre: dict, result: ApplyResult):
        fresh = self.redetect()
        target = self.plan.target_disk
        deleted = {o.target for o in self.plan.ops if o.kind == "DELETE"}
        # created partitions exist with the right filesystem
        want = dict(NEW_PART)
        want[OUR_ROOT_NAME] = (TYPE_LINUX, "luks" if getattr(self.plan, "encrypt", False)
                               else getattr(self.plan, "root_fs", "ext4"))
        for name, (_, fs) in want.items():
            if name in result.created:
                got = [p for p in fresh.disk(target).partitions if p.name == name and p.fs == fs]
                if not got:
                    raise ApplyError("VERIFY_NEW_PARTITION", f"{name} is missing or not {fs} after install")
        # every partition not planned for deletion is exactly as before
        for old in self.snap.partitions():
            if old.ref in deleted:
                continue
            # match by disk + number (DOS partitions have no GUID, and cloned disks can share GUIDs); the GUID is only
            # an extra check when there is one
            try:
                now = next((p for p in fresh.disk(old.disk).partitions if p.number == old.number), None)
            except KeyError:
                now = None
            if (now is None or (now.start, now.size, now.fs) != (old.start, old.size, old.fs)
                    or (old.partuuid and now.partuuid != old.partuuid)):
                raise ApplyError("VERIFY_PARTITION_CHANGED", f"{old.ref} ({old.name or old.fs}) is missing or changed")
        # other disks: partition table byte-for-byte identical
        for disk_id, dev in self.devices.items():
            if disk_id != target and table_dump(dev) != pre["dumps"][disk_id]:
                raise ApplyError("VERIFY_OTHER_DISK", f"partition table of {disk_id} changed")
        # Windows boot files: identical hashes
        for esp in windows_boot_esps(self.snap):
            if esp.ref in deleted:
                continue
            now = hash_efi_tree(self.devices[esp.disk], esp.start)
            if now != pre["esp_hashes"][esp.ref]:
                raise ApplyError("WINDOWS_ESP_CHANGED",
                                 f"Windows boot files on {esp.ref} changed! Originals are saved in {self.backup_dir}")
            result.windows_esps_verified.append(esp.ref)

    def _rollback(self, pre: dict) -> bool:
        """Put the target disk's partition table back. True only if the machine matches the original again."""
        target = self.plan.target_disk
        dev = self.devices[target]
        undo = getattr(self.bootloader, "undo", None)
        if undo:                      # unmount + remove the UEFI entry this run added, before touching the table
            try:
                undo()
            except Exception:
                pass
        self._close_crypt()           # an opened LUKS mapper would keep the partition busy
        try:
            dump = pre["dumps"][target]
            if dump:
                _run(["sfdisk", "--force", dev], input=dump)
            elif self.snap.disk(target).table == "none":
                _run(["wipefs", "-a", dev])              # the disk had no partition table at all: back to that
            else:
                return False                             # had a table but no usable backup: never wipe it

            _run(["udevadm", "settle"], check=False)
            return layout_key(self.redetect()) == layout_key(self.snap)
        except Exception:
            return False


class _signals_ignored:
    """Ignore Ctrl-C / SIGTERM / SIGHUP inside the block (only possible on the main thread)."""
    SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def __enter__(self):
        self.old = {}
        for sig in self.SIGNALS:
            try:
                self.old[sig] = signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                pass
        return self

    def __exit__(self, *exc):
        for sig, handler in self.old.items():
            signal.signal(sig, handler)
        return False


def same_as_confirmed(fresh: Snapshot, confirmed: Snapshot, target: str) -> bool:
    """The machine still looks like what the user confirmed: same layout, and the target is the same physical disk
    (size, plus model/serial wherever both scans know them; virtio disks have no serial)."""
    if layout_key(fresh) != layout_key(confirmed):
        return False
    try:
        a, b = fresh.disk(target), confirmed.disk(target)
    except KeyError:
        return False
    if a.size != b.size:
        return False
    for x, y in ((a.model, b.model), (a.serial, b.serial)):
        if x.strip() and y.strip() and " ".join(x.split()) != " ".join(y.split()):
            return False
    return True


def layout_key(snap: Snapshot) -> list:
    """What must not change between confirmation and apply: the real disk layout.
    Cosmetic fields (model, serial, removable flag) are ignored: two scans can legitimately differ there."""
    return [(d.id, d.size, d.table,
             [(p.number, p.start, p.size, p.partuuid, p.fs, p.name, p.has_bootmgfw, p.has_windows) for p in d.partitions])
            for d in snap.disks]


def _safe(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)
