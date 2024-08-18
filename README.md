# evilflowers-lcpencrypt-worker

`evilflowers-lcpencrypt-worker` is a Celery-based worker that executes the lcpencrypt tool from the Readium LCP Server.
The worker accepts a variety of parameters and options to securely encrypt content using the LCP
(Licensed Content Protection) standard. The project is designed to run as a Docker container.

## Installation

To build the Docker image locally:

```shell
docker build -t evilflowers-lcpencrypt-worker .
```

To run the Docker container:

```shell
docker run -d --name lcpencrypt-worker \
    -e BROKER=redis://your-redis-server:6379/0 \
    -e STORAGE_PATH=/mnt/data \
    -e READIUM_LCPENCRYPT_BIN=/usr/local/bin/lcpencrypt \
    evilflowers-lcpencrypt-worker

```

## Usage

```python
from celery import signature

lcpencrypt_signature = signature("evilflowers_lcpencrypt_worker.lcpencrypt", kwargs={
    "input_file": "yourfile.epub",
    "contentid": "optional-content-id",
    "storage": "optional-storage-location",
    "url": "optional-url",
    "filename": "optional-output-filename",
    "temp": "/tmp",
    "cover": False,
    "contentkey": "optional-content-key",
    "lcpsv": "optional-lcpsv-url",
    "v2": False,
    "username": "optional-username",
    "password": "optional-password",
    "notify": "optional-notify-url",
    "verbose": True
})

# Send the task to Celery
lcpencrypt_signature.delay()
```

## Environment variables

| Environment Variable          | Description                                                       | Default Value              | Example                       |
|-------------------------------|-----------------------------------------------------------------------|---------------------------------|
| `BROKER`                      | The URL of the Celery broker                                          | `redis://localhost:6379/0`      |
| `LOG_LEVEL`                   | Logging level (e.g., `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) | `INFO`                          |
| `STORAGE_PATH`                | The base path for storing files                                       | `/mnt/data`                     |
| `READIUM_LCPENCRYPT_BIN`      | Path to the `lcpencrypt` binary                                       | `/usr/local/bin/lcpencrypt`     |
| `OTEL_SERVICE_NAME`           | Name of the service for OpenTelemetry tracing                         | `evilflowers-lcpencrypt-worker` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | The endpoint of the OpenTelemetry Collector or backend for traces     | Not set                         | `http://collector:4317`       |


## Acknowledgment

This open-source project is maintained by students and PhD candidates of the
[Faculty of Informatics and Information Technologies](https://www.fiit.stuba.sk/) at the Slovak University of
Technology. The software is utilized by the university, aligning with its educational and research activities. We
appreciate the faculty's support of our work and their contribution to the open-source community.

![](docs/images/fiit.png)
