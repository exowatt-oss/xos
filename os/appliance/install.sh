#!/usr/bin/env bash
# Xos appliance: turn a fresh Debian 13 (trixie) box into an Xos display or room computer.
#
#   curl -fsSL https://xos.example.org/os/appliance/install.sh | sudo bash -s -- --role room --room atrium
#   curl -fsSL https://xos.example.org/os/appliance/install.sh | sudo bash -s -- --role display --show page/atrium-room
#   sudo ./os/appliance/install.sh --role room --room atrium --from .      # from a checkout (tests, image builds)
#
#   --role display   boots into the Xos screen on the TV; Admin → Screens names it and picks what it shows
#   --role room      the room computer: Google Meet, Zoom, Teams and Webex on the TV with the Rally Bar,
#                    run from the room tablet (a display paired as one in Admin becomes one by itself)
#   --server URL     the Xos server (default https://xos.example.org)
#   --show ROUTE     display: what it opens on until Admin picks (default: its pairing code)
#   --from DIR       install this checkout instead of the server's current release
#   --enroll CODE    the setup code from Admin → Screens → Add a Linux box: role, room, name, what it
#                    shows, hostname, maintenance login and device key all come from Xos
#   --no-start       set everything up but start nothing (image builds, the Debian installer)
#
# Layout: /opt/xos/releases/<release>/ (os/, app/, static/), /opt/xos/current → the one in use,
# /etc/xos/appliance.{json,env} (role, room, server), /var/lib/xos (identity, browser profile,
# update state; 0700, user xos). xos-update.timer moves to new releases and rolls back bad ones.
# See README.md.
set -euo pipefail

ROLE=""; ROOM=""; SERVER=https://xos.example.org; SHOW=""; FROM=""; START=1; ENROLL=""; NAME_=""
while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --room) ROOM="$2"; shift 2 ;;
    --server) SERVER="${2%/}"; shift 2 ;;
    --show) SHOW="${2#\#/}"; shift 2 ;;
    --from) FROM="$(cd "$2" && pwd)"; shift 2 ;;
    --no-start) START=0; shift ;;
    --enroll) ENROLL="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
