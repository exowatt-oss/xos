"""Admin → Integrations → Credentials: every secret Xos reads, set or not, never its value."""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import app.main as main
from app.config import settings

client = TestClient(main.app)
PATH = "/api/integrations/credentials"


@pytest.fixture
def secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "XOS_DATA_DIR", tmp_path)
    for key in (
        "GOOGLE_CALENDAR_CLIENT_SECRET",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "ZOOM_CLIENT_SECRET",
        "XOS_MCP_TOKEN",
        "XOS_TURN_PASSWORD",
    ):
        monkeypatch.setattr(settings, key, SecretStr(""))
    for key in (
        "GOOGLE_CALENDAR_CLIENT_ID",
        "XOS_CALENDAR_USER",
        "ZOOM_ACCOUNT_ID",
        "ZOOM_CLIENT_ID",
        "XOS_TURN_USERNAME",
    ):
        monkeypatch.setattr(settings, key, "")
    monkeypatch.setattr(settings, "XOS_ICE_SERVERS", "stun:stun.l.google.com:19302")
    return monkeypatch


def items():
    response = client.get(PATH)
    assert response.status_code == 200
    return {i["key"]: i for i in response.json()["items"]}


def test_only_admins_with_integrations_see_credentials(secrets):
    secrets.setattr(settings, "XOS_DEV_USER", "someone@example.com")  # a Member
    assert client.get(PATH).status_code == 403


def test_the_oauth_calendar_names_its_account_and_marks_the_service_account_unused(
    secrets, tmp_path
):
    secrets.setattr(settings, "GOOGLE_CALENDAR_CLIENT_ID", "client-id")
    secrets.setattr(settings, "GOOGLE_CALENDAR_CLIENT_SECRET", SecretStr("client-secret-value"))
    secrets.setattr(
        settings, "GOOGLE_SERVICE_ACCOUNT_JSON", SecretStr('{"private_key": "sa-secret"}')
    )
    secrets.setattr(settings, "XOS_CALENDAR_USER", "rooms@example.com")
    secrets.setattr(settings, "XOS_MCP_TOKEN", SecretStr("mcp-secret-value"))
    saved = tmp_path / "credentials" / "google-calendar.json"
    saved.parent.mkdir()
    saved.write_text(
        json.dumps(
            {
                "refresh_token": "refresh-secret",
                "account": "atrium@example.com",
                "connected_at": 1793000000,
                "connected_by": "ray@example.com",
            }
        )
    )
    body = client.get(PATH).text
    for value in ("client-secret-value", "sa-secret", "refresh-secret", "mcp-secret-value"):
        assert value not in body
    got = items()
    calendar = got["google_calendar"]
    assert calendar["status"] == "ok" and calendar["account"] == "atrium@example.com"
    assert calendar["connected_by"] == "ray@example.com"
    assert calendar["secrets"] == {
        "GOOGLE_CALENDAR_CLIENT_ID": True,
        "GOOGLE_CALENDAR_CLIENT_SECRET": True,
    }
    assert got["google_service_account"]["status"] == "unused"
    assert got["mcp"]["status"] == "ok"


def test_missing_and_optional_credentials_say_what_to_do(secrets):
    got = items()
    assert got["google_calendar"]["status"] == "action"
    assert got["google_service_account"]["status"] == "optional"
    assert got["zoom"]["status"] == "optional"
    assert got["mcp"]["status"] == "optional"
    assert got["turn"]["status"] == "action" and "STUN" in got["turn"]["detail"]
    assert got["turn"]["secrets"]["XOS_ICE_SERVERS"] is True


def test_a_turn_url_needs_its_username_and_password(secrets):
    secrets.setattr(
        settings, "XOS_ICE_SERVERS", "stun:stun.l.google.com:19302,turn:turn.example.org:3478"
    )
    assert items()["turn"]["status"] == "action"
    secrets.setattr(settings, "XOS_TURN_USERNAME", "xos")
    secrets.setattr(settings, "XOS_TURN_PASSWORD", SecretStr("turn-secret"))
    assert items()["turn"]["status"] == "ok"
    assert "turn-secret" not in client.get(PATH).text
