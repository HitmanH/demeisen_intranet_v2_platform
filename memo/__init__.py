"""Expenditure memos: every active LLP member must approve; any rejection blocks it; printable sign-off record.
Owns the tables `memos` and `signoffs`. Uses only portal.core and portal.storage."""
import os, uuid
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, abort
from werkzeug.utils import secure_filename
from portal import core, storage
from portal.core import db, login_required, audit, now, newref

bp = Blueprint("memo", __name__, template_folder="templates")

MIGRATIONS = [
    ("001_memos_signoffs", {
        "sqlite": """create table if not exists memos(id integer primary key, ref text, title text, amount real,
  currency text, supplier text, body text, attachment text, status text, created_by int, created text);
create table if not exists signoffs(id integer primary key, memo_id int, user_id int, decision text,
  comment text, at text, ip text, unique(memo_id, user_id))""",
        "mysql": """create table if not exists memos(id int auto_increment primary key, ref varchar(64), title text, amount double,
  currency text, supplier text, body mediumtext, attachment text, status varchar(64), created_by int, created varchar(32));
create table if not exists signoffs(id int auto_increment primary key, memo_id int, user_id int, decision text,
  comment text, at varchar(32), ip varchar(64), unique(memo_id, user_id))"""}),
]


def register(r):
    r.nav.append(("/memos", "Memos"))
    r.home_tiles.append(("/memos/new", "New expenditure memo", "Sign-off from all LLP members"))
    r.home_cards.append(_home_card)
    r.migrations["memo"] = MIGRATIONS


def _home_card(uid):
    rows = db().execute("""select m.* from memos m where m.status='Awaiting sign-off' and not exists
        (select 1 from signoffs s where s.memo_id=m.id and s.user_id=?)""", (uid,)).fetchall()
    return render_template("memo/_home_card.html", pending=rows) if rows else ""


def _status(mid):
    members = db().execute("select id from users where active=1").fetchall()
    s = {r["user_id"]: r["decision"] for r in db().execute("select * from signoffs where memo_id=?", (mid,))}
    if "Rejected" in s.values(): st = "Rejected"
    elif all(s.get(m["id"]) == "Approved" for m in members): st = "Approved"
    else: st = "Awaiting sign-off"
    db().execute("update memos set status=? where id=? and status!='Withdrawn'", (st, mid)); db().commit()
    return st


@bp.route("/memos")
@login_required
def memo_list():
    rows = db().execute("""select m.*, u.name as author,
        (select count(*) from signoffs s where s.memo_id=m.id and s.decision='Approved') as approvals
        from memos m left join users u on u.id=m.created_by order by m.id desc""").fetchall()
    n = db().execute("select count(*) from users where active=1").fetchone()[0]
    return render_template("memo/list.html", rows=rows, n=n)


@bp.route("/memos/new", methods=["GET", "POST"])
@login_required
def memo_new():
    if request.method == "POST":
        f = request.form; att = None
        if request.files.get("file") and request.files["file"].filename:
            att = secure_filename(f"{uuid.uuid4().hex[:6]}_{request.files['file'].filename}")
            request.files["file"].save(os.path.join(core.UPL, att)); storage.put("uploads", os.path.join(core.UPL, att))
        ref = newref("MEMO")
        cur = db().execute("insert into memos(ref,title,amount,currency,supplier,body,attachment,status,created_by,created) values(?,?,?,?,?,?,?,?,?,?)",
                           (ref, f["title"], float(f["amount"] or 0), f["currency"], f["supplier"], f["body"], att,
                            "Awaiting sign-off", session["uid"], now()))
        db().commit(); audit("memo", cur.lastrowid, "created", ref)
        return redirect(url_for("memo.memo_view", mid=cur.lastrowid))
    return render_template("memo/form.html")


@bp.route("/memos/<int:mid>", methods=["GET", "POST"])
@login_required
def memo_view(mid):
    m = db().execute("select m.*, u.name as author from memos m left join users u on u.id=m.created_by where m.id=?", (mid,)).fetchone() or abort(404)
    if request.method == "POST":
        act = request.form["action"]
        if act in ("Approved", "Rejected") and m["status"] == "Awaiting sign-off":
            if act == "Rejected" and not request.form.get("comment"):
                flash("Please give a reason for rejecting", "error")
            else:
                db().execute("insert or replace into signoffs(memo_id,user_id,decision,comment,at,ip) values(?,?,?,?,?,?)",
                             (mid, session["uid"], act, request.form.get("comment", ""), now(), request.remote_addr))
                db().commit(); audit("memo", mid, act, request.form.get("comment", "")); _status(mid)
        elif act == "withdraw" and m["created_by"] == session["uid"]:
            db().execute("update memos set status='Withdrawn' where id=?", (mid,)); db().commit(); audit("memo", mid, "withdrawn")
        return redirect(url_for("memo.memo_view", mid=mid))
    members = db().execute("""select u.id, u.name, s.decision, s.comment, s.at from users u
        left join signoffs s on s.user_id=u.id and s.memo_id=? where u.active=1 order by u.id""", (mid,)).fetchall()
    mine = next((x for x in members if x["id"] == session["uid"]), None)
    return render_template("memo/view.html", m=m, members=members, mine=mine)


@bp.route("/memos/<int:mid>/print")
@login_required
def memo_print(mid):
    m = db().execute("select m.*, u.name as author from memos m left join users u on u.id=m.created_by where m.id=?", (mid,)).fetchone() or abort(404)
    members = db().execute("""select u.name, s.decision, s.comment, s.at, s.ip from users u
        left join signoffs s on s.user_id=u.id and s.memo_id=? where u.active=1""", (mid,)).fetchall()
    return render_template("memo/print.html", m=m, members=members)
