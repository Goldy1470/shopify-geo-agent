"""Security helpers for the shopify-geo-agent dashboard.

- CSRF: double-submit cookie + Origin/Referer check on every state-changing request.
- SSRF guard: URLs we fetch server-side (accessibility checker) must be public http(s).
- Secret masking helpers for templates.
"""

import ipaddress
import re
import secrets
from urllib.parse import urlparse

CSRF_COOKIE = "geo_csrf"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"

_ALLOWED_PORTS = {80, 443, 3000, 8000, 8080, 8443}


def new_csrf_token() -> str:
    return secrets.token_hex(24)


def csrf_ok(cookie_token: str | None, submitted: str | None) -> bool:
    if not cookie_token or not submitted:
        return False
    return secrets.compare_digest(cookie_token, submitted)


def is_public_http_url(raw: str) -> bool:
    """True if the URL is safe to fetch server-side (SSRF guard, Phase 0 rules).

    Blocks: non-http(s) schemes, missing host, userinfo tricks ("@" in netloc),
    localhost/intranet names (.local/.internal), hosts without a dot, literal
    private/loopback/link-local IPs, odd ports. DNS-pin-based hardening: Phase 7.
    """
    if not raw or len(raw) > 2048:
        return False
    try:
        u = urlparse(raw)
    except ValueError:
        return False
    if u.scheme not in ("http", "https"):
        return False
    netloc = u.netloc or ""
    if "@" in netloc:
        return False
    host = (u.hostname or "").lower()
    if not host:
        return False
    if u.port is not None and u.port not in _ALLOWED_PORTS:
        return False
    if host in {"localhost", "0.0.0.0", "::1"} or host.endswith(".localhost"):
        return False
    if host.endswith((".local", ".internal", ".intra")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return ip.is_global and not ip.is_multicast
    if "." not in host:
        return False
    return not re.search(r"[\s\x00-\x1f]", raw)


def normalize_public_http_url(raw: str) -> str | None:
    """Scheme-less form input ('shop.com') becomes https; None if unsafe."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    return raw if is_public_http_url(raw) else None


def origin_matches_host(request_host: str, origin: str | None, referer: str | None) -> bool:
    """True when every present value shares the request Host (no info sniffing)."""
    if not origin and not referer:
        return True
    host = (request_host or "").lower()
    for value in (origin, referer):
        if not value:
            continue
        try:
            netloc = urlparse(value).netloc.lower()
        except ValueError:
            return False
        if not netloc:
            return False
        bare_url_host = host.split(":", 1)[0]
        bare_origin_host = netloc.split(":", 1)[0]
        if netloc != host and bare_origin_host != bare_url_host:
            return False
    return True


def mask_secret(secret: str | None, keep: int = 4) -> str:
    if not secret:
        return ""
    tail = secret[-keep:] if len(secret) > keep + 4 else ""
    return f"••••{tail}" if tail else "••••"
