"""Verified owner entry point using JSON-list syntax for legacy settings."""

import os

# Legacy pydantic-settings decodes list fields as JSON before validation.
os.environ["ADMIN_IDS"] = "[8884716304]"

import uvicorn

from app.config import get_settings


if __name__ == "__main__":
    settings = get_settings()
    print(f"[OWNER V2] ADMIN_IDS={settings.admin_ids}", flush=True)
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
