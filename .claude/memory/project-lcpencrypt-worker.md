---
name: project-lcpencrypt-worker
description: "Catalog wire contract, lcpencrypt v1.14 quirks, Python 3.14 traps and how to run the checks in this repo"
metadata:
  node_type: memory
  type: project
  verified: 2026-11-08
  originSessionId: 0c064b23-8064-4a5f-9bbb-5e6b4a4211ed
  modified: 2026-11-22T00:56:02.212Z
---

- Catalog (`apps/readium/services/content_encryption_service.py`, develop) sends `input_file`,
  `contentid`, `storage`, `filename`, `lcpsv`, `notify`, `url`, `title`, `author`; the result is
  not consumed (no result backend). `lcpsv`/`notify` carry htpasswd credentials: logs redact them.
- 2026-11-08: lcpencrypt pinned v1.14.0 (cgo/MuPDF; builder and runtime both trixie). It rejects
  `-storage` without `-url` (v1.13.4 too); the task now refuses that up front. It prints
  parameter errors on stdout.
- 2026-11-08 (issues #1, #2): PDFs with any metadata param are pre-packaged in
  `evilflowers_lcpencrypt_worker/package.py` (pypdfium2 + Pillow): cover, `numberOfPages`, toc,
  full RWPM metadata. lcpencrypt round-trips `manifest.json` through Go `rwpm.Publication`:
  unknown keys dropped, `null` or a partial `published` kills the run, `resources[].href` must be
  a zip entry. Smoke-tested in the image with the real binary (all fields survive). The catalog
  side is catalog#96 (on develop; not on dev while catalog#97 blocks its build).
- lcpencrypt v1.14 (`lcpencrypt/lcpencrypt.go`): every `exitWithError` reason goes to stdout, stderr
  is Go `log` progress; a failed `-notify` prints `Error notifying the CMS: …`, withdraws the
  content from the License Server and still exits 0. The task treats that as failure (worker #3).
- 2026-11-08 (worker #3, #4, closed; e80e73b live on dev, sidecar seen in trace): `report` kwarg → `evilflowers_lcpencrypt_worker/report.py`;
  sidecar `{filename}.manifest.json`, `package_preview` task, CLI `python -m
  evilflowers_lcpencrypt_worker.preview` (not `.package`: `__init__` imports it, runpy warns).
  Catalog side of the report endpoint is catalog#99, not built yet.
- 2026-11-08: to re-run a PDF on dev, `requeue_encryption` (elvira-dev MCP) only takes `pending`/`failed`
  jobs; `registered` needs `reencrypt` (management command only, catalog#98). Follow a run in Loki by
  content id → `trace_id` → Tempo. Filed: catalog#97 (catalog CI blocked, #96 not on dev), #98, #99,
  worker #3 (report outcomes), #4 (manifest sidecar/preview), knowledge#6 (entity + edges).
- 2026-11-09 (worker #5, closed; 06aebc1): image node name is `${CELERY_HOSTNAME:-lcpencrypt@%h}`;
  dev shows `lcpencrypt@dev` in Flower (env from knowledge#13). Compose default on the new image
  untested until prod (`:master`) gets it. Vault row: knowledge#15.
- 2026-11-11 (worker #6, closed; uncommitted at close): `broker_transport_options.visibility_timeout`
  = `BROKER_VISIBILITY_TIMEOUT` (default 43200). Dev Redis db 7 is shared with extraction; any
  consumer restores every unacked task older than its own timeout. Verify after rollout: knowledge#30 §4.
- 2026-11-22 (worker #8, closed; uncommitted at close on `feature/8-adopt-evilflowers-worker`): runtime is the
  evilflowers-worker kit referenced as `@master` (user: never a commit hash or a development build; the lock pins
  the resolved commit) (`create_app`, `run_task`, cap, shutdown, JSON logs). Traps: the kit reads
  `REDIS_URL`, `evilflowers_lcpencrypt_worker/settings.py` aliases `BROKER` first; pod grace is the k8s default
  30 s, so shutdown is 15 + 10 s; the kit's formatter drops `extra=` (use `observability.bind`), `logs.py` wraps it
  for redaction; the kit has no py.typed (mypy override, `app: Celery` annotation); the Dockerfile builds the
  git dep as a wheel (`--require-hashes` refuses VCS lines). Poetry drops `evilflowers-worker[otel]` in an extra
  when the main deps list the kit too: the lock had no SDK (CI run 44968567266), so the `observability` extra
  lists the otel packages itself and the Dockerfile import-checks them. `poetry lock` keeps an old git
  `reference`; `poetry update --lock evilflowers-worker` rewrites it. Role follow-up: knowledge#103 → PR knowledge#104 (comment + explicit 30 s grace). Alloy
  upper-cases the `level` label, so lower-case kit levels need no dashboard change.
- Python 3.14 lazy annotations: Celery reads `__annotations__`, so `Task[...]` must stay a string.
- Checks: local pipx Poetry is broken, use `uvx --python 3.14 poetry -C <repo> run pytest|ruff|mypy|black`.
  Test the task with `lcpencrypt.run(**kwargs)`, mock `evilflowers_lcpencrypt_worker.run_executable`;
  `tests/conftest.py` nulls the delivery counter's Redis client. Real-worker tests (`tests/test_runtime.py`) skip on
  macOS (prefork spawn bug): run them in `python:3.14-slim-trixie` against host Redis (`TEST_REDIS_URL`,
  `host.docker.internal:6379/15`). 235 tests on 2026-11-22. Mocked tests don't catch binary rules: smoke-test the image.
- Memory itself: pages in `.claude/memory/`, contract `.claude/rules/memory.md`, tool
  `tools/memory/memory.py` (copied from the knowledge repo 2026-11-08).

Related: [[project-k8s-deploy-and-otel]].
