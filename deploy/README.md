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
| **Config** (`config.yml`, `compose.yml`, `secrets.env`) | Per-org | One host directory per org — on otter today `~/docker/ingstr/<org>/`, measured 2026-11-12. `/etc/ingstr` is the path **inside** the container, the far end of the bind |
| **State DB** | Per-org | Per-org named Docker volume; project-name prefix in compose gives automatic isolation |

The "data NFS-in-container" pattern is the load-bearing decision: by mounting org data only inside the container, otter's host operator never has filesystem access to org documents. Compose handles this natively via a `local` volume with the `nfs` driver — see [compose.example.yml](compose.example.yml).

---

## 1. One-time GitHub setup

Push the repo to GitHub:

```bash
cd /path/to/ingstr-app
git remote add origin git@github.com:OneMoreRabbit/ingstr-app.git
git push -u origin main
```

The release workflow (`.github/workflows/release.yml`) is triggered by tags matching `v*`. It uses the built-in `GITHUB_TOKEN` and pushes to `ghcr.io/onemorerabbit/ingstr-app`. No PAT required.

**Image visibility.** GHCR images are private by default. Make the package public if you want unauthenticated pulls on otter:

1. After the first successful build, go to https://github.com/orgs/OneMoreRabbit/packages/container/ingstr-app/settings
2. *Danger zone* → *Change visibility* → *Public*

If you keep it private, otter needs to authenticate before pulling — see step 4.

---

## 2. Tag a release

```bash
git tag v0.1.0
git push --tags
```

Watch the run at https://github.com/OneMoreRabbit/ingstr-app/actions. On success the image will exist as:

- `ghcr.io/onemorerabbit/ingstr-app:v0.1.0`
- `ghcr.io/onemorerabbit/ingstr-app:0.1.0`
- `ghcr.io/onemorerabbit/ingstr-app:0.1`
- `ghcr.io/onemorerabbit/ingstr-app:latest` (only if the tag is on the default branch)

---

## 3. Prepare otter

Install the operator-managed pieces on otter in **one directory per org**. On otter
today that is `~/docker/ingstr/<org>/`, owned by the `otter` user (measured
2026-11-12); `/etc/ingstr/` on the host is empty and is **not** where these go — it is
the path inside the container, the far end of compose's `.:/etc/ingstr:ro` bind.

What ingstr requires is only that the three files sit together in one directory per
org: `compose.yml` binds its own directory into the container, so `config.yml` must be
beside it, and the directory name becomes the compose project name, which is what gives
each org its own state volume. Which parent directory holds them is the estate's choice.

```bash
ORG=arc
INGSTR_DIR=~/docker/ingstr/${ORG}      # the estate's layout on otter today
mkdir -p ${INGSTR_DIR}
cp config.example.yml         ${INGSTR_DIR}/config.yml
cp deploy/compose.example.yml ${INGSTR_DIR}/compose.yml

# Per-org secrets — Qdrant write key + NFS mount details for the org's data share.
tee ${INGSTR_DIR}/secrets.env > /dev/null <<EOF
QDRANT_RW_API_KEY=...
ORG_DATA_NFS_ADDR=10.0.0.10
ORG_DATA_NFS_DEVICE=:/exports/${ORG}/drive
INGSTR_VERSION=v0.1.0
EOF
chmod 0600 ${INGSTR_DIR}/secrets.env
```

Edit `${INGSTR_DIR}/config.yml` with the real Qdrant URL and collection name; defaults already match the standard mount points.

The shared registry is host-mounted on otter once at `/mnt/registry/` and bind-mounted into every org's container — no per-org config needed there.

---

## 4. Pull the image

