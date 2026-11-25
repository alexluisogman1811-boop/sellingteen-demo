"""PDF export of a job report (fpdf2, core fonts - no system dependencies)."""
import logging
import os
import unicodedata

from fpdf import FPDF

from .models import JOB_STATUSES, PHOTO_STAGES, format_date, format_datetime, photos_by_stage

log = logging.getLogger("jobproof.pdf")

SECTION_TITLES = [
    ("reported_issue", "Reported Issue"),
    ("findings", "Findings"),
    ("work_completed", "Work Completed"),
    ("materials", "Materials / Parts"),
    ("testing", "Testing / Verification"),
    ("customer_notes", "Customer Notes"),
    ("follow_up", "Recommended Follow-Up"),
]
ACCENT = (29, 78, 216)
INK = (17, 24, 39)
MUTED = (100, 116, 139)
RULE = (226, 232, 240)

_REPLACEMENTS = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...",
                 "•": "-", " ": " ", "°": " deg", "€": "EUR"}


def safe(text):
    """Core PDF fonts are Latin-1 only. Keep Latin-1 characters (José, Müller); transliterate or drop the rest."""
    text = str(text or "")
    for src, dst in _REPLACEMENTS.items():
        text = text.replace(src, dst)
    out = []
    for ch in text:
        try:
            ch.encode("latin-1")
            out.append(ch)
        except UnicodeEncodeError:
            out.append(unicodedata.normalize("NFKD", ch).encode("latin-1", "ignore").decode("latin-1"))
    return "".join(out)


def _short(text, limit):
    text = safe(text)
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


class ReportPDF(FPDF):
    footer_text = ""

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*MUTED)
        self.cell(0, 6, self.footer_text, align="L")
        self.set_x(self.l_margin)
        self.cell(0, 6, f"Page {self.page_no()} of {{nb}}", align="R")


class PDFError(Exception):
    pass


def build_pdf(business, job, report, photos, photo_path_fn, logo_path=None, include_customer_contact=True):
    try:
        return _build(business, job, report, photos, photo_path_fn, logo_path, include_customer_contact)
    except Exception as e:  # surface a single, friendly failure type to callers
        raise PDFError(type(e).__name__) from e


