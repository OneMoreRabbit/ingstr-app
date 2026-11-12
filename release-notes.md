---
title: Release notes — ingstr-app
status: active
updated: 2026-11-12
owner: agent-eco.component.ingstr
about: one short entry per release, newest first — what changed and what anyone must do about it
---

# Release notes — ingstr-app

Newest first. One entry per release tag, written in the same commit that cuts the
tag. Plain English: a reader skimming this page should know what changed and
whether they have to act.

## v0.1.1 — 2026-11-12

**TL;DR:** a full run can no longer quietly delete an index because a mount was
missing. If you run `ingest --full` anywhere, this is the version to run.

- **A full run refuses an implausibly large deletion.** Orphan removal is driven
  by a file *not being seen*, which is exactly what an unmounted or unreadable
  tree looks like. v0.1.0 deleted every unseen path with no gate and exited 0.
  This version refuses when more than half of what it knows has vanished,
  reports the refusal and exits non-zero. The guard applies to every
  deployment, including those with no `agent_source` section (ADR-0010 §6).
- Agent surfaces are read from the agent host's export, by stem rather than
  absolute path, and an unreadable subtree is reported as a skip rather than
  returned as empty (ADR-0010 §7).
- **The upstream group map is now checked in both directions.** A map *missing*
  a group that `compiled_plan.yml` requires now exits 1 naming it. v0.1.0
  accepted it and then failed every file owned by that group individually. See
  Action below.
- Optional `agent_source` config section; `root` has no default, deliberately.
- No change to the Qdrant payload, the point ID scheme or the classification
  rule. Reads the same upstream surface as v0.1.0: `required_groups` only.

**Action:** before deploying, run `ingstr health` against the existing config.
Exit 1 means the config or the group map is rejected — most likely the stricter
map check above, which is a real upstream fault v0.1.0 was hiding. Exit 2 means
config and map are fine and an upstream is unreachable.

**Known, unchanged in this release:** if the Qdrant collection is lost while the
state database survives, every file is skipped as unchanged and the collection
stays empty — `--full` does not force a re-index. `ingstr stats` shows the
signature (files known high, chunks stored 0); the remedy is to delete the state
database and let the next run rebuild. See `ingstr-architecture-v0_3` in the
Atlas vault.

## v0.1.0 — 2026-09-13

**TL;DR:** first release. Walks an org's files, chunks and embeds them, and writes
them to that org's Qdrant collection with the owning group stamped on every
passage.

- Classification is fail-closed: a file whose group number is not in the estate's
  `group_gid_map.yml` is skipped and reported, never indexed under a default group.
- Incremental runs skip files whose content hash is unchanged. `--full` walks
  everything, removes points for files that are gone, and refreshes the group
  stamp where a file's group changed without re-embedding it.
- Fails loudly on upstream trouble: a missing collection, a wrong vector size or an
  unreachable embedding endpoint ends the run with exit 2 rather than a partial write.
- One org per container; per-org Qdrant, config directory and state volume.

**Action:** none for existing deployments. See `deploy/README.md` to stand one up.

*This entry was written on 2026-11-12, not in the tagging commit — the repo had no
release-notes file when v0.1.0 was cut. Entries from the next tag onward are
written with the tag, as the method requires.*
