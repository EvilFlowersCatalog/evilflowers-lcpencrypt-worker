"""Write small but valid PDFs (pages, Info dictionary, outline) without a PDF library."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class Bookmark:
    title: str
    page: int  # 0-based page index; -1 = no destination
    children: list[Bookmark] = field(default_factory=list)


def _literal(text: str) -> str:
    """A PDF text string: UTF-16BE with BOM, hex encoded (safe for any character)."""
    return "<FEFF" + text.encode("utf-16-be").hex().upper() + ">"


def write_pdf(
    path: Path,
    *,
    pages: int = 1,
    size: tuple[int, int] = (200, 300),
    info: dict[str, str] | None = None,
    outline: list[Bookmark] | None = None,
) -> Path:
    objects: dict[int, str] = {}
    next_id = 3  # 1 = catalog, 2 = page tree

    page_ids = []
    for index in range(pages):
        content_id, page_id = next_id, next_id + 1
        next_id += 2
        stream = f"0 0 1 rg 20 20 {100 + index} 100 re f"
        objects[content_id] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {size[0]} {size[1]}] /Contents {content_id} 0 R >>"
        )
        page_ids.append(page_id)
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {pages} >>"

    catalog = "<< /Type /Catalog /Pages 2 0 R"
    if outline:
        outlines_id = next_id
        next_id += 1

        def emit(items: list[Bookmark], parent: int) -> tuple[int, int, int]:
            nonlocal next_id
            ids = list(range(next_id, next_id + len(items)))
            next_id += len(items)
            total = 0
            for position, (item, item_id) in enumerate(zip(items, ids, strict=True)):
                parts = [f"/Title {_literal(item.title)}", f"/Parent {parent} 0 R"]
                if item.page >= 0:
                    parts.append(f"/Dest [{page_ids[item.page]} 0 R /Fit]")
                if position > 0:
                    parts.append(f"/Prev {ids[position - 1]} 0 R")
                if position < len(ids) - 1:
                    parts.append(f"/Next {ids[position + 1]} 0 R")
                if item.children:
                    first, last, count = emit(item.children, item_id)
                    parts += [f"/First {first} 0 R", f"/Last {last} 0 R", f"/Count {count}"]
                    total += count
                objects[item_id] = "<< " + " ".join(parts) + " >>"
                total += 1
            return ids[0], ids[-1], total

        first, last, count = emit(outline, outlines_id)
        objects[outlines_id] = f"<< /Type /Outlines /First {first} 0 R /Last {last} 0 R /Count {count} >>"
        catalog += f" /Outlines {outlines_id} 0 R"
    objects[1] = catalog + " >>"

    info_id = None
    if info:
        info_id = next_id
        next_id += 1
        objects[info_id] = "<< " + " ".join(f"/{key} {_literal(value)}" for key, value in info.items()) + " >>"

    body = bytearray(b"%PDF-1.7\n")
    offsets: dict[int, int] = {}
    for object_id in sorted(objects):
        offsets[object_id] = len(body)
        body += f"{object_id} 0 obj\n{objects[object_id]}\nendobj\n".encode("latin-1")
    xref_at = len(body)
    size_total = max(objects) + 1
    body += f"xref\n0 {size_total}\n0000000000 65535 f \n".encode()
    for object_id in range(1, size_total):
        body += f"{offsets[object_id]:010d} 00000 n \n".encode()
    trailer = f"<< /Size {size_total} /Root 1 0 R" + (f" /Info {info_id} 0 R" if info_id else "") + " >>"
    body += f"trailer\n{trailer}\nstartxref\n{xref_at}\n%%EOF\n".encode()
    path.write_bytes(bytes(body))
    return path
