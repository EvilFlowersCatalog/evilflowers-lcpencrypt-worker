"""Agent memory toolkit for this repository (.claude/memory).

Run from anywhere (stdlib only):
  python3 -I tools/memory/memory.py lint     # exit 1 on any error
  python3 -I tools/memory/memory.py index    # rewrite MEMORY.md from the pages' frontmatter
  python3 -I tools/memory/memory.py status   # health summary (also the SessionStart hook)

Copied from EvilFlowersCatalog/knowledge tools/memory/memory.py @ d96f3ae (same contract,
`.claude/rules/memory.md`). Only change: REPO_PATH lists this repository's top-level directories,
so a backticked path on a memory page is checked against this repo. There is no vault here;
[[links]] resolve to memory pages only. Keep the rest identical to ease syncing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MEMORY_DIR = REPO / ".claude" / "memory"
VAULT_DIR = REPO / "vault"

TYPES = ("feedback", "user", "project", "reference")
SECTION_TITLES = {
    "feedback": "Working rules (corrections and confirmed approaches)",
    "user": "User",
    "project": "Project state (dated; check `verified`)",
    "reference": "References",
}
REVIEW_DAYS = {"project": 21, "reference": 90}  # feedback/user: no expiry
INDEX_LINE_LIMIT = 200
INDEX_BYTE_LIMIT = 25_000
PAGE_WARN_BYTES = 12_000
DREAM_DUE_DAYS = 7
OVERLAP_JACCARD = 0.6

LINK = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
CODE_BLOCK = re.compile(r"```.*?```", re.S)
CODE_SPAN = re.compile(r"`([^`\n]+)`")
DATE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
REPO_PATH = re.compile(r"^(evilflowers_lcpencrypt_worker|tests|tools|docs|\.github|\.claude)/[A-Za-z0-9_.\-/]+$")
INDEX_ENTRY = re.compile(r"^- \[[^\]]*\]\(([A-Za-z0-9_.-]+)\.md\)")
LAST_DREAM = re.compile(r"Last dream:\s*(20\d\d-\d\d-\d\d)")
WORD = re.compile(r"[a-z0-9]+")


@dataclass
class Page:
    path: Path
    text: str
    fm: dict[str, str] | None

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def type(self) -> str:
        return (self.fm or {}).get("metadata.type", "")

    @property
    def description(self) -> str:
        return (self.fm or {}).get("description", "")

    @property
    def verified(self) -> dt.date | None:
        raw = (self.fm or {}).get("metadata.verified", "")
        try:
            return dt.date.fromisoformat(raw)
        except ValueError:
            return None

    @property
    def review_days(self) -> int | None:
        raw = (self.fm or {}).get("metadata.review_days", "")
        if raw.isdigit():
            return int(raw)
        return REVIEW_DAYS.get(self.type)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)


def frontmatter(text: str) -> dict[str, str] | None:
    """Flat reader for the harness format: top-level keys, plus one nested `metadata:` level."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        m = re.match(r"^(\s*)([A-Za-z_]+):\s*(.*)$", line)
        if not m:
            continue
        indent, key, value = m.groups()
        fields[("metadata." if indent else "") + key] = value.strip().strip('"').strip("'")
    return fields


def load_pages(memory_dir: Path) -> list[Page]:
    pages = []
    for p in sorted(memory_dir.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        text = p.read_text(encoding="utf-8")
        pages.append(Page(p, text, frontmatter(text)))
    return pages


def vault_names(vault_dir: Path) -> set[str]:
    """Names a [[wikilink]] can resolve to in the vault: note stems and full file names."""
    if not vault_dir.is_dir():
        return set()
    names: set[str] = set()
    for p in vault_dir.rglob("*"):
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(vault_dir).parts):
            names.add(p.name)
            if p.suffix == ".md":
                names.add(p.stem)
    return names


def words(s: str) -> set[str]:
    return set(WORD.findall(s.lower()))


