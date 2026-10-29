import pytest


@pytest.fixture(autouse=True)
def _no_real_usage_tracker(tmp_path_factory, monkeypatch):
    """The header reads the OS tracker's cache; tests must not depend on the machine they run on."""
    monkeypatch.setenv("RABBLE_USAGE_FILE", str(tmp_path_factory.mktemp("usage") / "none.json"))
