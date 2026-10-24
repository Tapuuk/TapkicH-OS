#!/usr/bin/env bash
# Build the tapkich-os package from this repository's working tree into <outdir>.
#   bash packaging/build-tapkich-os.sh <outdir>          (root, Arch build host; makepkg runs as the user "builder")
# Version: <commit date YYYYMMDD.HHMM>.g<commit>, plus .d<time> when the tree has uncommitted changes, so every build sorts
# after the one before.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:?usage: build-tapkich-os.sh <outdir>}
mkdir -p "$OUT"
cd "$REPO"
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="*"   # the checkout is owned by the Windows user
ver="$(git log -1 --format=%cd --date=format-local:%Y%m%d.%H%M).g$(git rev-parse --short HEAD)"   # date of the commit: newer commits sort newer (also after the history rewrite of 2026-10-24)
[[ -n $(git status --porcelain -- installer packaging LICENSE 2>/dev/null) ]] && ver="$ver.d$(date +%Y%m%d%H%M)"
WORK=/var/tmp/tapkich-os-pkg
rm -rf "$WORK"; mkdir -p "$WORK/src"
# a clean copy the build user can read (no __pycache__)
cp -r "$REPO/installer" "$REPO/packaging" "$REPO/LICENSE" "$WORK/src/"
find "$WORK/src" -name __pycache__ -prune -exec rm -rf {} +
cp "$REPO/packaging/tapkich-os/PKGBUILD" "$REPO/packaging/tapkich-os/tapkich-os.install" "$WORK/"
id builder >/dev/null 2>&1 || useradd -m builder
chown -R builder:builder "$WORK"
runuser -u builder -- env TAPKICH_SRC="$WORK/src" TAPKICH_PKGVER="$ver" bash -c "cd '$WORK' && makepkg -f --nodeps >/dev/null"
rm -f "$OUT"/tapkich-os-[0-9r]*.pkg.tar.zst
cp "$WORK"/tapkich-os-*.pkg.tar.zst "$OUT/"
ls "$OUT"/tapkich-os-*.pkg.tar.zst
