#!/usr/bin/env bash
# Build the TapkicH-OS ISO. Run as root inside the Arch WSL distro:
#   wsl -d archlinux -u root -- bash /mnt/c/Users/e1024kb/TapkicH-OS/iso/build.sh [selftest]
# Work happens on the Linux filesystem (ext4); the ISO is copied to <repo>/out/.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
WORK=/root/tapkich-os-build
[[ ${1:-} == selftest ]] && export SELFTEST=1
[[ ${1:-} == autoinstall ]] && export SELFTEST=2

rm -rf "$WORK/profile" "$WORK/work"
mkdir -p "$WORK" "$REPO/out"
cp -a /usr/share/archiso/configs/releng "$WORK/profile"
export AUR_REPO=$WORK/aur-repo
bash "$REPO/iso/build-aur.sh"                  # paru from the AUR into a local repo shipped on the ISO
bash "$REPO/iso/customize.sh" "$WORK/profile" "$REPO"

mkarchiso -v -w "$WORK/work" -o "$WORK/out" "$WORK/profile"
iso=$(ls -t "$WORK"/out/*.iso | head -1)
name=$(basename "$iso" .iso)
[[ ${SELFTEST:-0} == 1 ]] && name="${name}-selftest"
[[ ${SELFTEST:-0} == 2 ]] && name="${name}-autoinstall"
# Keep the ISO on the Linux filesystem (copying 1.6 GB over /mnt/c can run out of memory in WSL).
mkdir -p "$WORK/iso"
cp "$iso" "$WORK/iso/$name.iso"
echo "ISO: $WORK/iso/$name.iso"
