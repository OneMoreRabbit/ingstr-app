# Changelog

All notable changes to Ingstr. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) with pre-release suffixes
(`-alpha`, `-beta`, `-rc<N>`) for non-stable tags.

GHCR image: `ghcr.io/jobcpf/ingstr-app:<version>`. Pin via `INGSTR_VERSION` in the per-org
`secrets.env`; `:latest` is intentionally NOT updated for pre-release tags.

---

## [Unreleased] — pending tag v0.1.0 (stable)

Awaiting confirmation that v0.1.0-rc4 ingest run on otter shows `files_errored: 0` (or only
genuinely-corrupt-document errors). When that lands, this section becomes the v0.1.0 release notes.

### Open

- A1: Real ingest end-to-end on otter against arc's live tree (in progress with rc4)
- A2: Integration test using testcontainers Qdrant (placeholder)

See [FURTHER_DEVELOPMENT.md §A](FURTHER_DEVELOPMENT.md) for the full v0.1 stable checklist.

---

## [v0.1.0-rc4] — 2026-04-30

### Fixed

- **Graphics-heavy PDFs now parse.** Added `libgl1` and `libglib2.0-0` to the runtime image's
  apt-installed system libs. Without these, `unstructured`'s PDF layout-analysis path fails
  with `libGL.so.1: cannot open shared object file` on any PDF containing embedded images
  or vector graphics (flyers, marketing material, design-heavy reports). Plain-text PDFs
  (Word exports, contracts) don't hit this code path, which is why earlier rc images parsed
  some PDFs successfully but failed on graphics-heavy ones. Adds ~30 MB to the image.
  Note: this is **not** the OCR / image-only-PDF case — that still needs
  `unstructured[local-inference]` and is deferred to v0.2. (`8c359ec`)

### Captured lesson

- When adding per-format support to `unstructured`, audit not just pip extras but also OS-level
  libs the format's backend can `dlopen`. Pillow, OpenCV-derived deps, and PDF layout libs are
  the usual suspects. Logged in [FURTHER_DEVELOPMENT.md §F](FURTHER_DEVELOPMENT.md).

---

## [v0.1.0-rc3b] — 2026-04-30

### Fixed

- **`spacy` and `nltk` are now direct deps in `pyproject.toml`** (`spacy>=3.7,<4`,
  `nltk>=3.8,<4`). Earlier rc3 attempts hit `ModuleNotFoundError: No module named 'nltk'`
  inside the NLP-data download script because neither package is a transitive dep of
  `unstructured`'s per-format extras (`[pdf,docx,pptx,xlsx,md,html]`). They have to be
  declared explicitly. Putting them in pyproject rather than as `pip install` lines in the
  Dockerfile keeps "what the image needs" in one place. (`02e0e21`)

### Note

- rc3a (and an unsuffixed rc3 attempt) were broken builds — kept as tags for traceability
  but not consumable images.

---

## [v0.1.0-rc3] — 2026-04-30

### Changed

- **NLTK data download moved to a separate diagnostic script**
  ([`scripts/download_nlp_data.py`](scripts/download_nlp_data.py)). The previous inline
  list-comprehension form swallowed exceptions, so build failures showed only `exit 1`
  with no detail on which package or why. The script handles each package independently
  in `try/except`, prints per-package status (`OK` / `NOT FOUND` / `ERROR <type>: <msg>`),
  prints the running NLTK version, and exits 1 only if **zero** packages were
  downloaded. Tolerates per-package availability variance across NLTK versions. (`e29fa0b`)
- **Split chained RUN steps in the Dockerfile** so any future build failure points at the
  exact command rather than a chained one. (`c510dc8`)

### Note

- The first rc3 image build still failed; the diagnostic output was needed to find the
  underlying `ModuleNotFoundError` that became rc3b's fix.

---

## [v0.1.0-rc2] — 2026-04-30

### Added

