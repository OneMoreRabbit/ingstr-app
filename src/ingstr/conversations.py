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

#: Codex: the one line type that carries a message. `event_msg`,
#: `item_completed` and `compacted` repeat the same material.
_CODEX_RESPONSE_ITEM = "response_item"

#: A partial answer is superseded by the final answer in the same turn.
_CODEX_PARTIAL = "partial_answer"

#: Where Codex says what KIND of content a message holds. Without it, `role:
#: user` cannot be read as "a person typed this".
_CODEX_KINDS_KEY = "internal_chat_message_metadata_passthrough"
_CODEX_PERSON_KINDS = frozenset({"user.text"})

#: Text a harness injected into a turn, wrapped in a tag of its own. Claude
#: Code writes system reminders; Codex writes its environment context and the
#: instructions it was given. Neither was typed by anybody, and both repeat on
#: every turn — indexed, they would be most of what a search over an agent's
#: conversations returned. The tag names are declared here rather than guessed
#: at the point of use, and a name agent-seat confirms differently is one line
#: to change.
_INJECTED_TAGS = (
    "system-reminder",
    "environment_context",
    "user_instructions",
    "AGENTS.md",
)

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
    #: Lines that were not JSON at all. Counted, but they do not stop the
    #: read: a file that holds one bad line in its middle is still a
    #: conversation.
    unparsed: int = 0
    #: True when the file ended mid-line. The read position stops BEFORE that
    #: line, so the turn is indexed once, when the runtime has finished
    #: writing it.
    incomplete_tail: bool = False


