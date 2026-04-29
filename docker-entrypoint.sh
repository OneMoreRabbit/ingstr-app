#!/usr/bin/env bash
# Ingstr container entrypoint.
#
# When started as root (the default), reads the configured group_gid_map.yml
# and re-execs the requested ingstr command as the non-root `ingstr` user with
# the supplementary GIDs needed to read group-restricted files on NFS.
#
# When started already non-root (operator passed `--user`), execs directly —
# the operator is responsible for `--group-add` flags in that case.
#
# Robust to commands that don't need the map (e.g. `ingstr version`,
# `ingstr --help`): if the config is unreadable, drops privileges with no
# supplementary groups instead of refusing to start.

set -euo pipefail

CFG="${INGSTR_CONFIG:-/etc/ingstr/config.yml}"

derive_supplementary_gids() {
    # Print a comma-separated GID list to stdout, or empty on any failure.
    # Failures here are non-fatal: ingstr's own validation will surface a
    # clearer error if the map is genuinely required.
    python - "$CFG" <<'PY' 2>/dev/null || true
import sys, yaml
cfg_path = sys.argv[1]
try:
    cfg = yaml.safe_load(open(cfg_path))
    map_path = cfg["plan"]["group_gid_map_path"]
    gmap = yaml.safe_load(open(map_path))
    gids = sorted({int(g) for g in gmap["groups"].values()})
    print(",".join(str(g) for g in gids))
except Exception:
    pass
PY
}

# Already non-root? Operator owns group setup; just exec.
if [ "$(id -u)" -ne 0 ]; then
    exec "$@"
fi

GIDS=""
if [ -r "$CFG" ]; then
    GIDS="$(derive_supplementary_gids)"
fi

if [ -n "$GIDS" ]; then
    exec setpriv \
        --reuid=ingstr \
        --regid=ingstr \
        --groups="$GIDS" \
        --inh-caps=-all \
        -- "$@"
else
    exec setpriv \
        --reuid=ingstr \
        --regid=ingstr \
        --clear-groups \
        --inh-caps=-all \
        -- "$@"
fi
