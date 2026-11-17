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
    for k in ("DB_HOST", "OIDC", "OIDC_ISSUER", "OIDC_SECRET_NAME", "FILES_BUCKET", "AUDIT_BUCKET", "AUTH_ONLY", "ORIGIN_VERIFY"):
        monkeypatch.delenv(k, raising=False)
    for m in [m for m in sys.modules if m.split(".")[0] in ("portal", "mnda", "memo", "documents")]:
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
    for label in ("NDAs", "Memos", "Issue document", "Verify", "Audit", "New Mutual NDA", "New expenditure memo"):
        assert label in html
    assert 'href="/crm/"' in html   # the Demeisen bar links to the CRM behind the same front door


def test_one_database_same_tables_as_v1(client):
    login(client)
    con = sqlite3.connect(os.path.join(client.tmp, "intranet.db"))
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert {"users", "ndas", "issues", "memos", "signoffs", "audit"} <= tables
    assert tables - {"users", "ndas", "issues", "memos", "signoffs", "audit"} == {"schema_migrations"}


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
    for module in ("mnda", "memo", "documents"):
        src = open(os.path.join(ROOT, module, "__init__.py"), encoding="utf-8").read()
        used = set(re.findall(r"\b(?:from|into|update|join|table if not exists)\s+(\w+)", src, re.I))
        foreign = {t for t in used if t in TABLE_OWNERS and TABLE_OWNERS[t] not in (module, "portal")}
        assert not foreign, f"{module} uses {foreign}"
        assert used & set(TABLE_OWNERS) - shared, module


def test_origin_verify_blocks_direct_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DATA", str(tmp_path)); monkeypatch.setenv("ORIGIN_VERIFY", "s3cret")
    for m in [m for m in sys.modules if m.split(".")[0] in ("portal", "mnda", "memo", "documents")]:
        del sys.modules[m]
    from portal.app import create_app
    c = create_app().test_client()
    assert c.get("/login").status_code == 403
    assert c.get("/login", headers={"X-Origin-Verify": "s3cret"}).status_code == 200
    assert c.get("/healthz").status_code == 200


def test_admin_task_rejects_unconfirmed_drops():
    from portal import lambda_handler
    assert "error" in lambda_handler.bootstrap({"dropDatabases": ["crm"]})