def _build(business, job, report, photos, photo_path_fn, logo_path, include_customer_contact):
    content = report["content"]
    pdf = ReportPDF(format="Letter", unit="mm")
    # Shorten the business name, never the date.
    pdf.footer_text = f"{_short(business['name'] or 'Job report', 60)} - generated {format_datetime(report['generated_at'])}"
    pdf.set_margins(16, 16, 16)
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    # Header: logo + business details (every line wraps, nothing runs off the page)
    top = pdf.get_y()
    text_x = pdf.l_margin
    if logo_path and os.path.exists(logo_path):
        try:
            pdf.image(logo_path, x=pdf.l_margin, y=top, h=18, keep_aspect_ratio=True, w=40)
            text_x = pdf.l_margin + 44
        except Exception as e:  # a damaged logo must not block the report
            log.warning("Skipping unreadable logo: %s", type(e).__name__)
    text_w = pdf.w - pdf.r_margin - text_x
    pdf.set_xy(text_x, top)
    pdf.set_font("Helvetica", "B", 17)
    pdf.set_text_color(*INK)
    pdf.multi_cell(text_w, 8, safe(business["name"] or "Job Report"), new_x="LEFT", new_y="NEXT", align="L")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*MUTED)
    contact = " | ".join(x for x in (business["contact_name"], business["phone"], business["email"]) if x)
    for line in (contact, business["address"]):
        if line:
            pdf.set_x(text_x)
            pdf.multi_cell(text_w, 5, safe(line), new_x="LEFT", new_y="NEXT", align="L")
    pdf.set_y(max(pdf.get_y(), top + 20) + 3)
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.8)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(5)

    # Title
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 5, "JOB COMPLETION REPORT", new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*INK)
    pdf.multi_cell(width, 7, safe(job["title"]), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.ln(2)

    # Details grid: two columns; each row is as tall as its tallest cell
    details = [
        ("Customer", job["customer_name"]),
        ("Service address", job["address"]),
        ("Job date", format_date(job["job_date"])),
        ("Service type", job["service_type"]),
        ("Technician", job["technician"]),
        ("Status", JOB_STATUSES.get(job["status"], job["status"])),
        ("Report date", format_datetime(report["generated_at"])),
    ]
    if include_customer_contact:
        details += [("Customer phone", job["customer_phone"]), ("Customer email", job["customer_email"])]
    details = [(k, v) for k, v in details if v]
    col = width / 2
    for i in range(0, len(details), 2):
        if pdf.get_y() > pdf.h - 40:
            pdf.add_page()
        y = pdf.get_y()
        bottom = y + 12
        for j, (label, value) in enumerate(details[i:i + 2]):
            x = pdf.l_margin + j * col
            pdf.set_xy(x + 2, y + 1.5)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(*MUTED)
            pdf.cell(col - 4, 4, label.upper())
            pdf.set_xy(x + 2, y + 5.5)
            pdf.set_font("Helvetica", "", 10.5)
            pdf.set_text_color(*INK)
            pdf.multi_cell(col - 4, 5, safe(value), new_x="LEFT", new_y="NEXT", align="L")
            bottom = max(bottom, pdf.get_y())
        pdf.set_y(bottom + 1)
    pdf.ln(2)

    def heading(text, min_space=40):
        if pdf.get_y() > pdf.h - min_space:
            pdf.add_page()
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(*INK)
        pdf.cell(0, 7, text, new_x="LMARGIN", new_y="NEXT", align="L")
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.3)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(2)

    if content.get("summary"):
        heading("Job Summary")
        pdf.set_font("Helvetica", "", 10.5)
        pdf.set_text_color(*INK)
        pdf.multi_cell(width, 5.5, safe(content["summary"]), new_x="LMARGIN", new_y="NEXT", align="L")

    for key, title in SECTION_TITLES:
        items = content.get(key) or []
        if not items:
            continue
        heading(title)
        pdf.set_font("Helvetica", "", 10.5)
        pdf.set_text_color(*INK)
        for item in items:
            if pdf.get_y() > pdf.h - 26:
                pdf.add_page()
            y = pdf.get_y()
            pdf.set_xy(pdf.l_margin + 1, y)
            pdf.cell(5, 5.5, "-")
            pdf.set_xy(pdf.l_margin + 6, y)
            pdf.multi_cell(width - 6, 5.5, safe(item), new_x="LMARGIN", new_y="NEXT", align="L")
            pdf.ln(0.8)

    # Photos: two per row, grouped by stage
    grouped = photos_by_stage(photos)
    if photos:
        heading("Photos", min_space=110)  # keep the heading with the first row of photos
        gap = 6
        cell_w = (width - gap) / 2
        for stage, label in PHOTO_STAGES.items():
            stage_photos = grouped[stage]
            if not stage_photos:
                continue
            if pdf.get_y() > pdf.h - 60:
                pdf.add_page()
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_text_color(*ACCENT)
            pdf.cell(0, 6, f"{label} ({len(stage_photos)})", new_x="LMARGIN", new_y="NEXT", align="L")
            for i in range(0, len(stage_photos), 2):
                row = stage_photos[i:i + 2]
                heights = [min(cell_w * p["height"] / max(p["width"], 1), 85) for p in row]
                row_h = max(heights)
                if pdf.get_y() + row_h > pdf.h - 20:
                    pdf.add_page()
                y = pdf.get_y()
                for j, p in enumerate(row):
                    x = pdf.l_margin + j * (cell_w + gap)
                    w = min(heights[j] * p["width"] / max(p["height"], 1), cell_w)
                    _photo(pdf, photo_path_fn(p), x, y, w, heights[j])
                pdf.set_y(y + row_h + 4)

    # Closing lines (business profile details only). Like the completion block below, they are left off
    # rather than starting an almost-empty page - the same contact details are in the header.
    closing = closing_lines(business)
    if closing and pdf.get_y() <= pdf.h - (40 + 6 * len(closing)):
        pdf.ln(3)
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.3)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(2)
        for i, line in enumerate(closing):
            pdf.set_font("Helvetica", "B" if i == 0 and len(closing) > 1 else "", 10)
            pdf.set_text_color(*(INK if i == 0 else MUTED))
            pdf.multi_cell(width, 5.5, safe(line), new_x="LMARGIN", new_y="NEXT", align="L")

    # Completion block. Status and report date are already in the details grid near the top, so if
    # this summary doesn't fit on the last page it is left off rather than starting an almost-empty page.
    if pdf.get_y() > pdf.h - 36:
        return bytes(pdf.output())
    pdf.ln(3)
    pdf.set_fill_color(239, 246, 255)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*INK)
    status = JOB_STATUSES.get(job["status"], job["status"])
    pdf.multi_cell(width, 6, safe(
        f"Job status: {status}    |    Report generated: {format_datetime(report['generated_at'])}"),
        fill=True, padding=3, new_x="LMARGIN", new_y="NEXT", align="L")

    return bytes(pdf.output())


def closing_lines(business):
    """'Questions about this work? Contact ...' and a thank-you, from the business profile only."""
    lines = []
    reach = [v for v in (business["phone"], business["email"]) if v]
    if reach:
        who = f"{business['contact_name']}, " if business["contact_name"] else ""
        lines.append(f"Questions about this work? Contact {who}{business['name'] or 'us'}: {' | '.join(reach)}")
    if business["name"]:
        lines.append(f"Thank you for choosing {business['name']}.")
    return lines


def _photo(pdf, path, x, y, w, h):
    """Draw a photo, or a labelled placeholder if the file is missing or unreadable."""
    try:
        if not os.path.exists(path):
            raise FileNotFoundError
        pdf.image(path, x=x, y=y, w=w, h=h)
    except Exception as e:
        log.warning("Photo unavailable for PDF: %s", type(e).__name__)
        pdf.set_fill_color(241, 245, 249)
        pdf.rect(x, y, w, h, style="F")
        pdf.set_xy(x, y + h / 2 - 3)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*MUTED)
        pdf.cell(w, 6, "Photo unavailable", align="C")
