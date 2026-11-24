"""Static operator console served at ``/ui`` (HTML + CSS + vanilla JS, no build step).

The console is a thin client of the public REST API: it holds the operator's API key in browser
``sessionStorage`` and sends it as ``Authorization: Bearer``. Nothing secret is rendered into the
served files, which are fully static.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from starlette.staticfiles import StaticFiles
from starlette.types import Message, Receive, Scope, Send

STATIC_DIR = Path(__file__).resolve().parent / "static"

# The console loads nothing from third parties; the policy keeps it that way.
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


class _SecureStaticFiles(StaticFiles):
    """StaticFiles that adds the console's security headers to every response."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for name, value in SECURITY_HEADERS.items():
                    if name.lower().encode() not in present:
                        headers.append((name.lower().encode(), value.encode()))
                message["headers"] = headers
            await send(message)

        await super().__call__(scope, receive, send_with_headers)


def mount_ui(app: FastAPI, path: str = "/ui") -> None:
    """Serve the console at ``path``/ (``path`` itself redirects there)."""

    @app.get(path, include_in_schema=False)
    def ui_root() -> RedirectResponse:
        return RedirectResponse(url=f"{path}/", status_code=307)

    app.mount(path, _SecureStaticFiles(directory=STATIC_DIR, html=True), name="ui")