- **`unstructured` per-format extras** explicit in `pyproject.toml`:
  `unstructured[pdf,docx,pptx,xlsx,md,html]>=0.14,<1.0`. Without these, `partition_<fmt>()`
  backends fail at runtime with "dependencies not installed". The earlier `unstructured>=0.14`
  bare install silently shipped an image that could only parse `.txt` / `.md` / `.html` —
  every PDF, DOCX, PPTX, and XLSX failed at the parse step. (`702cd3f`)
- **Pre-downloaded NLP models in the Dockerfile builder stage** (as root, before the
  privilege-drop happens at runtime entrypoint):
  - spaCy `en_core_web_sm` via `python -m spacy download`
  - NLTK `punkt`, `punkt_tab`, `averaged_perceptron_tagger` via `nltk.downloader` into
    `/opt/venv/share/nltk_data`
  
  Without this, `unstructured` lazy-downloads them on first parse, which fails at runtime
  with `[Errno 13] Permission denied` because the unprivileged `ingstr` user can't write
  into the root-owned `/opt/venv`. `NLTK_DATA` is set in both stages so the runtime resolves
  data through the venv copy. (`702cd3f`)
- **`pptx` added to the parsers map** in `config.example.yml` and `REFERENCE.md`. (`702cd3f`)
- **§2.9.5 "NLP models in the image"** added to `REFERENCE.md`. (`702cd3f`)

### Note

- rc2's first attempt had build issues (initial `pip install spacy` failure, then
  `nltk` not installed). Resolved across rc3 / rc3b. rc2 the tag itself was a broken build;
  superseded by rc3b.

---

## [v0.1.0-rc1] — 2026-04-30

### Verified

- **First all-OK real-services health check** on otter against arc's per-org Qdrant container
  (with the per-org Docker network architecture: `qdrant_arc_net`, no published ports) and the
  shared registry NFS mount. All five `health` checks returned `[OK]`:
  - `source.root` — NFS-in-container mount worked (Docker `local + nfs` volume driver)
  - `plan` — 8 groups loaded from `/mnt/registry/{compiled_plan,group_gid_map}.yml`,
    cross-validation passed
  - `ollama` — endpoint at `http://172.16.32.32:11434` reachable, `nomic-embed-text` loaded
  - `qdrant.connect` — Docker DNS resolved `qdrant_arc:6333` over the per-org network
  - `qdrant.collection` — collection exists with vector size 768

This proved the full deployment chain end-to-end before any real ingest. The rc2+ work was
plumbing fixes uncovered when ingest started exercising `unstructured`'s actual code paths.

---

## [v0.1.0-beta2] — 2026-04-30

### Fixed

- **`UpstreamUnavailable` mid-run was being swallowed.** The per-file `except IngstrError`
  handler in `pipeline.run_ingest` was catching `UpstreamUnavailable` (which extends
  `IngstrError`) and treating Qdrant/Ollama outages as per-file errors instead of aborting
  the run with exit 2. Brief is explicit that systemic failures are loud. Fixed by adding
  an explicit `except UpstreamUnavailable: raise` clause ahead of the `IngstrError`
  handler. (`86c53d0`)
- **Renamed `assert_collection_exists` → `verify_collection`.** `unittest.mock`'s
  auto-spec safety net rejects attribute names starting with `assert_*` (intended to catch
  typos like `mock.asssert_called_once`). Test mocks of `QdrantWriter` were failing to
  configure return values. Public-API methods shouldn't start with `assert_*`; rename is
  the Pythonic fix. (`86c53d0`)
- **Dropped `mix_stderr=False` from test `CliRunner` instantiations.** Click 8.2 removed
  the kwarg; stderr is always separate by default in newer Click. (`bbe0ce9`)

---

## [v0.1.0-beta] — 2026-04-30

### Added

