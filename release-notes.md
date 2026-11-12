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
