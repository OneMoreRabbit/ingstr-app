"""Enumerating agent surfaces under the beaver-side export mount (ADR-0010 §7).

The layout is `<root>/<org>/<name>/<surface>/`, where `<root>` is this host's
export mount (`/mnt/agent-hosts/<host>/`) and `<surface>` is one of the three
that moved to agent-local disk: `memory`, `sessions`, `scratch`. `configs/` is
not here at all — it stays on beaver, is agent-private, and carries no RBAC
group, so it is structurally absent rather than merely skipped.

What is shared across tools is the **stem** `<org>/<name>/<surface>`, not the
absolute path: rbac-compile prepends the host root, sync-compile and ingstr
prepend the beaver mount root. §7 re-scoped the cross-tool `resolve_surface_path`
guarantee to stem equality for exactly this reason, so the stem is carried here
explicitly rather than reconstructed by string-slicing a path later.

**Every failure to read is a skip, never an empty result.** §6 requires that any
errno met while traversing an agent's subtree classify that agent *unavailable*,
not *empty* — a root sentinel proves the mount, not the readability of every
subtree beneath it. An unreadable org that silently yields nothing is
indistinguishable from an org whose agents deleted everything, which is the
whole hazard this ADR exists to remove. So enumeration returns what it found
**and** what it could not read, and the caller is expected to report the latter:
a run that covered three orgs of four and said nothing reads exactly like one
that covered four.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .availability import (
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    Availability,
    ExportState,
    read_export_state,
)


@dataclass(frozen=True)
class AgentScope:
    """One readable `<org>/<name>/<surface>` directory."""

    org: str
    name: str
    surface: str
    path: Path

    @property
    def stem(self) -> str:
        """The cross-tool identity: `<org>/<name>/<surface>` (ADR-0010 §7).

        Note what is NOT here: the root. Two tools holding different roots agree
        on this string and disagree on the absolute path, by design.
        """
        return f"{self.org}/{self.name}/{self.surface}"

    @property
    def agent_stem(self) -> str:
        """`<org>/<name>` — the identity classification maps back to."""
        return f"{self.org}/{self.name}"


@dataclass(frozen=True)
class ScopeSkip:
    """Something we could not read, and why. Never silently dropped."""

    stem: str
    reason: str


@dataclass(frozen=True)
class Enumeration:
    scopes: list[AgentScope]
    skips: list[ScopeSkip]

    @property
    def skipped_orgs(self) -> list[str]:
        """Distinct top-level orgs touched by a skip, for the run note."""
        return sorted({s.stem.split("/")[0] for s in self.skips})


def iter_agent_scopes(root: Path, surfaces: list[str]) -> Enumeration:
    """Walk `<root>/<org>/<name>/<surface>`, isolating failures per subtree.

    Assumes the export root itself has already been cleared by
    `availability.read_export_state`: this function's job is the level below —
    an org or agent that is individually unreadable while the mount as a whole
    is fine.

    A missing surface directory is **not** a skip. An agent with no `scratch/`
    yet is an ordinary, legitimate state, and reporting it would bury the real
    skips in noise. An *unreadable* surface is a skip.
    """
    scopes: list[AgentScope] = []
    skips: list[ScopeSkip] = []

    try:
        orgs = sorted(e.name for e in os.scandir(root) if e.is_dir())
    except OSError as e:
        # The root was readable enough to carry a sentinel and is not readable
        # now, or is not a directory. Either way: unavailable, not empty.
        return Enumeration([], [ScopeSkip("", f"cannot list export root {root}: {_err(e)}")])

    for org in orgs:
        org_dir = root / org
        try:
            agents = sorted(e.name for e in os.scandir(org_dir) if e.is_dir())
        except OSError as e:
            skips.append(ScopeSkip(org, f"cannot list org {org}: {_err(e)}"))
            continue

        for name in agents:
            agent_dir = org_dir / name
            for surface in surfaces:
                surface_dir = agent_dir / surface
                try:
                    if not surface_dir.is_dir():
                        # Absent surface: legitimate for a new agent, not a fault.
                        continue
                    # Touch the directory now so an unreadable one fails here,
                    # attributed to this agent, rather than mid-walk where the
                    # caller would see a short file list and no reason for it.
                    os.scandir(surface_dir).close()
                except OSError as e:
                    skips.append(
                        ScopeSkip(
                            f"{org}/{name}/{surface}",
                            f"cannot read {org}/{name}/{surface}: {_err(e)}",
                        )
                    )
                    continue
                scopes.append(AgentScope(org=org, name=name, surface=surface, path=surface_dir))

    return Enumeration(scopes, skips)


def _err(e: OSError) -> str:
    """Name the errno in the note.

    Operators diagnosing this need to tell a permissions problem from a dead
    mount, and `ESTALE` in a log line is the difference between "fix the ACL"
    and "the export host went away".
    """
    name = getattr(e, "errno", None)
    return f"{e.__class__.__name__}({name}): {e}" if name else f"{e.__class__.__name__}: {e}"


@dataclass(frozen=True)
class AgentTree:
    """One agent's directory, with the verdict on its own continuity.

    The per-agent sentinel answers *"is this agent's tree the same tree?"*,
    which the export-root sentinel cannot: one recreated agent inside a healthy
    export is invisible to a root generation (ADR-0010 §6 v0.14).
    """

    org: str
    name: str
    state: ExportState
    scopes: list[AgentScope]

    @property
    def stem(self) -> str:
        return f"{self.org}/{self.name}"

    @property
    def safe_to_purge(self) -> bool:
        """Ingestible is not the same as safe to delete from.

        A new incarnation is read normally — we want its files indexed — but it
        cannot license concluding that anything was deleted, because its tree
        legitimately differs from the one we last saw.
        """
        return self.state.safe_to_purge


def iter_agent_trees(
    root: Path,
    surfaces: list[str],
    *,
    last_generation_for: Callable[[str], str | None],
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> tuple[list[AgentTree], list[ScopeSkip]]:
    """Enumerate agents, consulting each one's own sentinel.

    `last_generation_for` is passed the agent stem (`<org>/<name>`) and returns
    the generation last seen for it, or None on a first run.

    **An agent with surfaces but no readable sentinel is skipped WITH a note**
    (ADR-0010 §6 v0.14, consequence 1). It is not ingested and not purged: a
    tree that arrived by restore, rsync or a manual fix has no minted sentinel,
    and it must surface as a condition rather than quietly vanish from search.
    Silence there would be the same silent-missing failure this ADR removes,
    only inverted — nothing wrongly deleted, just nothing ever indexed.
    """
    enumeration = iter_agent_scopes(root, surfaces)
    skips = list(enumeration.skips)

    by_agent: dict[tuple[str, str], list[AgentScope]] = {}
    for scope in enumeration.scopes:
        by_agent.setdefault((scope.org, scope.name), []).append(scope)

    trees: list[AgentTree] = []
    for (org, name), scopes in by_agent.items():
        stem = f"{org}/{name}"
        state = read_export_state(
            root / org / name,
            last_generation=last_generation_for(stem),
            timeout=timeout,
        )
        if state.availability is Availability.UNAVAILABLE:
            skips.append(ScopeSkip(stem, f"{stem}: {state.reason}"))
            continue
        trees.append(AgentTree(org=org, name=name, state=state, scopes=scopes))

    return trees, skips
