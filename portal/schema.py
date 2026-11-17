"""Schema migrations. Each function lists its own in its MIGRATIONS (id, {"sqlite": ..., "mysql": ...}); they are
applied in order and recorded in schema_migrations, so each runs once. Locally they run at start-up; on AWS
they run as `platform_migrator` through the admin task "migrate" (the app's own login cannot change tables).
Never edit a migration that has run: add a new one."""
from . import dbconn

_TRACK = {
    "sqlite": "create table if not exists schema_migrations(module text, id text, applied_at text, primary key(module, id))",
    "mysql": "create table if not exists schema_migrations(module varchar(32), id varchar(64), applied_at varchar(32), primary key(module, id))",
}


def run(conn, registry, log=print):
    from datetime import datetime
    eng = "mysql" if dbconn.MYSQL else "sqlite"
    conn.execute(_TRACK[eng])
    done = {(r["module"], r["id"]) for r in conn.execute("select module, id from schema_migrations")}
    applied = []
    for module in ("portal", *[m for m in registry.migrations if m != "portal"]):
        for mid, sql in registry.migrations.get(module, []):
            if (module, mid) in done: continue
            for stmt in [s.strip() for s in sql[eng].split(";") if s.strip()]:
                conn.execute(stmt)
            conn.execute("insert into schema_migrations(module,id,applied_at) values(?,?,?)",
                         (module, mid, datetime.now().isoformat(timespec="seconds")))
            conn.commit(); applied.append(f"{module}/{mid}"); log(f"migration applied {module}/{mid}")
    return applied


def pending(conn, registry):
    eng = "mysql" if dbconn.MYSQL else "sqlite"
    conn.execute(_TRACK[eng])
    done = {(r["module"], r["id"]) for r in conn.execute("select module, id from schema_migrations")}
    return [f"{m}/{i}" for m, ms in registry.migrations.items() for i, _ in ms if (m, i) not in done]
