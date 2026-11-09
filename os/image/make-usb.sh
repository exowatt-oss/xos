#!/usr/bin/env bash
# Make the Xos USB: one stick that turns any mini PC into an Xos box.
#
#   Boot it → press Enter on "Xos install — ERASES this computer" → it installs by itself
#   (Debian 13, no desktop, Wi-Fi built in) → reboots → the TV shows the Xos pairing code →
#   Admin → Screens → + Add a screen: enter the code, name it, pick what it shows. Done.
#
#   ./os/image/make-usb.sh debian-13.x.y-amd64-netinst.iso [xos-usb.iso]
#
# Asks for (or takes from the environment, never from the command line):
#   XOS_WIFI         Wi-Fi networks, preferred first: one "SSID<TAB>password" per line. The first is
#                    the one the installer uses; the box keeps all of them (NetworkManager).
#   XOS_MAINT_PASSWORD  the maintenance login (xosadmin, in sudo) for every box made with this USB
#   XOS_TZ           timezone (default America/Denver)   XOS_SERVER  default https://xos.example.org
#
# The stick holds the Wi-Fi passwords and the maintenance password's hash: keep it with IT kit and
# remake it when they change. Never commit the image (*.iso is git-ignored). Needs xorriso, cpio,
# gzip, openssl and python3.
set -euo pipefail

ISO="${1:?usage: make-usb.sh <debian-13-amd64-netinst.iso> [output.iso]}"
OUT="${2:-xos-usb-debian13-amd64.iso}"
HERE="$(cd "$(dirname "$0")" && pwd)"
for tool in xorriso cpio gzip openssl python3; do command -v "$tool" >/dev/null || { echo "needs $tool" >&2; exit 1; }; done
SERVER="${XOS_SERVER:-https://xos.example.org}"
TZ_="${XOS_TZ:-America/Denver}"

WIFI="${XOS_WIFI:-}"
if [ -z "$WIFI" ]; then
  echo "Wi-Fi networks for Xos boxes, preferred first (empty name to finish):"
  while true; do
    read -r -p "  Network name: " s; [ -n "$s" ] || break
    read -r -s -p "  Password for $s: " p; echo
    WIFI+="$s"$'\t'"$p"$'\n'
  done
fi
MPW="${XOS_MAINT_PASSWORD:-}"
if [ -z "$MPW" ]; then
  read -r -s -p "Maintenance password for xosadmin on every box (12+ characters): " MPW; echo
