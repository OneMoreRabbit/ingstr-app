"""The rule for what a conversation yields, against both runtimes' shapes.

Every fixture here is written by hand. **No real conversation from any seat
goes into this repository** (arch, hub #16810): the shapes are taken from the
measurement, the words are invented.
"""

import json

import pytest

from ingstr.conversations import (
    is_wholly_injected,
    read_conversation,
    split_turn_text,
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
    assert is_wholly_injected(text)
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
    assert read.incomplete_tail is True


def test_a_half_written_turn_is_indexed_once_when_it_is_finished(tmp_path):
    """The read position must NOT pass a line still being written.

    It did in the first build: the position moved to the unparsable line, the
    runtime finished writing it, and the next run started after it. That turn
    was then lost for ever, with nothing saying so — and the busier the seat,
    the more often it would happen.
    """
    path = tmp_path / "c.jsonl"
    whole = json.dumps(_claude_line(uuid="turn-9", message={
        "role": "user", "content": "the half-written one"
    }))
    path.write_text(json.dumps(_claude_line()) + "\n" + whole[: len(whole) // 2])

    first = read_conversation(path)
    assert [t.turn_id for t in first.turns] == ["turn-1"]
    assert first.last_line == 1  # NOT 2

    path.write_text(json.dumps(_claude_line()) + "\n" + whole + "\n")
    second = read_conversation(path, from_line=first.last_line)
    assert [t.turn_id for t in second.turns] == ["turn-9"]
    assert second.last_line == 2


def test_a_bad_line_in_the_middle_does_not_stop_the_read(tmp_path):
    """Only the TAIL is held back. A line that is broken and followed by
    others was never going to be finished."""
    path = tmp_path / "c.jsonl"
    path.write_text(
        json.dumps(_claude_line(uuid="turn-a")) + "\n"
        + "{not json at all\n"
        + json.dumps(_claude_line(uuid="turn-b")) + "\n"
    )
    read = read_conversation(path)
    assert [t.turn_id for t in read.turns] == ["turn-a", "turn-b"]
    assert read.unparsed == 1
    assert read.last_line == 3


# ── Codex ──────────────────────────────────────────────────────────────────


_CODEX_META = {
    "type": "session_meta",
    "payload": {"id": "codex-conv-1", "timestamp": "2026-11-19T11:00:00Z"},
}


def _codex_line(**overrides):
    line = {
        "type": "response_item",
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
    path = _write(tmp_path, "rollout.jsonl", [_CODEX_META, _codex_line()])
    read = read_conversation(path)
    assert [t.text for t in read.turns] == ["the lock is beside the db"]
    assert read.turns[0].role == "agent"
    assert read.turns[0].conversation_id == "codex-conv-1"


def test_a_codex_function_call_is_not_a_turn(tmp_path):
    path = _write(
        tmp_path,
        "rollout.jsonl",
        [_CODEX_META, _codex_line(payload={"type": "function_call", "name": "shell"})],
    )
    assert read_conversation(path).turns == []


def test_a_compressed_codex_conversation_is_read(tmp_path):
    """Codex archives conversations as `.jsonl.zst`.

    Written here with the same library the reader uses, because the point of
    the test is the reader's path, not zstd's.
    """
    zstandard = pytest.importorskip("zstandard")
    raw = "".join(
        json.dumps(line) + "\n" for line in (_CODEX_META, _codex_line())
    ).encode()
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


# ── arch's review of 17da17d (hub #16843) ──────────────────────────────────


def test_a_meta_user_line_is_not_a_person_talking(tmp_path):
    """`isMeta` is command output and injected notes, not speech."""
    path = _write(tmp_path, "c.jsonl", [_claude_line(uuid="turn-m", isMeta=True)])
    assert read_conversation(path).turns == []


def test_a_compact_summary_is_not_indexed(tmp_path):
    """It is the model's summary of the conversation it is already in, so
    indexing it returns the same words twice to one search."""
    path = _write(
        tmp_path, "c.jsonl", [_claude_line(uuid="turn-s", isCompactSummary=True)]
    )
    assert read_conversation(path).turns == []


def test_speech_without_usable_ids_is_counted_not_dropped(tmp_path):
    """A runtime renaming `sessionId` must not empty the index in silence."""
    path = _write(
        tmp_path,
        "c.jsonl",
        [{"type": "user", "message": {"content": "hello"}, "conversationId": "x"}],
    )
    read = read_conversation(path)
    assert read.turns == []
    assert read.unrecognised == 1


def _codex_user(kinds, text="where does the lock live?"):
    payload = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }
    if kinds is not None:
        payload["internal_chat_message_metadata_passthrough"] = {
            "content_item_kinds": kinds
        }
    return _codex_line(payload=payload)


def test_a_codex_user_message_is_a_person_when_the_kinds_say_so(tmp_path):
    path = _write(tmp_path, "rollout.jsonl", [_CODEX_META, _codex_user(["user.text"])])
    read = read_conversation(path)
    assert [t.role for t in read.turns] == ["person"]


def test_codex_injected_material_is_not_a_person_talking(tmp_path):
    """Codex puts its AGENTS.md instructions and environment context in USER
    messages. They were typed by nobody and repeat on every conversation."""
    for kinds in (["agents_md.instructions"], ["environments.environment_context"]):
        path = _write(
            tmp_path, "rollout.jsonl", [_CODEX_META, _codex_user(kinds, "cwd: /work")]
        )
        assert read_conversation(path).turns == []


def test_a_codex_user_message_without_kinds_is_counted_not_assumed(tmp_path):
    """agent-seat could not confirm a live 0.166.0 rollout, so the field may be
    absent. Role alone cannot tell a person's prompt from injected text, and a
    wrong attribution is worse than a missing passage — so it is refused AND
    counted, which is how the estate learns the limit mattered."""
    path = _write(tmp_path, "rollout.jsonl", [_CODEX_META, _codex_user(None)])
    read = read_conversation(path)
    assert read.turns == []
    assert read.unrecognised == 1


def test_a_developer_message_is_never_a_turn(tmp_path):
    path = _write(
        tmp_path,
        "rollout.jsonl",
        [
            _CODEX_META,
            _codex_line(
                payload={
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "permissions"}],
                }
            ),
        ],
    )
    read = read_conversation(path)
    assert read.turns == []
    assert read.unrecognised == 0


def test_a_partial_answer_is_superseded_by_the_final_one(tmp_path):
    """Otherwise the same answer is indexed twice, half of it truncated."""
    partial = _codex_line(
        payload={
            "type": "message",
            "role": "assistant",
            "phase": "partial_answer",
            "content": [{"type": "output_text", "text": "the lock is"}],
        }
    )
    final = _codex_line(
        payload={
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "the lock is beside the db"}],
        }
    )
    path = _write(tmp_path, "rollout.jsonl", [_CODEX_META, partial, final])
    read = read_conversation(path)
    assert [t.text for t in read.turns] == ["the lock is beside the db"]


