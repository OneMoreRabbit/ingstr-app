# Deploying Ingstr on otter

End-to-end walkthrough: tag a release on GitHub, let CI publish the image to GHCR, pull on otter, run a smoke test, then wire up systemd triggering.

## Topology (per-org isolation)

One Ingstr deploy = one organisation. Multiple orgs share Ollama and the registry; everything else is per-org.

| Resource | Scope | Where it lives |
|---|---|---|
| **Ollama** | Shared | One container on otter, addressed by LAN IP from each org's Ingstr |
| **Qdrant** | Per-org | Per-org container with a per-org write API key (managed by Ansible) |
| **Registry** (`compiled_plan.yml`, `group_gid_map.yml`) | Shared | NFS-mounted on otter at `/mnt/registry/`, bind-mounted into each container |
| **Org data** | Per-org | NFS-mounted **inside the container** via a Docker NFS volume — otter's host filesystem never sees the org's data |
| **Config** (`config.yml`, `compose.yml`, `secrets.env`) | Per-org | Host-side at `/etc/ingstr/<org>/`, deployed by Ansible |
| **State DB** | Per-org | Per-org named Docker volume; project-name prefix in compose gives automatic isolation |

The "data NFS-in-container" pattern is the load-bearing decision: by mounting org data only inside the container, otter's host operator never has filesystem access to org documents. Compose handles this natively via a `local` volume with the `nfs` driver — see [compose.example.yml](compose.example.yml).

---

## 1. One-time GitHub setup

Push the repo to GitHub:

```bash
cd /path/to/ingstr-app
git remote add origin git@github.com:jobcpf/ingstr-app.git
git push -u origin main
```

The release workflow (`.github/workflows/release.yml`) is triggered by tags matching `v*`. It uses the built-in `GITHUB_TOKEN` and pushes to `ghcr.io/jobcpf/ingstr-app`. No PAT required.

**Image visibility.** GHCR images are private by default. Make the package public if you want unauthenticated pulls on otter:

1. After the first successful build, go to https://github.com/users/jobcpf/packages/container/ingstr-app/settings
2. *Danger zone* → *Change visibility* → *Public*

If you keep it private, otter needs to authenticate before pulling — see step 4.

---

## 2. Tag a release

```bash
git tag v0.1.0
git push --tags
```

Watch the run at https://github.com/jobcpf/ingstr-app/actions. On success the image will exist as:

- `ghcr.io/jobcpf/ingstr-app:v0.1.0`
- `ghcr.io/jobcpf/ingstr-app:0.1.0`
- `ghcr.io/jobcpf/ingstr-app:0.1`
- `ghcr.io/jobcpf/ingstr-app:latest` (only if the tag is on the default branch)

---

## 3. Prepare otter

Install the operator-managed pieces on otter under `/etc/ingstr/<org>/` (one directory per org):

```bash
ORG=arc
sudo mkdir -p /etc/ingstr/${ORG}
sudo cp config.example.yml         /etc/ingstr/${ORG}/config.yml
sudo cp deploy/compose.example.yml /etc/ingstr/${ORG}/compose.yml

# Per-org secrets — Qdrant write key + NFS mount details for the org's data share.
sudo tee /etc/ingstr/${ORG}/secrets.env > /dev/null <<EOF
QDRANT_RW_API_KEY=...
ORG_DATA_NFS_ADDR=10.0.0.10
ORG_DATA_NFS_DEVICE=:/exports/${ORG}/drive
INGSTR_VERSION=v0.1.0-beta
EOF
sudo chmod 0600 /etc/ingstr/${ORG}/secrets.env
```

Edit `/etc/ingstr/${ORG}/config.yml` with the real Qdrant URL and collection name; defaults already match the standard mount points.

The shared registry is host-mounted on otter once at `/mnt/registry/` and bind-mounted into every org's container — no per-org config needed there.

---

## 4. Pull the image (private package only)

If the GHCR package is public, skip this. Otherwise on otter:

```bash
# Generate a classic PAT with `read:packages` scope at
# https://github.com/settings/tokens, then:
echo "$GHCR_PAT" | docker login ghcr.io -u jobcpf --password-stdin

docker pull ghcr.io/jobcpf/ingstr-app:v0.1.0
```

For unattended pulls, store the PAT under root's `~/.docker/config.json` or a credential helper. Do **not** put it in `secrets.env` — that file is for runtime env vars passed into the container, not docker daemon auth.

---

## 5. Smoke test

