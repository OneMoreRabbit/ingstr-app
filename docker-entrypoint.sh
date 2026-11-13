#!/usr/bin/env bash
# Ingstr container entrypoint.
#
# When started as root (the default), works out the supplementary group ids the
# run needs and re-execs the requested ingstr command as the non-root `ingstr`
# user with those groups. They have to be set before the program starts, which
# is why this is here and not in Python.
#
# Since v0.2.0 the ids are NARROWED TO THE ORG BEING READ (one-search-service
# design, 2026-11-13). The group map is one list for the whole estate, so the
# old behaviour — take every gid in it — gave a container serving one org the
# groups of every org.
#
# Two kinds of failure, deliberately not treated alike:
#
#   * A command that does not read anything (`version`, `--help`, no args)
#     needs no groups and no config. It runs, with none.
#   * A command that DOES read (`ingest`, `stats`, `health`) must have the
#     group ids. If the config or the plan cannot be read, this fails the
#     container. It must not drop to "no groups" and let the run report every
#     file as unclassifiable — that would turn an upstream fault into a
#     plausible-looking result.
#
# When started already non-root (operator passed `--user`), execs directly —
# the operator owns `--group-add` in that case.

set -euo pipefail

CFG="${INGSTR_CONFIG:-/etc/ingstr/config.yml}"

# Does this invocation read anything?
#
# NOTE THE SHAPE OF "$@". The image's ENTRYPOINT ends with the program name:
#
#   ENTRYPOINT [..., "docker-entrypoint.sh", "ingstr"]
#
# so a `docker compose run ingstr ingest --org arc` arrives here as
# `ingstr ingest --org arc` — the subcommand is the SECOND word, not the
# first. An earlier version judged the first non-option argument, decided
# "ingstr" was not a reading command, and dropped privileges with no groups
# at all: every run would have read nothing, everywhere.
#
# So this scans for a reading subcommand anywhere in the arguments rather
# than deciding on position. Found by the arch seat before the release
# (hub #13871).
needs_groups() {
    local arg
    for arg in "$@"; do
        case "$arg" in
            ingest|stats|health) return 0 ;;
        esac
    done
    return 1
}

# The org this run is for, from `--org <name>` or `--org=<name>`. Empty when
# absent: the config may list exactly one org, and then it is unambiguous.
org_from_args() {
    local prev="" arg
    for arg in "$@"; do
        case "$arg" in
            --org=*) printf '%s' "${arg#--org=}"; return 0 ;;
            *) if [ "$prev" = "--org" ]; then printf '%s' "$arg"; return 0; fi ;;
        esac
        prev="$arg"
    done
    printf ''
}

# Already non-root? Operator owns group setup; just exec.
if [ "$(id -u)" -ne 0 ]; then
    exec "$@"
fi

GIDS=""
if needs_groups "$@"; then
    ORG="$(org_from_args "$@")"
    if ! GIDS="$(python -m ingstr.supplementary_gids "$CFG" "$ORG")"; then
        echo "ingstr: refusing to start: the supplementary group ids could not be determined." >&2
        echo "ingstr: a run that cannot read the plan must not proceed with no groups." >&2
        exit 1
    fi
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
