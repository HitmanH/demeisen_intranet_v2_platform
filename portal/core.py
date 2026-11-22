"""What every platform function shares: the database connection, the signed-in person, the audit trail, file storage
and a small registry through which functions add to the portal (menu, home-page cards, document lookups) without
importing each other. A function (mnda, memo, documents) imports only from here."""
import json, os, tempfile, time, uuid
from datetime import datetime, date, timezone
from functools import wraps
from flask import g, session, request, redirect, url_for, render_template, current_app
from . import dbconn, storage

SESSION_HOURS = 8   # an own sign-in lasts this long

# ---------- working folders for generated files (the cloud copy is the real one when storage.REMOTE) ----------
if storage.REMOTE or os.environ.get("AUTH_ONLY"):
    _SCRATCH = tempfile.mkdtemp(prefix="platform-")
    GEN, UPL = os.path.join(_SCRATCH, "generated"), os.path.join(_SCRATCH, "uploads")
else:
    _DATA = os.environ.get("PLATFORM_DATA", "local-data")
    GEN, UPL = os.path.join(_DATA, "generated"), os.path.join(_DATA, "uploads")
os.makedirs(GEN, exist_ok=True); os.makedirs(UPL, exist_ok=True)


def db():
    if "db" not in g:
        g.db = dbconn.Conn()
    return g.db


def close_db(_=None):
    c = g.pop("db", None)
    if c is not None: c.close()


def now(): return datetime.now().isoformat(timespec="seconds")
def newref(prefix): return f"{prefix}-{date.today():%Y%m}-{uuid.uuid4().hex[:5].upper()}"


def me():
    uid = session.get("uid")
    return db().execute("select * from users where id=?", (uid,)).fetchone() if uid else None


def audit(entity, eid, action, detail="", by=None):
    """Records an action in audit and, when cloud audit storage is set, as a write-once object that nobody (not even
    an administrator) can change or delete until its retention date: the tamper-proof copy of the trail."""
    at = now(); uid = by if by is not None else session.get("uid")
    db().execute("insert into audit(at,user_id,entity,entity_id,action,detail) values(?,?,?,?,?,?)",
                 (at, uid, entity, eid, action, detail))
    db().commit()
    if storage.AUDIT_REMOTE:
        u = db().execute("select * from users where id=?", (uid,)).fetchone() if uid else None
        rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "user_id": uid,
               "user_email": u["email"] if u else None, "entity": entity, "entity_id": eid, "action": action,
               "detail": detail, "ip": request.remote_addr if request else None}
        storage.put_audit(rec)


def signed_in_uid():
    """The signed-in person's users.id, from either sign-in (own: accounts.py; shared: Cognito), or None."""
    uid = session.get("uid")
    if session.get("local"):
        fresh = time.time() - session.get("signed_in_at", 0) < SESSION_HOURS * 3600
        if uid and fresh and db().execute("select id from users where id=? and active=1", (uid,)).fetchone(): return uid
        session.clear(); return None
    oidc = current_app.extensions.get("demeisen_oidc")
    if oidc is not None:
        email = oidc.email()
        if not email: session.pop("uid", None); return None
        if not uid:
            u = db().execute("select id from users where email=? and active=1", (email,)).fetchone()
            if not u: return None
            session["uid"] = uid = u["id"]
        return uid
    return uid


def sign_in_url(next_path):
    """Where someone who isn't signed in goes: own sign-in when SIGNIN_DEFAULT=local (or nothing else is set up),
    otherwise the shared (Cognito) sign-in."""
    import urllib.parse
    oidc = current_app.extensions.get("demeisen_oidc")
    own = os.environ.get("LOCAL_SIGNIN") == "1"
    if own and (os.environ.get("SIGNIN_DEFAULT") == "local" or oidc is None):
        return "/signin?" + urllib.parse.urlencode({"next": next_path})
    if oidc is not None: return oidc.login_url(next_path)
    return url_for("portal.login", next=next_path)


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not signed_in_uid():
            oidc = current_app.extensions.get("demeisen_oidc")
            if oidc is not None and oidc.email() and not session.get("local"):   # signed in to Cognito, but not a member
                return render_template("no_access.html", email=oidc.email()), 403
            return redirect(sign_in_url(request.full_path.rstrip("?")))
        return f(*a, **k)
    return w


# ---------- registry: how functions plug into the portal ----------
class Registry:
    def __init__(self):
        self.nav = []            # (href, label), in menu order
        self.home_tiles = []     # (href, title, description)
        self.home_cards = []     # fn(uid) -> rendered HTML or ""
        self.ref_lookups = []    # fn(ref) -> row with ref, prospect, created, or None (used by Verify)
        self.migrations = {}     # module -> [(id, {"sqlite": sql, "mysql": sql})]


REGISTRY = Registry()


def to_json(v): return json.dumps(v, default=str)