def test_an_event_msg_repeating_a_message_is_not_indexed_twice(tmp_path):
    """event_msg, item_completed and compacted repeat what the response items
    already hold: one message source, not both."""
    event = {
        "type": "event_msg",
        "timestamp": "2026-11-19T11:00:01Z",
        "payload": {"type": "item_completed", "item": {"text": "the lock is beside the db"}},
    }
    path = _write(tmp_path, "rollout.jsonl", [_CODEX_META, _codex_line(), event])
    read = read_conversation(path)
    assert [t.text for t in read.turns] == ["the lock is beside the db"]
    assert read.unrecognised == 0


def test_a_turn_context_line_is_not_speech(tmp_path):
    path = _write(
        tmp_path,
        "rollout.jsonl",
        [_CODEX_META, {"type": "turn_context", "payload": {"turn_id": "t", "model": "m"}}],
    )
    read = read_conversation(path)
    assert read.turns == []
    assert read.unrecognised == 0


def test_a_codex_turn_uses_the_ordinal_when_the_file_has_one(tmp_path):
    path = _write(tmp_path, "rollout.jsonl", [_CODEX_META, _codex_line(ordinal=7)])
    assert read_conversation(path).turns[0].turn_id == "ordinal-7"


def test_a_codex_turn_falls_back_to_the_line_number(tmp_path):
    """`payload.id` is optional and older files have no ordinal.

    The line number is stable BECAUSE the file is append-only: a line never
    moves, so a re-read writes the id it wrote before.
    """
    path = _write(tmp_path, "rollout.jsonl", [_CODEX_META, _codex_line()])
    read = read_conversation(path)
    assert read.turns[0].turn_id == "line-2"
    assert read.turns[0].conversation_id == "codex-conv-1"


