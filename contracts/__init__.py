"""Contract review: upload an NDA, engagement letter, vendor or trial agreement (PDF or Word) and get back drafting
errors, key terms, terms to push back on, a summary of money and liability, and the termination provisions. The review
runs in the background (a minute or few) and the page refreshes until it is ready. Owns the table `contracts`.

The original is kept in the files store (`uploads/`). The review is stored as JSON in `contracts.review`."""
import json, os, tempfile, threading
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, abort, current_app
from werkzeug.utils import secure_filename
from portal import core, doctools, storage
from portal.core import db, login_required, audit, now, newref
from . import review as engine

bp = Blueprint("contracts", __name__, template_folder="templates")
CATEGORIES = engine.CATEGORIES
ACCEPT = (".pdf", ".docx", ".doc")
STALE_MINUTES = 20      # a review "running" for longer than this was interrupted (e.g. the app restarted)

MIGRATIONS = [
    ("001_contracts", {
        "sqlite": """create table if not exists contracts(id integer primary key, ref text, category text,
  counterparty text, context text, src_name text, stored text, sha text, status text, review text, error text,
  model text, pages int, tokens_in int, tokens_out int, started text, reviewed text, created_by int, created text)""",
        "mysql": """create table if not exists contracts(id int auto_increment primary key, ref varchar(64),
  category varchar(32), counterparty text, context text, src_name text, stored text, sha varchar(64),
  status varchar(16), review mediumtext, error text, model varchar(64), pages int, tokens_in int, tokens_out int,
  started varchar(32), reviewed varchar(32), created_by int, created varchar(32), index(ref), index(category))"""}),
]


def register(r):
    r.nav.append(("/contracts", "Contracts"))
    r.home_tiles.append(("/contracts", "Review a contract", "NDAs, engagement letters, vendor &amp; trial agreements"))
    r.home_cards.append(_home_card)
    r.migrations["contracts"] = MIGRATIONS


def _home_card(uid):
    rows = db().execute("select * from contracts order by id desc limit 5").fetchall()
    return render_template("contracts/_home_card.html", rows=[_decorate(x) for x in rows], categories=CATEGORIES) if rows else ""


def _decorate(row):
    """The row plus its parsed review and a few counts for lists."""
    c = dict(row)
    c["data"] = json.loads(c["review"]) if c.get("review") else None
    c["stale"] = c["status"] == "running" and _is_stale(c.get("started"))
    concerns = (c["data"] or {}).get("concerns", [])
    c["n_high"] = sum(1 for x in concerns if x.get("severity") == "high")
    c["n_concerns"] = len(concerns)
    c["n_errors"] = len((c["data"] or {}).get("drafting_errors", []))
    c["risk"] = ((c["data"] or {}).get("overview") or {}).get("overall_risk")
    return c


def _is_stale(started):
    try:
        return datetime.fromisoformat(started) < datetime.now() - timedelta(minutes=STALE_MINUTES)
    except (TypeError, ValueError):
        return True


