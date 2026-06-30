"""Allowlist HTML sanitizer (Phase 16) — stdlib only, no third-party deps.

Concept content is authored by admins but rendered into students' browsers, so it must be sanitized
to prevent stored XSS. This keeps safe formatting tags (headings, lists, tables, images, links) and
strips anything executable: <script>/<style>/<iframe>, on* event handlers, and javascript: URLs.
"""
from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser

# Tags we keep (their text + safe attributes are preserved)
ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "code",
    "span", "div", "strong", "b", "em", "i", "u", "s", "strike", "mark", "small", "sub", "sup",
    "ul", "ol", "li", "dl", "dt", "dd", "a", "img", "figure", "figcaption",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
}
VOID = {"br", "hr", "img", "col"}                       # no closing tag
# Dangerous tags whose entire subtree is removed (content too). NOTE: <style> is handled specially
# below (its CSS is kept but sanitized) so authored colours/fonts survive.
DROP_TREE = {"script", "iframe", "object", "embed", "applet", "form", "button",
             "select", "textarea", "svg", "math", "noscript", "title", "frameset",
             "template"}
# Dangerous void tags — skip the single tag (entering drop mode would never end)
DROP_VOID = {"meta", "link", "base", "input", "frame", "param", "source", "track"}
ALLOWED_ATTRS = {"class", "id", "style", "title", "dir", "lang", "align", "valign", "width", "height",
                 "colspan", "rowspan", "span", "start", "type", "alt", "href", "src", "target", "rel"}
_BAD_URL = ("javascript:", "vbscript:", "data:text/html")
_BAD_CSS = ("javascript:", "expression(", "behavior:", "@import", "-moz-binding", "url(javascript")


def sanitize_css(css: str) -> str:
    """Neutralise the handful of CSS constructs that can execute or fetch abusively, while leaving
    colours, fonts, and layout intact. Defence-in-depth — the preview also renders CSS in a sandbox."""
    out = re.sub(r"(?i)expression\s*\(", "expr_blocked(", css or "")
    out = re.sub(r"(?i)javascript\s*:", "", out)
    out = re.sub(r"(?i)@import[^;]*;?", "", out)
    out = re.sub(r"(?i)behavior\s*:[^;]*;?", "", out)
    out = re.sub(r"(?i)-moz-binding[^;]*;?", "", out)
    out = re.sub(r"(?i)url\s*\(\s*['\"]?\s*javascript:", "url(", out)
    return out


def _url_ok(v: str) -> bool:
    s = re.sub(r"[\s\x00-\x1f]+", "", v or "").lower()        # browsers ignore embedded whitespace
    return not s.startswith(_BAD_URL)


def _style_ok(v: str) -> bool:
    s = (v or "").lower()
    return not any(b in s for b in _BAD_CSS)


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.drop = 0                                          # depth of a dropped subtree
        self.in_style = False                                  # inside a <style> block (keep its CSS)

    def _start(self, tag: str, attrs, self_close: bool) -> None:
        tag = tag.lower()
        if self.drop:
            if not self_close and tag in DROP_TREE:
                self.drop += 1
            return
        if tag == "style":
            self.out.append("<style>")
            if not self_close:
                self.in_style = True
            return
        if tag in DROP_VOID:
            return
        if tag in DROP_TREE:
            if not self_close:
                self.drop = 1
            return
        if tag not in ALLOWED_TAGS:
            return                                             # unwrap: drop tag, keep children
        safe = []
        for k, v in attrs:
            k = (k or "").lower()
            if k.startswith("on") or k not in ALLOWED_ATTRS:
                continue
            if v is None:
                safe.append(k)
                continue
            if k in ("href", "src") and not _url_ok(v):
                continue
            if k == "style" and not _style_ok(v):
                continue
            safe.append('%s="%s"' % (k, _html.escape(v, quote=True)))
        if tag == "a" and any(a.startswith("target=") for a in safe) \
                and not any(a.startswith("rel=") for a in safe):
            safe.append('rel="noopener noreferrer"')
        self.out.append("<%s%s>" % (tag, (" " + " ".join(safe)) if safe else ""))

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, True)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.drop:
            if tag in DROP_TREE:
                self.drop -= 1
            return
        if tag == "style" and self.in_style:
            self.out.append("</style>")
            self.in_style = False
            return
        if tag in VOID or tag not in ALLOWED_TAGS:
            return
        self.out.append("</%s>" % tag)

    def handle_data(self, data):
        if self.drop:
            return
        if self.in_style:
            self.out.append(sanitize_css(data))               # keep CSS, neutralise dangerous bits
            return
        self.out.append(_html.escape(data))

    def handle_comment(self, data):
        pass


def sanitize_html(html_text: str, max_len: int = 800_000) -> str:
    """Return a sanitized copy of html_text safe to render into a student's browser."""
    if not html_text:
        return ""
    p = _Sanitizer()
    p.feed(html_text[:max_len].lstrip("\ufeff"))
    p.close()
    return "".join(p.out).strip()
