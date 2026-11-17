"""Shared document tools: personalising Word files, Word to PDF (LibreOffice) and wordmarking/watermarking PDFs.
Used by mnda and documents."""
import io, os, re, shutil, subprocess, tempfile, hashlib
from datetime import date
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import Color
from . import config

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
WORDMARK = os.path.join(ASSETS, "wordmark.png")   # optional logo for the PDF header


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def set_header_footer(doc, prospect, ref, header=True):
    for s in doc.sections:
        s.header.is_linked_to_previous = False
        s.footer.is_linked_to_previous = False
        if not header:
            fp = s.footer.add_paragraph()
            r = fp.add_run(f"STRICTLY PRIVATE & CONFIDENTIAL — Prepared exclusively for {prospect}")
            r.font.size = Pt(7); r.bold = True; r.font.color.rgb = RGBColor(0x80, 0, 0)
            fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        else:
          hp = s.header.paragraphs[0] if s.header.paragraphs else s.header.add_paragraph()
          r = hp.add_run(f"STRICTLY PRIVATE & CONFIDENTIAL — Prepared exclusively for {prospect}")
          r.font.size = Pt(8); r.bold = True; r.font.color.rgb = RGBColor(0x80, 0, 0)
          hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        fp = s.footer.add_paragraph()
        r = fp.add_run(f"Ref {ref} · Issued {date.today():%d %B %Y} to {prospect} · Not for onward distribution")
        r.font.size = Pt(7); r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER


def _replace_in_paragraph(p, mapping):
    full = "".join(r.text for r in p.runs)
    if not any(k in full for k in mapping):
        return
    for k, v in mapping.items():
        full = full.replace(k, v)
    for i, r in enumerate(p.runs):
        r.text = full if i == 0 else ""


def _iter_paragraphs(doc):
    yield from doc.paragraphs
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                yield from cell.paragraphs
    for s in doc.sections:
        for part in (s.header, s.footer):
            yield from part.paragraphs


def personalise_docx(src, dst, prospect, ref, extra=None):
    """Replace {{PROSPECT}}, {{DATE}}, {{REF}} placeholders and stamp header/footer."""
    doc = Document(src)
    mapping = {"{{PROSPECT}}": prospect, "{{DATE}}": f"{date.today():%d %B %Y}", "{{REF}}": ref}
    mapping.update(extra or {})
    for p in _iter_paragraphs(doc):
        _replace_in_paragraph(p, mapping)
    set_header_footer(doc, prospect, ref)
    doc.core_properties.title = f"Prepared for {prospect}"
    doc.core_properties.comments = f"Ref {ref}"
    doc.save(dst)


def docx_to_pdf(src, outdir):
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run(["soffice", f"-env:UserInstallation=file://{profile}", "--headless",
                        "--convert-to", "pdf", "--outdir", outdir, src],
                       check=True, capture_output=True, timeout=180)
    return os.path.join(outdir, os.path.splitext(os.path.basename(src))[0] + ".pdf")


def _overlay(w, h, prospect, ref, logo=None, brand=True):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(w, h))
    # diagonal watermark
    c.saveState()
    c.setFillColor(Color(0.6, 0.6, 0.6, alpha=0.15))
    c.translate(w / 2, h / 2); c.rotate(45)
    c.setFont("Helvetica-Bold", 44); c.drawCentredString(0, 20, config.WORDMARK_TEXT)
    c.setFont("Helvetica", 20); c.drawCentredString(0, -20, f"Prepared for {prospect}")
    c.restoreState()
    # top-right wordmark
    if not brand:
        pass
    elif logo and os.path.exists(logo):
        c.drawImage(logo, w - 62, h - 62, width=40, height=40, preserveAspectRatio=True, mask="auto")
    else:
        c.setFillColor(Color(0.1, 0.15, 0.3, alpha=0.9)); c.setFont("Helvetica-Bold", 9)
        c.drawRightString(w - 30, h - 25, config.WORDMARK_TEXT)
    c.setFillColor(Color(0.4, 0.4, 0.4)); c.setFont("Helvetica", 6.5)
    c.drawCentredString(w / 2, 12, f"{config.FIRM_NAME} · Ref {ref} · Confidential to {prospect}")
    c.save(); buf.seek(0)
    return PdfReader(buf).pages[0]


def wordmark_pdf(src, dst, prospect, ref, append=None, password=None, brand=True):
    writer = PdfWriter(clone_from=src)
    logo = WORDMARK
    for page in writer.pages:
        w, h = float(page.mediabox.width), float(page.mediabox.height)
        page.merge_page(_overlay(w, h, prospect, ref, logo, brand))
    if append and os.path.exists(append):     # appended unmodified (e.g. OneNDA terms, CC BY-ND)
        writer.append(append)
    writer.add_metadata({"/Title": f"Prepared for {prospect}", "/Subject": f"Ref {ref}",
                         "/Author": config.FIRM_NAME})
    if password:
        writer.encrypt(user_password=password, owner_password=config.SECRET_KEY, permissions_flag=0b0100)
    with open(dst, "wb") as f:
        writer.write(f)
