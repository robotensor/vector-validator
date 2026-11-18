import os

import pytest


@pytest.fixture(autouse=True)
def vv_home(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("VECTOR_VALIDATOR_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("VECTOR_VALIDATOR_HOME", str(tmp_path / "home"))
    return tmp_path / "home"
