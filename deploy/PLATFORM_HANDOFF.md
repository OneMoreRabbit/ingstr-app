# Platform Handoff — Ansible Deployment of Ingstr

> Living document. Status as of 2026-04-30: Ingstr v0.1.0-beta tagged, image
> on GHCR, manual testing on otter underway. Per-org Qdrant containers and
> Ansible roles are next — this document is the contract between Ingstr
> (the application) and the platform / Ansible work that deploys it.

---

## 1. What Ingstr is, in a paragraph

Ingstr is a standalone Python application packaged as a Docker image. It walks an organisation's filesystem tree, parses + chunks documents via the `unstructured` library, embeds chunks via an Ollama HTTP endpoint, and upserts the result into a Qdrant collection with `classification_group` metadata stamped on every chunk's payload. It's headless, idempotent, runs to completion, and exits. **Per-org isolation is achieved by running multiple Ingstr containers — one per org — with different configs, different Qdrant collections, and different write API keys.** A separate downstream service (not Ingstr's concern) does query-time filtering by `classification_group`.

The full build brief is at [`ingstr-brief-v0.1.md`](../../ingstr-brief-v0.1.md) one directory up; this handoff assumes you've at least skimmed it.

---

## 2. Where the boundary sits

| Owned by Ingstr (this repo) | Owned by platform / Ansible |
|---|---|
| Image build, GHA workflow, GHCR publish | Image deploy on otter (`docker pull`, run) |
| `Dockerfile`, `docker-entrypoint.sh` | Templating `config.yml`, `compose.yml`, `secrets.env` per-org |
| Per-file pipeline logic, classification, embedding, Qdrant I/O | Provisioning per-org Qdrant containers (collection, payload indexes, API keys) |
| Reading `compiled_plan.yml` + `group_gid_map.yml` | Producing those files (upstream `rbac-compile`, `export_group_gids.yml`) |
| Reading `source.root` and writing to Qdrant | Mounting the org's data NFS share into the container |
| `ingstr health`, `ingstr stats` (verification CLIs) | Host-side trigger sentinel + systemd .path/.service unit |
| Unit tests, integration test (testcontainers Qdrant) | Real-services smoke test on otter, ongoing operations |

Two firm boundaries that should not be crossed by Ansible:

1. **Ingstr only consumes `compiled_plan.yml` and `group_gid_map.yml`.** Don't generate or mutate them in an Ingstr-deployment role; that belongs to the upstream `rbac-compile` / `export_group_gids` workflow on the source host (currently `beaver`).
2. **Ingstr never has filesystem access to org data on the host.** Org data is mounted into the container only, via Docker's `local + nfs` volume driver. otter's host operator must not have a host-side bind mount for org documents — that's the security boundary we're enforcing.

---

## 3. Current state of Ingstr development

| | Status |
|---|---|
| Code (config, plan, classify, hash, state, embed, qdrant_io, parse, chunk, pipeline, CLI) | Implemented |
| Unit tests (~99) | Passing on Zaphod |
| Integration test (testcontainers Qdrant) | Placeholder — not yet implemented |
| Image build via GHA → GHCR | Working (alpha + beta tags shipped) |
| Pull & run on otter (`version`, `--help`) | Verified on alpha |
| Real-services smoke test (`health`, `ingest`) | Pending — depends on per-org Qdrant being up |
| Ansible deploy role | Pending — this document is the input |

Latest tagged release: **`v0.1.0-beta`** at `ghcr.io/jobcpf/ingstr-app:v0.1.0-beta`. Public image (no auth required to pull). `:latest` is intentionally NOT updated for pre-release tags; pin the version explicitly via the `INGSTR_VERSION` env var.

---

## 4. Architecture topology (per-org isolation)

One Ingstr deploy = one organisation. Multiple orgs share infra where it's safe; everything else is per-org.

```
                           otter (host)
   ┌──────────────────────────────────────────────────────────────┐
   │                                                              │
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
   │   │  http://172.16.32.32   │    │  Ansible-deployed       │  │
   │   │       :11434           │    │                         │  │
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
   │                                                              │
   └──────────────────────────────────────────────────────────────┘
```

Critical facts:

