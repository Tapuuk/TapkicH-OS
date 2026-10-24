#!/usr/bin/env bash
# Run build.sh to completion, logging to /root/build.log, with a final EXIT= line.
bash "$(dirname "$0")/build.sh" "$@" > /root/build.log 2>&1
echo "EXIT=$?" >> /root/build.log
