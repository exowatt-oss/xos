import pytest

from app.meeting_links import meeting_link


def test_provider_links_and_passcode_encoding():
    assert meeting_link("abc-defg-hij")["provider"] == "meet"
    assert meeting_link("123 456 789", "a&b")["url"].endswith("?pwd=a%26b")
    link = meeting_link("https://company.zoom.us/j/123456789?pwd=secret&zak=host-token")
    assert link == {
        "provider": "zoom",
        "url": "https://company.zoom.us/wc/join/123456789?pwd=secret",
    }
    assert (
        meeting_link("https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc/0?context=test")[
            "provider"
        ]
        == "teams"
    )
    assert meeting_link("https://teams.cloud.microsoft/meet/123456?p=abc")["provider"] == "teams"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://acme.webex.com/meet/jane.doe", "https://acme.webex.com/meet/jane.doe"),
        ("https://ACME.webex.com/join/jdoe/", "https://acme.webex.com/join/jdoe/"),
        (
            "https://acme.webex.com/acme/j.php?MTID=m3f1a2b&token=host-secret",
            "https://acme.webex.com/acme/j.php?MTID=m3f1a2b",
        ),
        (
            "https://acme.webex.com/wbxmjs/joinservice/sites/acme/meeting/download/9f8e"
            "?siteurl=acme&MTID=m3f1a2b&launchApp=true",
            "https://acme.webex.com/wbxmjs/joinservice/sites/acme/meeting/download/9f8e"
            "?siteurl=acme&MTID=m3f1a2b",
        ),
    ],
)
def test_webex_invitations_keep_only_the_meeting(url, expected):
    assert meeting_link(url) == {"provider": "webex", "url": expected}


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/meet.google.com/abc-defg-hij",
        "https://meet.google.com.evil.test/abc-defg-hij",
        "https://evilzoom.us/j/123456789",
        "https://zoom.us@evil.test/j/123456789",
        "https://teams.microsoft.com:8443/meet/123456",
        "http://teams.microsoft.com/meet/123456",
        "https://teams.microsoft.com/arbitrary-page",
        "https://webex.com.evil.test/meet/jdoe",
        "https://acme.webex.com/acme/j.php",
        "https://acme.webex.com/recordingservice/sites/acme/recording/1",
        "http://acme.webex.com/meet/jdoe",
    ],
)
def test_rejects_untrusted_targets(url):
    with pytest.raises(ValueError):
        meeting_link(url)


@pytest.mark.parametrize(
    "path", ["/meet/abc-defg-hij", "/meet/abc-defg-hij/", "/MEET/ABC-DEFG-HIJ"]
)
def test_google_live_meet_path_normalizes_to_a_joinable_invitation(path):
    assert meeting_link("https://meet.google.com" + path + "?authuser=1") == {
        "provider": "meet",
        "url": "https://meet.google.com/abc-defg-hij",
    }
