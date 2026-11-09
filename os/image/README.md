# Xos appliance image (sketch, untested)

`mkosi` builds a bootable Debian 13 amd64 disk image with the Xos appliance installed for one
role, from this checkout. It's the step after the install-on-Debian prototype
(`os/appliance/install.sh`, see `README.md`).

```bash
pipx install mkosi                       # mkosi 25+, on an amd64 Linux host
cd os/image
mkosi --environment=XOS_ROLE=room --environment=XOS_ROOM=atrium build
# → xos-appliance.raw: write it to the mini PC's SSD (or a USB stick) with dd / Raspberry Pi Imager
```

Not yet:
- **A/B updates of the whole OS** with `systemd-sysupdate`: two `/usr` partitions with dm-verity, and boot counting so a bad image boots the previous one. Until then, `xos-update` rolls back the Xos layer and `unattended-upgrades` keeps Debian patched.
- **Full-disk encryption with TPM2 unlock:** `systemd-repart` + `systemd-cryptenroll`.
- **Enrollment without a per-room image:** role and room chosen on first boot.

Status: `mkosi summary` (mkosi 25.3) parses this configuration without errors. It hasn't been built: it needs an amd64 host or binfmt emulation, and the
devbox is arm64.
