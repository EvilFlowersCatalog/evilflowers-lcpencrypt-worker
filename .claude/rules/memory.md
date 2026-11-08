---
paths:
  - ".claude/memory/**"
  - "tools/memory/**"
---

# Agent memory pages

Same contract as EvilFlowersCatalog/knowledge (`.claude/rules/memory.md` there).

`.claude/memory/` is the auto-memory directory (`autoMemoryDirectory` in the untracked
`.claude/settings.local.json`; the pages themselves are git-tracked). It is the agent's working
knowledge: how the user wants work done, traps, and the current state of things the agent keeps
acting on. It is not documentation: facts a human needs live in `README.md`, `CHANGELOG.md` or the
knowledge vault (evilflowers-knowledge MCP); a memory page points there instead of copying it.

`MEMORY.md` is the index the harness loads at session start (first 200 lines / 25 KB). One line
per page, no content. It is generated: `python3 -I tools/memory/memory.py index`.

## Page contract
- File `<kebab-slug>.md`, slug prefixed with its type (`feedback-`, `project-`, `reference-`,
  `user-`); frontmatter `name` = the filename stem, `description` = one-line hook,
  `metadata.type` in `feedback | user | project | reference`. No harness bookkeeping fields
  (`originSessionId`, `modified`, `node_type`) in committed pages.
- `metadata.verified: YYYY-MM-DD` on every `project` and `reference` page: the day its claims were
  last checked against the cluster, GitHub or the repo. Past the review window (project 21 d,
  reference 90 d, or `metadata.review_days`) the page is reported stale at session start.
  Re-verify before acting on a stale page.
- Feedback pages: the rule, then `**Why:**` (the incident, dated, the user's words) and
  `**How to apply:**`.
- `[[slug]]` links another memory page (no vault here). Evidence is a pointer in prose: a commit,
  an issue (`knowledge#1`), a backticked repo path (`evilflowers_lcpencrypt_worker/...`,
  `.github/...`, `tests/...`; checked by the lint).

## Writing
- Search the index first; extend the subject's page instead of adding a new one.
- Drop what the repo, a skill or a rule already records. Keep a fix as one dated line.
- Replace a superseded claim; never keep it beside its correction.
- Never a secret value on a page (lcpsv/notify URLs carry htpasswd credentials).
- After any change: `python3 -I tools/memory/memory.py index` then `... lint` (must exit 0;
  `tools/memory/test_memory.py` also lints the real pages in CI).
- Git is read-only for the agent: leave the tree dirty, the user commits.
- Consolidation (git/issues → pages, retire, re-verify): the `memory-dream` skill.
