---
name: project-lcpencrypt-worker
description: "Catalog wire contract, lcpencrypt v1.14 quirks, Python 3.14 traps and how to run the checks in this repo"
metadata:
  type: project
  verified: 2026-11-08
---

- Catalog (`apps/readium/services/content_encryption_service.py`, develop) sends `input_file`,
  `contentid`, `storage`, `filename`, `lcpsv`, `notify`, `url`, `title`, `author`; the result is
  not consumed (no result backend). `lcpsv`/`notify` carry htpasswd credentials: logs redact them.
- 2026-11-08: lcpencrypt pinned v1.14.0 (cgo/MuPDF; builder and runtime both trixie). It rejects
  `-storage` without `-url` (v1.13.4 too), so the March "url optional" fix never worked against
  the real binary; it prints parameter errors on stdout. Raw PDFs get `cover.jpg` +
  `numberOfPages`, our title/author `.rpf` path loses them (issue #2).
- Python 3.14 lazy annotations: Celery reads `__annotations__`, so `Task[...]` must stay a string.
- Checks: local pipx Poetry is broken, use `uvx --python 3.14 poetry -C <repo> run pytest|ruff|mypy|black`.
  Test the task with `lcpencrypt.run(**kwargs)`, mock `evilflowers_lcpencrypt_worker.run_executable`.
  152 tests on 2026-11-08. Mocked tests don't catch binary rules: smoke-test inside the image.
- Memory itself: pages in `.claude/memory/`, contract `.claude/rules/memory.md`, tool
  `tools/memory/memory.py` (copied from the knowledge repo 2026-11-08).

Related: [[project-k8s-deploy-and-otel]].
