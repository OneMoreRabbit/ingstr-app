#!/usr/bin/env python
"""Prove, inside the built image and as the dropped user, that a PDF parses.

Run from the Dockerfile as `ingstr`, the user the entrypoint drops to. It
exists because of a failure that no unit test could have caught: on
2026-11-13 every one of ARC's 339 PDFs failed, not from a bug in ingstr but
because `setpriv` changes the uid and the groups and NOT the environment, so
HOME stayed `/root` while the process ran as `ingstr`. numba, reached through
unstructured's PDF layout analysis, could not write its cache and reported
"cannot cache function 'projection_by_bboxes': no locator available" — an
error that names neither HOME nor permissions.

What this checks is therefore not the parser. It is that **the environment
the image hands the dropped user lets the libraries write what they need**.
A build where that is untrue now fails.

Exits non-zero with a plain message on any failure; the build stops there.
"""

import os
import sys
import tempfile
from pathlib import Path

# A minimal one-page PDF with a line of text, built here rather than committed
# so there is no binary fixture to review or to drift.
_TEXT = b"BT /F1 24 Tf 72 700 Td (Ingstr image self test) Tj ET"


def _pdf_bytes() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(_TEXT)).encode() + b" >>\nstream\n" + _TEXT + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n".encode()
        + b"%%EOF\n"
    )
    return bytes(out)


def _fail(message: str) -> None:
    print(f"image self-test FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    # 1. The environment the image promises the dropped user.
    for var in ("HOME", "XDG_CACHE_HOME", "NUMBA_CACHE_DIR"):
        value = os.environ.get(var)
        if not value:
            _fail(f"{var} is not set; the dropped user has nowhere to cache")
        path = Path(value)
        if not os.access(path, os.W_OK):
            _fail(f"{var}={value} is not writable by uid {os.getuid()}")

    # 2. The venv must NOT be writable: parsing that writes into /opt/venv
    #    works as root at build time and fails in production.
    if os.access(Path("/opt/venv"), os.W_OK):
        _fail("/opt/venv is writable by the runtime user; it must not be")

    # 3. The exact function that failed, called directly.
    #
    #    A minimal PDF may never reach layout analysis, so parsing one is NOT
    #    on its own proof that the cache works — it would be a check of the
    #    adjacent property. numba compiles and caches on first call, so
    #    calling the cached function is what actually writes the cache.
    try:
        import numpy as np
        from unstructured.partition.utils.xycut import projection_by_bboxes
    except Exception as e:  # noqa: BLE001
        _fail(f"importing the numba-backed layout helper raised {type(e).__name__}: {e}")
    try:
        projection_by_bboxes(np.array([[0, 0, 10, 10], [20, 20, 30, 30]]), 0)
    except Exception as e:  # noqa: BLE001
        _fail(
            f"the numba-cached layout helper raised {type(e).__name__}: {e} — "
            "this is the 2026-11-13 PDF failure"
        )

    # 4. And a real parse, through the same path ingstr uses.
    from ingstr.parse import parse_file  # imported late so failures above are clearer

    with tempfile.TemporaryDirectory(dir="/tmp/ingstr-selftest") as tmp:
        pdf = Path(tmp) / "selftest.pdf"
        pdf.write_bytes(_pdf_bytes())
        try:
            elements = parse_file(pdf)
        except Exception as e:  # noqa: BLE001 — the build needs the reason, not a traceback
            _fail(f"parsing a PDF as uid {os.getuid()} raised {type(e).__name__}: {e}")
        if not elements:
            _fail("parsing a PDF produced no elements")

    # 5. The cache was actually written where we said it would be. Without
    #    this the two checks above pass on an image where numba silently
    #    falls back to not caching, which is the state that hid the fault.
    cache = Path(os.environ["NUMBA_CACHE_DIR"])
    if not any(cache.rglob("*.nbi")):
        _fail(
            f"nothing was written under NUMBA_CACHE_DIR={cache}; "
            "the cache is not being used, so the next image may fail the same way"
        )

    print(f"image self-test OK: PDF parsed as uid {os.getuid()}, HOME={os.environ['HOME']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
