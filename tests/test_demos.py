import json
import struct
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import access
from app.demos import Demos, build_router, validate_glb


@pytest.fixture
def studio(tmp_path):
    from fastapi import Depends, Header

    store = Demos(tmp_path)

    class Media:
        def get(self, aid):
            return (
                {
                    "id": "clip",
                    "name": "Clip",
                    "kind": "video",
                    "type": "video/mp4",
                    "url": "/media/clip.mp4",
                }
                if aid == "clip"
                else None
            )

        def all(self):
            return [self.get("clip")]

    def requires(perm):
        def check(role: str = Header(default="admin", alias="X-Test-Role")):
            caller = (
                access.caller_as(role, "tester@example.com")
                if role != "anonymous"
                else access.Caller()
            )
            access.need(caller, perm)
            return caller

        return Depends(check)

    app = FastAPI()
    app.include_router(build_router(store, Media(), requires))
    return store, TestClient(app), tmp_path


def enroll(client, device="quest-12345", key="a" * 48, unit="hq-01"):
    r = client.post("/api/demos/enroll", json={"id": device, "key": key})
    assert r.status_code == 200
    if unit:
        assert (
            client.post(
                "/api/demos/pair", json={"code": r.json()["code"], "unit_id": unit}
            ).status_code
            == 200
        )
    return {"X-Xos-Quest": device, "X-Xos-Key": key}


def demo(client, aid="clip"):
    r = client.put(
        "/api/demos/library/example",
        json={
            "name": "Tour",
            "description": "Guided",
            "steps": [{"title": "Opening", "asset_id": aid}, {"title": "Next", "asset_id": aid}],
        },
    )
    assert r.status_code == 200
    return r.json()


def test_twelve_slots_and_enrollment_is_not_control_authority(studio):
    store, client, directory = studio
    assert len(store.data["units"]) == 12
    assert sum(u["site"] == "annex" for u in store.data["units"].values()) == 6
    h = enroll(client, unit=None)
    assert client.get("/api/demos/headset/state", headers=h).json()["unit"] is None
    assert (
        client.post(
            "/api/demos/pair",
            headers={"X-Test-Role": "member"},
            json={"code": "123456", "unit_id": "annex-01"},
        ).status_code
        == 403
    )
    assert (
        client.post("/api/demos/enroll", json={"id": "quest-12345", "key": "b" * 48}).status_code
        == 401
    )
    assert client.get("/api/demos/headset/state").status_code == 401
    assert client.get("/api/demos/studio", headers={"X-Test-Role": "anonymous"}).status_code == 401
    public = client.get("/api/demos/studio").text
    assert "key_hash" not in public and "a" * 48 not in public
    assert len(Demos(directory).data["devices"]) == 1


def test_readiness_gate_common_clock_and_guided_steps(studio, monkeypatch):
    from app import demos as mod

    store, client, _ = studio
    h1 = enroll(client)
    h2 = enroll(client, "quest-second", "b" * 48, "annex-01")
    demo(client)
    now = [10000.0]
    monkeypatch.setattr(mod.time, "time", lambda: now[0])
    r = client.post(
        "/api/demos/sessions", json={"demo_id": "example", "unit_ids": ["hq-01", "annex-01"]}
    )
    sid = r.json()["id"]
    state = client.get("/api/demos/headset/state", headers=h1).json()
    token = state["session"]["step_token"]
    assert state["session"]["preparing"] and not state["session"]["playing"]
    beat = {"state": "ready", "ready_token": token}
    client.post("/api/demos/headset/heartbeat", headers=h1, json=beat)
    assert not store.data["sessions"][sid]["playing"]
    r = client.post("/api/demos/headset/heartbeat", headers=h2, json=beat)
    session = r.json()["session"]
    assert session["playing"] and session["anchor_at"] == 10002
    assert Demos.position(session, 10001) == 0
    assert Demos.position(session, 10006) == 4
    now[0] = 10007
    paused = client.post("/api/demos/sessions/" + sid + "/control", json={"action": "pause"}).json()
    assert paused["position"] == 5 and not paused["playing"]
    next_step = client.post(
        "/api/demos/sessions/" + sid + "/control", json={"action": "next"}
    ).json()
    assert next_step["step_index"] == 1 and next_step["preparing"]
    new_state = client.get("/api/demos/headset/state", headers=h1).json()
    assert new_state["session"]["step_token"] != token
    client.post("/api/demos/headset/heartbeat", headers=h1, json=beat)
    client.post("/api/demos/headset/heartbeat", headers=h2, json=beat)
    assert not store.data["sessions"][sid]["playing"]


