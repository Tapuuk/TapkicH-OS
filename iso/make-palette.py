#!/usr/bin/env python3
"""Turn theme/<name>/colors.toml into a shell palette for the installer. usage: make-palette.py colors.toml > palette.sh"""
import sys
import tomllib


def rgb(h: str) -> str:
    h = h.lstrip("#")
    return " ".join(str(int(h[i:i + 2], 16)) for i in (0, 2, 4))


with open(sys.argv[1], "rb") as f:
    c = tomllib.load(f)


def hx(key):
    return c[key].lstrip("#")


print("# generated from colors.toml; do not edit")
print(f'PRIMARY_HEX="{hx("primary")}"')            # violet
print(f'SECONDARY_HEX="{hx("secondary")}"')        # orchid
print(f'DANGER_HEX="{hx("danger")}"')              # red: warnings, risky actions
print(f'DANGER_BRIGHT_HEX="{hx("danger_bright")}"')
print(f'HEADER_HEX="{hx("bright_foreground")}"')   # near-white
print(f'BODY_HEX="{hx("light_foreground")}"')      # description text
print(f'MUTED_HEX="{hx("dark_foreground")}"')      # hints
print(f'G_START="{rgb(c["gradient_start"])}"')
print(f'G_END="{rgb(c["gradient_end"])}"')
