"""
Pinteresto backend — API key lock (security.py)

The Pins Android app already sends an `X-API-Key` header on every request
(see ApiClient.kt -> authAndHostInterceptor, key stored in app Settings).
This middleware is the matching server-side lock:

- Set ONE secret in the server environment:  PINS_API_KEY=<long random key>
- The same key goes into the Pins app (Settings -> API Key, one time) and
  into the browser once via the /key page (a long-lived cookie remembers it).
- If PINS_API_KEY is NOT set, the lock stays OFF (open) and a warning is
  printed at startup, so a deploy can never lock the owner out by accident.

Also included: docs pages are disabled in main.py, and the two chat
endpoints get a small per-IP rate limit so the free LLM quota cannot be
drained by a script.
"""

import os
import secrets
import time
from collections import defaultdict, deque
from string import Template

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE_NAME = "pins_auth"
COOKIE_MAX_AGE = 60 * 60 * 24 * 180  # 180 days — the app/browser asks rarely

# Paths that must stay reachable without a key.
PUBLIC_PATHS = {"/api/ping", "/key"}

# Chat endpoints burn paid/free LLM quota — cap them per IP.
RATE_LIMITED_PATHS = {"/api/chat", "/api/cmo-chat"}
RATE_LIMIT_MAX = 30          # requests
RATE_LIMIT_WINDOW = 60.0     # seconds

_hits: dict[str, deque] = defaultdict(deque)


def _expected_key() -> str:
    return os.getenv("PINS_API_KEY", "").strip()


if not _expected_key():
    print(
        "⚠️  SECURITY: PINS_API_KEY is not set — dashboard and API are OPEN. "
        "Set PINS_API_KEY in the server environment to enable the lock."
    )


def _key_matches(candidate: str | None) -> bool:
    expected = _expected_key()
    if not expected or not candidate:
        return False
    return secrets.compare_digest(candidate.strip(), expected)


def _request_key(request: Request) -> str | None:
    """Key can arrive as the app header, a Bearer token, or the browser cookie."""
    header = request.headers.get("x-api-key")
    if header:
        return header
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return request.cookies.get(COOKIE_NAME)


def _rate_limited(request: Request) -> bool:
    if request.url.path not in RATE_LIMITED_PATHS:
        return False
    ip = request.client.host if request.client else "unknown"
    bucket = _hits[(ip, request.url.path)]
    now = time.monotonic()
    while bucket and now - bucket[0] > RATE_LIMIT_WINDOW:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_MAX:
        return True
    bucket.append(now)
    return False


KEY_PAGE = Template("""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pinteresto — Unlock</title>
<style>
 body{font-family:system-ui,sans-serif;background:#fff7ed;display:flex;
      align-items:center;justify-content:center;min-height:100vh;margin:0}
 .card{background:#fff;padding:2rem;border-radius:16px;max-width:340px;width:90%;
      box-shadow:0 8px 30px rgba(0,0,0,.08);text-align:center}
 input{width:100%;padding:.7rem;border:1px solid #ddd;border-radius:10px;
      margin:.8rem 0;box-sizing:border-box}
 button{width:100%;padding:.7rem;border:0;border-radius:10px;background:#e60023;
      color:#fff;font-weight:600;cursor:pointer}
 .err{color:#c00;font-size:.9rem}
</style></head><body><div class="card">
<h2>🔒 Pinteresto Dashboard</h2>
<p>Enter your API key once — this browser will remember it.</p>
$error
<form method="post" action="/key">
 <input type="hidden" name="next" value="$next">
 <input type="password" name="key" placeholder="API key" autofocus required>
 <button type="submit">Unlock</button>
</form></div></body></html>""")


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # The unlock page itself must be reachable.
        if path == "/key":
            if request.method == "GET":
                nxt = request.query_params.get("next", "/")
                return HTMLResponse(KEY_PAGE.substitute(error="", next=nxt))
            if request.method == "POST":
                form = await request.form()
                if _key_matches(form.get("key")):
                    nxt = str(form.get("next") or "/")
                    if not nxt.startswith("/"):
                        nxt = "/"
                    resp = RedirectResponse(nxt, status_code=303)
                    resp.set_cookie(
                        COOKIE_NAME,
                        _expected_key(),
                        max_age=COOKIE_MAX_AGE,
                        httponly=True,
                        samesite="lax",
                        path="/",
                    )
                    return resp
                return HTMLResponse(
                    KEY_PAGE.substitute(
                        error='<p class="err">Wrong key — try again.</p>', next="/"
                    ),
                    status_code=401,
                )

        # Lock is OFF until the owner sets PINS_API_KEY — never lock him out.
        if not _expected_key():
            return await call_next(request)

        # CORS preflight carries no credentials; the real request is checked.
        if request.method == "OPTIONS" or path in PUBLIC_PATHS:
            return await call_next(request)

        if _key_matches(_request_key(request)):
            if _rate_limited(request):
                return JSONResponse(
                    {"detail": "Too many requests — slow down a bit."},
                    status_code=429,
                )
            return await call_next(request)

        # Browsers get sent to the unlock page; API clients get a clean 401
        # (the Pins app surfaces this as "enter your API key in Settings").
        accept = request.headers.get("accept", "")
        if request.method == "GET" and "text/html" in accept:
            return RedirectResponse(f"/key?next={path}", status_code=302)
        return JSONResponse(
            {"detail": "Invalid or missing API key."}, status_code=401
        )
