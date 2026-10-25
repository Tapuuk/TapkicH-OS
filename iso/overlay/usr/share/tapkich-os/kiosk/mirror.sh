#!/bin/bash
# TapkicH-OS live ISO kiosk: every screen shows what the first one shows (the installer and the picture on all of them).
# Run (as `bash mirror.sh`) when the kiosk starts and each time a monitor is plugged in. The first screen that is not
# itself a mirror stays the main one; every other screen is set to mirror it. Idempotent, and a no-op with one screen.
# (A generic rule in hyprland.lua cannot do this: a rule for every output would also tell the main screen to mirror.)
mons=$(hyprctl monitors -j 2>/dev/null) || exit 0
main=$(jq -r '[.[] | select((.mirrorOf // "none") == "none")][0].name // empty' <<<"$mons")
[ -n "$main" ] || exit 0
jq -r --arg main "$main" '.[] | select(.name != $main and (.mirrorOf // "none") == "none") | .name' <<<"$mons" |
while read -r name; do
    hyprctl eval "hl.monitor({ output = \"$name\", mode = \"preferred\", position = \"auto\", scale = 1, mirror = \"$main\" })" >/dev/null 2>&1 || true
done
# For a report from real hardware: what each screen ended up as, and whether the kernel really switched it on
# (a mirror on a second GPU that cannot copy the picture stays dark although Hyprland lists it as a mirror).
{
    echo "screens after mirroring (main: $main):"
    for c in "${TAPKICH_SYSFS:-/sys}"/class/drm/card*-*; do
        [[ -e $c/status ]] && echo "  ${c##*/}: $(cat "$c/status") $(cat "$c/enabled" 2>/dev/null)"
    done
} >>"${TAPKICH_KIOSK_LOG:-/run/tapkich-kiosk.log}" 2>/dev/null
exit 0
