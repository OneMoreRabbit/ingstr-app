"""The entrypoint's two decisions, exercised through bash.

`docker-entrypoint.sh` decides (1) whether this invocation needs group ids at
all and (2) which org it is for. Both are security-relevant — they decide what
the process can read — and both are shell, so they are tested as shell rather
than described in a comment and hoped for.
"""

import subprocess
from pathlib import Path

_ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker-entrypoint.sh"


def _call(func: str, *args: str) -> tuple[int, str]:
    """Source the entrypoint's functions without running its body."""
    script = (
        f'set -e; source_only() {{ sed "/^# Already non-root/,$ d" "{_ENTRYPOINT}"; }}; '
        f'eval "$(source_only)"; {func} "$@"'
    )
    proc = subprocess.run(
        ["bash", "-c", script, "bash", *args], capture_output=True, text=True
    )
    return proc.returncode, proc.stdout.strip()


def test_reading_commands_need_groups() -> None:
    for cmd in ("ingest", "stats", "health"):
        code, _ = _call("needs_groups", cmd, "--org", "arc")
        assert code == 0, cmd


def test_version_and_help_need_no_groups() -> None:
    """These must run without a config, so they must not be made to depend on
    one. That is the forgiving half of the entrypoint, and it stays."""
    for args in (("version",), ("--help",), ()):
        code, _ = _call("needs_groups", *args)
        assert code != 0, args


def test_org_is_read_from_either_flag_form() -> None:
    assert _call("org_from_args", "ingest", "--org", "arc")[1] == "arc"
    assert _call("org_from_args", "ingest", "--org=job")[1] == "job"


def test_no_org_flag_gives_empty_not_a_guess() -> None:
    """Empty means "not stated": the config may list exactly one org, and the
    helper decides. The entrypoint never picks one."""
    assert _call("org_from_args", "ingest", "--full")[1] == ""


def test_org_flag_after_other_options_is_still_found() -> None:
    assert _call("org_from_args", "ingest", "--full", "--org", "arc")[1] == "arc"
