#!/usr/bin/env bash
# Build the AUR packages we ship from source into a small local pacman repo on the ISO:
#   paru (AUR helper), Caelestia shell with every AUR package it needs, and the user's apps from the AUR.
# Run as root in the Arch build host:  bash iso/build-aur.sh        -> /root/tapkich-os-build/aur-repo
# Why: these are not in the official Arch repos, and we use no third-party binary repo. The ISO carries the built
# packages; the installer installs them from there, so nothing has to compile on the user's machine.
# Re-running is cheap: a package already built is kept unless FORCE=1.
set -euo pipefail
OUT=${AUR_REPO:-/root/tapkich-os-build/aur-repo}
SRC=/var/tmp/tapkich-aur-src          # not under /root: the unprivileged build user must be able to enter it
mkdir -p "$OUT" "$SRC"

# In dependency order: a package's AUR dependencies come before it (checked with the AUR RPC, 2026-10-18).
PKGS=(
  paru
  python-materialyoucolor libcava qt6-m3shapes-git quickshell-git ttf-rubik-vf
  caelestia-cli caelestia-shell
  darkly-bin qtengine papirus-folders pwvucontrol
  # the user's apps: built here so the install only copies them (Proton-GE is not here: it would push the ISO over
  # GitHub's 2 GiB release limit, so the installer gets it from the AUR)
  waterfox-bin omasnap-bin python-inputs python-steam protonup-qt localsend-bin
  # Gaming Mode: ChimeraOS's gamescope session with Steam's gamepad UI (both MIT)
  gamescope-session-git gamescope-session-steam-git
)

# PKGS_ONLY="paru libcava" builds just those (testing); the rest of the script then works on what is in $OUT.
if [[ -n ${PKGS_ONLY:-} ]]; then read -ra PKGS <<< "$PKGS_ONLY"; fi

# Every AUR package is built in a CLEAN CHROOT (Arch's devtools: mkarchroot once, makechrootpkg per package), the way
# Arch's own packagers and CachyOS/EndeavourOS do it. An unreviewed PKGBUILD then runs inside a throw-away copy of the
# chroot and cannot see this host: not /root/tapkich-signing (our package-signing key), and the host needs no sudo
# rule for the build user. Sources are downloaded and their PGP signatures checked on the host by `builder` (no
# sudo, no access to /root), exactly as makechrootpkg -U does it; the chroot build then skips the repeated check.
# not under /root: `builder` must be able to read the chroot's makepkg.conf (and /root stays closed to it)
CHROOT=${AUR_CHROOT:-/var/lib/tapkich-os-chroot}
pacman -S --noconfirm --needed base-devel git devtools >/dev/null
id builder >/dev/null 2>&1 || useradd -m builder
rm -f /etc/sudoers.d/tapkich-builder      # older builds gave `builder` a root-equivalent pacman rule: gone for good
if [[ ! -d $CHROOT/root ]]; then
  mkdir -p "$CHROOT"
  # only Arch's own repositories: a TapkicH-OS build host also lists [tapkich], whose key and Include file the chroot lacks
  awk '/^\[/ { keep = ($0 ~ /^\[(options|core|extra|multilib)\]$/) } keep && !/^NoExtract/' /etc/pacman.conf > "$CHROOT/pacman.conf"
  mkarchroot -C "$CHROOT/pacman.conf" -M /etc/makepkg.conf "$CHROOT/root" base-devel
fi
arch-nspawn "$CHROOT/root" pacman -Syu --noconfirm >/dev/null

built() { ls "$OUT"/"$1"-[0-9r]*.pkg.tar.zst >/dev/null 2>&1; }

# -I arguments for package $1: the AUR packages already in $OUT that it (transitively) depends on, found through the
# names and Provides of the built files (quickshell-git provides quickshell). Prints "-I file" pairs, one per line.
chroot_installs() {
  local p=$1 f n d line
  declare -A owner=() deps=() chosen=()
  for f in "$OUT"/*.pkg.tar.zst; do
    [[ -e $f && $f != *-debug-* ]] || continue
    n=$(pacman -Qip "$f" | sed -n 's/^Name *: //p')
    [[ $n == "$p" || $n == tapkich-os ]] && continue
    deps[$f]=$(pacman -Qip "$f" | sed -n 's/^Depends On *: //p')
    for d in $n $(pacman -Qip "$f" | sed -n 's/^Provides *: //p'); do
      [[ $d == None ]] || owner[${d%%[<>=]*}]=$f
    done
  done
  local queue=($(sed -n 's/^\s*\(make\|check\)\?depends = //p' "$SRC/$p/.SRCINFO" | sed 's/[<>=:].*//'))
  while ((${#queue[@]})); do
    d=${queue[0]}; queue=("${queue[@]:1}")
    f=${owner[$d]:-}
    [[ -n $f && -z ${chosen[$f]:-} ]] || continue
    chosen[$f]=1
    for line in ${deps[$f]}; do [[ $line == None ]] || queue+=("${line%%[<>=:]*}"); done
  done
  for f in "${!chosen[@]}"; do echo "-I"; echo "$f"; done
}

for p in "${PKGS[@]}"; do
  if built "$p" && [[ ${FORCE:-0} != 1 ]]; then
    echo "== $p: already built"
  else
    echo "== $p: building"
    rm -rf "${SRC:?}/$p"
    git clone -q --depth 1 "https://aur.archlinux.org/$p.git" "$SRC/$p"
    chown -R builder:builder "$SRC/$p"
    # signed sources: import exactly the keys the PKGBUILD itself names as valid (makepkg still checks them)
    keys=$(sed -n 's/^\s*validpgpkeys = //p' "$SRC/$p/.SRCINFO")
    if [[ -n $keys ]]; then
      runuser -u builder -- gpg --batch --keyserver hkps://keyserver.ubuntu.com --recv-keys $keys
    fi
    # AUR dependencies we built earlier go into the chroot with -I (nothing is installed on this host)
    mapfile -t inst < <(chroot_installs "$p")
    # -c: fresh copy of the chroot for every package; -U builder: sources fetched and verified as builder;
    # --nocheck as before (official dependencies are installed by makepkg -s inside the chroot)
    (cd "$SRC/$p" && makechrootpkg -c -r "$CHROOT" -U builder "${inst[@]}" -- --nocheck)
    rm -f "$OUT"/"$p"-[0-9r]*.pkg.tar.zst
    for f in "$SRC/$p"/*.pkg.tar.zst; do
      [[ $f == *-debug-* ]] || cp "$f" "$OUT"/
    done
  fi
done

# our own files as the tapkich-os package (rebuilt every time: it is this checkout's code)
echo "== tapkich-os: building"
bash "$(dirname "$0")/../packaging/build-tapkich-os.sh" "$OUT"
rm -f "$OUT"/paru-debug-*.pkg.tar.zst

rm -f "$OUT"/tapkich-local.db* "$OUT"/tapkich-local.files*
repo-add "$OUT/tapkich-local.db.tar.gz" "$OUT"/*.pkg.tar.zst >/dev/null
ls -la "$OUT"
