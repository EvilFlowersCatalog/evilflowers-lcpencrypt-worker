"""Readium package (`.rpf`) pre-packaging for raw-PDF input.

lcpencrypt wraps a raw PDF into a Readium package itself, but it reads bibliographic metadata
only from the PDF's own Info dictionary. To carry the catalog's metadata, the worker builds the
package and hands lcpencrypt the `.rpf` instead (issue #1). lcpencrypt then keeps our manifest,
so everything its native PDF path adds (cover thumbnail, `numberOfPages`, description from the
PDF subject) must be added here too (issue #2).

lcpencrypt (v1.14) decodes `manifest.json` into its `rwpm.Publication` Go struct and writes the
struct back out, so the manifest is limited by that struct:

- keys the struct does not know are dropped silently;
- a value of the wrong shape fails the decode and the whole encryption (never emit `null`);
- `title` must stay a plain string: lcpencrypt reads `title["und"]` for the License Server and
  CMS notifications;
- `published` must be a full date: a partial `2023` or `2023-05` does not parse;
- every `resources[].href` must name an entry of the zip (lcpencrypt dereferences it unchecked).
"""

import contextlib
import json
import logging
import os
import re
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pypdfium2 as pdfium
from PIL import Image

if TYPE_CHECKING:
    from evilflowers_lcpencrypt_worker.types import LCPEncryptParams

logger = logging.getLogger(__name__)

RWPM_CONTEXT: Final = "https://readium.org/webpub-manifest/context.jsonld"
RWPM_PDF_PROFILE: Final = "https://readium.org/webpub-manifest/profiles/pdf"
PUBLICATION_HREF: Final = "publication.pdf"

# lcpencrypt's own cover: the first page, longest side 1200 px, at most 300 DPI, JPEG.
COVER_MAX_SIDE: Final = 1200
COVER_MAX_DPI: Final = 300.0
COVER_JPEG_QUALITY: Final = 75

# Outline depth kept in the manifest `toc`; deeper levels add little in a reader's panel.
TOC_MAX_DEPTH: Final = 6


@dataclass(slots=True, kw_only=True)
class PublicationMetadata:
    """Bibliographic metadata the catalog sends, normalized for the RWPM manifest.

    Empty strings and empty lists mean "unknown"; nothing here is ever serialized as `null`.
    """

    title: str = ""
    subtitle: str = ""
    authors: list[str] = field(default_factory=list)
    translators: list[str] = field(default_factory=list)
    editors: list[str] = field(default_factory=list)
    illustrators: list[str] = field(default_factory=list)
    contributors: list[str] = field(default_factory=list)
    publisher: str = ""
    language: list[str] = field(default_factory=list)
    published: str = ""
    description: str = ""
    subjects: list[str] = field(default_factory=list)
    identifier: str = ""

    @classmethod
    def from_params(cls, params: LCPEncryptParams) -> PublicationMetadata:
        """Read and normalize the metadata keys of the task kwargs.

        `authors` wins over the single `author` the catalog sent before it had the list.
        A partial or malformed `published` is dropped (lcpencrypt cannot parse it).
        """
        authors = _strings(params.get("authors")) or _strings(params.get("author"))
        published = _full_date(params.get("published"))
        if params.get("published") and not published:
            logger.info("Dropping published=%r: lcpencrypt needs a full YYYY-MM-DD date", params.get("published"))
        return cls(
            title=_text(params.get("title")),
            subtitle=_text(params.get("subtitle")),
            authors=authors,
            translators=_strings(params.get("translators")),
            editors=_strings(params.get("editors")),
            illustrators=_strings(params.get("illustrators")),
            contributors=_strings(params.get("contributors")),
            publisher=_text(params.get("publisher")),
            language=_strings(params.get("language")),
            published=published,
            description=_text(params.get("description")),
            subjects=_strings(params.get("subjects")),
            identifier=_text(params.get("identifier")),
        )

    def is_empty(self) -> bool:
        return not any(getattr(self, name) for name in self.__slots__)


