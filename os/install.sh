#!/usr/bin/env bash
# Turn a fresh Raspberry Pi OS Lite (Bookworm, 64-bit) or Debian 12 box into an Xos node.
# Straight from the Xos server, nothing to copy (the rest is fetched from it):
#
#   curl -fsSL https://xos.example.org/os/install.sh | sudo bash -s -- --role tv --room atrium \
#     --show page/atrium-room --camera
#
#   sudo ./os/install.sh --role tv --room lamar          # front-of-room TV
#   sudo ./os/install.sh --role panel --room lamar       # door panel (Pi + 7" touch)
#   sudo ./os/install.sh --role signage --room hq    # lobby TV
#   sudo ./os/install.sh --role tablet --room lamar      # table controller on a Pi touch panel
#   sudo ./os/install.sh --role camera --room atrium [--headless]
#        # Rally Bar (USB mode) camera host: serves the bar's live feed to Xos screens, e.g. the
#        # Fire TV (Admin → Screens → Show → Live feed). --headless: no monitor needed.
#   sudo ./os/install.sh --role tv --room atrium --show page/atrium-room --camera
#        # Room TV run from the Xos server (Admin → Screens controls it, like the Fire TV), starting
#        # on --show; --camera also serves a Rally Bar (USB mode) plugged into this Pi as a live feed.
#   sudo ./os/install.sh --role tv --room atrium --show page/atrium-room --airplay ["Atrium TV"]
#        # Also an AirPlay receiver (UxPlay): iPhones, iPads and Macs mirror to it from Control
#        # Center → Screen Mirroring. The name defaults to the room, e.g. "Atrium".
#
# Prototype of the Xos layer only: packages + agent + kiosk + config. The production image adds a
# read-only root, A/B updates (RAUC), enrollment and mTLS — see README.md.
set -euo pipefail

ARGS=("$@")
ROLE=tv; ROOM=lamar; SERVER=https://xos.example.org; HEADLESS=0; SHOW=""; CAMERA=0; AIRPLAY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --room) ROOM="$2"; shift 2 ;;
    --server) SERVER="${2%/}"; shift 2 ;;
    --headless) HEADLESS=1; shift ;;
    --show) SHOW="${2#\#/}"; shift 2 ;;
    --camera) CAMERA=1; shift ;;
    --airplay)
      # Optional name: --airplay "Atrium TV". Without one, the room's name.
      if [ $# -gt 1 ] && [ "${2#--}" = "$2" ]; then AIRPLAY="$2"; shift 2
      else AIRPLAY_DEFAULT=1; shift; fi ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
# --airplay before --room: name it after the room given later.
[ "${AIRPLAY_DEFAULT:-0}" = 1 ] && AIRPLAY="$(echo "$ROOM" | sed 's/-/ /g; s/\b\(.\)/\u\1/g')"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }

# Run from the web (curl … | sudo bash): fetch the rest of Xos from the server and continue there.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ ! -f "$HERE/agent/xos_agent.py" ]; then
  echo "==> Fetching Xos from $SERVER"
  rm -rf /opt/xos/src.new && mkdir -p /opt/xos/src.new
  curl -fsSL "$SERVER/os/xos-node.tar.gz" | tar -xz -C /opt/xos/src.new
  rm -rf /opt/xos/src && mv /opt/xos/src.new /opt/xos/src
  curl -fsSL "$SERVER/os/xos-node.sha256" > /opt/xos/src/bundle.sha256
  exec bash /opt/xos/src/os/install.sh "${ARGS[@]}"
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
echo "==> Installing Xos node: role=$ROLE room=$ROOM from $REPO"

apt-get update
CHROMIUM_PKG=chromium
apt-cache show chromium >/dev/null 2>&1 || CHROMIUM_PKG=chromium-browser
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  cage "$CHROMIUM_PKG" python3 curl v4l-utils fonts-inter-variable fonts-noto-color-emoji \
  || DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends cage "$CHROMIUM_PKG" python3 curl v4l-utils
# HDMI-CEC tools: cec-ctl ships in v4l-utils (installed above).
if [ -n "$AIRPLAY" ]; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends uxplay avahi-daemon \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav
  systemctl enable --now avahi-daemon.service
fi

# render/input exist on Raspberry Pi OS but not on every minimal Debian; create any that are missing.
for g in video render input audio; do getent group "$g" >/dev/null || groupadd --system "$g"; done
id xos >/dev/null 2>&1 || useradd --create-home --shell /usr/sbin/nologin --groups video,render,input,audio xos

