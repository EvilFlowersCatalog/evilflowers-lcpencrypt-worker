"""`python -m evilflowers_lcpencrypt_worker.preview` (issue #4)."""

import json
import subprocess
import sys
import zipfile
from typing import TYPE_CHECKING

from evilflowers_lcpencrypt_worker.package import PublicationMetadata, preview_pdf_package
from evilflowers_lcpencrypt_worker.preview import main
from tests.pdf_factory import write_pdf

if TYPE_CHECKING:
    from pathlib import Path


class TestCommandLine:
    def test_writes_manifest_and_cover(self, tmp_path: Path, capsys):
        pdf = write_pdf(tmp_path / "book.pdf", pages=3)
        out = tmp_path / "out"

        metadata = ["--title", "Titul", "--author", "A", "--author", "B", "--language", "sk", "--published", "2021"]
        code = main([str(pdf), *metadata, "--name", "cid", "--out", str(out)])

        assert code == 0
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["metadata"]["title"] == "Titul"
        assert manifest["metadata"]["author"] == ["A", "B"]
        assert manifest["metadata"]["language"] == "sk"
        assert "published" not in manifest["metadata"]  # partial dates dropped, as in the task
        assert (out / "cid.jpg").read_bytes().startswith(b"\xff\xd8")
        captured = capsys.readouterr()
        assert json.loads(captured.out) == manifest
        assert "cover=rendered pages=3" in captured.err

    def test_same_manifest_as_the_task_builder(self, tmp_path: Path, capsys):
        pdf = write_pdf(tmp_path / "book.pdf", pages=2)

        main([str(pdf), "--title", "T", "--publisher", "STU", "--name", "cid", "--out", str(tmp_path / "out")])

        expected = preview_pdf_package(str(pdf), PublicationMetadata(title="T", publisher="STU"), cover_name="cid")
        assert json.loads((tmp_path / "out" / "manifest.json").read_text()) == expected.manifest

    def test_package_flag_writes_the_rpf(self, tmp_path: Path, capsys):
        pdf = write_pdf(tmp_path / "book.pdf")

        main([str(pdf), "--title", "T", "--out", str(tmp_path / "out"), "--package"])

        with zipfile.ZipFile(tmp_path / "out" / "book.rpf") as archive:
            assert set(archive.namelist()) == {"publication.pdf", "cover.jpg", "manifest.json"}

    def test_runs_as_a_module_without_warnings(self, tmp_path: Path):
        """`python -m` on a module the package imports would re-execute it (runpy RuntimeWarning)."""
        pdf = write_pdf(tmp_path / "book.pdf")

        module = [sys.executable, "-W", "error::RuntimeWarning", "-m", "evilflowers_lcpencrypt_worker.preview"]
        completed = subprocess.run(
            [*module, str(pdf), "--title", "T", "--out", str(tmp_path / "out")],
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        assert "RuntimeWarning" not in completed.stderr
        assert json.loads(completed.stdout)["metadata"]["title"] == "T"
