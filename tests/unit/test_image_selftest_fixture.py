"""The image self-test's fixture must stay current with the reader.

It did not once. The fixture was written inline in `scripts/image-selftest.py`,
the Codex rule was later tightened to require the line type, and nothing
noticed until the image would not build — by which time v0.2.4 was already
tagged. A build-time check that goes stale is worse than no check: it passes
for years and fails on the day of a release.

So the fixture lives in one function and this test reads that same function.
Tighten the rule again and this fails in a second, not in a build.
"""

import importlib.util
from pathlib import Path

from ingstr.conversations import read_conversation

_SELFTEST = Path(__file__).resolve().parents[2] / "scripts" / "image-selftest.py"


def _selftest_module():
    spec = importlib.util.spec_from_file_location("image_selftest", _SELFTEST)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_selftest_fixture_still_yields_a_turn(tmp_path):
    module = _selftest_module()
    archived = tmp_path / "rollout.jsonl.zst"
    archived.write_bytes(module.archived_conversation_bytes())

    read = read_conversation(archived)

    assert [t.text for t in read.turns] == [module.SELFTEST_TEXT]
    assert read.turns[0].conversation_id == "selftest"
    assert read.unrecognised == 0
