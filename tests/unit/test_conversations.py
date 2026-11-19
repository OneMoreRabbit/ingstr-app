"""The rule for what a conversation yields, against both runtimes' shapes.

Every fixture here is written by hand. **No real conversation from any seat
goes into this repository** (arch, hub #16810): the shapes are taken from the
measurement, the words are invented.
"""

import json

import pytest

from ingstr.conversations import (
    is_wholly_a_system_reminder,
    read_conversation,
)


def _claude_line(**overrides):
    line = {
        "type": "user",
        "uuid": "turn-1",
        "sessionId": "conv-1",
        "timestamp": "2026-11-19T10:00:00Z",
        "message": {"role": "user", "content": "what does the walk do first?"},
    }
    line.update(overrides)
    return line


def _write(tmp_path, name, lines):
    path = tmp_path / name
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def test_a_plain_string_user_message_is_a_turn(tmp_path):
    path = _write(tmp_path, "c.jsonl", [_claude_line()])
    read = read_conversation(path)
    assert [t.text for t in read.turns] == ["what does the walk do first?"]
    assert read.turns[0].role == "person"
    assert read.turns[0].conversation_id == "conv-1"
    assert read.turns[0].turn_id == "turn-1"


def test_assistant_text_blocks_are_turns_and_thinking_is_not():
    """Thinking is ruled out (arch, 2026-11-19).

    It is a fifth of a conversation by volume, it is drafting rather than what
    was said, and indexing it would make an agent's private reasoning readable
    by everyone in its group.
    """
    from ingstr.conversations import claude_turns

    line = {
        "type": "assistant",
        "uuid": "turn-2",
        "sessionId": "conv-1",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "maybe the mount is wrong"},
                {"type": "text", "text": "It classifies before it hashes."},
            ]
        },
    }
    turns = claude_turns(line, 1)
    assert [t.text for t in turns] == ["It classifies before it hashes."]


def test_tool_use_and_tool_result_blocks_are_never_indexed():
    from ingstr.conversations import claude_turns

    line = {
        "type": "assistant",
        "uuid": "turn-3",
        "sessionId": "conv-1",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                {"type": "tool_result", "content": "a.txt b.txt"},
            ]
        },
    }
    assert claude_turns(line, 1) == []


def test_a_user_line_carrying_tool_output_is_not_a_person_talking():
    """Claude Code records tool OUTPUT as a user line.

    918 of the 1,084 user lines in the measured file were this. Without the
    check, two thirds of what looks like somebody talking is machine output —
    and worse, it is file content indexed elsewhere under its own group.
    """
    from ingstr.conversations import claude_turns

    line = _claude_line(
        uuid="turn-4",
        toolUseResult={"stdout": "secret-looking file contents"},
        message={"role": "user", "content": "secret-looking file contents"},
    )
    assert claude_turns(line, 1) == []


def test_session_state_lines_are_not_speech(tmp_path):
    """The 14 other line types in the measured file are not conversation."""
    lines = [
        {"type": "attachment", "attachment": {"content": "a whole file"}},
        {"type": "cost-state", "sessionId": "conv-1", "totalCostUSD": 1.0},
        {"type": "file-history-snapshot", "messageId": "m1", "snapshot": {}},
        {"type": "system", "subtype": "hook", "hookCount": 2},
        _claude_line(uuid="turn-5"),
    ]
    read = _write(tmp_path, "c.jsonl", lines)
    result = read_conversation(read)
    assert len(result.turns) == 1
    assert result.unrecognised == 0


def test_a_sidechain_turn_is_indexed_and_flagged(tmp_path):
    path = _write(
        tmp_path, "c.jsonl", [_claude_line(isSidechain=True, uuid="turn-6")]
    )
    read = read_conversation(path)
    assert read.turns[0].sidechain is True


def test_a_block_that_is_wholly_a_system_reminder_is_dropped(tmp_path):
    text = "<system-reminder>\nthe branch moved\n</system-reminder>"
    assert is_wholly_a_system_reminder(text)
    path = _write(
        tmp_path,
        "c.jsonl",
        [_claude_line(uuid="turn-7", message={"role": "user", "content": text})],
    )
    assert read_conversation(path).turns == []