def test_disable_unpair_and_reenable_do_not_resume_unexpectedly(studio):
    store, client, _ = studio
    h = enroll(client)
    demo(client)
    sid = client.post(
        "/api/demos/sessions", json={"demo_id": "example", "unit_ids": ["hq-01"]}
    ).json()["id"]
    client.put("/api/demos/enabled", json={"enabled": False})
    assert client.get("/api/demos/headset/state", headers=h).json()["session"] is None
    assert not store.data["sessions"][sid]["preparing"]
    assert (
        client.post("/api/demos/sessions/" + sid + "/control", json={"action": "play"}).status_code
        == 409
    )
    client.put("/api/demos/enabled", json={"enabled": True})
    client.patch("/api/demos/units/hq-01", json={"enabled": False})
    client.patch("/api/demos/units/hq-01", json={"enabled": True})
    assert client.get("/api/demos/headset/state", headers=h).json()["session"] is None
    client.delete("/api/demos/units/hq-01/pair")
    assert client.get("/api/demos/headset/state", headers=h).status_code == 401


def test_preview_is_keyed_transient_expiring_and_viewer_only(studio, monkeypatch):
    from app import demos as mod

    store, client, _ = studio
    h = enroll(client)
    now = [100.0]
    monkeypatch.setattr(mod.time, "time", lambda: now[0])
    jpeg = b"\xff\xd8\xff" + b"test"
    assert client.post("/api/demos/headset/preview", content=jpeg).status_code == 401
    assert client.post("/api/demos/headset/preview", headers=h, content=jpeg).status_code == 200
    assert client.get("/api/demos/units/hq-01/preview").content == jpeg
    assert (
        client.get("/api/demos/units/hq-01/preview", headers={"X-Test-Role": "member"}).status_code
        == 403
    )
    assert not Demos(studio[2]).previews
    now[0] = 116
    assert client.get("/api/demos/units/hq-01/preview").status_code == 204
    assert (
        client.post("/api/demos/headset/preview", headers=h, content=b"x" * 600_001).status_code
        == 413
    )


def glb(doc):
    raw = json.dumps(doc).encode()
    raw += b" " * (-len(raw) % 4)
    return struct.pack("<4sIIII", b"glTF", 2, 20 + len(raw), len(raw), 0x4E4F534A) + raw


def test_glb_upload_validation_quota_and_content_role(studio, monkeypatch):
    import app.demos as mod

    store, client, directory = studio
    good = glb({"asset": {"version": "2.0"}, "scenes": [], "buffers": []})
    r = client.post(
        "/api/demos/assets?filename=scene.glb",
        content=good,
        headers={"X-Test-Role": "content_editor"},
    )
    assert r.status_code == 200
    item = r.json()
    assert item["kind"] == "model"
    assert (directory / "media" / "quest" / Path(item["url"]).name).exists()
    assert (
        client.post(
            "/api/demos/assets?filename=x.glb",
            content=good,
            headers={"X-Test-Role": "screen_operator"},
        ).status_code
        == 403
    )
    for bad in [
        b"bad",
        glb({"images": [{"uri": "https://remote/image.png"}]}),
        glb({"extensionsRequired": ["KHR_draco_mesh_compression"]}),
    ]:
        assert client.post("/api/demos/assets?filename=bad.glb", content=bad).status_code == 400
    assert len(store.data["assets"]) == 1
    monkeypatch.setattr(mod, "MAX_FILE", 3)
    assert client.post("/api/demos/assets?filename=huge.mp4", content=b"1234").status_code == 413
    demo(client, item["id"])
    assert client.delete("/api/demos/assets/" + item["id"]).status_code == 409


def test_busy_demo_edit_is_rejected_and_pair_code_cannot_be_reused(studio):
    store, client, _ = studio
    r = client.post("/api/demos/enroll", json={"id": "quest-12345", "key": "a" * 48}).json()
    assert (
        client.post("/api/demos/pair", json={"code": r["code"], "unit_id": "hq-01"}).status_code
        == 200
    )
    assert (
        client.post("/api/demos/pair", json={"code": r["code"], "unit_id": "annex-01"}).status_code
        == 404
    )
    d = demo(client)
    client.post("/api/demos/sessions", json={"demo_id": "example", "unit_ids": ["hq-01"]})
    assert client.put("/api/demos/library/example", json=d).status_code == 409
    assert client.delete("/api/demos/library/example").status_code == 409


