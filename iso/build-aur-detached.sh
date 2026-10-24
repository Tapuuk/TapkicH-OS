#!/usr/bin/env bash
# Run build-aur.sh to completion, logging to /root/aur-build.log with a final EXIT= line.
bash "$(dirname "$0")/build-aur.sh" "$@" > /root/aur-build.log 2>&1
echo "EXIT=$?" >> /root/aur-build.log
