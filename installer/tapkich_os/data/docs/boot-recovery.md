# TapkicH-OS: when it does not start

Use UUIDs or partition labels, never `/dev/nvme0n1p1`-style names: NVMe disks can swap numbers between starts.

## 1. From the boot menu

1. Pick **TapkicH-OS (last known good)**: the kernel and initramfs that last started fine, with their own hashes.
2. Or **TapkicH-OS (recovery)**: the fallback initramfs with every driver.
3. If Limine says a hash does not match and waits: press **Y** to start anyway (works while Secure Boot is off).
4. With Secure Boot off, **E** edits an entry for this one start (for example to add `systemd.unit=rescue.target`).

Once started: `sudo tapkich-boot-update`, then `sudo tapkich-boot-update --check`.

## 2. Secure Boot on and nothing starts

Turn Secure Boot off in the firmware settings and try step 1 again. Turn it back on only after
`sudo tapkich-secureboot-check` says it is safe.

## 3. From a USB stick (the TapkicH-OS ISO or any Arch ISO)

Find the partitions (on TapkicH-OS installs they are labelled `TAPKICH_EFI` and `TAPKICH_ROOT`):

    blkid -o list
    lsblk -o NAME,SIZE,FSTYPE,PARTLABEL,UUID

Note the UUID of the `vfat` partition labelled `TAPKICH_EFI` (ESP) and of the root (`TAPKICH_ROOT`). The root's UUID
is also the `root=UUID=...` in the boot menu. Then:

    mount /dev/disk/by-uuid/<ROOT-UUID> /mnt                 # btrfs: mount -o subvol=@ ...
    mount /dev/disk/by-uuid/<ESP-UUID> /mnt/boot
    arch-chroot /mnt
    tapkich-boot-update --force
    tapkich-boot-update --check
    exit
    umount -R /mnt

Encrypted root: first `cryptsetup open /dev/disk/by-uuid/<LUKS-UUID> cryptroot`, then mount `/dev/mapper/cryptroot`.
(Instead of `/dev/disk/by-uuid/...` you can also use `/dev/disk/by-partlabel/TAPKICH_ROOT` and `.../TAPKICH_EFI`.)

Always mount the ESP before running `mkinitcpio` in a chroot: without it the images land on the root partition's
empty `/boot`, which is not what starts (`tapkich-boot-update` refuses and says so).

## 4. Last resort, Secure Boot off

Inside the chroot of step 3, turn the checks off and put stock Limine back:

    sed -i 's/^VERIFY_HASHES=.*/VERIFY_HASHES=no/' /etc/tapkich-os/boot.conf
    tapkich-boot-update --force

Without the tool (very old install): `cp /usr/share/limine/BOOTX64.EFI /boot/EFI/limine/limine_x64.efi` and the same
to `/boot/EFI/BOOT/BOOTX64.EFI` (nothing enrolled: Limine no longer checks its config), then delete everything after
`#` on the `path:` and `module_path:` lines of both `/boot/EFI/*/limine.conf`.
