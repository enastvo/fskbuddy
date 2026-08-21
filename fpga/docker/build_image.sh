#!/bin/bash
# Builds the ISE 14.7 docker image. Requires docker/installer/ to contain
# the ISE 14.7 installer (tar or extracted dir) -- see docker/README.md.
set -euo pipefail
cd "$(dirname "$0")"

if [ -z "$(find installer -mindepth 1 ! -name .gitkeep 2>/dev/null)" ]; then
    echo "docker/installer/ is empty. Drop the ISE 14.7 installer there first." >&2
    exit 1
fi

docker build -t ise147-b2xxmini .