- **`/mnt/data` inside the container is NFS-mounted by Docker**, not by the host. otter's host filesystem never sees the org's data. This is the security isolation.
- **`/mnt/registry/` IS host-mounted** on otter; Ingstr just bind-mounts it in. Same registry across all orgs.
- **`/var/lib/ingstr/triggers/<org>/last_sync` IS host-visible** — needed for the systemd `.path` watcher, since it can't watch in-container paths.
- **The container starts as root and drops privileges** at entrypoint via `setpriv` with supplementary GIDs derived from `group_gid_map.yml`. This is how it gets read access to mode-0640 group-restricted files. Do not run with `--user` — that bypasses the entrypoint and breaks group access.

---

## 5. Per-host setup (otter, once)

Run these once when first provisioning otter, idempotent on re-runs:

### 5.1 Docker

Standard install. Compose plugin must be present (Ingstr uses `docker compose run`, not the legacy `docker-compose`).

### 5.2 Registry NFS mount

Mount the registry NFS share at `/mnt/registry/` on the host. Confirmed working as of handoff date — owned `otter:otter`, contains `compiled_plan.yml` and `group_gid_map.yml`. Should be read-only for safety.

```
/etc/fstab entry (illustrative):
nfs.server.local:/exports/registry  /mnt/registry  nfs  ro,nolock,soft,nfsvers=4  0 0
```

### 5.3 Trigger sentinel directory tree

Create the host-side trigger directory (per-org subdirectories will be created at deploy time):

```bash
sudo mkdir -p /var/lib/ingstr/triggers
sudo chown root:root /var/lib/ingstr/triggers
sudo chmod 755 /var/lib/ingstr/triggers
```

The upstream sync process is responsible for writing per-org sentinels here (e.g. `/var/lib/ingstr/triggers/arc/last_sync`). Coordinate with whoever owns the upstream sync — it currently writes into the data tree, which won't work since that tree isn't on the host any more.

### 5.4 GHCR auth (optional)

The `jobcpf/ingstr-app` image is currently public — no `docker login` needed. If the image becomes private later, store a `read:packages` PAT in root's docker config. **Do not** put this PAT in any per-org `secrets.env`.

---

## 6. Per-org setup (once per org)

For each org that should ingest documents, Ansible creates a per-org deploy directory and a per-org Qdrant container.

### 6.1 Variables Ansible should template per-org

| Variable | Source | Goes into | Notes |
|---|---|---|---|
| `org_name` | inventory | `config.yml:org`, directory paths | "arc", "cpf", etc. |
| `qdrant_url` | inventory | `config.yml:qdrant.url` | Per-org Qdrant container's URL |
| `qdrant_collection` | inventory or default | `config.yml:qdrant.collection` | Default `documents` works |
| `qdrant_rw_api_key` | secret store | `secrets.env:QDRANT_RW_API_KEY` | Per-org write key |
| `qdrant_ro_api_key` | secret store | Qdrant container env | For downstream query service |
| `org_data_nfs_addr` | inventory | `secrets.env:ORG_DATA_NFS_ADDR` | NFS server hosting org data |
| `org_data_nfs_device` | inventory | `secrets.env:ORG_DATA_NFS_DEVICE` | NFS export path |
| `org_data_nfs_options` | inventory or default | `secrets.env:ORG_DATA_NFS_OPTIONS` | Default `soft,ro,nolock` |
| `ingstr_version` | inventory | `secrets.env:INGSTR_VERSION` | Image tag, e.g. `v0.1.0-beta` |
| `ollama_endpoint` | inventory or default | `config.yml:embedding.endpoint` | Currently `http://172.16.32.32:11434` |
| `embedding_model` | inventory or default | `config.yml:embedding.model` | Default `nomic-embed-text` |
| `vector_dim` | inventory or default | `config.yml:embedding.vector_dim` | **Must match Qdrant collection size**. `nomic-embed-text` → 768 |

### 6.2 Files Ansible should produce

```
/etc/ingstr/<org>/
├── config.yml         # templated from config.example.yml
├── compose.yml        # copied from deploy/compose.example.yml (no per-org edits needed)
└── secrets.env        # templated; mode 0600
```

Reference templates in this repo:
- [`config.example.yml`](../config.example.yml)
- [`compose.example.yml`](compose.example.yml)

### 6.3 Per-org Qdrant container

Provisioning approach is up to platform, but the resulting Qdrant **must** satisfy these contracts before the first `ingstr ingest` runs:

#### Container env

```
QDRANT__SERVICE__API_KEY=<qdrant_rw_api_key>
QDRANT__SERVICE__READ_ONLY_API_KEY=<qdrant_ro_api_key>
```