def test_glb_parser_rejects_bad_header(tmp_path):
    p = tmp_path / "x.glb"
    p.write_bytes(b"nope")
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        validate_glb(p)


def test_hidden_headset_holds_start_and_disabled_headset_does_not(studio):
    store, client, _ = studio
    h1 = enroll(client)
    h2 = enroll(client, "quest-second", "b" * 48, "annex-01")
    demo(client)
    sid = client.post(
        "/api/demos/sessions", json={"demo_id": "example", "unit_ids": ["hq-01", "annex-01"]}
    ).json()["id"]
    token = client.get("/api/demos/headset/state", headers=h1).json()["session"]["step_token"]
    beat = {"state": "ready", "ready_token": token}
    client.post("/api/demos/headset/heartbeat", headers=h1, json={**beat, "visibility": "hidden"})
    client.post("/api/demos/headset/heartbeat", headers=h2, json=beat)
    assert store.data["sessions"][sid]["preparing"]
    client.patch("/api/demos/units/hq-01", json={"enabled": False})
    client.post("/api/demos/headset/heartbeat", headers=h2, json=beat)
    assert store.data["sessions"][sid]["playing"]


def test_operator_can_disable_but_cannot_pair_or_rename(studio):
    _, client, _ = studio
    h = {"X-Test-Role": "screen_operator"}
    assert (
        client.patch("/api/demos/units/hq-01", headers=h, json={"enabled": False}).status_code
        == 200
    )
    assert (
        client.patch("/api/demos/units/hq-01", headers=h, json={"name": "Changed"}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/demos/pair", headers=h, json={"code": "123456", "unit_id": "hq-01"}
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        {"asset": []},
        {"asset": {"version": "2.0"}, "images": [{"uri": 42}]},
        {"asset": {"version": "2.0"}, "extensionsRequired": [{}]},
        {"asset": {"version": "2.0"}, "buffers": {}},
        {"asset": {"version": "2.0"}, "images": [{"uri": "https://external/texture"}]},
        {"asset": {"version": "2.0"}, "extensionsRequired": ["KHR_draco_mesh_compression"]},
    ],
)
def test_malformed_glb_is_a_client_error(studio, metadata):
    _, client, _ = studio
    assert client.post("/api/demos/assets?filename=x.glb", content=glb(metadata)).status_code == 400


def test_expired_pairing_code_rotates_and_old_code_stays_invalid(studio):
    store, client, _ = studio
    body = {"id": "quest-expiring", "key": "a" * 48}
    old = client.post("/api/demos/enroll", json=body).json()["code"]
    store.data["devices"][body["id"]]["created"] -= 901
    new = client.post("/api/demos/enroll", json=body).json()["code"]
    assert old != new
    assert client.post("/api/demos/pair", json={"code": old, "unit_id": "hq-01"}).status_code == 404
    assert client.post("/api/demos/pair", json={"code": new, "unit_id": "hq-01"}).status_code == 200


def test_headset_reports_vr_support_for_setup_checks(studio):
    _, client, _ = studio
    headers = enroll(client)
    client.post(
        "/api/demos/headset/heartbeat",
        headers=headers,
        json={"vr_supported": True, "immersive": False, "visibility": "visible"},
    )
    unit = client.get("/api/demos/studio").json()["units"][0]
    assert unit["online"] and unit["telemetry"]["vr_supported"] is True
    assert unit["telemetry"]["immersive"] is False


