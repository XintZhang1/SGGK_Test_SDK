"""Bounded public Doxygen evidence for external-profile adaptation prompts.

The shipped Doxygen HTML under ``<sdk>/docs/html`` is public-interface
material (classification ``public_interface``), so bounded brief descriptions
may reach an external Message API profile.  This extractor is deliberately
narrow: it reads only member index rows (``memitem``/``memdesc``) for one
function name, skips ``*_source.html`` pages entirely (they embed raw header
text, which stays host-local), caps every brief, SHA-256-pins the text, and
returns opaque ``doc_*`` references with page basenames only.
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

MAX_BRIEF_CHARS = 2000
MAX_PAGES = 512
MAX_PAGE_BYTES = 4 * 1024 * 1024
DEFAULT_LIMIT = 4
_ROW_CLASS_RE = re.compile(r"\b(memitem|memdesc):([A-Za-z0-9]+)\b")


class _MemberRowParser(HTMLParser):
    """Collect member index rows and their brief description rows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self._row_kind = ""
        self._row_id = ""
        self._anchor_depth = 0
        self._anchors: list[str] = []
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row_kind = ""
            self._row_id = ""
            self._anchors = []
            self._text_parts = []
            for key, value in attrs:
                if key != "class" or not value:
                    continue
                match = _ROW_CLASS_RE.search(value)
                if match:
                    self._row_kind = match.group(1)
                    self._row_id = match.group(2)
                    break
        elif tag == "a" and self._row_kind:
            self._anchor_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor_depth:
            self._anchor_depth -= 1
        elif tag == "tr" and self._row_kind:
            self.rows.append(
                {
                    "kind": self._row_kind,
                    "row_id": self._row_id,
                    "anchors": list(self._anchors),
                    "text": " ".join("".join(self._text_parts).split()),
                }
            )
            self._row_kind = ""
            self._row_id = ""
            self._anchors = []
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if not self._row_kind:
            return
        self._text_parts.append(data)
        if self._anchor_depth:
            self._anchors.append(data.strip())


def _page_briefs(page_text: str, function_name: str) -> list[str]:
    parser = _MemberRowParser()
    parser.feed(page_text)
    wanted_ids: set[str] = set()
    qualified = f"sggk::{function_name}"
    for row in parser.rows:
        if row["kind"] != "memitem":
            continue
        anchors = {anchor for anchor in row["anchors"] if anchor}
        if function_name in anchors or qualified in anchors:
            wanted_ids.add(row["row_id"])
    briefs: list[str] = []
    for row in parser.rows:
        if row["kind"] == "memdesc" and row["row_id"] in wanted_ids and row["text"]:
            briefs.append(row["text"])
    return briefs


def discover_public_doc_evidence(
    function_name: str,
    docs_root: Path | None,
    *,
    limit: int = DEFAULT_LIMIT,
    max_brief_chars: int = MAX_BRIEF_CHARS,
) -> list[dict[str, Any]]:
    """Return bounded, hash-pinned Doxygen briefs for one public function.

    The result is empty when the docs tree is absent or the function is not
    documented; absence of evidence is never an error.
    """

    if not isinstance(function_name, str) or not function_name:
        return []
    if docs_root is None:
        return []
    root = Path(docs_root)
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    scanned = 0
    for page in sorted(root.glob("*.html"), key=lambda item: item.name.casefold()):
        if len(records) >= limit or scanned >= MAX_PAGES:
            break
        if page.name.endswith("_source.html"):
            continue
        scanned += 1
        try:
            if page.stat().st_size > MAX_PAGE_BYTES:
                continue
            text = page.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if function_name not in text:
            continue
        try:
            briefs = _page_briefs(text, function_name)
        except Exception:  # noqa: BLE001 - malformed vendor HTML must not break discovery
            continue
        for brief in briefs:
            if len(records) >= limit:
                break
            capped = brief[:max_brief_chars]
            records.append(
                {
                    "doc_ref_id": f"doc_{len(records) + 1:03d}",
                    "page": page.name,
                    "brief": capped,
                    "brief_sha256": hashlib.sha256(capped.encode("utf-8")).hexdigest(),
                }
            )
    return records