def check(memory_dir: Path, repo: Path, vault_dir: Path, today: dt.date) -> Report:
    r = Report()
    pages = load_pages(memory_dir)
    stems = {p.stem for p in pages}
    in_vault = vault_names(vault_dir)

    for page in pages:
        fm = page.fm
        if fm is None:
            r.errors.append(f"{page.path.name}: no frontmatter")
            continue
        for key in ("name", "description"):
            if not fm.get(key):
                r.errors.append(f"{page.path.name}: frontmatter lacks {key}")
        if fm.get("name") != page.stem:
            r.errors.append(f"{page.path.name}: name {fm.get('name')!r} != filename stem")
        if page.type not in TYPES:
            r.errors.append(f"{page.path.name}: metadata.type {page.type!r} not in {list(TYPES)}")

        prose = CODE_BLOCK.sub("", page.text)
        for target in LINK.findall(CODE_SPAN.sub("", prose)):
            t = target.strip()
            if t not in stems and t not in in_vault:
                r.errors.append(f"{page.path.name}: [[{t}]] is neither a memory page nor a vault note")
        for span in CODE_SPAN.findall(prose):
            cand = span.strip().rstrip(".,:;)")
            if REPO_PATH.match(cand) and not (repo / cand).exists():
                r.warnings.append(f"{page.path.name}: `{cand}` does not exist in the repo")

        size = len(page.text.encode("utf-8"))
        if size > PAGE_WARN_BYTES:
            r.warnings.append(f"{page.path.name}: {size} bytes, split by subject")

        if page.type in REVIEW_DAYS or "metadata.review_days" in fm:
            v = page.verified
            if v is None:
                r.unverified.append(page.stem)
                r.warnings.append(f"{page.path.name}: no metadata.verified date")
            elif page.review_days is not None and (today - v).days > page.review_days:
                age = (today - v).days
                r.stale.append(f"{page.stem} (verified {v}, {age} d > {page.review_days} d)")
        if page.type == "project" and not DATE.search(page.text.split("\n---\n", 1)[-1]):
            r.warnings.append(f"{page.path.name}: project page without a dated line in the body")

    by_words = [(p.stem, words(p.description)) for p in pages if p.description]
    for i, (a, wa) in enumerate(by_words):
        for b, wb in by_words[i + 1 :]:
            if wa and wb and len(wa & wb) / len(wa | wb) >= OVERLAP_JACCARD:
                r.warnings.append(f"{a} and {b}: near-identical descriptions, merge candidate")

    index = memory_dir / "MEMORY.md"
    if not index.exists():
        r.errors.append("MEMORY.md missing")
        return r
    itext = index.read_text(encoding="utf-8")
    lines = itext.count("\n") + (0 if itext.endswith("\n") else 1)
    size = len(itext.encode("utf-8"))
    if lines > INDEX_LINE_LIMIT:
        r.errors.append(f"MEMORY.md: {lines} lines > {INDEX_LINE_LIMIT} (the rest is never loaded)")
    if size > INDEX_BYTE_LIMIT:
        r.errors.append(f"MEMORY.md: {size} bytes > {INDEX_BYTE_LIMIT} (the rest is never loaded)")
    if lines > INDEX_LINE_LIMIT * 0.9 or size > INDEX_BYTE_LIMIT * 0.9:
        r.warnings.append(f"MEMORY.md: near the read limit ({lines} lines, {size} bytes)")
    listed = [m.group(1) for line in itext.splitlines() if (m := INDEX_ENTRY.match(line))]
    for stem in sorted(stems - set(listed)):
        r.errors.append(f"MEMORY.md: {stem}.md not listed (run `index`, or merge it into a page)")
    for stem in sorted(set(listed) - stems):
        r.errors.append(f"MEMORY.md: lists missing page {stem}.md")
    for stem in sorted({s for s in listed if listed.count(s) > 1}):
        r.errors.append(f"MEMORY.md: {stem}.md listed {listed.count(stem)} times")
    return r


