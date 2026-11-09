#!/bin/sh
# Inside the test container, as user xos: audio (PipeWire with a null sink), the stand-in Meet, Zoom, Teams and Webex,
# then the real agent with Cage on wlroots' headless backend.
set -eu
export XDG_RUNTIME_DIR=/tmp/xdg; mkdir -p -m 700 $XDG_RUNTIME_DIR
cd /tmp
openssl req -x509 -newkey rsa:2048 -nodes -keyout k.pem -out c.pem -days 1 -subj /CN=meet.google.com \
  -addext subjectAltName=DNS:meet.google.com,DNS:app.zoom.us,DNS:zoom.us,DNS:teams.microsoft.com,DNS:acme.webex.com,DNS:acme.my.webex.com 2>/dev/null
python3 /xos/os/appliance/test/fake_meet.py 8443 c.pem k.pem &
exec dbus-run-session -- sh -c '
  pipewire & sleep 1; wireplumber & pipewire-pulse & sleep 2
  pactl load-module module-null-sink sink_name=rallybar_test >/dev/null 2>&1 || true
  pactl set-default-sink rallybar_test 2>/dev/null || true
  export WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_LIBINPUT_NO_DEVICES=1
  exec python3 /xos/os/appliance/xos_room_linux.py --room atrium --server "$XOS_SERVER" \
    --state-dir /tmp/state --run-dir /tmp/run --idle-url "$XOS_SERVER/app/#/tv/atrium" \
    --chromium-arg=--no-sandbox \
    --chromium-arg="--host-resolver-rules=MAP meet.google.com:443 127.0.0.1:8443, MAP app.zoom.us:443 127.0.0.1:8443, MAP zoom.us:443 127.0.0.1:8443, MAP teams.microsoft.com:443 127.0.0.1:8443, MAP acme.webex.com:443 127.0.0.1:8443, MAP acme.my.webex.com:443 127.0.0.1:8443" \
    --chromium-arg=--ignore-certificate-errors \
    --chromium-arg=--use-fake-device-for-media-stream'
