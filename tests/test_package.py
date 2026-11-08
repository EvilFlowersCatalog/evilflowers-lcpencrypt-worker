"""Readium package building for raw PDFs (issues #1 and #2)."""

import io
import json
import zipfile
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from evilflowers_lcpencrypt_worker.package import (
    RWPM_PDF_PROFILE,
    PdfInfo,
    PublicationMetadata,
    TocEntry,
    _safe_name,
    analyze_pdf,
    build_manifest,
    build_pdf_package,
)
from tests.pdf_factory import Bookmark, write_pdf

if TYPE_CHECKING:
    from pathlib import Path


def _walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _assert_lcpencrypt_safe(manifest: dict, names: list[str]) -> None:
    """The shapes lcpencrypt's Go decoder needs (see the package module docstring)."""
    assert None not in list(_walk(manifest)), "null breaks lcpencrypt's decoder"
    assert isinstance(manifest["metadata"]["title"], str)
    assert manifest["metadata"]["conformsTo"] == RWPM_PDF_PROFILE
    assert manifest["readingOrder"][0]["href"] == "publication.pdf"
    assert manifest["readingOrder"][0]["type"] == "application/pdf"
    for resource in manifest.get("resources", []):
        assert resource["href"] in names


def _read(package: Path) -> tuple[list[str], dict, zipfile.ZipFile]:
    archive = zipfile.ZipFile(package)
    return archive.namelist(), json.loads(archive.read("manifest.json")), archive


@pytest.fixture
def real_pdf(tmp_path):
    return write_pdf(
        tmp_path / "real_book-v2.pdf",
        pages=3,
        info={"Title": "Embedded_Title", "Author": "Embedded Author", "Subject": "Embedded subject"},
        outline=[
            Bookmark("Chapter 1", 0, [Bookmark("Section 1.1", 1)]),
            Bookmark("Broken", -1, [Bookmark("Child of broken", 2)]),
            Bookmark("Chapter 2", 2),
        ],
    )


class TestPublicationMetadata:
    def test_authors_win_over_author(self):
        metadata = PublicationMetadata.from_params({"input_file": "x.pdf", "author": "A", "authors": ["B", "C"]})
        assert metadata.authors == ["B", "C"]

    def test_single_author_still_accepted(self):
        assert PublicationMetadata.from_params({"input_file": "x.pdf", "author": " Ada "}).authors == ["Ada"]

    def test_lists_are_cleaned_and_deduplicated(self):
        metadata = PublicationMetadata.from_params(
            {"input_file": "x.pdf", "authors": ["Ada", "", "  ", "Ada", "Bob\x00"], "subjects": ["Math", "Math"]}
        )
        assert metadata.authors == ["Ada", "Bob"]
        assert metadata.subjects == ["Math"]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2023-05-17", "2023-05-17"),
            ("2023-05-17T10:00:00Z", "2023-05-17"),
            ("2023-05-17T10:00:00+02:00", "2023-05-17"),
            ("2023", ""),
            ("2023-05", ""),
            ("17.05.2023", ""),
            ("", ""),
        ],
    )
    def test_published_must_be_a_full_date(self, value, expected):
        assert PublicationMetadata.from_params({"input_file": "x.pdf", "published": value}).published == expected

    def test_language_string_or_list(self):
        assert PublicationMetadata.from_params({"input_file": "x.pdf", "language": "sk"}).language == ["sk"]
        assert PublicationMetadata.from_params({"input_file": "x.pdf", "language": ["sk", "en"]}).language == [
            "sk",
            "en",
        ]

    def test_is_empty(self):
        assert PublicationMetadata.from_params({"input_file": "x.pdf"}).is_empty()
        assert PublicationMetadata.from_params({"input_file": "x.pdf", "title": "  "}).is_empty()
        assert not PublicationMetadata.from_params({"input_file": "x.pdf", "publisher": "STU"}).is_empty()


