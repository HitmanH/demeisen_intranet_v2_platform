"""Contract review: upload, background review (Claude stubbed out), the review page, categories, re-runs, failures
and the not-yet-configured state. No network: contracts.review.run is replaced in every test."""
import io, os, sys
import pytest
from test_platform import client, login, HAS_SOFFICE   # noqa: F401  (fixture)

REVIEW = {
    "overview": {"document_type": "Mutual NDA", "parties": [{"name": "Allocator Ltd", "role": "Allocator"},
                 {"name": "Demeisen LLP", "role": "Manager"}], "demeisen_position": "Manager seeking an allocation",
                 "dates": "5 November 2026", "governing_law": "England and Wales; no courts named",
                 "summary": "Mutual in form, one-sided in effect.", "overall_risk": "high",
                 "recommendation": "Do not sign until the use restriction is fixed."},
    "drafting_errors": [{"clause": "Cl. 7", "issue": "Refers to paragraph 6", "fix": "Change to paragraph 7"}],
    "key_terms": [{"term": "Term", "clause": "Cl. 6", "summary": "Two years"}],
    "concerns": [{"severity": "high", "clause": "Cl. 5", "title": "Trading carve-out swallows the use restriction",
                  "issue": "Nothing restricts their trading", "why_it_matters": "Strategy leakage",
                  "suggested_change": "Add: provided they do not use our information"},
                 {"severity": "low", "clause": "Cl. 8", "title": "No jurisdiction", "issue": "x", "why_it_matters": "y",
                  "suggested_change": "z"}],
    "financial": {"summary": "No fees.", "fees": [], "liability": [{"item": "Cap", "clause": "None", "detail": "None stated"}]},
    "termination": {"summary": "Two years.", "provisions": [{"item": "Expiry", "clause": "Cl. 6", "detail": "2 years"}],
                    "key_dates": [{"when": "5 November 2028", "what": "Obligations end"}]},
    "missing": [{"item": "Non-solicit", "why": "They hire PMs"}],
}


@pytest.fixture()
def fake_claude(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from contracts import review
    calls = []
    def run(pdf, **kw):
        calls.append((pdf, kw)); assert open(pdf, "rb").read(4) == b"%PDF"
        return REVIEW, {"model": "claude-test", "pages": 1, "tokens_in": 10, "tokens_out": 20}
    monkeypatch.setattr(review, "run", run)
    client.application.config["CONTRACTS_SYNC"] = True
    return calls


def _pdf():
    from reportlab.pdfgen import canvas
    buf = io.BytesIO(); c = canvas.Canvas(buf); c.drawString(72, 720, "1. Definitions"); c.save(); buf.seek(0)
    return buf


def _upload(client, name="nda.pdf", body=None, category="nda", **extra):
    data = {"file": (body or _pdf(), name), "category": category, "counterparty": "Allocator Ltd",
            "context": "Allocator we want money from", **extra}
    return client.post("/contracts", data=data, content_type="multipart/form-data")


def test_upload_review_and_page(client, fake_claude):
    login(client)
    r = _upload(client)
    assert r.status_code == 302
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    for text in ("Trading carve-out swallows the use restriction", "Refers to paragraph 6", "5 November 2028",
                 "Do not sign until", "High risk", "Non-solicit", "None stated"):
        assert text in page, text
    pdf, kw = fake_claude[0]
    assert kw["category"] == "nda" and kw["counterparty"] == "Allocator Ltd" and "money" in kw["context"]
    listing = client.get("/contracts?category=nda").get_data(as_text=True)
    assert "Allocator Ltd" in listing and "NDA / confidentiality (1)" in listing
    assert "Allocator Ltd" not in client.get("/contracts?category=vendor").get_data(as_text=True).split("<table>")[1]
    assert "Recent contract reviews" in client.get("/").get_data(as_text=True)
    audit = client.get("/audit").get_data(as_text=True)
    assert "uploaded" in audit and "reviewed" in audit


@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice not installed")
def test_word_documents_are_sent_as_pdf(client, fake_claude):
    from docx import Document
    login(client)
    d = Document(); d.add_paragraph("1. The Supplier shall..."); buf = io.BytesIO(); d.save(buf); buf.seek(0)
    assert _upload(client, "msa.docx", buf, "vendor").status_code == 302
    assert fake_claude and fake_claude[0][1]["category"] == "vendor"


def test_rejects_other_files_and_categories(client, fake_claude):
    login(client)
    r = client.post("/contracts", data={"file": (io.BytesIO(b"hi"), "x.txt"), "category": "nda"},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "Upload a PDF or Word document" in r.get_data(as_text=True)
    r = client.post("/contracts", data={"file": (_pdf(), "a.pdf"), "category": "bogus"},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "Choose a category" in r.get_data(as_text=True)
    assert not fake_claude


def test_failure_is_shown_and_can_be_rerun(client, fake_claude, monkeypatch):
    from contracts import review
    login(client)
    def boom(pdf, **kw): raise review.ReviewError("Claude's rate limit was reached. Try again in a few minutes.")
    monkeypatch.setattr(review, "run", boom)
    loc = _upload(client).headers["Location"]
    page = client.get(loc).get_data(as_text=True)
    assert "rate limit" in page and "Failed" in page
    monkeypatch.setattr(review, "run", lambda pdf, **kw: (REVIEW, {"model": "m", "pages": 1, "tokens_in": 1, "tokens_out": 1}))
    client.post(loc, data={"action": "details", "category": "other", "counterparty": "New name", "context": ""})
    client.post(loc, data={"action": "rerun"})
    page = client.get(loc).get_data(as_text=True)
    assert "Trading carve-out" in page and "New name" in page and "rate limit" not in page


def test_unexpected_errors_do_not_leak_details(client, fake_claude, monkeypatch):
    from contracts import review
    login(client)
    monkeypatch.setattr(review, "run", lambda pdf, **kw: (_ for _ in ()).throw(KeyError("secret-ish")))
    page = client.get(_upload(client).headers["Location"]).get_data(as_text=True)
    assert "Review failed: KeyError" in page and "secret-ish" not in page


def test_without_api_key_uploads_wait(client):
    login(client)
    assert "not switched on yet" in client.get("/contracts").get_data(as_text=True)
    page = client.get(_upload(client).headers["Location"]).get_data(as_text=True)
    assert "Waiting" in page and "API key is not set" in page


def test_a_running_review_is_not_started_twice(client, fake_claude):
    from portal.core import db
    login(client)
    loc = _upload(client).headers["Location"]; cid = int(loc.rsplit("/", 1)[1])
    with client.application.app_context():
        db().execute("update contracts set status='running', started=? where id=?", ("2999-01-01T00:00:00", cid)); db().commit()
    r = client.post(loc, data={"action": "rerun"}, follow_redirects=True)
    assert "already running" in r.get_data(as_text=True) and len(fake_claude) == 1
    with client.application.app_context():   # long-stuck reviews count as interrupted and may be re-run
        db().execute("update contracts set started=? where id=?", ("2000-01-01T00:00:00", cid)); db().commit()
    assert "Interrupted" in client.get(loc).get_data(as_text=True)
    client.post(loc, data={"action": "rerun"}); assert len(fake_claude) == 2


def test_schema_is_strict():
    """Structured outputs need every object closed and every property required."""
    from contracts.review import SCHEMA
    def walk(s):
        if s.get("type") == "object":
            assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
            for v in s["properties"].values(): walk(v)
        if s.get("type") == "array": walk(s["items"])
    walk(SCHEMA)