def test_short_quest_link_does_not_need_a_person_to_sign_in(monkeypatch):
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    response = TestClient(app).get("/quest", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/app/quest/"


def test_recovery_requires_live_headsets_expires_and_accepts_only_own_ack(studio, monkeypatch):
    from app import demos as mod

    store, client, _ = studio
    h1 = enroll(client)
    h2 = enroll(client, "quest-other", "b" * 48, "annex-01")
    body = {"unit_ids": ["hq-01"], "action": "recenter"}
    assert client.post("/api/demos/recovery", json=body).status_code == 409
    client.post("/api/demos/headset/heartbeat", headers=h1, json={})
    assert client.post("/api/demos/recovery", json={**body, "action": "reboot"}).status_code == 422
    command = client.post("/api/demos/recovery", json=body).json()["commands"][0]
    ack = {"command_acks": [{"id": command["id"], "status": "applied"}]}
    client.post("/api/demos/headset/heartbeat", headers=h2, json=ack)
    assert client.get("/api/demos/headset/state", headers=h1).json()["commands"]
    client.post("/api/demos/headset/heartbeat", headers=h1, json=ack)
    assert not client.get("/api/demos/headset/state", headers=h1).json()["commands"]
    assert store.overview(lambda _: None)["units"][0]["commands"][-1]["status"] == "applied"
    client.post("/api/demos/recovery", json=body)
    now = mod.time.time()
    monkeypatch.setattr(mod.time, "time", lambda: now + 21)
    assert not client.get("/api/demos/headset/state", headers=h1).json()["commands"]
    assert store.overview(lambda _: None)["units"][0]["commands"][-1]["status"] == "expired"
    # A scene retry may finish after the delivery deadline; accept its authenticated result.
    late = store.commands["quest-12345"][-1]["id"]
    client.post(
        "/api/demos/headset/heartbeat",
        headers=h1,
        json={"command_acks": [{"id": late, "status": "applied"}]},
    )
    assert store.commands["quest-12345"][-1]["status"] == "applied"
    assert not Demos(studio[2]).commands


def test_recovery_bulk_is_atomic_and_settings_persist_with_application_revision(studio):
    store, client, directory = studio
    h = enroll(client)
    client.post("/api/demos/headset/heartbeat", headers=h, json={})
    body = {"unit_ids": ["hq-01", "annex-01"], "action": "reload_app"}
    assert client.post("/api/demos/recovery", json=body).status_code == 409
    assert not store.commands
    settings = {"unit_ids": ["hq-01"], "settings": {"volume": 0.3, "preview_enabled": False}}
    assert client.put("/api/demos/settings", json=settings).status_code == 200
    assert (
        client.put("/api/demos/settings", json={**settings, "settings": {"volume": 2}}).status_code
        == 422
    )
    saved = Demos(directory).settings("hq-01")
    assert saved["revision"] == 2 and saved["values"]["volume"] == 0.3
    assert saved["values"]["preview_enabled"] is False
    state = client.post(
        "/api/demos/headset/heartbeat",
        headers=h,
        json={"settings_revision": 2, "app_mode": "installed"},
    ).json()
    assert state["settings"] == saved
    unit = client.get("/api/demos/studio").json()["units"][0]
    assert unit["telemetry"]["settings_revision"] == 2
    assert unit["telemetry"]["app_mode"] == "installed"
    for role in ("member", "content_editor", "anonymous"):
        headers = {"X-Test-Role": role}
        assert client.post(
            "/api/demos/recovery",
            headers=headers,
            json={"unit_ids": ["hq-01"], "action": "recenter"},
        ).status_code in (401, 403)
        assert client.put("/api/demos/settings", headers=headers, json=settings).status_code in (
            401,
            403,
        )


def test_disabling_preview_blocks_both_capture_and_last_image(studio):
    _, client, _ = studio
    headers = enroll(client)
    jpeg = b"\xff\xd8\xffimage"
    assert (
        client.post("/api/demos/headset/preview", headers=headers, content=jpeg).status_code == 200
    )
    client.put(
        "/api/demos/settings",
        json={"unit_ids": ["hq-01"], "settings": {"preview_enabled": False}},
    )
    assert client.get("/api/demos/units/hq-01/preview").status_code == 204
    assert (
        client.post("/api/demos/headset/preview", headers=headers, content=jpeg).status_code == 403
    )


def test_quest_installer_is_public_and_no_apk_is_shipped(monkeypatch):
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "XOS_DEV_USER", "")
    client = TestClient(app)
    guide = client.get("/quest/install")
    assert guide.status_code == 200 and "adb install -r" in guide.text
    # Signed APKs and the Android app-links association are deployment-specific: not served.
    assert client.get("/quest/xos-quest.apk").status_code == 404
    assert client.get("/.well-known/assetlinks.json").status_code == 404


