"""An Xos box in a UEFI x86 VM: `vm.py install` boots the Xos USB (XOS_USB_ISO) and presses Enter on
the menu like a person would; `vm.py boot` starts the installed disk. Screenshots every 45 s via the
QEMU monitor into $XOS_VM_DIR/shots; SSH is forwarded to 127.0.0.1:2224 (xosadmin). Needs
qemu-system-x86_64 and OVMF; make the disk first: qemu-img create -f qcow2 disk.qcow2 32G and copy
OVMF_VARS_4M.fd to vars.fd. On an ARM host Chromium needs --js-flags=--jitless in the VM (emulator only)."""
import os, socket, subprocess, sys, time
V = os.environ.get("XOS_VM_DIR", "/tmp/xos-demo")
usb = sys.argv[1] == "install"
args = ["qemu-system-x86_64", "-machine", "q35", "-m", "6144", "-smp", "6", "-accel", "tcg,thread=multi", "-cpu", "max",
    "-drive", "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd",
    "-drive", f"if=pflash,format=raw,file={V}/vars.fd",
    "-drive", f"file={V}/disk.qcow2,if=none,id=d0", "-device", "nvme,drive=d0,serial=xosdisk,bootindex=2",
    "-netdev", "user,id=n0,hostfwd=tcp:127.0.0.1:2224-:22", "-device", "e1000e,netdev=n0",
    "-vga", "std", "-display", "none", "-monitor", f"unix:{V}/mon.sock,server,nowait", "-serial", f"file:{V}/serial.log"]
if usb:
    args += ["-drive", f"file={os.environ.get("XOS_USB_ISO", V + "/demo.iso")},if=none,id=usb,format=raw,readonly=on", "-device", "qemu-xhci", "-device", "usb-storage,drive=usb,bootindex=1"]
q = subprocess.Popen(args, stdout=open(f"{V}/qemu.log", "a"), stderr=subprocess.STDOUT)
def mon(cmd):
    s = socket.socket(socket.AF_UNIX); s.connect(f"{V}/mon.sock"); s.settimeout(5)
    try: s.recv(4096)
    except Exception: pass
    s.sendall((cmd + "\n").encode()); time.sleep(0.7); s.close()
def shot(n): mon(f"screendump {V}/shots/{n}.ppm"); print(time.strftime("%H:%M:%S"), n, flush=True)
time.sleep(3)
if usb:
    for i in range(12): time.sleep(8); shot(f"a-boot-{i:02d}")
    mon("sendkey ret"); print("Enter on Xos install", flush=True)
n = 0
while q.poll() is None:
    time.sleep(45); n += 1; shot(f"b-run-{n:04d}")
