"""Resolve the optional sibling checkout only when integration tests execute."""
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def code_eval_root(tmp_path, monkeypatch):
    default = Path(__file__).resolve().parents[3] / "code-eval"
    root = Path(os.environ.get("CODE_EVAL_ROOT", str(default))).expanduser().resolve()
    backend = root / "backend"
    if not (backend / "app" / "__init__.py").is_file():
        pytest.skip("Compatible code-eval backend missing; set CODE_EVAL_ROOT")
    monkeypatch.syspath_prepend(str(backend))
    for name in ("DATA_DIR", "HOST_DATA_DIR", "STATE_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    return root
