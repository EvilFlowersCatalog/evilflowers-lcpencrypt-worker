# syntax=docker/dockerfile:1

# Readium lcpencrypt, pinned. Bump deliberately: release notes at
# https://github.com/readium/readium-lcp-server/releases
ARG LCPENCRYPT_VERSION=v1.14.0

# Builder and runtime share the Debian release: since v1.14.0 lcpencrypt links MuPDF through
# go-fitz (cgo) to read PDF metadata, so the binary needs the runtime's glibc.
FROM golang:1.26-trixie AS readium
ARG LCPENCRYPT_VERSION
ENV CGO_ENABLED=1
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go install -trimpath -ldflags="-s -w" "github.com/readium/readium-lcp-server/lcpencrypt@${LCPENCRYPT_VERSION}"

# The image installs exactly what poetry.lock pins (not whatever pyproject's ranges resolve to today).
# The export is platform-independent: run it natively, not under QEMU for each target platform.
FROM --platform=$BUILDPLATFORM python:3.14-slim-trixie AS requirements
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --root-user-action=ignore "poetry>=2,<3" poetry-plugin-export
WORKDIR /src
COPY pyproject.toml poetry.lock ./
RUN poetry export --format requirements.txt --extras observability --output /requirements.txt

FROM python:3.14-slim-trixie
ARG LCPENCRYPT_VERSION

LABEL org.opencontainers.image.title="evilflowers-lcpencrypt-worker" \
      org.opencontainers.image.description="Celery worker wrapping Readium lcpencrypt" \
      org.opencontainers.image.source="https://github.com/EvilFlowersCatalog/evilflowers-lcpencrypt-worker" \
      io.readium.lcpencrypt.version="${LCPENCRYPT_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    READIUM_LCPENCRYPT_BIN=/usr/local/bin/lcpencrypt \
    READIUM_LCPENCRYPT_VERSION=${LCPENCRYPT_VERSION} \
    # One JSON object per line on stdout: Alloy reads `level` and `trace_id` from it.
    LOG_FORMAT=json \
    CELERY_CONCURRENCY=2 \
    CELERY_MAX_TASKS_PER_CHILD=100 \
    CELERY_LOG_LEVEL=INFO

WORKDIR /usr/local/src

COPY --from=readium /go/bin/lcpencrypt /usr/local/bin/lcpencrypt
# Fail the build, not the first task, if the binary cannot load in this image.
RUN lcpencrypt -h >/dev/null 2>&1 || lcpencrypt --help 2>&1 | grep -qi usage

COPY --from=requirements /requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --root-user-action=ignore --require-hashes -r /tmp/requirements.txt

COPY pyproject.toml README.md ./
COPY evilflowers_lcpencrypt_worker/ evilflowers_lcpencrypt_worker/
RUN pip install --root-user-action=ignore --no-deps --no-cache-dir .

# Docker/Compose only (Kubernetes ignores HEALTHCHECK). Pings this worker, not the whole cluster.
HEALTHCHECK --interval=60s --timeout=15s --start-period=20s --retries=3 \
    CMD celery -A evilflowers_lcpencrypt_worker inspect ping -d "lcpencrypt@$(hostname)" --timeout 10 || exit 1

# `exec`: Celery becomes PID 1 and receives SIGTERM, so it finishes the running task (acks_late)
# and flushes telemetry within the pod's grace period. `-E` keeps task events for Flower.
ENTRYPOINT ["sh", "-c", "exec celery -A evilflowers_lcpencrypt_worker worker \
    -Q evilflowers_lcpencrypt_worker \
    --concurrency=${CELERY_CONCURRENCY} \
    --max-tasks-per-child=${CELERY_MAX_TASKS_PER_CHILD} \
    --loglevel=${CELERY_LOG_LEVEL} \
    --hostname=lcpencrypt@%h \
    --without-mingle \
    --without-gossip \
    -E"]
