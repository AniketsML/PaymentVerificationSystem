"""
PDF Generator for the Legal Notice Extraction workspace.

Generates professional legal notice PDFs from extracted notice data.
Supports both ReportLab-based generation (rich formatting) and a fallback
minimal PDF when ReportLab is not installed.

Usage:
    from workspaces.legal.pdf_generator import generate_notice_pdf
    pdf_bytes = generate_notice_pdf(notice_data_dict)
    # Returns io.BytesIO ready to be served via Flask send_file()
"""
from __future__ import annotations

import io
import logging
import struct
import zlib
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch, cm
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT, TA_LEFT
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle,
    )
    from reportlab.lib.colors import HexColor

    _HAS_REPORTLAB = True
except ImportError:
    _HAS_REPORTLAB = False


# ── public API ────────────────────────────────────────────────────────────────

def generate_notice_pdf(notice_data: Dict[str, Any], mode: str = "structured") -> io.BytesIO:
    """Generate a professional PDF from a legal notice result dict.

    Args:
        notice_data: Dict from ``logger.get_notice_journey(id)["final"]``,
                     or the extraction dict from the pipeline.
        mode: ``"structured"`` (entities + clauses) or ``"verbatim"`` (raw text).

    Returns:
        ``io.BytesIO`` positioned at 0, containing the PDF bytes.
    """
    # Resolve nested entity dicts (the DB stores them as JSONB under extracted_entities)
    entities = notice_data.get("extracted_entities") or {}
    advocate = entities.get("advocate") or notice_data.get("advocate") or {}
    client = entities.get("client") or notice_data.get("client") or {}
    recipient = entities.get("recipient") or notice_data.get("recipient") or {}
    case_loan = entities.get("case_loan_details") or notice_data.get("case_loan_details") or {}
    statutory = entities.get("statutory_compliance") or notice_data.get("statutory_compliance") or {}

    # Flatten for convenience
    flat = {
        "notice_id": notice_data.get("notice_id", "UNKNOWN"),
        "notice_type": notice_data.get("notice_type", "general_legal_notice"),
        "title_heading": notice_data.get("title_heading", "LEGAL NOTICE"),
        "notice_date": notice_data.get("notice_date", ""),
        "notice_ref_number": notice_data.get("notice_ref_number", ""),
        "advocate_name": advocate.get("name") or notice_data.get("advocate_name", ""),
        "enrollment_number": advocate.get("enrollment_number", ""),
        "law_firm": advocate.get("law_firm") or notice_data.get("law_firm", ""),
        "office_address": advocate.get("office_address", ""),
        "contact_email": advocate.get("contact_email", ""),
        "contact_phone": advocate.get("contact_phone", ""),
        "client_name": client.get("name") or notice_data.get("client_name", ""),
        "client_address": client.get("address", ""),
        "recipient_name": recipient.get("name") or notice_data.get("recipient_name", ""),
        "recipient_address": recipient.get("address", ""),
        "claim_amount": notice_data.get("claim_amount") or case_loan.get("total_claim_amount"),
        "claim_amount_text": notice_data.get("claim_amount_text") or case_loan.get("total_claim_amount_text", ""),
        "cure_period_days": notice_data.get("cure_period_days") or statutory.get("cure_period_days", ""),
        "statutory_act": notice_data.get("statutory_act") or statutory.get("statutory_act", ""),
        "jurisdiction": notice_data.get("jurisdiction") or statutory.get("jurisdiction", ""),
        "summary": notice_data.get("summary", ""),
        "verbatim_text": notice_data.get("verbatim_text", ""),
        "formatted_markdown": notice_data.get("formatted_markdown", ""),
        "clauses": notice_data.get("structured_clauses") or [],
        "confidence_score": notice_data.get("confidence_score", 1.0),
        "extraction_status": notice_data.get("extraction_status", "extracted"),
    }

    if _HAS_REPORTLAB:
        return _generate_reportlab_pdf(flat, mode)
    else:
        _log.warning("reportlab not installed — generating minimal text PDF")
        return _generate_fallback_pdf(flat)


