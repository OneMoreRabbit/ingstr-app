# Ingstr Reference

> Document ingestion pipeline that walks a filesystem tree, parses + chunks
> documents via the `unstructured` library, embeds via an Ollama HTTP
> endpoint, and upserts into a per-org Qdrant collection with `classification_group`
> metadata stamped on every chunk.

This reference is in two parts:

- **[Part 1 — Usage Manual](#part-1--usage-manual):** how to run Ingstr, config schemas, how to set up a new org, CLI reference, troubleshooting.
- **[Part 2 — Application Reference](#part-2--application-reference):** what Ingstr is, hard boundaries, classification flow, data contracts, deployment topology.

Other authoritative documents:
- [`ingstr-brief-v0.1.md`](../ingstr-brief-v0.1.md) (one directory up) — original build brief, source of truth for application contracts.
- [PLATFORM_HANDOFF.md](../PLATFORM_HANDOFF.md) — Ansible deployment contract for the platform team.
- [Platform pushback.md](../Platform%20pushback.md) — platform-side decisions on top of the handoff.
- [README.md](README.md) — short user-facing entry; this REFERENCE supersedes it for depth.

---

# Part 1 — Usage Manual

## 1.1 What you need

To run Ingstr, on a Linux host:

| | |
|---|---|
| Container runtime | Docker with the Compose v2 plugin |
| Image | `ghcr.io/jobcpf/ingstr-app:<version>` (public image, pinned via `INGSTR_VERSION`) |
| Registry NFS | `/mnt/registry/` host-mounted, containing `compiled_plan.yml` and `group_gid_map.yml` (produced upstream by `rbac-compile` and `export_group_gids.yml`) |
| Org data NFS | An NFS export the host can reach, containing the org's data tree |
| Per-org Qdrant | A running Qdrant container on a per-org Docker network, with a pre-provisioned collection |
| Ollama | A reachable Ollama HTTP endpoint with the configured embedding model loaded |
| Three host-side files per org | `config.yml`, `compose.yml`, `secrets.env` (see §1.3) |

Ingstr does not provision any of those — it consumes them.

## 1.2 Quick start (assumes everything in §1.1 exists)

```bash
ORG=arc
cd /etc/ingstr/${ORG}                                  # or wherever per-org files live

# Smoke check — verifies all five upstreams.
docker compose --env-file secrets.env run --rm ingstr health

# Plan-but-don't-write — exercises the full pipeline, no Qdrant or state writes.
docker compose --env-file secrets.env run --rm ingstr ingest --dry-run

# Real ingest.
docker compose --env-file secrets.env run --rm ingstr ingest

# Counts and last-run timestamps.
docker compose --env-file secrets.env run --rm ingstr stats
```

## 1.3 Config files

Three host-side files live together in a per-org directory.

### 1.3.1 `config.yml`

The Ingstr application config. Read by the container at `/etc/ingstr/config.yml` (the per-org host directory is bind-mounted in via `compose.yml`'s `.:/etc/ingstr:ro`).

All paths in `config.yml` are **container-internal**. The `compose.yml` controls what backs them on the host.

```yaml
org: arc                                  # logging label only

source:
  root: /mnt/data                         # container-internal mount of org's NFS share
  follow_symlinks: false
  exclude_patterns:                       # gitignore-style globs, applied to relative paths
    - "**/.tmp/**"
    - "**/~$*"
    - "**/Thumbs.db"

plan:
  compiled_plan_path: /mnt/registry/compiled_plan.yml
  group_gid_map_path: /mnt/registry/group_gid_map.yml
  reload_on_run: true                     # always re-read at start of each run

embedding:
  endpoint: http://172.16.32.32:11434     # shared Ollama
  model: nomic-embed-text
  vector_dim: 768                         # MUST match Qdrant collection's vector size
  timeout_seconds: 30
  batch_size: 16                          # chunks per HTTP call

qdrant:
  url: http://qdrant_arc:6333             # service name on per-org Docker network
  api_key_env: QDRANT_RW_API_KEY          # name of env var holding the key
  collection: documents
  upsert_batch_size: 64
  timeout_seconds: 30

chunking:
  strategy: unstructured                  # only "unstructured" in MVP
  chunk_size_chars: 2000                  # ~500 tokens
  chunk_overlap_chars: 200

parsers:                                  # MVP: unstructured handles all types
  pdf:   unstructured
  docx:  unstructured
  pptx:  unstructured
  xlsx:  unstructured
  txt:   unstructured
  md:    unstructured
  html:  unstructured

state:
  db_path: /var/lib/ingstr/state.db       # backed by per-org named Docker volume

logging:
  level: INFO                             # DEBUG | INFO | WARNING | ERROR
  format: json                            # json | text
  log_full_query: false                   # never log file contents at INFO
```

Validation is strict — pydantic v2 with `extra="forbid"`. Unknown keys, missing required keys, or wrong types cause an immediate `ConfigError` (exit 1).

### 1.3.2 `secrets.env`

Per-org environment variables, read by `docker compose --env-file secrets.env`. Values substitute into `compose.yml` and (for `QDRANT_RW_API_KEY`) flow into the container's environment.

```env
INGSTR_VERSION=v0.1.0-rc1                 # image tag
QDRANT_RW_API_KEY=<per-org write key>
QDRANT_NETWORK=qdrant_arc_net             # external Docker network name (created by Qdrant compose)
ORG_DATA_NFS_ADDR=10.0.0.10               # NFS server IP
ORG_DATA_NFS_DEVICE=:/exports/arc/drive   # NFS export path
ORG_DATA_NFS_OPTIONS=soft,ro,nolock       # optional, has default
```

Mode `0600`. Owned by the docker-running user.

### 1.3.3 `compose.yml`

Copied verbatim from [`deploy/compose.example.yml`](deploy/compose.example.yml). No per-org edits — all variation comes from `secrets.env`. The compose file declares:

- The Ingstr service (image tag from `INGSTR_VERSION`).
- The per-org Docker network as `external: true` (created by the Qdrant compose stack).
- Volume mounts: NFS-driven `org_data` (in-container only), bind from host's `/mnt/registry`, bind from `.` (the directory containing `compose.yml`) → `/etc/ingstr`, named volume for state.
- Env passthrough of `QDRANT_RW_API_KEY` into the container.

## 1.4 Setting up a new org

End-to-end, assuming a working otter host with Docker, the registry NFS mount, and (eventually) the platform's Ansible roles:

1. **Create the per-org directory** at `/etc/ingstr/<org>/` (production via Ansible) or `/home/<user>/docker/ingstr/<org>/` (manual testing).
2. **Drop in the three files**: `config.yml` (templated), `compose.yml` (verbatim copy of the example), `secrets.env` (templated, mode 0600).
3. **Stand up the per-org Qdrant container** on a network named `qdrant_<org>_net`, with the Qdrant service joining as `qdrant_<org>`. No published ports.
4. **Bootstrap the Qdrant collection** (idempotent — safe to re-run):
   ```bash
   QURL=http://localhost:6333    # from inside the Qdrant network, or via host-side admin
   KEY=<per-org write key>

   curl -X PUT "${QURL}/collections/documents" \
     -H "api-key: ${KEY}" -H "Content-Type: application/json" \
     -d '{ "vectors": { "size": 768, "distance": "Cosine" } }'

   curl -X PUT "${QURL}/collections/documents/index" \
     -H "api-key: ${KEY}" -H "Content-Type: application/json" \
     -d '{ "field_name": "source_path", "field_schema": "keyword" }'

   curl -X PUT "${QURL}/collections/documents/index" \
     -H "api-key: ${KEY}" -H "Content-Type: application/json" \
     -d '{ "field_name": "classification_group", "field_schema": "keyword" }'
   ```
5. **Verify upstreams via `health`** — should return all `[OK]`. Failures pinpoint exactly what's wrong (see §1.6).
6. **Run `ingest --dry-run`** to exercise the pipeline without writing.
7. **Run `ingest`** for the first real ingest. Start with a small subset by temporarily pointing `source.root` at a subdirectory if you can.
8. **Wire the systemd `.path`/`.service` units** for triggered ingest (see [`deploy/systemd/`](deploy/systemd/)).

## 1.5 CLI reference

Four subcommands, no more.

### `ingstr ingest [--full] [--dry-run] [--config PATH]`

Walk the configured tree and ingest changed files.

- **Default (incremental):** files where `mtime > last_indexed_at` OR content hash changed are re-processed; the rest are skipped.
- **`--full`:** complete re-walk. Re-embeds nothing whose hash matches; deletes Qdrant points + state rows for files no longer on disk; refreshes the `classification_group` payload (no re-embed) where the filesystem GID has changed.
- **`--dry-run`:** plan but do not write. Exercises classify, parse, chunk, embed, and point construction; skips Qdrant writes and state updates.

### `ingstr stats [--config PATH]`

Print read-only counters: files known, files errored, chunks stored (from Qdrant `count_points`), and timestamps for last successful / last incremental / last full run. Tolerates Qdrant unreachable (prints a diagnostic instead of crashing) so operators can still inspect state after Qdrant goes down.

### `ingstr health [--config PATH]`

Check connectivity to Qdrant, Ollama, the NFS mount, and the compiled plan. Prints `[OK]` / `[FAIL]` for each. Exits `0` if all pass, `2` otherwise.

### `ingstr version`

Print version and exit.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | config error (missing file, invalid YAML, validation failed, missing required env var) |
| 2 | upstream unavailable (Qdrant, Ollama, NFS, plan unreachable or unreadable) |
| 3 | partial failure (some files errored during ingest, run otherwise OK) |
| 4 | fatal during run (unhandled exception, run aborted) |

## 1.6 Troubleshooting

### `ingstr health` failures

| Failing check | Likely cause | Fix |
|---|---|---|
| `source.root FAIL` | NFS volume didn't mount | Check `ORG_DATA_NFS_*` in `secrets.env`; verify NFS server allows otter; `docker compose --env-file secrets.env run --rm ingstr ls /mnt/data` to inspect |
| `plan FAIL: required_groups` | `group_gid_map.yml` is stale or has groups not in `compiled_plan.yml` | Regenerate the map upstream (`export_group_gids.yml`) and redeploy |
| `ollama FAIL` | Endpoint unreachable, or the configured model isn't loaded | Check Ollama container; `ollama pull <model>` if missing |
| `qdrant.connect FAIL` | URL/network/key wrong, or the per-org Qdrant network doesn't exist yet | Check `qdrant.url` resolves on the per-org network; verify `QDRANT_NETWORK` matches the actual Docker network name |
| `qdrant.collection FAIL: does not exist` | Bootstrap step skipped | Run the `PUT /collections/documents` step from §1.4 |
| `qdrant.collection FAIL: vector size <N> != <M>` | Collection size doesn't match `embedding.vector_dim` | Recreate the collection with the correct size, or change `vector_dim` in config |

### `ingest` errors

| Symptom | Diagnosis | Action |
|---|---|---|
| `UnclassifiableFile: gid <N> not present in group_gid_map.yml` | A file's group ownership isn't covered by the upstream-generated map. NFS may be GID-squashing, or upstream `rbac-compile` is missing the group. | If GID is `nobody`/`65534`, check NFS export squash settings. Otherwise flag upstream. Ingstr will not classify with a default — by design. |
| `IngstrError: failed to parse <path>` | `unstructured` couldn't handle the file (corrupt, exotic format, libmagic failure). | The file is logged + skipped; the run continues. Inspect the file manually. |
| `IngstrError: parsed but produced 0 chunks` | File parsed but yielded no extractable text (e.g. an image-only PDF without OCR). | MVP doesn't OCR; consider whether to skip these by extension via `exclude_patterns`. |
| `UpstreamUnavailable` mid-run | Ollama or Qdrant became unreachable during the run. | The run aborts with exit 2. Investigate the upstream and re-run; idempotency means no progress is lost. |

### Per-file errors are recorded

Errors during a per-file step are recorded in the `state.files.last_error` column. They're re-attempted automatically on the next run. Use `sqlite3 /var/lib/ingstr/state.db 'SELECT source_path, last_error FROM files WHERE last_error IS NOT NULL'` to inspect (note: this is inside the named volume; mount it for inspection).

## 1.7 Image versioning and upgrades

Tags follow semver with optional pre-release suffixes (`-alpha`, `-beta`, `-rc1`). The image tag is supplied via `INGSTR_VERSION` in `secrets.env` — no compose edit needed for upgrades:

```bash
docker pull ghcr.io/jobcpf/ingstr-app:vX.Y.Z
sudo sed -i 's|^INGSTR_VERSION=.*|INGSTR_VERSION=vX.Y.Z|' /etc/ingstr/<org>/secrets.env
docker compose --env-file secrets.env run --rm ingstr health
```

Rollback is the reverse — old images stay in the local cache unless explicitly pruned.

`:latest` is intentionally NOT updated for pre-release tags. Always pin explicitly.

## 1.8 Triggering

Ingstr is one-shot — invoked, runs to completion, exits. The intended trigger is a systemd `.path` unit watching a host-visible sentinel file (typically `/var/lib/ingstr/triggers/<org>/last_sync`) which fires a `.service` unit running `docker compose run`.

The sentinel path is **deploy config, not application contract** — Ingstr never touches the sentinel. Platform team decides where the upstream sync writes it.

Examples in [`deploy/systemd/`](deploy/systemd/).

---

# Part 2 — Application Reference

## 2.1 Purpose

Ingstr is the **write path** of a Retrieval-Augmented Generation system. Specifically, it turns files on a mounted filesystem into vectors in a Qdrant collection, with RBAC group metadata stamped on every chunk's payload. A separate query-time service (the proxy) is responsible for filtering by that field at retrieval time.

One Ingstr invocation = one organisation. Multi-org is achieved by running multiple instances with different configs, different Qdrant collections, and different write API keys.

## 2.2 Hard boundaries (what Ingstr does NOT do)

These are deliberate. Each boundary keeps the application small and testable.

| Boundary | Why |
|---|---|
| Does not enforce access | Ingstr stamps `classification_group`; a separate query-time service filters by that field. Ingstr never reads who-can-see-what; it only writes what-this-is. |
| Does not embed | Ingstr calls an HTTP embedding endpoint (Ollama-compatible). The embedding model lives elsewhere. |
| Does not classify by path | Classification comes from the file's filesystem group ownership (`stat(file).st_gid`), resolved via `group_gid_map.yml`. Path-based classification is upstream's responsibility (rclone copies files with the right group ownership). |
| Does not create the Qdrant collection | The collection is provisioned separately (Ansible). Ingstr fails fast if the collection doesn't exist or has the wrong vector dimension. |
| Does not serve queries | Write-only into Qdrant. |
| Does not run a daemon | Invoked (by systemd, cron, or shell) and runs to completion. Triggering is upstream. |
| Does not manage credentials | All secrets come from env vars or the operator-owned `secrets.env`. |
| Does not multi-tenant in one process | One invocation = one org. |
| Does not refuse loudly when partially broken | If Ollama is down, Ingstr fails the run and exits non-zero — it does not silently skip embedding. **Failures are loud.** |
| Does not produce or mutate `compiled_plan.yml` / `group_gid_map.yml` | Both files are read-only inputs from upstream. |

## 2.3 Architecture topology (per-org isolation)

```
                           otter (host)
   ┌──────────────────────────────────────────────────────────────┐
   │   /mnt/registry/  ◄── shared NFS mount (host-level)          │
   │                       compiled_plan.yml + group_gid_map.yml  │
   │                                                              │
   │   /var/lib/ingstr/triggers/<org>/last_sync ◄── host-visible  │
   │                       sentinel; upstream sync touches this   │
   │                       to trigger ingest via systemd .path    │
   │                                                              │
   │   /etc/ingstr/<org>/                                         │
   │       config.yml         (per-org, Ansible-templated)        │
   │       compose.yml        (per-org, copied from example)      │
   │       secrets.env        (per-org, Ansible-templated)        │
   │                                                              │
   │   ┌────────────────────────┐    ┌─────────────────────────┐  │
   │   │  Ollama container      │    │  Qdrant containers      │  │
   │   │  ollama/ollama:0.22.0  │    │  one per org,           │  │
   │   │  shared across orgs    │    │  per-org API key,       │  │
   │   │  http://172.16.32.32   │    │  per-org Docker net     │  │
   │   │       :11434           │    │  no published ports     │  │
   │   └────────────────────────┘    └─────────────────────────┘  │
   │                                                              │
   │   ┌──────────────────────── Ingstr container <org> ──────┐   │
   │   │   /mnt/data        ◄── NFS volume from org's NAS     │   │
   │   │                        (mounted by Docker, not host) │   │
   │   │   /mnt/registry    ◄── bind from host /mnt/registry  │   │
   │   │   /etc/ingstr      ◄── bind from /etc/ingstr/<org>/  │   │
   │   │   /var/lib/ingstr  ◄── named volume (per-org state)  │   │
   │   │                                                      │   │
   │   │   docker-entrypoint.sh:                              │   │
   │   │     reads group_gid_map.yml                          │   │
   │   │     setpriv → drops to ingstr:ingstr w/ sup GIDs     │   │
   │   │     execs `ingstr <subcommand>`                      │   │
   │   └──────────────────────────────────────────────────────┘   │
   └──────────────────────────────────────────────────────────────┘
```

Critical invariants:

- **Org data is NFS-mounted by Docker INTO the container, never onto the host.** otter's host filesystem never has filesystem access to org documents — the security boundary.
- **The registry IS host-mounted** on otter and bind-mounted into each container. Same registry across all orgs.
- **The trigger sentinel IS host-visible** — needed for the systemd `.path` watcher, since it can't watch in-container paths.
- **The container starts as root and drops privileges** via `setpriv` with supplementary GIDs derived from `group_gid_map.yml`. Do NOT run with `--user` — that bypasses the entrypoint and breaks group access.
- **Qdrant is per-org with its own write API key**, on its own Docker network, with no published ports. The proxy service is the only path to Qdrant from outside the per-org network.
- **Ollama is shared across orgs**, addressed by LAN IP (`http://172.16.32.32:11434` currently). It will move to a different host (`lynx`) later.

## 2.4 Classification flow (the security-critical bit)

Classification is **fail-closed** and derived from filesystem group ownership. Ingstr never falls back to `/etc/group`, path heuristics, or default groups.

### Inputs

Two YAML files from upstream, mounted at `/mnt/registry/`:

#### `compiled_plan.yml` (from `rbac-compile`)

```yaml
required_groups:
  - arc_g0_engineering_global
  - arc_g18_any_global
  - arc_g1_any_uk
  # ... etc.

agent_users:
  - name: agent_oversight
    groups: [arc_g0_engineering_global, arc_g18_any_global, ...]
  # ... etc.

admin_users:
  # ... etc.

directory_classifications:
  # ... informational only; Ingstr classifies by GID, not path
```

Ingstr reads only `required_groups` (the canonical name set).

#### `group_gid_map.yml` (from `export_group_gids.yml` on the source host)

```yaml
groups:
  arc_g0_engineering_global: 1003
  arc_g18_any_global: 1004
  arc_g1_any_uk: 1005
  # ...
```

This is the host-specific name → numeric-GID mapping. Regenerated upstream when groups are added/removed or the source host is rebuilt.

### Startup behaviour

1. Load both YAMLs.
2. Invert `group_gid_map.yml` into `gid_to_group: dict[int, str]`.
3. Cross-validate: every group in `group_gid_map.yml` MUST appear in `compiled_plan.yml:required_groups`. Mismatch = `PlanError`, exit 1.

### Per-file classification

For each file:

```python
gid = file.stat().st_gid
if gid not in gid_to_group:
    raise UnclassifiableFile(path, gid)        # fail-closed: skip the file, log error
group = gid_to_group[gid]                       # stamp as classification_group on every chunk
```

Files that raise `UnclassifiableFile` are recorded in the state DB's `last_error` column and skipped. They are never indexed with a default group. This is the entire access-control model: if the GID isn't in the trusted map, the file doesn't make it into the index.

## 2.5 The ingest pipeline (per-file, brief §8)

```
1. classify          — stat → gid → group_name (above)
2. parse             — unstructured.partition.auto.partition(file) → list of elements
3. chunk             — unstructured.chunking.title.chunk_by_title(elements) → list of chunks
4. hash              — sha256 of file contents (for change detection)
5. embed             — POST chunks to Ollama in batches, get list of vectors
6. build points      — assemble Qdrant point records with deterministic UUIDs
7. delete old points — if file was previously indexed: delete by source_path
8. upsert            — write points to Qdrant in batches
9. update state      — record source_path, hash, mtime, group, chunk_count, indexed_at
```

### Idempotency

- **Deterministic point IDs** via `uuid5(_NAMESPACE, f"{source_path}:{chunk_index}")`. Re-running on the same file overwrites the same points cleanly — no duplicates.
- **Hash-based skip:** if a file's content hash matches what's in the state DB, the entire pipeline short-circuits. No parse, no embed, no Qdrant writes. Empty re-runs are a no-op.

### Full-mode behaviour (`--full`)

- Hash unchanged + group unchanged → still skip (no re-embed).
- Hash unchanged + group changed → call `set_payload` on Qdrant to update only `classification_group` + `indexed_at` on existing points. **No re-embed.**
- Hash changed → delete old points by `source_path`, then upsert new points (full pipeline).
- Files in state but not on disk → delete points + state row (orphan cleanup).

### Error handling

- `UnclassifiableFile`, parse failures, empty-chunk results → record per-file error, log at `error` level, continue with the next file. Run exits 3 if any per-file errors occurred.
- `UpstreamUnavailable` (Ollama or Qdrant unreachable) → propagate, abort the run, exit 2. Don't silently swallow.
- Unexpected exceptions → log with traceback, abort, exit 4.

## 2.6 Qdrant point structure (output contract)

This is what downstream consumers (the proxy) read. Stable across versions; changes to this schema are breaking.

```python
{
    "id": <UUID-v5 string>,                     # uuid5(_NAMESPACE, f"{source_path}:{chunk_index}")
    "vector": [<vector_dim float>...],          # 768 for nomic-embed-text
    "payload": {
        "text": "<chunk content>",
        "source_path": "/mnt/data/...",         # absolute path inside the container
        "source_path_rel": "drive/...",         # relative to source.root
        "classification_group": "arc_g0_engineering_global",
        "modified_at": "2026-04-28T08:00:00Z",  # file mtime, ISO8601 UTC
        "indexed_at":  "2026-04-28T08:15:00Z",  # when ingstr processed it
        "file_type": "pdf",                     # extension lowercase, "unknown" if none
        "chunk_index": 3,                       # 0-based
        "chunk_total": 12,                      # total chunks for this file
        "content_hash": "a3f7b9..."             # sha256 of source file
    }
}
```

Required payload indexes on the collection (provisioned upstream):

- `source_path` (keyword) — required for Ingstr's delete-by-source operation.
- `classification_group` (keyword) — required for the proxy to filter efficiently. Ingstr doesn't query, but this index is what makes the whole RBAC story work at retrieval time.

## 2.7 State DB schema

SQLite, single file, container-internal at `/var/lib/ingstr/state.db` (backed by per-org named Docker volume).

```sql
CREATE TABLE files (
    source_path          TEXT PRIMARY KEY,
    content_hash         TEXT NOT NULL,
    mtime                REAL NOT NULL,
    size_bytes           INTEGER NOT NULL,
    classification_group TEXT NOT NULL,
    chunk_count          INTEGER NOT NULL,
    last_indexed_at      TEXT NOT NULL,
    last_error           TEXT                   -- nullable
);
CREATE INDEX idx_files_group ON files(classification_group);
CREATE INDEX idx_files_mtime ON files(mtime);

CREATE TABLE runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,                       -- nullable until run ends
    mode            TEXT NOT NULL,              -- 'incremental' | 'full'
    files_seen      INTEGER,
    files_indexed   INTEGER,
    files_skipped   INTEGER,
    files_errored   INTEGER,
    chunks_written  INTEGER,
    exit_code       INTEGER
);
```

The `files` table drives the hash-based skip and mtime-based change detection. The `runs` table feeds `ingstr stats`.

Why SQLite (not Qdrant payload as state)? Two-source-of-truth is acceptable for v0.1 to keep ingest fast and the implementation simple. The brief flags state-in-Qdrant as a v0.2+ candidate.

## 2.8 Group permissions in the container

Files on the org's NFS mount are mode `0640` and owned by canonical groups (`arc_g0_engineering_global`, etc.). To read them, the container's `ingstr` user needs those GIDs as supplementary groups. The image uses a **dynamic** approach so the GID set is not baked into the image (otherwise upstream regenerating `group_gid_map.yml` would force a rebuild).

### Entrypoint flow

1. Container starts as root.
2. [`docker-entrypoint.sh`](docker-entrypoint.sh) reads `${INGSTR_CONFIG}` to find the path of `group_gid_map.yml`.
3. Loads the map's `groups:` and extracts the GID set as a comma-separated string.
4. `setpriv --reuid=ingstr --regid=ingstr --groups=<gids> --inh-caps=-all -- ingstr <subcommand>` drops to non-root, with those GIDs added as supplementary groups.

### Why this matters

- When upstream regenerates `group_gid_map.yml`, no image rebuild is needed; the next run picks up the new map automatically.
- `--user` flags **break this** — `setpriv --groups` requires `CAP_SETGID`, which a non-root process doesn't have. Don't pass `--user`.
- Files with GIDs not in the map fail at the application layer (`UnclassifiableFile`), not at the entrypoint — by design. The entrypoint only gives the process the *option* to read those groups; classification still gates.

## 2.9 Logging

Structured JSON to stdout via `structlog`. One record per file processed (event: `file_indexed`, `file_unclassifiable`, `file_failed`, `file_payload_refreshed`, `file_orphaned_deleted`), plus run-level start/end (`run_started`, `run_finished`, `run_aborted_unexpected`).

Required fields per file event:

```json
{
  "ts": "2026-04-28T08:15:23.451Z",
  "level": "info",
  "event": "file_indexed",
  "source_path": "/mnt/data/Engineering/spec.pdf",
  "classification_group": "arc_g0_engineering_global",
  "chunk_count": 12
}
```

**File contents are never logged at INFO level** — only at DEBUG, and only when `logging.log_full_query: true`.

### NLP models in the image

`unstructured` lazy-loads NLP models (spaCy `en_core_web_sm`, NLTK `punkt`/`punkt_tab`/`averaged_perceptron_tagger`) the first time it parses a file that needs them. Without intervention these would fail under the privilege-dropped `ingstr` user with `[Errno 13] Permission denied` writing into `/opt/venv`. The Dockerfile builder pre-downloads them at build time (as root) into `/opt/venv` and `/opt/venv/share/nltk_data`, with `NLTK_DATA` set in both stages. `unstructured` therefore never attempts a runtime download and the privilege-dropped process only ever reads from `/opt/venv`.

When upgrading `unstructured`, check its release notes for any new lazy-loaded model dependencies — they will need to be added to the same builder-stage download step.

## 2.10 Testing

| Layer | Where | What |
|---|---|---|
| Unit tests | [`tests/unit/`](tests/unit/) | ~99 cases covering config validation, plan loading + cross-validation, classify (with mocked stat), hashing, state DB CRUD + idempotency, embed via `httpx.MockTransport`, qdrant_io with mocked `QdrantClient`, parse/chunk via mocked `unstructured`, pipeline orchestration with patched I/O, CLI via Typer's `CliRunner` |
| Integration test | [`tests/integration/`](tests/integration/) | Placeholder for a testcontainers-Qdrant end-to-end test — not yet implemented |
| Real-services smoke test | manual on otter | `health`, `ingest --dry-run`, `ingest`, `stats` against real Ollama + per-org Qdrant + NFS mounts |
| CI | [`.github/workflows/ci.yml`](.github/workflows/ci.yml) | Runs `pytest` + `ruff` + `mypy` on every push/PR, on Python 3.11 and 3.12 |
| Image build | [`.github/workflows/release.yml`](.github/workflows/release.yml) | Triggered on tags matching `v*`; builds + pushes to GHCR |

## 2.11 Out of scope for v0.1 (deferred)

- Migration of state from SQLite into Qdrant payload (single source of truth)
- Webhook-based or filesystem-watch triggering (currently invoked externally)
- Multi-org in one process
- Reranking, hybrid search, or any query-side concern (that's the proxy)
- Quantisation
- Cross-collection deduplication
- Custom parsers per file type (`unstructured` handles all in MVP)
- OCR for image-only PDFs (would require the heavy `unstructured[local-inference]` extras)
- Distributed running (single-process for MVP)
- Encryption at rest beyond what Qdrant provides
- Prometheus / OpenTelemetry metrics

## 2.12 Versioning

Semver. Pre-release suffixes (`-alpha`, `-beta`, `-rc1`) for non-stable tags. The image tag is what consumers pin via `INGSTR_VERSION`. `:latest` only updates for non-prerelease tags on the default branch.

The brief, this REFERENCE, and the Qdrant point payload schema (§2.6) are the three documents that define application contracts. Changes to any of them are breaking unless additive in a backwards-compatible way.
