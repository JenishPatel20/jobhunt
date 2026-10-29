import shutil
from pathlib import Path

import pytest

from jobhunt.config import Ctx

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def offline_requests(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must mock HTTP requests; live network access is disabled")

    monkeypatch.setattr("requests.sessions.Session.request", blocked)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.fixture
def ctx(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    for name in ("settings.yaml", "titles.yaml", "dealbreakers.yaml"):
        shutil.copyfile(ROOT / "config" / name, config / name)
    for sample in (ROOT / "config" / "examples").iterdir():
        if sample.is_file():
            shutil.copyfile(sample, config / sample.name)
    return Ctx(tmp_path)
