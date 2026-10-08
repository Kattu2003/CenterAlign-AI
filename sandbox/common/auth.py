"""Session login and CSRF helpers shared by the sandbox apps."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from urllib.parse import quote, urlsplit

from fastapi import Request


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    salt, _, digest = stored.partition("$")
    candidate = hashlib.sha256((salt + password).encode()).hexdigest()
    return hmac.compare_digest(candidate, digest)


class LoginRequired(Exception):
    """Raised by `require_user`; the app turns it into a redirect to /login?next=..."""

    def __init__(self, next_path: str) -> None:
        super().__init__(next_path)
        self.next_path = next_path


def current_user(request: Request) -> str | None:
    return request.session.get("user")


def require_user(request: Request) -> str:
    user = current_user(request)
    if user:
        return user
    if request.method == "GET":
        next_path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    else:
        # After a lost session, send the user back to the form they came from;
        # the data they submitted is gone (realistic, and it is what chaos tests).
        referer = request.headers.get("referer", "")
        parts = urlsplit(referer)
        same_origin = parts.netloc in ("", request.url.netloc)
        next_path = (parts.path or "/") if referer and same_origin else "/"
    raise LoginRequired(next_path)


def login_redirect_url(next_path: str) -> str:
    return f"/login?next={quote(next_path, safe='/')}"


def safe_next(next_path: str | None) -> str:
    """Only allow local redirects after login."""
    if not next_path or not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    return next_path


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


def csrf_ok(request: Request, submitted: str | None) -> bool:
    expected = request.session.get("csrf")
    return bool(expected and submitted and hmac.compare_digest(expected, submitted))