def test_a_resumed_read_keeps_the_same_conversation_id(tmp_path):
    """The id must not change between runs.

    The first build took it from `session_meta` on the first run and from the
    file name on the next, so the same conversation got two ids: its passages
    were written twice under different ids, and the move check compared unlike
    values. Line 1 is re-read for the id on every pass.
    """
    path = _write(
        tmp_path,
        "rollout-2026-11-19T11-00-00-codex-conv-1.jsonl",
        [_CODEX_META, _codex_line()],
    )
    first = read_conversation(path)
    assert first.turns[0].conversation_id == "codex-conv-1"

    with path.open("a") as handle:
        handle.write(json.dumps(_codex_line(ordinal=3)) + "\n")
    second = read_conversation(path, from_line=first.last_line)

    assert [t.conversation_id for t in second.turns] == ["codex-conv-1"]
    assert second.last_line == 3


def test_a_stored_conversation_id_is_used_as_given(tmp_path):
    """The caller holds the id it wrote with; re-reading line 1 is the belt,
    this is the braces — a file whose first line has gone still keeps its
    identity."""
    path = _write(tmp_path, "rollout.jsonl", [_codex_line()])
    read = read_conversation(path, conversation_id="codex-conv-1")
    assert read.turns[0].conversation_id == "codex-conv-1"


def test_a_file_with_no_session_meta_falls_back_to_its_name(tmp_path):
    path = _write(tmp_path, "rollout-codex-conv-5.jsonl", [_codex_line()])
    read = read_conversation(path)
    assert read.turns[0].conversation_id == "rollout-codex-conv-5"


def test_the_last_line_of_a_compressed_conversation_is_complete(tmp_path):
    """A compressed file cannot grow, so its last line is finished even
    without a trailing newline. Held back, the last turn of every archived
    conversation would never be indexed."""
    zstandard = pytest.importorskip("zstandard")
    raw = (
        json.dumps(_CODEX_META) + "\n" + json.dumps(_codex_line())  # no newline
    ).encode()
    path = tmp_path / "rollout.jsonl.zst"
    path.write_bytes(zstandard.ZstdCompressor().compress(raw))

    read = read_conversation(path)
    assert len(read.turns) == 1
    assert read.incomplete_tail is False


# ── a long turn is chunked within itself ───────────────────────────────────


def test_a_short_turn_is_one_passage():
    assert split_turn_text("one line", size=100, overlap=10) == ["one line"]


def test_a_long_turn_is_split_on_a_line_ending():
    text = "\n".join(f"line {i} of an answer that goes on" for i in range(50))
    pieces = split_turn_text(text, size=200, overlap=20)
    assert len(pieces) > 1
    assert all(len(piece) <= 200 for piece in pieces)
    # Nothing is lost: every line appears somewhere.
    joined = " ".join(pieces)
    assert "line 0 " in joined and "line 49 " in joined


def test_a_pasted_block_with_no_line_breaks_is_still_split():
    """The case the line-ending rule cannot help with: a pasted file."""
    text = "x" * 5000
    pieces = split_turn_text(text, size=1000, overlap=0)
    assert len(pieces) == 5
    assert all(len(piece) <= 1000 for piece in pieces)
