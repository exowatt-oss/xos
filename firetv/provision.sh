#!/usr/bin/env bash
# Set up a Fire TV Stick as an Xos screen in one step and prove it works.
#
#   firetv/provision.sh <fire-tv-ip | adb-serial> [screen] [server] [--reboot-test]
#
#   screen   kiosk route without "#/", default setup: the stick shows a pairing code; enter it in
#            Admin → Devices → Add a screen to name it and choose what it shows. Or give one directly:
#            tv/<room>, tv/<room>/2, panel/<room>, signage/<site>, wall, page/<page-id>
#   server   default https://xos.example.org (or a Pi hub such as 192.0.2.40:7000)
#
# On the stick first: Settings → My Fire TV → Developer Options → ADB debugging = On
# (About → click the device name 7 times to show Developer Options), then accept
# "Allow USB debugging?" on the TV when this script connects.
#
# What it does: installs Xos TV, grants it permission to open itself (boot, return timer),
# enables display control (sleep/wake for Admin → Devices), points it at the screen, opens it,
# then checks it is on top and saves a screenshot.
set -euo pipefail

cd "$(dirname "$0")"
TARGET="${1:?usage: provision.sh <fire-tv-ip|adb-serial> [screen] [server] [--reboot-test]}"
SCREEN="${2:-setup}"
SERVER="${3:-https://xos.example.org}"
REBOOT_TEST=false
for a in "$@"; do [ "$a" = "--reboot-test" ] && REBOOT_TEST=true; done
[ "$SCREEN" = "--reboot-test" ] && SCREEN=setup
[ "$SERVER" = "--reboot-test" ] && SERVER=https://xos.example.org
PKG=com.example.xos.tv
APK=xos-tv.apk
[ -f build/xos-tv.apk ] && [ build/xos-tv.apk -nt xos-tv.apk ] && APK=build/xos-tv.apk

# An IP (with or without :port) is a network ADB target; anything else is an adb serial.
SERIAL="$TARGET"
if [[ "$TARGET" =~ ^[0-9.]+(:[0-9]+)?$ ]]; then
  [[ "$TARGET" == *:* ]] || SERIAL="$TARGET:5555"
  echo "→ Connecting to $SERIAL (accept \"Allow USB debugging?\" on the TV)…"
  adb connect "$SERIAL" >/dev/null || true
fi
ADB=(adb -s "$SERIAL")

for _ in $(seq 1 60); do
  [ "$("${ADB[@]}" get-state 2>/dev/null)" = "device" ] && break
  sleep 2
done
[ "$("${ADB[@]}" get-state 2>/dev/null)" = "device" ] || { echo "✗ $SERIAL not authorised — accept the prompt on the TV and rerun."; exit 1; }

prop() { "${ADB[@]}" shell getprop "$1" | tr -d '\r'; }
SDK=$(prop ro.build.version.sdk)
echo "✓ Connected: $(prop ro.product.manufacturer) $(prop ro.product.model) · $(prop ro.build.version.name || true) · Android $(prop ro.build.version.release) (SDK $SDK)"

on_top() { "${ADB[@]}" shell dumpsys activity activities | tr -d '\r' | grep -E "mResumedActivity|topResumedActivity|ResumedActivity:" | grep -q "$PKG"; }
wait_on_top() {
  for _ in $(seq 1 "$1"); do on_top && return 0; sleep 2; done
  return 1
}

echo "→ Installing ${APK}…"
"${ADB[@]}" install -r "$APK" >/dev/null
echo "✓ Installed Xos TV $("${ADB[@]}" shell dumpsys package $PKG | tr -d '\r' | grep -m1 versionName | sed 's/.*=//')"

if [ "$SDK" -ge 29 ]; then
  "${ADB[@]}" shell appops set $PKG SYSTEM_ALERT_WINDOW allow
  "${ADB[@]}" shell appops write-settings 2>/dev/null || true # save now; Android otherwise writes it lazily
  echo "✓ Auto-start permission: $("${ADB[@]}" shell appops get $PKG SYSTEM_ALERT_WINDOW | tr -d '\r')"
else
  echo "✓ Auto-start permission: not needed on Android $SDK"
fi

# "Display off/on" from Admin → Devices: a device admin with only the force-lock policy.
if "${ADB[@]}" shell dpm set-active-admin $PKG/.AdminReceiver >/dev/null 2>&1 \
   || "${ADB[@]}" shell dpm list-owners 2>/dev/null | grep -q AdminReceiver \
   || "${ADB[@]}" shell dumpsys device_policy | grep -q "$PKG/.AdminReceiver"; then
  echo "✓ Display control: enabled"
else
  echo "! Display control: Fire OS refused the device admin; display off/on from Admin won't work"
fi
# While the TV is off Xos TV keeps a connection open so Xos can turn it back on; exempt it from idle limits.
"${ADB[@]}" shell dumpsys deviceidle whitelist +$PKG >/dev/null 2>&1 || true

echo "→ Opening #/$SCREEN from ${SERVER}…"
"${ADB[@]}" shell am start -n $PKG/.MainActivity --es server "$SERVER" --es route "'#/$SCREEN'" \
  --ez boot true --ei return_min 2 >/dev/null
wait_on_top 15 && echo "✓ Xos TV is on screen" || { echo "✗ Xos TV did not come to the front"; exit 1; }
sleep 8
SHOT="build/provision-${SERIAL//[:.]/-}.png"
mkdir -p build && "${ADB[@]}" exec-out screencap -p > "$SHOT" && echo "✓ Screenshot: firetv/$SHOT"

if $REBOOT_TEST; then
  # Android records "app has been launched" lazily; reboot too soon and it still counts as never
  # launched, so the boot broadcast is skipped. Wait until the stopped flag is cleared on disk.
  echo "→ Reboot test: waiting for Android to save the launch, then rebooting…"
  for _ in $(seq 1 30); do
    "${ADB[@]}" shell dumpsys package $PKG | tr -d '\r' | grep -q "stopped=false" && break; sleep 2
  done
  sleep 20; "${ADB[@]}" shell sync
  "${ADB[@]}" reboot
  sleep 10
  [[ "$SERIAL" == *:* ]] && for _ in $(seq 1 60); do adb connect "$SERIAL" >/dev/null 2>&1 || true; [ "$("${ADB[@]}" get-state 2>/dev/null)" = "device" ] && break; sleep 3; done
  "${ADB[@]}" wait-for-device
  for _ in $(seq 1 90); do [ "$("${ADB[@]}" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && break; sleep 2; done
  if wait_on_top 60; then echo "✓ Xos TV opened by itself after reboot"; else echo "✗ Xos TV did not open after reboot"; exit 1; fi
  sleep 8; "${ADB[@]}" exec-out screencap -p > "${SHOT%.png}-after-reboot.png" && echo "✓ Screenshot: firetv/${SHOT%.png}-after-reboot.png"
fi

echo "Done. The stick shows Xos on boot and returns to it 2 min after anyone leaves (Menu → settings)."
