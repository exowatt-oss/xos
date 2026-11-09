#!/usr/bin/env bash
# Simulated end-to-end check of the Linux room computer: a local Xos server (isolated data) on
# this machine, the agent driving real Chromium in Cage in a Debian 13 container, stand-in Meet,
# Zoom, Teams and Webex pages.
# No Google, Zoom, Microsoft or Cisco, no Rally Bar, no TV: see README.md for the
# physical acceptance test.
#   ./os/appliance/test/run.sh          (needs Docker and uv)
set -euo pipefail
cd "$(dirname "$0")/../../.."
PORT=${PORT:-8792}
DATA=$(mktemp -d /tmp/xos_test-appliance-XXXX)
NAME=xos-appliance-test-$$
docker build -q -f os/appliance/test/Dockerfile -t xos-appliance-test . >/dev/null
# Instant meetings on: the run starts (and ends) a Zoom meeting from the tablet.
XOS_DATA_DIR=$DATA XOS_DEV_USER=root@example.com XOS_INSTANT_MEETINGS=1 uv run uvicorn app.main:app --app-dir src \
  --host 127.0.0.1 --port "$PORT" >"$DATA/server.log" 2>&1 &
SERVER_PID=$!
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true; kill $SERVER_PID 2>/dev/null || true' EXIT
until curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null; do sleep 0.5; done
# Linux shares the host's network. Docker Desktop (macOS) has no host network by default: the
# container reaches this machine's loopback server through host.docker.internal instead.
if [ "$(uname)" = Darwin ]; then NET=(); BOX_SERVER="http://host.docker.internal:$PORT"
else NET=(--network host); BOX_SERVER="http://127.0.0.1:$PORT"; fi
docker run -d --name "$NAME" ${NET[@]+"${NET[@]}"} --shm-size 1g -u xos -v "$PWD:/xos:ro" \
  -e XOS_SERVER="$BOX_SERVER" xos-appliance-test /xos/os/appliance/test/in_container.sh >/dev/null
python3 os/appliance/test/drive.py "http://127.0.0.1:$PORT" "$NAME" || { echo "--- agent log"; docker logs "$NAME" 2>&1 | tail -40; exit 1; }
