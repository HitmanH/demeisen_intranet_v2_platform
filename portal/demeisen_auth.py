"""Shared sign-in for Flask apps, using any OpenID Connect provider (AWS Cognito by default).

Single file, no Demeisen-specific settings: copy it into an app (see README). Requires `PyJWT[crypto]`.

    from demeisen_auth import OIDC
    auth = OIDC(app, issuer=..., client_id=..., client_secret=..., base_url="https://intranet.example.com",
                logout_url="https://<domain>.auth.eu-west-2.amazoncognito.com/logout")
    ...
    email = auth.email()        # verified email of the signed-in person, or None

Routes added: /auth/login?next=/x, /auth/callback, /auth/logout, /auth/signed-out.
The signed-in email is kept in Flask's signed session cookie; the app decides what that person may do.
"""
import base64, json, secrets, time, urllib.parse, urllib.request
from flask import Blueprint, abort, redirect, request, session

import jwt

_SKEW = 60


def _get(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


class OIDC:
    def __init__(self, app=None, *, issuer, client_id, client_secret=None, base_url, logout_url=None,
                 scopes="openid email", after_logout="/"):
        self.issuer, self.client_id, self.client_secret = issuer.rstrip("/"), client_id, client_secret
        self.base_url, self.logout_url, self.scopes, self.after_logout = base_url.rstrip("/"), logout_url, scopes, after_logout
        self._meta = None; self._jwks = None
        if app is not None: self.init_app(app)

    # ---------- provider metadata ----------
    @property
    def meta(self):
        if self._meta is None:
            self._meta = _get(f"{self.issuer}/.well-known/openid-configuration")
        return self._meta

    def _verify(self, id_token, nonce):
        if self._jwks is None: self._jwks = jwt.PyJWKClient(self.meta["jwks_uri"])
        key = self._jwks.get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(id_token, key, algorithms=["RS256"], audience=self.client_id, issuer=self.issuer,
                            leeway=_SKEW, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        if claims.get("nonce") != nonce: raise jwt.InvalidTokenError("nonce mismatch")
        if claims.get("email_verified") in (False, "false"): raise jwt.InvalidTokenError("email not verified")
        return claims

    # ---------- public helpers ----------
    def email(self):
        a = session.get("auth")
        if a and a.get("exp", 0) > time.time(): return a["email"]
        return None

    def login_url(self, next_path="/"):
        return "/auth/login?" + urllib.parse.urlencode({"next": next_path})

    # ---------- routes ----------
    def init_app(self, app):
        bp = Blueprint("demeisen_auth", __name__)
        redirect_uri = f"{self.base_url}/auth/callback"

        @bp.route("/auth/login")
        def login():
            nxt = request.args.get("next") or "/"
            if not nxt.startswith("/") or nxt.startswith("//"): nxt = "/"
            state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
            session["auth_req"] = {"state": state, "nonce": nonce, "next": nxt}
            return redirect(self.meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
                "response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
                "scope": self.scopes, "state": state, "nonce": nonce}))

        @bp.route("/auth/callback")
        def callback():
            req = session.pop("auth_req", None)
            if not req or request.args.get("state") != req["state"] or "code" not in request.args:
                return redirect(self.login_url())          # stale or forged response: start again
            form = {"grant_type": "authorization_code", "code": request.args["code"],
                    "redirect_uri": redirect_uri, "client_id": self.client_id}
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if self.client_secret:
                basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
                headers["Authorization"] = f"Basic {basic}"
            try:
                tok = _get(self.meta["token_endpoint"], urllib.parse.urlencode(form).encode(), headers)
                claims = self._verify(tok["id_token"], req["nonce"])
            except Exception:
                abort(401)
            session.clear()
            session.permanent = True
            session["auth"] = {"email": claims["email"].lower(), "sub": claims["sub"], "exp": int(claims["exp"])}
            return redirect(req["next"])

        @bp.route("/auth/logout")
        def logout():
            session.clear()
            if self.logout_url:   # also end the provider's own session (Cognito: /logout)
                return redirect(self.logout_url + "?" + urllib.parse.urlencode({
                    "client_id": self.client_id, "logout_uri": f"{self.base_url}/auth/signed-out"}))
            return redirect(self.after_logout)

        @bp.route("/auth/signed-out")
        def signed_out():
            return redirect(self.after_logout)

        app.register_blueprint(bp)
        return self
