import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# livemax.paths 는 import 시점에 환경변수를 읽으므로 테스트 모듈보다 먼저 설정한다
_TMP = Path(tempfile.mkdtemp(prefix="livemax-test-"))
os.environ["LIVEMAX_HOME"] = str(ROOT)
os.environ["LIVEMAX_CONFIG_DIR"] = str(_TMP / "config")
os.environ["LIVEMAX_SECRETS_DIR"] = str(_TMP / "secrets")
(_TMP / "config").mkdir()
(_TMP / "secrets").mkdir()


@pytest.fixture
def server(tmp_path):
    from livemax import config

    s = config._deep_merge(config.DEFAULT_SERVER, {
        "paths": {
            "media_root": str(tmp_path / "media"), "log_dir": str(tmp_path / "log"),
            "run_dir": str(tmp_path / "run"), "state_dir": str(tmp_path / "state"),
            "db": str(tmp_path / "state" / "livemax.db"),
        }
    })
    return s


@pytest.fixture
def paths(server):
    from livemax import config, db

    p = config.get_paths(server)
    for d in (p.video, p.audio, p.incoming, p.rejected, p.run_dir, p.state_dir, p.log_dir):
        d.mkdir(parents=True, exist_ok=True)
    db.init(p.db)
    return p


def live_dict(**over):
    d = {
        "id": "ch01", "name": "Test", "enabled": True,
        "video": {"bitrate": "5500k"},
        "audio": {"mode": "random_norepeat", "norepeat_count": 30, "crossfade": 0, "harbor_port": 8101},
        "timezone": "America/Chicago",
        "schedule": [
            {"from": "06:00", "video": ["morning"], "audio": ["upbeat"]},
            {"from": "18:00", "video": ["evening"], "audio": ["chill"]},
        ],
        "restart_at": "04:17",
        "stream_key_file": "secrets/ch01.key",
    }
    d.update(over)
    return d
