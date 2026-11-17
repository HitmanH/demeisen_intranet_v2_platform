"""Shared sign-in (OpenID Connect, the Demeisen Cognito pool) when configured; built-in passwords otherwise.

On AWS two functions run the same code:
  - the sign-in function (AUTH_ONLY=1, outside the private network) handles /auth/*. It reads the app client's ID,
    secret and the public address from Secrets Manager (OIDC_SECRET_NAME), written there by the app stack.
  - the main function (inside the private network, no internet) only reads the signed session cookie, so it needs
    nothing but OIDC=1 to know that sign-in is shared.
Locally, OIDC_ISSUER / OIDC_CLIENT_ID / OIDC_CLIENT_SECRET / PUBLIC_URL can be set directly instead."""
import json, os
from .demeisen_auth import OIDC


class _SessionOnly:
    """What the main function needs from sign-in: who is signed in (from the session) and where to sign in."""
    def __init__(self, app):
        self._o = OIDC(None, issuer="https://unused", client_id="unused", base_url="https://unused")
    def email(self): return self._o.email()
    def login_url(self, next_path="/"): return self._o.login_url(next_path)


def _settings():
    name = os.environ.get("OIDC_SECRET_NAME")
    if name:
        import boto3
        s = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "eu-west-2")).get_secret_value(SecretId=name)
        return json.loads(s["SecretString"])
    if os.environ.get("OIDC_ISSUER"):
        return {"issuer": os.environ["OIDC_ISSUER"], "client_id": os.environ["OIDC_CLIENT_ID"],
                "client_secret": os.environ.get("OIDC_CLIENT_SECRET"), "public_url": os.environ["PUBLIC_URL"],
                "logout_url": os.environ.get("OIDC_LOGOUT_URL")}
    return None


def setup(app, auth_only):
    if auth_only:
        s = _settings()
        if not s: raise RuntimeError("The sign-in function needs OIDC_SECRET_NAME or OIDC_* settings.")
        return OIDC(app, issuer=s["issuer"], client_id=s["client_id"], client_secret=s.get("client_secret"),
                    base_url=s["public_url"], logout_url=s.get("logout_url"))
    if os.environ.get("OIDC") == "1":
        return _SessionOnly(app)
    s = _settings()
    if s:   # local testing of the full flow in one process
        return OIDC(app, issuer=s["issuer"], client_id=s["client_id"], client_secret=s.get("client_secret"),
                    base_url=s["public_url"], logout_url=s.get("logout_url"))
    return None
