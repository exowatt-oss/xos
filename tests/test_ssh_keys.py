"""SSH keys for every Xos box's maintenance login: the ones in Git plus the ones added in Admin."""

import pytest
from fastapi.testclient import TestClient

from app.appliances import ssh_keys_in
from app.main import app, ssh_keys

client = TestClient(app)
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFmQ3Zy0p9u8kq7c9V3l1b3qH8xg2vB7n6m5c4x3z2y1 me@laptop"


@pytest.fixture(autouse=True)
def clean():
    ssh_keys.keys = []
    yield
    ssh_keys.keys = []


def test_the_repo_keys_are_public_keys_only():
    from app.main import _repo_ssh_keys

    repo = _repo_ssh_keys()
    # The checked-in file ships empty (comments only); whatever an operator adds must be public.
    assert all(k.startswith("ssh-") for k in repo)
    assert "PRIVATE" not in "".join(repo)


def test_admin_adds_keys_and_every_box_gets_them_with_the_repo_keys():
    r = client.put("/api/appliances/ssh-keys", json={"keys": f"# laptop\n{KEY}\n\n{KEY}\n"})
    assert r.status_code == 200 and r.json()["keys"] == [KEY]  # comments dropped, no duplicates
    text = client.get("/os/appliance/authorized_keys").text
    lines = text.splitlines()
    assert KEY in lines and len(lines) == len(set(lines))
    assert all(line.startswith(("ssh-", "ecdsa-", "sk-")) for line in lines)


@pytest.mark.parametrize(
    "bad",
    [
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        'command="rm -rf /" ' + KEY,  # options would run on the box: never
        "ssh-ed25519 short",
        "hello",
    ],
)
def test_only_plain_public_keys(bad):
    with pytest.raises(ValueError):
        ssh_keys_in(bad)
    assert client.put("/api/appliances/ssh-keys", json={"keys": bad}).status_code == 400


def test_editing_is_team_only(monkeypatch):
    import app.access as access

    monkeypatch.setattr(access.settings, "XOS_DEV_USER", "")
    assert client.put("/api/appliances/ssh-keys", json={"keys": KEY}).status_code in (401, 403)
    assert client.get("/os/appliance/authorized_keys").status_code == 200  # boxes read it