class TestBuildManifest:
    def test_full_metadata(self):
        metadata = PublicationMetadata(
            title="Title",
            subtitle="Sub",
            authors=["Ada", "Bob"],
            translators=["Tom"],
            publisher="STU Press",
            language=["sk"],
            published="2020-01-31",
            description="About",
            subjects=["Math", "Logic"],
            identifier="urn:isbn:9780000000000",
        )
        info = PdfInfo(page_count=42, toc=[TocEntry("One", 1, [TocEntry("One.One", 2)])])

        manifest = build_manifest(metadata, info, fallback_title="file", cover_href="abc.jpg")

        meta = manifest["metadata"]
        assert meta["title"] == "Title"
        assert meta["subtitle"] == "Sub"
        assert meta["author"] == ["Ada", "Bob"]
        assert meta["translator"] == "Tom"
        assert meta["publisher"] == "STU Press"
        assert meta["language"] == "sk"
        assert meta["published"] == "2020-01-31"
        assert meta["description"] == "About"
        assert meta["subject"] == ["Math", "Logic"]
        assert meta["identifier"] == "urn:isbn:9780000000000"
        assert meta["numberOfPages"] == 42
        assert manifest["resources"] == [{"rel": ["cover"], "href": "abc.jpg", "type": "image/jpeg"}]
        assert manifest["toc"] == [
            {
                "href": "publication.pdf#page=1",
                "title": "One",
                "children": [{"href": "publication.pdf#page=2", "title": "One.One"}],
            }
        ]
        _assert_lcpencrypt_safe(manifest, ["abc.jpg"])

    def test_empty_values_are_omitted(self):
        manifest = build_manifest(PublicationMetadata(title="T"), PdfInfo(), fallback_title="f", cover_href=None)

        assert set(manifest["metadata"]) == {"@type", "conformsTo", "title"}
        assert "resources" not in manifest
        assert "toc" not in manifest
        _assert_lcpencrypt_safe(manifest, [])

    def test_pdf_metadata_fills_the_gaps(self):
        info = PdfInfo(title="My_Book-Title*", author="PDF Author", subject="PDF subject", keywords="a; b, a")

        meta = build_manifest(PublicationMetadata(), info, fallback_title="f", cover_href=None)["metadata"]

        # lcpencrypt's own title cleanup
        assert meta["title"] == "My Book Title"
        assert meta["author"] == "PDF Author"
        assert meta["description"] == "PDF subject"
        assert meta["subject"] == ["a", "b"]

    def test_catalog_metadata_wins(self):
        info = PdfInfo(title="PDF title", author="PDF Author", subject="PDF subject")
        metadata = PublicationMetadata(title="Catalog", authors=["Catalog Author"], description="Catalog desc")

        meta = build_manifest(metadata, info, fallback_title="f", cover_href=None)["metadata"]

        assert (meta["title"], meta["author"], meta["description"]) == ("Catalog", "Catalog Author", "Catalog desc")

    def test_file_name_fallback_title(self):
        meta = build_manifest(PublicationMetadata(), PdfInfo(), fallback_title="the book", cover_href=None)
        assert meta["metadata"]["title"] == "the book"


class TestAnalyzePdf:
    def test_reads_pages_info_and_outline(self, real_pdf, tmp_path):
        cover = tmp_path / "cover.jpg"

        info, written = analyze_pdf(str(real_pdf), str(cover))

        assert info.page_count == 3
        assert (info.title, info.author, info.subject) == ("Embedded_Title", "Embedded Author", "Embedded subject")
        # The bookmark without a destination is dropped together with its subtree.
        assert info.toc == [TocEntry("Chapter 1", 1, [TocEntry("Section 1.1", 2)]), TocEntry("Chapter 2", 3)]
        assert written
        with Image.open(cover) as image:
            assert image.format == "JPEG"
            # 200x300 pt page, longest side rendered to 1200 px (288 DPI, under the 300 cap)
            assert image.size == (800, 1200)

    def test_tiny_page_is_capped_at_300_dpi(self, tmp_path):
        pdf = write_pdf(tmp_path / "tiny.pdf", size=(72, 72))
        cover = tmp_path / "cover.jpg"

        analyze_pdf(str(pdf), str(cover))

        with Image.open(cover) as image:
            assert image.size == (300, 300)

    def test_without_cover_path_nothing_is_rendered(self, real_pdf):
        info, written = analyze_pdf(str(real_pdf), None)
        assert info.page_count == 3
        assert not written

    def test_unparseable_pdf_raises(self, sample_pdf, tmp_path):
        with pytest.raises(Exception):  # noqa: B017 - pdfium's error type is an implementation detail
            analyze_pdf(sample_pdf, str(tmp_path / "cover.jpg"))


