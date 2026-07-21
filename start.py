"""Entry point for bot-hosting panels."""

import os

import uvicorn

# The hosting panel may override values from .env. Set the verified store owner
# before importing the application so every router receives the correct ID.
os.environ["ADMIN_IDS"] = "[8884716304]"

from app import __version__  # noqa: E402
from app.config import get_settings  # noqa: E402

if __name__ == "__main__":
    settings = get_settings()
    print(
        f"[MADMOUN RELEASE {__version__}] ADMIN_IDS={settings.admin_ids}",
        flush=True,
    )
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
