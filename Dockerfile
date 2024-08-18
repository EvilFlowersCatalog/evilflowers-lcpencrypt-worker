FROM golang:1.22 AS readium

RUN go install github.com/readium/readium-lcp-server/lcpencrypt@latest

FROM python:3.12-slim

# Create a non-root user called 'celery' and set ownership of the directory
RUN useradd --system --home /usr/local/src --shell /bin/bash celery \
    && mkdir -p /usr/local/src \
    && chown -R celery:celery /usr/local/src

# Switch to the working directory
WORKDIR /usr/local/src

# Copy the application code and set ownership to 'celery'
COPY --from=readium --chown=celery:celery /go/bin/lcpencrypt /usr/local/bin/lcpencrypt
COPY --chown=celery:celery . .

ENV READIUM_LCPENCRYPT_BIN=/usr/local/bin/lcpencrypt

# Install Python dependencies
RUN pip install celery[redis]

# Switch to the 'celery' user
USER celery

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 CMD celery -A evilflowers_lcpencrypt_worker status || exit 1

# Set the entrypoint and command to run the Celery worker
ENTRYPOINT ["celery", "-A", "evilflowers_lcpencrypt_worker", "worker", "-E"]
