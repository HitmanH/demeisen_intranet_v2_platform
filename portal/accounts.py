"""Demeisen's own sign-in: email + password + authenticator-app code (TOTP). One set of people, managed by
administrators in the intranet under Settings -> Users, signs in to the intranet (mycroft.demeisen.io) and the CRM
(crm.demeisen.io and mycroft.demeisen.io/crm/).

The intranet is the sign-in hub. For the CRM it is a minimal OpenID Connect provider (/oauth2/authorize,
/oauth2/token, /oauth2/jwks, /.well-known/openid-configuration, /oauth2/logout) for the one public client
`demeisen-crm` (authorization code + PKCE, redirect addresses fixed in CRM_REDIRECT_URIS).

Move-over, one person at a time:
  - LOCAL_SIGNIN=1 turns it on. SIGNIN_DEFAULT says where someone who isn't signed in is sent: "cognito" (the
    trial: everyone keeps the old sign-in unless they go to /signin themselves) or "local" (after the move).
  - While Cognito is still configured, /signin offers "use the previous sign-in", so nobody is ever locked out.
  - Either kind of sign-in is accepted everywhere, including by the CRM through the hub.

Security choices:
  - Nobody (administrator included) sees or sets another person's password or 2FA. An administrator issues a
    one-time setup link (valid 24 hours, stored only as a hash); the person chooses their password and scans
    their own authenticator code with it. Issuing a new link wipes the old password and 2FA.
  - Passwords: at least 12 characters, scrypt hashes. 2FA secrets are encrypted (ACCOUNTS_KEY).
  - 5 wrong passwords or codes lock the account for 15 minutes; a code can't be used twice; sessions last 8 hours.
  - Every sign-in, failure, lock-out and administrator action goes to the audit trail (and its write-once copy).
"""
import base64, hashlib, io, json, os, secrets, time, urllib.parse
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, session, url_for, flash, make_response

from . import core

bp = Blueprint("accounts", __name__, template_folder="templates")

MIGRATIONS = [
    ("002_accounts", {
        "sqlite": """
create table if not exists accounts(user_id integer primary key, pw_hash text, totp_secret text, totp_on int default 0,
  totp_last_step int default 0, failed int default 0, locked_until text, setup_hash text, setup_expires text,
  pending_secret text, last_signin text);
create table if not exists oauth_codes(code_hash text primary key, user_id int, client_id text, redirect_uri text,
  challenge text, nonce text, expires text, used int default 0)""",
        "mysql": """
create table if not exists accounts(user_id int primary key, pw_hash text, totp_secret text, totp_on int default 0,
  totp_last_step bigint default 0, failed int default 0, locked_until varchar(32), setup_hash varchar(128),
  setup_expires varchar(32), pending_secret text, last_signin varchar(32), index(setup_hash));
create table if not exists oauth_codes(code_hash varchar(128) primary key, user_id int, client_id varchar(64),
  redirect_uri text, challenge varchar(128), nonce text, expires varchar(32), used int default 0)"""}),
]
APP_GRANTS = {"accounts": "SELECT, INSERT, UPDATE", "oauth_codes": "SELECT, INSERT, UPDATE, DELETE"}

MAX_FAILS, LOCK_MINUTES, SETUP_HOURS, MIN_PW = 5, 15, 24, 12
CRM_CLIENT = os.environ.get("CRM_CLIENT_ID", "demeisen-crm")
TOKEN_HOURS = 8
_IP_FAILS = {}


# ---------------------------------------------------------------- settings
def enabled():
    return os.environ.get("LOCAL_SIGNIN") == "1"


def issuer():
    return os.environ.get("LOCAL_ISSUER", request.host_url.rstrip("/")).rstrip("/")


def cognito():
    return current_app.extensions.get("demeisen_oidc")


def crm_redirects():
    return [u.strip() for u in os.environ.get("CRM_REDIRECT_URIS", "").split(",") if u.strip()]


def now(): return datetime.now(timezone.utc)
def iso(t): return t.isoformat(timespec="seconds")
def h(s): return hashlib.sha256(s.encode()).hexdigest()


