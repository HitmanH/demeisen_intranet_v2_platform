"""Demeisen platform: the portal (sign-in, home, people, audit, files) hosting the functions listed in
config.FUNCTIONS. Each function is a folder (mnda/, memo/, documents/) with its own blueprint, templates and schema.

    create_app()          the web app (local: python -m portal.app; on AWS: portal/lambda_handler.py)
"""
import importlib, os, time
from flask import Flask, Blueprint, render_template, request, redirect, url_for, session, flash, send_from_directory, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from . import config, core, dbconn, schema, storage
from .core import db, me, login_required, REGISTRY

bp = Blueprint("portal", __name__, template_folder="templates")
_FAILS = {}

MIGRATIONS = [
    ("001_users_audit", {
        "sqlite": """
create table if not exists users(id integer primary key, name text, email text unique, pw text,
  admin int default 0, active int default 1);
create table if not exists audit(id integer primary key, at text, user_id int, entity text, entity_id int,
  action text, detail text)""",
        "mysql": """
create table if not exists users(id int auto_increment primary key, name varchar(255), email varchar(255) unique,
  pw text, admin int default 0, active int default 1);
create table if not exists audit(id int auto_increment primary key, at varchar(32), user_id int, entity varchar(32),
  entity_id int, action text, detail text, index(entity, entity_id))"""}),
]


# --------------------------------------------------------------- sign-in (built-in passwords: local / standalone only)
@bp.route("/login", methods=["GET", "POST"])
def login():
    oidc = _oidc()
    if oidc: return redirect(oidc.login_url(request.args.get("next") or "/"))
    if request.method == "POST":
        key = request.remote_addr; t = time.time()
        recent = [x for x in _FAILS.get(key, []) if t - x < 900]
        if len(recent) >= 5:
            flash("Too many failed attempts - try again in 15 minutes", "error")
            return render_template("login.html"), 429
        u = db().execute("select * from users where email=? and active=1",
                         (request.form["email"].strip().lower(),)).fetchone()
        if u and u["pw"] and check_password_hash(u["pw"], request.form["password"]):
            _FAILS.pop(key, None); session.permanent = True; session["uid"] = u["id"]
            nxt = request.args.get("next") or "/"
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        _FAILS[key] = recent + [t]
        flash("Invalid login", "error")
    return render_template("login.html")


@bp.route("/logout")
def logout():
    if session.get("local"): return redirect(url_for("accounts.signout"))
    if _oidc(): return redirect("/auth/logout")
    session.clear(); return redirect(url_for("portal.login"))


@bp.route("/password", methods=["GET", "POST"])
@login_required
def password():
    if _oidc(): return redirect("/")       # passwords are managed by the shared sign-in
    if request.method == "POST":
        if not check_password_hash(me()["pw"], request.form["old"]): flash("Current password wrong", "error")
        elif len(request.form["new"]) < 8: flash("Use at least 8 characters", "error")
        else:
            db().execute("update users set pw=? where id=?", (generate_password_hash(request.form["new"]), me()["id"]))
            db().commit(); flash("Password changed"); return redirect("/")
    return render_template("password.html")


# --------------------------------------------------------------- home, files, audit
@bp.route("/")
@login_required
def home():
    cards = [c for c in (fn(session["uid"]) for fn in REGISTRY.home_cards) if c]
    return render_template("home.html", cards=cards, tiles=REGISTRY.home_tiles)


@bp.route("/files/<kind>/<path:name>")
@login_required
def files(kind, name):
    if kind not in ("generated", "uploads"): abort(404)
    if storage.REMOTE: return storage.download_response(kind, secure_filename(name))
    return send_from_directory(os.path.abspath(core.GEN if kind == "generated" else core.UPL), name, as_attachment=True)


@bp.route("/audit")
@login_required
def audit_log():
    rows = db().execute("select a.*, u.name from audit a left join users u on u.id=a.user_id order by a.id desc limit 500").fetchall()
    return render_template("audit.html", rows=rows)


@bp.route("/healthz")
def healthz(): return "ok"


# --------------------------------------------------------------- app factory
def _oidc():
    from flask import current_app
    return current_app.extensions.get("demeisen_oidc")


def load_functions(names=None):
    """Import each function package and let it register its blueprint, menu entries, cards and migrations."""
    REGISTRY.__init__()
    REGISTRY.migrations["portal"] = MIGRATIONS
    from . import accounts
    REGISTRY.migrations["accounts"] = accounts.MIGRATIONS
    mods = []
    for name in names if names is not None else config.FUNCTIONS:
        m = importlib.import_module(name)
        m.register(REGISTRY)
        mods.append(m)
    return mods


def seed_members(conn):
    """Local / standalone use only: the members in config.py, with password `changeme`."""
    for m in config.MEMBERS:
        conn.execute("insert or ignore into users(name,email,pw,admin) values(?,?,?,?)",
                     (m["name"], m["email"], generate_password_hash("changeme"), int(m["admin"])))
    conn.commit()


def create_app(functions=None, auth_only=None):
    auth_only = bool(os.environ.get("AUTH_ONLY")) if auth_only is None else auth_only
    app = Flask(__name__)
    app.secret_key = config.SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
    if os.environ.get("PLATFORM_PROD"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
        app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                          SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=8 * 3600)
    verify = os.environ.get("ORIGIN_VERIFY")
    if verify:   # only requests that came through the Demeisen front door (CloudFront adds this header)
        @app.before_request
        def _from_front_door():
            if request.path != "/healthz" and request.headers.get("X-Origin-Verify") != verify: abort(403)

    from . import signin
    oidc = signin.setup(app, auth_only)
    if os.environ.get("PLATFORM_CLOUD") == "azure" and oidc is None:
        raise RuntimeError("On Azure, shared sign-in (OIDC_ISSUER etc.) must be configured; built-in passwords are off.")
    if os.environ.get("PLATFORM_CLOUD") == "azure" and config.SECRET_KEY in ("", "dev-change-me"):
        raise RuntimeError("On Azure, INTRANET_SECRET must be set (from Key Vault).")
    if oidc is not None: app.extensions["demeisen_oidc"] = oidc
    app.teardown_appcontext(core.close_db)
    if auth_only:            # the sign-in-only function: no database, no pages
        return app

    mods = load_functions(functions)
    app.register_blueprint(bp)
    from . import accounts
    accounts.init(app)
    for m in mods: app.register_blueprint(m.bp)

    @app.context_processor
    def inject():
        return {"cfg": config, "me": me() if session.get("uid") else None, "shared_signin": oidc is not None,
                "nav": REGISTRY.nav}

    @app.template_filter("fromiso")
    def fromiso(s): return s.replace("T", " ") if s else ""

    if not dbconn.MYSQL:      # local: create the schemas and the members straight away
        with app.app_context():
            schema.run(db(), REGISTRY, log=lambda *_: None); seed_members(db())
    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=5000, debug=True)
