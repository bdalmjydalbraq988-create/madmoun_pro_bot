"""Runtime helpers shared by local, Docker, and managed hosting entry points."""

from __future__ import annotations

import os
from collections.abc import Mapping


def resolve_http_port(
    environ: Mapping[str, str] | None = None,
    *,
    default: int = 8000,
) -> int:
    """Return the HTTP port assigned by the hosting platform.

    Bot-Hosting/Pterodactyl exposes the primary allocation as ``SERVER_PORT``.
    Other platforms commonly use ``PORT``. Invalid configured values fail fast
    instead of starting successfully on an unreachable port.
    """

    values = os.environ if environ is None else environ
    for key in ("SERVER_PORT", "PORT"):
        raw_value = values.get(key, "").strip()
        if not raw_value:
            continue
        try:
            port = int(raw_value)
        except ValueError as exc:
            raise RuntimeError(f"{key} must be a valid integer port") from exc
        if not 1 <= port <= 65_535:
            raise RuntimeError(f"{key} must be between 1 and 65535")
        return port
    return default
