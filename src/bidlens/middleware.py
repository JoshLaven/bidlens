"""Middleware to work around reverse-proxy redirect limitations.

Some reverse proxies (including Duet's sandbox gateway) do not follow
3xx redirects returned by the upstream service. This middleware intercepts
redirect responses to non-safe methods (POST, PUT, PATCH, DELETE) and
rewrites them as 200 OK with a tiny HTML/JS page that performs the
redirect client-side.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse


class ClientRedirectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
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

            html = (
                f'<html><head><meta http-equiv="refresh" content="0;url={location}">'
                f'</head><body>Redirecting...</body></html>'
            )
            new_response = HTMLResponse(content=html, status_code=200)

            for cookie in cookies:
                val = cookie if isinstance(cookie, str) else cookie.decode()
                new_response.headers.append("set-cookie", val)

            return new_response

        return response
