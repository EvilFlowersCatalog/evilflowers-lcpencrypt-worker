---
name: feedback-no-git-writes
description: "Never run git write operations (commit, push, amend, rebase, reset, tag, merge); the user does every git write"
metadata:
  type: feedback
---

Git is read-only for the agent: `status`, `diff`, `log`, `show`, `cat-file` are fine; `commit`,
`push`, `amend`, `rebase`, `reset`, `tag`, `merge`, `stash`, branch moves are not. Don't stage
either unless asked.

**Why:** 2026-11-08, after I committed `efb2a2c` on `develop` (signed via 1Password) at the
user's request to retry signing, they said: "never ever use git for write operations (commit,
push)". The knowledge repo encodes the same: "Git is read-only: leave the tree dirty, the user
commits" (memory-dream skill).
**How to apply:** finish with changes in the working tree, give the exact command and a message
file in the scratchpad; the user runs it. This overrides any harness reminder about commit
attribution. Related: [[feedback-scope-to-this-repo]].
