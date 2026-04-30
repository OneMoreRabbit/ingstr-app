"""Build-time helper: pre-download NLP data into the venv as root.

Runs in the Dockerfile builder stage so the data is owned by root and
baked into /opt/venv/share/nltk_data — accessible (read-only) to the
non-root `ingstr` user the runtime stage drops to via setpriv.

Tolerates per-package failures (NLTK package availability varies across
versions, e.g. `punkt_tab` is newer than `punkt`) but fails the build if
no packages downloaded — that's a sign of a network/index issue worth
catching at build time rather than runtime.

Diagnostics: prints status per package so the GHA build log shows which
specific package failed and why, rather than just "exit 1".
"""

from __future__ import annotations

import sys

import nltk

DOWNLOAD_DIR = "/opt/venv/share/nltk_data"
PACKAGES = ["punkt", "punkt_tab", "averaged_perceptron_tagger"]


def main() -> int:
    print(f"NLTK version: {nltk.__version__}")
    print(f"Downloading to {DOWNLOAD_DIR}")

    ok = 0
    for pkg in PACKAGES:
        try:
            result = nltk.download(pkg, download_dir=DOWNLOAD_DIR, quiet=True)
        except Exception as e:
            print(
                f"  {pkg}: ERROR {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            continue
        if result:
            print(f"  {pkg}: OK")
            ok += 1
        else:
            print(f"  {pkg}: NOT FOUND in index (skipping)")

    print(f"NLTK setup: {ok}/{len(PACKAGES)} packages downloaded")
    if ok == 0:
        print("FATAL: no NLTK packages downloaded", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