@dataclass(slots=True)
class TocEntry:
    title: str
    page: int  # 1-based
    children: list[TocEntry] = field(default_factory=list)

    def to_link(self) -> dict[str, Any]:
        link: dict[str, Any] = {"href": f"{PUBLICATION_HREF}#page={self.page}", "title": self.title}
        if self.children:
            link["children"] = [child.to_link() for child in self.children]
        return link


@dataclass(slots=True)
class PdfInfo:
    """What the PDF itself says about the publication (all fields optional)."""

    page_count: int = 0
    title: str = ""
    author: str = ""
    subject: str = ""
    keywords: str = ""
    toc: list[TocEntry] = field(default_factory=list)


@dataclass(slots=True)
class PackageReport:
    """What went into the package; recorded on the task span."""

    cover: str = "none"  # "catalog", "rendered" or "none"
    page_count: int = 0
    toc_entries: int = 0
    warnings: list[str] = field(default_factory=list)


# -- PDF inspection -------------------------------------------------------------------------


def analyze_pdf(pdf_path: str, cover_path: str | None) -> tuple[PdfInfo, bool]:
    """Read page count, Info dictionary and outline; render page 1 to `cover_path` when given.

    Returns `(info, cover_written)`. Raises when the PDF cannot be opened at all; a failed cover
    render is logged and reported as `cover_written=False`.
    """
    document = pdfium.PdfDocument(pdf_path)
    try:
        metadata = document.get_metadata_dict(skip_empty=True)
        info = PdfInfo(
            page_count=len(document),
            title=_clean(metadata.get("Title", "")),
            author=_clean(metadata.get("Author", "")),
            subject=_clean(metadata.get("Subject", "")),
            keywords=_clean(metadata.get("Keywords", "")),
            toc=_read_toc(document),
        )
        cover_written = False
        if cover_path is not None and info.page_count > 0:
            try:
                _render_page(document, 0, cover_path)
                cover_written = True
            except Exception as error:  # noqa: BLE001 - no cover is not worth failing the task
                logger.warning("Could not render the PDF's first page as cover: %s", error)
        return info, cover_written
    finally:
        document.close()


def _read_toc(document: Any) -> list[TocEntry]:
    """The PDF outline as a tree; entries without a page target are skipped with their subtree."""
    roots: list[TocEntry] = []
    stack: list[tuple[int, TocEntry]] = []
    try:
        bookmarks = list(document.get_toc(max_depth=TOC_MAX_DEPTH))
    except Exception as error:  # noqa: BLE001 - a broken outline only costs the toc
        logger.warning("Could not read the PDF outline: %s", error)
        return []

    skip_below: int | None = None
    for bookmark in bookmarks:
        level = int(bookmark.level)
        if skip_below is not None:
            if level > skip_below:
                continue
            skip_below = None
        dest = bookmark.get_dest()
        index = dest.get_index() if dest is not None else None
        title = _clean(bookmark.get_title() or "")
        if index is None or index < 0 or not title:
            skip_below = level
            continue
        entry = TocEntry(title=title, page=index + 1)
        while stack and stack[-1][0] >= level:
            stack.pop()
        (stack[-1][1].children if stack else roots).append(entry)
        stack.append((level, entry))
    return roots


def _render_page(document: Any, index: int, cover_path: str) -> None:
    page = document[index]
    try:
        width, height = page.get_size()  # PDF points, 72 per inch
        dpi = min(COVER_MAX_SIDE / max(width, height, 1.0) * 72.0, COVER_MAX_DPI)
        bitmap = page.render(scale=dpi / 72.0)
        try:
            image = bitmap.to_pil()
            image.convert("RGB").save(cover_path, format="JPEG", quality=COVER_JPEG_QUALITY)
        finally:
            bitmap.close()
    finally:
        page.close()


def prepare_catalog_cover(source: str, cover_path: str) -> None:
    """Re-encode the catalog's cover image as a JPEG of lcpencrypt's cover size."""
    with Image.open(source) as image:
        image.thumbnail((COVER_MAX_SIDE, COVER_MAX_SIDE))
        image.convert("RGB").save(cover_path, format="JPEG", quality=COVER_JPEG_QUALITY)