def _fernet():
    from cryptography.fernet import Fernet
    raw = os.environ.get("ACCOUNTS_KEY") or ("dev:" + current_app.secret_key)
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))


def enc(s): return _fernet().encrypt(s.encode()).decode()
def dec(s): return _fernet().decrypt(s.encode()).decode()


_DEV_KEY = None
def signing_key():
    """RSA private key (PEM) that signs the CRM's sign-in tokens: OIDC_SIGNING_KEY from Key Vault; a throwaway one locally."""
    global _DEV_KEY
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    pem = os.environ.get("OIDC_SIGNING_KEY")
    if pem: return serialization.load_pem_private_key(pem.encode(), password=None)
    if os.environ.get("PLATFORM_CLOUD") == "azure": raise RuntimeError("OIDC_SIGNING_KEY is not set")
    if _DEV_KEY is None: _DEV_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _DEV_KEY


def _kid(pub):
    n = pub.public_numbers().n.to_bytes((pub.key_size + 7) // 8, "big")
    return hashlib.sha256(n).hexdigest()[:16]


# ---------------------------------------------------------------- csrf (forms on this blueprint)
def csrf_token():
    if "csrf" not in session: session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def check_csrf():
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "~")): abort(400)


# ---------------------------------------------------------------- account helpers
def account(uid):
    db = core.db()
    a = db.execute("select * from accounts where user_id=?", (uid,)).fetchone()
    if not a:
        db.execute("insert into accounts(user_id) values(?)", (uid,)); db.commit()
        a = db.execute("select * from accounts where user_id=?", (uid,)).fetchone()
    return a


def locked(a):
    return bool(a["locked_until"]) and a["locked_until"] > iso(now())


def note_fail(uid, what):
    db = core.db(); a = account(uid)
    n = (a["failed"] or 0) + 1
    until = iso(now() + timedelta(minutes=LOCK_MINUTES)) if n >= MAX_FAILS else a["locked_until"]
    db.execute("update accounts set failed=?, locked_until=? where user_id=?", (0 if n >= MAX_FAILS else n, until, uid))
    db.commit()
    _audit(uid, f"sign-in failed ({what})" + (f"; locked for {LOCK_MINUTES} minutes" if n >= MAX_FAILS else ""), uid=uid)


def _audit(eid, action, detail="", uid=None):
    core.audit("account", eid, action, detail, by=uid)


def ip_limited():
    t = time.time(); key = request.remote_addr
    recent = [x for x in _IP_FAILS.get(key, []) if t - x < 900]
    _IP_FAILS[key] = recent
    return len(recent) >= 20


def ip_fail(): _IP_FAILS.setdefault(request.remote_addr, []).append(time.time())


def totp_ok(secret, code, last_step):
    """Returns the time step the code matched (to stop re-use), or None."""
    import pyotp
    code = (code or "").replace(" ", "")
    if not code.isdigit() or len(code) != 6: return None
    t = pyotp.TOTP(secret)
    step_now = int(time.time()) // 30
    for step in (step_now - 1, step_now, step_now + 1):
        if step > (last_step or 0) and secrets.compare_digest(t.at(step * 30), code): return step
    return None


def safe_next(nxt):
    if nxt and nxt.startswith("/") and not nxt.startswith("//"): return nxt
    return "/"


# ---------------------------------------------------------------- sign-in
@bp.route("/signin", methods=["GET", "POST"])
def signin():
    if not enabled(): abort(404)
    nxt = safe_next(request.args.get("next"))
    if request.method == "POST":
        check_csrf()
        if ip_limited():
            flash("Too many attempts from this network. Try again in 15 minutes.", "error"); return render_template("signin.html", nxt=nxt, prev=_prev(nxt)), 429
        from werkzeug.security import check_password_hash
        email = request.form.get("email", "").strip().lower()
        u = core.db().execute("select * from users where email=? and active=1", (email,)).fetchone()
        a = account(u["id"]) if u else None
        if a and locked(a):
            flash(f"This account is locked after too many attempts. Try again in {LOCK_MINUTES} minutes.", "error")
            return render_template("signin.html", nxt=nxt, prev=_prev(nxt)), 429
        if u and a["pw_hash"] and a["totp_on"] and check_password_hash(a["pw_hash"], request.form.get("password", "")):
            session.clear(); session["pending"] = {"uid": u["id"], "at": int(time.time()), "next": nxt}
            return redirect(url_for("accounts.signin_code"))
        ip_fail()
        if u: note_fail(u["id"], "password")
        flash("Email or password not recognised.", "error")
    return render_template("signin.html", nxt=nxt, prev=_prev(nxt))


