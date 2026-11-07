"""CSP regression tests — the served UI document must keep a strict policy.

The policy is pinned verbatim so it cannot be silently dropped or loosened;
an intentional change to the header has to update EXPECTED_CSP as well.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self' ws: wss:; font-src 'self'; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
)


def _directive(csp: str, name: str) -> str:
    """Return the value of one CSP directive, or "" when it is absent."""
    for part in csp.split(";"):
        key, _, value = part.strip().partition(" ")
        if key == name:
            return value
    return ""


class TestContentSecurityPolicy:
    def test_root_html_has_strict_csp(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Serve a minimal document through the real route so this assertion does
        # not depend on a local frontend build (CI runs pytest before npm run build).
        (tmp_path / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")
        monkeypatch.setattr("rwmod.server.STATIC_DIR", tmp_path)

        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")

        csp = resp.headers.get("content-security-policy")
        assert csp is not None
        assert _directive(csp, "script-src") == "'self'"
        # The queue feed connects over WebSocket; without ws:/wss: the browser
        # blocks live updates.
        connect_src = _directive(csp, "connect-src").split()
        assert "ws:" in connect_src
        assert "wss:" in connect_src
        assert csp == EXPECTED_CSP

    def test_api_json_has_no_csp(self, client: TestClient) -> None:
        resp = client.get("/api/config")
        assert resp.status_code == 200
        assert "content-security-policy" not in resp.headers

    def test_api_docs_html_is_excluded(self, client: TestClient) -> None:
        # Swagger UI loads its JS from a CDN; applying script-src 'self' here
        # would break /api/docs, so HTML under /api is deliberately exempt.
        resp = client.get("/api/docs")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "content-security-policy" not in resp.headers
