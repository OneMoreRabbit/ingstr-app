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

## v0.2.2 — 2026-11-13

**TL;DR:** folders shared by an added ACL rather than by classification are
now indexed, classified by the grant on the folder. And the other half, which
matters more: **taking access away takes it away in search** — a file that can
no longer be classified has its passages removed, in every run.

### Classifying by a grant

- A file whose own group is an RBAC group is classified as today. **The
  file's own group always wins**: a grant on a folder above it never widens
  the audience for a file that was classified on purpose.
- Otherwise, if the file sits under a path in the plan's `acl_grants`, it
  takes that grant's group. Where grants nest, **the deepest wins** — the only
  rule that makes a narrower share mean anything.
- A folder carrying **two different grants** has its files refused and
  reported. A passage has one group, so choosing would hand one of two
  audiences access the plan does not state.
- A file matching neither is refused, as before. **Nothing becomes readable
  that the plan does not grant.**
- **New required setting when the plan grants:** `source.plan_path`, per org —
  where that org's `source.root` sits beneath the plan's `acl` root, written
  as the plan writes paths (for example `arc/drive/`). The plan writes its
  grant paths relative to a root on beaver and this container sees its own
  mount point; nothing else relates the two, and nobody but the estate knows
  the answer. A run whose plan has grants and whose org has not declared it
  **stops**, rather than quietly matching no file. `ingstr health` reports it.

### Taking access away

- **A file that can no longer be classified loses its passages.** Its group
  left the plan, a grant was withdrawn, or it is now another org's — and
  before this, its passages stayed in Qdrant under the old group for ever.
  Nothing removed them: the file is still on disk so it is not an orphan, and
  it never reaches the indexing path so nothing overwrites it. Withdrawing a
  grant left every passage in that folder findable by the revoked group.
- **In every run, not only `--full`.** A grant is withdrawn in the plan, not
  on disk; waiting for the nightly run is waiting with the access still
  granted.
- **Under the same guard as the orphan purge, counted with it.** A plan read
  while half-written could re-classify everything at once — the same accident
  as a broken mount in different clothes. A refusal removes **nothing**, not
  some, and ends the run non-zero.
- **An intended large removal gets through one declared way:**
  `ingstr ingest --org <org> --expect-removals <n>`. Get `<n>` from
  `--dry-run`, which reports what it would remove and removes nothing. A
  number cannot be typed from habit the way a `--force` flag can. **Never put
  it in a timer unit.**
- Removal counts are reported as two numbers — files gone from disk, and
  files that can no longer be classified — and **never as paths**: a path is
  org data and the journal is on the host. Both numbers are kept with a
  refusal and shown by `ingstr stats`, because the two have different causes
  and different remedies.
- **`ingstr health` checks `source.plan_path` against the plan**, reporting
  how many granted folders lie under it. A wrong value is otherwise silent:
  it matches nothing, and every granted file reads as unclassifiable exactly
  as it did before.

### Also

- `pyproject.toml`'s comment about dependencies said what was excluded and
  implied the image was therefore slim. It is **about 3.75 GB compressed**,
  and has been since v0.1.0: the `[pdf]` extra brings `unstructured-inference`
  and with it torch, the CUDA runtime, transformers and onnxruntime. The
  comment now says so.

**One limit, stated rather than built around:** a grant classifies by the
plan's path, not by the access entries on the file. A file moved into a
granted folder keeps its old entries until the RBAC play next runs, so for
that window search can show a passage its group cannot yet open. The window
is the play's cadence, and closing it belongs where the ACLs are applied.

**Action:** if the plan has `acl_grants`, add `source.plan_path` to each org
before deploying — a run without it stops at startup and says what to add.
`ingstr health` tells you whether the value you chose matches any grant.

## v0.2.1 — 2026-11-13

**TL;DR:** an unchanged run was taking 6 minutes 24 seconds on otter, five of
them re-parsing 520 files that had failed and would fail again. A failure is
now recorded once. Also: one file vanishing mid-run no longer stops the run,
and a refused removal can be asked about after the fact.

- **A file that failed TO PARSE is not tried again** while its size, its
  modified time, its classification group and the ingstr version are all
  unchanged. `--full` tries every failure again, so the nightly run stays the
  thorough one.
- **Only parse failures are recorded that way.** A file refused for its group
  — not in the plan, or another org's — is classified again on every run,
  because that refusal belongs to the plan and not to the file: a `chgrp`
  changes neither size nor modified time, and a new plan or a new grant
  changes nothing in the file at all. So giving a folder a group, or adding a
  grant, is still picked up by the next ordinary run. A file that could not be
  read for a moment is tried again too.
- **The version is part of that** on purpose. The common failures are facts
  about the image, not about the file — "tesseract is not installed" is one —
  so a record that never expired would mean those files are never retried
  after the image gains what they needed. An upgrade retries them with nobody
  remembering to.
- **A skipped failure still counts as errored**, with its recorded reason, and
  the exit code stays 3. A run is not healthier for having skipped it.
- **A file that vanishes or becomes unreadable mid-run is that file's
  result**, not the run's. Before this, one file deleted between being listed
  and being hashed raised `FileNotFoundError` out of the loop and ended the
  run at exit 4, discarding every other file's work.
- **A refused removal is kept** in the run record and shown by `ingstr stats`
  **while it is outstanding** — that is, until a later run reaches the removal
  step and removes normally. It is a state, not a moment: until it is cleared,
  passages of files whose access was taken away can still be found, and that
  question had no answer once the journal had rolled. It stops being printed
  once it is over, because a warning that never clears is one nobody reads.
