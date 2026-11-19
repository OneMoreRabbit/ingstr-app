"""Keeping a key out of the index — a net, not a guarantee.

A conversation can hold a credential a tool printed, even though no key file is
ever shared and `configs` is a cleaned copy made by the estate
(`agent-folders-design-v0_1` §4). A passage that looks like a credential is not
indexed.

Three decisions, each worth more than the patterns themselves:

**The passage is dropped, not the file.** One leaked token in one turn must not
hide a whole conversation from search.

**The patterns are part of the release, never configuration.** Two orgs that
could tune them would disagree about what a secret is and hold two different
indexes, and the one that tuned it loosest would be the one nobody checked.
Changing this list is a release, with a test and a line in the release notes.

**It is a net, and the documents say so.** A secret in an unusual shape passes
it, and a count of what it caught is not evidence of what it missed. The
guarantees are elsewhere: key files are never shared, and the index is readable
only by the declared group.
"""

from __future__ import annotations

import re

#: Each pattern is anchored to something a credential has and ordinary prose
#: does not: a vendor prefix, a header name, a PEM banner, or an assignment of
#: a long opaque value to a word that means "secret". Matching on entropy alone
#: was rejected: base64 of anything scores the same, and a conversation is full
#: of hashes, uuids and digests that are not secrets.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Vendor-prefixed tokens. The prefix is the evidence; the length keeps a
    # sentence that merely mentions `sk-` from matching.
    ("vendor-token", re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    # A private key in a conversation is a private key, whatever surrounds it.
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # An Authorization header with anything substantial after the scheme.
    (
        "authorization-header",
        re.compile(r"\bauthorization\s*:\s*(?:bearer|basic|token)\s+\S{12,}", re.I),
    ),
    # `password = "…"`, `api_key: …`, `token=…` with a long opaque value.
    # `\S` and the length together are what keep `password: changeme` and
    # `token: <redacted>` out of the count.
    (
        "assigned-secret",
        re.compile(
            r"\b(?:pass(?:word|wd)|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)"
            r"\b\s*[:=]\s*[\"']?[A-Za-z0-9/+_=-]{20,}",
            re.I,
        ),
    ),
    # JWTs: three base64url segments, the first decoding to a JSON header.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
)


def looks_like_a_secret(text: str) -> str | None:
    """The name of the first pattern that matched, or None.

    The NAME is returned rather than the match: a caller that logs what it
    found must be able to say which net caught it without ever holding the
    value. Nothing here returns, logs or stores the secret itself.
    """
    for name, pattern in _PATTERNS:
        if pattern.search(text):
            return name
    return None


def pattern_names() -> tuple[str, ...]:
    """Every net, for the test that pins the list to the release."""
    return tuple(name for name, _ in _PATTERNS)
