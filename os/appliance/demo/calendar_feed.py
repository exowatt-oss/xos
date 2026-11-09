"""A local Xos server whose room calendars come from a JSON file instead of Google, for demos and
screenshots before the calendar service account is set up.

    uv run python os/appliance/demo/calendar_feed.py events.json [port]

events.json is a list of Calendar API events (the shape GET .../calendars/{id}/events returns in
"items"): start/end dateTime, summary, hangoutLink or conferenceData.entryPoints, location,
description, attendees. Only Google's token and events endpoints are replaced (httpx MockTransport);
everything after them (booking(), now/next, join links, the tablet, TV and door panel) is the real
code. Uses XOS_DATA_DIR and XOS_DEV_USER like `task dev`; never point it at production data.
"""

import json
import sys
from pathlib import Path

import httpx
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
import app.main as main  # noqa: E402
from app.calendars import Calendars  # noqa: E402

events = Path(sys.argv[1])
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8796
key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
s = main.settings
s.GOOGLE_SERVICE_ACCOUNT_JSON = SecretStr(json.dumps(
    {"client_email": "xos-demo@localhost", "private_key": key, "token_uri": "https://oauth2.googleapis.com/token"}
))
s.XOS_CALENDAR_USER = s.XOS_CALENDAR_USER or "demo@localhost"


def google(req: httpx.Request) -> httpx.Response:
    if "token" in req.url.path:
        return httpx.Response(200, json={"access_token": "demo", "expires_in": 3600})
    return httpx.Response(200, json={"items": json.loads(events.read_text())})


main.calendars = Calendars(s, transport=httpx.MockTransport(google))
uvicorn.run(main.app, host="127.0.0.1", port=port, log_level="warning")
