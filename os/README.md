# Xos node (prototype)

> **Appliance:** `os/appliance/` is the newer Debian 13 appliance for the two roles going forward,
> a display and a room computer (Google Meet with a Rally Bar, run from the room tablet). It has
> persistent identity, health reporting and updates with rollback. See `README.md`.
> The node installer below remains for Raspberry Pi screens, the camera host and AirPlay.

This turns a stock Linux box into an Xos screen. It installs a kiosk shell (cage + Chromium),
`xos-agent`, and systemd units. The design is in `README.md`.

```
os/
├── install.sh               # sudo ./os/install.sh --role tv|tablet|panel|signage|wall --room lamar
├── agent/xos_agent.py       # role UI server, LAN state hub, health, CEC, dry-run Zoom adapter
├── rootfs/                  # files installed onto the node (systemd units, kiosk launcher, config)
└── test/                    # Debian 12 container smoke test: ./os/test/run.sh
```

## Test without hardware (container)

```bash
./os/test/run.sh          # needs Docker; screenshots in os/test/out/
```

## Test on a Raspberry Pi 5 + TV

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. Enable SSH and set a
   hostname such as `xos-lamar-tv`.
2. Copy this repo to the Pi, then run `sudo ./os/install.sh --role tv --room lamar`.
3. The TV boots straight into the Lamar room screen.
4. On a tablet on the same network, open `http://xos-lamar-tv.local:7000/app/?kiosk=1#/tablet/lamar`.
   Tap Join and Mute; the TV follows.
5. To run the agent without the kiosk, on any machine:
   `XOS_NODE_CONFIG=os/rootfs/etc/xos/node.json XOS_APP_ROOT=. python3 os/agent/xos_agent.py`

Nothing here connects to Zoom, Google or Logitech The local-hub agent still serves
the old shared demo state (`/api/state`, `/api/adapter`); the web app no longer uses it, so on a
local-hub node screens show their idle views and remotes need the Xos server. Server mode (the
default) is unaffected.

## AirPlay receiver

Add `--airplay ["Name"]` to make the screen an AirPlay receiver with
[UxPlay](https://github.com/FDH2/UxPlay):

```bash
sudo ./os/install.sh --role tv --room atrium --show page/atrium-room --airplay "Atrium TV"
```

This installs `uxplay`, `avahi-daemon` and the GStreamer plugins, writes the name to
`/etc/xos/airplay.name`, and enables `xos-airplay.service`. The service runs as the `xos` user
next to the kiosk and draws on Cage's Wayland display, so a mirrored iPhone, iPad or Mac appears
full screen over Chromium and disappears when it stops. With `--show`, the name is also passed to
the kiosk URL (`&airplay=`) so the Xos share page and idle screen can point Apple users to it.
Without a name, the room's is used. Check it with `systemctl status xos-airplay` and
`journalctl -u xos-airplay`. The Pi 5 decodes the AirPlay stream in software; a mini PC
handles 1080p more comfortably.
