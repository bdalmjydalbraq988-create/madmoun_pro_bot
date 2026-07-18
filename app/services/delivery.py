from __future__ import annotations

import html
import re
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_PLACEHOLDERS = {
    "تم تنفيذ وتفعيل الخدمة بنجاح لدى المورد.",
    "تم تنفيذ الخدمة بنجاح لدى المورد.",
}


def is_placeholder_delivery(value: str | None) -> bool:
    return not value or value.strip() in _PLACEHOLDERS


def first_http_url(value: str) -> str | None:
    match = _URL_RE.search(value)
    if not match:
        return None
    url = match.group(0).rstrip(".,،؛;!?)]}»\"'")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def delivery_html(value: str) -> str:
    clean = value.strip()
    escaped = html.escape(clean)
    url = first_http_url(clean)
    if url:
        safe_url = html.escape(url, quote=True)
        return f'<a href="{safe_url}">🔗 فتح رابط التسليم</a>\n<pre>{escaped}</pre>'
    return f"<pre>{escaped}</pre>"
