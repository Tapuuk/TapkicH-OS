#!/usr/bin/env bash
# One-time setup of the Arch (WSL) build host for TapkicH-OS. Official Arch repositories only.
# (Older versions of this script also set up the CachyOS keyring and mirrorlists: no longer used or wanted.)
set -euo pipefail
pacman -Syu --noconfirm --needed archiso qemu-base edk2-ovmf git base-devel rust \
  dosfstools ntfs-3g ntfsprogs mtools parted gptfdisk jq gum python-pytest btrfs-progs cryptsetup xkeyboard-config
echo "build host ready"
