#!/usr/bin/env bash
# The universal Xos USB's path on a systemd-booted Debian 13 (privileged container): install.sh
# --role display (what the USB's late_command runs), a first-boot hostname of its own, and the
# kiosk launcher opening the Xos server's pairing code (#/setup) with a persistent device id; then
# Admin pairs it as the Atrium meeting computer and the box reinstalls itself as one (xos-box).
# Cage is replaced by a stub that records what it would run (no screen in a container).
#   ./os/appliance/test/display_test.sh     (needs Docker with --privileged, and uv)
set -euo pipefail
cd "$(dirname "$0")/../../.."
PORT=${PORT:-8795}
DATA=$(mktemp -d /tmp/xos_test-appliance-disp-XXXX)
NAME=xos-appliance-disp-$$
docker build -q -f os/appliance/test/Dockerfile.systemd -t xos-appliance-sd . >/dev/null
XOS_DATA_DIR=$DATA XOS_DEV_USER=root@example.com uv run uvicorn app.main:app --app-dir src --host 127.0.0.1 --port "$PORT" >"$DATA/server.log" 2>&1 &
SERVER_PID=$!
trap '[ -n "${KEEP:-}" ] || { docker rm -f "$NAME" >/dev/null 2>&1; kill $SERVER_PID 2>/dev/null; } || true' EXIT
until curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null; do sleep 0.5; done
# Linux shares the host's network. Docker Desktop (macOS) has no host network by default: the
# box reaches this machine's loopback server through host.docker.internal instead.
if [ "$(uname)" = Darwin ]; then NET=(); BOX_SERVER="http://host.docker.internal:$PORT"
else NET=(--network host); BOX_SERVER="http://127.0.0.1:$PORT"; fi
docker run -d --name "$NAME" --hostname xos --privileged --cgroupns=host ${NET[@]+"${NET[@]}"} \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw -v "$PWD:/xos:ro" xos-appliance-sd >/dev/null
X() { docker exec "$NAME" "$@"; }
until case "$(X systemctl is-system-running 2>/dev/null || true)" in running|degraded) true ;; *) false ;; esac; do sleep 1; done
pass=0; fail=0
t() { local n="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PASS $n"; pass=$((pass+1)); else echo "FAIL $n"; fail=$((fail+1)); fi; }
X systemctl mask NetworkManager.service NetworkManager-wait-online.service >/dev/null 2>&1
# A stand-in Cage: records the command instead of drawing on a screen.
X sh -c 'printf "#!/bin/sh\necho \"\$@\" > /var/lib/xos/cage-args\nexec sleep infinity\n" > /usr/local/bin/cage && chmod +x /usr/local/bin/cage'
X mkdir -p /etc/systemd/system/xos-display.service.d
X sh -c 'printf "[Service]\nPAMName=\nTTYPath=\nStandardInput=null\nUtmpIdentifier=\nEnvironment=PATH=/usr/local/bin:/usr/bin:/bin\n" > /etc/systemd/system/xos-display.service.d/test.conf'
X /xos/os/appliance/install.sh --role display --server "$BOX_SERVER" --from /xos --no-start > "$DATA/install.log" 2>&1 \
  && echo "PASS install.sh --role display --no-start (what the Xos USB runs)" && pass=$((pass+1)) || { echo "FAIL install"; tail -20 "$DATA/install.log"; exit 1; }
t "xos-display enabled for boot" X systemctl is-enabled xos-display.service
t "first-boot hostname enabled" X systemctl is-enabled xos-hostname.service
t "updates enabled" X systemctl is-enabled xos-update.timer
t "display policy (DevTools off)" X grep -q DeveloperToolsAvailability /etc/chromium/policies/managed/xos-appliance.json
# "boot": run what systemd would at startup
X systemctl start xos-hostname.service || true  # fails here: Docker owns /etc/hostname
H=$(X hostname)
if [ "$H" = xos ] && X journalctl -u xos-hostname --no-pager | grep -q "Device or resource busy"; then
  # Docker bind-mounts /etc/hostname: it can't change here. Check the box would try again next boot.
  echo "SKIP its own hostname (Docker owns /etc/hostname in containers; works on a real box)"
  t "a failed rename isn't marked done (retried next boot)" sh -c "! docker exec $NAME test -e /var/lib/xos/hostname-set"
else
  t "its own hostname on first boot ($H)" sh -c "echo $H | grep -Eq '^xos-[0-9a-f]{6}$'"
