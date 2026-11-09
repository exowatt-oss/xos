"""Validate room-browser invitations without accepting arbitrary navigation targets."""

import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

PROVIDERS = {"meet", "teams", "webex", "zoom"}
TEAMS_HOSTS = {"teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft"}
# Webex invitations: personal rooms, scheduled meetings (j.php with MTID) and browser-join links,
# on the meeting site's webex.com subdomain. Webex's own event and recording pages aren't meetings.
WEBEX_PATHS = re.compile(
    r"/(?:meet|join)/[\w.-]+/?"
    r"|/[\w-]+/j\.php"
    r"|/wbxmjs/joinservice/sites/[\w-]+/meeting/download/[\w-]+/?"
    r"|/webappng/sites/[\w-]+/meeting/[\w/-]+"
)
WEBEX_QUERY = {"MTID", "siteurl"}


def meeting_link(text: str, passcode: str = "") -> dict:
    text = text.strip()
    if re.fullmatch(r"[a-z]{3}-[a-z]{4}-[a-z]{3}", text, re.I):
        return {"provider": "meet", "url": "https://meet.google.com/" + text.lower()}
    digits = re.sub(r"[\s-]", "", text)
    if re.fullmatch(r"\d{9,11}", digits):
        query = urlencode({"pwd": passcode}) if passcode else ""
        return {
            "provider": "zoom",
            "url": urlunsplit(("https", "zoom.us", "/j/" + digits, query, "")),
        }
    try:
        u = urlsplit(text)
        if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
            raise ValueError
    except ValueError:
        raise ValueError("Paste a Meet, Teams, Webex or Zoom invitation link") from None
    host = (u.hostname or "").lower()
    if host == "meet.google.com" and re.fullmatch(
        r"/(?:meet/)?[a-z]{3}-[a-z]{4}-[a-z]{3}/?", u.path, re.I
    ):
        return {
            "provider": "meet",
            "url": "https://meet.google.com/" + u.path.strip("/").lower().removeprefix("meet/"),
        }
    if host in TEAMS_HOSTS and re.fullmatch(r"/(?:l/meetup-join/[^\s]+|meet/[\w-]+)/?", u.path):
        # Preserve invitation context/passcode, never unrelated login/host tokens.
        query = parse_qs(u.query)
        query = {k: v for k, v in query.items() if k in {"context", "p"}}
        return {
            "provider": "teams",
            "url": urlunsplit(("https", host, u.path, urlencode(query, doseq=True), "")),
        }
    if host in {"zoom.us", "zoom.com"} or host.endswith((".zoom.us", ".zoom.com")):
        match = re.fullmatch(r"/(?:j|w|s|wc/join)/(\d{9,11})/?", u.path)
        if match:
            pwd = passcode or parse_qs(u.query).get("pwd", [""])[0]
            query = urlencode({"pwd": pwd}) if pwd else ""
            return {
                "provider": "zoom",
                "url": urlunsplit(("https", host, "/wc/join/" + match[1], query, "")),
            }
    if (host == "webex.com" or host.endswith(".webex.com")) and WEBEX_PATHS.fullmatch(u.path):
        # Keep the meeting's id, never sign-in or host tokens from a copied browser address.
        query = {k: v for k, v in parse_qs(u.query).items() if k in WEBEX_QUERY}
        if u.path.endswith("/j.php") and "MTID" not in query:
            raise ValueError("That Webex link is missing its meeting id (MTID)")
        return {
            "provider": "webex",
            "url": urlunsplit(("https", host, u.path, urlencode(query, doseq=True), "")),
        }
    raise ValueError("Paste a Meet, Teams, Webex or Zoom invitation link, a Meet code or a Zoom ID")
