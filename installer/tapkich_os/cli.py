"""Command line entry: python3 -m tapkich_os {detect|plan|report}. Read-only; writes nothing to disks."""
from __future__ import annotations

import argparse
import json
import sys

from .detect import detect_system
from .model import GIB, MIB, Snapshot
from .planner import (
    Choices, ESP_SIZE, MIN_ROOT, PlanError, MODE_FREE_SPACE, MODE_REINSTALL, MODE_WHOLE_DISK,
    boot_map, disk_roles, plan,
)


def _snapshot(args) -> Snapshot:
    if getattr(args, "snapshot", None):
        with open(args.snapshot) as f:
            return Snapshot.from_json(f.read())
    return detect_system()


def _what_is_on(snap: Snapshot, disk) -> str:
    bits = []
    roles = disk_roles(snap, disk.id)
    if "HOLDS_WINDOWS" in roles:
        bits.append("Windows")
    if "HOLDS_WINDOWS_BOOT" in roles:
        bits.append("Windows boot files")
    if "HOLDS_FOREIGN_BOOT" in roles:
        bits.append("other bootloader")
    if any(p.is_ours for p in disk.partitions):
        bits.append("previous TapkicH-OS")
    if not bits and disk.partitions:
        kinds = sorted({p.fs or "unknown" for p in disk.partitions})
        bits.append(f"{len(disk.partitions)} partition{'s' if len(disk.partitions) != 1 else ''}: {', '.join(kinds)}")
    if bits:
        return ", ".join(bits)
    if disk.raw_fs:
        return f"{disk.raw_fs} written straight on the disk, no partition table"
    return "empty, no partition table" if disk.table == "none" else "empty, partition table with no partitions"


def disks_for_wizard(snap: Snapshot) -> list:
    out = []
    for d in snap.disks:
        if d.installer_media:
            continue
        free = max((z for _, z in d.free_regions()), default=0)
        out.append({
            "id": d.id,
            "size_gb": round(d.size / GIB),
            "free_gb": round(free / GIB),
            "model": d.model,
            "table": d.table,
            "roles": sorted(disk_roles(snap, d.id)),
            "has_previous_install": any(p.is_ours for p in d.partitions),
            "partitions": len(d.partitions),
            "label": f"{d.id}  {round(d.size / GIB)} GB  {d.model or 'disk'}  [{_what_is_on(snap, d)}]"
                     + (f"  (too small: needs {(ESP_SIZE + MIN_ROOT) // GIB} GB)" if d.size - 2 * MIB < ESP_SIZE + MIN_ROOT else ""),
        })
    return out


def _gb(value):
    return None if value is None else int(value) * GIB


