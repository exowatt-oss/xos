#!/usr/bin/env bash
# The installer and the services on a systemd-booted Debian 13 (privileged container): install as a
# room computer from this checkout, then check the services, identity, logs and a real xos-update
# cycle against a local Xos server (isolated data). The container has no seat or TTY, so a test-only
# drop-in runs Cage on wlroots' headless backend; everything else is what a box gets.
#   ./os/appliance/test/systemd_test.sh     (needs Docker with --privileged, and uv)
set -euo pipefail
cd "$(dirname "$0")/../../.."
PORT=${PORT:-8793}
DATA=$(mktemp -d /tmp/xos_test-appliance-sd-XXXX)
NAME=xos-appliance-sd-$$
docker build -q -f os/appliance/test/Dockerfile.systemd -t xos-appliance-sd . >/dev/null
XOS_DATA_DIR=$DATA XOS_DEV_USER=root@example.com uv run uvicorn app.main:app --app-dir src \
  --host 127.0.0.1 --port "$PORT" >"$DATA/server.log" 2>&1 &
SERVER_PID=$!
trap '[ -n "${KEEP:-}" ] || docker rm -f "$NAME" >/dev/null 2>&1 || true; [ -n "${KEEP:-}" ] || kill $SERVER_PID 2>/dev/null || true' EXIT
until curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null; do sleep 0.5; done
docker run -d --name "$NAME" --privileged --cgroupns=host --network host --shm-size 1g \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw -v "$PWD:/xos:ro" xos-appliance-sd >/dev/null
X() { docker exec "$NAME" "$@"; }
# "degraded" is normal in a container (no seat, no TTY); is-system-running exits non-zero for it.
until case "$(X systemctl is-system-running 2>/dev/null || true)" in running|degraded) true ;; *) false ;; esac; do sleep 1; done
pass=0; fail=0
ok() { echo "PASS $1"; pass=$((pass+1)); }
no() { echo "FAIL $1"; fail=$((fail+1)); }
t() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else no "$name"; fi; }

X mkdir -p /etc/systemd/system/xos-room.service.d
X sh -c 'cat > /etc/systemd/system/xos-room.service.d/test.conf <<CONF
[Service]
PAMName=
TTYPath=
StandardInput=null
UtmpIdentifier=
Environment=XDG_RUNTIME_DIR=/run/xos-xdg WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_LIBINPUT_NO_DEVICES=1
ExecStartPre=+/bin/sh -c "install -d -m 0700 -o xos -g xos /run/xos-xdg"
CONF'
# The container shares this machine's network: NetworkManager must never run in it (it could take
# over the host's interfaces). Masked, the package installs without starting; profiles still get written.
X systemctl mask NetworkManager.service NetworkManager-wait-online.service >/dev/null 2>&1
# A Debian installed with a desktop: an enabled login screen the installer must switch off.
X sh -c 'printf "[Unit]\nDescription=stand-in desktop login screen\n[Service]\nExecStart=/bin/sleep infinity\n[Install]\nAlias=display-manager.service\nWantedBy=graphical.target\n" > /etc/systemd/system/lightdm.service && systemctl daemon-reload && systemctl enable lightdm.service' >/dev/null 2>&1
# Wi-Fi for Xos boxes (Admin), placeholder networks only.
curl -fs -X PUT -H 'Content-Type: application/json' "http://127.0.0.1:$PORT/api/appliances/wifi" \
  -d '{"networks": [{"ssid": "Test OT", "psk": "test-ot-psk-1"}, {"ssid": "Test IOT", "psk": "test-iot-psk-2"}]}' >/dev/null
# Admin → Screens → Add a Linux box (as an Admin), then the box enrolls with its code.
BOX=$(curl -fs -X POST -H 'Content-Type: application/json' "http://127.0.0.1:$PORT/api/appliances" \
  -d '{"role": "room", "room": "atrium", "name": "Lab room computer"}')