```bash
ORG=arc
cd /etc/ingstr/${ORG}

# Connectivity to qdrant + ollama + NFS + plan.
docker compose --env-file secrets.env run --rm ingstr health

# What would change without writing.
docker compose --env-file secrets.env run --rm ingstr ingest --dry-run

# Real run.
docker compose --env-file secrets.env run --rm ingstr ingest

# Counts and last-run timestamps.
docker compose --env-file secrets.env run --rm ingstr stats
```

Logs are JSON to stdout, captured by docker / journald.

---

## 6. Wire up systemd triggering

Copy the unit files and enable the path watcher:

```bash
sudo cp deploy/systemd/ingstr-ingest.{service,path} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ingstr-ingest.path
```

The `.path` unit watches a host-visible sentinel file. Because org data lives only inside the container (NFS mounted by Docker, invisible to the host), the upstream sync should write its sentinel to a host-side path like `/var/lib/ingstr/triggers/<org>/last_sync` rather than into the data tree itself. When that file changes, the `.service` runs `docker compose run ingstr ingest` and exits.

Verify:

```bash
systemctl status ingstr-ingest.path
journalctl -u ingstr-ingest.service -f
sudo touch /var/lib/ingstr/triggers/last_sync   # manual trigger
```

---

## How the dynamic group permissions work

Org data files (mode 0640, owned by canonical groups like `arc_g0_engineering_global`) are mounted inside the container via NFS. To read them, the container process needs those GIDs as supplementary groups. Approach:

1. The container starts as root.
2. `docker-entrypoint.sh` reads `${INGSTR_CONFIG}` to find the path of `group_gid_map.yml`.
3. It loads the map's `groups:` and extracts the GID set.
4. `setpriv --reuid=ingstr --regid=ingstr --groups=<gids> -- ingstr ...` drops to the non-root `ingstr` user with those GIDs added.

Consequences:

- When upstream regenerates `group_gid_map.yml`, no image rebuild is needed; the next run picks up the new map automatically.
- If you bypass the entrypoint by running with `--user 1000:1000`, you must also pass each GID via `--group-add` — the entrypoint cannot use `setpriv --groups` from a non-root process (no `CAP_SETGID`).
- Files whose GID isn't in the map are still skipped by Ingstr's own classification logic (`UnclassifiableFile`); the entrypoint just gives the process the *option* to read them. Fail-closed semantics are unchanged.

---

## Updating to a new image version

The image tag is supplied via `INGSTR_VERSION` in `secrets.env` — no compose.yml edit needed:

```bash
# On otter:
ORG=arc
docker pull ghcr.io/jobcpf/ingstr-app:v0.2.0
sudo sed -i 's|^INGSTR_VERSION=.*|INGSTR_VERSION=v0.2.0|' /etc/ingstr/${ORG}/secrets.env
docker compose --env-file /etc/ingstr/${ORG}/secrets.env run --rm ingstr health
```

Or omit `INGSTR_VERSION` entirely (the compose default is `:latest`) — but explicit version tags make rollbacks trivial.

---

## Troubleshooting

**"Permission denied" reading files in `/mnt/data/`**
The entrypoint couldn't derive supplementary GIDs. Check:
- Is `/etc/ingstr/config.yml` readable inside the container? (`docker compose run ingstr ls -la /etc/ingstr/`)
- Does `plan.group_gid_map_path` resolve to a readable file on the mount? (`docker compose run ingstr cat /mnt/registry/group_gid_map.yml`)
- Did the operator pass `--user`, bypassing the privilege-drop logic? Remove `--user` and let the entrypoint handle it.

**Org data NFS volume fails to mount**
Docker reports the NFS mount error when the container starts. Check:
- `ORG_DATA_NFS_ADDR` and `ORG_DATA_NFS_DEVICE` in `secrets.env` resolve to a real NFS export the host can reach (`showmount -e ${ORG_DATA_NFS_ADDR}` from otter)
- The NFS server allows mounts from otter's IP
- For NFSv4-only servers, append `nfsvers=4` to `ORG_DATA_NFS_OPTIONS`

**`PlanError: ... absent from compiled_plan.yml's required_groups`**
`group_gid_map.yml` is stale. Regenerate upstream via `export_group_gids.yml` and redeploy `/mnt/registry/group_gid_map.yml`.

**`UnclassifiableFile: gid <N> not present in group_gid_map.yml`**
A file's group ownership wasn't covered by the map. Either upstream missed creating that group, or a file was placed under the wrong group. Fix at the source — Ingstr will not classify with a default group, by design.
