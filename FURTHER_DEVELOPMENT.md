# Further Development — Ingstr

> Living backlog of work for Ingstr beyond the v0.1.0-rc1 cut. Compiled
> from the brief's §14 (out of scope), surviving placeholders in the
> codebase, decisions made during build that left rough edges, and
> open platform questions that may produce Ingstr-side work.
>
> Status as of 2026-04-30 — first all-OK real-services health check
> on otter just passed; real ingest still pending.

This is **not** a roadmap. There's no commitment to deliver any of these.
It's a captured list so they don't get lost between sessions or platform
handovers.

---

## A. Blocking v0.1.0 stable (must-do before un-pre-releasing)

Items required to drop the `-rc1` suffix and tag a true stable.

### A1. Real ingest end-to-end on otter

**What:** `docker compose ... run --rm ingstr ingest` against the per-org
Qdrant container, landing real chunks with the right payloads, then
`stats` showing matching counts.

**Why:** `health` proves connectivity; `ingest` proves the entire pipeline
including parse, chunk, embed, point construction, upsert, and state.

**State:** Pending. Last we left off, `health` returned all `[OK]`. Next
steps: `ingest --dry-run`, then `ingest`, then `stats`. Sequence in
[REFERENCE.md §1.4](REFERENCE.md#14-setting-up-a-new-org).

**Ref:** Brief §15 (Definition of done).

### A2. Integration test with testcontainers Qdrant

**What:** Replace the placeholder at
[`tests/integration/test_qdrant_roundtrip.py`](tests/integration/test_qdrant_roundtrip.py)
with a real test that:
- spins up Qdrant via `testcontainers[qdrant]` (already a dev dep),
- bootstraps a collection at the right vector size,
- runs `run_ingest()` against a small `tests/fixtures/` tree (also empty —
  needs a few sample documents),
- asserts the resulting points have the §2.6 payload shape, deterministic
  IDs, and that re-running is idempotent.

**Why:** Closes the gap between unit tests (mocked everything) and the
manual real-services smoke test. Lets CI catch regressions in the
upsert/delete contract automatically.

**State:** Stub raises `pytest.skip("not yet implemented; awaiting embed.py + qdrant_io.py")`
and `tests/fixtures/` contains only a `.gitkeep`.

**Ref:** Brief §13. Adds the fixture corpus needed for §15 DoD's
"Incremental and full modes both pass integration tests".

### A3. `--full` mode end-to-end coverage

**What:** Verify on real otter that:
- a file's hash unchanged → skip (no re-embed)
- a file's hash unchanged but GID changed → `set_payload` refresh path fires (no re-embed)
- a file removed from disk → orphan deletion in full mode

**Why:** The branch logic exists with unit tests but full mode hasn't
been exercised against a real Qdrant. The `set_payload`-without-reembed
path in particular is a brief contract that's unit-tested but not yet
seen in production.

**State:** Pending. Run after A1 succeeds.

### A4. PyPI publishing (per brief §3)

**What:** Brief says "Pipx-installable from PyPI". Currently only on GHCR.

**Why:** The brief lists this in the v0.1 DoD (§15). Whether it still
matters depends on whether the deploy model has settled on container-only
(GHCR) or expects to support pipx installs too.

**State:** Not done. Likely waivable — the actual deploy on otter is
container-based and there's no current consumer asking for pipx.

**Decision needed:** Is PyPI publishing actually wanted, or is GHCR-only
fine? If waivable, downgrade this from "blocking stable" to "open question"
and update the DoD.

---

## B. Brief §14 — explicitly deferred to post-v0.1

Verbatim from the original brief's "out of scope for v0.1" list. Each is a
real future feature that was deliberately punted to keep the MVP small.

### B1. Migrate state from SQLite into Qdrant payload (single source of truth)

**What:** Today `last_indexed_at`, `content_hash`, `mtime`, `last_error` etc.
live in a per-org SQLite DB at `/var/lib/ingstr/state.db`. Brief flags that
this could move into Qdrant payload (using the existing `content_hash`,
`indexed_at` fields, plus new ones for error tracking).

**Why now:** Two-source-of-truth is acceptable for v0.1 to keep ingest
fast and the implementation simple — a Qdrant query per file at the start
of every ingest is a perf concern. Reconsider when:
- The state volume becomes a backup / DR pain
- Multi-machine ingest becomes desirable (state lives where Qdrant lives)
- Or the state schema starts drifting from Qdrant payload semantics

**Cost:** Medium refactor. The `state.py` DAO is well-isolated, but the
hash-skip logic in `pipeline.py` reads it on every file. Performance
analysis needed before the move.

### B2. Webhook / filesystem-watch triggering

**What:** Replace the systemd `.path` sentinel approach with either:
- An HTTP endpoint that triggers `ingest` (webhook from upstream sync)
- A long-running watcher (inotify or fanotify) that triggers on actual
  file changes rather than a sentinel

**Why now:** The sentinel pattern (and §A1's host-side coordination
requirement) is fragile — depends on upstream cooperation. A webhook
endpoint would let the upstream sync fire-and-forget directly.

**Cost:** Webhook is small (a tiny HTTP server next to Ingstr, or a
sidecar). Filesystem watch is more invasive — Ingstr's "one-shot, runs
to completion" model would have to relax, which is a hard architectural
change. Probably webhook only.

### B3. Multi-org in one process

**What:** Today, multi-org is achieved via multiple container instances
with separate configs. A future version could run multiple orgs from a
single process with separate Qdrant connections + state DBs.

**Why now:** Dropped per brief §2 because per-process isolation is the
cleanest security boundary. Reconsider only if the operational cost of
running N containers per host becomes significant (it shouldn't — Ingstr
is one-shot, not long-running).

**Likely never:** This actively conflicts with the brief's design
principles. List for completeness but probably stay-out-of-scope.

### B4. Reranking, hybrid search, query-side concerns

**Where it goes:** Not in Ingstr — that's the **proxy's** problem. See
[`../proxy-brief-input.md`](../proxy-brief-input.md). Listing here only
to make it clear that this is *not* an Ingstr backlog item.

### B5. Quantisation

**What:** Qdrant supports scalar/binary/product quantisation to reduce
RAM footprint at the cost of recall.

**Why now:** Brief §14 explicitly defers. Reconsider only if the per-org
Qdrant containers' RAM footprint becomes a problem at scale.

**Cost:** Low — it's a collection-creation flag. The catch is that
quantisation interacts with the GID-based RBAC model in subtle ways
(quantised vectors → some recall loss → some legitimate matches missed
for users who should have seen them). Test carefully.

### B6. Cross-collection deduplication

**What:** A document that exists in multiple orgs' trees with the same
content currently produces independent embeddings + storage in each
org's collection.

**Why now:** Each org gets its own Qdrant collection by design. Cross-
collection dedup would require a shared "canonical embeddings" store
that Ingstr-instances-per-org consult. Big architectural change.

**Likely never (within Ingstr):** A separate dedup service makes more
sense than coupling Ingstr instances together.

### B7. Custom parsers per file type

**What:** `config.yml:parsers` is a map of extension → parser, but only
`unstructured` is currently supported. The map is a placeholder for
future parsers (PyPDF2, custom DOCX handlers, etc.).

**Why now:** `unstructured` handles all v0.1 file types adequately. Add
custom parsers only when a specific file type causes parse-quality issues
that justify the per-type investment.

**Cost:** Low if added one at a time. The plug-in shape is already there
in `parse.py` — just dispatch on `cfg.parsers[suffix]` rather than
hard-coding `unstructured.partition.auto.partition`.

### B8. Distributed running

**What:** Multi-host parallel ingest of the same org's tree.

**Why now:** Single-process is enough at expected scale. Distributed
ingest would require state coordination (which file is being processed
by which worker) — solvable but not urgent.

**Likely never (in Ingstr):** A wrapper that splits the tree across N
single-process Ingstr invocations would do this with no code change to
Ingstr itself.

### B9. Encryption at rest beyond Qdrant native

**What:** Application-level encryption of payload `text` field before
storing in Qdrant.

**Why now:** Qdrant's storage encryption (when configured) is sufficient
for v0.1. Reconsider if compliance requires "Ingstr can write to a Qdrant
the operator doesn't fully trust" — that's a different threat model.

---

## C. Operational maturity (not in brief, surfaced post-deploy)

Things that became obvious as the deploy chain matured. Not blocking
v0.1 stable, but worth doing in v0.2 / early operations.

### C1. OCR support for image-only PDFs

**What:** `unstructured` is installed without the `[local-inference]`
extras to keep the image lean (those extras pull in multi-GB of ML
deps). Image-only PDFs (scanned documents) currently fall through with
0 chunks → recorded as per-file error.

**Options:**
- Build a separate `ingstr:ocr` image variant with `[local-inference]`
  for orgs that need OCR
- Sidecar OCR service that converts PDF → text-PDF before Ingstr sees it
- Skip-and-flag at ingest time so operators know which files failed

**Cost:** The image variant is simplest but the resulting image is ~4×
larger. The sidecar is more flexible. Pick when an org actually needs it.

### C2. Prometheus / OpenTelemetry metrics

**What:** Brief §11 only specifies structured JSON logs. No metrics
endpoint exists. For production observability (per-org chunk counts over
time, ingest duration distributions, error rates) metrics would be
useful.

**Cost:** Small. `structlog` already produces structured events; adding
a Prometheus client wrapper or OpenTelemetry exporter is mostly plumbing.

**Decision needed:** Platform team has it as an open question
(PLATFORM_HANDOFF §11 — "Does Ingstr need to expose any metrics?").

### C3. Better state DB inspection tooling

**What:** Operators inspecting `state.db` currently need to mount the
named volume and run `sqlite3` against it. There's no `ingstr` subcommand
that exposes this directly.

**Options:**
- Extend `ingstr stats` with a `--by-error` or `--errored` flag to list
  failing files
- A new subcommand `ingstr inspect` for state DB queries
- Just document the named-volume access pattern (cheap, decent UX)

**Cost:** Trivial. The `state.py` DAO has all the queries already.

### C4. Retry / backoff for transient Ollama / Qdrant issues

**What:** Brief is explicit: "Failures are loud." There's no retry on
HTTP errors. A single transient blip (a brief Ollama restart, a Qdrant
network glitch) currently aborts the run with exit 2.

**Options:**
- Add bounded retry with exponential backoff at the `EmbeddingClient` /
  `QdrantWriter` layer (3 attempts with 1s/2s/4s)
- Keep "failures are loud" as the contract and rely on systemd to
  re-trigger on the next sentinel touch

**Decision needed:** Is the "loud failure → external retry" model
working in practice, or do operators want in-process retry for
robustness? Wait for operations data before changing.

### C5. Health-check probes RW Qdrant access

**What:** `ingstr health` currently does `get_collections()` which works
with read-only keys. A configured RW key that's actually been revoked
(or was never granted) would pass `health` but fail `ingest`.

**Options:**
- Add a no-op upsert (e.g. of a sentinel point) to `health`'s qdrant
  check, then delete it
- Or just document that operators should `ingest --dry-run` immediately
  after `health` to surface auth issues earlier

**Cost:** Trivial code, but the no-op upsert / delete dance has its own
edge cases.

### C6. Image size

**What:** Current image is ~600MB+ thanks to `unstructured`'s
transitive deps. Slim alternatives exist (multi-stage with explicit
copy of only needed packages, alpine base).

**Cost:** Medium. Alpine has `unstructured`-compatibility issues
historically; debian-slim is the safer base. The win is significant
for pull bandwidth on remote sites.

### C7. `dataclass` for `RunSummary` → public API question

**What:** [pipeline.py](src/ingstr/pipeline.py) returns a `RunSummary`
dataclass. If anyone ever wants to embed Ingstr as a library
(`from ingstr.pipeline import run_ingest`), this becomes a public API.

**Cost:** Zero now. Just be aware that changing `RunSummary`'s field
names is a breaking change for any future library users.

### C8. Reduce `unstructured` dependency surface

**What:** `unstructured` is a heavy dependency. For v0.1 it does parse
+ chunk in two calls. We could replace it with thinner alternatives
(PyPDF2 + python-docx + openpyxl + a simpler chunker).

**Cost:** High — re-implements a lot of `unstructured`'s file-type
handling. Probably not worth it unless image size or install time
becomes a real pain.

---

## D. Code quality / dev ergonomics (small, mostly)

### D1. Test fixtures directory is empty

**What:** [`tests/fixtures/`](tests/fixtures/) only has `.gitkeep`.
The integration test in §A2 needs real fixtures (a few small PDFs,
DOCXs, XLSXs, TXT, MD, HTML).

**Cost:** Trivial — sample a small public-domain corpus.

### D2. mypy strict but not enforcing

**What:** [pyproject.toml](pyproject.toml) configures
`[tool.mypy] strict = true`, but the CI workflow runs mypy non-blocking.

**Options:**
- Make CI actually fail on type errors
- Or relax to non-strict if some genuine `Any` patterns can't be
  reasonably typed (e.g. `unstructured` returns are `list[Any]`)

**Cost:** Trivial code change once a baseline pass is clean.

### D3. Test stage in Dockerfile

**What:** Discussed earlier and skipped in favour of venv-on-Zaphod.
Worth revisiting only if a container-specific bug surfaces that the
venv tests miss.

**Cost:** Small. ~15 lines of `FROM ... AS test` stage with `[dev]`
extras + `tests/` copy.

### D4. Stub helper functions outside happy paths

**What:** A few helper functions in `pipeline.py` (`_record_per_file_error`,
`_build_point`) have edge cases that aren't directly unit-tested
(only via the run_ingest orchestration tests).

**Cost:** Low, but adds noise to the test suite. Probably not worth it
unless one of these helpers grows.

### D5. Docstring coverage on private helpers

**What:** Most public methods have docstrings. Private `_helper`
functions are sparser. A pass for coverage would help future
maintainers.

**Cost:** Trivial.

---

## E. Open questions (decisions pending)

These need decisions before producing actionable Ingstr-side work.

### E1. Trigger sentinel ownership

**Status:** Resolved by platform pushback §3 — sentinel path is platform
config, not Ingstr contract. Ingstr has no remaining work here. Listed
for completeness because it was an open item earlier.

### E2. Image visibility on GHCR

**What:** The `jobcpf/ingstr-app` image is currently public.
PLATFORM_HANDOFF §10 notes this — if it goes private, otter needs PAT
auth (one-time setup, not breaking). No application code change either way.

**Decision needed:** Stay public, or go private?

### E3. PyPI publishing

**What:** See §A4. Whether brief §3 / §15 still applies to v0.1.

**Decision needed:** Yes/no on PyPI.

### E4. Hot-standby / multi-host Ingstr

**What:** PLATFORM_HANDOFF §11 — does the operational expectation
include a backup otter for failover? If yes, state DB volume becomes
a coordination problem (don't run two ingests for the same org
simultaneously).

**Decision needed:** Single-host or multi-host operational model.

### E5. Ingest cadence

**What:** PLATFORM_HANDOFF §11 — event-driven (systemd .path) is
simplest. Cron-on-interval is fine. Continuous polling is not. Has the
trigger model been validated end-to-end in production yet?

**Decision needed:** Confirm event-driven is what platform wants long-term.

### E6. NFS server topology

**What:** PLATFORM_HANDOFF §11 — does each org get its own NFS server,
or is there one server with per-org exports? Affects how `ORG_DATA_NFS_*`
gets templated by Ansible.

**Decision needed:** Topology preference. Not Ingstr's call but the
shape of the answer affects the deploy template.

---

## F. Minor lessons captured during build (for future-Claude / future-engineer)

These were resolved during build but are worth keeping as cautionary
notes.

- **Mock's `assert_*` deny list bites public APIs.** Methods named
  `assert_*` confuse `unittest.mock`'s typo guard. Fixed in commit
  `86c53d0` by renaming `assert_collection_exists` → `verify_collection`.
  When adding new public methods, avoid `assert_` prefix.
- **`UpstreamUnavailable` extends `IngstrError`.** The exception handler
  for per-file errors must exclude `UpstreamUnavailable` explicitly
  (with a `raise`-only block above it), else systemic outages get
  swallowed as per-file errors. Fixed in `86c53d0`.
- **Click 8.2 removed `mix_stderr`.** Tests using `CliRunner(mix_stderr=False)`
  break against modern Click. Fixed in `bbe0ce9`. Use `CliRunner()` —
  stderr is separate by default in 8.2+.
- **Compose bind-mount with absolute source path doesn't compose with
  per-org subdirectories.** Use `.` (relative to compose file directory)
  rather than absolute paths for the config bind. Fixed in `f97e9f9`.
- **`compiled_plan.yml` doesn't ship GIDs.** It has `required_groups:`
  but no GID column. The GID mapping comes from the separately-generated
  `group_gid_map.yml`. The brief §7 anticipated this might need adapting
  and it did. Don't assume the plan alone is enough for classification.
- **`unstructured` per-format extras are not optional.** The library's
  `partition_<fmt>()` backends are gated behind per-format pip extras
  (`[pdf]`, `[docx]`, `[pptx]`, etc.) that fail at runtime with
  "dependencies not installed" if the extra wasn't pip-installed. The
  initial pyproject had bare `unstructured>=0.14` and silently shipped
  an image that could only parse `.txt` / `.md` / `.html`. PDFs and
  Office formats all failed. Fixed by pinning
  `unstructured[pdf,docx,pptx,xlsx,md,html]`. **Rule:** the
  `config.yml:parsers` map and the `unstructured[...]` extras list in
  `pyproject.toml` must stay in lockstep — one without the other is a
  silent runtime bug.
- **`unstructured` lazy-downloads NLP models at runtime — must
  pre-install at build time.** First versions of the image hit
  `[Errno 13] Permission denied` writing `en_core_web_sm` into
  `/opt/venv/lib/python3.12/site-packages` because the runtime container
  drops privileges to the unprivileged `ingstr` user before `unstructured`
  tries to install the spaCy model on first use. Same problem will hit
  any code path that triggers NLTK data downloads. Fixed by
  `python -m spacy download en_core_web_sm` and
  `python -m nltk.downloader ... punkt punkt_tab averaged_perceptron_tagger`
  in the builder stage as root. **Rule:** any pip dep that does
  lazy-download / cache-on-first-use must have the relevant data
  pre-fetched in the Dockerfile builder, otherwise it'll fail at parse
  time the first time the privilege-dropped runtime hits that code path.
  When upgrading `unstructured`, check release notes for new model deps.
- **`unstructured`'s PDF layout-analysis path needs OS-level graphics
  libs.** Graphics-heavy PDFs (flyers, marketing material, embedded
  vector graphics) trigger code paths that load `libGL.so.1` and
  glib via PIL/pillow. Without `libgl1` + `libglib2.0-0` apt-installed
  in the runtime stage, these PDFs fail with
  `failed to parse <foo>.pdf: libGL.so.1: cannot open shared object
  file`. Plain-text PDFs (Word exports, contracts) don't hit this path
  and used to succeed silently, masking the gap. **Rule:** when adding
  per-format support to `unstructured`, audit not just pip extras but
  also the OS-level libs the format's backend can dlopen. Pillow,
  OpenCV-derived deps, and PDF layout libs are the usual suspects.
- **spacy and nltk are NOT transitive deps of `unstructured`'s
  per-format extras.** The `[pdf,docx,pptx,xlsx,md,html]` extras don't
  pull either in. They have to be declared as direct deps in
  `pyproject.toml` (`spacy>=3.7,<4`, `nltk>=3.8,<4`) — putting them as
  `pip install` lines in the Dockerfile hides what the package actually
  needs.

---

## G. References

- [`README.md`](README.md) — short user-facing entry
- [`REFERENCE.md`](REFERENCE.md) — full reference (Part 1 usage, Part 2
  architecture)
- [`../ingstr-brief-v0.1.md`](../ingstr-brief-v0.1.md) — original build
  brief; §14 is the canonical "out of scope" list
- [`../PLATFORM_HANDOFF.md`](../PLATFORM_HANDOFF.md) — platform team's
  contract for Ansible deploy
- [`../Platform pushback.md`](../Platform%20pushback.md) — platform-side
  decisions on top of the handoff
- [`../proxy-brief-input.md`](../proxy-brief-input.md) — input to the
  proxy build; reuses Ingstr's patterns

---

## H. How to use this document

When starting a new development session on Ingstr:

1. Skim §A — anything blocking v0.1 stable still pending?
2. Skim §C and §D — anything in operational practice that's bitten you
   recently?
3. Pick one §B item if doing planned post-v0.1 work, with the user's
   prioritisation.
4. Resolve any §E open questions before acting on items they gate.

Update entries as you go: tick off completed items, add new ones below
existing sections, move items between sections as their status changes.
Treat as a living doc — branch + PR rather than asking "should I update
this?"
