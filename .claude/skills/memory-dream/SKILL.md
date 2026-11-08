---
name: memory-dream
description: >-
  Consolidate the agent memory in `.claude/memory/`: read what changed since the last pass
  (git log, CHANGELOG, the issues this repo filed or tracks), attach each durable fact to its
  subject page, re-verify stale project/reference pages with read-only tools (cluster/GitHub
  MCPs, repo files), retire what the repo now records, regenerate the index and lint. Use when
  asked to "dream", "consolidate memory", "update memory", "tidy memory", when the session-start
  memory health says a pass is due or pages are stale, or after a long session that changed what
  we know. Not for saving a single memory mid-task (do that inline).
---

# Memory dream

Adapted from EvilFlowersCatalog/knowledge `.claude/skills/memory-dream`. Memory = the agent's
working knowledge (how to work here, traps, current state it acts on). Contract:
`.claude/rules/memory.md`. Tool: `tools/memory/memory.py` (stdlib, `python3 -I`).

## 1. Orient
```bash
python3 -I tools/memory/memory.py status
python3 -I tools/memory/memory.py lint
```
Note the last dream date, the stale pages and the lint errors. Then collect what changed since
the last dream:
- `git log --since=<last dream> --oneline` (read-only);
- `CHANGELOG.md` entries added since;
- state of the issues the pages name (knowledge#1, EvilFlowersCatalog#95, this repo's issues) via
  the GitHub MCP;
- loose pages the harness wrote mid-session (lint: "not listed").

## 2. Attach
- a correction from the user → the matching `feedback-*` page (incident, date, the user's words).
  A new kind of correction → a new feedback page;
- a state change → the subject's `project-*` page as a dated line, replacing the old claim;
- a trap that cost time (tool quirk, binary rule, CI behavior) → the subject's page, one dated
  line with the evidence pointer;
- a loose harness page → merge into its subject page, delete the loose file; strip harness
  bookkeeping fields from frontmatter.

## 3. Re-verify
For every stale `project`/`reference` page: check each claim with read-only tools (Grafana/Mimir
and evilflowers-knowledge MCPs for the cluster, GitHub MCP for issues and other repos, repo
files). Confirmed → bump `metadata.verified` to today. Wrong → rewrite the claim with today's
date. Never apply anything to the cluster and never write to git.

## 4. Retire
- A fact the repo, a skill or a rule now states → delete it from the page.
- A fixed defect → one dated line, not the story.
- A page past ~12 KB → split by subject, not by date.

## 5. Write and check
```bash
python3 -I tools/memory/memory.py index --dream <today>
python3 -I tools/memory/memory.py lint
```
Fix every error. Report what moved, what was re-verified (and how), what was retired, and what is
still unverified. Git is read-only: leave the tree dirty, the user commits.

## Guardrails
- Invent nothing: every date and number traces to a commit, an issue, or a tool result you ran.
- No secret values on a page, ever.