def _prev(nxt):
    o = cognito()
    return o.login_url(nxt) if o is not None and hasattr(o, "login_url") else None


@bp.route("/signin/code", methods=["GET", "POST"])
def signin_code():
    p = session.get("pending")
    if not enabled() or not p or time.time() - p["at"] > 300: session.pop("pending", None); return redirect(url_for("accounts.signin"))
    if request.method == "POST":
        check_csrf()
        a = account(p["uid"])
        if locked(a):
            session.clear(); flash(f"This account is locked after too many attempts. Try again in {LOCK_MINUTES} minutes.", "error")
            return redirect(url_for("accounts.signin"))
        step = totp_ok(dec(a["totp_secret"]), request.form.get("code"), a["totp_last_step"])
        if step:
            db = core.db()
            db.execute("update accounts set failed=0, locked_until=null, totp_last_step=?, last_signin=? where user_id=?",
                       (step, iso(now()), p["uid"])); db.commit()
            session.clear(); session.permanent = True
            session["uid"] = p["uid"]; session["local"] = True; session["signed_in_at"] = int(time.time())
            _audit(p["uid"], "signed in", request.remote_addr or "", uid=p["uid"])
            return redirect(p["next"])
        ip_fail(); note_fail(p["uid"], "code")
        flash("That code didn't work. Check your authenticator app and try again.", "error")
    return render_template("signin_code.html")


@bp.route("/signout")
def signout():
    uid, own = session.get("uid"), session.get("local")
    if not own and cognito() is not None: return redirect("/auth/logout")
    session.clear()
    if uid: _audit(uid, "signed out", uid=uid)
    return redirect(url_for("accounts.signin") if enabled() else "/")


# ---------------------------------------------------------------- one-time setup link
def _setup_account(token):
    if not token: return None
    a = core.db().execute("select * from accounts where setup_hash=?", (h(token),)).fetchone()
    if not a or not a["setup_expires"] or a["setup_expires"] < iso(now()): return None
    u = core.db().execute("select * from users where id=? and active=1", (a["user_id"],)).fetchone()
    return (a, u) if u else None


def qr_svg(uri):
    import qrcode, qrcode.image.svg
    buf = io.BytesIO(); qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8).save(buf)
    return buf.getvalue().decode()


@bp.route("/setup/<token>", methods=["GET", "POST"])
def setup(token):
    import pyotp
    found = _setup_account(token)
    if not found: return render_template("setup_invalid.html"), 404
    a, u = found
    db = core.db()
    if not a["pending_secret"]:
        db.execute("update accounts set pending_secret=? where user_id=?", (enc(pyotp.random_base32()), u["id"])); db.commit()
        a = account(u["id"])
    secret = dec(a["pending_secret"])
    uri = pyotp.TOTP(secret).provisioning_uri(name=u["email"], issuer_name="Demeisen")
    if request.method == "POST":
        check_csrf()
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        if len(pw) < MIN_PW: flash(f"Use at least {MIN_PW} characters.", "error")
        elif pw != pw2: flash("The two passwords don't match.", "error")
        elif pw.lower() in (u["email"], u["name"].lower()): flash("Choose a password that isn't your name or email.", "error")
        else:
            step = totp_ok(secret, request.form.get("code"), 0)
            if not step: flash("That code didn't work. Scan the QR code again and enter the 6-digit code shown.", "error")
            else:
                from werkzeug.security import generate_password_hash
                db.execute("""update accounts set pw_hash=?, totp_secret=?, totp_on=1, totp_last_step=?, pending_secret=null,
                              setup_hash=null, setup_expires=null, failed=0, locked_until=null where user_id=?""",
                           (generate_password_hash(pw, method="scrypt"), enc(secret), step, u["id"])); db.commit()
                _audit(u["id"], "password and 2FA set up", uid=u["id"])
                session.clear(); flash("All set. Sign in with your email, new password and authenticator code.")
                return redirect(url_for("accounts.signin"))
    return render_template("setup.html", u=u, qr=qr_svg(uri), secret=secret, min_pw=MIN_PW)


