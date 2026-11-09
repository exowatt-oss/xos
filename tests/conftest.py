import json
import os
import tempfile

# Keep test runs from touching the real data directory.
# One fresh directory per test process (pytest-xdist workers each get their own).
os.environ["XOS_DATA_DIR"] = tempfile.mkdtemp(prefix="xos-test-")
# A synthetic deployment: two sites, one seeded room, per-site timezones. Nothing here is real.
os.environ["XOS_SITES"] = json.dumps([{"id": "hq", "name": "HQ"}, {"id": "annex", "name": "Annex"}])
os.environ["XOS_SEED_ROOMS"] = json.dumps([{"id": "atrium", "name": "Atrium", "site": "hq"}])
os.environ["XOS_ROOM_CALENDARS"] = json.dumps(
    {"atrium": "c_atrium0example@resource.calendar.google.com"}
)
os.environ["XOS_SITE_TIMEZONES"] = json.dumps({"hq": "America/Denver", "annex": "America/New_York"})
os.environ["XOS_ALLOWED_EMAIL_DOMAINS"] = "example.com"
# The signed-in test user (below) is the super admin; lee@ is a starting Admin.
os.environ["XOS_BOOTSTRAP_ADMINS"] = "root@example.com,lee@example.com"
os.environ["XOS_SUPER_ADMINS"] = "root@example.com"
# Never verify against a real identity provider; tests that need tokens configure their own.
for key in [k for k in os.environ if k.startswith("FORWARDED_AUTH_")]:
    del os.environ[key]
# TestClient uses Host: testserver; only tests may opt into loopback MCP transport.
os.environ["FORWARDED_AUTH_ALLOW_LOOPBACK_HOSTS"] = "true"

import pytest  # noqa: E402

from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def signed_in_as_team(monkeypatch):
    """Tests act as a starting Admin unless they set XOS_DEV_USER themselves."""
    monkeypatch.setattr(settings, "XOS_DEV_USER", "root@example.com")