def generate_verbatim_pdf(text: str, notice_id: str = "NOTICE") -> io.BytesIO:
    """Generate a simple PDF containing only the verbatim text."""
    flat = {
        "notice_id": notice_id,
        "title_heading": "LEGAL NOTICE",
        "verbatim_text": text,
        "formatted_markdown": "",
        "clauses": [],
        "advocate_name": "", "law_firm": "", "enrollment_number": "",
        "office_address": "", "contact_email": "", "contact_phone": "",
        "client_name": "", "client_address": "",
        "recipient_name": "", "recipient_address": "",
        "notice_date": "", "notice_ref_number": "",
        "claim_amount": None, "claim_amount_text": "",
        "cure_period_days": "", "statutory_act": "", "jurisdiction": "",
        "summary": "", "confidence_score": 1.0, "extraction_status": "extracted",
        "notice_type": "general_legal_notice",
    }
    if _HAS_REPORTLAB:
        return _generate_reportlab_pdf(flat, mode="verbatim")
    return _generate_fallback_pdf(flat)


# ── ReportLab implementation ─────────────────────────────────────────────────

_UNICODE_FONT_NAME = "Helvetica"


def _init_unicode_fonts():
    global _UNICODE_FONT_NAME
    if not _HAS_REPORTLAB:
        return
    import os
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        ("Nirmala", r"C:\Windows\Fonts\Nirmala.ttc", 0),
        ("Mangal", r"C:\Windows\Fonts\mangal.ttf", None),
        ("SegoeUI", r"C:\Windows\Fonts\segoeui.ttf", None),
        ("Arial", r"C:\Windows\Fonts\arial.ttf", None),
        ("NotoDevanagari", "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf", None),
        ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", None),
        ("FreeSans", "/usr/share/fonts/truetype/freefont/FreeSans.ttf", None),
    ]
    for name, path, sub_idx in candidates:
        if os.path.exists(path):
            try:
                if sub_idx is not None:
                    pdfmetrics.registerFont(TTFont("LegalUnicodeFont", path, subfontIndex=sub_idx))
                else:
                    pdfmetrics.registerFont(TTFont("LegalUnicodeFont", path))
                _UNICODE_FONT_NAME = "LegalUnicodeFont"
                break
            except Exception as e:
                _log.debug("Could not register font %s: %s", name, e)


_init_unicode_fonts()


def _format_text_to_reportlab_html(text: str) -> str:
    """Safely converts markdown formatting (**bold**, *italic*) to valid ReportLab XML.
    
    1. Escapes raw XML characters (&, <, >).
    2. Converts markdown bold (**text** or __text__) to <b>text</b>.
    3. Converts markdown italic (*text* or _text_) to <i>text</i>.
    4. Preserves standard formatting tags.
    """
    if not text:
        return ""
    import re
    # 1. Escape XML characters
    s = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # 2. Convert markdown bold **text** or __text__ to <b>text</b>
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"__(.+?)__", r"<b>\1</b>", s)
    # 3. Convert markdown italic *text* to <i>italic</i>
    s = re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)
    s = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", s)
    # 4. Unescape supported safe tags
    s = s.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    s = s.replace("&lt;i&gt;", "<i>").replace("&lt;/i&gt;", "</i>")
    s = s.replace("&lt;u&gt;", "<u>").replace("&lt;/u&gt;", "</u>")
    s = s.replace("&lt;br/&gt;", "<br/>").replace("&lt;br&gt;", "<br/>")
    return s


def _page_footer(canvas_obj, doc):
    """Add clean page number to footer without any software metadata or watermarks."""
    canvas_obj.saveState()
    canvas_obj.setFont(_UNICODE_FONT_NAME, 8)
    canvas_obj.setFillColor(HexColor("#888888"))
    w, h = A4
    canvas_obj.drawCentredString(w / 2.0, 0.4 * inch, f"{canvas_obj.getPageNumber()}")
    canvas_obj.restoreState()


