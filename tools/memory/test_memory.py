"""Tests for tools/memory/memory.py (adapted from EvilFlowersCatalog/knowledge).

Run: `poetry run pytest tools/memory` (CI runs them with the rest), or
`python3 -I -m unittest discover -s tools/memory` without dependencies.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("memory", Path(__file__).with_name("memory.py"))
assert _spec and _spec.loader
memory = importlib.util.module_from_spec(_spec)
sys.modules["memory"] = memory  # dataclasses look their module up here
_spec.loader.exec_module(memory)

TODAY = dt.date(2026, 11, 8)


def page(name: str, type_: str, body: str = "Body 2026-11-01.", desc: str | None = None, extra: str = "") -> str:
    return (
        f'---\nname: {name}\ndescription: "{desc or "about " + name}"\n'
        f"metadata:\n  type: {type_}\n{extra}---\n\n{body}\n"
    )


class MemoryLintTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root
        self.mem = root / ".claude" / "memory"
        self.vault = root / "vault"  # absent in this repo: links resolve to memory pages only
        self.mem.mkdir(parents=True)
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".github" / "workflows" / "docker.yml").write_text("---\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, name: str, text: str) -> None:
        (self.mem / f"{name}.md").write_text(text)

    def lint(self) -> memory.Report:
        (self.mem / "MEMORY.md").write_text(memory.render_index(self.mem, TODAY))
        return memory.check(self.mem, self.repo, self.vault, TODAY)

    def test_clean_pages_pass(self) -> None:
        self.write("a", page("a", "feedback", "See [[b]] and `.github/workflows/docker.yml`."))
        self.write("b", page("b", "project", extra="  verified: 2026-11-04\n"))
        r = self.lint()
        self.assertEqual(r.errors, [])
        self.assertEqual(r.warnings, [])
        self.assertEqual(r.stale, [])

    def test_this_repos_paths_are_checked(self) -> None:
        self.write("a", page("a", "feedback", "Edit `evilflowers_lcpencrypt_worker/gone.py`."))
        self.assertTrue(any("does not exist" in w for w in self.lint().warnings))

    def test_name_must_equal_stem(self) -> None:
        self.write("a", page("other", "feedback"))
        self.assertTrue(any("!= filename stem" in e for e in self.lint().errors))

    def test_unknown_type(self) -> None:
        self.write("a", page("a", "note"))
        self.assertTrue(any("metadata.type" in e for e in self.lint().errors))

    def test_link_must_resolve(self) -> None:
        self.write("a", page("a", "feedback", "See [[Nowhere]]."))
        self.assertTrue(any("[[Nowhere]]" in e for e in self.lint().errors))

    def test_link_in_code_span_is_ignored(self) -> None:
        self.write("a", page("a", "feedback", "Syntax: `[[Nowhere]]`."))
        self.assertEqual(self.lint().errors, [])

    def test_project_page_goes_stale(self) -> None:
        self.write("p", page("p", "project", extra="  verified: 2026-10-01\n"))
        self.assertTrue(self.lint().stale)

    def test_review_days_override(self) -> None:
        self.write("p", page("p", "project", extra="  verified: 2026-11-01\n  review_days: 2\n"))
        self.assertTrue(self.lint().stale)

    def test_feedback_never_stale(self) -> None:
        self.write("f", page("f", "feedback", extra="  verified: 2020-01-01\n"))
        self.assertEqual(self.lint().stale, [])

    def test_project_without_verified_is_reported(self) -> None:
        self.write("p", page("p", "project"))
        self.assertEqual(self.lint().unverified, ["p"])

    def test_unlisted_page_is_an_error(self) -> None:
        self.write("a", page("a", "feedback"))
        (self.mem / "MEMORY.md").write_text("# Memory index\n")
        r = memory.check(self.mem, self.repo, self.vault, TODAY)
        self.assertTrue(any("not listed" in e for e in r.errors))

    def test_overlap_warns(self) -> None:
        d = "never run git writes yourself give the command"
        self.write("a", page("a", "feedback", desc=d))
        self.write("b", page("b", "feedback", desc=d + " always"))
        self.assertTrue(any("merge candidate" in w for w in self.lint().warnings))

    def test_index_keeps_header_and_sets_dream(self) -> None:
        self.write("a", page("a", "feedback"))
        (self.mem / "MEMORY.md").write_text("# Memory index\nHeader line.\nLast dream: 2026-01-01.\n\n## Old\n- x\n")
        text = memory.render_index(self.mem, TODAY)
        self.assertIn("Header line.", text)
        self.assertIn("Last dream: 2026-11-08", text)
        self.assertNotIn("## Old", text)
        self.assertIn("- [a](a.md) — about a", text)


class RepoMemoryTest(unittest.TestCase):
    """The repository's own `.claude/memory` must lint clean (as the knowledge repo requires)."""

    def test_repo_memory_lints_clean(self) -> None:
        if not memory.MEMORY_DIR.is_dir():
            self.skipTest("no .claude/memory in this checkout")
        r = memory.check(memory.MEMORY_DIR, memory.REPO, memory.VAULT_DIR, dt.date.today())
        self.assertEqual(r.errors, [])


if __name__ == "__main__":
    unittest.main()
