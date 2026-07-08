"""Middleware to work around reverse-proxy redirect limitations.

Some reverse proxies (including Duet's sandbox gateway) do not follow
3xx redirects returned by the upstream service. This middleware intercepts
redirect responses to non-safe methods (POST, PUT, PATCH, DELETE) and
rewrites them as 200 OK with a tiny HTML/JS page that performs the
redirect client-side.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse

from .auth import is_platform_admin_email, serializer
from .config import SESSION_COOKIE_NAME
from .database import SessionLocal
from .models import User


PLATFORM_ALLOWED_PATH_PREFIXES = (
    "/platform",
    "/invite",
    "/login",
    "/logout",
    "/health",
    "/static",
    "/favicon.ico",
)


def _client_redirect(location: str) -> HTMLResponse:
    html = (
        f'<html><head><meta http-equiv="refresh" content="0;url={location}">'
        f'</head><body>Redirecting...</body></html>'
    )
    return HTMLResponse(content=html, status_code=200)


class ClientRedirectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if _platform_owner_should_return_to_platform(request):
            return _client_redirect("/platform")

        response = await call_next(request)

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "/")

            # Carry over any Set-Cookie headers (important for login)
            cookies = response.headers.getlist("set-cookie") if hasattr(response.headers, "getlist") else []
            if not cookies:
                cookies = [
                    v for k, v in response.raw_headers
                    if k.lower() == b"set-cookie"
                ]

            new_response = _client_redirect(location)

            for cookie in cookies:
                val = cookie if isinstance(cookie, str) else cookie.decode()
                new_response.headers.append("set-cookie", val)

            return new_response

        return response


def _platform_owner_should_return_to_platform(request) -> bool:
    path = request.url.path
    if any(path == prefix or path.startswith(f"{prefix}/") for prefix in PLATFORM_ALLOWED_PATH_PREFIXES):
        return False

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return False

    try:
        data = serializer.loads(token)
        user_id = data.get("user_id")
    except Exception:
        return False

    if not user_id:
        return False

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        return bool(user and is_platform_admin_email(user.email))
    finally:
        db.close()