fi
X systemctl start xos-display.service
for i in $(seq 1 30); do X test -s /var/lib/xos/cage-args && break; sleep 1; done
ARGS=$(X cat /var/lib/xos/cage-args 2>/dev/null || true)
t "kiosk opens the Xos pairing code" sh -c "echo '$ARGS' | grep -q '$BOX_SERVER/app/?kiosk=1&device=xos-.*&client=xos-display-local-.*&restore=1#/setup'"
ID=$(echo "$ARGS" | grep -o 'device=[^&]*' | cut -d= -f2)
t "device id kept in /var/lib/xos, no doubled prefix ($ID)" sh -c "echo $ID | grep -Eq '^xos-([a-z0-9-]+-)?[0-9a-f]{6}$' && ! echo $ID | grep -q '^xos-xos-' && docker exec $NAME grep -q $ID /var/lib/xos/identity.json"
X systemctl restart xos-display.service; sleep 3
t "same device id after a restart" sh -c "docker exec $NAME cat /var/lib/xos/cage-args | grep -q device=$ID"
t "no browser debugging port" sh -c "! docker exec $NAME cat /var/lib/xos/cage-args | grep -q 'remote-debugging-p[o]rt'"

# Admin pairs this box as the Atrium room's meeting computer (Meet, Zoom, Teams, Webex).
S="http://127.0.0.1:$PORT"
t "box helper enabled (Admin can make it a room computer)" X systemctl is-enabled xos-box.timer
X systemctl start xos-box.service
t "the box's secret is root-only" sh -c "docker exec $NAME stat -c '%a %U' /var/lib/xos/box-secret | grep -qx '600 root'"
curl -fs -X POST -H 'Content-Type: application/json' "$S/api/devices/heartbeat" \
  -d "{\"id\": \"$ID\", \"client\": \"xos-display-local\", \"role\": \"setup\"}" >/dev/null  # what its page does
PAIR=$(curl -fs -X POST -H 'Content-Type: application/json' "$S/api/devices/pair" -d "{\"id\": \"$ID\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])')
PC=$(curl -fs -X POST -H 'Content-Type: application/json' "$S/api/devices/claim" \
  -d "{\"code\": \"$PAIR\", \"room\": \"atrium\", \"use\": \"room\", \"name\": \"Lab meeting computer\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
t "Admin: pair it as the room's meeting computer ($PC)" test -n "$PC"
# The room computer has no seat in a container: the same test-only drop-in as systemd_test.sh.
X mkdir -p /etc/systemd/system/xos-room.service.d
X sh -c 'printf "[Service]\nPAMName=\nTTYPath=\nStandardInput=null\nUtmpIdentifier=\n" > /etc/systemd/system/xos-room.service.d/test.conf'
X systemctl start xos-box.service > "$DATA/convert.log" 2>&1 || true  # what its timer does within 15 s
t "the box reinstalled itself as the atrium room computer" sh -c "docker exec $NAME grep -q '\"role\": \"room\", \"room\": \"atrium\"' /etc/xos/appliance.json"
t "room computer service on, display off" sh -c "docker exec $NAME systemctl is-enabled xos-room.service && ! docker exec $NAME systemctl is-enabled xos-display.service"
t "box helper off once it's a room computer" sh -c "! docker exec $NAME systemctl is-enabled xos-box.timer"
t "camera and mics for Meet, Zoom, Teams and Webex" sh -c "docker exec $NAME grep -q app.zoom.us /etc/chromium/policies/managed/xos-appliance.json && docker exec $NAME grep -q teams.microsoft.com /etc/chromium/policies/managed/xos-appliance.json && docker exec $NAME grep -q 'webex.com' /etc/chromium/policies/managed/xos-appliance.json"
t "it has the room computer's identity and key" sh -c "docker exec $NAME grep -q $PC /var/lib/xos/identity.json && docker exec $NAME test -s /var/lib/xos/device-key"
for i in $(seq 1 60); do curl -fs "$S/api/devices" | grep -q "\"id\":\"$PC\".*\"online\":true" && break; sleep 2; done
t "online in Admin as the atrium room computer, offering Meet, Zoom, Teams and Webex" sh -c "curl -fs $S/api/devices | python3 -c 'import json,sys; ds=json.load(sys.stdin); d=[x for x in ds if x[\"id\"]==\"$PC\"][0]; assert d[\"online\"] and d[\"role\"]==\"roompc\" and d[\"room\"]==\"atrium\" and d[\"name\"]==\"Lab meeting computer\" and set(d[\"native\"][\"meeting_providers\"])=={\"meet\",\"zoom\",\"teams\",\"webex\"}; assert not [x for x in ds if x[\"id\"]==\"$ID\"]'"
echo; echo "$pass passed, $fail failed"; [ "$fail" = 0 ] || { tail -30 "$DATA/convert.log"; X journalctl -u xos-box -u xos-room -n 40 --no-pager; exit 1; }
