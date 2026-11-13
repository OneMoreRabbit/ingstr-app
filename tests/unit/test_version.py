"""The package version must match the release being cut.

v0.2.0 shipped with `version = "0.1.1"` in pyproject.toml: the bump was
forgotten, the tag said one thing and the package another, and `ingstr
version` in the released image reports 0.1.1. Nothing caught it — not review,
not CI, not the release build — because nothing was looking.

It matters beyond tidiness. From v0.2.1 a recorded failure is keyed to the
version, so a release that forgets the bump also quietly stops retrying every
file that failed: the record from the previous image still matches. A silent
wrong answer produced by a missing edit.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version() -> str:
    for line in (_ROOT / "pyproject.toml").read_text().splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    raise AssertionError("no version in pyproject.toml")


def _newest_release_note() -> str:
    for line in (_ROOT / "release-notes.md").read_text().splitlines():
        match = re.match(r"^## v(\d+\.\d+\.\d+) — \d{4}-\d{2}-\d{2}$", line)
        if match:
            return match.group(1)
    raise AssertionError("no release entry in release-notes.md")


def test_the_package_version_matches_the_newest_release_note() -> None:
    """The newest entry is written in the same commit as the tag (method
    1.33.14), so these two agreeing is what a forgotten bump breaks."""
    assert _pyproject_version() == _newest_release_note()


def test_the_version_is_a_plain_release_number() -> None:
    """Constitution §12: a release is vMAJOR.MINOR.PATCH. No rc, alpha or
    beta — v0.1 of the architecture document described them and they are no
    longer allowed."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", _pyproject_version())
