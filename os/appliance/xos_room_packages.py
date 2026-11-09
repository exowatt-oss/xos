#!/usr/bin/env python3
"""Install the fixed native room-control dependencies when an older box updates.

Invoked by systemd as root before the unprivileged room agent. An already provisioned box
needs no network access. The package names and import check are fixed, with no caller input.
"""
import os
from pathlib import Path
import subprocess

PACKAGES = ("at-spi2-core", "python3-pyatspi", "python3-gi", "wtype")


def ready() -> bool:
    return Path("/usr/bin/wtype").is_file() and subprocess.run(
        ["/usr/bin/python3", "-I", "-c", "import pyatspi; from gi.repository import Gio"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0


def main() -> None:
    if ready():
        return
    if os.geteuid() != 0:
        raise RuntimeError("Native meeting dependencies must be provisioned by the room service")
    options = ["-o", "Acquire::Retries=1", "-o", "Acquire::http::Timeout=20"]
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    subprocess.run(["/usr/bin/apt-get", *options, "update", "-q"], env=env, check=False)
    subprocess.run(["/usr/bin/apt-get", *options, "install", "-y", "--no-install-recommends", *PACKAGES],
                   env=env, check=True)
    if not ready():
        raise RuntimeError("Native room dependencies were installed but could not be loaded")


if __name__ == "__main__":
    main()