CODE=$(echo "$BOX" | python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])')
BOXID=$(echo "$BOX" | python3 -c 'import json,sys; print(json.load(sys.stdin)["device_id"])')
PASSWORD=$(echo "$BOX" | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')
X /xos/os/appliance/install.sh --enroll "$CODE" --server "http://127.0.0.1:$PORT" --from /xos > "$DATA/install.log" 2>&1 \
  && ok "install.sh --enroll <code from Admin> (Debian 13, systemd)" || { no "install.sh --enroll"; tail -30 "$DATA/install.log"; exit 1; }
t "the setup code works only once" sh -c "! curl -fs -X POST -H 'Content-Type: application/json' http://127.0.0.1:$PORT/api/appliances/enroll -d '{\"code\": \"$CODE\"}'"
t "hostname from Admin" sh -c "docker exec $NAME cat /etc/hostname | grep -qx xos-lab-room-computer"
t "maintenance login xosadmin with Admin's password, in sudo" sh -c "docker exec $NAME perl -e '@e=getpwnam(\"xosadmin\"); exit(crypt(\"$PASSWORD\", \$e[1]) eq \$e[1] ? 0 : 1)' && docker exec $NAME id -nG xosadmin | grep -qw sudo"
REPOKEY=$(grep -m1 '^ssh-' os/appliance/ssh/authorized_keys | cut -d' ' -f2)
t "xosadmin takes the SSH keys from Xos (repo + Admin), private" sh -c "docker exec $NAME grep -q '$REPOKEY' /home/xosadmin/.ssh/authorized_keys && docker exec $NAME stat -c '%a %U' /home/xosadmin/.ssh/authorized_keys | grep -qx '600 xosadmin'"
t "SSH key sync every 10 minutes, SSH server on" sh -c "docker exec $NAME systemctl is-enabled xos-keys.timer && docker exec $NAME test -x /usr/sbin/sshd"
X sh -c 'echo "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMine0wnLaptopKeyKeptOutsideTheBlock0000000000 me@laptop" >> /home/xosadmin/.ssh/authorized_keys'
X /opt/xos/current/os/appliance/xos-keys >/dev/null
t "a key someone added on the box by hand is kept" X grep -q 'me@laptop' /home/xosadmin/.ssh/authorized_keys
t "the password isn't left in the install log" sh -c "! grep -q '$PASSWORD' $DATA/install.log"
t "device id from Admin" sh -c "docker exec $NAME grep -q $BOXID /var/lib/xos/identity.json"
t "Wi-Fi from Admin: OT preferred, then IOT" sh -c "docker exec $NAME grep -q '^ssid=Test OT' /etc/NetworkManager/system-connections/xos-wifi-1.nmconnection && docker exec $NAME grep -q '^autoconnect-priority=20' /etc/NetworkManager/system-connections/xos-wifi-1.nmconnection && docker exec $NAME grep -q '^ssid=Test IOT' /etc/NetworkManager/system-connections/xos-wifi-2.nmconnection && docker exec $NAME grep -q '^autoconnect-priority=10' /etc/NetworkManager/system-connections/xos-wifi-2.nmconnection"
t "Wi-Fi profiles root-only" sh -c "docker exec $NAME stat -c '%a %U' /etc/NetworkManager/system-connections/xos-wifi-1.nmconnection | grep -qx '600 root'"
t "Wi-Fi passwords not in the install log" sh -c "! grep -q 'test-ot-psk-1' $DATA/install.log"
t "device key kept private" sh -c "docker exec $NAME stat -c '%a %U' /var/lib/xos/device-key | grep -qx '600 xos'"

t "a desktop login screen is switched off for Xos" sh -c "! docker exec $NAME systemctl is-enabled lightdm.service && ! docker exec $NAME test -e /etc/systemd/system/display-manager.service"
t "xos-room.service enabled for boot" X systemctl is-enabled xos-room.service
t "xos-update.timer enabled" X systemctl is-enabled xos-update.timer
t "boots to graphical.target" sh -c "docker exec $NAME systemctl get-default | grep -q graphical"
t "Chromium policy: only Meet gets camera and mics" X grep -q meet.google.com /etc/chromium/policies/managed/xos-appliance.json
t "journald keeps logs across reboots" X test -d /var/log/journal
t "hardware watchdog configured" X grep -q RuntimeWatchdogSec /etc/systemd/system.conf.d/xos.conf
for i in $(seq 1 90); do X test -s /run/xos/health.json 2>/dev/null && X grep -q '"browser": "running"' /run/xos/health.json && break; sleep 1; done
t "agent up as user xos with the browser running" X grep -q '"browser": "running"' /run/xos/health.json
t "agent runs as xos, not root" sh -c "docker exec $NAME ps -o user= -C python3 | grep -qx xos"
t "identity kept in /var/lib/xos, private" sh -c "docker exec $NAME stat -c %a /var/lib/xos/identity.json | grep -qx 600"
ID=$(X python3 -c 'import json; print(json.load(open("/var/lib/xos/identity.json"))["id"])')
t "online in Admin under its name and room, with its key" sh -c "curl -fs http://127.0.0.1:$PORT/api/devices | python3 -c 'import json,sys; d=[x for x in json.load(sys.stdin) if x[\"id\"]==\"$ID\"][0]; assert d[\"role\"]==\"roompc\" and d[\"online\"] and d[\"name\"]==\"Lab room computer\" and d[\"room\"]==\"atrium\" and d[\"native\"][\"release\"].startswith(\"local-\")'"
t "a heartbeat without its key is refused" sh -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' http://127.0.0.1:$PORT/api/devices/heartbeat -d '{\"id\": \"$ID\", \"role\": \"roompc\"}')\" = 401 ]"
t "no browser debugging port" sh -c "! docker exec $NAME ss -ltnpH | grep -Eq 'chrom|cage'"

# process recovery: kill the agent; systemd brings it back with the same identity
X systemctl kill -s KILL xos-room.service
sleep 8
t "systemd restarts a killed agent" X systemctl is-active xos-room.service
t "same identity after the restart" sh -c "docker exec $NAME grep -q $ID /var/lib/xos/identity.json"

# a real update, started from Admin → Update now: the agent leaves the trigger, the path unit runs it
curl -fs -X POST -H 'Content-Type: application/json' "http://127.0.0.1:$PORT/api/devices/$ID/commands" -d '{"type": "update"}' >/dev/null
# done when xos-update has logged its outcome (it logs after the health check, not at the switch)
for i in $(seq 1 150); do X sh -c 'journalctl -u xos-update --no-pager | grep -q "xos-update: "' 2>/dev/null && break; sleep 2; done
t "Update now from Admin runs xos-update on the box" sh -c "docker exec $NAME journalctl -u xos-update --no-pager | grep -q 'xos-update: updated to'"
CUR=$(X readlink /opt/xos/current)
t "xos-update moves to the server's release (healthy)" sh -c "echo $CUR | grep -Eq 'releases/[0-9a-f]{64}$'"
for i in $(seq 1 60); do X grep -q '"browser": "running"' /run/xos/health.json 2>/dev/null && break; sleep 1; done
t "running from the new release" sh -c "docker exec $NAME grep -q \"\\\"release\\\": \\\"$(basename "$CUR" | cut -c1-12)\" /run/xos/health.json"
until [ "$(X systemctl is-active xos-update.service 2>/dev/null || true)" != activating ]; do sleep 1; done
t "xos-update again: up to date" sh -c "docker exec $NAME systemctl start xos-update.service && docker exec $NAME journalctl -u xos-update -n 3 --no-pager | grep -q 'up to date'"
t "the previous release is kept to roll back to" sh -c "docker exec $NAME ls /opt/xos/releases | grep -q '^local-'"

# a release that never reports healthy: back on the old one, restarted, and not tried again
LOCAL=$(X sh -c 'ls /opt/xos/releases | grep "^local-" | head -1')
X sh -c "ln -sfn releases/$LOCAL /opt/xos/current.new && mv -T /opt/xos/current.new /opt/xos/current && rm -f /var/lib/xos/update/bad.json"
X systemctl restart xos-room.service
for i in $(seq 1 60); do X grep -q "\"release\": \"${LOCAL:0:12}\"" /run/xos/health.json 2>/dev/null && break; sleep 1; done
set +e
X env XOS_HEALTH=/run/xos/never-written.json python3 /opt/xos/current/os/appliance/xos_update.py > "$DATA/rollback.log" 2>&1
RC=$?
set -e
t "an unhealthy release is rolled back (exit 2)" test "$RC" = 2
t "back on the previous release" sh -c "docker exec $NAME readlink /opt/xos/current | grep -q $LOCAL"
t "the bad release is remembered" X grep -q "$(basename "$CUR")" /var/lib/xos/update/bad.json
for i in $(seq 1 60); do X grep -q '"browser": "running"' /run/xos/health.json 2>/dev/null && break; sleep 1; done
t "the room computer runs again on the old release" sh -c "docker exec $NAME grep -q \"${LOCAL:0:12}\" /run/xos/health.json && docker exec $NAME systemctl is-active xos-room.service"
grep -h "xos-update" "$DATA/rollback.log" || true

echo; echo "$pass passed, $fail failed"
[ "$fail" = 0 ] || { echo "--- update log"; X journalctl -u xos-update -u xos-update-now.path --no-pager | tail -30; echo "--- agent log"; X journalctl -u xos-room -n 30 --no-pager; exit 1; }
