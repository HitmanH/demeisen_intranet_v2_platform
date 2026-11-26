"""Own sign-in (portal/accounts.py): setup link -> password + authenticator code, lock-out, Settings -> Users,
and the sign-in service the CRM uses (authorization code + PKCE, signed ID token)."""
import base64, hashlib, os, re, sys, time, urllib.parse
import pytest, pyotp, jwt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CRM_CB = "https://crm.demeisen.io/crm/"
RUSSELL = "russell.hart@demeisen.co.uk"


@pytest.fixture()
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DATA", str(tmp_path))
    for k in ("DB_HOST", "OIDC", "OIDC_ISSUER", "OIDC_SECRET_NAME", "FILES_BUCKET", "AUDIT_BUCKET", "AUTH_ONLY", "ORIGIN_VERIFY",
              "PLATFORM_CLOUD", "AZURE_STORAGE_ACCOUNT", "INTRANET_SECRET", "DB_PASSWORD", "DB_MIGRATOR_PASSWORD",
              "SIGNIN_DEFAULT", "OIDC_SIGNING_KEY", "ACCOUNTS_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LOCAL_SIGNIN", "1")
    monkeypatch.setenv("LOCAL_ISSUER", "https://mycroft.demeisen.io")
    monkeypatch.setenv("CRM_REDIRECT_URIS", CRM_CB + ",https://mycroft.demeisen.io/crm/")
    for m in [m for m in sys.modules if m.split(".")[0] in ("portal", "mnda", "memo", "documents", "contracts")]:
        del sys.modules[m]
    from portal.app import create_app
    app = create_app(); app.config["TESTING"] = True
    cl = app.test_client(); cl.app = app
    return cl


def csrf(c, path):
    html = c.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1), html


def old_login(c, email=RUSSELL):   # the built-in password (local only), standing in for the previous sign-in
    assert c.post("/login", data={"email": email, "password": "changeme"}).status_code == 302


def setup_link(c, uid=1):
    tok, _ = csrf(c, "/settings/users")
    html = c.post("/settings/users", data={"csrf": tok, "action": "link", "uid": uid}).get_data(as_text=True)
    return re.search(r'value="(https://mycroft\.demeisen\.io/setup/[^"]+)"', html).group(1)


def do_setup(c, link, pw="correct horse battery"):
    path = urllib.parse.urlparse(link).path
    tok, html = csrf(c, path)
    secret = re.search(r"<code>([A-Z2-7]+)</code>", html).group(1)
    r = c.post(path, data={"csrf": tok, "password": pw, "password2": pw, "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 302, r.data
    return secret


def own_signin(c, secret, email=RUSSELL, pw="correct horse battery", code=None, nxt="/"):
    tok, _ = csrf(c, "/signin")
    r = c.post("/signin?next=" + urllib.parse.quote(nxt), data={"csrf": tok, "email": email, "password": pw})
    assert r.headers["Location"].endswith("/signin/code"), r.data
    tok, _ = csrf(c, "/signin/code")
    return c.post("/signin/code", data={"csrf": tok, "code": code or pyotp.TOTP(secret).at(time.time() + 30)})


def fresh(c):
    with c.session_transaction() as s: s.clear()


def test_setup_link_then_password_and_code(c):
    old_login(c); link = setup_link(c); fresh(c)
    secret = do_setup(c, link)
    assert c.get(urllib.parse.urlparse(link).path).status_code == 404          # works once
    r = own_signin(c, secret)
    assert r.status_code == 302 and r.headers["Location"] == "/"
    assert "Settings" in c.get("/").get_data(as_text=True)
    con = c.app.extensions                                                        # signed in on its own sign-in
    with c.session_transaction() as s: assert s["local"] and s["uid"] == 1


def test_wrong_password_and_code_lock_after_five(c):
    old_login(c); secret = do_setup(c, setup_link(c)); fresh(c)
    for _ in range(5):
        tok, _ = csrf(c, "/signin")
        c.post("/signin", data={"csrf": tok, "email": RUSSELL, "password": "nope nope nope"})
    tok, _ = csrf(c, "/signin")
    r = c.post("/signin", data={"csrf": tok, "email": RUSSELL, "password": "correct horse battery"})
    assert r.status_code == 429 and b"locked" in r.data


def test_code_cannot_be_reused(c):
    old_login(c); secret = do_setup(c, setup_link(c)); fresh(c)
    code = pyotp.TOTP(secret).at(time.time() + 30)
    assert own_signin(c, secret, code=code).status_code == 302
    fresh(c)
    r = own_signin(c, secret, code=code)
    assert r.status_code == 200 and b"didn&#39;t work" in r.data


def test_forms_need_csrf_token(c):
    old_login(c)
    assert c.post("/settings/users", data={"action": "link", "uid": 1}).status_code == 400
    fresh(c)
    assert c.post("/signin", data={"email": RUSSELL, "password": "x"}).status_code == 400


def test_only_administrators_manage_users(c):
    old_login(c, "richard.heath@demeisen.co.uk")
    assert c.get("/settings/users").status_code == 403
    assert "Settings" not in c.get("/").get_data(as_text=True)


def test_add_person_gives_setup_link_and_audit(c):
    old_login(c)
    tok, _ = csrf(c, "/settings/users")
    html = c.post("/settings/users", data={"csrf": tok, "action": "add", "name": "Test Person", "email": "Test@Demeisen.co.uk"}).get_data(as_text=True)
    assert "/setup/" in html and "test@demeisen.co.uk" in html
    audit = c.get("/audit").get_data(as_text=True)
    assert "user added" in audit and "setup link issued" not in audit


def test_crm_sign_in_code_flow_with_pkce(c):
    old_login(c); secret = do_setup(c, setup_link(c)); fresh(c)
    verifier = "v" * 50
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    q = {"client_id": "demeisen-crm", "redirect_uri": CRM_CB, "response_type": "code", "scope": "openid email",
         "state": "st", "nonce": "nn", "code_challenge": challenge, "code_challenge_method": "S256"}
    auth = "/oauth2/authorize?" + urllib.parse.urlencode(q)
    r = c.get(auth)
    assert "/auth/login" in r.headers["Location"] or "/login" in r.headers["Location"] or "/signin" in r.headers["Location"]
    r = own_signin(c, secret, nxt=auth)
    assert r.headers["Location"] == auth
    r = c.get(auth); loc = urllib.parse.urlparse(r.headers["Location"])
    assert loc.geturl().startswith(CRM_CB)
    code = urllib.parse.parse_qs(loc.query)["code"][0]
    form = {"grant_type": "authorization_code", "client_id": "demeisen-crm", "code": code, "redirect_uri": CRM_CB}
    assert c.post("/oauth2/token", data={**form, "code_verifier": "w" * 50}).status_code == 400   # wrong verifier
    r = c.get(auth); code = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["Location"]).query)["code"][0]
    r = c.post("/oauth2/token", data={**form, "code": code, "code_verifier": verifier}, headers={"Origin": "https://crm.demeisen.io"})
    assert r.status_code == 200 and r.headers["Access-Control-Allow-Origin"] == "https://crm.demeisen.io"
    idt = r.get_json()["id_token"]
    assert c.post("/oauth2/token", data={**form, "code": code, "code_verifier": verifier}).status_code == 400   # once only
    keys = c.get("/oauth2/jwks").get_json()["keys"]
    key = jwt.PyJWK(keys[0]).key
    claims = jwt.decode(idt, key, algorithms=["RS256"], audience="demeisen-crm", issuer="https://mycroft.demeisen.io")
    assert claims["email"] == RUSSELL and claims["nonce"] == "nn"
    assert c.get("/.well-known/openid-configuration").get_json()["token_endpoint"] == "https://mycroft.demeisen.io/oauth2/token"


