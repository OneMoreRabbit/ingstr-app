"""The supplementary group ids one run needs, for the container entrypoint.

The entrypoint drops privileges before `ingstr` starts, so the group ids have
to be known before the program runs. Until v0.2.0 it took **every** gid in
`group_gid_map.yml` — which is one list for the whole estate, so a container
serving one org held the groups of every org. v0.2.0 narrows it to the org
being read, using the `groups[]` section of `compiled-rbac-plan` 0.11.

Why this is a module rather than more shell in the entrypoint: it is the part
that decides what the process can read, it has real failure modes, and the
entrypoint's own error handling is deliberately forgiving so that
`ingstr version` works without a config. Forgiving is right for *not needing*
the map and wrong for *failing to read* it — a plan that cannot be parsed
must stop the run, not quietly yield an empty list and a run that then calls
every file unclassifiable. Separating the two is the whole point:

    exit 0  — the gids are on stdout (possibly empty, see below)
    exit 1  — something needed could not be read; the message is on stderr
              and the entrypoint must fail the container

An empty list on exit 0 is legitimate and means "this org has no groups":
the run will read nothing it does not own, which is a true answer, and
`ingstr health` reports it as such.
"""

import sys
from pathlib import Path

from .config import load_config
from .exceptions import ConfigError, PlanError
from .plan import load_plan


def gids_for_run(config_path: Path, org: str | None) -> list[int]:
    """The sorted gids this run should hold, narrowed to its own org.

    A plan without `groups[]` raises: see `plan._load_group_orgs`. There is
    deliberately no fallback to every gid in the map — that is the behaviour
    this release exists to end, and a fallback is exactly what nobody would
    have noticed.
    """
    cfg = load_config(config_path)
    view = cfg.for_org(org)
    plan = load_plan(view.plan.compiled_plan_path, view.plan.group_gid_map_path)
    return sorted(plan.gids_for_org(view.org))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m ingstr.supplementary_gids <config> [org]", file=sys.stderr)
        return 1
    config_path = Path(args[0])
    org = args[1] if len(args) > 1 and args[1] else None
    try:
        gids = gids_for_run(config_path, org)
    except (ConfigError, PlanError) as e:
        print(f"cannot determine supplementary group ids: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — the entrypoint needs a reason, not a traceback
        print(f"cannot determine supplementary group ids: {e!r}", file=sys.stderr)
        return 1
    print(",".join(str(g) for g in gids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