# -- manifest -------------------------------------------------------------------------------


def build_manifest(
    metadata: PublicationMetadata,
    info: PdfInfo,
    *,
    fallback_title: str,
    cover_href: str | None,
) -> dict[str, Any]:
    """The RWPM manifest: catalog metadata first, then the PDF's own, then lcpencrypt's fallbacks."""
    authors = metadata.authors or ([info.author] if info.author else [])
    subjects = metadata.subjects or _split_keywords(info.keywords)

    meta: dict[str, Any] = {
        "@type": "http://schema.org/Book",
        "conformsTo": RWPM_PDF_PROFILE,
        "title": metadata.title or _clean_title(info.title) or fallback_title,
    }
    optional: dict[str, Any] = {
        "identifier": metadata.identifier,
        "subtitle": metadata.subtitle,
        "description": metadata.description or info.subject,
        "language": _one_or_many(metadata.language),
        "published": metadata.published,
        "publisher": _one_or_many([metadata.publisher] if metadata.publisher else []),
        "author": _one_or_many(authors),
        "translator": _one_or_many(metadata.translators),
        "editor": _one_or_many(metadata.editors),
        "illustrator": _one_or_many(metadata.illustrators),
        "contributor": _one_or_many(metadata.contributors),
        "subject": _one_or_many(subjects),
        "numberOfPages": info.page_count,
    }
    meta |= {key: value for key, value in optional.items() if value}

    manifest: dict[str, Any] = {
        "@context": RWPM_CONTEXT,
        "metadata": meta,
        "readingOrder": [{"href": PUBLICATION_HREF, "type": "application/pdf", "title": "publication"}],
    }
    if cover_href is not None:
        manifest["resources"] = [{"rel": ["cover"], "href": cover_href, "type": "image/jpeg"}]
    if info.toc:
        manifest["toc"] = [entry.to_link() for entry in info.toc]
    return manifest


def write_package(pdf_path: str, package_path: str, manifest: dict[str, Any], cover: tuple[str, str] | None) -> None:
    """Zip the PDF, the optional `(cover_file, href)` and the manifest into `package_path`."""
    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(pdf_path, arcname=PUBLICATION_HREF)
        if cover is not None:
            cover_file, href = cover
            # Stored, as lcpencrypt does: JPEG does not deflate.
            archive.write(cover_file, arcname=href, compress_type=zipfile.ZIP_STORED)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))


def build_pdf_package(
    pdf_path: str,
    package_path: str,
    metadata: PublicationMetadata,
    *,
    cover_source: str | None = None,
    cover_name: str = "cover",
) -> PackageReport:
    """Wrap a PDF into a Readium package carrying the catalog's metadata and a cover.

    Args:
        pdf_path: Local path of the source PDF.
        package_path: Destination `.rpf`.
        metadata: Catalog metadata (wins over what the PDF says about itself).
        cover_source: Local path of the catalog's cover image; without it page 1 is rendered.
        cover_name: Stem of the cover entry. lcpencrypt stores the cover next to the publication
            under this entry's base name, so it should be the output file name (`filename` or
            `contentid`), as lcpencrypt's own PDF path names it.

    The PDF analysis and the cover are best effort: a PDF that cannot be parsed still gets a
    package with the catalog metadata, as before.
    """
    report = PackageReport()
    work_dir = tempfile.mkdtemp(prefix="cover-", dir=str(Path(package_path).parent))
    rendered_cover = str(Path(work_dir) / "rendered.jpg")
    catalog_cover = str(Path(work_dir) / "catalog.jpg")
    try:
        cover_file: str | None = None
        if cover_source is not None:
            try:
                prepare_catalog_cover(cover_source, catalog_cover)
                cover_file, report.cover = catalog_cover, "catalog"
            except Exception as error:  # noqa: BLE001 - fall back to the rendered first page
                report.warnings.append(f"catalog cover unusable: {error}")
                logger.warning("Catalog cover %s unusable, rendering the first page instead: %s", cover_source, error)

        try:
            info, rendered = analyze_pdf(pdf_path, None if cover_file else rendered_cover)
            if rendered:
                cover_file, report.cover = rendered_cover, "rendered"
        except Exception as error:  # noqa: BLE001 - metadata injection must not depend on parsing
            info = PdfInfo()
            report.warnings.append(f"PDF analysis failed: {error}")
            logger.warning("Could not analyze %s; packaging without page count, outline or cover: %s", pdf_path, error)

        href = f"{_safe_name(cover_name)}.jpg" if cover_file else None
        manifest = build_manifest(metadata, info, fallback_title=_fallback_title(pdf_path), cover_href=href)
        write_package(pdf_path, package_path, manifest, (cover_file, href) if cover_file and href else None)
        report.page_count = info.page_count
        report.toc_entries = _count(info.toc)
        return report
    finally:
        for leftover in (rendered_cover, catalog_cover):
            with contextlib.suppress(FileNotFoundError):
                os.remove(leftover)
        with contextlib.suppress(OSError):
            os.rmdir(work_dir)