# A box installed from the Debian / Xos USB still lists the stick as a package source, and apt
# refuses to update while it does. The stick is gone once the box is installed: disable it.
sed -i 's/^deb cdrom:/# deb cdrom:/' /etc/apt/sources.list 2>/dev/null || true
for f in /etc/apt/sources.list.d/*.sources; do
  [ -e "$f" ] && grep -q '^URIs: *cdrom:' "$f" && mv "$f" "$f.disabled-by-xos"
done
# A power cut during an earlier attempt leaves dpkg half done, and apt refuses to install until
# it's finished: finish it (a no-op otherwise).
DEBIAN_FRONTEND=noninteractive dpkg --configure -a >/dev/null 2>&1 || true
# Enrolling: the box's settings come from Xos, set up in Admin. The code works once.
if [ -n "$ENROLL" ]; then
  echo "==> Enrolling with $SERVER"
  apt-get update -q >/dev/null || echo "    (apt-get update reported errors; continuing)"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends curl ca-certificates python3 >/dev/null
  ENROLL_JSON="$(curl -fsS --max-time 30 -X POST -H 'Content-Type: application/json' \
    -d "{\"code\": \"$ENROLL\"}" "$SERVER/api/appliances/enroll")" \
    || { echo "That setup code wasn't accepted (used, expired or mistyped). Admin → Screens → Linux boxes can make a new one." >&2; exit 1; }
  get() { printf '%s' "$ENROLL_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('$1') or '')"; }
  ROLE="$(get role)"; ROOM="$(get room)"; SHOW="$(get show)"; NAME_="$(get name)"
fi
case "$ROLE" in display|room) ;; *) echo "--role display or --role room (or --enroll CODE)" >&2; exit 2 ;; esac
[ "$ROLE" = room ] && [ -z "$ROOM" ] && { echo "--role room needs --room (e.g. atrium)" >&2; exit 2; }
printf '%s\n' "$ROOM$SHOW" | grep -Eq '^[a-z0-9/-]*$' || { echo "room and show are lowercase ids" >&2; exit 2; }
. /etc/os-release
[ "${ID:-}" = debian ] && [ "${VERSION_ID:-}" = 13 ] || echo "warning: built for Debian 13; this is ${PRETTY_NAME:-unknown}" >&2

echo "==> Packages"
PKGS=(cage chromium python3 curl ca-certificates dbus-user-session fonts-noto-color-emoji sudo
      network-manager wpasupplicant openssh-server)
[ "$ROLE" = room ] && PKGS+=(pipewire pipewire-pulse pipewire-alsa wireplumber at-spi2-core python3-pyatspi python3-gi wtype)
apt-get update -q || echo "    (apt-get update reported errors; continuing)"
DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${PKGS[@]}"
# Wi-Fi chips in mini PCs (Intel, Realtek, MediaTek); from non-free-firmware, so best effort.
DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
  firmware-iwlwifi firmware-realtek firmware-mediatek >/dev/null 2>&1 || echo "    (no extra Wi-Fi firmware: enable non-free-firmware if the Wi-Fi chip needs it)"

echo "==> User and directories"
for g in video render input audio; do getent group "$g" >/dev/null || groupadd --system "$g"; done
id xos >/dev/null 2>&1 || useradd --system --home-dir /var/lib/xos/home --shell /usr/sbin/nologin \
  --groups video,render,input,audio xos
install -d -m 0700 -o xos -g xos /var/lib/xos /var/lib/xos/home
install -d -m 0755 /opt/xos/releases /etc/xos
if [ -n "$ENROLL" ]; then
  # Identity and key from Xos (the box shows up in Admin under the name and room set there).
  get device_id | python3 -c 'import json,sys; print(json.dumps({"id": sys.stdin.read().strip()}))' \
    | install -m 0600 -o xos -g xos /dev/stdin /var/lib/xos/identity.json
  get device_key | tr -d '\n' | install -m 0600 -o xos -g xos /dev/stdin /var/lib/xos/device-key
  # Hostname, and the maintenance login whose password is in Admin (never in the image or Git).
  HOST="$(get hostname)"; MUSER="$(get user)"
  echo "$HOST" > /etc/hostname
  sed -i '/^127\.0\.1\.1/d' /etc/hosts && echo "127.0.1.1 $HOST" >> /etc/hosts
  hostname "$HOST" 2>/dev/null || true
  id "$MUSER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$MUSER"
  usermod -aG sudo "$MUSER"
  printf '%s:%s\n' "$MUSER" "$(get password)" | chpasswd
  echo "    $HOST · maintenance login $MUSER (password in Admin → Screens → Linux boxes)"
fi

echo "==> Release"
if [ -n "$FROM" ]; then
  NAME="local-$(date -u +%Y%m%d%H%M%S)"
  install -d "/opt/xos/releases/.$NAME.partial"
  cp -a "$FROM/os" "$FROM/app" "$FROM/static" "/opt/xos/releases/.$NAME.partial/"
else
  NAME="$(curl -fsS --max-time 30 "$SERVER/os/xos-node.sha256" | tr -d '[:space:]')"
  [ "${#NAME}" -eq 64 ] || { echo "no release checksum from $SERVER" >&2; exit 1; }
  TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
  curl -fsSL --max-time 300 "$SERVER/os/xos-node.tar.gz" -o "$TMP"
  echo "$NAME  $TMP" | sha256sum -c --quiet - || { echo "release checksum mismatch" >&2; exit 1; }
  rm -rf "/opt/xos/releases/.$NAME.partial"; install -d "/opt/xos/releases/.$NAME.partial"
  tar -xzf "$TMP" -C "/opt/xos/releases/.$NAME.partial" --no-same-owner
fi
rm -rf "/opt/xos/releases/$NAME" && mv "/opt/xos/releases/.$NAME.partial" "/opt/xos/releases/$NAME"
ln -sfn "releases/$NAME" /opt/xos/current.new && mv -T /opt/xos/current.new /opt/xos/current
REL=/opt/xos/current/os/appliance

echo "==> Network"
# NetworkManager runs every interface: wired Ethernet when plugged in (preferred), otherwise the
# Wi-Fi networks from Admin, best in range first. The installer's ifupdown setup is kept aside.
if [ -f /etc/network/interfaces ] && grep -Eq '^(auto|allow-hotplug) +[^l]' /etc/network/interfaces; then
  cp -n /etc/network/interfaces /etc/network/interfaces.before-xos
  printf 'source /etc/network/interfaces.d/*\n\nauto lo\niface lo inet loopback\n' > /etc/network/interfaces
  NET_NEXT_BOOT=1
fi
if [ -n "$ENROLL" ]; then
  printf '%s' "$ENROLL_JSON" | python3 "$REL/xos-wifi"
fi
systemctl enable NetworkManager.service >/dev/null 2>&1 || true

echo "==> Configuration ($ROLE${ROOM:+, room $ROOM})"
cat > /etc/xos/appliance.json <<JSON
{"role": "$ROLE", "room": "$ROOM", "server": "$SERVER", "show": "$SHOW", "name": "$NAME_"}
JSON
printf 'XOS_ROLE=%s\nXOS_ROOM=%s\nXOS_SERVER=%s\nXOS_SHOW=%s\n' "$ROLE" "$ROOM" "$SERVER" "$SHOW" > /etc/xos/appliance.env
for unit in "$REL"/systemd/*; do install -m 0644 "$unit" /etc/systemd/system/; done
for dir in /etc/chromium/policies/managed; do
  install -d "$dir"; install -m 0644 "$REL/policies/$ROLE.json" "$dir/xos-appliance.json"
done
install -d /etc/systemd/journald.conf.d /etc/systemd/system.conf.d
install -m 0644 "$REL/conf/journald-xos.conf" /etc/systemd/journald.conf.d/xos.conf
install -m 0644 "$REL/conf/system-xos.conf" /etc/systemd/system.conf.d/xos.conf
install -d /var/log/journal
if [ "$ROLE" = room ]; then
  install -d /etc/wireplumber/wireplumber.conf.d
  install -m 0644 "$REL/conf/51-xos-rally-bar.conf" /etc/wireplumber/wireplumber.conf.d/
  install -m 0755 "$REL/xos-room-signin" /usr/local/sbin/xos-room-signin
fi

# Boot straight into Xos: no boot menu (hold Shift or Esc while starting to get it back).
if [ -f /etc/default/grub ]; then
  sed -i -e 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=0/' -e '/^GRUB_TIMEOUT_STYLE=/d' /etc/default/grub
  echo 'GRUB_TIMEOUT_STYLE=hidden' >> /etc/default/grub
  update-grub >/dev/null 2>&1 || true
fi

echo "==> Services"
# The earlier node prototype (os/install.sh) used the same TTY; retire its units if present.
for old in xos-kiosk xos-agent xos-camera xos-airplay; do
  systemctl disable --now "$old.service" 2>/dev/null || true
done
# A Debian installed with a desktop: its login screen owns the TV (tty1 and the seat). Xos takes
# over from the next boot; switching the desktop off now would close the terminal running this.
DESKTOP=""
for dm in gdm3 gdm lightdm sddm lxdm xdm slim; do
  if systemctl is-enabled "$dm.service" >/dev/null 2>&1; then
    systemctl disable "$dm.service" >/dev/null 2>&1 || true
    systemctl is-active --quiet "$dm.service" && DESKTOP="$dm"
  fi
done
rm -f /etc/systemd/system/display-manager.service
[ -n "$DESKTOP" ] && START=0
OTHER=display; [ "$ROLE" = display ] && OTHER=room
systemctl disable --now "xos-$OTHER.service" 2>/dev/null || true
systemctl daemon-reload
systemctl set-default graphical.target
systemctl disable getty@tty1.service 2>/dev/null || true
systemctl enable "xos-$ROLE.service" xos-update.timer
systemctl enable xos-hostname.service >/dev/null 2>&1 || true
# SSH keys for the maintenance login, from Xos (os/appliance/ssh/authorized_keys and Admin).
systemctl enable xos-keys.timer ssh.service >/dev/null 2>&1 || true
"$REL/xos-keys" || echo "    (SSH keys: will sync when the box reaches $SERVER)"
[ "$ROLE" = room ] && systemctl enable xos-update-now.path
# A display's helper: Admin can pair this box as a room's meeting computer (os/appliance/xos-box).
if [ "$ROLE" = display ]; then
  systemctl enable xos-box.service xos-box.timer
else
  systemctl disable --now xos-box.timer 2>/dev/null || true
  systemctl disable xos-box.service 2>/dev/null || true
fi
if [ "$START" = 1 ]; then
  systemctl restart systemd-journald
  systemctl daemon-reexec
  systemctl restart "xos-$ROLE.service"
  systemctl start xos-update.timer
  [ "$ROLE" = room ] && systemctl start xos-update-now.path
  [ "$ROLE" = display ] && systemctl start xos-box.timer
  systemctl start xos-keys.timer
fi

echo
echo "==> Done: Xos $ROLE, release ${NAME:0:12}."
if [ -n "$DESKTOP" ]; then
  echo "    The desktop ($DESKTOP) is switched off from the next boot. Restart now: sudo reboot"
  echo "    After the restart the TV shows Xos instead of the desktop."
fi
[ "${NET_NEXT_BOOT:-0}" = 1 ] && echo "    Network: NetworkManager (Wi-Fi from Admin, Ethernet preferred) takes over at the next boot."
if [ "$ROLE" = room ]; then
  echo "    1. Plug in the Rally Bar (USB mode) and the TV (HDMI). The TV shows the $ROOM room screen."
  echo "    2. Meet, Zoom, Teams and Webex invitations join as a guest named after the room. To start meetings"
  echo "       as the room's account, sign in once with a USB keyboard: sudo xos-room-signin [google|zoom|teams]"
  echo "    3. Admin → Screens → Room computers lists it; the $ROOM tablet's Join, mute and leave run here."
else
  echo "    The TV shows ${SHOW:+#/$SHOW}${SHOW:-its pairing code}; name it and pick what it shows in Admin → Screens."
fi
echo "    Health: cat /run/xos/health.json (room) · journalctl -u xos-$ROLE -f · xos-update: systemctl start xos-update"
