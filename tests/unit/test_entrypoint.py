"""The entrypoint's two decisions, exercised through bash.

`docker-entrypoint.sh` decides (1) whether this invocation needs group ids at
all and (2) which org it is for. Both decide what the process can read, and
both are shell, so they are tested as shell rather than described in a comment
and hoped for.

**Every case here passes the arguments AS THE IMAGE PASSES THEM**, with the
leading program name, because the ENTRYPOINT ends with it:

    ENTRYPOINT [..., "docker-entrypoint.sh", "ingstr"]

The first version of these tests dropped that leading word. The tests passed
and the entrypoint was broken: it judged "ingstr" to be a non-reading command
and gave every run no groups at all. Caught by the arch seat, not by this
file (hub #13871). A test that does not call the thing the way it is called
is a test of something else.
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
        code, _ = _call("needs_groups", "ingstr", cmd, "--org", "arc")
        assert code == 0, cmd


def test_the_program_name_alone_is_not_a_reading_command() -> None:
    """The regression: `ingstr` with no subcommand prints help and reads
    nothing, so it needs no groups — but it must not make a real command look
    like it needs none either, which is what positional judging did."""
    code, _ = _call("needs_groups", "ingstr")
    assert code != 0


def test_version_and_help_need_no_groups() -> None:
    """These must run without a config, so they must not be made to depend on
    one. That is the forgiving half of the entrypoint, and it stays."""
    for args in (("ingstr", "version"), ("ingstr", "--help"), ("ingstr",), ()):
        code, _ = _call("needs_groups", *args)
        assert code != 0, args


def test_org_is_read_from_either_flag_form() -> None:
    assert _call("org_from_args", "ingstr", "ingest", "--org", "arc")[1] == "arc"
    assert _call("org_from_args", "ingstr", "ingest", "--org=job")[1] == "job"


def test_no_org_flag_gives_empty_not_a_guess() -> None:
    """Empty means "not stated": the config may list exactly one org, and the
    helper decides. The entrypoint never picks one."""
    assert _call("org_from_args", "ingstr", "ingest", "--full")[1] == ""


def test_org_flag_after_other_options_is_still_found() -> None:
    assert _call("org_from_args", "ingstr", "ingest", "--full", "--org", "arc")[1] == "arc"


def test_the_entrypoint_argument_shape_is_what_the_image_sends() -> None:
    """Pins the coupling that caused the fault: if the ENTRYPOINT stops ending
    with the program name, or starts passing something else first, these tests
    are no longer testing the real call and should fail here first."""
    dockerfile = (_ENTRYPOINT.parent / "Dockerfile").read_text()
    assert 'docker-entrypoint.sh", "ingstr"]' in dockerfile


def test_the_image_gives_the_dropped_user_somewhere_to_cache() -> None:
    """`setpriv` changes the uid and the groups, not the environment.

    Without these, HOME stays /root while the process runs as `ingstr`, and
    every library that caches under HOME fails — which is what took out all
    339 PDFs in the ARC restore on 2026-11-13, reported as "cannot cache
    function 'projection_by_bboxes': no locator available".

    The real check runs in the built image (`scripts/image-selftest.py`,
    invoked from the Dockerfile as the dropped user). This one needs no
    container and fails fast if the lines are ever dropped from the image.
    """
    dockerfile = (_ENTRYPOINT.parent / "Dockerfile").read_text()
    for var in ("HOME=/home/ingstr", "NUMBA_CACHE_DIR=", "XDG_CACHE_HOME="):
        assert var in dockerfile, var
    assert "image-selftest.py" in dockerfile


def test_the_build_pins_its_dependencies() -> None:
    """The same source built on another day was a different image, which is
    how v0.1.1 acquired a numba that needs a writable cache and v0.1.0 did
    not. The pin is only real if the build actually uses it."""
    dockerfile = (_ENTRYPOINT.parent / "Dockerfile").read_text()
    assert "-c constraints.txt" in dockerfile
    constraints = (_ENTRYPOINT.parent / "constraints.txt").read_text()
    assert "numba==" in constraints and "unstructured==" in constraints