# -- input locators -------------------------------------------------------------------------


def localize(locator: str, temp_dir: str, suffix: str) -> tuple[str, bool]:
    """Make a file available locally. Returns `(path, downloaded)`; the caller removes downloads."""
    if locator.startswith(("http://", "https://")):
        os.makedirs(temp_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir=temp_dir)
        os.close(fd)
        urllib.request.urlretrieve(locator, tmp_path)  # noqa: S310 - trusted catalog input
        return tmp_path, True
    return locator, False


def suffix_of(locator: str) -> str:
    path = urllib.parse.urlsplit(locator).path if "://" in locator else locator
    return Path(path).suffix.lower()


# -- normalization helpers ------------------------------------------------------------------


def _text(value: object) -> str:
    return _clean(value) if isinstance(value, str) else ""


def _strings(value: object) -> list[str]:
    """A string or a list of strings → non-empty, de-duplicated strings in order."""
    items = [value] if isinstance(value, str) else value if isinstance(value, list | tuple) else []
    seen: dict[str, None] = {}
    for item in items:
        if isinstance(item, str) and (cleaned := _clean(item)):
            seen.setdefault(cleaned, None)
    return list(seen)


def _clean(value: str) -> str:
    return value.replace("\x00", "").strip()


def _full_date(value: object) -> str:
    """`YYYY-MM-DD` from a full ISO date or date-time; empty for anything partial or invalid."""
    if not isinstance(value, str) or len(value.strip()) < 10:
        return ""
    text = value.strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def _one_or_many(values: list[str]) -> str | list[str]:
    """RWPM's string-or-array shape (lcpencrypt accepts both); empty list for none."""
    if len(values) == 1:
        return values[0]
    return values


def _split_keywords(keywords: str) -> list[str]:
    return _strings(re.split(r"[;,]", keywords)) if keywords else []


def _clean_title(title: str) -> str:
    """lcpencrypt's cleanup of embedded PDF titles: `_`, `-` and `*` become spaces."""
    return re.sub(r"[_\-*]", " ", title).strip()


def _fallback_title(pdf_path: str) -> str:
    """lcpencrypt's file-name title: the stem with `_`, `-` and `.` as spaces."""
    return re.sub(r"[_\-.]", " ", Path(pdf_path).stem).strip() or "No Title Available"


def _safe_name(name: str) -> str:
    """A cover entry name without directories (it becomes a file next to the publication)."""
    stem = Path(name).name
    stem = stem.removesuffix(Path(stem).suffix) if Path(stem).suffix else stem
    return re.sub(r"[^\w.-]", "_", stem) or "cover"


def _count(entries: list[TocEntry]) -> int:
    return sum(1 + _count(entry.children) for entry in entries)


__all__ = [
    "PackageReport",
    "PdfInfo",
    "PublicationMetadata",
    "TocEntry",
    "analyze_pdf",
    "build_manifest",
    "build_pdf_package",
    "localize",
    "suffix_of",
]