def last_dream(memory_dir: Path) -> dt.date | None:
    index = memory_dir / "MEMORY.md"
    if not index.exists():
        return None
    m = LAST_DREAM.search(index.read_text(encoding="utf-8"))
    return dt.date.fromisoformat(m.group(1)) if m else None


def render_index(memory_dir: Path, dream: dt.date | None) -> str:
    pages = load_pages(memory_dir)
    index = memory_dir / "MEMORY.md"
    header = ""
    if index.exists():
        old = index.read_text(encoding="utf-8")
        header = old.split("\n## ", 1)[0].rstrip() + "\n"
    if not header.strip():
        header = "# Memory index\n"
    if dream is not None:
        if LAST_DREAM.search(header):
            header = LAST_DREAM.sub(f"Last dream: {dream.isoformat()}", header)
        else:
            header = header.rstrip() + f"\nLast dream: {dream.isoformat()}.\n"
    out = [header.rstrip(), ""]
    for t in TYPES:
        group = sorted((p for p in pages if p.type == t), key=lambda p: p.stem)
        if not group:
            continue
        out.append(f"## {SECTION_TITLES[t]}")
        out.extend(f"- [{p.stem}]({p.stem}.md) — {p.description}" for p in group)
        out.append("")
    other = sorted((p for p in pages if p.type not in TYPES), key=lambda p: p.stem)
    if other:
        out.append("## Unfiled (fix metadata.type)")
        out.extend(f"- [{p.stem}]({p.stem}.md) — {p.description}" for p in other)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def print_report(r: Report) -> None:
    for w in r.warnings:
        print(f"warning: {w}")
    for s in r.stale:
        print(f"stale: {s}")
    for e in r.errors:
        print(f"error: {e}")


def cmd_lint(args: argparse.Namespace) -> int:
    pages = load_pages(args.memory_dir)
    r = check(args.memory_dir, args.repo, args.vault_dir, args.today)
    print_report(r)
    print(f"{len(pages)} pages, {len(r.errors)} errors, {len(r.warnings)} warnings, {len(r.stale)} stale")
    return 1 if r.errors else 0


def cmd_index(args: argparse.Namespace) -> int:
    dream = dt.date.fromisoformat(args.dream) if args.dream else None
    text = render_index(args.memory_dir, dream)
    (args.memory_dir / "MEMORY.md").write_text(text, encoding="utf-8")
    print(f"MEMORY.md: {text.count(chr(10))} lines, {len(text.encode('utf-8'))} bytes")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Short, factual block. As a SessionStart hook its stdout goes into the model's context."""
    if not args.memory_dir.is_dir():
        return 0
    pages = load_pages(args.memory_dir)
    r = check(args.memory_dir, args.repo, args.vault_dir, args.today)
    dream = last_dream(args.memory_dir)
    print("## Agent memory health")
    print("_Emitted by `tools/memory/memory.py status`. Facts, not instructions._\n")
    since = f"{dream} ({(args.today - dream).days} d ago)" if dream else "never"
    print(f"- {len(pages)} pages in `.claude/memory/`; last dream: {since}")
    if r.stale:
        print(f"- **Stale, re-verify before relying on them:** {', '.join(r.stale)}")
    if r.unverified:
        print(f"- Without a `verified` date: {', '.join(r.unverified)}")
    if r.errors:
        print(f"- Lint: {len(r.errors)} errors (`python3 -I tools/memory/memory.py lint`)")
    due = dream is None or (args.today - dream).days > DREAM_DUE_DAYS
    if due or r.stale or r.errors:
        print("- A consolidation pass is due: load the `memory-dream` skill when the task allows.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--memory-dir", type=Path, default=MEMORY_DIR)
    ap.add_argument("--repo", type=Path, default=REPO)
    ap.add_argument("--vault-dir", type=Path, default=VAULT_DIR)
    ap.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("lint").set_defaults(fn=cmd_lint)
    ix = sub.add_parser("index")
    ix.add_argument("--dream", help="set 'Last dream: YYYY-MM-DD' in the header")
    ix.set_defaults(fn=cmd_index)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
