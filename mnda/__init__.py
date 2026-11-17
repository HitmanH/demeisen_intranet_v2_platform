"""Mutual NDAs (oneNDA v2.1): form -> filled Word document and wordmarked PDF, status workflow, executed-copy upload,
history. Owns the table `ndas`. Uses only portal.core, portal.config and portal.doctools."""
import os
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, session, abort
from werkzeug.utils import secure_filename
from portal import config, core, doctools, storage
from portal.core import db, me, login_required, audit, now, newref
from . import onenda

bp = Blueprint("mnda", __name__, template_folder="templates", static_folder="assets", static_url_path="/mnda/assets")

STATUSES = ["Draft", "Internal review", "Sent to counterparty", "Signed by counterparty",
            "Fully executed", "Expired / terminated"]

MIGRATIONS = [
    ("001_ndas", {
        "sqlite": """create table if not exists ndas(id integer primary key, ref text, counterparty text, cp_details text,
  cp_type text, purpose text, ci_scope text, duration text, conf_period text, governing_law text,
  changes text, effective_date text, our_signatory text, cp_signatory text, cp_signatory_title text,
  cp_email text, status text, docx text, pdf text, signed text, created_by int, created text,
  dispute text, our_title text, our_email text)""",
        "mysql": """create table if not exists ndas(id int auto_increment primary key, ref varchar(64), counterparty text, cp_details text,
  cp_type text, purpose text, ci_scope text, duration text, conf_period text, governing_law text,
  changes text, effective_date text, our_signatory text, cp_signatory text, cp_signatory_title text,
  cp_email text, status varchar(64), docx text, pdf text, signed text, created_by int, created varchar(32),
  dispute text, our_title text, our_email text, index(ref))"""}),
]


def register(r):
    r.nav.append(("/nda", "NDAs"))
    r.home_tiles.append(("/nda/new", "New Mutual NDA", "OneNDA for investors, data vendors &amp; more"))
    r.home_cards.append(_home_card)
    r.ref_lookups.append(lambda ref: db().execute(
        "select ref, counterparty as prospect, created from ndas where ref=?", (ref,)).fetchone())
    r.migrations["mnda"] = MIGRATIONS


def _home_card(uid):
    rows = db().execute("select * from ndas where status not in ('Fully executed','Expired / terminated') order by id desc limit 5").fetchall()
    return render_template("mnda/_home_card.html", ndas=rows) if rows else ""


@bp.route("/nda")
@login_required
def nda_list():
    rows = db().execute("select n.*, u.name as author from ndas n left join users u on u.id=n.created_by order by n.id desc").fetchall()
    return render_template("mnda/list.html", rows=rows)


@bp.route("/nda/new", methods=["GET", "POST"])
@login_required
def nda_new():
    if request.method == "POST":
        f = request.form
        purpose = f["purpose"].strip() or config.NDA_PURPOSES.get(f["cp_type"], "").format(
            firm=config.FIRM_NAME, cp=f["counterparty"])
        ref = newref("NDA")
        cur = db().execute("""insert into ndas(ref,counterparty,cp_details,cp_type,purpose,ci_scope,duration,
            conf_period,governing_law,changes,effective_date,our_signatory,cp_signatory,cp_signatory_title,
            cp_email,status,created_by,created,dispute,our_title,our_email) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ref, f["counterparty"].strip(), f["cp_details"], f["cp_type"], purpose, "", "",
             f["conf_period"], "England and Wales", f["changes"], f["effective_date"], f["our_signatory"],
             f["cp_signatory"], f["cp_signatory_title"], f["cp_email"], "Draft", session["uid"], now(),
             "Litigation in the courts of England and Wales", f["our_title"], f["our_email"]))
        db().commit(); nid = cur.lastrowid
        generate(nid); audit("nda", nid, "created", ref)
        return redirect(url_for("mnda.nda_view", nid=nid))
    return render_template("mnda/form.html", purposes=config.NDA_PURPOSES, today=date.today().isoformat(), user=me())


def generate(nid):
    n = dict(db().execute("select * from ndas where id=?", (nid,)).fetchone())
    base = secure_filename(f"{n['ref']}_{n['counterparty']}")
    docx = os.path.join(core.GEN, base + ".docx")
    onenda.build_nda_docx(n, docx)
    raw = doctools.docx_to_pdf(docx, core.GEN)
    pdf = os.path.join(core.GEN, base + "_issued.pdf")
    doctools.wordmark_pdf(raw, pdf, n["counterparty"], n["ref"], append=None, brand=False)
    os.remove(raw); storage.put("generated", docx); storage.put("generated", pdf)
    db().execute("update ndas set docx=?, pdf=? where id=?", (os.path.basename(docx), os.path.basename(pdf), nid))
    db().commit()


@bp.route("/nda/<int:nid>", methods=["GET", "POST"])
@login_required
def nda_view(nid):
    n = db().execute("select * from ndas where id=?", (nid,)).fetchone() or abort(404)
    if request.method == "POST":
        act = request.form["action"]
        if act == "status":
            st = request.form["status"]
            if st not in STATUSES: abort(400)
            db().execute("update ndas set status=? where id=?", (st, nid)); audit("nda", nid, "status", st)
        elif act == "regen":
            generate(nid); audit("nda", nid, "regenerated")
        elif act == "signed" and request.files.get("file"):
            fn = secure_filename(f"{n['ref']}_signed_{request.files['file'].filename}")
            request.files["file"].save(os.path.join(core.UPL, fn)); storage.put("uploads", os.path.join(core.UPL, fn))
            db().execute("update ndas set signed=?, status='Fully executed' where id=?", (fn, nid))
            audit("nda", nid, "signed copy uploaded", fn)
        db().commit(); return redirect(url_for("mnda.nda_view", nid=nid))
    log = db().execute("select a.*, u.name from audit a left join users u on u.id=a.user_id where entity='nda' and entity_id=? order by a.id", (nid,)).fetchall()
    return render_template("mnda/view.html", n=n, statuses=STATUSES, log=log,
                           terms_present=os.path.exists(onenda.ONENDA_TEMPLATE))
