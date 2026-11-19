"""Tiny DB layer for the platform: SQLite locally, MySQL (RDS) when DB_HOST is set.

One database, `intranet`, exactly as in v1 (same tables, rows and IDs). Each function owns its own tables and touches
no others: portal `users`, `audit`; mnda `ndas`; memo `memos`, `signoffs`; documents `issues`.
App code keeps sqlite-style SQL (? placeholders, `insert or ignore`, `insert or replace`); it is translated for MySQL.

On RDS the app signs in as `platform_app` with a short-lived IAM token, so no database password is stored anywhere.
On Azure the passwords come from Key Vault (DB_PASSWORD for the app, DB_MIGRATOR_PASSWORD for migrations) and the
server certificate is checked against the public CA certificates (DB_SSL_CA).
"""
import os, re, sqlite3, time

MYSQL = bool(os.environ.get("DB_HOST"))
TABLE_OWNERS = {"users": "portal", "audit": "portal", "ndas": "mnda", "memos": "memo", "signoffs": "memo", "issues": "documents"}
_TOKEN = {"value": None, "at": 0.0}


class Row(dict):
    """Row usable as r["col"], r[0] and dict(r)."""
    def __getitem__(self, k):
        if isinstance(k, int): return list(self.values())[k]
        return super().__getitem__(k)


def _mysql_sql(sql):
    sql = re.sub(r"(?i)\binsert\s+or\s+ignore\b", "insert ignore", sql)
    sql = re.sub(r"(?i)\binsert\s+or\s+replace\b", "replace", sql)
    return sql.replace("%", "%%").replace("?", "%s")


def iam_token(host, user, port=3306):
    """RDS IAM sign-in token (valid 15 minutes; reused for 10). Signed locally: needs no network access."""
    if _TOKEN["value"] and time.time() - _TOKEN["at"] < 600 and _TOKEN.get("user") == user:
        return _TOKEN["value"]
    import boto3
    tok = boto3.client("rds", region_name=os.environ.get("AWS_REGION", "eu-west-2")).generate_db_auth_token(
        DBHostname=host, Port=port, DBUsername=user)
    _TOKEN.update(value=tok, at=time.time(), user=user)
    return tok


def mysql_connect(user=None, password=None, database="__default__"):
    import pymysql
    host, user = os.environ["DB_HOST"], user or os.environ.get("DB_USER", "platform_app")
    if password is None:
        env = "DB_MIGRATOR_PASSWORD" if user.endswith("_migrator") else "DB_PASSWORD"
        password = os.environ.get(env) or iam_token(host, user)
    return pymysql.connect(host=host, port=int(os.environ.get("DB_PORT", 3306)), user=user, password=password,
                           database=os.environ.get("DB_NAME", "intranet") if database == "__default__" else database, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
                           autocommit=False, ssl={"ca": os.environ.get("DB_SSL_CA", "/app/rds-ca.pem")})


class _Cur:
    def __init__(self, cur): self._c = cur; self.lastrowid = cur.lastrowid; self.rowcount = cur.rowcount
    def _row(self, r):
        if r is None: return None
        return Row(r) if isinstance(r, dict) else Row(zip(r.keys(), tuple(r)))
    def fetchone(self): return self._row(self._c.fetchone())
    def fetchall(self): return [self._row(r) for r in self._c.fetchall()]
    def __iter__(self): return iter(self.fetchall())


class Conn:
    def __init__(self, data_dir=None):
        if MYSQL:
            self._c = mysql_connect()
        else:
            data_dir = data_dir or os.environ.get("PLATFORM_DATA", "local-data")
            os.makedirs(data_dir, exist_ok=True)
            self._c = sqlite3.connect(os.path.join(data_dir, "intranet.db")); self._c.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        if MYSQL:
            cur = self._c.cursor(); cur.execute(_mysql_sql(sql), tuple(params)); return _Cur(cur)
        return _Cur(self._c.execute(sql, params))

    def commit(self): self._c.commit()
    def rollback(self): self._c.rollback()
    def close(self): self._c.close()
