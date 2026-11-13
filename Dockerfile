# syntax=docker/dockerfile:1.7
#
# Ingstr container image.
#
# Two-stage build:
#   builder — installs Ingstr and its Python deps into an isolated venv.
#   runtime — slim image with only the runtime system deps + the venv.
#
# Runtime stage runs an entrypoint that, when started as root, reads the
# `group_gid_map.yml` referenced by the mounted config, populates supplementary
# GIDs on the `ingstr` user, and drops privileges via `setpriv`. The image
# therefore needs no rebuild when groups are added/removed upstream — only the
# `group_gid_map.yml` mount changes.
# ────────────────────────────────────────────────────────────────────────────

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build-time deps (some unstructured transitives may need a C toolchain).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml constraints.txt README.md LICENSE ./
COPY src ./src

# Install into an isolated venv that the runtime stage will copy verbatim.
#
# -c constraints.txt pins every transitive version. pyproject.toml's ranges
# stay as the compatibility statement; this decides what is actually
# installed, so the same source builds the same image on any day. Without it
# v0.1.1 picked up a numba that needs a writable cache directory while v0.1.0
# had not — a change nobody made, which took out every PDF in the ARC restore.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install . -c constraints.txt

# Pre-download NLP models that `unstructured` lazy-loads at parse time.
# Doing this at build time (as root) bakes the data into /opt/venv so the
# runtime container — which drops privileges to a non-root `ingstr` user
# via setpriv — does not try to write into the venv at parse time.
#
# spaCy and NLTK are direct deps in pyproject.toml (not transitive via
# unstructured's per-format extras), so they're already installed by the
# `pip install .` step above.
#
# spaCy: en_core_web_sm is installed as a pip-style package under the
# venv and gets copied with the rest of /opt/venv to the runtime stage.
RUN /opt/venv/bin/python -m spacy download en_core_web_sm

# NLTK: data files go to /opt/venv/share/nltk_data so they ship with the
# venv copy. NLTK_DATA env var must be set in BOTH stages for it to find
# them. We use a separate script so per-package failures print diagnostic
# output to the build log rather than dying silently inside a list comp.
ENV NLTK_DATA=/opt/venv/share/nltk_data
COPY scripts/download_nlp_data.py /tmp/download_nlp_data.py
RUN mkdir -p /opt/venv/share/nltk_data \
 && /opt/venv/bin/python /tmp/download_nlp_data.py \
 && rm /tmp/download_nlp_data.py

# ────────────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

# The entrypoint drops privileges with setpriv, which changes the uid and the
# groups and NOT the environment — so HOME stayed /root while the process ran
# as `ingstr`, and every library that caches under HOME tried to write into a
# directory it cannot. That is not a theory: it took out all 339 PDFs in the
# ARC restore on 2026-11-13. numba (via unstructured's PDF layout analysis)
# failed with "cannot cache function 'projection_by_bboxes': no locator
# available", which names neither HOME nor permissions, so the cause is not
# visible from the error.
#
# These are set in the IMAGE, not in a deployment, because they are a property
# of how this image drops privileges. A deployment that has to know about
# numba to parse a PDF is a deployment that will one day not know.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    INGSTR_CONFIG=/etc/ingstr/config.yml \
    NLTK_DATA=/opt/venv/share/nltk_data \
    HOME=/home/ingstr \
    XDG_CACHE_HOME=/home/ingstr/.cache \
    NUMBA_CACHE_DIR=/home/ingstr/.cache/numba \
    MPLCONFIGDIR=/home/ingstr/.cache/matplotlib

# Runtime system deps:
#   libmagic1       — unstructured uses libmagic for file-type sniffing
#   poppler-utils   — PDF text extraction (pdftotext)
#   libgl1          — provides libGL.so.1 for unstructured's PDF layout-
#                     analysis path on graphics-heavy PDFs (vector graphics,
#                     embedded images). Without it: "libGL.so.1: cannot open
#                     shared object file" at parse time. Pure-text PDFs
#                     don't hit this code path; flyers and design-heavy
#                     documents do. NOT related to [local-inference] OCR —
#                     just basic layout detection.
#   libglib2.0-0    — common companion to libgl1; some unstructured paths
#                     also reach for glib via PIL/pillow.
#   util-linux      — provides setpriv (used by entrypoint to drop privileges)
#   tini            — PID 1 / signal handling for long-running ingest jobs
#   ca-certificates — for httpx → Ollama / qdrant-client over TLS
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libmagic1 \
        poppler-utils \
        libgl1 \
        libglib2.0-0 \
        util-linux \
        tini \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Non-root user. Supplementary group memberships are added at runtime.
ARG INGSTR_UID=1000
ARG INGSTR_GID=1000
RUN groupadd --gid ${INGSTR_GID} ingstr \
 && useradd --uid ${INGSTR_UID} --gid ${INGSTR_GID} \
            --home-dir /home/ingstr --create-home \
            --shell /usr/sbin/nologin \
            ingstr

# Copy the venv from the builder stage.
COPY --from=builder /opt/venv /opt/venv

# Pre-create the state dir so a named volume mount inherits correct ownership.
RUN mkdir -p /var/lib/ingstr \
 && chown -R ingstr:ingstr /var/lib/ingstr

# The cache directories named in ENV above, owned by the user that will write
# them. Created here rather than left to the libraries: a library that cannot
# create its cache directory usually reports the failure as something else.
RUN mkdir -p /home/ingstr/.cache/numba /home/ingstr/.cache/matplotlib \
 && chown -R ingstr:ingstr /home/ingstr

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Prove, at build time and AS THE DROPPED USER, that a PDF parses.
#
# This is the test that would have caught the 2026-11-13 failure, and it is in
# the image rather than in CI on purpose: the fault was not in the code, it
# was in what the environment let the code write, so only something running in
# the built image as `ingstr` can see it. A build that cannot parse a PDF now
# fails instead of shipping.
#
# The fixture is generated here rather than committed: a few hundred bytes of
# PDF that nobody has to review, and it exercises the real partition path.
COPY scripts/image-selftest.py /usr/local/lib/ingstr-image-selftest.py
RUN set -eu; \
    install -d -o ingstr -g ingstr /tmp/ingstr-selftest; \
    setpriv --reuid=ingstr --regid=ingstr --clear-groups \
        /opt/venv/bin/python /usr/local/lib/ingstr-image-selftest.py; \
    rm -rf /tmp/ingstr-selftest

# tini reaps zombies and forwards signals; entrypoint handles privilege drop;
# `ingstr` is the fixed first arg so callers pass only the subcommand:
#   docker run ingstr ingest --dry-run
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh", "ingstr"]
CMD ["--help"]
