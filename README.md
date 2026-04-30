# Ingstr

> Document ingestion pipeline for Qdrant: walk a filesystem tree, parse + chunk documents, embed via an Ollama-compatible HTTP endpoint, and upsert into Qdrant with RBAC classification metadata stamped on every chunk.

Ingstr is the **write path** of a Retrieval-Augmented Generation system. It is not a query tool, not an access enforcer, and not a database manager. One Ingstr invocation = one organisation. Headless, idempotent, configurable.

## Architecture context

Ingstr is one component of a larger RAG/RBAC deployment. Upstream, an `rbac-compile` step produces `compiled_plan.yml` (canonical group names) and an `export_group_gids.yml` step produces `group_gid_map.yml` (`name → gid` for the source host). Downstream, a separate query-time service filters Qdrant search results by the `classification_group` payload field that Ingstr stamps. Ingstr reads from the filesystem, calls out to Ollama for embeddings, and writes to Qdrant — nothing else.

## Install

```bash
pipx install ingstr
ingstr --help
```

Requires Python 3.11+ and a Linux host with the source filesystem mounted and group ownership populated by the upstream playbook.

## Minimal configuration

```yaml
org: arc
source:
  root: /mnt/raid_arc/drive
plan:
  compiled_plan_path: /mnt/registry/compiled_plan.yml
  group_gid_map_path: /mnt/registry/group_gid_map.yml
embedding:
  endpoint: http://ollama:11434
  model: nomic-embed-text
  vector_dim: 768
qdrant:
  url: http://qdrant_arc:6333
  api_key_env: QDRANT_RW_API_KEY
  collection: documents
state:
  db_path: /var/lib/ingstr/state.db
```

See [config.example.yml](config.example.yml) for the full schema with all options.

## CLI

```
ingstr ingest [--full] [--dry-run] [--config PATH]
    Walk the configured tree and ingest changed files. Default is incremental
    (only files where mtime > last_indexed_at OR content hash changed).
    --full forces a complete re-walk: re-embeds nothing whose hash matches,
    deletes Qdrant points for files no longer on disk, refreshes the
    classification_group payload where filesystem GID has changed.

ingstr stats [--config PATH]
    Print counts: files known, chunks stored, files errored, last successful
    run, last incremental run, last full run. Reads from SQLite state DB
    and Qdrant. Read-only.

ingstr health [--config PATH]
    Check connectivity to Qdrant, Ollama, NFS mount, and the compiled plan.
    Print status of each. Exit non-zero if anything is unreachable.

ingstr version
    Print version and exit.
```

Exit codes: `0` success · `1` config error · `2` upstream unavailable · `3` partial failure (some files errored, run otherwise OK) · `4` fatal during run.

## How it classifies

Classification is **fail-closed** and derived from filesystem group ownership. Ingstr never falls back to `/etc/group` or path heuristics.

1. At startup, Ingstr loads `compiled_plan.yml` (for the canonical `required_groups:` set) **and** `group_gid_map.yml` (for the `name → gid` mapping on the source host). It inverts the map to `gid → name` and cross-validates that every mapped group is in `required_groups`. A mismatch (stale map) causes Ingstr to exit `1`.
2. For each file, Ingstr calls `file.stat().st_gid`, looks the GID up in the inverted map, and stamps the result as `classification_group` on every chunk's Qdrant payload.
3. If the file's GID is **not** in the map, Ingstr raises `UnclassifiableFile`, logs it as an error, records `last_error` in the state DB, and **skips** the file. It is not indexed with a default group. Ever.

GIDs are OS-assigned and host-specific. `group_gid_map.yml` must be regenerated upstream whenever groups are added/removed or the source host is rebuilt — Ingstr fails loudly when the map is missing or inconsistent.

## What Ingstr does NOT do

Each of these is a deliberate boundary:

