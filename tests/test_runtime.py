from __future__ import annotations

import pytest

from app.runtime import resolve_http_port


def test_server_port_has_priority_for_bot_hosting() -> None:
    assert resolve_http_port({"SERVER_PORT": "26377", "PORT": "9000"}) == 26377


def test_port_is_supported_for_other_platforms() -> None:
    assert resolve_http_port({"PORT": "9000"}) == 9000


def test_local_default_remains_8000() -> None:
    assert resolve_http_port({}) == 8000


@pytest.mark.parametrize("value", ["abc", "0", "65536", "-1"])
def test_invalid_assigned_port_fails_fast(value: str) -> None:
    with pytest.raises(RuntimeError):
        resolve_http_port({"SERVER_PORT": value})
