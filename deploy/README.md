# Deploying Ingstr on otter

End-to-end walkthrough: tag a release on GitHub, let CI publish the image to GHCR, pull on otter, run a smoke test, then wire up systemd triggering.

Assumes:
- Qdrant and Ollama are already running on otter, exposed on a shared Docker network (`rag-net` in the example — change to whatever Ansible created).
- `compiled_plan.yml` and `group_gid_map.yml` are mounted at `/mnt/registry/` on the host.
- The source data tree is mounted at `/mnt/raid_arc/` (NFS, read-only).

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

Install the operator-managed pieces on otter at `/etc/ingstr/`:

```bash
sudo mkdir -p /etc/ingstr
sudo cp config.example.yml /etc/ingstr/config.yml      # then edit
sudo cp deploy/compose.example.yml /etc/ingstr/compose.yml
echo "QDRANT_RW_API_KEY=..." | sudo tee /etc/ingstr/secrets.env
sudo chmod 0600 /etc/ingstr/secrets.env
```

Edit `/etc/ingstr/config.yml` with the real paths and the Qdrant collection name. The `compose.yml` reads `QDRANT_RW_API_KEY` from `/etc/ingstr/secrets.env` via `--env-file` (see step 5).

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
cd /etc/ingstr

# Connectivity to qdrant + ollama + NFS + plan.
docker compose --env-file /etc/ingstr/secrets.env run --rm ingstr health

# What would change without writing.
docker compose --env-file /etc/ingstr/secrets.env run --rm ingstr ingest --dry-run

# Real run.
docker compose --env-file /etc/ingstr/secrets.env run --rm ingstr ingest

# Counts and last-run timestamps.
docker compose --env-file /etc/ingstr/secrets.env run --rm ingstr stats
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

The `.path` unit watches `/mnt/raid_arc/.last_sync`. When something upstream (e.g. an `rsync` from the source host) touches that file, the `.service` runs `docker compose run ingstr ingest` and exits.

Verify:

```bash
systemctl status ingstr-ingest.path
journalctl -u ingstr-ingest.service -f
sudo touch /mnt/raid_arc/.last_sync     # manual trigger
```

---

## How the dynamic group permissions work

Files on `/mnt/raid_arc/` are mode 0640 owned by canonical groups (`arc_g0_engineering_global`, etc.). To read them, the container process needs those GIDs as supplementary groups. Approach:

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

```bash
# On otter:
docker pull ghcr.io/jobcpf/ingstr-app:v0.2.0
sudo sed -i 's|ingstr:v0.1.0|ingstr:v0.2.0|' /etc/ingstr/compose.yml
docker compose --env-file /etc/ingstr/secrets.env run --rm ingstr health
```

Or pin to `:latest` in `compose.yml` and just `docker pull` — but explicit version tags make rollbacks trivial (`docker pull v0.1.0` and edit the file back).

---

## Troubleshooting

**"Permission denied" reading files on /mnt/raid_arc/**
The entrypoint couldn't derive supplementary GIDs. Check:
- Is `/etc/ingstr/config.yml` readable inside the container? (`docker compose run ingstr ls -la /etc/ingstr/`)
- Does `plan.group_gid_map_path` resolve to a readable file on the mount? (`docker compose run ingstr cat /mnt/registry/group_gid_map.yml`)
- Did the operator pass `--user`, bypassing the privilege-drop logic? Remove `--user` and let the entrypoint handle it.

**`PlanError: ... absent from compiled_plan.yml's required_groups`**
`group_gid_map.yml` is stale. Regenerate upstream via `export_group_gids.yml` and redeploy `/mnt/registry/group_gid_map.yml`.

**`UnclassifiableFile: gid <N> not present in group_gid_map.yml`**
A file's group ownership wasn't covered by the map. Either upstream missed creating that group, or a file was placed under the wrong group. Fix at the source — Ingstr will not classify with a default group, by design.
