"""Preview the Readium package the worker builds for a raw PDF, from the command line (issue #4).

    python -m evilflowers_lcpencrypt_worker.preview book.pdf --title … --author … --out preview/

Builds the package exactly as the encryption task does (`package.preview_pdf_package`), prints
its manifest and writes the manifest and the cover (with `--package` also the `.rpf`) into
`--out`. Nothing is encrypted and nothing is sent anywhere. Kept out of `package.py` so that
`python -m` does not re-run a module the package has already imported.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from evilflowers_lcpencrypt_worker.package import (
    PublicationMetadata,
    build_pdf_package,
    localize,
    preview_pdf_package,
    suffix_of,
)

if TYPE_CHECKING:
    from evilflowers_lcpencrypt_worker.types import LCPEncryptParams

# CLI destinations that are task metadata parameters of the same name.
_METADATA_KEYS = (
    *("title", "subtitle", "authors", "translators", "editors", "illustrators", "contributors"),
    *("publisher", "language", "published", "description", "subjects", "identifier"),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evilflowers_lcpencrypt_worker.preview",
        description="Preview the Readium package the worker builds for a raw PDF (nothing is encrypted).",
    )
    parser.add_argument("pdf", help="local path or http(s) URL of the PDF")
    parser.add_argument("--title")
    parser.add_argument("--subtitle")
    parser.add_argument("--author", dest="authors", action="append", help="repeat for several authors")
    parser.add_argument("--translator", dest="translators", action="append")
    parser.add_argument("--editor", dest="editors", action="append")
    parser.add_argument("--illustrator", dest="illustrators", action="append")
    parser.add_argument("--contributor", dest="contributors", action="append")
    parser.add_argument("--publisher")
    parser.add_argument("--language", action="append", help="BCP 47; repeat for several")
    parser.add_argument("--published", help="YYYY-MM-DD (partial dates are dropped, as in the task)")
    parser.add_argument("--description")
    parser.add_argument("--subject", dest="subjects", action="append")
    parser.add_argument("--identifier", help="urn:isbn:… or https://doi.org/…")
    parser.add_argument("--cover", help="catalog cover image (path or URL); default: page 1 rendered")
    parser.add_argument("--name", default="cover", help="cover entry stem; the task uses filename or contentid")
    parser.add_argument("--out", default=".", help="directory for manifest.json and the cover (default: .)")
    parser.add_argument("--package", action="store_true", help="also write the .rpf the task would encrypt")
    args = parser.parse_args(argv)

    params: LCPEncryptParams = {"input_file": args.pdf}
    params.update({key: value for key in _METADATA_KEYS if (value := getattr(args, key))})  # type: ignore[typeddict-item]
    metadata = PublicationMetadata.from_params(params)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lcp-cli-") as temp_dir:
        pdf, _ = localize(args.pdf, temp_dir, ".pdf")
        cover = localize(args.cover, temp_dir, suffix_of(args.cover) or ".img")[0] if args.cover else None
        preview = preview_pdf_package(pdf, metadata, cover_source=cover, cover_name=args.name, work_dir=temp_dir)
        (out / "manifest.json").write_text(json.dumps(preview.manifest, ensure_ascii=False, indent=2) + "\n")
        if preview.cover is not None and preview.cover_href is not None:
            (out / preview.cover_href).write_bytes(preview.cover)
        if args.package:
            package_path = str(out / f"{Path(args.pdf.split('?')[0]).stem or 'book'}.rpf")
            build_pdf_package(pdf, package_path, metadata, cover_source=cover, cover_name=args.name)

    print(json.dumps(preview.manifest, ensure_ascii=False, indent=2))
    report = preview.report
    print(f"cover={report.cover} pages={report.page_count} toc_entries={report.toc_entries} -> {out}", file=sys.stderr)
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
