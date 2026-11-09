"""TV off must never strand a screen: a sleeping stick stays reachable, or Xos wakes it over ADB."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app, hub
from app.provision import ProvisionError

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_hub():
    hub.devices.clear()
    hub.commands.clear()
    main._waking.clear()


def _stick(dev: str, ip: str = "192.0.2.102") -> None:
    client.post(
        "/api/devices/heartbeat",
        json={
            "id": dev,
            "client": "firetv-0.5.0",
            "role": "page",
            "arg": "welcome",
            "label": "Page · welcome",
            "native": {"ip": ip, "displayOn": True},
        },
    )


def test_a_sleeping_stick_checks_in_without_forgetting_what_it_shows():
    _stick("firetv-sleep1")
    r = client.post(
        "/api/devices/heartbeat",
        json={
            "id": "firetv-sleep1",
            "client": "firetv-0.6.0",
            "native": {"ip": "192.0.2.102", "displayOn": False, "wakeable": True},
        },
    )
    d = r.json()
    assert (d["role"], d["arg"], d["label"]) == ("page", "welcome", "Page · welcome")
    assert d["native"]["displayOn"] is False and d["client"] == "firetv-0.6.0"


def _tv_on(dev: str, wake) -> dict:
    async def run():
        main.provisioner.wake = wake
        cmd = main.command_device(dev, "display_on", {})
        await asyncio.gather(*main._wake_tasks)
        return cmd

    original = main.provisioner.wake
    try:
        return asyncio.run(run())
    finally:
        main.provisioner.wake = original


def test_tv_on_wakes_an_unreachable_stick_over_adb():
    _stick("firetv-sleep2")
    hub.mark_adb("firetv-sleep2", "192.0.2.102")
    woken = []

    async def wake(ip):
        woken.append(ip)

    cmd = _tv_on("firetv-sleep2", wake)
    assert woken == ["192.0.2.102"]
    assert cmd["status"] == "pending"  # it runs when the woken stick checks back in


def test_tv_on_says_why_when_the_stick_cant_be_woken():
    _stick("firetv-sleep3")
    hub.mark_adb("firetv-sleep3", "192.0.2.102")

    async def wake(ip):
        raise ProvisionError(f"The Xos server can't reach {ip}.")

    cmd = _tv_on("firetv-sleep3", wake)
    assert cmd["status"] == "failed"
    assert "can't reach 192.0.2.102" in cmd["result"]
    assert "remote" in cmd["result"]


def test_tv_on_for_a_listening_screen_or_a_browser_skips_adb():
    _stick("firetv-awake")
    hub._device_streams["firetv-awake"] = {asyncio.Queue()}
    client.post("/api/devices/heartbeat", json={"id": "web-abc1234", "role": "page"})
    woken = []

    async def wake(ip):
        woken.append(ip)

    try:
        assert _tv_on("firetv-awake", wake)["status"] == "sent"
        _tv_on("web-abc1234", wake)
    finally:
        hub._device_streams.pop("firetv-awake", None)
    assert woken == []


def test_tv_on_never_tries_adb_on_a_stick_this_server_didnt_set_up():
    """Its key isn't allowed there, so trying would put "Allow USB debugging?" on the TV."""
    _stick("firetv-laptop")  # set up with provision.sh from a laptop
    _stick("firetv-moved", ip="192.0.2.120")
    hub.mark_adb("firetv-moved", "192.0.2.77")  # set up here, but it has a new address now
    woken = []

    async def wake(ip):
        woken.append(ip)

    assert _tv_on("firetv-laptop", wake)["status"] == "pending"
    _tv_on("firetv-moved", wake)
    assert woken == []