def new_setup_link(uid):
    """Wipes the person's password and 2FA and returns a fresh one-time setup link (shown once to the administrator)."""
    token = secrets.token_urlsafe(32)
    core.db().execute("""update accounts set setup_hash=?, setup_expires=?, pw_hash=null, totp_secret=null, totp_on=0,
                         pending_secret=null, failed=0, locked_until=null where user_id=?""",
                      (h(token), iso(now() + timedelta(hours=SETUP_HOURS)), uid))
    core.db().commit()
    return f"{issuer()}/setup/{token}"


# ---------------------------------------------------------------- Settings -> Users (administrators)
def admin_required(f):
    @wraps(f)
    @core.login_required
    def w(*a, **k):
        u = core.me()
        if not u or not u["admin"]: abort(403)
        return f(*a, **k)
    return w


@bp.route("/settings/users", methods=["GET", "POST"])
@admin_required
def users():
    db = core.db(); link = None; me = core.me()
    if request.method == "POST":
        check_csrf()
        act, uid = request.form.get("action"), request.form.get("uid", type=int)
        if act == "add":
            name, email = request.form.get("name", "").strip(), request.form.get("email", "").strip().lower()
            if not name or "@" not in email: flash("Add a name and a valid email.", "error")
            elif db.execute("select id from users where email=?", (email,)).fetchone(): flash("That email already has an account.", "error")
            else:
                db.execute("insert into users(name,email,admin,active) values(?,?,?,1)", (name, email, int(bool(request.form.get("admin")))))
                db.commit()
                uid = db.execute("select id from users where email=?", (email,)).fetchone()["id"]
                account(uid); link = (uid, new_setup_link(uid))
                _audit(uid, "user added", f"{name} <{email}>{' (administrator)' if request.form.get('admin') else ''}")
        elif uid and db.execute("select id from users where id=?", (uid,)).fetchone():
            if act == "link":
                account(uid); link = (uid, new_setup_link(uid)); _audit(uid, "setup link issued (password and 2FA reset)")
            elif act == "unlock":
                db.execute("update accounts set failed=0, locked_until=null where user_id=?", (uid,)); db.commit(); _audit(uid, "unlocked")
            elif act in ("disable", "enable") and uid != me["id"]:
                db.execute("update users set active=? where id=?", (int(act == "enable"), uid)); db.commit(); _audit(uid, f"user {act}d")
            elif act in ("make_admin", "remove_admin") and uid != me["id"]:
                db.execute("update users set admin=? where id=?", (int(act == "make_admin"), uid)); db.commit(); _audit(uid, act.replace("_", " "))
    rows = db.execute("""select u.*, a.totp_on, a.locked_until, a.last_signin, a.setup_expires
                         from users u left join accounts a on a.user_id=u.id order by u.name""").fetchall()
    resp = make_response(render_template("users.html", rows=rows, link=link, now=iso(now()), me_id=me["id"],
                                         own=enabled()))
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- sign-in service for the CRM (OpenID Connect)
def _cors(resp):
    origin = request.headers.get("Origin", "")
    if origin and any(r.startswith(origin + "/") for r in crm_redirects()):
        resp.headers["Access-Control-Allow-Origin"] = origin; resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "content-type"; resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/.well-known/openid-configuration")
def oidc_config():
    if not enabled(): abort(404)
    i = issuer()
    return jsonify(issuer=i, authorization_endpoint=f"{i}/oauth2/authorize", token_endpoint=f"{i}/oauth2/token",
                   jwks_uri=f"{i}/oauth2/jwks", end_session_endpoint=f"{i}/oauth2/logout", response_types_supported=["code"],
                   subject_types_supported=["public"], id_token_signing_alg_values_supported=["RS256"],
                   code_challenge_methods_supported=["S256"], scopes_supported=["openid", "email"])


