"""Indexing the agents' own folders, declared by the plan.

The second source an org can have (`ingstr-deploy` 0.8). An agent keeps its
conversations and notes on its own host; beaver mounts them read-only at
`/mnt/agent-hosts/<host>/<org>/<name>/<folder>/`, and `compiled-rbac-plan` 0.12
declares which folders exist and which group classifies each one.

**The plan classifies, never the files.** A seat's files belong to its home
user, so their group says nothing about who may read them. This is not a new
mechanism: v0.2.2 already classifies by a declaration for `acl_grants`, and
this reads a different section of the same plan.

**A conversation is not a document.** It is read turn by turn, from where the
last run stopped, and only the words are taken (`conversations.py`). Everything
else under an indexed folder — a memory note, a file in `scratch` — is an
ordinary document and goes through the ordinary pipeline with the declared
group.

**Nothing here removes anything.** A conversation that has vanished or shrunk
is recorded as a fault and its passages stay, because conversations are kept
for ever and absence therefore means the export, the host or the retention
setting (`agent-folders-design-v0_1` D4). The only removal in an agent folder
is the declaration changing, which the ordinary removal step already handles:
a folder that stops being declared stops being walked, and its files are then
orphans like any other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import structlog

from .availability import Availability, read_export_state
from .chunk import Chunk
from .config import OrgView
from .conversations import COMPRESSED_SUFFIX
from .plan import AgentFolder, ResolvedPlan
from .secrets_net import looks_like_a_secret
from .state import ConversationRead

_log = structlog.get_logger(__name__)

#: A conversation file, either runtime. Codex compresses archived ones.
_CONVERSATION_SUFFIXES = (".jsonl", ".jsonl" + COMPRESSED_SUFFIX)


@dataclass
class AgentFolderSummary:
    """What the agent-folder pass did, counted and never named.

    Counts only, deliberately: a path here is an agent's own data and the
    journal is on the host.
    """

    folders_declared: int = 0
    folders_read: int = 0
    #: Declared but not mounted, unreadable, or without a marker file. A skip
    #: with a note, never an empty reading (ADR-0010 §6).
    folders_unavailable: int = 0
    conversations_read: int = 0
    turns_indexed: int = 0
    #: Passages dropped because they look like a credential. The value is
    #: never logged, stored or returned — only this number.
    passages_with_secrets: int = 0
    #: Lines that looked like speech and matched neither runtime's shape. Above
    #: zero means a runtime changed its format and the rule needs looking at.
    lines_unrecognised: int = 0
    documents_indexed: int = 0
    documents_errored: int = 0
    conversations_gone: int = 0
    conversations_shrunk: int = 0
    chunks_written: int = 0
    #: Absolute paths read this pass, so the caller can keep them out of the
    #: orphan set — they are not under the org's own root.
    seen: set[str] = field(default_factory=set)


def is_conversation(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in _CONVERSATION_SUFFIXES)


def agent_root_of(mount_root: Path, folder: AgentFolder) -> Path:
    """`<mount>/<host>/<org>/<name>/` — where the marker file lives.

    The marker is per agent, not per folder (`agent-folders-design-v0_1` §3),
    so an agent whose host is down reads as unavailable once rather than once
    per folder.
    """
    return (mount_root / folder.path).parent


def turn_text_is_indexable(text: str, summary: AgentFolderSummary) -> bool:
    """Whether one turn's words go into the index.

    The passage is dropped, not the file: one leaked token in one turn must not
    hide a whole conversation from search.
    """
    caught = looks_like_a_secret(text)
    if caught is None:
        return True
    summary.passages_with_secrets += 1
    _log.info("passage_withheld_secret", pattern=caught)
    return False


def chunks_for_turn(text: str, index: int) -> list[Chunk]:
    """One turn, one passage — chunked within itself if it is long.

    Never across a turn boundary: a passage carries exactly one speaker, so a
    search result is attributable to the person or to the agent and not to a
    seam between them (arch agreed, 2026-11-19).
    """
    return [Chunk(text=text, index=index, total=index + 1)]


def read_agent_folders(
    cfg: OrgView,
    plan: ResolvedPlan,
    *,
    dry_run: bool,
) -> tuple[list[AgentFolder], AgentFolderSummary]:
    """The folders this run will read, and why the others were skipped.

    Split out from the indexing so `ingstr health` can answer "what is declared
    and what can I actually see?" without embedding anything.
    """
    summary = AgentFolderSummary()
    if cfg.agent_source is None:
        return [], summary

    declared = plan.indexed_folders_for_org(cfg.org)
    summary.folders_declared = len(declared)

    readable: list[AgentFolder] = []
    for folder in declared:
        root = agent_root_of(cfg.agent_source.root, folder)
        # Continuity (layer 2) belongs to the caller that holds the state
        # database; what is asked here is layer 1 — is the export there at
        # all. A generation change is never a reason to skip: the export is
        # readable, it is only not evidence that anything was deleted, and
        # nothing in this pass deletes.
        state = read_export_state(root, last_generation=None)
        if state.availability is Availability.UNAVAILABLE:
            summary.folders_unavailable += 1
            _log.warning(
                "agent_folder_unavailable",
                agent=folder.name,
                folder=folder.folder,
                reason=state.reason,
            )
            continue
        directory = cfg.agent_source.root / folder.path
        if not directory.is_dir():
            summary.folders_unavailable += 1
            _log.warning(
                "agent_folder_missing",
                agent=folder.name,
                folder=folder.folder,
                reason="declared by the plan but not a directory under the mount",
            )
            continue
        summary.folders_read += 1
        readable.append(folder)
    return readable, summary


def conversation_fault(path: Path, known: ConversationRead | None) -> str | None:
    """`'gone'`, `'shrunk'`, or None — the file is as we left it or new.

    Shrunk is the second half of the same fault. A conversation that is
    suddenly shorter than the lines already indexed has not been edited by a
    person: it has been replaced, truncated, or restored from somewhere older.
    Treating it as "changed" would re-index it from the top and quietly leave
    the passages of the turns that are now missing.
    """
    if not path.exists():
        return "gone" if known is not None else None
    if known is None:
        return None
    return "shrunk" if path.stat().st_size < known.size_bytes else None