def _generate_reportlab_pdf(flat: Dict[str, Any], mode: str) -> io.BytesIO:
    """Build an authentic legal notice PDF using ReportLab Platypus with Unicode Indic font support."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=0.8 * inch,
        rightMargin=0.8 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()
    fn = _UNICODE_FONT_NAME

    # ── custom typography styles ───────────────────────────────────────────
    s_body = ParagraphStyle("DocBody", parent=styles["Normal"], fontName=fn, fontSize=10, leading=15, alignment=TA_JUSTIFY, spaceAfter=6)
    s_title = ParagraphStyle("DocTitle", parent=styles["Heading1"], fontName=fn, fontSize=11.5, leading=16, alignment=TA_CENTER, spaceBefore=8, spaceAfter=10, textColor=HexColor("#1a1a2e"))
    s_clause = ParagraphStyle("DocClause", parent=styles["Normal"], fontName=fn, fontSize=10, leading=15, alignment=TA_JUSTIFY, spaceAfter=8, leftIndent=16)
    s_demand = ParagraphStyle("DocDemand", parent=s_clause, fontName=fn, textColor=HexColor("#8b0000"), spaceAfter=10)
    s_firm = ParagraphStyle("DocFirm", parent=styles["Heading2"], fontName=fn, fontSize=13, leading=17, spaceAfter=3, textColor=HexColor("#1a1a2e"))
    s_advocate = ParagraphStyle("DocAdvocate", parent=styles["Normal"], fontName=fn, fontSize=10, leading=14, spaceAfter=2, textColor=HexColor("#333333"))
    s_addr = ParagraphStyle("DocAddr", parent=styles["Normal"], fontName=fn, fontSize=9, leading=13, textColor=HexColor("#555555"))
    s_ref = ParagraphStyle("DocRef", parent=styles["Normal"], fontName=fn, fontSize=10, leading=14, alignment=TA_LEFT, spaceAfter=6)
    s_sig = ParagraphStyle("DocSig", parent=styles["Normal"], fontName=fn, fontSize=10, leading=14, spaceBefore=18, spaceAfter=2)

    story: list = []

    # Priority 1: If formatted_markdown or verbatim_text is available, render the authentic notice
    text_content = (flat.get("formatted_markdown") or flat.get("verbatim_text") or "").strip()

    if text_content:
        import re
        normalized = text_content.replace("\r\n", "\n").replace("\r", "\n")
        blocks = re.split(r"\n\s*\n", normalized)

        for block in blocks:
            raw = block.strip()
            if not raw:
                continue

            # Skip markdown code fences or raw JSON objects if present
            if raw.startswith("```") or (raw.startswith("{") and raw.endswith("}")) or raw.startswith('{"is_legal_notice'):
                continue

            # Convert markdown formatting
            html_text = _format_text_to_reportlab_html(raw).replace("\n", "<br/>")

            # Check if horizontal rule
            if raw in ("---", "___", "***", "--------------------------------------------------------------------"):
                story.append(HRFlowable(width="100%", thickness=0.8, color=HexColor("#cccccc"), spaceAfter=8))
                continue

            # Detect block role
            is_subject = any(k in raw for k in ("विषय:", "विषय :-", "विषय:-", "SUBJECT:", "SUB:", "Subject:", "LEGAL NOTICE", "विधिक मांग सूचना", "विधिक सूचना"))
            is_clause = bool(re.match(r"^\s*(\d+|[क-ह]|\([a-zA-Z0-9क-ह]+\))\s*[\.\:\-\)]", raw))
            is_demand = is_clause and any(k in raw for k in ("मांग", "अवधि", "भुगतान", "DEMAND", "demand", "call upon", "failing which"))

            if is_subject:
                story.append(Paragraph(f"<b>{html_text}</b>", s_title))
            elif is_demand:
                story.append(Paragraph(html_text, s_demand))
            elif is_clause:
                story.append(Paragraph(html_text, s_clause))
            else:
                story.append(Paragraph(html_text, s_body))

            story.append(Spacer(1, 3))
    else:
        # Priority 2: Structured mode fallback if verbatim is empty
        if flat["law_firm"] or flat["advocate_name"]:
            if flat["law_firm"]:
                story.append(Paragraph(f"<b>{_format_text_to_reportlab_html(flat['law_firm'])}</b>", s_firm))
            if flat["advocate_name"]:
                label = f"<b>{_format_text_to_reportlab_html(flat['advocate_name'])}</b>"
                if flat["enrollment_number"]:
                    label += f" &nbsp;(Enrl. {_format_text_to_reportlab_html(flat['enrollment_number'])})"
                story.append(Paragraph(label, s_advocate))
            if flat["office_address"]:
                story.append(Paragraph(_format_text_to_reportlab_html(flat["office_address"]).replace("\n", "<br/>"), s_addr))
            story.append(Spacer(1, 4))
            story.append(HRFlowable(width="100%", thickness=0.8, color=HexColor("#cccccc"), spaceAfter=10))

        parts = []
        if flat["notice_ref_number"]:
            parts.append(f"<b>Ref:</b> {_format_text_to_reportlab_html(flat['notice_ref_number'])}")
        if flat["notice_date"]:
            parts.append(f"<b>Date:</b> {_format_text_to_reportlab_html(flat['notice_date'])}")
        if parts:
            story.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp;&nbsp;".join(parts), s_ref))
            story.append(Spacer(1, 8))

        if flat["recipient_name"]:
            story.append(Paragraph(f"<b>TO: {_format_text_to_reportlab_html(flat['recipient_name'])}</b>", s_body))
            if flat["recipient_address"]:
                story.append(Paragraph(_format_text_to_reportlab_html(flat["recipient_address"]).replace("\n", "<br/>"), s_addr))
            story.append(Spacer(1, 10))

        title = flat["title_heading"] or "LEGAL NOTICE"
        story.append(Paragraph(f"<b><u>{_format_text_to_reportlab_html(title.upper())}</u></b>", s_title))

        clauses = flat.get("clauses") or []
        for c in clauses:
            num = c.get("clause_number", "")
            heading = c.get("heading", "")
            content = c.get("content", "")
            is_demand = c.get("is_demand_clause", False)

            prefix = f"<b>{_format_text_to_reportlab_html(str(num))}.</b> " if num else ""
            hdr = f"<b>{_format_text_to_reportlab_html(heading)}</b><br/>" if heading else ""
            para = f"{prefix}{hdr}{_format_text_to_reportlab_html(content)}"
            style = s_demand if is_demand else s_clause
            story.append(Paragraph(para, style))

        if flat["advocate_name"]:
            story.append(Spacer(1, 18))
            story.append(Paragraph(f"<b>{_format_text_to_reportlab_html(flat['advocate_name'])}</b>", s_sig))
            if flat["law_firm"]:
                story.append(Paragraph(_format_text_to_reportlab_html(flat["law_firm"]), s_addr))
            story.append(Paragraph("Advocate", s_addr))

    # Build document
    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    buf.seek(0)
    return buf


# ── Fallback: minimal PDF without ReportLab ──────────────────────────────────

def _generate_fallback_pdf(flat: Dict[str, Any]) -> io.BytesIO:
    """Generate a basic but valid PDF using raw PDF operators (no dependencies)."""
    title = flat.get("title_heading") or "LEGAL NOTICE"
    text = flat.get("verbatim_text") or flat.get("formatted_markdown") or flat.get("summary") or ""

    lines: list[str] = []
    lines.append("BT")
    lines.append("/F1 12 Tf")
    lines.append("50 780 Td")
    lines.append("14 TL")

    safe_title = title.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    lines.append(f"({safe_title}) Tj T*")
    lines.append("() Tj T*")

    y = 752
    for raw_line in text.split("\n"):
        safe = raw_line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        chunks = [safe[i : i + 85] for i in range(0, max(1, len(safe)), 85)] if safe.strip() else [""]
        for chunk in chunks:
            if y < 60:
                break
            lines.append(f"({chunk}) Tj T*")
            y -= 14
        if y < 60:
            break

    lines.append("ET")
    stream = "\n".join(lines).encode("utf-8", errors="replace")

    objs: list[bytes] = []
    objs.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    objs.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
    objs.append(
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> "
        b"/Contents 4 0 R >>\nendobj\n"
    )
    objs.append(f"4 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream\nendobj\n")

    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    offsets = []
    for obj in objs:
        offsets.append(buf.tell())
        buf.write(obj)

    xref_pos = buf.tell()
    buf.write(f"xref\n0 {len(objs) + 1}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for off in offsets:
        buf.write(f"{off:010d} 00000 n \n".encode())

    buf.write(f"trailer\n<< /Root 1 0 R /Size {len(objs) + 1} >>\n".encode())
    buf.write(f"startxref\n{xref_pos}\n%%EOF\n".encode())

    buf.seek(0)
    return buf
