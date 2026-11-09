#!/bin/sh
# Save what the demo room computer's TV shows: tv.sh <out.png> [container]
set -eu
C="${2:-xos-demo-room}"
docker exec -u xos -e XDG_RUNTIME_DIR=/tmp/xdg -e WAYLAND_DISPLAY=wayland-0 "$C" grim /tmp/tv.png
docker cp -q "$C:/tmp/tv.png" "$1"
