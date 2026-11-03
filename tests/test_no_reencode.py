"""DESIGN 11-1, 11-2: 재인코딩 금지 — 이 프로젝트의 성패를 가르는 규칙의 회귀 테스트."""

import ast
import shutil
import subprocess
from pathlib import Path

import pytest

from livemax import ffmpeg_cmd, liquidsoap
from livemax.ffmpeg_cmd import ReencodeForbidden, assert_copy_only

from conftest import ROOT, make_live

SRC = ROOT / "src" / "livemax"


def test_build_is_copy_only():
    args = ffmpeg_cmd.build(Path("/x/list.ffconcat"), "http://127.0.0.1:8101/ch01.aac", "rtmps://h/live2/KEY")
    assert args[args.index("-c:v") + 1] == "copy"
    assert args[args.index("-c:a") + 1] == "copy"
    for bad in ("-vf", "-af", "-filter_complex", "-lavfi", "-s", "-r", "-b:v", "-preset"):
        assert bad not in args


@pytest.mark.parametrize("extra", [
    ["-vf", "drawtext=text=hello"],
    ["-filter_complex", "[0:v][1:v]overlay"],
    ["-filter:v", "scale=1280:720"],
    ["-s", "1280x720"],
    ["-r", "25"],
    ["-b:v", "4500k"],
    ["-af", "volume=0.5"],
    ["-preset", "veryfast"],
    ["-g", "60"],
])
def test_guard_rejects_filters_and_encoder_options(extra):
    base = ffmpeg_cmd.build(Path("/l"), "http://127.0.0.1:1/a.aac", "rtmps://h/k")
    with pytest.raises(ReencodeForbidden):
        assert_copy_only(base[:-1] + extra + base[-1:])


@pytest.mark.parametrize("codec_opt,codec", [("-c:v", "libx264"), ("-c:a", "aac"), ("-vcodec", "h264"), ("-c", "libx265")])
def test_guard_rejects_non_copy_codecs(codec_opt, codec):
    base = ffmpeg_cmd.build(Path("/l"), "http://127.0.0.1:1/a.aac", "rtmps://h/k")
    with pytest.raises(ReencodeForbidden):
        assert_copy_only(base[:-1] + [codec_opt, codec] + base[-1:])


FORBIDDEN_TOKENS = {"-vf", "-af", "-filter_complex", "-lavfi", "-filter:v", "-filter:a", "-filter_script"}
FORBIDDEN_IN_VALUE = ("drawtext", "overlay=", "scale=", "subtitles=")


def _string_constants(tree):
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                docstrings.add(id(node.body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node


def test_source_never_passes_filters_to_ffmpeg():
    """소스 전체에서 ffmpeg 필터 옵션을 문자열로 쓰는 곳이 없어야 한다 (가드 자신은 제외)."""
    offenders = []
    for py in SRC.rglob("*.py"):
        if py.name == "ffmpeg_cmd.py":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in _string_constants(tree):
            v = node.value
            if v in FORBIDDEN_TOKENS or any(w in v for w in FORBIDDEN_IN_VALUE):
                offenders.append(f"{py.relative_to(ROOT)}:{node.lineno}: {v!r}")
    assert not offenders, "재인코딩 유발 문자열 발견:\n" + "\n".join(offenders)


def test_liquidsoap_output_is_copy(paths, settings):
    liq = liquidsoap.render(make_live(), paths, settings)
    assert '%ffmpeg(format="adts", %audio.copy)' in liq
    for bad in ("crossfade(", "%fdkaac", "%mp3", "%audio(", "amplify(", "normalize("):
        assert bad not in liq, bad
    assert 'settings.harbor.bind_addrs.set(["127.0.0.1"])' in liq


def test_crossfade_reencodes_audio_only(paths, settings):
    liq = liquidsoap.render(make_live(crossfade=3), paths, settings)
    assert "crossfade(" in liq and '%audio(codec="aac", b="128k", samplerate=44100, channels=2)' in liq
    args = ffmpeg_cmd.build(Path("/l"), "http://127.0.0.1:8101/live1.aac", "rtmps://h/k")
    assert args[args.index("-c:v") + 1] == "copy" and args[args.index("-c:a") + 1] == "copy"


@pytest.mark.skipif(not shutil.which("liquidsoap"), reason="liquidsoap 미설치")
@pytest.mark.parametrize("crossfade,folders", [(0, ["chill"]), (3, ["chill", "jazz"])])
def test_rendered_liq_typechecks(paths, settings, tmp_path, crossfade, folders):
    """생성된 스크립트가 설치된 Liquidsoap(2.2.x)에서 실제로 타입체크를 통과하는지."""
    liq = tmp_path / "t.liq"
    liq.write_text(liquidsoap.render(make_live(crossfade=crossfade, audio_folders=folders), paths, settings))
    r = subprocess.run(["liquidsoap", "--check", str(liq)], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
