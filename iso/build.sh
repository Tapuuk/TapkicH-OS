#!/usr/bin/env bash
# Build the TapkicH-OS ISO. Run as root inside the Arch WSL distro:
#   wsl -d archlinux -u root -- bash /mnt/c/Users/e1024kb/TapkicH-OS/iso/build.sh [selftest]
# Work happens on the Linux filesystem (ext4); the ISO is copied to <repo>/out/.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
WORK=/root/tapkich-os-build
[[ ${1:-} == selftest ]] && export SELFTEST=1
[[ ${1:-} == autoinstall ]] && export SELFTEST=2

export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="*"   # the checkout is owned by the Windows user
# TAPKICH_RELEASE=1: a release ISO is built only from a clean tree at the pushed origin/main commit
if [[ ${TAPKICH_RELEASE:-0} == 1 ]]; then
  [[ ${SELFTEST:-0} == 0 ]] || { echo "release build refused: a selftest/autoinstall ISO is a test image (it installs by itself)"; exit 1; }
  [[ -z $(git -C "$REPO" status --porcelain -- installer packaging iso theme LICENSE) ]] || { echo "release build refused: uncommitted changes in installer packaging iso theme LICENSE"; exit 1; }
  git -C "$REPO" fetch -q origin main 2>/dev/null || true
  [[ $(git -C "$REPO" rev-parse HEAD) == $(git -C "$REPO" rev-parse origin/main) ]] || { echo "release build refused: HEAD is not origin/main (push the commit first)"; exit 1; }
fi

# Guard: the build works only inside $WORK. It must never touch this computer's own boot files: on a TapkicH-OS build
# host a rebuilt /boot/initramfs-* without new hashes in limine.conf stops the next start (seen 2026-11-03). So the
# host's boot tools are replaced, for this build, by stubs that stop it (inside a chroot the real ones run: the stub
# directory does not exist there), and /boot is compared before and after.
case $(realpath -m "$WORK") in
  /|/boot|/boot/*|/efi|/efi/*|/usr|/usr/*|/etc|/etc/*) echo "build refused: work directory $WORK is a system path"; exit 1 ;;
esac
GUARD=$WORK/guard-bin
rm -rf "$GUARD"; mkdir -p "$GUARD"
for t in mkinitcpio bootctl kernel-install limine sbctl efibootmgr grub-install dracut tapkich-boot-update; do
  printf '%s\n' '#!/bin/sh' \
    "echo \"iso/build.sh: refused to run '$t \$*' on the build host: the ISO build never touches this computer's boot files\" >&2" \
    'exit 97' > "$GUARD/$t"
  chmod 755 "$GUARD/$t"
done
export PATH="$GUARD:$PATH"
boot_state() { if mountpoint -q /boot; then find /boot -xdev -printf '%P %s %T@\n' | sort | sha256sum; else echo none; fi; }
BOOT_BEFORE=$(boot_state)
boot_guard() {
  local rc=$?
  trap - EXIT
  if [[ $(boot_state) != "$BOOT_BEFORE" ]]; then
    # Something else on this computer may change /boot meanwhile (tapkich-boot-good.timer, 3 minutes after a start):
    # what matters is that the boot menu is still in step. The real command, not the stub on PATH.
    if [[ -x /usr/bin/tapkich-boot-update ]] && /usr/bin/tapkich-boot-update --check >/dev/null 2>&1; then
      echo "note: /boot changed during the build, but not by it (no boot tool ran from it), and the boot menu is in step"
    else
      echo "!!! /boot on this computer CHANGED during the ISO build and the boot menu may be out of step. Before restarting:"
      echo "!!!   sudo tapkich-boot-update --check     (and without --check to put the hashes back in step)"
      rc=1
    fi
  fi
  exit $rc
}
trap boot_guard EXIT

rm -rf "$WORK/profile" "$WORK/work"
mkdir -p "$WORK" "$REPO/out"
cp -a /usr/share/archiso/configs/releng "$WORK/profile"
export AUR_REPO=$WORK/aur-repo
bash "$REPO/iso/build-aur.sh"                  # paru from the AUR into a local repo shipped on the ISO
bash "$REPO/iso/customize.sh" "$WORK/profile" "$REPO"

mkarchiso -v -w "$WORK/work" -o "$WORK/out" "$WORK/profile"
# The ISO starts only its own boot loader (archiso's systemd-boot) with its own entries. No Limine and no limine.conf
# on it: a Limine started from a USB stick (Ventoy) that cannot tell which volume it came from scans every disk for a
# limine.conf, so it could pick up the installed system's config on the NVMe.
efi_copy=$WORK/check-bootx64.efi
mcopy -o -i "$WORK/work/efiboot.img" ::/EFI/BOOT/BOOTX64.EFI "$efi_copy"
if grep -aq '++CONFIG_B2SUM_SIGNATURE++' "$efi_copy" || ! grep -aq 'systemd-boot' "$efi_copy" \
   || mdir -/ -b -i "$WORK/work/efiboot.img" :: | grep -qi 'limine' || find "$WORK/work/iso" -iname 'limine*' | grep -q .; then
  echo "build refused: the ISO's EFI boot loader is not archiso's systemd-boot, or it carries Limine files"; exit 1
fi
rm -f "$efi_copy"
iso=$(ls -t "$WORK"/out/*.iso | head -1)
name=$(basename "$iso" .iso)
[[ ${SELFTEST:-0} == 1 ]] && name="${name}-selftest"
[[ ${SELFTEST:-0} == 2 ]] && name="${name}-autoinstall"
# Keep the ISO on the Linux filesystem (copying 1.6 GB over /mnt/c can run out of memory in WSL).
mkdir -p "$WORK/iso"
cp "$iso" "$WORK/iso/$name.iso"
cp "$WORK/profile/airootfs/usr/share/tapkich-os/build-commit" "$WORK/iso/$name.commit"       # the stamp inside the ISO (incl. -dirty), read by tools/publish-iso.sh
echo "ISO: $WORK/iso/$name.iso"
