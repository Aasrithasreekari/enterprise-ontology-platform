"""Static operator console served at /ui."""

import re

import pytest
from fastapi.testclient import TestClient

from conftest import new_service
from ontology_platform.api.app import create_app
from ontology_platform.api.ui import STATIC_DIR


@pytest.fixture(scope="module")
def client():
    svc = new_service()
    return TestClient(create_app(settings=svc.settings, service=svc))


def test_ui_redirects_and_serves_page(client):
    r = client.get("/ui", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/ui/"
    page = client.get("/ui/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    html = page.text
    for tab in ("Ask", "Explorer", "Impact &amp; deployments", "Documents", "History", "Ingestion"):
        assert f">{tab}</button>" in html
    assert 'id="api-key"' in html and 'type="password"' in html


def test_static_assets_served_with_security_headers(client):
    for path, ctype in (("/ui/app.js", "javascript"), ("/ui/styles.css", "text/css"), ("/ui/favicon.svg", "image/svg+xml")):
        r = client.get(path)
        assert r.status_code == 200 and ctype in r.headers["content-type"], path
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
    assert client.get("/ui/missing.js").status_code == 404


def test_ui_is_self_contained_and_has_no_secrets():
    for f in STATIC_DIR.iterdir():
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"(src|href)=\"https?://", text), f"{f.name} loads an external resource"
        assert not re.search(r"op_[A-Za-z0-9]{16,}", text), f"{f.name} contains something that looks like an API key"
        assert "localStorage" not in text
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "sessionStorage" in js and "Authorization" in js and "Bearer" in js
    assert "innerHTML" not in js  # API data is rendered as text only
    assert "console.log" not in js


def test_ui_does_not_change_api_contracts(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert "/ui" not in client.get("/openapi.json").json()["paths"]
