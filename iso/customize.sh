#!/usr/bin/env bash
# Turn a stock archiso "releng" profile into the TapkicH-OS profile.
# usage: customize.sh <profile_dir> <repo_root>      (SELFTEST=1 adds a headless boot self-test)
set -euo pipefail
P=$1
REPO=$2

# 1. Only official Arch repositories. (No CachyOS repo, keyring or mirrorlist anywhere in the product: their repository
#    policy forbids shipping them in another distribution.)
cp "$P/pacman.conf" "$P/airootfs/etc/pacman.conf"

# 2. Kernel: swap linux -> linux-zen (official Arch package); add the installer's dependencies
sed -i '/^linux$/d;/^nmap$/d' "$P/packages.x86_64"   # nmap: not GPL-compatible licence, not needed
cat >> "$P/packages.x86_64" <<'EOF'
linux-zen
python
ntfs-3g
ntfsprogs
mtools
parted
gptfdisk
limine
EOF

# 3. Boot entries use the linux-zen image names
grep -rlE 'vmlinuz-linux|initramfs-linux' "$P"/{efiboot,grub,syslinux} | xargs sed -i \
  -e 's/vmlinuz-linux/vmlinuz-linux-zen/g' \
  -e 's/initramfs-linux\.img/initramfs-linux-zen.img/g'

# 3b. paru is not in the official repos: it is built from the AUR by iso/build-aur.sh into a local repo that is
#     shipped on the ISO (/opt/tapkich-repo) and used only while installing.
if [[ -d ${AUR_REPO:-/nonexistent} ]]; then
  mkdir -p "$P/airootfs/opt/tapkich-repo"
  cp "$AUR_REPO"/* "$P/airootfs/opt/tapkich-repo/"
fi

# 4. Branding
grep -rlI 'Arch Linux' "$P"/{efiboot,grub,syslinux} "$P/airootfs/etc/motd" | xargs sed -i 's/Arch Linux/TapkicH-OS/g'
sed -i \
  -e 's/^iso_name=.*/iso_name="tapkich-os"/' \
  -e 's/^iso_label=.*/iso_label="TAPKICH_$(date --date="@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%Y%m)"/' \
  -e 's/^iso_publisher=.*/iso_publisher="TapkicH-OS"/' \
  -e 's/^iso_application=.*/iso_application="TapkicH-OS installer"/' \
  "$P/profiledef.sh"
echo "tapkich-os" > "$P/airootfs/etc/hostname"


