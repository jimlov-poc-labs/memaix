# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render a mail message to PDF — email_export_pdf's engine.

Why fpdf2, and why HTML becomes text first:

- The gateway image (python:3.12-slim) has no browser, so there is no
  Chromium/Playwright to print with. fpdf2 is pure Python (its only
  dependencies are fontTools, Pillow and defusedxml) and adds a few MB,
  where WeasyPrint would need Pango/Cairo system libraries.
- fpdf2's own `write_html` is not used. It downloads every `<img src>`
  itself (urlopen, i.e. a request to whoever sent the mail — a tracking
  pixel fires), and it refuses nested tables, which is how nearly every
  receipt mail is laid out. Instead the HTML is reduced to its text here:
  script/style/head content dropped, block elements become line breaks,
  table cells on one row are joined with " | ", images become their alt
  text. Nothing in this module opens a socket, so no remote resource can
  be fetched however the mail is written.

Fonts: a Unicode TTF (DejaVu Sans, `fonts-dejavu-core` in the Dockerfile,
or MEMAIX_PDF_FONT) is used when present. Without one, fpdf2's built-in
Helvetica is used with the text folded into Latin-1 (€ -> EUR, dashes and
curly quotes to ASCII, anything else -> "?"), so export never fails on a
character, it only loses it.
"""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from pathlib import Path

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Debian/Ubuntu fonts-dejavu-core
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",  # Fedora/Alpine
    "/usr/share/fonts/TTF/DejaVuSans.ttf",  # Arch
)

# Elements whose text is not message content.
_SKIP = {"head", "title", "style", "script", "noscript", "template", "svg", "object", "iframe"}
# Elements that start and end on their own line.
_BLOCK = {
    "address", "article", "aside", "blockquote", "center", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5",
    "h6", "header", "hr", "main", "nav", "ol", "p", "pre", "section", "table", "tbody",
    "tfoot", "thead", "tr", "ul",
}
_CELL = {"td", "th"}

# Latin-1 stand-ins for common characters Helvetica cannot encode.
_FOLD = str.maketrans({
    "€": "EUR", "–": "-", "—": "-", "‒": "-", "−": "-", "‘": "'", "’": "'",
    "‚": ",", "“": '"', "”": '"', "„": '"', "…": "...", "•": "*", "·": "*",
    "\u2009": " ", "\u200b": "", "\u202f": " ", "\u2002": " ", "\u2003": " ",
    "™": "(TM)", "‹": "<", "›": ">", "✓": "v", "→": "->",
})


class _TextExtractor(HTMLParser):
    """HTML -> readable plain text, keeping the line structure of tables."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0
        self._pre_depth = 0
        self._cells_in_row = 0

    def _newline(self) -> None:
        if self._out and not self._out[-1].endswith("\n"):
            self._out.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "br":
            self._out.append("\n")
        elif tag == "tr":
            self._newline()
            self._cells_in_row = 0
        elif tag in _CELL:
            if self._cells_in_row:
                self._out.append(" | ")
            self._cells_in_row += 1
        elif tag == "li":
            self._newline()
            self._out.append("• ")
        elif tag == "img":
            alt = (dict(attrs).get("alt") or "").strip()
            if alt:
                self._out.append(f"[{alt}]")
        elif tag in _BLOCK:
            self._newline()
            if tag == "pre":
                self._pre_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in _BLOCK or tag == "li":
            self._newline()
            if tag == "pre":
                self._pre_depth = max(0, self._pre_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if not self._pre_depth:
            data = re.sub(r"\s+", " ", data)
        self._out.append(data)

    def text(self) -> str:
        return "".join(self._out)


def html_to_text(html: str) -> str:
    """Readable text of an HTML mail body."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return _tidy(parser.text())


def _tidy(text: str) -> str:
    """Trim lines, drop cell separators left on empty rows, keep at most one
    blank line in a row."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.replace("\xa0", " ").strip()
        if line and not line.strip("| "):
            line = ""
        if line.startswith("| "):
            line = line[2:]
        if line.endswith(" |"):
            line = line[:-2]
        if not line and lines and not lines[-1]:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def unicode_font_path() -> str | None:
    """A Unicode TTF to render with, or None to fall back to Helvetica."""
    override = os.environ.get("MEMAIX_PDF_FONT")
    candidates = ([override] if override else []) + list(_FONT_CANDIDATES)
    return next((p for p in candidates if p and Path(p).is_file()), None)


def _latin1(text: str) -> str:
    return text.translate(_FOLD).encode("latin-1", "replace").decode("latin-1")


def _unchanged(text: str) -> str:
    return text


def pdf_filename(subject: str) -> str:
    """A safe file name for the PDF, from the subject."""
    stem = re.sub(r"[^\w.\- ]+", "", subject, flags=re.UNICODE).strip(" .")
    stem = re.sub(r"\s+", " ", stem)[:80].strip() or "email"
    return f"{stem}.pdf"


def render_message_pdf(
    *,
    from_: str,
    to: list[str],
    cc: list[str],
    date: str,
    subject: str,
    html: str,
    text: str,
    attachment_names: list[str],
) -> tuple[bytes, str]:
    """(PDF bytes, rendered_from) for a message.

    rendered_from is "html" when the HTML body was used, "text" for the
    plain-text body and "empty" when the message had neither.
    """
    from fpdf import FPDF

    body, rendered_from = "", "empty"
    if html.strip():
        body, rendered_from = html_to_text(html), "html"
    if not body and text.strip():
        body, rendered_from = _tidy(text), "text"

    pdf = FPDF(format="A4")
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(True, margin=15)
    pdf.set_title(subject or "email")
    pdf.set_creator("Memaix")

    font_path = unicode_font_path()
    family, clean = "Helvetica", _latin1
    if font_path:
        pdf.add_font("body", "", font_path)
        family, clean = "body", _unchanged

    pdf.add_page()
    headers = [("From", from_), ("To", ", ".join(to)), ("Cc", ", ".join(cc)), ("Date", date), ("Subject", subject)]
    if attachment_names:
        headers.append(("Attachments", ", ".join(attachment_names)))
    pdf.set_font(family, size=10)
    for label, value in headers:
        if value:
            pdf.multi_cell(0, 5, clean(f"{label}: {value}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    y = pdf.get_y()
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.ln(4)
    pdf.set_font(family, size=10)
    pdf.multi_cell(0, 5, clean(body or "(no message body)"), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output()), rendered_from
