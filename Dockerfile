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
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# Install into an isolated venv that the runtime stage will copy verbatim.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .

# ────────────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    INGSTR_CONFIG=/etc/ingstr/config.yml

# Runtime system deps:
#   libmagic1     — unstructured uses libmagic for file-type sniffing
#   poppler-utils — PDF text extraction (pdftotext)
#   util-linux    — provides setpriv (used by entrypoint to drop privileges)
#   tini          — PID 1 / signal handling for long-running ingest jobs
#   ca-certificates — for httpx → Ollama / qdrant-client over TLS
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libmagic1 \
        poppler-utils \
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

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# tini reaps zombies and forwards signals; entrypoint handles privilege drop;
# `ingstr` is the fixed first arg so callers pass only the subcommand:
#   docker run ingstr ingest --dry-run
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh", "ingstr"]
CMD ["--help"]
