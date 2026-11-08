---
name: project-lcpencrypt-worker
description: "Catalog wire contract, lcpencrypt v1.14 quirks, Python 3.14 traps and how to run the checks in this repo"
metadata:
  node_type: memory
  type: project
  verified: 2026-11-08
  originSessionId: 0c064b23-8064-4a5f-9bbb-5e6b4a4211ed
  modified: 2026-11-08T13:29:12.840Z
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
  still sends only `title` + first `author`; the rest is filed as EvilFlowersCatalog#96.
- Python 3.14 lazy annotations: Celery reads `__annotations__`, so `Task[...]` must stay a string.
- Checks: local pipx Poetry is broken, use `uvx --python 3.14 poetry -C <repo> run pytest|ruff|mypy|black`.
  Test the task with `lcpencrypt.run(**kwargs)`, mock `evilflowers_lcpencrypt_worker.run_executable`.
  152 tests on 2026-11-08. Mocked tests don't catch binary rules: smoke-test inside the image.
- Memory itself: pages in `.claude/memory/`, contract `.claude/rules/memory.md`, tool
  `tools/memory/memory.py` (copied from the knowledge repo 2026-11-08).

Related: [[project-k8s-deploy-and-otel]].
