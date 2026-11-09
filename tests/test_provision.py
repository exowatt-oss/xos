import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app, hub
from app.provision import ProvisionError, check_ip

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_hub():
    hub.devices.clear()
    hub.commands.clear()
    main.provisioner.jobs.clear()


def test_only_office_network_addresses():
    assert check_ip(" 192.0.2.102 ") == "192.0.2.102"
    for bad in ["8.8.8.8", "127.0.0.1", "169.254.1.1", "nope", ""]:
        with pytest.raises(ProvisionError):
            check_ip(bad)
    r = client.post("/api/provision", json={"ip": "8.8.8.8"})
    assert r.status_code == 400
    assert "office network" in r.json()["detail"]


def test_unreachable_screen_fails_with_a_clear_reason(monkeypatch, tmp_path):
    # No APK is checked in; stand in a file so provisioning gets as far as the network.
    apk = tmp_path / "xos-tv.apk"
    apk.write_bytes(b"PK")
    monkeypatch.setattr(main.provisioner, "apk", apk)

    async def run():
        async def never(*_):
            return None

        job = main.provisioner.start("198.51.100.1", "Nowhere", "", never)
        for _ in range(60):
            if job["state"] != "running":
                break
            await asyncio.sleep(0.2)
        return job

    job = asyncio.run(run())
    assert job["state"] == "failed"
    assert "can't reach 198.51.100.1" in job["error"]
    assert client.get("/api/provision").json()[0]["id"] == job["id"]


def test_a_new_screen_is_named_and_starts_on_the_story():
    job = {"ip": "192.0.2.50", "name": "Kitchen", "started": time.time() - 1}
    hub.heartbeat({"id": "firetv-new1", "role": "setup", "native": {"ip": "192.0.2.50"}})
    code = hub.pair_code("firetv-new1")
    found = asyncio.run(main._provisioned(job, {"had_xos": False}, "", 2))
    assert found == "firetv-new1"
    assert hub.devices["firetv-new1"]["name"] == "Kitchen"
    assert hub.commands["firetv-new1"][-1]["args"] == {"route": main.DEFAULT_ROUTE}
    assert code not in hub.pair_codes


def test_a_screen_that_had_xos_keeps_what_it_shows():
    job = {"ip": "192.0.2.51", "name": "", "started": time.time() - 1}
    hub.heartbeat({"id": "firetv-old1", "role": "page", "native": {"ip": "192.0.2.51"}})
    assert asyncio.run(main._provisioned(job, {"had_xos": True}, "", 2)) == "firetv-old1"
    assert not hub.commands.get("firetv-old1")
