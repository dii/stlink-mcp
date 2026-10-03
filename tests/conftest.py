"""Every test runs as if ST's stlink-server were not running, whatever this
machine is doing: while it runs, every programmer connect adds `shared`, and a
test that checks a command line would otherwise depend on the bench."""

import pytest

from stlink_mcp import probe


@pytest.fixture(autouse=True)
def no_sharing_daemon(monkeypatch):
    monkeypatch.setattr(probe, "sharing_daemon_running", lambda *a, **k: False)