@bp.route("/oauth2/jwks")
def jwks():
    if not enabled(): abort(404)
    pub = signing_key().public_key(); n = pub.public_numbers()
    b = lambda i: base64.urlsafe_b64encode(i.to_bytes((i.bit_length() + 7) // 8, "big")).decode().rstrip("=")
    return jsonify(keys=[{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": _kid(pub), "n": b(n.n), "e": b(n.e)}])


@bp.route("/oauth2/authorize")
def authorize():
    if not enabled(): abort(404)
    q = request.args
    if q.get("client_id") != CRM_CLIENT or q.get("redirect_uri") not in crm_redirects(): abort(400)
    if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" or not q.get("code_challenge"): abort(400)
    uid = core.signed_in_uid()
    u = core.db().execute("select * from users where id=? and active=1", (uid,)).fetchone() if uid else None
    if not u: return redirect(core.sign_in_url(request.full_path))
    code = secrets.token_urlsafe(32)
    db = core.db()
    db.execute("delete from oauth_codes where expires<?", (iso(now()),))
    db.execute("insert into oauth_codes(code_hash,user_id,client_id,redirect_uri,challenge,nonce,expires) values(?,?,?,?,?,?,?)",
               (h(code), u["id"], CRM_CLIENT, q["redirect_uri"], q["code_challenge"], q.get("nonce", ""), iso(now() + timedelta(seconds=60))))
    db.commit()
    sep = "&" if "?" in q["redirect_uri"] else "?"
    return redirect(q["redirect_uri"] + sep + urllib.parse.urlencode({"code": code, "state": q.get("state", "")}))


@bp.route("/oauth2/token", methods=["POST", "OPTIONS"])
def token():
    if not enabled(): abort(404)
    if request.method == "OPTIONS": return _cors(make_response("", 204))
    f = request.form; db = core.db()
    bad = lambda: _cors(make_response(jsonify(error="invalid_grant"), 400))
    if f.get("grant_type") != "authorization_code" or f.get("client_id") != CRM_CLIENT: return bad()
    c = db.execute("select * from oauth_codes where code_hash=?", (h(f.get("code", "")),)).fetchone()
    if not c or c["used"] or c["expires"] < iso(now()) or c["redirect_uri"] != f.get("redirect_uri"): return bad()
    db.execute("update oauth_codes set used=1 where code_hash=?", (c["code_hash"],)); db.commit()
    verifier = f.get("code_verifier", "")
    calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    if not verifier or not secrets.compare_digest(calc, c["challenge"]): return bad()
    u = db.execute("select * from users where id=? and active=1", (c["user_id"],)).fetchone()
    if not u: return bad()
    import jwt
    key = signing_key(); t = int(time.time())
    claims = {"iss": issuer(), "aud": CRM_CLIENT, "sub": str(u["id"]), "email": u["email"], "email_verified": True,
              "name": u["name"], "iat": t, "exp": t + TOKEN_HOURS * 3600, "token_use": "id"}
    if c["nonce"]: claims["nonce"] = c["nonce"]
    idt = jwt.encode(claims, key, algorithm="RS256", headers={"kid": _kid(key.public_key())})
    _audit(u["id"], "signed in to the CRM", uid=u["id"])
    return _cors(make_response(jsonify(id_token=idt, access_token=idt, token_type="Bearer", expires_in=TOKEN_HOURS * 3600)))


@bp.route("/oauth2/logout")
def oauth_logout():
    if not enabled(): abort(404)
    back = request.args.get("logout_uri", "")
    back = back if back in crm_redirects() else None
    if not session.get("local") and cognito() is not None and session.get("auth"):
        return redirect("/auth/logout")          # Cognito session: sign out there too (lands on the intranet)
    session.clear()
    return redirect(back or url_for("accounts.signin"))


def init(app):
    if os.environ.get("PLATFORM_CLOUD") == "azure" and enabled():
        for k in ("ACCOUNTS_KEY", "OIDC_SIGNING_KEY", "LOCAL_ISSUER"):
            if not os.environ.get(k): raise RuntimeError(f"Own sign-in is on (LOCAL_SIGNIN=1) but {k} is not set.")
    app.register_blueprint(bp)
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["own_signin"] = enabled()
