import shutil
from pathlib import Path

import pytest

from livemax import ffmpeg_cmd, tls_relay


def test_plan_uses_relay_with_matching_tcurl():
    p = tls_relay.plan("rtmps://a.rtmps.youtube.com/live2", "KEY", 40001)
    assert p.mode == "rtmps-relay"
    assert p.url == "rtmp://127.0.0.1:40001/live2/KEY"
    # 유튜브가 받는 tcUrl 은 직접 RTMPS 일 때와 같아야 한다
    assert p.extra_args == ["-rtmp_tcurl", "rtmps://a.rtmps.youtube.com:443/live2"]


def test_plan_falls_back_to_direct():
    p = tls_relay.plan("rtmps://a.rtmps.youtube.com/live2/", "KEY", None)
    assert (p.mode, p.url, p.extra_args) == ("rtmps-direct", "rtmps://a.rtmps.youtube.com/live2/KEY", [])


def test_plan_plain_rtmp_never_relayed():
    p = tls_relay.plan("rtmp://127.0.0.1:1935/live", "KEY", 40001)
    assert p.mode == "direct" and p.url == "rtmp://127.0.0.1:1935/live/KEY"


def test_conf_verifies_certificate_and_sni():
    c = tls_relay.render_conf(40001, "a.rtmps.youtube.com", 443, Path("/var/log/livemax/tls-ch01.log"))
    assert "accept = 127.0.0.1:40001" in c          # 외부에 열지 않는다
    assert "connect = a.rtmps.youtube.com:443" in c
    assert "sni = a.rtmps.youtube.com" in c
    assert "verifyChain = yes" in c and "checkHost = a.rtmps.youtube.com" in c


def test_relay_output_still_copy_only():
    p = tls_relay.plan("rtmps://a.rtmps.youtube.com/live2", "KEY", 40001)
    args = ffmpeg_cmd.build(Path("/l"), "http://127.0.0.1:8101/ch01.aac", p.url, extra_out=p.extra_args)
    i = args.index("-rtmp_tcurl")
    assert args[i + 1] == "rtmps://a.rtmps.youtube.com:443/live2" and args[-1] == p.url
    assert args[args.index("-c:v") + 1] == "copy" and args[args.index("-c:a") + 1] == "copy"


@pytest.mark.skipif(not (shutil.which("stunnel4") or shutil.which("stunnel")), reason="stunnel 미설치")
def test_relay_process_starts_and_stops(tmp_path):
    r = tls_relay.Relay("t1", tmp_path, tmp_path)
    port = r.start("rtmps://127.0.0.1:9/live", verify=False, wait_s=5)
    assert port and tls_relay.is_listening(port) and r.alive()
    r.stop()
    assert not r.alive() and not (tmp_path / "t1.stunnel.conf").exists()