(Both keys; the read-only one isn't used by Ingstr but should exist for the future query service.)

#### Volume

`/qdrant/storage` should be a named volume so collection state persists.

#### Image

`qdrant/qdrant:v1.13.x` (pin a tag — avoid `:latest`).

#### Network

Reachable from the Ingstr container at `${qdrant_url}` (which goes into Ingstr's `config.yml:qdrant.url`). Either same Docker network (use service names) or LAN IP (current pattern, matching the Ollama setup).

#### Bootstrap (once, after Qdrant is up)

```bash
QURL=http://qdrant_<org>:6333
KEY=<qdrant_rw_api_key>

# Collection — vector size MUST match config.yml:embedding.vector_dim
curl -X PUT "${QURL}/collections/documents" \
  -H "api-key: ${KEY}" \
  -H "Content-Type: application/json" \
  -d '{ "vectors": { "size": 768, "distance": "Cosine" } }'

# Payload index on source_path — required for delete-by-source efficiency
curl -X PUT "${QURL}/collections/documents/index" \
  -H "api-key: ${KEY}" -H "Content-Type: application/json" \
  -d '{ "field_name": "source_path", "field_schema": "keyword" }'

# Payload index on classification_group — REQUIRED for downstream query
# service to filter by RBAC group efficiently. Ingstr doesn't query, but
# the index is what makes the whole RBAC story actually work.
curl -X PUT "${QURL}/collections/documents/index" \
  -H "api-key: ${KEY}" -H "Content-Type: application/json" \
  -d '{ "field_name": "classification_group", "field_schema": "keyword" }'
```

Out of scope for v0.1: quantisation, named vectors, sharding. Don't enable any of those.

### 6.4 Per-org systemd units (for triggered ingest)

Copy and customise [`deploy/systemd/ingstr-ingest.service`](systemd/ingstr-ingest.service) and [`deploy/systemd/ingstr-ingest.path`](systemd/ingstr-ingest.path):

- Rename to `ingstr-ingest-<org>.service` and `ingstr-ingest-<org>.path` for multi-org hosts.
- Update `WorkingDirectory=` and `EnvironmentFile=` to point at `/etc/ingstr/<org>/`.
- Update `PathChanged=` to `/var/lib/ingstr/triggers/<org>/last_sync`.

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ingstr-ingest-<org>.path
```

### 6.5 NFS export from the org's NAS

The NFS server exporting the org's data tree must:
- Allow mounts from otter's IP
- Export with appropriate squash/permission rules so file GIDs are preserved (no `all_squash` or `anonuid`/`anongid` rewriting — Ingstr classifies via `st_gid` and needs the original numeric GIDs)
- Match the GIDs in `/mnt/registry/group_gid_map.yml`

This is upstream's responsibility, but flag it during deploy planning — it's the most common source of "every file is unclassifiable" failures.

---

## 7. Per-deploy operations (image upgrade or config change)

```bash
ORG=arc

# Image upgrade
docker pull ghcr.io/jobcpf/ingstr-app:v0.x.y
sudo sed -i 's|^INGSTR_VERSION=.*|INGSTR_VERSION=v0.x.y|' /etc/ingstr/${ORG}/secrets.env

# Smoke test (does not run an ingest)
cd /etc/ingstr/${ORG}
docker compose --env-file secrets.env run --rm ingstr health

# If health passes and the systemd .path unit is enabled, the next
# upstream sync will trigger an ingest automatically. Or trigger
# manually:
sudo touch /var/lib/ingstr/triggers/${ORG}/last_sync
journalctl -u ingstr-ingest-${ORG}.service -f
```

Rollback is just changing `INGSTR_VERSION` back; old images stay in the local cache unless explicitly pruned.

---

## 8. Pre-deploy checklist (before the first real ingest run)

The platform team should be able to tick all of these off:

- [ ] Docker installed on otter, `docker compose` works
- [ ] `/mnt/registry/` mounted, contains valid `compiled_plan.yml` and `group_gid_map.yml`
- [ ] `/var/lib/ingstr/triggers/` exists, world-readable
- [ ] Ollama container running at the configured endpoint, `nomic-embed-text` model pulled (or whatever `embedding.model` is set to)
- [ ] Per-org Qdrant container running, collection `documents` exists with vector size 768
- [ ] Payload indexes on `source_path` and `classification_group` present in the collection
- [ ] `/etc/ingstr/<org>/{config.yml,compose.yml,secrets.env}` deployed; `secrets.env` mode 0600
- [ ] Org's data NFS share is mountable from otter (verify with `showmount -e <ORG_DATA_NFS_ADDR>`)
- [ ] NFS export preserves file GIDs (no squashing); GIDs match `group_gid_map.yml`
- [ ] systemd `.path` and `.service` units installed and enabled
- [ ] Upstream sync wired to write per-org sentinel to `/var/lib/ingstr/triggers/<org>/last_sync`
- [ ] `docker compose run --rm ingstr health` shows all `[OK]` lines

---

## 9. Verification — smoke test sequence

Run these in order. Each rules out a different failure mode.

```bash
ORG=arc
cd /etc/ingstr/${ORG}

# A. Image is sound (no mounts needed):
docker run --rm ghcr.io/jobcpf/ingstr-app:${INGSTR_VERSION} version
docker run --rm ghcr.io/jobcpf/ingstr-app:${INGSTR_VERSION} --help

# B. Mounts + entrypoint group-perm drop + all four upstream checks:
docker compose --env-file secrets.env run --rm ingstr health

# Expected output (all OK):
#   [OK]   source.root          /mnt/data
#   [OK]   plan                 8 groups
#   [OK]   ollama               http://172.16.32.32:11434 model=nomic-embed-text
#   [OK]   qdrant.connect       http://qdrant_arc:6333
#   [OK]   qdrant.collection    documents (dim=768)

# C. Plan-but-don't-write — exercises pipeline without writing:
docker compose --env-file secrets.env run --rm ingstr ingest --dry-run

# D. Real ingest. Start with a small subset by temporarily pointing
#    config.yml:source.root at a small subdirectory of the org's data
#    if you can:
docker compose --env-file secrets.env run --rm ingstr ingest

# E. Counts after ingest:
docker compose --env-file secrets.env run --rm ingstr stats
```

Failure modes to expect:

| Health check fail | Root cause | Fix |
|---|---|---|
| `source.root FAIL` | NFS volume didn't mount | Check `ORG_DATA_NFS_*` in secrets.env; verify NFS server allows otter |
| `plan FAIL: required_groups` | `group_gid_map.yml` is stale or has groups not in `compiled_plan.yml` | Regenerate the map upstream (`export_group_gids.yml`) |
| `ollama FAIL` | Endpoint unreachable, or configured model not pulled | Check Ollama container status; `ollama pull nomic-embed-text` |
| `qdrant.connect FAIL` | URL/network/key wrong | Check `qdrant.url` and `QDRANT_RW_API_KEY` |
| `qdrant.collection FAIL: does not exist` | Bootstrap step skipped | Run the `curl -X PUT .../collections/documents` step in §6.3 |
| `qdrant.collection FAIL: vector size N != 768` | Collection size doesn't match `embedding.vector_dim` | Recreate collection with correct size, or change config |

If `health` passes but `ingest` fails with `UnclassifiableFile: gid <N> not present in group_gid_map.yml`, the NFS export is squashing GIDs or the file's group isn't in the upstream plan. Both are upstream issues.

---

## 10. Known gotchas and rough edges

- **Trigger sentinel placement** is a real coordination point with whoever owns the upstream rsync. The current `ingstr-ingest.path` template watches a host-visible path (`/var/lib/ingstr/triggers/<org>/last_sync`); if upstream still writes a sentinel into the org data tree, you'll either need to change the upstream (preferred) or write a small bridge script that watches the upstream sentinel via NFS-on-host and touches the host-side one. Avoid the bridge if possible — it's another moving part.
- **Multi-org on one host** is handled via Compose project names: putting each org's compose.yml under its own directory (`/etc/ingstr/<org>/`) means Compose prefixes volume names with the directory name, giving automatic per-org isolation. Verify this by running two stacks side-by-side and confirming `docker volume ls` shows distinct `<org>_ingstr-state` volumes.
- **Image visibility**: the GHCR package is currently public. If you make it private, otter needs `docker login ghcr.io` before pulls work. Use a `read:packages`-only PAT, store under root's docker config, not in `secrets.env`.
- **`assert_collection_exists` was renamed to `verify_collection`** in the Python API to avoid a `unittest.mock` deny-list collision. Mentioned only because if someone reads old commits and wonders, that's the rename. Production code only ever uses `verify_collection`.
- **Qdrant client version sensitivity**: the Ingstr code is pinned at `qdrant-client>=1.9,<2.0`. If the Qdrant *server* version drifts far ahead of the client, gRPC compatibility may bite. Stick to a 1.x server unless we coordinate a client bump.
- **The Dockerfile installs `unstructured` without `[local-inference]`** to keep the image lean. PDF parsing is rule-based, not OCR. If you discover a PDF that doesn't extract well, the answer is to file an upstream issue, not change the Ingstr image — an OCR-capable variant would 4×+ the image size.
- **Health check is best-effort.** It tries to verify connectivity but doesn't probe Qdrant write permission directly; a Qdrant key with read-only access would pass `health` but fail `ingest`. If you suspect this, do a `dry_run` first.

---

## 11. Open questions for the platform team

- Is the upstream rsync going to write a host-side sentinel, or do we need a bridge?
- Does each org get its own NFS server, or is there one server with per-org exports? (Affects how `ORG_DATA_NFS_*` is templated — could be one var per server with the device path varying.)
- Is otter the only host running Ingstr, or do we want hot-standby on another host? If standby, the state DB volume becomes a coordination problem (don't run two ingests at once for the same org).
- What's the operational expectation for ingest cadence? Per-sync (event-driven via the .path unit) is simplest. Hourly cron is also fine. Continuous polling is not — Ingstr is a one-shot.
- Does Ingstr need to expose any metrics (Prometheus, OpenTelemetry)? Brief §11 only says structured JSON logs. If we need metrics, that's a v0.2 addition.

---

## 12. Quick reference — environment variables and where they're consumed

| Variable | Set in | Read by | Required? |
|---|---|---|---|
| `INGSTR_VERSION` | `secrets.env` | compose.yml (image tag) | Optional, defaults to `latest` |
| `QDRANT_RW_API_KEY` | `secrets.env` | compose.yml → container env → Ingstr config (`qdrant.api_key_env`) | **Required** |
| `ORG_DATA_NFS_ADDR` | `secrets.env` | compose.yml volume driver_opts | **Required** |
| `ORG_DATA_NFS_DEVICE` | `secrets.env` | compose.yml volume driver_opts | **Required** |
| `ORG_DATA_NFS_OPTIONS` | `secrets.env` | compose.yml volume driver_opts | Optional, default `soft,ro,nolock` |
| `INGSTR_CONFIG` | compose.yml `environment:` | Ingstr CLI default config path | Already set in compose example |

`config.yml` itself contains no secrets and can be world-readable. Only `secrets.env` needs mode 0600.

---

## 13. Reference — files in this repo

| Path | What it is |
|---|---|
| [`Dockerfile`](../Dockerfile) | Image build — multi-stage, runs as root then drops to `ingstr` user |
| [`docker-entrypoint.sh`](../docker-entrypoint.sh) | Reads `group_gid_map.yml`, sets supplementary GIDs, drops privileges via `setpriv` |
| [`config.example.yml`](../config.example.yml) | Template for `/etc/ingstr/<org>/config.yml` |
| [`deploy/compose.example.yml`](compose.example.yml) | Template for `/etc/ingstr/<org>/compose.yml` |
| [`deploy/systemd/ingstr-ingest.service`](systemd/ingstr-ingest.service) | Systemd service unit template |
| [`deploy/systemd/ingstr-ingest.path`](systemd/ingstr-ingest.path) | Systemd path watcher unit template |
| [`deploy/README.md`](README.md) | Operator walkthrough (manual deploy version of the steps above) |
| [`.github/workflows/release.yml`](../.github/workflows/release.yml) | GHCR build + publish on tag push |
| [`src/ingstr/`](../src/ingstr/) | Application code (Python) |
| [`tests/`](../tests/) | Unit tests + integration test placeholder |
| [`../ingstr-brief-v0.1.md`](../../ingstr-brief-v0.1.md) | Original build brief — the source of truth for application contracts |

---

## 14. How to extend this document

Treat as a living doc. When platform development surfaces a new variable, gotcha, or boundary, edit this file in a branch and PR it. Sections worth keeping in sync as deployment matures:

- §3 (current state) when a new tag ships or a new feature lands
- §6 (per-org variables) when Ansible adds or renames a variable
- §8 (checklist) when a new pre-deploy requirement appears
- §10 (gotchas) every time someone hits a new operational rough edge — this is the "how we found out" log
- §11 (open questions) — close them as decisions are made

If a question recurs across multiple platform engineers, that's a signal to promote it from §11 into prose elsewhere.
