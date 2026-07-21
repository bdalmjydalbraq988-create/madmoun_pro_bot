"""Verified bot-hosting entry point for the store owner."""

import os

import uvicorn

os.environ["ADMIN_IDS"] = "[8884716304]"

from app.config import get_settings  # noqa: E402
from app.runtime import resolve_http_port  # noqa: E402

if __name__ == "__main__":
    settings = get_settings()
    http_port = resolve_http_port()
    print(
        f"[OWNER START] ADMIN_IDS={settings.admin_ids} HTTP_PORT={http_port}",
        flush=True,
    )
    uvicorn.run("app.main:app", host="0.0.0.0", port=http_port)
