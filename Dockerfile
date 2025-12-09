FROM golang:1.24 AS readium

RUN go install github.com/readium/readium-lcp-server/lcpencrypt@latest

FROM python:3.14-slim

WORKDIR /usr/local/src

# Copy the lcpencrypt binary
COPY --from=readium /go/bin/lcpencrypt /usr/local/bin/lcpencrypt

# Copy the application code
COPY . .

ENV READIUM_LCPENCRYPT_BIN=/usr/local/bin/lcpencrypt

# Install Python dependencies
RUN pip install celery[redis]

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD celery -A evilflowers_lcpencrypt_worker status || exit 1

# Set the entrypoint and command to run the Celery worker
ENTRYPOINT ["celery", "-A", "evilflowers_lcpencrypt_worker", "worker", "-Q", "evilflowers_lcpencrypt_worker", "-E"]
