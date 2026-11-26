"""AWS Lambda entry point (Function URL -> Flask), plus admin tasks that only a direct `aws lambda invoke` with AWS
credentials can trigger (web requests never carry a top-level "admin_task" key).

Admin tasks:
  {"admin_task": "bootstrap"}          once per database, as the RDS master user (needs the data stack's
                                       AdminEndpoint on): creates the `intranet` database if missing and the IAM-only
                                       `platform_migrator` login. Options: "dropDatabases": ["crm"], "dropUsers":
                                       ["intranet"], each with "confirm": "<same list joined by commas>", to remove
                                       another app's data or v1 logins from a copied server.
  {"admin_task": "migrate"}            applies pending schema migrations as platform_migrator, then (re)grants the
                                       app login `platform_app` exactly the rights it needs (audit: read and add only).
  {"admin_task": "status"}             applied and pending migrations
  {"admin_task": "list_ndas"}
  {"admin_task": "delete_nda", "ref": "...", "confirm": "<ref>"}   dry run without confirm; the audit trail is kept
"""
import json, os

_AUTH_ONLY = bool(os.environ.get("AUTH_ONLY"))
_app = None


def app():
    global _app
    if _app is None:
        from portal.app import create_app
        _app = create_app()
    return _app


# What platform_app may do, table by table. The audit trail can be read and added to, never changed.
APP_GRANTS = {
    "users": "SELECT, INSERT, UPDATE",
    "audit": "SELECT, INSERT",
    "ndas": "SELECT, INSERT, UPDATE, DELETE",
    "memos": "SELECT, INSERT, UPDATE",
    "signoffs": "SELECT, INSERT, UPDATE, DELETE",
    "issues": "SELECT, INSERT, UPDATE",
    "contracts": "SELECT, INSERT, UPDATE",     # contract reviews (contracts/)
    "schema_migrations": "SELECT",
    "accounts": "SELECT, INSERT, UPDATE",          # own sign-in (accounts.py)
    "oauth_codes": "SELECT, INSERT, UPDATE, DELETE",
}
PROTECTED = {"mysql", "sys", "information_schema", "performance_schema", "intranet"}


def _master():
    import boto3
    from portal import dbconn
    s = json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["MASTER_SECRET_ARN"])["SecretString"])
    return dbconn.mysql_connect(user=s["username"], password=s["password"], database=None)


def bootstrap(ev):
    drops_db, drops_user = ev.get("dropDatabases", []), ev.get("dropUsers", [])
    if drops_db or drops_user:
        if ev.get("confirm") != ",".join(drops_db + drops_user):
            return {"error": f'To drop these, also pass "confirm": "{",".join(drops_db + drops_user)}"'}
    c = _master()
    try:
        with c.cursor() as k:
            for d in drops_db:
                if d in PROTECTED or not d.isidentifier(): return {"error": f"refusing to drop {d}"}
                k.execute(f"drop database if exists `{d}`")
            for u in drops_user:
                if not u.isidentifier() or u in ("platform_app", "platform_migrator"): return {"error": f"refusing to drop {u}"}
                k.execute("select host from mysql.user where user=%s", (u,))
                for row in k.fetchall(): k.execute(f"drop user '{u}'@'{row['host']}'")
            k.execute("create database if not exists `intranet` character set utf8mb4 collate utf8mb4_unicode_ci")
            k.execute("create user if not exists 'platform_migrator'@'%' identified with AWSAuthenticationPlugin as 'RDS' require ssl")
            k.execute("grant all privileges on `intranet`.* to 'platform_migrator'@'%' with grant option")
            k.execute("grant create user on *.* to 'platform_migrator'@'%'")
        c.commit()
        return {"bootstrapped": True, "dropped": drops_db + drops_user}
    finally:
        c.close()


def _as_migrator():
    from portal import dbconn
    class M(dbconn.Conn):
        def __init__(self):
            self._c = dbconn.mysql_connect(user="platform_migrator")
    return M()


def migrate():
    from portal import schema
    from portal.app import load_functions
    from portal.core import REGISTRY
    load_functions()
    c = _as_migrator()
    try:
        applied = schema.run(c, REGISTRY)
        if os.environ.get("PLATFORM_CLOUD") != "azure":   # AWS only: on Azure the login is created by the Azure setup (step 3)
            c.execute("create user if not exists 'platform_app'@'%' identified with AWSAuthenticationPlugin as 'RDS' require ssl")
        for table, rights in APP_GRANTS.items():
            c.execute(f"grant {rights} on `intranet`.`{table}` to 'platform_app'@'%'")
        c.commit()
        return {"applied": applied, "pending": schema.pending(c, REGISTRY)}
    finally:
        c.close()


def status():
    from portal import schema
    from portal.app import load_functions
    from portal.core import REGISTRY
    load_functions()
    c = _as_migrator()
    try:
        done = [f"{r['module']}/{r['id']}" for r in c.execute("select module, id from schema_migrations order by module, id")] \
            if c.execute("show tables like 'schema_migrations'").fetchone() else []
        return {"applied": done, "pending": schema.pending(c, REGISTRY)}
    finally:
        c.close()


def _delete_nda(ref, confirm):
    from portal.core import db, audit
    with app().test_request_context():
        n = db().execute("select * from ndas where ref=?", (ref,)).fetchone()
        if not n: return {"error": f"no NDA {ref}"}
        info = {"id": n["id"], "ref": n["ref"], "counterparty": n["counterparty"],
                "files": [f for f in (n["docx"], n["pdf"], n["signed"]) if f]}
        if confirm != ref: return {"dry_run": info}
        db().execute("delete from ndas where id=?", (n["id"],)); db().commit()
        audit("nda", n["id"], "deleted by administrator", ref)   # the trail itself is never deleted
        info["s3_keys_to_delete"] = [("uploads/" if f == n["signed"] else "generated/") + f for f in info["files"]]
        return {"deleted": info}


def handler(event, context):
    task = event.get("admin_task") if isinstance(event, dict) else None
    if task == "bootstrap": return bootstrap(event)
    if task == "migrate": return migrate()
    if task == "status": return status()
    if task == "delete_nda": return _delete_nda(event["ref"], event.get("confirm"))
    if task == "list_ndas":
        from portal.core import db
        with app().app_context():
            return [dict(r) for r in db().execute("select id, ref, counterparty, status, created from ndas")]
    if task: return {"error": f"unknown admin task {task}"}
    from apig_wsgi import make_lambda_handler
    global _web
    try: _web
    except NameError: _web = make_lambda_handler(app(), binary_support=True)
    return _web(event, context)
