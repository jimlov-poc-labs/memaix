# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document generation — render a report as a real .odt file, written to
Nextcloud via WebDAV (FEATURE-NEXTCLOUD-BACKEND.md §8, Byggordning steg 7).

Lowest-priority piece of the Nextcloud spec by its own admission ("lågt
prioriterat men högt upplevt värde för intressent-kommunikation") — kept
deliberately simple, matching "en enkel mall": no template engine, no
python-docx/odfpy dependency. An .odt is just a zip archive of a few XML
files; building one by hand is ~60 lines and keeps this feature free of a
new dependency for something this narrow. If richer formatting (tables,
styles, images) is ever needed, reach for odfpy then — not preemptively.

v1 scope: title + a flat list of (heading, paragraphs) sections — exactly
what pm_report()'s rollup already produces per section (milestones/
variance/raid/utilization), nothing more elaborate.
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from xml.sax.saxutils import escape  # nosec B406 -- escapes text INTO xml output, never parses untrusted xml

_MIMETYPE = b"application/vnd.oasis.opendocument.text"

_MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">
  <manifest:file-entry manifest:full-path="/" manifest:version="1.2" manifest:media-type="application/vnd.oasis.opendocument.text"/>
  <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
</manifest:manifest>
"""

_CONTENT_HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
    xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
    xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
    office:version="1.2">
  <office:body>
    <office:text>
"""

_CONTENT_FOOTER = """    </office:text>
  </office:body>
</office:document-content>
"""


def render_odt(title: str, sections: list[dict]) -> bytes:
    """sections: [{"heading": str, "paragraphs": [str, ...]}, ...].
    Returns the raw bytes of a valid, minimal .odt file."""
    body = [f'      <text:h text:outline-level="1">{escape(title)}</text:h>\n']
    for section in sections:
        heading = section.get("heading")
        if heading:
            body.append(f'      <text:h text:outline-level="2">{escape(heading)}</text:h>\n')
        for para in section.get("paragraphs", []):
            body.append(f"      <text:p>{escape(str(para))}</text:p>\n")
    content_xml = _CONTENT_HEADER + "".join(body) + _CONTENT_FOOTER

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # The ODF spec requires "mimetype" to be the first entry and stored
        # uncompressed, so a byte-sniffing tool can identify the format
        # without inflating the archive.
        zf.writestr(zipfile.ZipInfo("mimetype"), _MIMETYPE, compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/manifest.xml", _MANIFEST)
        zf.writestr("content.xml", content_xml)
    return buf.getvalue()


def _milestone_section(milestones: list) -> dict:
    paras = [
        f"{m['name']}: {m['target_date'] or 'no target date'}"
        + (" — OVERDUE" if m["overdue"] else "")
        for m in milestones
    ] or ["No milestones."]
    return {"heading": "Milestones", "paragraphs": paras}


def _variance_section(variance: dict) -> dict:
    if not variance.get("ok"):
        return {"heading": "Variance", "paragraphs": [variance.get("error", "No baseline yet.")]}
    paras = [
        f"{t['title']}: {t['percent_complete']}% complete, "
        f"{t['hours_logged']}h logged vs {t['estimate_hours']}h estimated"
        + (f", {t['slippage_days']} day(s) behind" if t.get("slippage_days") else "")
        for t in variance["tasks"]
    ] or ["No tasks in the baseline."]
    return {"heading": "Variance", "paragraphs": paras}


def _raid_section(raid: dict) -> dict:
    paras = [
        f"[{e['type']}/{e.get('severity') or 'n/a'}] {e['summary']}"
        for e in raid.get("entries", [])
    ] or ["No open RAID entries."]
    return {"heading": "RAID", "paragraphs": paras}


def _utilization_section(util: dict) -> dict:
    paras = [
        f"{r['name']}: {r['utilization_pct']}% ({r['allocated_hours']}h / {r['capacity_hours']}h)"
        for r in util["resources"]
    ] or ["No resources in this scenario."]
    return {"heading": f"Utilization ({util['period_start']} to {util['period_end']})", "paragraphs": paras}


# Fixed order = the order sections appear in the report.
_PM_SECTION_BUILDERS = (
    ("milestones", _milestone_section),
    ("variance", _variance_section),
    ("raid", _raid_section),
    ("utilization", _utilization_section),
)


def pm_report_sections(report: dict) -> list[dict]:
    """Convert tools/pm_engine.py's pm_report() output into render_odt()'s
    section shape — a straight re-formatting, no new computation."""
    return [build(report[key]) for key, build in _PM_SECTION_BUILDERS if key in report]