def is_wholly_injected(text: str) -> bool:
    """True when the text is only something a harness put there.

    These arrive as ordinary user messages and were typed by nobody. The limit,
    which the architecture document states rather than hides: a turn that MIXES
    a person's words with injected text keeps both, because the format carries
    nothing that separates them.
    """
    stripped = text.strip()
    return any(
        stripped.startswith(f"<{tag}>") and stripped.endswith(f"</{tag}>")
        for tag in _INJECTED_TAGS
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
        if not text.strip() or is_wholly_injected(text):
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
    # `isMeta` is command output and injected notes; `isCompactSummary` is a
    # summary the model wrote of the conversation it is already in. Indexing
    # either returns the same words twice to a search.
    if line.get("isMeta") or line.get("isCompactSummary"):
        return []
    if not isinstance(line.get("sessionId") or line.get("session_id"), str) or not isinstance(
        line.get("uuid"), str
    ):
        # Speech-shaped, but a passage built from it could not have a stable
        # id. Returning None rather than [] puts it in the unrecognised count:
        # a runtime that renamed these keys must not be able to empty the
        # index in silence.
        return None
    return _turns_from(
        message=message,
        line_type=str(line_type),
        conversation_id=line.get("sessionId") or line.get("session_id"),
        turn_id=line.get("uuid"),
        timestamp=line.get("timestamp"),
        sidechain=bool(line.get("isSidechain")),
        line_number=line_number,
    )


def codex_turns(
    line: dict[str, Any], line_number: int, conversation_id: str
) -> list[Turn] | None:
    """Turns from a Codex line, or None when it is not that shape.

    Written against the shapes agent-seat confirmed for Codex 0.166.0
    (hub #16853), and deliberately narrow, because that answer came with its
    own evidence limit: a generated schema and older local files, not a live
    0.166.0 rollout. So anything this does not recognise is COUNTED, never
    quietly dropped.

    - **One message source.** Only a `response_item` whose payload is a
      `message`. `event_msg`, `item_completed` and `compacted` repeat material
      the response items already hold, and indexing both would return the same
      words twice to one search.
    - **Neither id comes from the line.** The conversation id is in the file's
      `session_meta`; `payload.id` is optional, so the turn id is the line's
      `ordinal` when it has one and its line number otherwise — stable because
      the file is append-only.
    - **`role: user` is not evidence of a person.** Codex puts its AGENTS.md
      instructions and environment context in user messages too. The kinds in
      `internal_chat_message_metadata_passthrough.content_item_kinds` say
      which it is; without them, a user message is NOT indexed as somebody's
      words, because a wrong attribution is worse than a missing passage.
    - **`developer` is never a turn**, and a `partial_answer` is superseded by
      the final answer that follows it.
    """
    if line.get("type") != _CODEX_RESPONSE_ITEM:
        return None
    payload = line.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "message":
        return []
    role = payload.get("role")
    if role not in _SPOKEN_LINE_TYPES:
        # `developer` lands here: permissions and hook text, never a turn.
        return []
    if payload.get("phase") == _CODEX_PARTIAL:
        return []
    if role == "user" and not _is_a_person_speaking(payload):
        # Not dropped in silence: without the kinds there is no way to tell a
        # person's prompt from injected instructions, and that uncertainty is
        # exactly what the unrecognised count is for. agent-seat's answer came
        # with an evidence limit (a generated schema and older files, not a
        # live 0.166.0 rollout), so this number is how the estate finds out
        # that the limit mattered.
        return None
    ordinal = line.get("ordinal")
    turn_id = f"ordinal-{ordinal}" if isinstance(ordinal, int) else f"line-{line_number}"
    return _turns_from(
        message=payload,
        line_type=str(role),
        conversation_id=conversation_id,
        turn_id=turn_id,
        timestamp=line.get("timestamp"),
        sidechain=False,
        line_number=line_number,
    )


def _is_a_person_speaking(payload: dict[str, Any]) -> bool:
    """Whether a Codex `role: user` message is really the person's words.

    The kinds are the only evidence the format carries: `user.text` is a
    person, `agents_md.instructions` and `environments.environment_context`
    are injected and repeat on every conversation. When the field is absent —
    older files, and anything 0.166.0 does differently — this returns False,
    so the message is not indexed and the caller counts it. That way the cost
    of not knowing is a missing passage somebody can ask about, rather than an
    agent's instructions filed under what a person said.
    """
    passthrough = payload.get(_CODEX_KINDS_KEY)
    if not isinstance(passthrough, dict):
        return False
    kinds = passthrough.get("content_item_kinds")
    if not isinstance(kinds, list):
        return False
    return any(kind in _CODEX_PERSON_KINDS for kind in kinds)


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


def read_conversation(
    path: Path, *, from_line: int = 0, conversation_id: str | None = None
) -> ConversationRead:
    """Read the turns of a conversation file after `from_line`.

    `from_line` is the last line already indexed, counting from 1; 0 reads the
    whole file. A line that is not JSON is skipped rather than failing the
    file: a conversation being written while it is read can end in half a line,
    and refusing the whole conversation for that would mean never indexing a
    live one.
    """
    result = ConversationRead(last_line=from_line)
    # A COMPRESSED CONVERSATION CANNOT GROW. Nothing is appended to a file that
    # has been compressed, so its last line is complete even without a
    # trailing newline — and holding it back would mean never indexing the
    # last turn of every archived conversation.
    can_grow = path.suffix != COMPRESSED_SUFFIX
    with opener_for(path)(path) as handle:
        for line_number, raw in enumerate(handle, start=1):
            if line_number <= from_line and not (line_number == 1 and conversation_id is None):
                continue
            # A LINE WITHOUT A TRAILING NEWLINE IS A LINE STILL BEING WRITTEN.
            # The reader and the runtime are looking at the same file, so this
            # is ordinary rather than exceptional. Advancing the read position
            # past it would lose that turn FOR EVER: the runtime finishes the
            # line, and the next run starts after it.
            if can_grow and not raw.endswith("\n"):
                result.incomplete_tail = True
                break
            text = raw.strip()
            if not text:
                result.last_line = line_number
                continue
            try:
                line = json.loads(text)
            except ValueError:
                result.unparsed += 1
                result.last_line = line_number
                continue
            if not isinstance(line, dict):
                result.unparsed += 1
                result.last_line = line_number
                continue
            conversation_id = conversation_id or _session_meta_id(line)
            if line_number <= from_line:
                # Line 1, re-read only to recover the conversation id. Its
                # turns were indexed by an earlier run, and the read position
                # must not move backwards.
                continue
            result.last_line = line_number
            turns = claude_turns(line, line_number)
            if turns is None:
                turns = codex_turns(
                    line, line_number, conversation_id or _conversation_id_from_name(path)
                )
            if turns is None:
                if _looks_like_speech(line):
                    result.unrecognised += 1
                continue
            result.turns.extend(turns)
    return result


def _session_meta_id(line: dict[str, Any]) -> str | None:
    """The conversation id Codex writes in the file's first line.

    A `response_item` line carries no conversation id of its own, so without
    this every Codex turn would be skipped for want of an id — silently, which
    is the failure this module is built to avoid.
    """
    if line.get("type") != "session_meta":
        return None
    payload = line.get("payload")
    source = payload if isinstance(payload, dict) else line
    for key in ("id", "conversation_id", "session_id"):
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _conversation_id_from_name(path: Path) -> str:
    """The last resort: the file name, when the file has no `session_meta`.

    Not used for a resumed read — line 1 is re-read for the id on every pass,
    and the caller also passes the id it stored — because a conversation whose
    id changed between runs would get two sets of passage ids and would look
    like two different conversations to the move check.
    """
    name = path.name
    for suffix in (".zst", ".jsonl"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def split_turn_text(text: str, *, size: int, overlap: int) -> list[str]:
    """Split one turn's words into passage-sized pieces.

    Chunked WITHIN the turn and never across two, so every passage carries one
    speaker and a search result is attributable to the person or to the agent.
    A turn is often short and then this returns it whole; it is sometimes a
    pasted document, and the embedder would silently truncate or refuse that.

    Split on a blank line, then on a line ending, then — only if a single line
    is longer than a passage — mid-line. Prose survives the first two; the
    third is for a pasted file with no line breaks at all.
    """
    if size <= 0:
        raise ValueError("chunk size must be positive")
    if len(text) <= size:
        return [text]

    step = max(1, size - max(0, overlap))
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"))
            # Only honour a break in the second half: a break near the start
            # would make passages that are mostly overlap.
            if cut > size // 2:
                end = start + cut
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        start = max(end, start + step) if overlap == 0 else max(end - overlap, start + 1)
    return pieces or [text]