install -d /opt/xos/agent /opt/xos/site /etc/xos
install -m 0755 "$REPO/os/agent/xos_agent.py" /opt/xos/agent/xos_agent.py
rm -rf /opt/xos/site/*
cp -r "$REPO/app" "$REPO/static" /opt/xos/site/
install -m 0755 "$REPO/os/rootfs/usr/local/bin/xos-kiosk" /usr/local/bin/xos-kiosk
install -m 0644 "$REPO/os/rootfs/etc/systemd/system/xos-agent.service" /etc/systemd/system/
install -m 0755 "$REPO/os/rootfs/usr/local/bin/xos-camera" /usr/local/bin/xos-camera
install -m 0644 "$REPO/os/rootfs/etc/systemd/system/xos-kiosk.service" /etc/systemd/system/
install -m 0644 "$REPO/os/rootfs/etc/systemd/system/xos-camera.service" /etc/systemd/system/
install -m 0755 "$REPO/os/rootfs/usr/local/bin/xos-airplay" /usr/local/bin/xos-airplay
install -m 0644 "$REPO/os/rootfs/etc/systemd/system/xos-airplay.service" /etc/systemd/system/
install -m 0755 "$REPO/os/rootfs/usr/local/bin/xos-update" /usr/local/bin/xos-update
install -m 0644 "$REPO/os/rootfs/etc/systemd/system/xos-update.service" /etc/systemd/system/
install -m 0644 "$REPO/os/rootfs/etc/systemd/system/xos-update.timer" /etc/systemd/system/
# For the nightly sync (xos-update): where to update from, and this install's options.
echo "$SERVER" > /etc/xos/server
printf '%q ' "${ARGS[@]}" > /etc/xos/install.args
cat > /etc/xos/node.json <<JSON
{ "role": "$ROLE", "room": "$ROOM", "zoom_room_id": "$ROOM-zr", "adapter": "zrc-sdk", "port": 7000 }
JSON

# Camera host: the kiosk opens the camera page on the Xos server (feeds are relayed there), with
# camera and microphone pre-allowed for that site only. 720p by default: the Pi 5 has no hardware
# video encoder, and 1080p in software is too much for it.
rm -f /etc/xos/kiosk.url /etc/xos/camera.url /etc/xos/headless /etc/xos/airplay.name
[ -n "$AIRPLAY" ] && printf '%s\n' "$AIRPLAY" > /etc/xos/airplay.name
# The screen tells Xos its AirPlay name, so its share page and idle screen can point people to it.
AIRPLAY_Q=""
[ -n "$AIRPLAY" ] && AIRPLAY_Q="&airplay=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$AIRPLAY")"
CAM_URL="$SERVER/app/?kiosk=1&device=pi-$(hostname)-camera&client=pi-camera&res=720#/camera"
if [ "$ROLE" = camera ]; then
  echo "$CAM_URL" > /etc/xos/kiosk.url
  [ "$HEADLESS" = 1 ] && touch /etc/xos/headless
fi
# Server-run screen: the kiosk opens the Xos server like the Fire TV does, so Admin → Screens can
# switch what it shows. restore=1 reopens the last screen Admin chose after a reboot.
[ -n "$SHOW" ] && echo "$SERVER/app/?kiosk=1&device=pi-$(hostname)&client=pi-$ROLE&restore=1$AIRPLAY_Q#/$SHOW" > /etc/xos/kiosk.url
[ "$CAMERA" = 1 ] && echo "$CAM_URL" > /etc/xos/camera.url
if [ "$ROLE" = camera ] || [ "$CAMERA" = 1 ]; then
  for dir in /etc/chromium/policies/managed /etc/chromium-browser/policies/managed; do
    install -d "$dir"
    cat > "$dir/xos-camera.json" <<POLICY
{ "VideoCaptureAllowedUrls": ["$SERVER"], "AudioCaptureAllowedUrls": ["$SERVER"] }
POLICY
  done
fi

systemctl daemon-reload
systemctl enable --now xos-agent.service
systemctl set-default graphical.target
systemctl disable getty@tty1.service || true
systemctl enable --now xos-kiosk.service
systemctl enable --now xos-update.timer
if [ "$CAMERA" = 1 ]; then systemctl enable xos-camera.service; systemctl restart xos-camera.service
else systemctl disable --now xos-camera.service 2>/dev/null || true; fi
if [ -n "$AIRPLAY" ]; then systemctl enable xos-airplay.service; systemctl restart xos-airplay.service
else systemctl disable --now xos-airplay.service 2>/dev/null || true; fi

IP=$(hostname -I | awk '{print $1}')
echo
if [ "$ROLE" = camera ]; then
  echo "==> Done. This Pi now opens the Xos camera page on boot (device pi-$(hostname))."
  echo "    1. Plug the Rally Bar (USB mode) into a blue USB 3 port; check it with: v4l2-ctl --list-devices"
  echo "    2. Admin → Screens: the Pi appears as 'Live camera'; name it, e.g. 'Atrium Rally Bar'"
  echo "    3. Atrium room → Control → Show → 'Live feed · Atrium Rally Bar'"
  exit 0
fi
if [ -n "$SHOW" ]; then
  echo "==> Done. This screen opens #/$SHOW from $SERVER on boot (device pi-$(hostname))."
  echo "    Admin → Screens: name it, e.g. 'Atrium room', and switch what it shows from there."
  [ "$CAMERA" = 1 ] && echo "    Camera: plug the Rally Bar (USB mode) into a blue USB 3 port; it appears as pi-$(hostname)-camera." \
    && echo "    Show it with Control → Show → 'Live feed'. Check the bar with: v4l2-ctl --list-devices"
  [ -n "$AIRPLAY" ] && echo "    AirPlay: mirror to '$AIRPLAY' from an iPhone, iPad or Mac on this network."
  exit 0
fi
echo "==> Done. This screen now boots into the '$ROLE' view for '$ROOM'."
echo "    Health:      http://$IP:7000/api/health"
echo "    Controller:  open http://$IP:7000/app/?kiosk=1#/tablet/$ROOM on a tablet on the same network"
echo "    Admin:       http://$IP:7000/app/#/admin/overview"
