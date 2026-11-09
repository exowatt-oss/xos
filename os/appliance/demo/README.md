# Xos room demo kit

Tools for showing and checking the meeting room end to end without touching production or a
live room. Everything runs against a local Xos server with its own data.

| File | What it is |
|---|---|
| `calendar_feed.py` | The local Xos server, with room calendars read from a JSON file of Calendar API events (`events.example.json`) instead of Google. Everything after the HTTP call is the real code. |
| `Dockerfile`, `room.sh` | A room computer: the real agent (`xos_room_linux.py`) with Chromium in Cage, real internet, and a VNC view on `127.0.0.1:5901` for signing room accounts in |
| `tv.sh` | Save what that room computer's TV shows |
| `tablet.mjs` | The room iPad, tapped like a person, then a screenshot |
| `vm.py` | An x86 UEFI VM that installs from the Xos USB like a mini PC would |

## A room in five commands

```bash
# 1. the server, with a calendar
XOS_DATA_DIR=/tmp/xos_test-demo XOS_DEV_USER=you@example.com \
  uv run python os/appliance/demo/calendar_feed.py os/appliance/demo/events.example.json

# 2. the room computer (needs Docker)
docker build -f os/appliance/test/Dockerfile -t xos-appliance-test .
docker build -f os/appliance/demo/Dockerfile -t xos-demo-room .
docker volume create xos-demo-state
docker run --rm -v xos-demo-state:/state debian:trixie-slim chown 1000:1000 /state
docker run -d --name xos-demo-room --network host --shm-size 2g -u xos -e TZ=America/Denver \
  -v xos-demo-state:/state -v "$PWD:/xos:ro" -e XOS_SERVER=http://127.0.0.1:8796 \
  xos-demo-room /xos/os/appliance/demo/room.sh

# 3. sign the room's accounts in (from a laptop): ssh -N -L 5901:127.0.0.1:5901 <this host>,
#    open vnc://localhost:5901, and on the box screen:
docker exec -u xos xos-demo-room sh -c 'echo https://accounts.google.com/ > /tmp/run/open-url'

# 4. the tablet: join, mute, leave …
PLAYWRIGHT=/path/to/playwright-core/index.mjs node os/appliance/demo/tablet.mjs home.png
node os/appliance/demo/tablet.mjs joined.png "await pg.click('[data-act=rm-cal-join]')"

# 5. what the room TV shows
os/appliance/demo/tv.sh tv.png
```

What we learned running it against real services (October 2026):

- Google Meet in a locked-down workspace refuses signed-out guests outright, so the room's Google account must be signed in.
- Zoom's web client can ask a signed-out browser on a cloud network to sign in.
- Teams (free) invitations reach the guest join screen without signing in.
- The VNC view listens on loopback only; reach it through SSH.

The Xos USB image itself is built with `os/image/make-usb.sh`. It holds Wi-Fi passwords and the
maintenance password hash, so it never goes into Git; `*.iso` is ignored.
