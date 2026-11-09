from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file.

    Every default works with no external service: no sign-in proxy, no Zoom, no Google Calendar.
    Sign-in itself is configured with FORWARDED_AUTH_* variables (see README.md, forwarded-auth).
    """

    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    # Where shared room state and the device registry are saved between restarts.
    XOS_DATA_DIR: Path = REPO_ROOT / "data"
    # Seconds since the last heartbeat before a device shows as offline.
    DEVICE_ONLINE_S: int = 90
    # Total size of uploaded photos and videos (Admin → Content → Media), in MB.
    XOS_MEDIA_MAX_MB: int = 2000
    # Public address of this server: share links, pairing links, node installers, MCP answers.
    XOS_PUBLIC_URL: str = "http://localhost:8000"
    # Sites and the rooms a fresh server starts with (src/app/catalog.py), as JSON. Rooms are
    # then managed in Admin → Rooms. Example:
    #   XOS_SITES='[{"id":"hq","name":"Headquarters"}]'
    #   XOS_SEED_ROOMS='[{"id":"boardroom","name":"Boardroom","site":"hq"}]'
    #   XOS_ROOM_CALENDARS='{"boardroom":"room-calendar-id@resource.calendar.google.com"}'
    XOS_SITES: str = ""
    XOS_SEED_ROOMS: str = ""
    XOS_ROOM_CALENDARS: str = ""
    # Default timezone for Linux room boxes, and per-site overrides as JSON {"site": "Area/City"}.
    XOS_TIMEZONE: str = "UTC"
    XOS_SITE_TIMEZONES: str = ""
    # WebRTC ICE servers for screen sharing and camera feeds, comma-separated URLs. Video goes
    # peer to peer; a STUN server also finds a route when the laptop and the TV are on different
    # subnets. TURN (relay) URLs take XOS_TURN_USERNAME / XOS_TURN_PASSWORD. Empty: LAN only.
    XOS_ICE_SERVERS: str = ""
    XOS_TURN_USERNAME: str = ""
    XOS_TURN_PASSWORD: SecretStr = Field(default=SecretStr(""), repr=False)
    # Private key the server uses for network ADB when Admin sets up a screen by IP
    # (src/app/provision.py). Default: XOS_DATA_DIR/adb/adbkey, made on first use.
    XOS_ADB_KEY: Path | None = None
    # Bearer token for the MCP server at /mcp (docs in README.md). Unset: only callers with a
    # verified sign-in (forwarded-auth) get in.
    XOS_MCP_TOKEN: SecretStr = Field(default=SecretStr(""), repr=False)
    # Super admins: always Admin, whatever Admin → People & roles says (so Xos can't lock everyone
    # out), and the only people who can make someone Admin or remove Admin. Comma-separated.
    XOS_SUPER_ADMINS: str = ""
    # The starting Admins: Admin until a super admin gives them another role in People & roles
    # (src/app/access.py). Everyone else gets the role assigned there (Member by default).
    XOS_BOOTSTRAP_ADMINS: str = ""
    # Signed-in people must have an address in one of these domains (comma-separated) to get a
    # role. Empty: rely on forwarded-auth's FORWARDED_AUTH_ALLOWED_EMAIL_DOMAINS, which it
    # requires for any verifying mode.
    XOS_ALLOWED_EMAIL_DOMAINS: str = ""
    # Local development only: act as this signed-in address (there is no sign-in proxy locally).
    # Never set it on a server other people can reach.
    XOS_DEV_USER: str = ""

    # Zoom Server-to-Server OAuth app (marketplace.zoom.us), optional. Zoom stays in dry-run
    # mode until all three are present.
    ZOOM_ACCOUNT_ID: str = ""
    ZOOM_CLIENT_ID: str = ""
    ZOOM_CLIENT_SECRET: SecretStr = Field(default=SecretStr(""), repr=False)
    ZOOM_API_BASE: str = "https://api.zoom.us/v2"
    ZOOM_OAUTH_URL: str = "https://zoom.us/oauth/token"
    # Browser rooms join invitations as guests; creating calls requires a room account.
    XOS_INSTANT_MEETINGS: bool = False

    # Google Calendar (optional): an OAuth client connected in Admin → Integrations, or a service
    # account key with domain-wide delegation for calendar.readonly, acting as XOS_CALENDAR_USER.
    GOOGLE_CALENDAR_CLIENT_ID: str = ""
    GOOGLE_CALENDAR_CLIENT_SECRET: SecretStr = Field(default=SecretStr(""), repr=False)
    GOOGLE_SERVICE_ACCOUNT_JSON: SecretStr = Field(default=SecretStr(""), repr=False)
    XOS_CALENDAR_USER: str = ""
    GOOGLE_TOKEN_URL: str = "https://oauth2.googleapis.com/token"
    GOOGLE_CALENDAR_API: str = "https://www.googleapis.com/calendar/v3"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
