# SPDX-License-Identifier: AGPL-3.0-or-later
"""Password login for the web UI: one password from the environment, and a signed session cookie.

There are no user accounts. CapacityLab is a tool you run for yourself or a small team, so the whole of it is:

    CAPACITYLAB_WEB_PASSWORD_HASH=pbkdf2_sha256$600000$...   # from `capacitylab hash-password`
    CAPACITYLAB_SESSION_SECRET=<random string>               # keeps sessions valid across restarts

The password is never stored in the clear if you use the hash form, comparisons are constant-time, and the cookie
holds only an expiry and a signature - no password, no key material, nothing to decrypt. A cookie whose signature or
expiry does not check out is simply not a session.

With no password configured the UI stays open, which is the default for a tool bound to localhost. Serving it on any
other address without a password is refused rather than silently exposed (see `capacitylab serve`).

SSO belongs behind the same seam: `session_user()` is the only thing the rest of the app asks about.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field

from fastapi import Request

COOKIE = "capacitylab_session"
DEFAULT_HOURS = 12.0
ITERATIONS = 600_000  # PBKDF2-HMAC-SHA256 rounds; slow on purpose
LOCKOUT_AFTER = 5  # failed attempts from one address
LOCKOUT_SECONDS = 60.0
OPEN_PATHS = ("/login", "/logout", "/static", "/healthz")


def hash_password(password: str, *, salt: str | None = None) -> str:
    """`pbkdf2_sha256$<rounds>$<salt>$<hash>`, the form CAPACITYLAB_WEB_PASSWORD_HASH expects."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


def verify_password(password: str, *, stored_hash: str | None = None, stored_plain: str | None = None) -> bool:
    """Constant-time check against a hash, or against a plain password when that is all that is configured."""
    if stored_hash:
        try:
            algorithm, rounds, salt, digest = stored_hash.split("$", 3)
        except ValueError:
            return False
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(candidate, digest)
    if stored_plain:
        return hmac.compare_digest(password, stored_plain)
    return False


@dataclass
class Auth:
    """What the app needs to know about signing in. `required` is False when no password is configured."""

    password_hash: str | None = None
    password_plain: str | None = None
    secret: str = ""
    hours: float = DEFAULT_HOURS
    secret_is_ephemeral: bool = False
    failures: dict[str, list[float]] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings) -> Auth:
        secret = settings.session_secret or ""
        return cls(password_hash=settings.web_password_hash, password_plain=settings.web_password,
                   secret=secret or secrets.token_hex(32), hours=settings.session_hours,
                   secret_is_ephemeral=not secret)

    @property
    def required(self) -> bool:
        return bool(self.password_hash or self.password_plain)

    # -- sessions ------------------------------------------------------------------------------------------------

    def issue(self, *, now: float | None = None) -> str:
        """A cookie value: the expiry, signed. Nothing secret travels in it."""
        payload = json.dumps({"exp": (now or time.time()) + self.hours * 3600}, separators=(",", ":"))
        body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return f"{body}.{self._sign(body)}"

    def session_user(self, request: Request, *, now: float | None = None) -> str | None:
        """"admin" for a valid session, None otherwise. The only question the rest of the app asks."""
        if not self.required:
            return "open"
        cookie = request.cookies.get(COOKIE)
        if not cookie or "." not in cookie:
            return None
        body, signature = cookie.rsplit(".", 1)
        if not hmac.compare_digest(signature, self._sign(body)):
            return None
        try:
            padded = body + "=" * (-len(body) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        except (ValueError, json.JSONDecodeError):
            return None
        return "admin" if float(payload.get("exp", 0)) > (now or time.time()) else None

    def _sign(self, body: str) -> str:
        return hmac.new(self.secret.encode(), body.encode(), hashlib.sha256).hexdigest()

    # -- sign-in attempts ----------------------------------------------------------------------------------------

    def locked_for(self, client: str, *, now: float | None = None) -> float:
        """Seconds still to wait after too many failures from this address; 0 when it may try."""
        now = now or time.time()
        recent = [t for t in self.failures.get(client, []) if now - t < LOCKOUT_SECONDS]
        self.failures[client] = recent
        if len(recent) < LOCKOUT_AFTER:
            return 0.0
        return round(LOCKOUT_SECONDS - (now - recent[-LOCKOUT_AFTER]), 1)

    def attempt(self, password: str, client: str, *, now: float | None = None) -> bool:
        """Check a password, recording the failure so guessing gets slow rather than staying free."""
        now = now or time.time()
        if self.locked_for(client, now=now):
            return False
        if verify_password(password, stored_hash=self.password_hash, stored_plain=self.password_plain):
            self.failures.pop(client, None)
            return True
        self.failures.setdefault(client, []).append(now)
        return False


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def same_origin(request: Request) -> bool:
    """A form POST must come from this page. With a SameSite=Lax cookie this is the belt to that's braces."""
    origin = request.headers.get("origin")
    if not origin:
        return True  # no Origin header: a same-origin form post in browsers that omit it, or a CLI client
    return origin.rstrip("/") == str(request.base_url).rstrip("/")
