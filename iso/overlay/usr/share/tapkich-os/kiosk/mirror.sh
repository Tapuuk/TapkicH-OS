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
exit 0