# ------------------------------------------------------------------------------------------------------- pages
@bp.route("/contracts", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        f = request.files.get("file"); category = request.form.get("category", "")
        if not f or not f.filename or not f.filename.lower().endswith(ACCEPT):
            flash("Upload a PDF or Word document", "error"); return redirect(url_for("contracts.index"))
        if category not in CATEGORIES:
            flash("Choose a category", "error"); return redirect(url_for("contracts.index"))
        ref = newref("CON")
        stored = secure_filename(f"{ref}_{f.filename}")
        path = os.path.join(core.UPL, stored); f.save(path); storage.put("uploads", path)
        cur = db().execute(
            "insert into contracts(ref,category,counterparty,context,src_name,stored,sha,status,created_by,created) "
            "values(?,?,?,?,?,?,?,?,?,?)",
            (ref, category, request.form.get("counterparty", "").strip(), request.form.get("context", "").strip(),
             f.filename, stored, doctools.sha256(path), "queued", session["uid"], now()))
        db().commit(); cid = cur.lastrowid
        audit("contract", cid, "uploaded", f"{f.filename} ({CATEGORIES[category]})")
        _start(cid)
        return redirect(url_for("contracts.view", cid=cid))
    cat = request.args.get("category", "")
    sql = "select c.*, u.name as author from contracts c left join users u on u.id=c.created_by"
    rows = db().execute(sql + (" where c.category=?" if cat in CATEGORIES else "") + " order by c.id desc",
                        (cat,) if cat in CATEGORIES else ()).fetchall()
    counts = {r["category"]: r["n"] for r in db().execute("select category, count(*) as n from contracts group by category")}
    return render_template("contracts/list.html", rows=[_decorate(r) for r in rows], categories=CATEGORIES,
                           current=cat if cat in CATEGORIES else "", counts=counts, configured=engine.configured())


@bp.route("/contracts/<int:cid>", methods=["GET", "POST"])
@login_required
def view(cid):
    row = db().execute("select c.*, u.name as author from contracts c left join users u on u.id=c.created_by where c.id=?",
                       (cid,)).fetchone()
    if not row: abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "details":
            category = request.form.get("category", row["category"])
            if category not in CATEGORIES: category = row["category"]
            db().execute("update contracts set category=?, counterparty=?, context=? where id=?",
                         (category, request.form.get("counterparty", "").strip(), request.form.get("context", "").strip(), cid))
            db().commit(); audit("contract", cid, "details changed", CATEGORIES[category])
            flash("Details saved. Re-run the review to use them.")
        elif action == "rerun":
            if _start(cid, force=True): audit("contract", cid, "review re-run"); flash("Review started")
            else: flash("A review is already running", "error")
        return redirect(url_for("contracts.view", cid=cid))
    c = _decorate(row)
    log = db().execute("select a.*, u.name from audit a left join users u on u.id=a.user_id where entity='contract' "
                       "and entity_id=? order by a.id desc", (cid,)).fetchall()
    return render_template("contracts/view.html", c=c, r=c["data"], categories=CATEGORIES, log=log,
                           configured=engine.configured())


# --------------------------------------------------------------------------------------------- background review
def _start(cid, force=False):
    """Claims the contract for review and runs it on a background thread. False if one is already running."""
    if not engine.configured():
        db().execute("update contracts set status='queued', error=? where id=?",
                     ("Reviews are not switched on yet: the Claude API key is not set (see contracts/README.md).", cid))
        db().commit(); return True
    cutoff = (datetime.now() - timedelta(minutes=STALE_MINUTES)).isoformat(timespec="seconds")
    claimed = db().execute("update contracts set status='running', error=null, started=? where id=? and "
                           "(status<>'running' or started is null or started<?)", (now(), cid, cutoff)).rowcount
    db().commit()
    if not claimed: return False
    app = current_app._get_current_object()
    t = threading.Thread(target=_review_in_background, args=(app, cid), daemon=True, name=f"contract-review-{cid}")
    t.start()
    if app.config.get("CONTRACTS_SYNC"): t.join()     # tests
    return True


def _review_in_background(app, cid):
    with app.app_context():
        row = db().execute("select * from contracts where id=?", (cid,)).fetchone()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pdf = _as_pdf(row, tmp)
                result, info = engine.run(pdf, category=row["category"], title=row["src_name"],
                                          counterparty=row["counterparty"] or "", context=row["context"] or "")
            db().execute("update contracts set status='done', review=?, error=null, model=?, pages=?, tokens_in=?, "
                         "tokens_out=?, reviewed=? where id=?",
                         (json.dumps(result), info["model"], info["pages"], info["tokens_in"], info["tokens_out"], now(), cid))
            db().commit()
            audit("contract", cid, "reviewed", f'{info["model"]}, {info["pages"]} pages', by=row["created_by"])
        except Exception as e:      # anything at all: record it so the page can say what happened and offer a retry
            msg = str(e) if isinstance(e, engine.ReviewError) else f"Review failed: {type(e).__name__}"
            app.logger.exception("contract review %s failed", cid)
            db().execute("update contracts set status='failed', error=? where id=?", (msg, cid)); db().commit()


def _as_pdf(row, tmp):
    """The original as a local PDF: fetched from the files store if this container doesn't have it, and converted
    with LibreOffice if it is a Word document."""
    src = os.path.join(core.UPL, row["stored"])
    if not os.path.exists(src):
        if not storage.REMOTE: raise engine.ReviewError("The uploaded file is missing.")
        storage.get("uploads", row["stored"], src)
    if src.lower().endswith(".pdf"): return src
    try:
        return doctools.docx_to_pdf(src, tmp)
    except Exception:
        raise engine.ReviewError("Could not convert the Word document to PDF. Save it as PDF and upload that.")