def test_crm_rejects_unknown_redirect(c):
    q = {"client_id": "demeisen-crm", "redirect_uri": "https://evil.example/", "response_type": "code",
         "code_challenge": "x", "code_challenge_method": "S256"}
    assert c.get("/oauth2/authorize?" + urllib.parse.urlencode(q)).status_code == 400


def test_disabled_person_is_signed_out(c):
    old_login(c); link = setup_link(c, uid=2); fresh(c)
    secret = do_setup(c, link)
    assert own_signin(c, secret, email="richard.heath@demeisen.co.uk").status_code == 302
    rich = c.app.test_client()
    with rich.session_transaction() as s: pass
    old_login(rich)
    tok, _ = csrf(rich, "/settings/users")
    rich.post("/settings/users", data={"csrf": tok, "action": "disable", "uid": 2})
    assert c.get("/").status_code == 302


def test_browser_that_used_own_signin_is_sent_back_to_it(c, monkeypatch):
    from portal import core
    old_login(c); secret = do_setup(c, setup_link(c)); fresh(c)
    r = own_signin(c, secret)
    assert "signin=own" in r.headers.get("Set-Cookie", "")
    fresh(c)
    with c.app.test_request_context("/x", headers={"Cookie": "signin=own"}):
        monkeypatch.setenv("SIGNIN_DEFAULT", "cognito")
        c.app.extensions["demeisen_oidc"] = type("O", (), {"login_url": lambda self, n: "/auth/login?next=" + n, "email": lambda self: None})()
        try:
            assert core.sign_in_url("/x").startswith("/signin")
        finally:
            del c.app.extensions["demeisen_oidc"]
    with c.app.test_request_context("/x"):
        c.app.extensions["demeisen_oidc"] = type("O", (), {"login_url": lambda self, n: "/auth/login?next=" + n, "email": lambda self: None})()
        try:
            assert core.sign_in_url("/x").startswith("/auth/login")   # other browsers (Richard) keep the previous sign-in
        finally:
            del c.app.extensions["demeisen_oidc"]


def test_crm_people_never_see_the_intranet_address(c, monkeypatch):
    from portal import core
    monkeypatch.setenv("SIGNIN_URL", "https://crm.demeisen.io")
    old_login(c)
    tok, _ = csrf(c, "/settings/users")
    html = c.post("/settings/users", data={"csrf": tok, "action": "link", "uid": 2}).get_data(as_text=True)
    assert "https://crm.demeisen.io/setup/" in html and "mycroft.demeisen.io/setup" not in html
    fresh(c)
    oidc = type("O", (), {"login_url": lambda self, n: "https://mycroft.demeisen.io/auth/login?next=" + n, "email": lambda self: None})()
    monkeypatch.setenv("SIGNIN_DEFAULT", "cognito")
    c.app.extensions["demeisen_oidc"] = oidc
    try:
        crm = {"X-Forwarded-Host": "crm.demeisen.io"}
        with c.app.test_request_context("/oauth2/authorize", headers=crm):
            assert core.sign_in_url("/oauth2/authorize").startswith("/signin")     # never the previous sign-in there
        page = c.get("/signin", headers=crm).get_data(as_text=True)
        assert "mycroft" not in page and "previous sign-in" not in page
        with c.app.test_request_context("/x", headers={"X-Forwarded-Host": "mycroft.demeisen.io"}):
            assert "auth/login" in core.sign_in_url("/x")                          # the intranet's address is unchanged
    finally:
        del c.app.extensions["demeisen_oidc"]