class TestBuildPdfPackage:
    def test_real_pdf_gets_cover_pages_and_toc(self, real_pdf, tmp_path):
        package = tmp_path / "out.rpf"
        metadata = PublicationMetadata(title="Catalog Title", authors=["Ada"])

        report = build_pdf_package(str(real_pdf), str(package), metadata, cover_name="abc-123")

        names, manifest, archive = _read(package)
        assert set(names) == {"publication.pdf", "abc-123.jpg", "manifest.json"}
        assert archive.read("publication.pdf") == real_pdf.read_bytes()
        assert archive.getinfo("abc-123.jpg").compress_type == zipfile.ZIP_STORED
        meta = manifest["metadata"]
        assert meta["title"] == "Catalog Title"
        assert meta["author"] == "Ada"
        assert meta["numberOfPages"] == 3
        assert meta["description"] == "Embedded subject"
        assert manifest["resources"][0]["href"] == "abc-123.jpg"
        assert len(manifest["toc"]) == 2
        assert (report.cover, report.page_count, report.toc_entries) == ("rendered", 3, 3)
        _assert_lcpencrypt_safe(manifest, names)

    def test_catalog_cover_wins_over_rendered_page(self, real_pdf, tmp_path):
        source = tmp_path / "cover.png"
        Image.new("RGBA", (2400, 1200), (255, 0, 0, 255)).save(source)
        package = tmp_path / "out.rpf"

        report = build_pdf_package(
            str(real_pdf), str(package), PublicationMetadata(title="T"), cover_source=str(source), cover_name="id"
        )

        names, manifest, archive = _read(package)
        assert report.cover == "catalog"
        with Image.open(io.BytesIO(archive.read("id.jpg"))) as image:
            assert image.format == "JPEG"
            assert image.size == (1200, 600)
        _assert_lcpencrypt_safe(manifest, names)

    def test_broken_catalog_cover_falls_back_to_first_page(self, real_pdf, tmp_path):
        source = tmp_path / "cover.png"
        source.write_bytes(b"not an image")
        package = tmp_path / "out.rpf"

        report = build_pdf_package(
            str(real_pdf), str(package), PublicationMetadata(title="T"), cover_source=str(source), cover_name="id"
        )

        assert report.cover == "rendered"
        assert report.warnings
        assert "id.jpg" in zipfile.ZipFile(package).namelist()

    def test_unparseable_pdf_still_carries_the_metadata(self, sample_pdf, tmp_path):
        package = tmp_path / "out.rpf"

        report = build_pdf_package(sample_pdf, str(package), PublicationMetadata(title="T", authors=["A"]))

        names, manifest, _ = _read(package)
        assert set(names) == {"publication.pdf", "manifest.json"}
        assert manifest["metadata"]["title"] == "T"
        assert manifest["metadata"]["author"] == "A"
        assert "numberOfPages" not in manifest["metadata"]
        assert "resources" not in manifest
        assert report.cover == "none"
        assert report.warnings
        _assert_lcpencrypt_safe(manifest, names)

    def test_missing_title_falls_back_to_cleaned_file_name(self, tmp_path):
        pdf = tmp_path / "the_great-book.v2.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        package = tmp_path / "out.rpf"

        build_pdf_package(str(pdf), str(package), PublicationMetadata(authors=["A"]))

        assert _read(package)[1]["metadata"]["title"] == "the great book v2"

    def test_unicode_metadata_preserved(self, real_pdf, tmp_path):
        package = tmp_path / "out.rpf"

        build_pdf_package(str(real_pdf), str(package), PublicationMetadata(title="Žižek: Naše", authors=["Šimon Ő"]))

        meta = _read(package)[1]["metadata"]
        assert (meta["title"], meta["author"]) == ("Žižek: Naše", "Šimon Ő")

    def test_leaves_no_working_files(self, real_pdf, tmp_path):
        out_dir = tmp_path / "pkg"
        out_dir.mkdir()

        build_pdf_package(str(real_pdf), str(out_dir / "out.rpf"), PublicationMetadata(title="T"))

        assert [path.name for path in out_dir.iterdir()] == ["out.rpf"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("abc-123", "abc-123"),
        ("encrypted/abc-123.lcp.pdf", "abc-123.lcp"),
        ("book.pdf", "book"),
        ("weird name/ü?.x", "ü_"),
        ("", "cover"),
    ],
)
def test_safe_name(name, expected):
    assert _safe_name(name) == expected