- **Does not enforce access.** It only stamps `classification_group`; a separate query-time service filters by it.
- **Does not embed.** It calls an HTTP endpoint (Ollama-compatible).
- **Does not classify by path.** GID-based, period.
- **Does not create the Qdrant collection.** Provisioned by Ansible. Ingstr fails fast if the collection is missing.
- **Does not serve queries.** Write-only into Qdrant.
- **Does not run a daemon.** Invoked by systemd, cron, or shell. Triggering is upstream.
- **Does not manage credentials.** All secrets via env vars or operator-owned config.
- **Does not multi-tenant.** One invocation, one org. Run multiple instances for multiple orgs.
- **Does not silently skip on partial failure.** Loud failures, non-zero exit.

## Running in Docker on otter

The intended deployment is a one-shot container on `otter`, alongside (but separate from) the Qdrant and Ollama stacks. Tagged releases publish an image to GHCR (`ghcr.io/jobcpf/ingstr-app`) via [.github/workflows/release.yml](.github/workflows/release.yml). On otter, pull the image and invoke ingest with `docker compose run`:

```bash
docker compose -f /etc/ingstr/compose.yml run --rm ingstr health
docker compose -f /etc/ingstr/compose.yml run --rm ingstr ingest
```

Required volume mounts: `/mnt/raid_arc:ro` (source), `/mnt/registry:ro` (compiled plan + GID map), `/etc/ingstr:ro` (config), and a named volume for `/var/lib/ingstr` (state DB).

**Group permissions.** Files on the source NFS mount are mode `0640` and owned by canonical groups (`arc_g0_engineering_global`, etc.). The container starts as root, the entrypoint reads the configured `group_gid_map.yml`, and `setpriv` drops to a non-root `ingstr` user with those GIDs as supplementary groups. No image rebuild is needed when the upstream map changes.

See [deploy/README.md](deploy/README.md) for the full walkthrough — GitHub repo setup, GHCR publication, otter pull, smoke test, and systemd path-unit triggering.

## Operational notes

**Triggering from systemd path units.** Ingstr is invoked when something upstream touches a host-visible sentinel file (path is platform-controlled — typically `/var/lib/ingstr/triggers/<org>/last_sync`). A systemd `path` unit watches the sentinel and triggers an associated `service` unit that runs `docker compose run ingstr ingest`. Example unit files in [deploy/systemd/](deploy/systemd/).

**Checking stats.** `ingstr stats` is read-only and safe to run at any time. It reports file counts, chunk counts, error counts, and last-run timestamps.

**Recovering from errors.** Per-file errors are recorded in the state DB's `last_error` column and re-attempted on the next run. Systemic failures (Qdrant unreachable, plan unreadable) abort the run with a non-zero exit code; fix the underlying issue and re-invoke. Re-running an incremental ingest with no changes is a no-op.

**The `unstructured` parser.** MVP uses `unstructured` without the `[local-inference]` extra to keep the install lean (those extras pull in multi-gigabyte ML dependencies). The Docker image installs `libmagic1` and `poppler-utils` for the rule-based partitioners; PDFs with complex layouts may parse less accurately than they would with the local-inference path. If higher fidelity is needed, install `unstructured[local-inference]` separately or adjust the dependency in a downstream fork.

## Development setup

```bash
git clone https://github.com/jobcpf/ingstr-app.git
cd ingstr
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Integration tests (which spin up Qdrant via testcontainers) are marked `integration` and skipped by default. Run them explicitly:

```bash
pytest -m integration
```

Ingstr targets Linux only. Develop and test on a Linux host.

## Future work (out of scope for v0.1)

- Migration of state from SQLite into Qdrant payload (single source of truth)
- Webhook-based or filesystem-watch triggering
- Multi-org in one process
- Reranking, hybrid search, or any query-side concern
- Quantisation
- Cross-collection deduplication
- Custom parsers per file type
- Distributed running
- Encryption at rest beyond what Qdrant provides

## License

Apache-2.0. See [LICENSE](LICENSE).