def test_a_turn_mixing_words_with_injected_text_keeps_both(tmp_path):
    """The limit, tested so it is visible rather than discovered.

    Nothing in the format separates what a person typed from what a hook
    injected into the same line. Dropping the whole turn would lose the
    question; keeping it keeps some noise. The architecture document states
    this rather than implying the rule is clean.
    """
    text = "please check the mount\n<system-reminder>the branch moved</system-reminder>"
    path = _write(
        tmp_path,
        "c.jsonl",
        [_claude_line(uuid="turn-8", message={"role": "user", "content": text})],
    )
    assert len(read_conversation(path).turns) == 1


def test_a_line_without_both_ids_is_skipped_not_guessed(tmp_path):
    """A passage id is built from the conversation and turn ids.

    Without them a re-read could not write the same id, so the turn would be
    duplicated every time the file was read again.
    """
    path = _write(tmp_path, "c.jsonl", [_claude_line(uuid=None)])
    assert read_conversation(path).turns == []


# ── reading from where the last run stopped ────────────────────────────────


def test_reading_resumes_after_the_recorded_line(tmp_path):
    lines = [_claude_line(uuid=f"turn-{i}") for i in range(1, 4)]
    path = _write(tmp_path, "c.jsonl", lines)
    first = read_conversation(path)
    assert len(first.turns) == 3
    assert first.last_line == 3

    with path.open("a") as handle:
        handle.write(json.dumps(_claude_line(uuid="turn-4")) + "\n")

    second = read_conversation(path, from_line=first.last_line)
    assert [t.turn_id for t in second.turns] == ["turn-4"]
    assert second.last_line == 4


def test_a_half_written_last_line_does_not_fail_the_file(tmp_path):
    """A conversation is read while it is being written.

    Refusing the whole file for a partial last line would mean never indexing a
    live conversation — exactly the ones somebody is most likely to search for.
    """
    path = tmp_path / "c.jsonl"
    path.write_text(json.dumps(_claude_line()) + "\n" + '{"type": "user", "mess')
    read = read_conversation(path)
    assert len(read.turns) == 1
    assert read.unparsed == 1


# ── Codex ──────────────────────────────────────────────────────────────────


def _codex_line(**overrides):
    line = {
        "id": "item-1",
        "conversation_id": "codex-conv-1",
        "timestamp": "2026-11-19T11:00:00Z",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "the lock is beside the db"}],
        },
    }
    line.update(overrides)
    return line


def test_a_codex_message_is_a_turn(tmp_path):
    path = _write(tmp_path, "rollout.jsonl", [_codex_line()])
    read = read_conversation(path)
    assert [t.text for t in read.turns] == ["the lock is beside the db"]
    assert read.turns[0].role == "agent"
    assert read.turns[0].conversation_id == "codex-conv-1"


def test_a_codex_function_call_is_not_a_turn(tmp_path):
    path = _write(
        tmp_path,
        "rollout.jsonl",
        [_codex_line(payload={"type": "function_call", "name": "shell"})],
    )
    assert read_conversation(path).turns == []


def test_a_compressed_codex_conversation_is_read(tmp_path):
    """Codex archives conversations as `.jsonl.zst`.

    Written here with the same library the reader uses, because the point of
    the test is the reader's path, not zstd's.
    """
    zstandard = pytest.importorskip("zstandard")
    raw = (json.dumps(_codex_line()) + "\n").encode()
    path = tmp_path / "rollout.jsonl.zst"
    path.write_bytes(zstandard.ZstdCompressor().compress(raw))

    read = read_conversation(path)
    assert [t.text for t in read.turns] == ["the lock is beside the db"]


def test_a_line_that_looks_like_speech_and_fits_no_shape_is_counted(tmp_path):
    """The signal that a runtime changed its format.

    Without it, an update that renamed a field would stop conversations being
    indexed and every run would still report success (constitution §9).
    """
    path = _write(
        tmp_path,
        "c.jsonl",
        [{"type": "user", "words": "a shape nobody has seen"}],
    )
    read = read_conversation(path)
    assert read.turns == []
    assert read.unrecognised == 1
