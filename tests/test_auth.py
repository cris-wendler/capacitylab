# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signing in to the web UI: what a session is, what it is not, and what stays reachable without one."""

import base64
import json

import pytest
from fastapi.testclient import TestClient

from capacitylab.settings import Settings
from capacitylab.web.app import create_app
from capacitylab.web.auth import COOKIE, Auth, hash_password, verify_password

PASSWORD = "correct-horse-battery"


@pytest.fixture
def secured(tmp_path):
    settings = Settings(runs_dir=tmp_path, web_password_hash=hash_password(PASSWORD),
                        session_secret="test-secret-not-a-real-one", session_hours=1)
    with TestClient(create_app(settings, inline_jobs=True)) as client:
        yield client


@pytest.fixture
def open_ui(tmp_path):
    with TestClient(create_app(Settings(runs_dir=tmp_path), inline_jobs=True)) as client:
        yield client


def sign_in(client, password=PASSWORD, **extra):
    return client.post("/login", data={"password": password, **extra}, follow_redirects=False)


def test_hashing_never_keeps_the_password_and_compares_the_right_way():
    stored = hash_password(PASSWORD)
    assert PASSWORD not in stored and stored.startswith("pbkdf2_sha256$600000$")
    assert verify_password(PASSWORD, stored_hash=stored)
    assert not verify_password("wrong", stored_hash=stored)
    assert not verify_password(PASSWORD, stored_hash="not-a-hash")
    assert not verify_password(PASSWORD, stored_hash="argon2$1$x$y")  # only the algorithm we wrote is accepted
    assert hash_password(PASSWORD) != stored  # a fresh salt every time
    # A plain password in .env also works, for someone who cannot be bothered to hash it.
    assert verify_password(PASSWORD, stored_plain=PASSWORD)
    assert not verify_password(PASSWORD, stored_hash=None, stored_plain=None)


def test_everything_needs_a_session_and_signing_in_grants_one(secured):
    for path in ("/", "/runs", "/lab", "/aws", "/gcp", "/azure"):
        response = secured.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login?next=")

    assert secured.get("/login").status_code == 200
    assert "Sign in" in secured.get("/login").text

    response = sign_in(secured)
    assert response.status_code == 303 and response.headers["location"] == "/"
    cookie = response.cookies[COOKIE]
    assert PASSWORD not in cookie  # the cookie carries an expiry and a signature, nothing else

    home = secured.get("/")
    assert home.status_code == 200 and "Decision record" in home.text
    assert "Sign out" in home.text


def test_a_wrong_password_grants_nothing_and_then_locks_out(secured):
    response = sign_in(secured, "nope")
    assert response.status_code == 401 and COOKIE not in response.cookies
    assert "does not match" in response.text

    for _ in range(4):
        sign_in(secured, "nope")
    locked = sign_in(secured, PASSWORD)  # the right password, but too late
    assert locked.status_code == 429 and "Too many attempts" in locked.text
    assert secured.get("/", follow_redirects=False).status_code == 303


def test_a_tampered_or_expired_cookie_is_not_a_session(secured, tmp_path):
    auth = Auth(password_hash=hash_password(PASSWORD), secret="test-secret-not-a-real-one", hours=1)
    valid = auth.issue(now=1000.0)

    class FakeRequest:
        def __init__(self, cookie):
            self.cookies = {COOKIE: cookie} if cookie else {}

    assert auth.session_user(FakeRequest(valid), now=1000.0) == "admin"
    assert auth.session_user(FakeRequest(valid), now=1000.0 + 3601) is None  # past its hour
    assert auth.session_user(FakeRequest(valid + "x"), now=1000.0) is None  # signature broken
    assert auth.session_user(FakeRequest(None), now=1000.0) is None
    assert auth.session_user(FakeRequest("nonsense"), now=1000.0) is None

    # Forging a later expiry without the secret does not work: the signature is over the payload.
    body = base64.urlsafe_b64encode(json.dumps({"exp": 9e12}).encode()).decode().rstrip("=")
    forged = f"{body}.{valid.rsplit('.', 1)[1]}"
    assert auth.session_user(FakeRequest(forged), now=1000.0) is None

    # A session signed with a different secret is not a session here either.
    other = Auth(password_hash=hash_password(PASSWORD), secret="a-different-secret", hours=1)
    assert auth.session_user(FakeRequest(other.issue(now=1000.0)), now=1000.0) is None

    secured.cookies.set(COOKIE, forged)
    assert secured.get("/", follow_redirects=False).status_code == 303


def test_signing_out_ends_the_session(secured):
    sign_in(secured)
    assert secured.get("/").status_code == 200
    out = secured.post("/logout", follow_redirects=False)
    assert out.status_code == 303 and out.headers["location"] == "/login"
    secured.cookies.clear()
    assert secured.get("/", follow_redirects=False).status_code == 303


def test_the_health_check_and_static_files_stay_open(secured):
    assert secured.get("/healthz").status_code == 200
    assert secured.get("/static/app.css").status_code == 200


def test_json_callers_get_401_rather_than_a_redirect_to_a_form(secured):
    response = secured.get("/jobs/whatever.json", follow_redirects=False)
    assert response.status_code == 401 and response.json()["error"] == "sign in first"


def test_the_redirect_target_cannot_send_you_off_site(secured):
    response = sign_in(secured, next="https://example.invalid/steal")
    assert response.status_code == 303 and response.headers["location"] == "/"
    secured.cookies.clear()
    assert sign_in(secured, next="/runs").headers["location"] == "/runs"  # a real page is honoured


def test_with_no_password_configured_the_ui_stays_open(open_ui):
    assert open_ui.get("/").status_code == 200
    assert "Sign out" not in open_ui.get("/").text
    assert open_ui.get("/login", follow_redirects=False).status_code == 303  # nothing to sign in to


def test_serve_refuses_a_public_address_without_a_password(tmp_path, capsys, monkeypatch):
    from capacitylab.cli import main

    monkeypatch.setenv("CAPACITYLAB_RUNS_DIR", str(tmp_path))
    assert main(["serve", "--host", "0.0.0.0"]) == 2  # noqa: S104 - the address being refused
    assert "refusing to serve" in capsys.readouterr().err
