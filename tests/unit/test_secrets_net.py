"""The net that keeps a printed credential out of the index.

Every value here is invented. Nothing in this file is, or has ever been, a
real credential.
"""

import pytest

from ingstr.secrets_net import looks_like_a_secret, pattern_names

_CAUGHT = [
    ("sk-abcdefghijklmnopqrstuvwx", "vendor-token"),
    ("ghp_aaaabbbbccccddddeeeeffffgggghhhh", "github-token"),
    ("github_pat_11AAAAAAA0bbbbbbbbbbcccccc", "github-pat"),
    ("xoxb-1111111111-2222222222-aaaaaaaaaa", "slack-token"),
    ("AKIAIOSFODNN7EXAMPLE", "aws-access-key"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", "private-key"),
    ("Authorization: Bearer aaaaaaaaaaaaaaaaaaaa", "authorization-header"),
    ('password = "a8Fj29dkRm21PqLs93xZ"', "assigned-secret"),
    ("api_key: 0123456789abcdef0123456789abcdef", "assigned-secret"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij", "jwt"),
]


@pytest.mark.parametrize("text,expected", _CAUGHT)
def test_a_credential_in_a_turn_is_caught(text, expected):
    assert looks_like_a_secret(f"here it is: {text} — try that") == expected


_PASSED = [
    "the key is in the registry, ask estate-manage",
    "password: changeme",
    "token: <redacted>",
    "run 377 wrote 2770 skipped and 2060 errored",
    "the digest is sha256:1356b386f97c3f668c26dd9663277550dcea0f5229ed19c76a1081a20ee08034",
    "commit 6504d16 on dev",
]


@pytest.mark.parametrize("text", _PASSED)
def test_ordinary_talk_is_not_withheld(text):
    """A net that catches conversation is a net that empties the index.

    A digest and a commit are the obvious traps: both are long, opaque and
    everywhere in this estate's conversations.
    """
    assert looks_like_a_secret(text) is None


def test_the_name_is_returned_and_never_the_value():
    """A caller must be able to log WHAT caught it without holding the value."""
    caught = looks_like_a_secret("ghp_aaaabbbbccccddddeeeeffffgggghhhh")
    assert caught == "github-token"
    assert "ghp_" not in caught


def test_the_patterns_are_part_of_the_release():
    """Pinned so a change to the list is a change somebody decided.

    Two orgs that could tune this would disagree about what a secret is and
    hold two different indexes, and the loosest would be the one nobody
    checked — which is why there is no configuration key for it.
    """
    assert pattern_names() == (
        "vendor-token",
        "github-token",
        "github-pat",
        "slack-token",
        "aws-access-key",
        "google-api-key",
        "private-key",
        "authorization-header",
        "assigned-secret",
        "jwt",
    )