def _apply(args) -> int:
    from .apply import Applier, ApplyError
    from .detect import detect_devices

    if args.devices:
        devices = json.loads(args.devices)
        snap = detect_devices(devices)
    else:
        snap = detect_system()
        devices = {d.id: d.id for d in snap.disks}
    from .progress import Progress
    import signal

    def _interrupt(signum, frame):                      # SIGTERM/SIGHUP behave like Ctrl-C: roll back, then exit
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGHUP, _interrupt)
    overrides = frozenset(args.override)
    confirmed = None
    if args.confirmed_snapshot:
        try:
            with open(args.confirmed_snapshot) as f:
                confirmed = Snapshot.from_json(f.read())
        except (OSError, ValueError, KeyError, TypeError) as e:     # e.g. a half-written file: nothing is written then
            print(json.dumps({"ok": False, "code": "CRASH", "message": "Could not read the disk list confirmed on the "
                              f"review screen ({type(e).__name__}). Nothing was written; start again."}))
            return 3
    prog = Progress(sys.stdout if args.progress else None)
    prog.log(f"apply: disk={args.disk} mode={args.mode} fs={args.fs} root_gb={args.root_gb}")
    prog.start_heartbeat()                              # the screen keeps counting during long steps
    try:
        try:
            p = plan(snap, Choices(args.disk, args.mode, root_size=_gb(args.root_gb), root_fs=args.fs,
                                   encrypt=args.encrypt, overrides=overrides))
            hook = None
            answers = None
            if args.system_answers:
                from .system import SystemAnswers, SystemInstaller
                answers = SystemAnswers.from_file(args.system_answers)
                hook = SystemInstaller(snap, p, answers)
            result = Applier(snap, p, devices, args.backup_dir, overrides=overrides, bootloader=hook, progress=prog,
                             luks_passphrase=(answers.disk_password or answers.password) if (answers and args.encrypt) else None,
                             redetect=None if args.devices else detect_system,
                             confirmed=confirmed).run(i_have_confirmed=args.i_have_confirmed)
        finally:
            prog.stop_heartbeat()                       # nothing may interleave with the final result line
    except PlanError as e:
        prog.log(f"STOPPED by planner [{e.code}] {e.message}")
        print(json.dumps({"ok": False, "code": e.code, "message": e.message}))
        return 2
    except ApplyError as e:
        prog.log(f"FAILED [{e.code}] {e.message} (rolled_back={e.rolled_back})")
        print(json.dumps({"ok": False, "code": e.code, "message": e.message, "rolled_back": e.rolled_back,
                          "backup_dir": e.backup_dir}))
        return 3
    except KeyboardInterrupt as e:                      # Ctrl-C / SIGTERM: apply() has already tried the rollback
        rolled_back = getattr(e, "rolled_back", None)   # None: stopped before anything was written
        prog.log(f"INTERRUPTED (rolled_back={rolled_back})")
        message = "The installation was interrupted. " + (
            "The disk layout was put back as it was." if rolled_back else
            "The disk layout could not be fully restored." if rolled_back is False else
            "Nothing was written to the disks.")
        print(json.dumps({"ok": False, "code": "INTERRUPTED", "message": message, "rolled_back": rolled_back,
                          "backup_dir": getattr(e, "backup_dir", None) or args.backup_dir}))
        return 130
    except Exception as e:                              # never leave the wizard without a reason to show
        prog.log(f"CRASH {type(e).__name__}: {e}")
        print(json.dumps({"ok": False, "code": "CRASH", "message": f"The installer hit an unexpected error: {type(e).__name__}: {e}"}))
        return 3
    prog.log("apply: OK")
    prog.close()
    print(json.dumps({"ok": True, "created": result.created, "backup_dir": result.backup_dir,
                      "windows_esps_verified": result.windows_esps_verified, "later_stages": result.skipped,
                      "notes": list(getattr(hook, "notes", []) or [])}))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tapkich-installer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect", help="print the machine snapshot as JSON")
    d.set_defaults(snapshot=None)
    pl = sub.add_parser("plan", help="show the plan for a disk (dry run)")
    pl.add_argument("--disk", required=True)
    pl.add_argument("--mode", choices=[MODE_FREE_SPACE, MODE_WHOLE_DISK, MODE_REINSTALL], default=MODE_FREE_SPACE)
    pl.add_argument("--override", action="append", default=[])
    pl.add_argument("--snapshot")
    r = sub.add_parser("report", help="boot map plus, for every disk, what a whole-disk install would do")
    r.add_argument("--snapshot")
    ap_ = sub.add_parser("apply", help="WRITES to disks: partition + format the confirmed plan (backup, verify, rollback)")
    ap_.add_argument("--disk", required=True)
    ap_.add_argument("--mode", choices=[MODE_FREE_SPACE, MODE_WHOLE_DISK, MODE_REINSTALL], default=MODE_FREE_SPACE)
    ap_.add_argument("--override", action="append", default=[])
    ap_.add_argument("--devices", help='JSON {"id": "/dev/path"}; default = scan this machine')
    ap_.add_argument("--backup-dir", default="/var/log/tapkich-os-install/backup")
    ap_.add_argument("--i-have-confirmed", action="store_true", help="required: the user approved the plan")
    ap_.add_argument("--system-answers", help="JSON file (username, password, ...): also install the system after partitioning")
    ap_.add_argument("--confirmed-snapshot", help="detect JSON the user confirmed; apply refuses if the disks no longer match it")
    ap_.add_argument("--progress", action="store_true", help="print progress as JSON lines before the final result")
    for sp in (pl, ap_):
        sp.add_argument("--fs", choices=["ext4", "btrfs"], default="ext4", help="root filesystem (default ext4)")
        sp.add_argument("--encrypt", action="store_true", help="LUKS2-encrypt the system partition (login password = disk password)")
    dk = sub.add_parser("disks", help="JSON list of installable disks for the wizard")
    dk.add_argument("--snapshot")
    pl.add_argument("--json", action="store_true", help="machine-readable result for the wizard")
    for sp in (pl, ap_):
        sp.add_argument("--root-gb", type=int, help="size of the TapkicH-OS system partition in GB (default: all available)")
    args = ap.parse_args(argv)

    if args.cmd == "apply":
        return _apply(args)

    snap = _snapshot(args)
    if args.cmd == "detect":
        print(snap.to_json())
        return 0
    if args.cmd == "disks":
        print(json.dumps(disks_for_wizard(snap), indent=2))
        return 0
    if args.cmd == "plan":
        try:
            result = plan(snap, Choices(args.disk, args.mode, root_size=_gb(args.root_gb), root_fs=args.fs,
                                        encrypt=args.encrypt, overrides=frozenset(args.override)))
        except PlanError as e:
            if args.json:
                print(json.dumps({"ok": False, "kind": type(e).__name__, "code": e.code, "message": e.message,
                                  "alternatives": e.alternatives, "override": e.override}))
            else:
                print(f"STOP {e}")
            return 2
        except Exception as e:
            if not args.json:
                raise
            print(json.dumps({"ok": False, "kind": "Crash", "code": "CRASH", "alternatives": [], "override": None,
                              "message": f"The installer hit an unexpected error: {type(e).__name__}: {e}"}))
            return 3
        if args.json:
            print(json.dumps({"ok": True, "text": result.describe(), "warnings": result.warnings,
                              "boot_map": result.boot_map, "root_fs": result.root_fs, "encrypt": result.encrypt,
                              "root_gb_min": result.root_gb_min,
                              "root_gb_max": result.root_gb_max, "created": result.created,
                              "deleted": result.deleted, "windows_boot_kept": result.windows_boot_kept}))
        else:
            print(result.describe())
        return 0
    # report
    print("== boot map ==")
    lines, _, warns = boot_map(snap)
    for line in lines or ["(no Windows detected)"]:
        print(" ", line)
    for w in warns:
        print("  warn:", w)
    for disk in snap.disks:
        print(f"== {disk.id}: table={disk.table} roles={sorted(disk_roles(snap, disk.id)) or ['-']}")
        for mode in (MODE_WHOLE_DISK, MODE_FREE_SPACE):
            try:
                p = plan(snap, Choices(disk.id, mode))
                print(f"  {mode}: PLAN OK ({len(p.ops)} ops, deletes: {[o.target for o in p.ops if o.kind == 'DELETE']})")
            except PlanError as e:
                print(f"  {mode}: STOP [{e.code}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
