"""대시보드 '재부팅 필요' 알림 — /var/run/reboot-required 존재 여부를 반영하는지."""

import warnings

import pytest
import yaml

from livemax import paths as lm_paths

warnings.filterwarnings("ignore", category=DeprecationWarning)


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from livemax.web import auth, data
    from livemax.web.app import create_app

    cfg = lm_paths.CONFIG_DIR
    (cfg / "server.yaml").write_text(yaml.safe_dump({
        "paths": {"media_root": str(tmp_path / "media"), "log_dir": str(tmp_path / "log"),
                  "run_dir": str(tmp_path / "run"), "state_dir": str(tmp_path / "state"),
                  "db": str(tmp_path / "state" / "t.db")},
        "geo": {"enabled": False},
    }))
    flag, pkgs = tmp_path / "reboot-required", tmp_path / "reboot-required.pkgs"
    monkeypatch.setattr(data, "REBOOT_FLAG", flag)
    monkeypatch.setattr(data, "REBOOT_PKGS", pkgs)
    auth.set_password("Correct-Horse-Battery-9")
    c = TestClient(create_app(), base_url="https://testserver")
    r = c.post("/login", data={"username": "admin", "password": "Correct-Horse-Battery-9"},
               headers={"origin": "https://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    yield c, flag, pkgs
    (cfg / "server.yaml").unlink(missing_ok=True)


def test_no_banner_without_flag(client):
    c, _flag, _pkgs = client
    assert c.get("/api/status").json()["reboot"]["required"] is False
    html = c.get("/").text
    assert 'id="reboot-banner" role="status" hidden' in html


def test_banner_with_flag_and_packages(client):
    c, flag, pkgs = client
    flag.write_text("*** System restart required ***\n")
    pkgs.write_text("linux-image-6.8.0-99-generic\nlinux-base\nlinux-image-6.8.0-99-generic\n")
    j = c.get("/api/status").json()
    assert j["reboot"]["required"] is True
    assert j["reboot"]["packages"] == ["linux-base", "linux-image-6.8.0-99-generic"]
    assert any("재부팅 필요" in a["message"] for a in j["alerts"])
    html = c.get("/").text
    assert 'id="reboot-banner" role="status" hidden' not in html
    assert "linux-image-6.8.0-99-generic" in html
