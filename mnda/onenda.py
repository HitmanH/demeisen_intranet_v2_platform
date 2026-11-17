"""oneNDA v2.1 (CC BY 4.0): fills the Parties and Variables tables of the official template; the Terms are left as
published. Presentation tidy-ups only."""
import os
from docx import Document
from docx.shared import Pt
from portal import config, doctools

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


ONENDA_TEMPLATE = os.path.join(ASSETS, "onenda_v2.1.docx")


def _fill(cell, text, para=0, keep_label=True):
    """Write text after the label in a template cell (keeps template formatting)."""
    p = cell.paragraphs[para]
    if keep_label and p.runs:
        p.add_run(text)
    else:
        for r in p.runs[1:]: r.text = ""
        if p.runs: p.runs[0].text = text
        else: p.add_run(text)


def _add_logo_banner(d):
    """Demeisen logo (left) and oneNDA logo (right) at the top of page 1."""
    from docx.shared import Cm
    from docx.enum.text import WD_TAB_ALIGNMENT
    sec = d.sections[0]
    width = sec.page_width - sec.left_margin - sec.right_margin
    first = d.paragraphs[0]
    p = first.insert_paragraph_before()
    p.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
    p.paragraph_format.space_after = Pt(12)
    ours = doctools.WORDMARK
    onenda = os.path.join(ASSETS, "onenda_logo.png")
    if os.path.exists(ours): p.add_run().add_picture(ours, height=Cm(1.6))
    p.add_run("\t")
    if os.path.exists(onenda): p.add_run().add_picture(onenda, height=Cm(1.0))


def _tidy_onenda(d):
    """Presentation fixes to the template (terms text untouched):
    - remove the two full-width red bars in the page header
    - put the CC BY attribution on one footer line, linking 'oneNDA Version 2.1'
    - stop clause headings being numbered twice (typed '1.' + automatic list number)"""
    import copy
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.enum.text import WD_TAB_ALIGNMENT
    for sec in d.sections:
        for hdr in (sec.header, sec.first_page_header, sec.even_page_header, d):
            root = d.element.body if hdr is d else hdr._element
            for ext in root.findall(".//" + qn("wp:extent")):
                if int(ext.get("cx")) > 6000000:          # page-wide bar
                    run = ext
                    while run is not None and run.tag != qn("w:r"):
                        run = run.getparent()
                    if run is not None and run.getparent() is not None:
                        run.getparent().remove(run)
        f = sec.footer
        paras = f.paragraphs
        if len(paras) >= 3 and paras[0].text.startswith("Standard Mutual"):
            url = "https://www.onenda.org/products/onenda"
            for rel in f.part.rels.values():
                if rel.reltype == RT.HYPERLINK: url = rel.target_ref
            page_runs = [r for r in paras[1]._p.findall(qn("w:r"))
                         if r.find(qn("w:fldChar")) is not None or r.find(qn("w:instrText")) is not None
                         or (r.find(qn("w:t")) is not None and r.find(qn("w:t")).text and r.find(qn("w:t")).text.strip().isdigit())]
            p0 = paras[0]
            for r in p0._p.findall(qn("w:r")): p0._p.remove(r)
            def run(text):
                r = p0.add_run(text); r.font.size = Pt(7.5); return r
            run("Standard Mutual Non-Disclosure Agreement (")
            rid = f.part.relate_to(url, RT.HYPERLINK, is_external=True)
            h = OxmlElement("w:hyperlink"); h.set(qn("r:id"), rid)
            r = OxmlElement("w:r"); rpr = OxmlElement("w:rPr")
            col = OxmlElement("w:color"); col.set(qn("w:val"), "1155CC"); rpr.append(col)
            u = OxmlElement("w:u"); u.set(qn("w:val"), "single"); rpr.append(u)
            sz = OxmlElement("w:sz"); sz.set(qn("w:val"), "15"); rpr.append(sz)
            t = OxmlElement("w:t"); t.text = "oneNDA Version 2.1"
            r.append(rpr); r.append(t); h.append(r); p0._p.append(h)
            run(") | Law Insider Standards | Licensed under CC-BY 4.0\t")
            for pr in page_runs: p0._p.append(copy.deepcopy(pr))
            width = sec.page_width - sec.left_margin - sec.right_margin
            p0.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
            for p in paras[1:]:
                if not p.text.strip() or "Law Insider" in p.text or "learn more" in p.text:
                    p._p.getparent().remove(p._p)
    terms = d.tables[2]
    for i, row in enumerate(terms.rows):
        if i % 2 == 0:                                   # heading rows
            for c in row.cells:
                for p in c.paragraphs:
                    for n in p._p.findall(".//" + qn("w:numPr")):
                        n.getparent().remove(n)


def build_nda_docx(nda, dst):
    """Fill the variables of the official oneNDA v2.1 template (CC BY 4.0).
    Only the Parties/Variables tables are touched; the Terms are left as published."""
    d = Document(ONENDA_TEMPLATE)
    # drop the drafting guidance note under "PARTIES AND EXECUTION"
    for p in list(d.paragraphs):
        if p.text.startswith("Guidance:"):
            p._element.getparent().remove(p._element)
    _tidy_onenda(d)
    _add_logo_banner(d)
    parties, var = d.tables[0], d.tables[1]
    ours = (f"{config.FIRM_NAME}, a limited liability partnership registered in England and Wales "
            f"(no. {config.FIRM_COMPANY_NO}) whose registered office is at {config.FIRM_ADDRESS} "
            f"and whose principal place of business is at {config.FIRM_OPERATING_ADDRESS}")
    cells = parties.rows
    _fill(cells[1].cells[0], ours, para=1, keep_label=False)
    _fill(cells[1].cells[1], nda["counterparty"] + (f", {nda['cp_details']}" if nda["cp_details"] else ""),
          para=1, keep_label=False)
    for row, (a, b) in zip((3, 4, 5), ((nda["our_signatory"], nda["cp_signatory"]),
                                       (nda["our_title"], nda["cp_signatory_title"]),
                                       (nda["our_email"], nda["cp_email"]))):
        _fill(cells[row].cells[0], a or ""); _fill(cells[row].cells[1], b or "")
    _fill(var.cell(0, 1), nda["purpose"], keep_label=False)
    _fill(var.cell(1, 1), nda["conf_period"], keep_label=False)
    _fill(var.cell(2, 1), nda["governing_law"], keep_label=False)
    dr = var.cell(3, 1)
    _fill(dr, nda["dispute"], keep_label=False)
    for p in dr.paragraphs[1:]: p._element.getparent().remove(p._element)
    sp = var.cell(4, 1)
    _fill(sp, (nda["changes"] or "None.").strip(), para=1, keep_label=False)
    for p in sp.paragraphs[1:]:
        for r in p.runs: r.font.highlight_color = None
    doctools.set_header_footer(d, nda["counterparty"], nda["ref"], header=False)
    d.core_properties.title = f"Mutual NDA - {config.FIRM_NAME} / {nda['counterparty']}"
    d.save(dst)
