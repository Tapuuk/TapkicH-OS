# TapkicH-OS boot locks

Limine starts TapkicH-OS from the EFI partition (mounted at `/boot`). Three locks can sit on that chain. Each one
protects something, and each one goes stale when what it protects changes. `tapkich-boot-update` puts all three back
in step, in the right order, whenever something changes.

| Lock | What it is | Where it lives | Goes stale when |
|---|---|---|---|
| 1. Path hashes | `#<blake2b>` after every kernel and initramfs path in `limine.conf` | both `limine.conf` files | the kernel or an initramfs is rebuilt (pacman, `mkinitcpio -P`) |
| 2. Enrolled config hash | the blake2b of `limine.conf`, written into Limine's EFI file (`limine enroll-config`) | `EFI/limine/limine_x64.efi`, `EFI/BOOT/BOOTX64.EFI` | `limine.conf` changes (also because lock 1 was updated) |
| 3. Secure Boot signatures | sbctl's signature with our key | both Limine EFI files, the kernel | the file changes after signing (also because lock 2 was enrolled: sbctl then says "incorrect digest") |

So the order is always: hashes, then enroll, then sign. Limine is copied fresh from the package each time and
enrolled before it is signed. A kernel with a stale signature has that signature stripped and is signed again.

## What a stale lock does

- Lock 1 with Secure Boot **on**: Limine refuses the entry ("Blake2b hash ... does not match"). With Secure Boot
  **off**: Limine warns and waits; press **Y** to start anyway (`hash_mismatch_panic: no`).
- Lock 2: Limine refuses the config (checksum mismatch). Hand edits of the ESP's `limine.conf` cause this, so edit
  `/etc/tapkich-os/limine.conf` instead and run `sudo tapkich-boot-update`.
- Lock 3: with Secure Boot on, the firmware refuses to start Limine. With it off, nothing happens.

## Settings: `/etc/tapkich-os/boot.conf`

- `VERIFY_HASHES=auto|yes|no` (locks 1 and 2). `auto` (the default) means yes while Secure Boot is on, no while it
  is off. **The trade-off:** with `yes` a changed kernel or initramfs is noticed, which is what makes Secure Boot
  worth having, but a rebuild that was not followed by an update blocks the start. With `no` nothing can go stale
  and the editor works, but a signed Limine starts any kernel on the ESP. Without Secure Boot that is no real loss:
  anyone with the disk can change anything anyway.
- `BOOT_EDITOR=auto|on|off`: the **E** key in the boot menu. `auto` means on while Secure Boot is off. With Secure
  Boot on and a config hash enrolled, Limine always turns it off (by design).

After turning Secure Boot on, the first start (after about 3 minutes) switches the hashes on by itself (`auto`).

## One source, two copies

`/etc/tapkich-os/limine.conf` is the menu (no hashes in it). Both `limine.conf` files on the ESP are generated from
it and are always identical: `EFI/limine/` is read by `limine_x64.efi` (the firmware's TapkicH-OS entry),
`EFI/BOOT/` by `BOOTX64.EFI` (the firmware's fallback path).

## When it runs

- after every pacman transaction (`zzzz-tapkich-boot-update.hook`, after `90-mkinitcpio-install.hook` and sbctl's hook);
- after every mkinitcpio image, so a manual `mkinitcpio -P` is covered (`/usr/lib/initcpio/post/tapkich-boot-update`);
- after `kernel-install add|remove`;
- at every shutdown (`tapkich-boot-update-shutdown.service`): catches anything else;
- about 3 minutes after every start (`tapkich-boot-good.timer`): this start's kernel and initramfs become the
  **last known good** copy (`/boot/tapkich-good/<version>/`, own hashes; a rebuild never touches it);
- when the tapkich-os package is installed or upgraded (a full redo and check).

It is all or nothing: every new file is built and checked beside its target first, then moved in, one config and its
Limine file at a time. On any error nothing is moved and the command exits non-zero.

## Commands

- `sudo tapkich-boot-update`: bring everything in step (`--force` redoes everything).
- `sudo tapkich-boot-update --check`: only report; changes nothing; exit 1 when something is out of step.
- `sudo tapkich-secureboot-check`: before turning Secure Boot on. Checks our key is in the firmware, every EFI file
  and the kernel are signed and unchanged since, `sbctl verify` is clean, and locks 1 and 2 match. Exit 0 means it is
  safe to switch on.

If a start fails anyway: `boot-recovery.md` in this folder.
