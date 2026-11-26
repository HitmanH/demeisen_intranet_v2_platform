"""End-to-end checks of the platform on SQLite: sign-in, home, each function, audit, verify, and the rule that
each function touches only its own tables. LibreOffice is needed for the NDA and issue tests (skipped without it)."""
import io, os, re, shutil, sqlite3, sys
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
HAS_SOFFICE = shutil.which("soffice") is not None


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DATA", str(tmp_path))
    for k in ("DB_HOST", "OIDC", "OIDC_ISSUER", "OIDC_SECRET_NAME", "FILES_BUCKET", "AUDIT_BUCKET", "AUTH_ONLY", "ORIGIN_VERIFY",
              "PLATFORM_CLOUD", "AZURE_STORAGE_ACCOUNT", "INTRANET_SECRET", "DB_PASSWORD", "DB_MIGRATOR_PASSWORD", "LOCAL_SIGNIN",
              "SIGNIN_DEFAULT", "LOCAL_ISSUER", "CRM_REDIRECT_URIS", "OIDC_SIGNING_KEY", "ACCOUNTS_KEY",
              "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for m in [m for m in sys.modules if m.split(".")[0] in ("portal", "mnda", "memo", "documents", "contracts")]:
        del sys.modules[m]
    from portal.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    c.tmp = tmp_path
    return c


def login(c, email="russell.hart@demeisen.co.uk"):
    r = c.post("/login", data={"email": email, "password": "changeme"})
    assert r.status_code == 302, r.data


def test_sign_in_required_and_home(client):
    assert client.get("/").status_code == 302
    login(client)
    html = client.get("/").get_data(as_text=True)
    for label in ("NDAs", "Memos", "Issue document", "Verify", "Audit", "New Mutual NDA", "New expenditure memo", "Contracts"):
        assert label in html
    assert 'href="/crm/"' in html   # the Demeisen bar links to the CRM behind the same front door


def test_one_database_same_tables_as_v1(client):
    login(client)
    con = sqlite3.connect(os.path.join(client.tmp, "intranet.db"))
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert {"users", "ndas", "issues", "memos", "signoffs", "audit"} <= tables
    assert tables - {"users", "ndas", "issues", "memos", "signoffs", "audit"} - {"contracts"} == {"schema_migrations", "accounts", "oauth_codes"}


def test_memo_sign_off_by_every_member(client):
    login(client)
    r = client.post("/memos/new", data={"title": "Bloomberg terminal", "amount": "24000", "currency": "GBP",
                                       "supplier": "Bloomberg", "body": "Annual licence"})
    assert r.status_code == 302
    mid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    client.post(f"/memos/{mid}", data={"action": "Approved"})
    assert "Awaiting sign-off" in client.get(f"/memos/{mid}").get_data(as_text=True)
    client.get("/logout"); login(client, "richard.heath@demeisen.co.uk")
    assert "Awaiting your sign-off" in client.get("/").get_data(as_text=True)
    client.post(f"/memos/{mid}", data={"action": "Approved"})
    assert "Approved" in client.get(f"/memos/{mid}/print").get_data(as_text=True)
    audit = client.get("/audit").get_data(as_text=True)
    assert audit.count("Approved") >= 2 and "created" in audit


def test_reject_needs_reason(client):
    login(client)
    r = client.post("/memos/new", data={"title": "x", "amount": "1", "currency": "GBP", "supplier": "s", "body": "b"})
    mid = int(r.headers["Location"].split("/")[-1])
    r = client.post(f"/memos/{mid}", data={"action": "Rejected"}, follow_redirects=True)
    assert "give a reason" in r.get_data(as_text=True)


@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice not installed")
def test_nda_generated_and_verifiable(client):
    login(client)
    r = client.post("/nda/new", data={"counterparty": "Example Data Ltd", "cp_details": "", "cp_type": "data_vendor",
        "purpose": "", "conf_period": "2 years", "changes": "", "effective_date": "2026-11-17", "our_signatory": "Russell Hart",
        "our_title": "Member", "our_email": "russell.hart@demeisen.co.uk", "cp_signatory": "Sam Rivers",
        "cp_signatory_title": "CEO", "cp_email": "sam@example.com"})
    assert r.status_code == 302
    nid = int(r.headers["Location"].split("/")[-1])
    page = client.get(f"/nda/{nid}").get_data(as_text=True)
    pdf = re.search(r'href="/files/generated/([^"]+\.pdf)"', page).group(1)
    data = client.get(f"/files/generated/{pdf}").data
    assert data[:4] == b"%PDF"
    r = client.post("/verify", data={"file": (io.BytesIO(data), "x.pdf")}, content_type="multipart/form-data")
    assert "Example Data Ltd" in r.get_data(as_text=True)
    assert client.get("/mnda/assets/onenda_badge.png").status_code == 200


@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice not installed")
def test_issue_register_and_verify(client):
    from docx import Document
    login(client)
    d = Document(); d.add_paragraph("Dear {{PROSPECT}}, ref {{REF}}"); buf = io.BytesIO(); d.save(buf); buf.seek(0)
    r = client.post("/issue", data={"prospect": "Harbour Endowment", "file": (buf, "deck.docx")},
                    content_type="multipart/form-data", follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Issued DOC-" in html
    pdf = re.search(r'href="/files/generated/([^"]+\.pdf)"', html).group(1)
    data = client.get(f"/files/generated/{pdf}").data
    r = client.post("/verify", data={"file": (io.BytesIO(data), "fwd.pdf")}, content_type="multipart/form-data")
    assert "Harbour Endowment" in r.get_data(as_text=True)


def test_functions_touch_only_their_own_tables():
    """Each function's SQL may name only its own tables, plus `users` and `audit` (shared by the portal)."""
    from portal.dbconn import TABLE_OWNERS
    shared = {"users", "audit"}
    for module in ("mnda", "memo", "documents", "contracts"):
        src = open(os.path.join(ROOT, module, "__init__.py"), encoding="utf-8").read()
        used = set(re.findall(r"\b(?:from|into|update|join|table if not exists)\s+(\w+)", src, re.I))
        foreign = {t for t in used if t in TABLE_OWNERS and TABLE_OWNERS[t] not in (module, "portal")}
        assert not foreign, f"{module} uses {foreign}"
        assert used & set(TABLE_OWNERS) - shared, module


def test_origin_verify_blocks_direct_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DATA", str(tmp_path)); monkeypatch.setenv("ORIGIN_VERIFY", "s3cret")
    for m in [m for m in sys.modules if m.split(".")[0] in ("portal", "mnda", "memo", "documents", "contracts")]:
        del sys.modules[m]
    from portal.app import create_app
    c = create_app().test_client()
    assert c.get("/login").status_code == 403
    assert c.get("/login", headers={"X-Origin-Verify": "s3cret"}).status_code == 200
    assert c.get("/healthz").status_code == 200


def test_admin_task_rejects_unconfirmed_drops():
    from portal import lambda_handler
    assert "error" in lambda_handler.bootstrap({"dropDatabases": ["crm"]})


def _fresh_app_module(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("PLATFORM_DATA", str(tmp_path))
    for k in ("DB_HOST", "OIDC", "OIDC_ISSUER", "OIDC_SECRET_NAME", "FILES_BUCKET", "AUDIT_BUCKET", "AUTH_ONLY",
              "ORIGIN_VERIFY", "PLATFORM_CLOUD", "AZURE_STORAGE_ACCOUNT", "INTRANET_SECRET"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items(): monkeypatch.setenv(k, v)
    for m in [m for m in sys.modules if m.split(".")[0] in ("portal", "mnda", "memo", "documents", "contracts")]:
        del sys.modules[m]
    from portal import app
    return app


def test_azure_refuses_built_in_passwords(tmp_path, monkeypatch):
    app = _fresh_app_module(monkeypatch, tmp_path, PLATFORM_CLOUD="azure", INTRANET_SECRET="s" * 40)
    with pytest.raises(RuntimeError, match="shared sign-in"):
        app.create_app()


def test_azure_refuses_default_session_secret(tmp_path, monkeypatch):
    app = _fresh_app_module(monkeypatch, tmp_path, PLATFORM_CLOUD="azure", OIDC_ISSUER="https://issuer.example",
                            OIDC_CLIENT_ID="c", OIDC_CLIENT_SECRET="s", PUBLIC_URL="https://app.example")
    with pytest.raises(RuntimeError, match="INTRANET_SECRET"):
        app.create_app()


def test_azure_storage_switches_files_and_audit_to_the_cloud(tmp_path, monkeypatch):
    _fresh_app_module(monkeypatch, tmp_path, AZURE_STORAGE_ACCOUNT="acct")
    from portal import storage, core
    assert storage.REMOTE and storage.AUDIT_REMOTE
    assert not core.GEN.startswith(str(tmp_path))   # scratch folders, not the local data folder


def test_db_password_comes_from_env_per_login(monkeypatch):
    for m in [m for m in sys.modules if m.split(".")[0] == "portal"]: del sys.modules[m]
    from portal import dbconn
    seen = {}
    class FakePymysql:
        class cursors: DictCursor = object
        @staticmethod
        def connect(**kw): seen.update(kw); return object()
    monkeypatch.setitem(sys.modules, "pymysql", FakePymysql)
    monkeypatch.setenv("DB_HOST", "h"); monkeypatch.setenv("DB_PASSWORD", "app-pw"); monkeypatch.setenv("DB_MIGRATOR_PASSWORD", "mig-pw")
    dbconn.mysql_connect(); assert seen["password"] == "app-pw" and seen["user"] == "platform_app"
    dbconn.mysql_connect(user="platform_migrator"); assert seen["password"] == "mig-pw"


MYSQL_RESERVED = set("""accessible add all alter analyze and as asc asensitive before between bigint binary blob both by call
cascade case change char character check collate column condition constraint continue convert create cross cube
cume_dist current_date current_time current_timestamp current_user cursor database databases day_hour
day_microsecond day_minute day_second dec decimal declare default delayed delete dense_rank desc describe
deterministic distinct distinctrow div double drop dual each else elseif empty enclosed escaped except exists exit
explain false fetch first_value float float4 float8 for force foreign from fulltext function generated get grant group
grouping groups having high_priority hour_microsecond hour_minute hour_second if ignore in index infile inner inout
insensitive insert int int1 int2 int3 int4 int8 integer intersect interval into io_after_gtids io_before_gtids is
iterate join json_table key keys kill lag last_value lateral lead leading leave left like limit linear lines load
localtime localtimestamp lock long longblob longtext loop low_priority master_bind master_ssl_verify_server_cert match
maxvalue mediumblob mediumint mediumtext middleint minute_microsecond minute_second mod modifies natural not
no_write_to_binlog nth_value ntile null numeric of on optimize optimizer_costs option optionally or order out outer
outfile over partition percent_rank precision primary procedure purge range rank read reads read_write real recursive
references regexp release rename repeat replace require resignal restrict return revoke right rlike row row_number rows
schema schemas second_microsecond select sensitive separator set show signal smallint spatial specific sql
sql_big_result sql_calc_found_rows sql_small_result sqlexception sqlstate sqlwarning ssl starting stored straight_join
system table terminated then tinyblob tinyint tinytext to trailing trigger true undo union unique unlock unsigned update
usage use using utc_date utc_time utc_timestamp values varbinary varchar varcharacter varying virtual when where while
window with write xor year_month zerofill""".split())


def test_mysql_migrations_avoid_reserved_words():
    """Column and table names must not be MySQL reserved words: SQLite accepts them, so only the live deploy would fail
    (as `stored` did in contracts/001)."""
    for m in [m for m in sys.modules if m.split(".")[0] in ("portal", "mnda", "memo", "documents", "contracts")]:
        del sys.modules[m]
    from portal.app import load_functions
    from portal.core import REGISTRY
    load_functions()
    for module, migrations in REGISTRY.migrations.items():
        for mid, sql in migrations:
            for stmt in sql["mysql"].split(";"):
                body = re.search(r"create table if not exists\s+(\w+)\s*\((.*)\)\s*$", stmt.strip(), re.S | re.I)
                if not body: continue
                names = [body.group(1)] + [c.strip().split()[0] for c in re.split(r",(?![^()]*\))", body.group(2))]
                bad = [n for n in names if n.lower() in MYSQL_RESERVED and not n.lower().startswith(("index", "primary", "unique", "key"))]
                assert not bad, f"{module}/{mid}: reserved word(s) {bad}"