fi
[ ${#MPW} -ge 12 ] || { echo "use 12 or more characters for the maintenance password" >&2; exit 2; }
HASH="$(printf '%s' "$MPW" | openssl passwd -6 -stdin)"

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
umask 077
xorriso -osirrox on -indev "$ISO" \
  -extract /install.amd/initrd.gz "$WORK/initrd.gz" \
  -extract /boot/grub/grub.cfg "$WORK/grub.cfg" \
  -extract /isolinux/txt.cfg "$WORK/txt.cfg" 2>/dev/null
chmod u+w "$WORK"/*
mkdir -p "$WORK/seed/xos"

# Everything the installer asks, answered; plus the box's Wi-Fi profiles and the finishing script.
printf '%s' "$WIFI" > "$WORK/wifi.tsv"  # the script below is python's stdin, so the networks go via a file
python3 - "$WORK/seed" "$SERVER" "$TZ_" "$HASH" "$HERE/../appliance/xos-wifi" "$WORK/wifi.tsv" <<'PY'
import json, subprocess, sys
seed, server, tz, hashed, writer, wifi_file = sys.argv[1:7]
nets = [dict(zip(("ssid", "psk"), line.split("\t", 1))) for line in open(wifi_file).read().splitlines() if line.strip()]
for n in nets:
    if not 1 <= len(n["ssid"].encode()) <= 32 or not 8 <= len(n.get("psk", "")) <= 63:
        sys.exit(f"Wi-Fi {n['ssid']!r}: name 1-32 bytes, WPA password 8-63 characters")
if nets:  # the box's own NetworkManager profiles, preferred first
    subprocess.run([sys.executable, writer, f"{seed}/xos"], input=json.dumps({"wifi": nets}),
                   text=True, check=True, stdout=subprocess.DEVNULL)
wifi = "" if not nets else f"""d-i netcfg/wireless_show_essids select manual
d-i netcfg/wireless_essid string {nets[0]['ssid']}
d-i netcfg/wireless_essid_again string {nets[0]['ssid']}
d-i netcfg/wireless_security_type select wpa
d-i netcfg/wireless_wpa string {nets[0]['psk']}
"""
open(f"{seed}/preseed.cfg", "w").write(f"""# Xos box: unattended Debian 13 install. ERASES the computer's internal disk.
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us
d-i hw-detect/load_firmware boolean true
d-i netcfg/choose_interface select auto
{wifi}d-i netcfg/get_hostname string xos
d-i netcfg/get_domain string
d-i netcfg/hostname string xos
d-i mirror/country string manual
d-i mirror/http/hostname string deb.debian.org
d-i mirror/http/directory string /debian
d-i mirror/http/proxy string
d-i clock-setup/utc boolean true
d-i time/zone string {tz}
d-i clock-setup/ntp boolean true
d-i passwd/root-login boolean false
d-i passwd/user-fullname string Xos maintenance
d-i passwd/username string xosadmin
d-i passwd/user-password-crypted password {hashed}
# The internal disk: the first disk that isn't this USB stick.
d-i partman/early_command string USB=$(mount | awk '$3=="/cdrom"{{print $1}}' | sed 's/p\\?[0-9]*$//'); DISK=$(list-devices disk | grep -v "^$USB$" | head -n1); debconf-set partman-auto/disk "$DISK"; debconf-set grub-installer/bootdev "$DISK"
d-i partman-auto/method string regular
d-i partman-auto/choose_recipe select atomic
d-i partman-lvm/device_remove_lvm boolean true
d-i partman-md/device_remove_md boolean true
d-i partman-lvm/confirm boolean true
d-i partman-lvm/confirm_nooverwrite boolean true
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true
d-i partman-efi/non_efi_system boolean true
d-i apt-setup/cdrom/set-first boolean false
d-i apt-setup/disable-cdrom-entries boolean true
d-i apt-setup/non-free-firmware boolean true
d-i apt-setup/services-select multiselect security, updates
tasksel tasksel/first multiselect standard, ssh-server
d-i pkgsel/include string curl ca-certificates python3 sudo network-manager wpasupplicant
d-i pkgsel/upgrade select full-upgrade
popularity-contest popularity-contest/participate boolean false
d-i grub-installer/only_debian boolean true
d-i grub-installer/with_other_os boolean true
d-i preseed/late_command string sh /xos/late.sh
d-i cdrom-detect/eject boolean true
d-i finish-install/reboot_in_progress note
""")
# Finishing: Wi-Fi profiles, then Xos as a display that boots to its pairing code. If the network
# isn't there now, xos-install.service tries again at every boot until it is.
open(f"{seed}/xos/late.sh", "w").write(f"""#!/bin/sh
mkdir -p /target/etc/NetworkManager/system-connections
for f in /xos/xos-wifi-*.nmconnection; do [ -e "$f" ] && cp "$f" /target/etc/NetworkManager/system-connections/; done
chmod 600 /target/etc/NetworkManager/system-connections/xos-wifi-* 2>/dev/null
cat > /target/etc/systemd/system/xos-install.service <<'UNIT'
[Unit]
Description=Install Xos from the Xos server (first boot, if the installer couldn't)
Wants=network-online.target
After=network-online.target
ConditionPathExists=!/opt/xos/current
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'curl -fsSL {server}/os/appliance/install.sh | bash -s -- --role display --server {server}'
Restart=on-failure
RestartSec=30
[Install]
WantedBy=multi-user.target
UNIT
in-target systemctl enable xos-install.service
in-target sh -c 'curl -fsSL {server}/os/appliance/install.sh | bash -s -- --role display --server {server} --no-start' > /target/var/log/xos-install.log 2>&1 || true
""")
PY

gunzip "$WORK/initrd.gz"
(cd "$WORK/seed" && find preseed.cfg xos | cpio -H newc -o -A -F "$WORK/initrd" 2>/dev/null)
gzip -9 "$WORK/initrd"

# Boot menus: the Xos install first. It's not chosen on a timer: someone presses Enter, because it
# erases the computer. auto + critical: no questions.
ARGS="auto=true priority=critical vga=788"
python3 - "$WORK/grub.cfg" "$WORK/txt.cfg" "$ARGS" <<'PY'
import sys
grub, txt, args = sys.argv[1:4]
label = "Xos install (ERASES this computer)"
g = open(grub).read()
g = g.replace("set timeout=", "# set timeout=")
entry = (f"menuentry --hotkey=x '{label}' --id xos-install {{\n    set background_color=black\n"
         f"    linux    /install.amd/vmlinuz {args} --- quiet\n    initrd   /install.amd/initrd.gz\n}}\n")
# A computer that already has Xos (the stick left in after the install, or plugged in again): start
# it after 5 s instead of waiting at the menu. A computer without Xos waits for Enter on the install.
entry = ("set timeout=-1\n"
         "if search --no-floppy --file --set=xos_disk /etc/xos/appliance.json; then\n"
         "    set default=xos-start\n    set timeout=5\n"
         "    menuentry 'Start Xos (already installed on this computer)' --id xos-start {\n"
         "        search --no-floppy --file --set=root /EFI/debian/grubx64.efi\n"
         "        chainloader /EFI/debian/grubx64.efi\n    }\n"
         "fi\n") + entry
first = g.find("menuentry")
# Debian's own entries (normal install, rescue, …) go into a submenu, so "Xos install" is the one
# obvious choice: picking "Graphical install" by mistake gives a desktop, not an Xos box.
open(grub, "w").write(g[:first] + entry + "submenu 'Debian installer (not Xos) ...' {\n" + g[first:] + "\n}\n")
t = open(txt).read()
open(txt, "w").write(f"default xos\nlabel xos\n\tmenu label ^{label}\n\tmenu default\n"
                     f"\tkernel /install.amd/vmlinuz\n\tappend {args} initrd=/install.amd/initrd.gz --- quiet\n"
                     + t.replace("menu default", "").replace("default install", ""))
PY

rm -f "$OUT"  # xorriso won't overwrite an existing image
xorriso -indev "$ISO" -outdev "$OUT" -boot_image any replay \
  -map "$WORK/initrd.gz" /install.amd/initrd.gz \
  -map "$WORK/grub.cfg" /boot/grub/grub.cfg \
  -map "$WORK/txt.cfg" /isolinux/txt.cfg 2>/dev/null
chmod 600 "$OUT"
NETS="$(printf '%s' "$WIFI" | cut -f1 | paste -sd, - | sed 's/,/, then /g')"
echo "Made $OUT (Wi-Fi: ${NETS:-none, use Ethernet}; timezone $TZ_)."
echo "Write it to a USB stick (balenaEtcher or dd). On a box: boot it, press Enter on 'Xos install',"
echo "wait for the pairing code on the TV, then Admin → Screens → + Add a screen."
