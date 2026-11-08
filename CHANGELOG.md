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