- **A dry run writes nothing to the state database.** `--dry-run` says "plan
  but do not write", and until now that was true of Qdrant and false of the
  state: a dry run recorded per-file failures. It no longer records anything
  about a file — not a parse failure, not an unclassifiable one, not an
  unreadable one — while still reporting everything it found. (A dry run does
  still record the run itself, so `ingstr stats` shows it happened.) The
  simplest rule was the right one, and it is the one the help text already
  claimed.
- **A parse failure no longer leaves passages under the wrong group.** A file
  indexed under one group, whose content then changes to something that will
  not parse and whose group has changed, has the group on its existing
  passages refreshed before the failure is recorded. Nothing else in a run
  would have fixed it: the hash no longer matches, so the refresh branch is
  not reached, and the parse that would re-index it cannot succeed. The index
  follows access in every run, whether or not the file can be read.
- **Two orgs may not write to one collection.** Same Qdrant with different
  collections is fine; different Qdrant with the same collection name is
  fine. Sharing one would not collide, since a passage id carries its org —
  the points would simply sit together, and the first sign would be one org's
  passage returned to the other's reader.

**What a timer run no longer notices**, until the nightly `--full`:

- a file that failed to parse, was then changed with its size and modified
  time preserved, and would now parse;
- a changed setting that would let a failed file parse — when the page limit
  or the language list arrive, raising either does not retry failures until
  the nightly run or the next version.

**Rolling back is safe.** This release keeps its new state in new tables, not
in new columns on the existing ones. An older image reads `SELECT * FROM
files` and builds its record from the row, so a column added here would make
v0.1.1 raise `TypeError` on the first file it looked at — measured, not
assumed. A table it does not know about is simply never read.

**Action:** none. A database written by an earlier image is read unchanged,
and a file that failed under one has no recorded version, so it is tried once
more and then recorded properly.

## v0.2.0 — 2026-11-13

**TL;DR:** one configuration lists every org and a run reads one of them; a
passage id no longer depends on where the data is mounted; a run holds only
its own org's group ids. **Every passage id changes**, so a collection is
rebuilt rather than upgraded — this release is deployed together with the new
embedding model, and one full index pays for both.

- **One configuration, every org.** `config.yml` grows an `orgs:` list, each
  with its own source root, Qdrant and state database; plan, embedding,
  chunking, parsers and logging are shared. Every run names its org:
  `ingstr ingest --org arc`. With exactly one org configured `--org` may be
  omitted. **This is a breaking configuration change**: unknown keys are
  refused, so a v0.1.x configuration fails at startup.
- **A passage id is now `uuid5("<org>:<path below the org's root>:<passage
  number>")`** where it was the container-absolute path. The old form moved
  whenever a mount moved, and collided between orgs sharing a relative path.
  See `qdrant-collection-contract` 0.2 in the Atlas vault.
- **A run holds only its own org's group ids**, read from `groups[]` in
  `compiled-rbac-plan` 0.11. Until now the entrypoint took every group id in
  the estate-wide map. **v0.2.0 requires that contract** (rbac-compiler
  v0.7.3): an older plan exits 1 naming what to do, rather than falling back.
- **An unreadable plan stops the run** instead of starting with no groups and
  reporting every file as unclassifiable. `ingstr version` and `--help` still
  need no configuration.
- **A file whose group belongs to another org** is refused, recorded against
  the file, and counted apart from parse failures.
- **PDFs parse as the dropped user.** The image sets `HOME`,
  `XDG_CACHE_HOME`, `NUMBA_CACHE_DIR` and `MPLCONFIGDIR` to directories that
  user owns, and **the build now fails if a PDF cannot be parsed as that
  user** — the check that would have caught the v0.1.1 failure. No deployment
  needs `NUMBA_CACHE_DIR`.
- **Library versions are pinned** in `constraints.txt`, used by the image
  build. The same source built on another day is now the same image; it was
  not before, which is how v0.1.1 got a numba that needed a cache directory
  and v0.1.0 did not.

**Action:** deploy the new configuration and the new image together, then
index into a **new collection**. The old collection's ids cannot be reused.
Run `ingstr health --org <org>` first: exit 1 names a configuration value, a
group missing from the map, or a plan older than contract 0.11.

**Known limits, unchanged:** exclude patterns match case-sensitively, as
gitignore does — `**/*.heic` does not exclude `IMG_1.HEIC`; write both cases.
`.doc` (not `.docx`) needs LibreOffice, which this image does not carry, so
those files are reported as per-file errors. A file that can never be parsed
fails every run rather than being recorded once and skipped.

## v0.1.1 — 2026-11-12

> **Added 2026-11-13, after the tag: PDFs need `NUMBA_CACHE_DIR` set by the
> deployment.** Every PDF fails on this version with "cannot cache function
> 'projection_by_bboxes': no locator available". The image drops privileges
> with `setpriv`, which changes the uid and the groups and not the
> environment, so `HOME` stays `/root` and numba cannot write its cache. Set
> `NUMBA_CACHE_DIR=/tmp/numba-cache` in the compose file, and re-run an
> ordinary `ingest` — a file that failed has an empty stored hash, so it is
> tried again. Word, Excel and PowerPoint files are unaffected. Fixed in the
> image from v0.2.0; no deployment line is needed there. Measured on otter in
> the ARC restore: 339 PDFs, all failed.


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
