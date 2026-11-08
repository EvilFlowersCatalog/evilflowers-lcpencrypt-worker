# Memory index
Agent working knowledge for evilflowers-lcpencrypt-worker, one page per subject; read a page
before acting on its subject. Pages past their review window are stale: re-verify before relying
on them. Contract `.claude/rules/memory.md`, tool `tools/memory/memory.py`, consolidation
`/memory-dream` (same setup as EvilFlowersCatalog/knowledge).
Last dream: 2026-11-08 (moved into the repo from the machine-local memory: 4 pages; legacy index notes retired).

## Working rules (corrections and confirmed approaches)
- [feedback-no-git-writes](feedback-no-git-writes.md) — Never run git write operations (commit, push, amend, rebase, reset, tag, merge); the user does every git write
- [feedback-scope-to-this-repo](feedback-scope-to-this-repo.md) — Local filesystem = this repo only; other EvilFlowers repos through the GitHub MCP, and changes there as issues, not edits

## Project state (dated; check `verified`)
- [project-k8s-deploy-and-otel](project-k8s-deploy-and-otel.md) — Where the worker runs (k3s evilflowers-dev), how CI deploys it (develop auto-deploy works since c37f894), and the OTel contract
- [project-lcpencrypt-worker](project-lcpencrypt-worker.md) — Catalog wire contract, lcpencrypt v1.14 quirks, Python 3.14 traps and how to run the checks in this repo
