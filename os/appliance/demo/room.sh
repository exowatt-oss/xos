#!/bin/sh
# Inside the demo container, as user xos: the real room computer agent with Chromium in Cage on a
# headless 1920x1080 output, real internet, and a VNC view of its screen on 127.0.0.1:5901 so a
# person can sign the room's Google, Zoom or Microsoft account in. No Rally Bar: Chromium's test
# camera and microphone stand in. ROOM (default atrium) and XOS_SERVER come from the environment.
set -eu
export XDG_RUNTIME_DIR=/tmp/xdg; mkdir -p -m 700 $XDG_RUNTIME_DIR
exec dbus-run-session -- sh -c '
  pipewire & sleep 1; wireplumber & pipewire-pulse & sleep 2
  pactl load-module module-null-sink sink_name=rallybar_demo >/dev/null 2>&1 || true
  pactl set-default-sink rallybar_demo 2>/dev/null || true
  export WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_LIBINPUT_NO_DEVICES=1 WLR_HEADLESS_OUTPUTS=1
  (sleep 5; wlr-randr --output HEADLESS-1 --custom-mode 1920x1080 || true; WAYLAND_DISPLAY=wayland-0 wayvnc 127.0.0.1 5901) &
  exec python3 /xos/os/appliance/xos_room_linux.py --room "${ROOM:-atrium}" --server "$XOS_SERVER" \
    --state-dir /state --run-dir /tmp/run --idle-url "$XOS_SERVER/app/#/tv/${ROOM:-atrium}" \
    --chromium-arg=--no-sandbox --chromium-arg=--use-fake-device-for-media-stream'