# 4b. No Arch welcome text or artwork: empty login message (the installer starts right away), plain violet menu
#     background instead of the Arch splash picture, no timing line after the installer
: > "$P/airootfs/etc/motd"
echo "console-mode 0" >> "$P/efiboot/loader/loader.conf"      # smallest text mode: redraws fast (VirtualBox's UEFI console is slow in big modes)
printf '%s\n\n' 'TapkicH-OS \r (\l)' > "$P/airootfs/etc/issue"      # login prompt text (was "Arch Linux"); \r \l are agetty codes
# 4c. The boot menu never shows: the ISO boots straight into our installer (hold a key at power-on to get the
#     systemd-boot menu back). No speech / memtest entries. BIOS (syslinux): 0.1 s timeout instead of 15 s.
sed -i -e 's/^timeout .*/timeout 0/' -e 's/^beep .*/beep off/' "$P/efiboot/loader/loader.conf"
rm -f "$P"/efiboot/loader/entries/02-* "$P"/efiboot/loader/entries/03-*
sed -i 's/^TIMEOUT .*/TIMEOUT 1/' "$P/syslinux/archiso_sys.cfg"
# systemd prints "Welcome to <PRETTY_NAME>!" while booting: make that our name, not "Arch Linux"
cat > "$P/airootfs/etc/os-release" <<'OSR'
NAME="TapkicH-OS"
PRETTY_NAME="TapkicH-OS"
ID=tapkich
ID_LIKE=arch
BUILD_ID=rolling
ANSI_COLOR="38;2;124;58;237"
OSR
# 4d. Quiet boot (normal builds): no kernel / initramfs text, no "Welcome to ..." line; the screen goes straight to the installer
if [[ ${SELFTEST:-0} == 0 ]]; then
  sed -i 's#^\(options .*\)$#\1 quiet loglevel=3 systemd.show_status=false vt.global_cursor_default=0#' "$P"/efiboot/loader/entries/*.conf
  sed -i 's#^\(APPEND .*\)$#\1 quiet loglevel=3 systemd.show_status=false#' "$P"/syslinux/archiso_sys-linux.cfg
fi
python3 - "$P/syslinux/splash.png" <<'PY'
import struct, sys, zlib
w, h = 640, 480
rows = b"".join(b"\x00" + bytes((11, 7, 18)) * w for _ in range(h))     # near-black violet
def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
open(sys.argv[1], "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                              + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
PY
# 5. Installer (detect/plan) on the live ISO
mkdir -p "$P/airootfs/opt/tapkich-os" "$P/airootfs/usr/local/bin"
cp -r "$REPO/installer/tapkich_os" "$P/airootfs/opt/tapkich-os/"
find "$P/airootfs/opt/tapkich-os" -name __pycache__ -prune -exec rm -rf {} +
cat > "$P/airootfs/usr/local/bin/tapkich-installer" <<'EOF'
#!/bin/sh
exec env PYTHONPATH=/opt/tapkich-os python3 -m tapkich_os "$@"
EOF
cat > "$P/airootfs/usr/local/bin/tapkich" <<'EOF'
#!/bin/sh
exec env PYTHONPATH=/opt/tapkich-os python3 -m tapkich_os.command "$@"
EOF
sed -i '/^file_permissions=(/a\  ["/usr/local/bin/tapkich-installer"]="0:0:755"\n  ["/usr/local/bin/tapkich"]="0:0:755"' "$P/profiledef.sh"

# 5b. Guided installer wizard (gum) + theme palette; starts automatically on tty1
sed -i '/^python$/a gum\njq' "$P/packages.x86_64"
cp "$REPO/iso/overlay/usr/local/bin/tapkich-setup" "$P/airootfs/usr/local/bin/tapkich-setup"
sed -i '/^file_permissions=(/a\  ["/usr/local/bin/tapkich-setup"]="0:0:755"' "$P/profiledef.sh"
mkdir -p "$P/airootfs/usr/share/tapkich-os/theme"
cp "$REPO/iso/overlay/usr/share/tapkich-os/tui.sh" "$REPO/iso/overlay/usr/share/tapkich-os/keyboards.tsv" \
   "$REPO/iso/overlay/usr/share/tapkich-os/languages.tsv" "$P/airootfs/usr/share/tapkich-os/"
cp "$REPO/theme/tapkich-purple/colors.toml" "$P/airootfs/usr/share/tapkich-os/theme/colors.toml"
python3 "$REPO/iso/make-palette.py" "$REPO/theme/tapkich-purple/colors.toml" > "$P/airootfs/usr/share/tapkich-os/palette.sh"
# 5c. The installer kiosk: Hyprland shows the installer in one glass terminal over the dusk picture
#     (iso/overlay/usr/share/tapkich-os/kiosk). Without working graphics the installer runs on the plain console.
printf '%s\n' hyprland foot swaybg ttf-jetbrains-mono >> "$P/packages.x86_64"
mkdir -p "$P/airootfs/usr/share/tapkich-os/kiosk"
cp "$REPO"/iso/overlay/usr/share/tapkich-os/kiosk/* "$P/airootfs/usr/share/tapkich-os/kiosk/"
cp "$REPO/iso/overlay/usr/local/bin/tapkich-kiosk" "$REPO/iso/overlay/usr/local/bin/tapkich-kiosk-term" "$P/airootfs/usr/local/bin/"
sed -i '/^file_permissions=(/a\  ["/usr/local/bin/tapkich-kiosk"]="0:0:755"\n  ["/usr/local/bin/tapkich-kiosk-term"]="0:0:755"' "$P/profiledef.sh"
if [[ ${SELFTEST:-0} == 0 ]]; then
  cat >> "$P/airootfs/root/.zlogin" <<'EOF'

# TapkicH-OS: start the guided installer on the first console (in the kiosk; plain console if graphics fail)
unset REPORTTIME
if [[ $(tty) == /dev/tty1 && -x /usr/local/bin/tapkich-setup ]]; then
  tapkich-kiosk || tapkich-setup
fi
EOF
fi

# 6b. SELFTEST=2: unattended install onto /dev/vda (the VM's incident disks), then power off
if [[ ${SELFTEST:-0} == 2 ]]; then
  sed -i 's#^\(options .*\)$#\1 console=tty0 console=ttyS0,115200#' "$P"/efiboot/loader/entries/*.conf
  cat > "$P/airootfs/usr/share/tapkich-os/test-answers.json" <<'EOF'
{"keyboard":"us","xkb":"us,ua","locale":"uk_UA","font":"cyr-sun16","fs":"ext4","encrypt":false,"username":"kai","password":"test-only-pw","hostname":"tapkich-vm","timezone":"UTC",
 "disk":"/dev/vda","mode":"free_space","overrides":[],"selftest":true}
EOF
  cat > "$P/airootfs/etc/systemd/system/tapkich-selftest.service" <<'EOF'
[Unit]
Description=TapkicH-OS unattended install self-test
Wants=network-online.target
After=network-online.target pacman-init.service
[Service]
Type=oneshot
StandardOutput=journal+console
StandardError=journal+console
Environment=TERM=xterm-256color TAPKICH_DRY=0 TAPKICH_ANSWERS=/usr/share/tapkich-os/test-answers.json
# Dev loop: if the host shares the repo over 9p, use that code instead of the copy baked into the ISO
ExecStartPre=-/bin/sh -c 'mkdir -p /mnt/dev && mount -t 9p -o trans=virtio,version=9p2000.L tapkichdev /mnt/dev && mount --bind /mnt/dev/installer/tapkich_os /opt/tapkich-os/tapkich_os && mount --bind /mnt/dev/iso/overlay/usr/local/bin/tapkich-setup /usr/local/bin/tapkich-setup && mount --bind /mnt/dev/iso/overlay/usr/share/tapkich-os/tui.sh /usr/share/tapkich-os/tui.sh && mount --bind /mnt/dev/iso/overlay/usr/share/tapkich-os/keyboards.tsv /usr/share/tapkich-os/keyboards.tsv && mount --bind /mnt/dev/iso/overlay/usr/share/tapkich-os/languages.tsv /usr/share/tapkich-os/languages.tsv && { [ ! -f /mnt/dev/answers.json ] || mount --bind /mnt/dev/answers.json /usr/share/tapkich-os/test-answers.json; } && echo TAPKICH-USING-9P-DEV-CODE'
ExecStart=/bin/sh -c 'echo TAPKICH-AUTOINSTALL-BEGIN; tapkich-installer report; tapkich-setup; echo TAPKICH-AUTOINSTALL-END; systemctl poweroff'
TimeoutStartSec=infinity
[Install]
WantedBy=multi-user.target
EOF
  mkdir -p "$P/airootfs/etc/systemd/system/multi-user.target.wants"
  ln -sf ../tapkich-selftest.service "$P/airootfs/etc/systemd/system/multi-user.target.wants/tapkich-selftest.service"
fi

# 6. Optional headless self-test (test builds only): serial console + report + poweroff
if [[ ${SELFTEST:-0} == 1 ]]; then
  sed -i 's#^\(options .*\)$#\1 console=tty0 console=ttyS0,115200#' "$P"/efiboot/loader/entries/*.conf
  cat > "$P/airootfs/etc/systemd/system/tapkich-selftest.service" <<'EOF'
[Unit]
Description=TapkicH-OS headless self-test
After=multi-user.target
[Service]
Type=oneshot
StandardOutput=journal+console
StandardError=journal+console
ExecStart=/bin/sh -c 'echo TAPKICH-SELFTEST-BEGIN; uname -r; echo "cachyos refs in pacman.conf: $(grep -ci cachyos /etc/pacman.conf)"; pacman -Q linux-zen; tapkich-installer report; echo TAPKICH-SELFTEST-END; systemctl poweroff'
[Install]
WantedBy=multi-user.target
EOF
  mkdir -p "$P/airootfs/etc/systemd/system/multi-user.target.wants"
  ln -sf ../tapkich-selftest.service "$P/airootfs/etc/systemd/system/multi-user.target.wants/tapkich-selftest.service"
fi
