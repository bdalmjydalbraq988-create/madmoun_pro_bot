"""Verified owner entry point using JSON-list syntax for legacy settings."""

import os

import uvicorn

# Legacy pydantic-settings decodes list fields as JSON before validation.
os.environ["ADMIN_IDS"] = "[8884716304]"

from app.config import get_settings  # noqa: E402
from app.runtime import resolve_http_port  # noqa: E402

if __name__ == "__main__":
    settings = get_settings()
    http_port = resolve_http_port()
    print(
        f"[OWNER V2] ADMIN_IDS={settings.admin_ids} HTTP_PORT={http_port}",
        flush=True,
    )
    uvicorn.run("app.main:app", host="0.0.0.0", port=http_port)
