"""Loads settings, YAML configs and .env. Home dir = $JOBHUNT_HOME or cwd."""
import os
from pathlib import Path
import yaml


def home() -> Path:
    return Path(os.environ.get("JOBHUNT_HOME", Path.cwd())).resolve()


def load_env(root: Path | None = None) -> None:
    f = (root or home()) / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_yaml(name: str, root: Path | None = None) -> dict:
    p = (root or home()) / "config" / name
    value = yaml.safe_load(p.read_text()) or {}
    if not isinstance(value, dict):
        raise ValueError(f'{name} must contain a YAML mapping')
    return value


class Ctx:
    """Everything a module needs, built once."""

    def __init__(self, root: Path | None = None):
        self.root = (root or home())
        load_env(self.root)
        self.settings = load_yaml("settings.yaml", self.root)
        self.profile = load_yaml("profile.yaml", self.root)
        self.answer_bank = load_yaml("answer_bank.yaml", self.root)
        self.dealbreakers = load_yaml("dealbreakers.yaml", self.root)
        self.titles = load_yaml("titles.yaml", self.root)
        self.data_dir = self.root / "data"
        self.out_dir = self.root / "out"
        self.data_dir.mkdir(exist_ok=True)
        self.out_dir.mkdir(exist_ok=True)
        from .db import DB
        self.db = DB(self.data_dir / "jobhunt.db")
