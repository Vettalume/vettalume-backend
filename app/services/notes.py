"""Split a concept's HTML notes into contiguous sections for chunked/windowed delivery.

The student notes viewer only ever holds a couple of sections in the DOM, so the full body is never
delivered (or present) in one response — a much stronger anti-scrape posture than shipping the whole
HTML at once.

Guarantees:
- Sections are cut ONLY at element boundaries, so each section is valid HTML.
- Concatenating the sections in order reproduces the (newline-normalized) body exactly.
- An oversized container element (e.g. everything wrapped in one <div>) is split recursively by
  descending into its children and re-wrapping each chunk in the element's own tags, so no single
  section is much larger than the target.
- Short bodies return a single section; malformed input degrades to a single section (never crashes).
"""
from __future__ import annotations

from html.parser import HTMLParser

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
         "link", "meta", "param", "source", "track", "wbr"}
_MAX_DEPTH = 5


class _Spans(HTMLParser):
    """Records the char span (start, end, tag) of every top-level element (depth 0)."""

    def __init__(self, html: str, line_offsets: list[int]):
        super().__init__(convert_charrefs=False)
        self._html = html
        self._offs = line_offsets
        self._depth = 0
        self._el_start = 0
        self._el_tag = ""
        self.spans: list[tuple[int, int, str]] = []

    def _pos(self) -> int:
        line, col = self.getpos()
        return self._offs[line - 1] + col

    def _after_gt(self, start: int) -> int:
        gt = self._html.find(">", start)
        return (gt + 1) if gt != -1 else len(self._html)

    def handle_starttag(self, tag, attrs):
        if tag.lower() in _VOID:
            if self._depth == 0:
                p = self._pos()
                self.spans.append((p, self._after_gt(p), tag))
            return
        if self._depth == 0:
            self._el_start = self._pos()
            self._el_tag = tag
        self._depth += 1

    def handle_startendtag(self, tag, attrs):
        if self._depth == 0:
            p = self._pos()
            self.spans.append((p, self._after_gt(p), tag))

    def handle_endtag(self, tag):
        if tag.lower() in _VOID:
            return
        if self._depth > 0:
            self._depth -= 1
            if self._depth == 0:
                self.spans.append((self._el_start, self._after_gt(self._pos()), self._el_tag))


def _units(text: str) -> list[tuple[str, int, int, str]]:
    """Top-level document order: interleaved ("text"|"el", start, end, tag) covering all of `text`."""
    offs = [0]
    for line in text.split("\n"):
        offs.append(offs[-1] + len(line) + 1)
    p = _Spans(text, offs)
    p.feed(text)
    p.close()
    spans = sorted(p.spans)
    units: list[tuple[str, int, int, str]] = []
    pos = 0
    for (s, e, tag) in spans:
        if s < pos:  # overlapping/degenerate — bail to a single unit
            return [("text", 0, len(text), "")]
        if s > pos:
            units.append(("text", pos, s, ""))
        units.append(("el", s, e, tag))
        pos = e
    if pos < len(text):
        units.append(("text", pos, len(text), ""))
    return units


def _is_container(seg: str, tag: str) -> bool:
    return tag.lower() not in _VOID and ("</" + tag.lower()) in seg.lower()


def split_html(html: str, target_chars: int = 3500, _depth: int = 0) -> list[str]:
    if not html:
        return [""]
    text = html.replace("\r\n", "\n").replace("\r", "\n") if _depth == 0 else html
    if len(text.strip()) <= target_chars:
        return [text]

    try:
        units = _units(text)
    except Exception:
        return [text]

    sections: list[str] = []
    buf_from = 0

    def flush_gap(upto: int):
        gap = text[buf_from:upto]
        if gap.strip():
            sections.append(gap)
        elif gap and sections:
            sections[-1] += gap

    for (kind, s, e, tag) in units:
        seg = text[s:e]
        if (kind == "el" and (e - s) > target_chars * 2
                and _depth < _MAX_DEPTH and _is_container(seg, tag)):
            # Descend into this oversized wrapper: split its children, re-wrap each chunk.
            flush_gap(s)
            open_end = seg.find(">") + 1
            close_start = seg.rfind("</")
            if 0 < open_end <= close_start:
                open_tag, inner, close_tag = seg[:open_end], seg[open_end:close_start], seg[close_start:]
                for sub in split_html(inner, target_chars, _depth + 1):
                    sections.append(open_tag + sub + close_tag)
            else:
                sections.append(seg)
            buf_from = e
        elif (e - buf_from) >= target_chars:
            sections.append(text[buf_from:e])
            buf_from = e

    tail = text[buf_from:]
    if tail.strip():
        sections.append(tail)
    elif tail and sections:
        sections[-1] += tail
    return sections or [text]