- **All four CLI commands wired** with full per-file pipeline orchestration per brief §8:
  - `ingest` — incremental + full + dry-run modes; classify → hash → (skip if unchanged) →
    parse → chunk → embed → build points → delete old → upsert → state.
  - `stats` — files known, files errored, chunks stored (Qdrant `count_points`), and
    last-run summaries.
  - `health` — `[OK]`/`[FAIL]` per upstream, exits 0 / 2.
  - `version` — already wired in alpha.
- **Idempotency contracts:**
  - Deterministic point IDs via `uuid5(NAMESPACE, f"{source_path}:{chunk_index}")`.
  - Hash-based skip — re-running with no content change is a no-op.
  - Full mode: GID-changed-without-content-changed payload refresh via `set_payload`
    (no re-embed); orphan cleanup for files no longer on disk.
- **Per-org isolation topology** finalised in deploy templates:
  - Data NFS-mounted **inside the container** via Docker `local + nfs` driver — host
    never sees org data.
  - Registry host-mounted on otter at `/mnt/registry/`, bind-mounted into container.
  - Config + secrets per-org under `/etc/ingstr/<org>/`, Ansible-templated.
  - Per-org named state volume.
- **Per-org Qdrant Docker network** (`qdrant_<org>_net`) for service discovery without LAN
  exposure. Compose declares the network as `external: true`; Qdrant compose stack creates
  it. Service-name resolution (`qdrant_<org>:6333`) via Docker DNS.
- **Dynamic supplementary-GID entrypoint** reads `group_gid_map.yml` at container startup
  and drops privileges to `ingstr:ingstr` with the right supplementary groups via `setpriv`.
  Image versioning is decoupled from group-membership changes.
- **Deploy artefacts**: `deploy/compose.example.yml`, `deploy/systemd/ingstr-ingest.{service,path}`,
  `deploy/README.md` operational walkthrough.
- **REFERENCE.md** (operational + architectural reference, two parts).
- **FURTHER_DEVELOPMENT.md** (post-v0.1 backlog).

---

## [v0.1.0-alpha] — 2026-04-29

### Added

- **First GHCR image build and publish** via `.github/workflows/release.yml` (tag → GHA →
  GHCR). Public package on `ghcr.io/jobcpf/ingstr-app`.
- **Image structure**: multi-stage `Dockerfile` (builder pip-installs into `/opt/venv`;
  runtime is `python:3.12-slim` + `libmagic1` + `poppler-utils` + `util-linux` + `tini` +
  `ca-certificates`). Non-root `ingstr` user.
- **`docker-entrypoint.sh`** with the dynamic-GID setpriv pattern.
- **`docker run --rm ingstr version` / `--help`** verified end-to-end on otter.
- **Most subcommands stubbed** (`raise NotImplementedError`); intent was deploy-chain
  proof-of-concept, not feature completeness.
- **Initial scaffolding:** Apache-2.0 license, pyproject.toml (Hatchling, Typer, pydantic,
  qdrant-client, unstructured, structlog, pathspec; pytest/ruff/mypy/testcontainers as
  dev extras), src/ingstr/ module layout, ~99 unit tests across config / plan / classify /
  hashing / state / embed / qdrant_io / parse / chunk / pipeline / cli, integration test
  placeholder, CI workflow.

---

## How this changelog is maintained

When you cut a new tag:

1. Move the relevant entries from `[Unreleased]` into a new `[vX.Y.Z]` section with the
   release date.
2. Reset `[Unreleased]` to a stub (`### Added`, `### Fixed` etc., empty).
3. The git log is the authoritative detail; this file is for human consumption — keep
   entries focused on **what changed for users / operators** rather than every commit.

For a fuller account of decisions (per-org isolation, Docker network, sentinel ownership)
see [REFERENCE.md](REFERENCE.md). For the live backlog see
[FURTHER_DEVELOPMENT.md](FURTHER_DEVELOPMENT.md). For lessons captured during build that
inform future work see [§F there](FURTHER_DEVELOPMENT.md).
