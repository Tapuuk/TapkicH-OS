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
  [[ -z $(git -C "$REPO" status --porcelain -- installer packaging iso theme LICENSE) ]] || { echo "release build refused: uncommitted changes in installer packaging iso theme LICENSE"; exit 1; }
  git -C "$REPO" fetch -q origin main 2>/dev/null || true
  [[ $(git -C "$REPO" rev-parse HEAD) == $(git -C "$REPO" rev-parse origin/main) ]] || { echo "release build refused: HEAD is not origin/main (push the commit first)"; exit 1; }
fi

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
git -C "$REPO" rev-parse HEAD > "$WORK/iso/$name.commit"       # read by tools/publish-iso.sh
echo "ISO: $WORK/iso/$name.iso"
