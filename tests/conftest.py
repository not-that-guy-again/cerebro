import pytest


@pytest.fixture(autouse=True)
def isolate_cerebro_home(tmp_path, monkeypatch):
    """Redirect CEREBRO_HOME to a per-test tmp dir so no test touches the real home."""
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path))
