"""Indexing the agents' folders, end to end through `run_ingest`.

The plan rows are the real shape of `compiled-rbac-plan` 0.12's
`agent_folders[]`. Every conversation here is written by hand: **no real
conversation from any seat goes into this repository** (arch, hub #16810).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ingstr.config import (
    AgentSourceConfig,
    ChunkingConfig,
    EmbeddingConfig,
    IngstrConfig,
    LoggingConfig,
    OrgConfig,
    OrgView,
    PlanConfig,
    QdrantConfig,
    SourceConfig,
    StateConfig,
)
from ingstr.pipeline import run_ingest
from ingstr.plan import AgentFolder, ResolvedPlan
from ingstr.state import StateDB

_GROUP = "arc_g0_engineering_global"
_OTHER_GROUP = "arc_g18_any_global"
_FOLDER_PATH = "marten/arc/test-claude/sessions/"


def _cfg(tmp_path: Path, *, mount: Path | None) -> OrgView:
    source_root = tmp_path / "data"
    source_root.mkdir(exist_ok=True)
    whole = IngstrConfig(
        orgs=[
            OrgConfig(
                org="arc",
                source=SourceConfig(root=source_root),
                agent_source=AgentSourceConfig(root=mount) if mount else None,
                qdrant=QdrantConfig(url="http://x", api_key_env="X", collection="c"),
                state=StateConfig(db_path=tmp_path / "state.db"),
            )
        ],
        plan=PlanConfig(
            compiled_plan_path=tmp_path / "_p", group_gid_map_path=tmp_path / "_m"
        ),
        embedding=EmbeddingConfig(endpoint="http://x", model="m", vector_dim=4),
        chunking=ChunkingConfig(),
        logging=LoggingConfig(),
    )
    return whole.for_org("arc")


def _plan(*folders: AgentFolder, declares: bool = True) -> ResolvedPlan:
    return ResolvedPlan(
        gid_to_group={1003: _GROUP, 1004: _OTHER_GROUP},
        required_groups=frozenset({_GROUP, _OTHER_GROUP}),
        group_org={_GROUP: "arc", _OTHER_GROUP: "arc"},
        grants={},
        agent_folders=folders,
        declares_agent_folders=declares,
        plan_source_path=Path("/dev/null"),
        map_source_path=Path("/dev/null"),
    )


def _folder(**overrides) -> AgentFolder:
    fields = {
        "name": "test-claude",
        "kind": "seat",
        "host": "marten",
        "org": "arc",
        "folder": "sessions",
        "path": _FOLDER_PATH,
        "group": _GROUP,
        "indexed": True,
    }
    fields.update(overrides)
    return AgentFolder(**fields)


def _turn(uuid: str, text: str, *, role: str = "user") -> dict:
    return {
        "type": role,
        "uuid": uuid,
        "sessionId": "conv-1",
        "timestamp": "2026-11-19T10:00:00Z",
        "message": {"role": role, "content": text},
    }


def _mount_with_conversation(tmp_path: Path, lines: list[dict]) -> tuple[Path, Path]:
    """A mount with the marker file the design requires, and one conversation."""
    mount = tmp_path / "agent-hosts"
    folder = mount / _FOLDER_PATH
    (folder / "claude").mkdir(parents=True)
    (mount / "marten/arc/test-claude/.export-ok").write_text(
        "generation=5d1b0f94-1f0a-4c2e-9a3d-7b2f1c0e4a55\n"
    )
    conversation = folder / "claude" / "project.jsonl"
    conversation.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return mount, conversation


@pytest.fixture
def run(tmp_path):
    """Run one ingest over a mount, with Qdrant and the embedder faked."""

    def _run(mount: Path | None, plan: ResolvedPlan, *, full=False, dry_run=False, state=None):
        cfg = _cfg(tmp_path, mount=mount)
        owned = state is None
        state = state or StateDB(tmp_path / "state.db")
        embedder = MagicMock()
        embedder.embed.side_effect = lambda texts: [[0.1, 0.2, 0.3, 0.4] for _ in texts]
        qdrant = MagicMock()
        try:
            summary = run_ingest(
                cfg,
                plan,
                state=state,
                embedder=embedder,
                qdrant=qdrant,
                full=full,
                dry_run=dry_run,
            )
        finally:
            if owned:
                state.close()
        return summary, qdrant, embedder

    return _run


def test_a_conversation_is_indexed_under_the_declared_group(tmp_path, run):
    """The whole point: the plan classifies, never the file.

    A seat's conversations belong to its home user, so their own group says
    nothing about who may read them.
    """
    mount, _ = _mount_with_conversation(
        tmp_path, [_turn("t1", "where does the lock live?")]
    )
    summary, qdrant, _ = run(mount, _plan(_folder()))

    assert summary.agent_folders is not None
    assert summary.agent_folders.turns_indexed == 1
    points = qdrant.upsert_points.call_args.args[0]
    assert points[0].payload["classification_group"] == _GROUP
    assert points[0].payload["agent"] == "test-claude"
    assert points[0].payload["runtime"] == "claude"
    assert points[0].payload["role"] == "person"
    assert points[0].payload["conversation_id"] == "conv-1"


def test_a_second_run_indexes_only_the_new_turns(tmp_path, run):
    """Otherwise one new turn re-embeds a 12 MB file, every five minutes."""
    mount, conversation = _mount_with_conversation(tmp_path, [_turn("t1", "first")])
    state = StateDB(tmp_path / "state.db")
    try:
        run(mount, _plan(_folder()), state=state)
        with conversation.open("a") as handle:
            handle.write(json.dumps(_turn("t2", "second")) + "\n")
        summary, qdrant, embedder = run(mount, _plan(_folder()), state=state)
    finally:
        state.close()

    assert summary.agent_folders.turns_indexed == 1
    assert embedder.embed.call_args.args[0] == ["second"]


def test_an_unchanged_conversation_embeds_nothing(tmp_path, run):
    mount, _ = _mount_with_conversation(tmp_path, [_turn("t1", "first")])
    state = StateDB(tmp_path / "state.db")
    try:
        run(mount, _plan(_folder()), state=state)
        summary, qdrant, embedder = run(mount, _plan(_folder()), state=state)
    finally:
        state.close()

    assert summary.agent_folders.turns_indexed == 0
    embedder.embed.assert_not_called()
    qdrant.upsert_points.assert_not_called()


def test_a_full_run_rereads_and_writes_the_same_ids(tmp_path, run):
    """Re-reading must not duplicate: the id is the conversation and the turn."""
    mount, _ = _mount_with_conversation(tmp_path, [_turn("t1", "first")])
    state = StateDB(tmp_path / "state.db")
    try:
        _, first_qdrant, _ = run(mount, _plan(_folder()), state=state)
        _, second_qdrant, _ = run(mount, _plan(_folder()), state=state, full=True)
    finally:
        state.close()

    first = first_qdrant.upsert_points.call_args.args[0]
    second = second_qdrant.upsert_points.call_args.args[0]
    assert [p.id for p in first] == [p.id for p in second]


def test_a_passage_that_looks_like_a_credential_is_withheld(tmp_path, run):
    """The passage, not the file: one leaked token must not hide a conversation."""
    mount, _ = _mount_with_conversation(
        tmp_path,
        [
            _turn("t1", "the token is ghp_aaaabbbbccccddddeeeeffffgggghhhh"),
            _turn("t2", "and the mount is read-only"),
        ],
    )
    summary, qdrant, _ = run(mount, _plan(_folder()))

    assert summary.agent_folders.passages_with_secrets == 1
    assert summary.agent_folders.turns_indexed == 1
    texts = [p.payload["text"] for p in qdrant.upsert_points.call_args.args[0]]
    assert texts == ["and the mount is read-only"]


def test_a_missing_marker_is_a_skip_with_a_note_not_an_empty_reading(tmp_path, run):
    """ADR-0010 §6: a mount that is not there must never empty an index."""
    mount, _ = _mount_with_conversation(tmp_path, [_turn("t1", "first")])
    (mount / "marten/arc/test-claude/.export-ok").unlink()

    summary, qdrant, _ = run(mount, _plan(_folder()))

    assert summary.agent_folders.folders_declared == 1
    assert summary.agent_folders.folders_unavailable == 1
    assert summary.agent_folders.turns_indexed == 0
    qdrant.delete_points_by_source_path.assert_not_called()


def test_a_vanished_conversation_keeps_its_passages_and_is_reported(tmp_path, run):
    """Conversations are kept for ever, so absence is a fault, never a removal."""
    mount, conversation = _mount_with_conversation(tmp_path, [_turn("t1", "first")])
    state = StateDB(tmp_path / "state.db")
    try:
        run(mount, _plan(_folder()), state=state)
        conversation.unlink()
        summary, qdrant, _ = run(mount, _plan(_folder()), state=state, full=True)
        faults = state.outstanding_conversation_faults()
    finally:
        state.close()

    assert summary.agent_folders.conversations_gone == 1
    assert faults == {"gone": 1}
    qdrant.delete_points_by_source_path.assert_not_called()


def test_a_shrunk_conversation_is_the_same_fault(tmp_path, run):
    """Shorter than we have read means replaced or truncated, not edited."""
    mount, conversation = _mount_with_conversation(
        tmp_path, [_turn("t1", "first"), _turn("t2", "second")]
    )
    state = StateDB(tmp_path / "state.db")
    try:
        run(mount, _plan(_folder()), state=state)
        conversation.write_text("")
        summary, qdrant, _ = run(mount, _plan(_folder()), state=state)
        faults = state.outstanding_conversation_faults()
    finally:
        state.close()

    assert summary.agent_folders.conversations_shrunk == 1
    assert faults == {"shrunk": 1}
    qdrant.delete_points_by_source_path.assert_not_called()


def test_a_returning_conversation_clears_the_fault(tmp_path, run):
    """A warning that never clears is one nobody reads."""
    mount, conversation = _mount_with_conversation(tmp_path, [_turn("t1", "first")])
    state = StateDB(tmp_path / "state.db")
    try:
        run(mount, _plan(_folder()), state=state)
        text = conversation.read_text()
        conversation.unlink()
        run(mount, _plan(_folder()), state=state, full=True)
        conversation.write_text(text)
        run(mount, _plan(_folder()), state=state, full=True)
        faults = state.outstanding_conversation_faults()
    finally:
        state.close()

    assert faults == {}


def test_a_folder_the_plan_stops_declaring_loses_its_passages(tmp_path, run):
    """The one removal an agent folder has: access withdrawn.

    Tested against the DECLARATION and never against the filesystem — an
    undeclared folder is a decision, an unreachable one is a fault.
    """
    mount, _ = _mount_with_conversation(tmp_path, [_turn("t1", "first")])
    state = StateDB(tmp_path / "state.db")
    try:
        run(mount, _plan(_folder()), state=state)
        summary, qdrant, _ = run(mount, _plan(), state=state)
        remaining = state.all_conversation_paths()
    finally:
        state.close()

    qdrant.delete_points_by_source_path.assert_called_once()
    assert remaining == set()
    assert summary.passages_removed_lost_access == 1


def test_a_changed_group_moves_the_passages_already_written(tmp_path, run):
    """Nothing else would: the turns are not re-read, so nothing rewrites them."""
    mount, _ = _mount_with_conversation(tmp_path, [_turn("t1", "first")])
    state = StateDB(tmp_path / "state.db")
    try:
        run(mount, _plan(_folder()), state=state)
        _, qdrant, _ = run(mount, _plan(_folder(group=_OTHER_GROUP)), state=state)
    finally:
        state.close()

    qdrant.set_classification_group.assert_called_once()
    assert qdrant.set_classification_group.call_args.args[1] == _OTHER_GROUP


def test_a_folder_that_is_not_indexed_is_not_read(tmp_path, run):
    mount, _ = _mount_with_conversation(tmp_path, [_turn("t1", "first")])
    summary, qdrant, _ = run(mount, _plan(_folder(indexed=False)))

    assert summary.agent_folders.folders_declared == 0
    qdrant.upsert_points.assert_not_called()


def test_a_dry_run_writes_nothing_about_an_agent_folder(tmp_path, run):
    mount, _ = _mount_with_conversation(tmp_path, [_turn("t1", "first")])
    state = StateDB(tmp_path / "state.db")
    try:
        summary, qdrant, _ = run(mount, _plan(_folder()), state=state, dry_run=True)
        positions = state.all_conversation_paths()
    finally:
        state.close()

    assert summary.agent_folders.turns_indexed == 1  # it says what it WOULD do
    assert positions == set()
    qdrant.upsert_points.assert_not_called()


def test_agent_folder_files_are_not_orphaned_by_a_full_run(tmp_path, run):
    """They live under a different root, and a full run must not delete them.

    The removal step takes `known - seen`, so a pass that had not added its
    own files to `seen` would read every one of them as a file gone from disk.
    """
    mount, _ = _mount_with_conversation(tmp_path, [_turn("t1", "first")])
    state = StateDB(tmp_path / "state.db")
    try:
        run(mount, _plan(_folder()), state=state)
        summary, qdrant, _ = run(mount, _plan(_folder()), state=state, full=True)
    finally:
        state.close()

    assert summary.files_deleted == 0
    qdrant.delete_points_by_source_path.assert_not_called()


def test_an_org_without_agent_source_is_untouched(tmp_path, run):
    summary, qdrant, _ = run(None, _plan(_folder()))
    assert summary.agent_folders is None


def _memory_folder(tmp_path: Path) -> tuple[Path, Path]:
    """A `memory` folder with one note — an ordinary document, declared group."""
    mount = tmp_path / "agent-hosts"
    folder = mount / "marten/arc/test-claude/memory"
    folder.mkdir(parents=True, exist_ok=True)
    (mount / "marten/arc/test-claude/.export-ok").write_text(
        "generation=5d1b0f94-1f0a-4c2e-9a3d-7b2f1c0e4a55\n"
    )
    note = folder / "what-the-operator-asked.md"
    note.write_text("# Notes\n\nThe nightly run waits for the lock.\n")
    return mount, note


_MEMORY = {"folder": "memory", "path": "marten/arc/test-claude/memory/"}


def test_a_memory_note_is_an_ordinary_document_with_a_declared_group(tmp_path, run):
    """Everything after classification is the ordinary pipeline — one pipeline,
    two sources. Parsed for real here, not faked: the point is that a note goes
    through the same steps a document does."""
    mount, _ = _memory_folder(tmp_path)
    summary, qdrant, _ = run(mount, _plan(_folder(**_MEMORY)))

    assert summary.agent_folders.documents_indexed == 1
    points = qdrant.upsert_points.call_args.args[0]
    assert points[0].payload["classification_group"] == _GROUP
    assert "nightly run" in " ".join(p.payload["text"] for p in points)


def test_a_memory_note_is_not_orphaned_by_a_full_run(tmp_path, run):
    """THE one that would have bitten: a note lives in the files table, under a
    root the org's walk never visits, so a removal step that had not seen it
    would read it as gone from disk and delete its passages on the first
    nightly run."""
    mount, _ = _memory_folder(tmp_path)
    state = StateDB(tmp_path / "state.db")
    try:
        run(mount, _plan(_folder(**_MEMORY)), state=state)
        summary, qdrant, _ = run(mount, _plan(_folder(**_MEMORY)), state=state, full=True)
        known = state.all_paths()
    finally:
        state.close()

    assert len(known) == 1
    assert summary.files_deleted == 0
    qdrant.delete_points_by_source_path.assert_not_called()
    # And it was not merely saved by the blast-radius guard: a note the run had
    # not seen would be an orphan, the guard would refuse the purge because it
    # is the whole index, and the run would say so. Silence here is the proof
    # that the note was SEEN.
    assert summary.purge_refused_reason is None
    assert summary.passages_removed_gone == 0