> **Measured 2026-11-12. The image is there and `docker pull` works — but a
> hand-rolled `curl` check will tell you it is missing unless you send the right
> `Accept` header.**
>
> A bare request with no token returns `401`. That is the registry's normal
> first answer, not a private package: ask for a token first. Then the header
> decides the answer, because this image is an **OCI** manifest and GHCR will
> not serve it to a client that has not said it accepts one:
>
> ```bash
> TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:onemorerabbit/ingstr-app:pull" \
>   | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
> curl -s -H "Authorization: Bearer $TOKEN" \
>   -H "Accept: application/vnd.oci.image.manifest.v1+json" \
>   https://ghcr.io/v2/onemorerabbit/ingstr-app/manifests/v0.1.0
> ```
>
> That returns `200`, `mediaType: application/vnd.oci.image.manifest.v1+json`,
> ten layers. The same request with `*/*`, with no `Accept`, or with either
> docker manifest type returns **`404`** — and the body says exactly why:
>
> ```json
> {"errors":[{"code":"MANIFEST_UNKNOWN","message":"OCI manifest found, but Accept header does not support OCI manifests"}]}
> ```
>
> **A `404` without that header is not a missing image.** `docker pull` sends
> all four types and is unaffected; only hand-rolled checks see it. If you check
> this by hand, read the response body — the status code alone is what made an
> earlier version of this page claim the package was unreachable.
>
> The old `ghcr.io/jobcpf/ingstr-app` holds only pre-release tags — `…-rc4` and
> earlier, no `v0.1.0`. It answers on any header because those are docker v2
> manifests, which is why it makes a misleading control. Namespace ruling
> `agenteco-needs-ghcr-namespace`: published images go to
> `ghcr.io/onemorerabbit/`, never a personal namespace.
>
> **`latest` exists in the org package and must not be used.** It is the same
> digest as `v0.1.0` today and carries no promise of staying so. The tag decides
> what code runs, so it is never left to a default: `compose.example.yml` fails
> the run if `INGSTR_VERSION` is unset, deliberately. Pin an exact tag.

If an anonymous pull works, skip this. Otherwise on otter:

```bash
# Generate a classic PAT with `read:packages` scope at
# https://github.com/settings/tokens, then:
echo "$GHCR_PAT" | docker login ghcr.io -u "$GITHUB_USERNAME" --password-stdin

docker pull ghcr.io/onemorerabbit/ingstr-app:v0.1.0
```

For unattended pulls, store the PAT under root's `~/.docker/config.json` or a credential helper. Do **not** put it in `secrets.env` — that file is for runtime env vars passed into the container, not docker daemon auth.

---

## 5. Smoke test

```bash
ORG=arc
cd ~/docker/ingstr/${ORG}      # wherever the org's directory is

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

The units are **templates, one instance per org** — `ingstr-ingest@<org>`. The org
is not optional: each org has its own directory holding `compose.yml`, `config.yml`
and `secrets.env` together, because `compose.yml` binds its own directory to
`/etc/ingstr` **inside the container** (so `config.yml` must sit beside it) and the
compose project name — the directory name — is what gives each org a separate state
volume. A unit pointing at the parent directly cannot run a second org.

`/etc/ingstr` is the container path, not a host path. On otter the host directory is
`/home/otter/docker/ingstr/<org>/` (measured 2026-11-12); the example units use that.
Change `User=` and `WorkingDirectory=` if yours sits elsewhere — nothing in ingstr
depends on the parent directory.

**Two runs for one org must never overlap.** From v0.2.3 ingstr enforces that
itself with a lock beside the state database: an ordinary run gives way and
exits 0, a full run waits for `--wait-for-lock <seconds>` and exits **5** if
it never gets in. Alert on 5 — it means the nightly run did not happen.

**Do not use `Conflicts=` between the units.** It *stops* the other unit
rather than refusing the new one, so whichever starts second kills the one
already running. On otter that stopped the one nightly full run due since the restore, a
second after it started — and it would have stopped every one after it.

**What otter actually runs today is two timers per org**, not the `.path` unit:
`ingstr-ingest-<org>.timer` every five minutes and `ingstr-ingest-full-<org>.timer`
nightly at 03:00. The `.path` unit below is the sentinel-driven shape ADR-0010 §6
calls for; the two need reconciling when ARC ingest is restored.

```bash
sudo cp deploy/systemd/ingstr-ingest@.{service,path} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ingstr-ingest@arc.path
```

The `.path` unit watches a host-visible sentinel file. Because org data lives only inside the container (NFS mounted by Docker, invisible to the host), the upstream sync should write its sentinel to a host-side path like `/var/lib/ingstr/triggers/<org>/last_sync` rather than into the data tree itself. When that file changes, the `.service` runs `docker compose run ingstr ingest` and exits.

Verify:

```bash
systemctl status ingstr-ingest@arc.path
journalctl -u ingstr-ingest@arc.service -f
sudo touch /var/lib/ingstr/triggers/arc/last_sync   # manual trigger
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
INGSTR_DIR=~/docker/ingstr/${ORG}
docker pull ghcr.io/onemorerabbit/ingstr-app:v0.2.0
sed -i 's|^INGSTR_VERSION=.*|INGSTR_VERSION=v0.2.0|' ${INGSTR_DIR}/secrets.env
cd ${INGSTR_DIR} && docker compose --env-file secrets.env run --rm -T ingstr health
```

`INGSTR_VERSION` has no default and omitting it fails the run. That is deliberate: the tag decides what code runs, so it is declared, never guessed. Pin an exact tag; it also makes rollbacks trivial.

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
