import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated bridge home; also exported via BUDDY_BRIDGE_HOME for code
    that resolves the home itself (ipc client, hooks)."""
    h = tmp_path / "bridge-home"
    h.mkdir()
    monkeypatch.setenv("BUDDY_BRIDGE_HOME", str(h))
    return h
