---
name: feedback-scope-to-this-repo
description: "Local filesystem = this repo only; other EvilFlowers repos through the GitHub MCP, and changes there as issues, not edits"
metadata:
  type: feedback
---

Never read or list sibling directories (e.g. `~/Projects/EvilFlowers`). Read other repos
(EvilFlowersCatalog, evilflowers-text-service, knowledge, …) with `mcp__github-evilflowers__*`;
infra facts with the `evilflowers-knowledge` MCP (`Deploy-a-Service` note). A change another
repo needs becomes an issue there.

**Why:** 2026-11-08 the user rejected an `ls` of the parent directory ("Work in this repository.
For rest use MCPs") and had asked to "create proper issue and we will implement there".
**How to apply:** cross-repo context → MCP reads; cross-repo change → `issue_write`. Related:
[[feedback-no-git-writes]].
