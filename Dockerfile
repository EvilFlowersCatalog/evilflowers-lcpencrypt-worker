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
# evilflowers-worker is a git dependency (its `master`; the export carries the commit poetry.lock resolved),
# which `pip --require-hashes` refuses: it is split
# off the export and built into a wheel here (pure Python, so one wheel serves every platform). Its own
# dependencies, the `otel` extra included, are in the hashed export like any other.
FROM --platform=$BUILDPLATFORM python:3.14-slim-trixie AS requirements
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --root-user-action=ignore "poetry>=2,<3" poetry-plugin-export
WORKDIR /src
COPY pyproject.toml poetry.lock ./
RUN poetry export --format requirements.txt --extras observability --output /tmp/all.txt \
    && grep '^evilflowers-worker @ git+' /tmp/all.txt > /tmp/vcs.txt \
    && grep -v '^evilflowers-worker @ git+' /tmp/all.txt > /requirements.txt \
    && pip wheel --no-deps --wheel-dir /wheels -r /tmp/vcs.txt

FROM python:3.14-slim-trixie
ARG LCPENCRYPT_VERSION
# CI passes the image tag: the telemetry's `service.version`.
ARG IMAGE_TAG=dev

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
    IMAGE_TAG=${IMAGE_TAG} \
    # The kit's default would be `evilflowers-lcpencrypt`; the metrics, traces and dashboards say `lcpencrypt`.
    OTEL_SERVICE_NAME=lcpencrypt \
    CELERY_CONCURRENCY=2 \
    CELERY_MAX_TASKS_PER_CHILD=100 \
    CELERY_LOG_LEVEL=INFO

WORKDIR /usr/local/src

COPY --from=readium /go/bin/lcpencrypt /usr/local/bin/lcpencrypt
# Fail the build, not the first task, if the binary cannot load in this image.
RUN lcpencrypt -h >/dev/null 2>&1 || lcpencrypt --help 2>&1 | grep -qi usage

COPY --from=requirements /requirements.txt /tmp/requirements.txt
COPY --from=requirements /wheels /tmp/wheels
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --root-user-action=ignore --require-hashes -r /tmp/requirements.txt \
    && pip install --root-user-action=ignore --no-deps --no-cache-dir /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels
# Fail the build, not the dashboards, if the lock ever loses the telemetry SDK (without it the kit is a silent no-op).
RUN python -c "import opentelemetry.sdk, opentelemetry.exporter.otlp.proto.http, opentelemetry.instrumentation.celery, opentelemetry.instrumentation.requests, opentelemetry.instrumentation.system_metrics, evilflowers_worker"

COPY pyproject.toml README.md ./
COPY evilflowers_lcpencrypt_worker/ evilflowers_lcpencrypt_worker/
RUN pip install --root-user-action=ignore --no-deps --no-cache-dir .

# Docker/Compose only (Kubernetes ignores HEALTHCHECK; the same command works as an exec liveness probe).
# The worker touches WORKER_HEARTBEAT_FILE every 30 s from its event loop (evilflowers-worker `heartbeat`);
# stale for 90 s means the loop is stuck, even while a long encryption runs in the pool child.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD evilflowers-worker-alive --max-age 90 || exit 1

# `exec`: Celery becomes PID 1 and receives SIGTERM. The kit's shutdown (worker #8) lets the running
# encryption go on for WORKER_SOFT_SHUTDOWN_TIMEOUT (15 s), then cancels it and puts the message back
# on the queue at once, requeueing it itself if the cancel takes over WORKER_CANCEL_TIMEOUT (10 s). Both
# fit the pod's 30 s grace. Never set REMAP_SIGTERM. `-E` keeps task events for Flower.
# Node name: CELERY_HOSTNAME (k8s sets a stable `lcpencrypt@dev`, so Flower keeps one worker across
# rollouts), else `lcpencrypt@<container hostname>`.
ENTRYPOINT ["sh", "-c", "exec celery -A evilflowers_lcpencrypt_worker worker \
    -Q evilflowers_lcpencrypt_worker \
    --concurrency=${CELERY_CONCURRENCY} \
    --max-tasks-per-child=${CELERY_MAX_TASKS_PER_CHILD} \
    --loglevel=${CELERY_LOG_LEVEL} \
    --hostname=${CELERY_HOSTNAME:-lcpencrypt@%h} \
    --without-mingle \
    --without-gossip \
    -E"]
