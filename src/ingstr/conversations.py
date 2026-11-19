"""Reading an agent's conversation: only the words, and only once.

Claude Code and Codex write JSON Lines, one object per line, appended as the
conversation runs. Two things follow, and they are the whole of this module.

**Only the words are indexed.** Measured on a real Claude Code conversation
(7,293 lines, 12 MB, of which about 3 MB is message content): tool calls and
their output are 70% of it, the agent's thinking 20%, and what the person and
the agent actually said about 10%. Indexing the rest would fill an org's search
with machine chatter, and worse: a tool result holds the contents of files
indexed elsewhere under their own classification, so copying it into a passage
here would give that content a different group. The rule is in
`ingstr-architecture-v0_7`, ruled by arch 2026-11-19, and it is deliberately a
whitelist — a line or block type arriving in a runtime update is not indexed
until somebody looks at it.

**A conversation is read from where the last run stopped.** The file is
append-only and grows for as long as the conversation lives, so a run that read
it whole would re-parse and re-embed everything every five minutes, for every
live conversation. A run reads from a recorded line count, and a passage id is
built from the conversation and turn ids rather than from a chunk's position in
the file — an append is then a pure insert.

**Two runtimes, one rule, and a count when neither fits.** Claude Code and
Codex shape a line differently; both are read here, and a line that looks like
speech but matches neither shape is COUNTED rather than silently dropped. That
count is the signal that a runtime has changed its format — without it, an
update would quietly stop indexing conversations and every run would still
report success (constitution §9).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

#: Claude Code: the two line types that carry what was said. Everything else in
#: the file is session state — attachments, hook output, costs, file-history
#: snapshots, titles, modes, queue operations (14 further types in the measured
#: file).
_SPOKEN_LINE_TYPES = frozenset({"user", "assistant"})

#: The block types that are words. `tool_use` and `tool_result` are machine
#: traffic; `thinking` is drafting rather than what was said, and indexing it
#: would make an agent's private reasoning searchable by everyone in its group
#: (arch, 2026-11-19 — the operator may overturn it). Codex spells its text
#: blocks `input_text` and `output_text`.
_SPOKEN_BLOCK_TYPES = frozenset({"text", "input_text", "output_text"})

#: Claude Code records tool OUTPUT as a user line carrying this key. Without
#: this check two thirds of what looks like a person talking is machine output
#: (918 of the 1,084 user lines in the measured file).
_TOOL_RESULT_KEY = "toolUseResult"

_ROLE_OF_LINE = {"user": "person", "assistant": "agent"}

_SYSTEM_REMINDER_OPEN = "<system-reminder>"
_SYSTEM_REMINDER_CLOSE = "</system-reminder>"

#: Codex compresses archived conversations. A compressed file is read whole
#: every time: nothing is appended to a file that has been compressed, so there
#: is no read position to keep for it.
COMPRESSED_SUFFIX = ".zst"


@dataclass(frozen=True)
class Turn:
    """One thing said, by one speaker, in one conversation."""

    conversation_id: str
    turn_id: str
    role: str
    text: str
    #: ISO-8601 as the runtime wrote it, or "" when the line carries none. Not
    #: parsed: it is payload for a reader, and inventing a time for a line that
    #: has none would be worse than having none.
    turn_time: str
    sidechain: bool
    #: Which line of the file this turn came from, counting from 1. A file's
    #: read position is the highest line it has yielded.
    line_number: int


@dataclass
class ConversationRead:
    """What one pass over a conversation file found."""

    turns: list[Turn] = field(default_factory=list)
    #: The last line read, counting from 1 — the next run starts after it.
    last_line: int = 0
    #: Lines that looked like speech and matched neither runtime's shape. A
    #: number above zero is the signal that a runtime changed its format.
    unrecognised: int = 0
    #: Lines that were not JSON at all. A conversation being written while it
    #: is read can end in half a line, so this is expected to be 0 or 1.
    unparsed: int = 0


def is_wholly_a_system_reminder(text: str) -> bool:
    """True when the text is only a reminder the harness injected.

    These arrive inside a user line and were typed by nobody. The limit, which
    the architecture document states rather than hides: a line that MIXES a
    person's words with injected text keeps both, because the format carries
    nothing that separates them.
    """
    stripped = text.strip()
    return stripped.startswith(_SYSTEM_REMINDER_OPEN) and stripped.endswith(
        _SYSTEM_REMINDER_CLOSE
    )


def _texts_of(message: dict[str, Any]) -> list[str]:
    """The spoken text of one message, which may be a string or blocks."""
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") not in _SPOKEN_BLOCK_TYPES:
            continue
        text = block.get("text")
        if isinstance(text, str):
            out.append(text)
    return out


def _turns_from(
    *,
    message: dict[str, Any],
    line_type: str,
    conversation_id: Any,
    turn_id: Any,
    timestamp: Any,
    sidechain: bool,
    line_number: int,
) -> list[Turn]:
    if not isinstance(conversation_id, str) or not isinstance(turn_id, str):
        # Without both ids a passage has no stable id, so a re-read could not
        # write the same one. Skipped rather than invented.
        return []
    out: list[Turn] = []
    for index, text in enumerate(_texts_of(message)):
        if not text.strip() or is_wholly_a_system_reminder(text):
            continue
        out.append(
            Turn(
                conversation_id=conversation_id,
                # One line may carry several text blocks; each passage keeps a
                # distinct id so a re-read writes exactly the ids it wrote.
                turn_id=turn_id if index == 0 else f"{turn_id}#{index}",
                role=_ROLE_OF_LINE[line_type],
                text=text,
                turn_time=timestamp if isinstance(timestamp, str) else "",
                sidechain=sidechain,
                line_number=line_number,
            )
        )
    return out


def claude_turns(line: dict[str, Any], line_number: int) -> list[Turn] | None:
    """Turns from a Claude Code line, or None when it is not that shape.

    None and `[]` differ on purpose: `[]` is a line of this shape that said
    nothing indexable (a tool result, a thinking block); None means "not mine",
    and the caller then tries the other runtime before counting the line as
    unrecognised.
    """
    line_type = line.get("type")
    if line_type not in _SPOKEN_LINE_TYPES:
        return None
    message = line.get("message")
    if not isinstance(message, dict):
        return None
    if line.get(_TOOL_RESULT_KEY) is not None:
        return []
    return _turns_from(
        message=message,
        line_type=str(line_type),
        conversation_id=line.get("sessionId") or line.get("session_id"),
        turn_id=line.get("uuid"),
        timestamp=line.get("timestamp"),
        sidechain=bool(line.get("isSidechain")),
        line_number=line_number,
    )


def codex_turns(line: dict[str, Any], line_number: int) -> list[Turn] | None:
    """Turns from a Codex line, or None when it is not that shape.

    Codex wraps each item: the message sits in `payload`, with the role on it
    rather than on the line. A line whose payload is a function call or its
    output is this shape and yields nothing — the same rule as Claude's tool
    traffic, which is why it returns `[]` and not None.
    """
    payload = line.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "message":
        return []
    role = payload.get("role")
    if role not in _SPOKEN_LINE_TYPES:
        return []
    return _turns_from(
        message=payload,
        line_type=str(role),
        conversation_id=line.get("conversation_id") or line.get("session_id"),
        turn_id=line.get("id") or payload.get("id"),
        timestamp=line.get("timestamp"),
        sidechain=bool(line.get("isSidechain")),
        line_number=line_number,
    )


def _looks_like_speech(line: dict[str, Any]) -> bool:
    """Whether a line that matched no shape was probably somebody talking.

    Only such lines are counted as unrecognised: a file is full of state lines
    that are not speech and never were, and counting those would make the
    signal useless by burying it (constitution §9).
    """
    if line.get("type") in _SPOKEN_LINE_TYPES:
        return True
    payload = line.get("payload")
    return isinstance(payload, dict) and payload.get("type") == "message"


@contextmanager
def _open_plain(path: Path) -> Iterator[IO[str]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        yield handle


@contextmanager
def _open_compressed(path: Path) -> Iterator[IO[str]]:
    """Read a `.jsonl.zst` conversation, as Codex archives them."""
    import io

    import zstandard

    with path.open("rb") as raw:
        reader = zstandard.ZstdDecompressor().stream_reader(raw)
        yield io.TextIOWrapper(reader, encoding="utf-8", errors="replace")


def opener_for(path: Path) -> Callable[[Path], Any]:
    return _open_compressed if path.suffix == COMPRESSED_SUFFIX else _open_plain


def read_conversation(path: Path, *, from_line: int = 0) -> ConversationRead:
    """Read the turns of a conversation file after `from_line`.

    `from_line` is the last line already indexed, counting from 1; 0 reads the
    whole file. A line that is not JSON is skipped rather than failing the
    file: a conversation being written while it is read can end in half a line,
    and refusing the whole conversation for that would mean never indexing a
    live one.
    """
    result = ConversationRead(last_line=from_line)
    with opener_for(path)(path) as handle:
        for line_number, raw in enumerate(handle, start=1):
            if line_number <= from_line:
                continue
            result.last_line = line_number
            text = raw.strip()
            if not text:
                continue
            try:
                line = json.loads(text)
            except ValueError:
                result.unparsed += 1
                continue
            if not isinstance(line, dict):
                result.unparsed += 1
                continue
            turns = claude_turns(line, line_number)
            if turns is None:
                turns = codex_turns(line, line_number)
            if turns is None:
                if _looks_like_speech(line):
                    result.unrecognised += 1
                continue
            result.turns.extend(turns)
    return result
