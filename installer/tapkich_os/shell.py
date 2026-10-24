"""The login shell: fish, as in stock Caelestia (its terminal and dotfiles use fish; Caelestia's installer puts its
fish config in place)."""
from __future__ import annotations

SHELL_PACKAGES = ["fish"]
LOGIN_SHELL = "/usr/bin/fish"                  # what useradd gets (-s); listed in /etc/shells by the fish package
