import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("LIVEMAX_DATA", tempfile.mkdtemp(prefix="livemax-test-"))
os.environ["LIVEMAX_ASSETS"] = str(ROOT / "assets")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    from livemax import db
    from livemax.paths import Paths

    monkeypatch.setenv("LIVEMAX_DATA", str(tmp_path))
    p = Paths(tmp_path)
    p.ensure()
    db.init(p.db)
    return p


@pytest.fixture
def settings(paths):
    from livemax import config

    return config.load_settings(paths)


def make_live(**over):
    from livemax.config import Live

    d = dict(id="live1", name="Test", video_folders=["morning"], audio_folders=["chill"], harbor_port=8101, restart_slot=0)
    d.update(over)
    return Live(**d)