def test_headsets_get_every_demo_file_and_report_what_they_saved(studio):
    store, client, _ = studio
    h = enroll(client)
    assert client.get("/api/demos/headset/state", headers=h).json()["library"] == []
    demo(client)
    library = client.get("/api/demos/headset/state", headers=h).json()["library"]
    # Two steps use the same clip: the headset downloads it once.
    assert library == [{"id": "clip", "url": "/media/clip.mp4", "bytes": 0, "kind": "video"}]
    beat = {
        "cached": ["clip"],
        "download": {"id": "xr-ride01", "received": 10, "total": 40},
        "duration": 196.0,
        "local": True,
    }
    assert client.post("/api/demos/headset/heartbeat", headers=h, json=beat).status_code == 200
    units = client.get("/api/demos/studio").json()["units"]
    unit = next(u for u in units if u["id"] == "hq-01")
    assert unit["telemetry"]["cached"] == ["clip"]
    assert unit["telemetry"]["download"] == {"id": "xr-ride01", "received": 10, "total": 40}
    assert unit["telemetry"]["duration"] == 196.0 and unit["telemetry"]["local"] is True
    bad = {"cached": ["../../etc"]}
    assert client.post("/api/demos/headset/heartbeat", headers=h, json=bad).status_code == 422
    # An unpaired headset gets no file list.
    other = enroll(client, device="quest-99999", key="b" * 48, unit="")
    assert client.get("/api/demos/headset/state", headers=other).json()["library"] == []


def test_hd_previews_fit_and_larger_ones_are_refused(studio):
    _, client, _ = studio
    h = enroll(client)
    jpeg = b"\xff\xd8\xff" + b"0" * 400_000
    assert client.post("/api/demos/headset/preview", headers=h, content=jpeg).status_code == 200
    big = b"\xff\xd8\xff" + b"0" * 600_001
    assert client.post("/api/demos/headset/preview", headers=h, content=big).status_code == 413


def test_large_files_upload_in_resumable_pieces(studio, monkeypatch):
    import app.demos as mod

    store, client, directory = studio
    monkeypatch.setattr(mod, "CHUNK", 4)
    body = b"0123456789"
    start = client.post("/api/demos/uploads", json={"filename": "ride.mp4", "size": len(body)})
    assert start.status_code == 200
    uid, chunk = start.json()["upload"], start.json()["chunk"]
    assert chunk == 4
    put = lambda offset, data: client.put(f"/api/demos/uploads/{uid}?offset={offset}", content=data)  # noqa: E731
    assert put(0, body[:4]).json() == {"received": 4}
    # A repeated or out-of-order piece changes nothing and says where to resume.
    assert put(0, body[:4]).json() == {"received": 4}
    assert put(8, body[8:]).json() == {"received": 4}
    assert put(4, body[4:9] + b"x").status_code == 413
    assert client.post(f"/api/demos/uploads/{uid}/done").status_code == 409
    assert put(4, body[4:8]).json() == {"received": 8}
    assert put(8, body[8:]).json() == {"received": 10}
    item = client.post(f"/api/demos/uploads/{uid}/done").json()
    assert item["kind"] == "video" and item["bytes"] == 10 and item["id"] == uid
    assert (directory / "media" / "quest" / f"{uid}.mp4").read_bytes() == body
    assert client.post(f"/api/demos/uploads/{uid}/done").status_code == 404
    assert (
        client.post(
            "/api/demos/uploads",
            json={"filename": "x.mp4", "size": 5},
            headers={"X-Test-Role": "screen_operator"},
        ).status_code
        == 403
    )
    assert (
        client.post("/api/demos/uploads", json={"filename": "x.exe", "size": 5}).status_code == 415
    )


def test_watched_headsets_send_views_every_second_and_unchanged_frames_are_not_resent(studio):
    _, client, _ = studio
    h = enroll(client)
    assert client.get("/api/demos/headset/state", headers=h).json()["preview_every"] == 5
    jpeg = b"\xff\xd8\xff" + b"1" * 100
    client.post("/api/demos/headset/preview", headers=h, content=jpeg)
    first = client.get("/api/demos/units/hq-01/preview")
    assert first.status_code == 200 and first.content == jpeg
    tag = first.headers["etag"]
    same = client.get("/api/demos/units/hq-01/preview", headers={"If-None-Match": tag})
    assert same.status_code == 304 and same.content == b""
    # Someone is watching now, so the headset sends a view every second.
    assert client.get("/api/demos/headset/state", headers=h).json()["preview_every"] == 1
    client.post("/api/demos/headset/preview", headers=h, content=jpeg + b"2")
    newer = client.get("/api/demos/units/hq-01/preview", headers={"If-None-Match": tag})
    assert newer.status_code == 200 and newer.headers["etag"] != tag
