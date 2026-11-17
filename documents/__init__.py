"""Prospect-named documents and the issue register: upload a .docx, fill {{PROSPECT}} / {{DATE}} / {{REF}}, stamp
"Prepared exclusively for" on every page, convert to a wordmarked, watermarked PDF (optional password), and log its
SHA-256. Verify tells you who a forwarded PDF was issued to. Owns the table `issues`."""
import os, uuid
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from werkzeug.utils import secure_filename
from portal import core, doctools, storage
from portal.core import db, login_required, audit, now, newref, REGISTRY

bp = Blueprint("documents", __name__, template_folder="templates")

MIGRATIONS = [
    ("001_issues", {
        "sqlite": """create table if not exists issues(id integer primary key, ref text, prospect text, src_name text,
  docx text, pdf text, sha text, notes text, created_by int, created text)""",
        "mysql": """create table if not exists issues(id int auto_increment primary key, ref varchar(64), prospect text, src_name text,
  docx text, pdf text, sha varchar(64), notes text, created_by int, created varchar(32), index(ref), index(sha))"""}),
]


def register(r):
    r.nav.extend([("/issue", "Issue document"), ("/verify", "Verify")])
    r.home_tiles.append(("/issue", "Issue personalised document", "Name the prospect, wordmark &amp; export to PDF"))
    r.ref_lookups.insert(0, lambda ref: db().execute("select * from issues where ref=?", (ref,)).fetchone())
    r.migrations["documents"] = MIGRATIONS


@bp.route("/issue", methods=["GET", "POST"])
@login_required
def issue():
    if request.method == "POST":
        f = request.files.get("file"); prospect = request.form["prospect"].strip()
        if not f or not f.filename.lower().endswith(".docx") or not prospect:
            flash("Upload a .docx and enter the prospect's name", "error"); return redirect(url_for("documents.issue"))
        ref = newref("DOC")
        src = os.path.join(core.UPL, secure_filename(f"{ref}_src_{f.filename}")); f.save(src); storage.put("uploads", src)
        stem = secure_filename(f"{ref}_{os.path.splitext(f.filename)[0]}_{prospect}")
        docx = os.path.join(core.GEN, stem + ".docx")
        doctools.personalise_docx(src, docx, prospect, ref)
        raw = doctools.docx_to_pdf(docx, core.GEN)
        pdf = os.path.join(core.GEN, stem + "_wm.pdf")
        doctools.wordmark_pdf(raw, pdf, prospect, ref, password=request.form.get("password") or None)
        os.remove(raw); storage.put("generated", docx); storage.put("generated", pdf)
        cur = db().execute("insert into issues(ref,prospect,src_name,docx,pdf,sha,notes,created_by,created) values(?,?,?,?,?,?,?,?,?)",
                           (ref, prospect, f.filename, os.path.basename(docx), os.path.basename(pdf), doctools.sha256(pdf),
                            request.form.get("notes", ""), session["uid"], now()))
        db().commit(); audit("issue", cur.lastrowid, "issued", f"{f.filename} -> {prospect}")
        flash(f"Issued {ref} for {prospect}")
        return redirect(url_for("documents.issue"))
    rows = db().execute("select i.*, u.name as author from issues i left join users u on u.id=i.created_by order by i.id desc").fetchall()
    return render_template("documents/issue.html", rows=rows)


@bp.route("/verify", methods=["GET", "POST"])
@login_required
def verify():
    """Upload a leaked/forwarded PDF to find out who it was issued to (by exact hash, then by the reference in its
    metadata, asking every function that issues references: documents, then NDAs)."""
    hit = None
    if request.method == "POST" and request.files.get("file"):
        tmp = os.path.join(core.UPL, "verify_" + uuid.uuid4().hex); request.files["file"].save(tmp)
        hit = db().execute("select * from issues where sha=?", (doctools.sha256(tmp),)).fetchone()
        if not hit:
            try:
                meta = doctools.PdfReader(tmp).metadata or {}
                ref = (meta.get("/Subject") or "").replace("Ref ", "")
                for lookup in REGISTRY.ref_lookups:
                    hit = lookup(ref) if ref else None
                    if hit: break
            except Exception: pass
        os.remove(tmp)
        if not hit: flash("No match found in the issue register", "error")
    return render_template("documents/verify.html", hit=hit)
