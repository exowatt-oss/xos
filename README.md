# Xos

A self-hostable hub for an office's screens and meeting rooms: room TVs, table remotes, door
panels, lobby signage and Linux room computers, run from one web Admin, with an MCP server so AI
agents can put content on screens and run rooms too.

![Status: experimental](https://img.shields.io/badge/status-experimental-orange) ![License: Apache--2.0](https://img.shields.io/badge/license-Apache--2.0-blue)

Created at [Exowatt](https://exowatt.com).

## Status

Experimental. Xos runs real rooms where it was built, but this public release is new: APIs and
the device protocol can change, and there is no support commitment.

```
                     Xos server (FastAPI · src/app)
                     ├─ /app/        Xos Admin + screen views   (/ redirects here)
                     ├─ /api/        devices · rooms · pages · media · zoom · calendar · config
                     ├─ /u/…         the same API, reached through your sign-in proxy
                     └─ /mcp         MCP server for AI agents
              ┌──────────────┬──────────────┴───────┬──────────────────┐
         Fire TV stick   Linux box (os/)        any browser        tablet / phone
         Xos TV (firetv/) room computer,        ?kiosk=1 links     remote, door panel,
         room TV         display, Pi node       signage, admin     share-to-screen
```

What it does:

- **Screens** register with a heartbeat and receive commands over Server-Sent Events: switch
  what they show, screenshot, identify, message, reload, TV on/off (Fire TV + HDMI-CEC).
  Devices only make outbound connections, so it works through any office NAT.
- **Content**: upload photos and videos, then make slideshows, announcements, web links,
  uploaded HTML pages and playlists, and show them on any screen. Lobby signage plays a built-in
  welcome page until you pick something.
- **Rooms**: each room's TV, table remote and door panel; room computers join Google Meet, Zoom
  (web client), Teams and Webex invitations as guests and are driven from the table remote;
  optional Zoom Rooms control and Google Calendar room agendas.
- **Sharing**: every display has a share link and QR code; open it on a laptop or phone to
  share your screen (WebRTC), a link or a photo.
- **Whiteboard**: a live room whiteboard, with optional hand-off to
  [Copper](https://github.com/copper-browser/Copper) canvases.
- **Roles**: Admin, Content editor, Screen operator, Member; per-role and per-person permission
  overrides, an append-only audit log, and "preview as" a role or person.
- **MCP** at `/mcp`: every Admin action as an agent tool, with the same permission checks.

## Install

Requirements: Python 3.13+, [uv](https://docs.astral.sh/uv/), and Node 22+ only to run the web
tests. Xos depends on [forwarded-auth](https://github.com/exowatt-oss/forwarded-auth), which is
installed from GitHub (pinned in `pyproject.toml` / `uv.lock`).

```sh
git clone https://github.com/exowatt-oss/xos
cd xos
uv sync
```

## Usage / Quickstart

Run it locally with no external services, acting as a local Admin:

```sh
XOS_DEV_USER=you@example.com XOS_SUPER_ADMINS=you@example.com \
  uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Then:

1. Open <http://localhost:8000/app/#/admin> (Xos Admin) and add a room in **Rooms** (sites come
   from `XOS_SITES`; without it there is one site, "Main site").
2. Open <http://localhost:8000/setup> in another browser tab (or on a TV browser): it shows a
   pairing code. In **Screens → Add a screen**, enter the code and pick what it shows.
3. Upload a photo in **Content**, make a slideshow, and **Show** it on the screen.

`XOS_DEV_USER` makes every request act as that address. It is for local development only:
never set it on a server anyone else can reach, and keep the dev server on `127.0.0.1`.

Tests:

```sh
uv run pytest -n 4           # server (no network, no services)
node --test tests/*.test.mjs # web app modules
uv run ruff check . && uv run ruff format --check .
```

## Configuration

All settings are environment variables (or a `.env` file); see `src/app/config.py`. Every default
works with zero external services.

| Variable | Default | Meaning |
|---|---|---|
| `XOS_DATA_DIR` | `./data` | Where devices, rooms, pages, media and people are saved. Back it up. |
| `XOS_PUBLIC_URL` | `http://localhost:8000` | This server's public address (share, pairing and installer links). |
| `XOS_SITES` / `XOS_SEED_ROOMS` | one site, no rooms | JSON. Sites, and the rooms a fresh server starts with; then use Admin → Rooms. |
| `XOS_ROOM_CALENDARS` | empty | JSON `{room: calendar-id}`: default room calendars (Admin can override). |
| `XOS_SUPER_ADMINS` | empty | Comma-separated: always Admin; the only people who grant or remove Admin. |
| `XOS_BOOTSTRAP_ADMINS` | empty | Comma-separated starting Admins until a super admin changes their role. |
| `XOS_ALLOWED_EMAIL_DOMAINS` | empty | Domains whose signed-in people get a role (empty: whatever forwarded-auth admits). |
| `XOS_MCP_TOKEN` | empty | Bearer token for `/mcp` with full Admin. Unset: only signed-in Admins. |
| `FORWARDED_AUTH_TRUSTED_HOSTNAMES` | empty | Required when exposing `/mcp`: the public hostname (comma-separated if multiple); without it, forwarded/OIDC-mode MCP requests are rejected. |
| `XOS_ICE_SERVERS`, `XOS_TURN_USERNAME`, `XOS_TURN_PASSWORD` | empty | WebRTC STUN/TURN for screen sharing across networks. Empty: same LAN only. |
| `XOS_TIMEZONE`, `XOS_SITE_TIMEZONES` | `UTC` | Timezone for Linux room boxes, per site (JSON). |
| `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET` | empty | Optional Zoom Server-to-Server OAuth app for Zoom Rooms; dry-run until set. |
| `GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_SECRET` | empty | Optional Google OAuth client for room calendars (or a service account below). |
| `GOOGLE_SERVICE_ACCOUNT_JSON`, `XOS_CALENDAR_USER` | empty | Optional service account with domain-wide delegation (calendar.readonly). |
| `XOS_DEV_USER` | empty | Local development only: act as this address. |

### Sign-in (production)

Screens never sign in; people do. Run Xos behind an auth proxy (for example oauth2-proxy with
Caddy `forward_auth`, or any OIDC provider) that protects **only `/u/*`** and forwards a verified
identity token. Everything else (`/app`, `/api`, `/static`, `/media`, `/pages`, `/setup`, `/s`,
`/os`, `/firetv`, `/quest`) must stay reachable by screens without sign-in; the browser reaches the
signed-in API at `/u/api/…`. Configure verification with `forwarded-auth`'s variables, for
example:

```sh
FORWARDED_AUTH_ISSUER=https://accounts.google.com,accounts.google.com
FORWARDED_AUTH_AUDIENCE=<your OAuth client id>
FORWARDED_AUTH_ALLOWED_EMAIL_DOMAINS=example.com
FORWARDED_AUTH_TRUSTED_HOSTNAMES=xos.example.org
```

In forwarded/OIDC mode, `/mcp` does **not** trust localhost, `127.0.0.1`, `[::1]` or
`testserver` by default. Set `FORWARDED_AUTH_TRUSTED_HOSTNAMES` to the public hostname
serving `/mcp`; do not enable `FORWARDED_AUTH_ALLOW_LOOPBACK_HOSTS` on a deployment.

A Bearer token that doesn't verify is refused (401), never treated as anonymous; if verification
isn't configured, every signed-in request is refused. Optionally, agents can call the read-only
API with short-lived machine tokens from a gateway you run
(`FORWARDED_AUTH_ADDITIONAL_ISSUER*`): such callers only read screens, rooms and content.

Screens can't sign in, so the screen-facing API is unauthenticated by design: for example,
`GET /api/devices` lists every screen with its share code, and pairing, heartbeats and the
command stream are open to anything that can reach the server. Run Xos on a network you trust,
and before exposing it more widely review the device-key, share-link, screenshot, ADB
provisioning and MCP surfaces for your threat model.

## Clients in this repository

- `app/`: Xos Admin and every screen view (vanilla JS modules, no build step), plus the Quest
  demo viewer (`app/quest/`, a PWA) and Demo Studio.
- `static/player/`: the slideshow player. `static/welcome/`: the built-in welcome page.
- `os/`: the Linux room computer / display appliance (Debian 13, `os/appliance/`), a Raspberry Pi
  node (`os/install.sh`), and a Mac room-computer agent (`os/mac/`). They download from your Xos
  server (`/os/...`); pass `--server <your XOS_PUBLIC_URL>`.
- `firetv/`: Xos TV, a small Fire TV / Android TV WebView app, built with the plain Android SDK
  (`firetv/build.sh`). Set your server in `firetv/src/com/example/xos/tv/Config.java` (or pick it
  in the app's menu) and change the `com.example.xos.tv` package id before you distribute it.
  No APK is checked in.

## Prerequisites

Optional, obtained and licensed separately; none are bundled:

- **Fire TV / Android**: a JDK and the Android SDK (build-tools 35, platform 34) to build Xos TV;
  `adb` to provision sticks. Admin's "add a screen by IP" uses network ADB.
- **Zoom Rooms**: a Zoom account with Zoom Rooms licences and a Server-to-Server OAuth app.
- **Google Calendar**: a Google Cloud OAuth client or a service account.
- **Meta Quest demos**: to install the viewer as an app, package `/app/quest/` yourself (for
  example with Bubblewrap) and serve it at `quest/xos-quest.apk`.

Not included in this release: the iPad/iPhone app (it needs Xcode and per-organization signing),
signed APKs, and site-specific content and documentation. A browser in kiosk mode
(`/app/?kiosk=1#/remote/<room>`) covers the tablet roles.

## License

Original code is licensed under [Apache-2.0](LICENSE). See [NOTICE](NOTICE) for attribution and
third-party notices (vendored three.js is MIT).

## Contributing

See [CONTRIBUTING](https://github.com/exowatt-oss/.github/blob/main/CONTRIBUTING.md).
Contributions require DCO sign-off (`git commit -s`); no CLA is required. CI runs the server
tests, ruff and the web tests on every pull request.

## Security

Please report vulnerabilities privately using [GitHub private vulnerability reporting](https://github.com/exowatt-oss/xos/security/advisories/new), or contact software@exowatt.com. See the [security policy](https://github.com/exowatt-oss/.github/blob/main/SECURITY.md); do not open a public issue.
