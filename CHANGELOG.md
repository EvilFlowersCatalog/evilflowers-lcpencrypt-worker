# Changelog

This changelog suppose to follow rules defined in the [changelog.md](https://changelog.md)

## 0.2.0 - TBD

- **Changed**: Readium `lcpencrypt` pinned to **v1.14.0** (was an unpinned `@latest`); builder and
  runtime images share Debian trixie because the binary now links MuPDF through cgo
- **Changed**: observability moved from Logfire to the OpenTelemetry SDK (OTLP/HTTP to Grafana
  Alloy, catalog release train 1.45.2 / 0.66b2). Fixes the dev pod logging every Redis command as a
  WARNING (Logfire's console fallback without a token) and exporting nothing to Tempo
- **Added**: `lcpencrypt` span continuing the catalog's trace, `lcpencrypt.*` metrics, per-process metrics
- **Added**: JSON logs (`LOG_FORMAT=json`, image default) with `level`, `trace_id`, `content_id`, `task_id`
- **Added**: CI gate (ruff, black, mypy strict, pytest) and k3s auto-deploy: `develop` →
  `evilflowers-dev`; `master` → `evilflowers-prod` behind `vars.PRODUCTION_DEPLOY_ENABLED`
- **Fixed**: the container's PID 1 was `sh`, so SIGTERM never reached Celery (killed after the grace period)
- **Fixed**: License Server / CMS credentials (`lcpsv`, `notify`) were written to the logs, both in
  the lcpencrypt command line and in Celery's own task lines; every log line is now redacted
- **Changed**: `worker_prefetch_multiplier=1` (one long-running subprocess per task: never reserve
  a second message behind it)
- **Fixed**: a failed run's `error` now carries lcpencrypt's reason (it prints parameter errors on stdout)
- **Fixed**: lcpencrypt's normal stderr progress is logged at DEBUG instead of WARNING on success
- **Changed**: the image installs exactly what `poetry.lock` pins (hash-checked); `requirements.sh` removed
- **Changed**: `pyproject.toml` uses PEP 621 `[project]` metadata (Poetry 2)
- **Fixed**: a PDF pre-packaged with the catalog's title/author lost the cover and `numberOfPages`
  that lcpencrypt v1.14 adds natively. The worker now renders the first page as cover (or uses the
  catalog's `cover`), and adds `numberOfPages`, the PDF's outline as `toc` and its subject as
  `description` ([#2](https://github.com/EvilFlowersCatalog/evilflowers-lcpencrypt-worker/issues/2))
- **Added**: publication metadata params for raw-PDF input: `subtitle`, `authors`, `translators`,
  `editors`, `illustrators`, `contributors`, `publisher`, `language`, `published`, `description`,
  `subjects`, `identifier`, `cover` — all that lcpencrypt keeps from a Readium manifest
  ([#1](https://github.com/EvilFlowersCatalog/evilflowers-lcpencrypt-worker/issues/1))
- **Added**: dependencies `pypdfium2` (PDF page count, outline, first-page render) and `pillow`
- **Changed**: `storage` without `url` fails the task up front (lcpencrypt v1.13.4+ refuses it);
  the "url optional" branch is gone
- **Fixed**: `protected_content_location` now carries the extension lcpencrypt appended (`.lcpdf`, …)
- **Added**: `report` param: the worker POSTs an `EncryptionReport` (success or failure, package
  summary, sidecar path, never the content key) to the catalog once per task, with Basic auth from
  the URL, a W3C `traceparent` and bounded retries, so a failed job no longer stays `pending`
  ([#3](https://github.com/EvilFlowersCatalog/evilflowers-lcpencrypt-worker/issues/3))
- **Fixed**: lcpencrypt v1.14 exits 0 after a failed `notify` call, having withdrawn the content
  from the License Server; the task now returns `success: False` for it instead of success (#3)
- **Fixed**: a failed run's `error` ends with lcpencrypt's reason from stdout instead of its last
  stderr progress line (e.g. `Process as EPUB`)
- **Added**: manifest sidecar `{storage}/{filename}.manifest.json` (`MANIFEST_SIDECAR`), the
  `lcp.package.manifest` span event, the `package_preview` task and
  `python -m evilflowers_lcpencrypt_worker.preview`, to check a package without Thorium
  ([#4](https://github.com/EvilFlowersCatalog/evilflowers-lcpencrypt-worker/issues/4))
- **Added**: `CELERY_HOSTNAME` sets the image's Celery node name (default `lcpencrypt@%h`), so the
  k8s worker keeps one stable name in Flower across rollouts
  ([#5](https://github.com/EvilFlowersCatalog/evilflowers-lcpencrypt-worker/issues/5))
- **Fixed**: the worker ran with kombu's default Redis `visibility_timeout` of 1 h and, sharing
  the broker db with the extraction pipeline, redelivered any of its tasks held longer than that.
  It is now `BROKER_VISIBILITY_TIMEOUT` (default 12 h, as the catalog and the analyzer); a lost
  encryption is redelivered after up to 12 h
  ([#6](https://github.com/EvilFlowersCatalog/evilflowers-lcpencrypt-worker/issues/6))
- **Changed**: the Celery runtime is [evilflowers-worker](https://github.com/EvilFlowersCatalog/evilflowers-worker)
  (its `master`, locked in `poetry.lock`), shared with the other workers on the Redis db; `celery[redis]` floor 5.6
  ([#8](https://github.com/EvilFlowersCatalog/evilflowers-lcpencrypt-worker/issues/8)):
  - **Fixed**: a publication that killed the worker was redelivered forever. Past `MAX_DELIVERIES` (5) the task
    now reports `success: false` (`Gave up after N deliveries …`) to the `report` URL and is dropped
  - **Fixed**: SIGTERM let a long encryption run into the pod's SIGKILL, and its message came back only after the
    visibility timeout. Now it gets `WORKER_SOFT_SHUTDOWN_TIMEOUT` (15 s), is cancelled and requeued at once
    (fallback requeue after `WORKER_CANCEL_TIMEOUT`, 10 s); nothing is reported for it
  - **Changed**: settings are `Settings(RuntimeSettings)` (`settings.py`); same variables, plus the kit's.
    `LOG_LEVEL` is gone (Celery's `--loglevel`)
  - **Changed**: telemetry and logs are the kit's. JSON lines now say `message` (was `msg`), `exception` (was
    `exc`) and a lower-case `level`, and carry `task_name` and `delivery`. URL credentials stay redacted. The
    default-gateway collector probe is gone (set `OTEL_EXPORTER_OTLP_ENDPOINT` under Compose);
    `lcpencrypt.version` moved from the resource to the `lcpencrypt` span. The `lcpencrypt.*` metric names are
    unchanged
  - **Changed**: task kwargs are validated; a message without `input_file` is reported `success: false`
    (`Invalid task message: …`, field names only) and fails at once, without a retry. Validation errors never
    echo the kwargs (pydantic truncated them mid-URL, past the log redaction)
  - **Fixed**: a failed run's `error` (reported to the catalog, in task events) is redacted like the logs
  - **Changed**: the image `HEALTHCHECK` is the kit's liveness file (`evilflowers-worker-alive --max-age 90`)
  - **Added**: CI runs the real-worker tests (SIGTERM requeue, delivery cap) against a Redis service

## 0.1.0 - TBD

Initial minimum valuable product pre-release.

- **Added**: OpenTelemetry support
- **Added**: `evilflowers_lcpencrypt_worker.lcpencrypt` task introduced
- **Added**: `title`/`author` task params — for raw-PDF input the PDF is pre-packaged into a
  Readium Web Publication whose `manifest.json` carries the metadata, so the encrypted `.lcpdf`
  shows the correct title/author in Thorium instead of "no title and no authors available" ([#1](https://github.com/EvilFlowersCatalog/evilflowers-lcpencrypt-worker/issues/1))
- **Added**: Sample docs
- **Added**: Docker image
- **Fixed**: encrypted-file metadata lookup now probes LCP output extensions (`.lcpdf`, `.lcpa`, `.lcpdi`, `.webpub`)
