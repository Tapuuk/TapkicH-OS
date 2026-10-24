"""Secure Boot / anti-cheat readiness check: `tapkich secureboot status`.

Read-only. Checks what Epic says Easy Anti-Cheat Linux v2 needs (announced 2026-10-14; requirements may
change) and reports pass / fail / unknown with a plain-language fix for each.
All file reads go through `root` so tests can point at a fake /sys and /proc tree.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from typing import Optional

EFI_GLOBAL = "8be4df61-93ca-11d2-aa0d-00e098032b8c"
EFI_SECURITY_DB = "d719b2cb-3d3a-4596-a3bc-dad00e67656f"
# Microsoft's 2011 certificates expire in 2026; newer firmware carries only the 2023 replacements
# (names from Microsoft's published rollout, not verified against a real machine here).
MS_DB_MARKERS = (b"Microsoft Corporation UEFI CA", b"Microsoft Windows Production PCA",
                 b"Microsoft UEFI CA 2023", b"Windows UEFI CA 2023")
MS_KEK_MARKERS = (b"Microsoft Corporation KEK CA", b"Microsoft Corporation KEK 2K CA 2023")


@dataclass
class Check:
    name: str
    ok: Optional[bool]          # True pass, False fail, None unknown / cannot be checked from here
    detail: str
    fix: str = ""


def _read(root: str, path: str, binary: bool = False):
    try:
        with open(os.path.join(root, path.lstrip("/")), "rb" if binary else "r") as f:
            return f.read()
    except OSError:
        return None


def _efivar(root: str, name: str, guid: str):
    return _read(root, f"sys/firmware/efi/efivars/{name}-{guid}", binary=True)


def read_state(root: str = "/") -> dict:
    """Everything we look at, as plain data (also what tests build fixtures for)."""
    st: dict = {}
    st["uefi"] = os.path.isdir(os.path.join(root, "sys/firmware/efi"))
    sb = _efivar(root, "SecureBoot", EFI_GLOBAL)
    sm = _efivar(root, "SetupMode", EFI_GLOBAL)
    # an efivar is 4 attribute bytes followed by the value; an empty/short file means "cannot read it"
    st["secure_boot"] = None if sb is None or len(sb) <= 4 else sb[-1] == 1
    st["setup_mode"] = None if sm is None or len(sm) <= 4 else sm[-1] == 1
    pk = _efivar(root, "PK", EFI_GLOBAL)
    st["pk_present"] = None if pk is None and st["uefi"] is False else bool(pk and len(pk) > 4)
    db = _efivar(root, "db", EFI_SECURITY_DB) or b""
    kek = _efivar(root, "KEK", EFI_GLOBAL) or b""
    st["ms_in_db"] = any(m in db for m in MS_DB_MARKERS)
    st["ms_in_kek"] = any(m in kek for m in MS_KEK_MARKERS)
    st["tapkich_key_in_db"] = b"TapkicH-OS" in db
    tpm_major = _read(root, "sys/class/tpm/tpm0/tpm_version_major")
    st["tpm2"] = None if not os.path.isdir(os.path.join(root, "sys/class/tpm/tpm0")) else (tpm_major or "").strip() == "2"
    st["tpm_present"] = os.path.isdir(os.path.join(root, "sys/class/tpm/tpm0"))
    log = _read(root, "sys/kernel/security/tpm0/binary_bios_measurements", binary=True)
    st["measured_boot_log"] = bool(log)
    lock = _read(root, "sys/kernel/security/lockdown")
    st["lockdown"] = None
    if lock:
        for mode in ("none", "integrity", "confidentiality"):
            if f"[{mode}]" in lock:
                st["lockdown"] = mode
    kx = _read(root, "proc/sys/kernel/kexec_load_disabled")
    st["kexec_disabled"] = None if kx is None else kx.strip() == "1"
    lsm = _read(root, "sys/kernel/security/lsm")
    st["bpf_lsm"] = None if lsm is None else "bpf" in lsm.strip().split(",")
    groups = glob.glob(os.path.join(root, "sys/kernel/iommu_groups/*"))
    st["iommu_groups"] = len(groups)
    st["iommu_passthrough"] = any((_read(root, os.path.relpath(g + "/type", root)) or "").strip() == "identity" for g in groups)
    sig = _read(root, "sys/module/module/parameters/sig_enforce")
    st["module_sig_enforce"] = None if sig is None else sig.strip() in ("Y", "1")
    cmd = _read(root, "proc/cmdline") or ""
    st["cmdline"] = cmd.strip()
    return st


def evaluate(st: dict) -> list:
    c: list[Check] = []
    c.append(Check("UEFI boot", bool(st["uefi"]), "booted in UEFI mode" if st["uefi"] else "booted in legacy BIOS mode",
                   "Secure Boot needs UEFI. Switch the firmware to UEFI mode and reinstall in UEFI mode."))
    if st["secure_boot"] is None:
        c.append(Check("Secure Boot enabled", None, "cannot read the Secure Boot state (not UEFI, or not root)", ""))
    else:
        c.append(Check("Secure Boot enabled", st["secure_boot"],
                       "on" if st["secure_boot"] else ("off (firmware is in Setup Mode: keys not enrolled yet)" if st["setup_mode"] else "off"),
                       "Run `sudo tapkich secureboot setup` (our keys plus Microsoft's), or turn Secure Boot on in the firmware settings."))
    c.append(Check("Microsoft keys still trusted by the firmware", (st["ms_in_db"] or st["ms_in_kek"]) if st["uefi"] else None,
                   "Microsoft keys found: Windows and most graphics cards keep booting" if (st["ms_in_db"] or st["ms_in_kek"])
                   else "no Microsoft keys found in the firmware key database",
                   "When enrolling our own keys we always keep the Microsoft keys, otherwise Windows may stop booting."))
    c.append(Check("TPM 2.0 present", st["tpm2"] if st["tpm_present"] else False,
                   "TPM 2.0 found" if st["tpm2"] else ("TPM found but not version 2.0" if st["tpm_present"] else "no TPM found"),
                   "Turn on fTPM / PTT / TPM 2.0 in the firmware settings."))
    c.append(Check("Measured boot (TPM boot log)", st["measured_boot_log"],
                   "firmware boot log present" if st["measured_boot_log"] else "no TPM boot log",
                   "Needs a TPM; the bootloader must also record what it loads (Limine support unverified)."))
    c.append(Check("Kernel lockdown = integrity", st["lockdown"] in ("integrity", "confidentiality") if st["lockdown"] else False,
                   f"lockdown mode: {st['lockdown'] or 'not available'}",
                   "Boot with `lockdown=integrity` (TapkicH-OS does not set it: only anti-cheat asks for it)."))
    c.append(Check("kexec disabled", st["kexec_disabled"], "disabled" if st["kexec_disabled"] else "enabled or unknown",
                   "Set sysctl `kernel.kexec_load_disabled=1`."))
    c.append(Check("BPF security module loaded", st["bpf_lsm"], "loaded" if st["bpf_lsm"] else "not loaded",
                   "Add `bpf` to the kernel's `lsm=` list on the command line."))
    iommu_ok = st["iommu_groups"] > 0 and not st["iommu_passthrough"]
    c.append(Check("IOMMU on, passthrough off", iommu_ok,
                   (f"{st['iommu_groups']} IOMMU groups, passthrough off" if iommu_ok else
                    "IOMMU is off" if st["iommu_groups"] == 0 else "IOMMU passthrough is on"),
                   "Turn on IOMMU / VT-d / AMD-Vi in the firmware, and boot with `iommu.passthrough=0`."))
    c.append(Check("Kernel modules must be signed", st["module_sig_enforce"],
                   "module signatures enforced" if st["module_sig_enforce"] else "module signatures not enforced or unknown",
                   "Needs a kernel built with module signing enforced (`module.sig_enforce=1`) and signed modules."))
    c.append(Check("Kernel and modules signed by a key Epic recognizes", None,
                   "UNKNOWN: Epic's trusted list currently names Canonical, Fedora, Universal Blue, Debian, openSUSE/SUSE, "
                   "Rocky and the Microsoft shim CA. Arch's linux-zen is signed by nobody on that list (Arch ships no Secure Boot signature), and our own key is not on it (yet).",
                   "This is the one we cannot fix from the inside."))
    return c


def summarize(checks: list) -> str:
    failed = [x for x in checks if x.ok is False]
    unknown = [x for x in checks if x.ok is None]
    if not failed and unknown:
        return "Everything we can check passes, but the key Epic recognizes cannot be confirmed from this system."
    if failed:
        return f"{len(failed)} requirement(s) not met yet."
    return "All checks pass."


def render(checks: list) -> str:
    lines = ["Anti-cheat readiness (Epic EAC Linux v2, announced 2026-10-14, requirements may change)", ""]
    for x in checks:
        mark = {True: "[ ok ]", False: "[FAIL]", None: "[ ?? ]"}[x.ok]
        lines.append(f"{mark} {x.name}: {x.detail}")
        if x.ok is not True and x.fix:
            lines.append(f"         -> {x.fix}")
    lines += ["", summarize(checks)]
    return "\n".join(lines)
