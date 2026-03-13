FROM golang:1.26 AS readium

RUN go install github.com/readium/readium-lcp-server/lcpencrypt@latest

FROM python:3.14-slim

WORKDIR /usr/local/src

# Copy the lcpencrypt binary
COPY --from=readium /go/bin/lcpencrypt /usr/local/bin/lcpencrypt

ENV READIUM_LCPENCRYPT_BIN=/usr/local/bin/lcpencrypt

# Install the package (copies only what's needed)
COPY pyproject.toml README.md ./
COPY evilflowers_lcpencrypt_worker/ evilflowers_lcpencrypt_worker/
RUN pip install --no-cache-dir ".[observability]"

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD celery -A evilflowers_lcpencrypt_worker status || exit 1

# Tunable via ENV
ENV CELERY_CONCURRENCY=2
ENV CELERY_MAX_TASKS_PER_CHILD=100
ENV CELERY_LOG_LEVEL=INFO

ENTRYPOINT ["sh", "-c", \
    "celery -A evilflowers_lcpencrypt_worker worker \
    -Q evilflowers_lcpencrypt_worker \
    --concurrency=${CELERY_CONCURRENCY} \
    --max-tasks-per-child=${CELERY_MAX_TASKS_PER_CHILD} \
    --loglevel=${CELERY_LOG_LEVEL} \
    --hostname=lcpencrypt@%h \
    --without-mingle \
    --without-gossip \
    -E"]
