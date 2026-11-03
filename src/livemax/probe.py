"""ffprobe 규격 검증 (DESIGN 1.2, 2.2, 4.2).

copy 방식은 규격 불일치를 교정할 수 없으므로 입구에서 막는다.
여기서는 컨테이너·스트림 헤더와 패킷 플래그만 읽는다. 디코딩은 하지 않는다.

라우드니스(-14 LUFS)는 검사하지 않는다: 측정하려면 전체 오디오를 디코딩해야 하므로
서버 원칙(인코딩·디코딩 작업 금지)에 맞지 않는다. 사용자 PC 쪽 인코딩 도구의 책임이다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .config import parse_bitrate

FFPROBE = shutil.which("ffprobe") or "ffprobe"

VIDEO_EXT = {".mp4": "mp4", ".mkv": "mkv"}
AUDIO_EXT = {".m4a": "m4a", ".aac": "aac"}


@dataclass
class ProbeResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)  # 사람이 읽는 실패 사유 (대시보드에 그대로 표시)
    warnings: list[str] = field(default_factory=list)
    duration: float | None = None
    bitrate_kbps: float | None = None
    info: dict = field(default_factory=dict)


def _run(args: list[str], timeout: int = 120) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:500] or f"ffprobe 실패 rc={proc.returncode}")
    return proc.stdout


def probe_json(path: Path) -> dict:
    out = _run([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
    return json.loads(out)


def keyframe_times(path: Path, limit_seconds: float | None = None) -> list[float]:
    """비디오 패킷의 키프레임 pts_time 목록. 패킷 헤더만 읽는다 (디코딩 없음)."""
    args = [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0"]
    if limit_seconds:
        args += ["-read_intervals", f"%+{limit_seconds}"]
    out = _run([*args, str(path)], timeout=300)
    times: list[float] = []
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2 or parts[0] in ("", "N/A"):
            continue
        if "K" in parts[1]:
            times.append(float(parts[0]))
    return sorted(times)


def duration_of(path: Path) -> float | None:
    try:
        out = _run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], timeout=60)
        return float(out.strip())
    except (RuntimeError, ValueError, subprocess.TimeoutExpired):
        return None


def _fps(s: str | None) -> float | None:
    if not s or s in ("0/0", "N/A"):
        return None
    try:
        return float(Fraction(s))
    except (ValueError, ZeroDivisionError):
        return None


def _container_ok(fmt_name: str, ext: str, allowed: list[str]) -> bool:
    names = set(fmt_name.split(","))
    if ext not in allowed:
        return False
    if ext in ("mp4", "m4a"):
        return bool(names & {"mov", "mp4", "m4a"})
    if ext == "mkv":
        return "matroska" in names
    if ext == "aac":
        return "aac" in names  # ADTS raw
    return False


def validate_video(path: Path, spec: dict) -> ProbeResult:
    r = ProbeResult(ok=False)
    ext = VIDEO_EXT.get(path.suffix.lower())
    if not ext:
        r.reasons.append(f"확장자 {path.suffix or '(없음)'} — .mp4 또는 .mkv 여야 함")
        return r
    try:
        data = probe_json(path)
    except Exception as e:  # noqa: BLE001
        r.reasons.append(f"ffprobe 로 읽을 수 없음: {e}")
        return r

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    vids = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    auds = [s for s in streams if s.get("codec_type") == "audio"]

    if not _container_ok(fmt.get("format_name", ""), ext, spec["containers"]):
        r.reasons.append(f"컨테이너 {fmt.get('format_name')} — MP4 또는 MKV 여야 함")
    if len(vids) != 1:
        r.reasons.append(f"영상 스트림 {len(vids)}개 — 정확히 1개여야 함")
    if auds:
        r.reasons.append(f"오디오 트랙 {len(auds)}개 있음 — 영상 클립에는 오디오가 없어야 함")
    if not vids:
        return r

    v = vids[0]
    if v.get("codec_name") != spec["codec"]:
        r.reasons.append(f"코덱 {v.get('codec_name')} — {spec['codec'].upper()} 여야 함")
    if str(v.get("profile", "")) != spec["profile"]:
        r.reasons.append(f"프로파일 {v.get('profile')} — {spec['profile']} 여야 함")
    w, h = v.get("width"), v.get("height")
    if (w, h) != (spec["width"], spec["height"]):
        r.reasons.append(f"해상도 {w}x{h} — {spec['width']}x{spec['height']} 여야 함")

    rfps, afps = _fps(v.get("r_frame_rate")), _fps(v.get("avg_frame_rate"))
    want = float(spec["fps"])
    if rfps is None or abs(rfps - want) > 0.01 or (afps is not None and abs(afps - want) > 0.05):
        r.reasons.append(f"프레임 {v.get('r_frame_rate')} (평균 {v.get('avg_frame_rate')}) — {spec['fps']}fps 고정이어야 함")

    if int(v.get("has_b_frames", 0) or 0) > 0:
        r.reasons.append("B프레임 사용 중 — B프레임 없이 인코딩해야 함 (-bf 0)")

    br = v.get("bit_rate") or fmt.get("bit_rate")
    min_kbps = parse_bitrate(spec["min_bitrate"])
    if br:
        kbps = int(br) / 1000
        r.bitrate_kbps = kbps
        if kbps < min_kbps * 0.98:
            r.reasons.append(f"비트레이트 {kbps:.0f}k — 최소 {min_kbps}k 이상이어야 함")
    else:
        r.warnings.append("비트레이트 정보를 읽을 수 없음")

    try:
        r.duration = float(fmt.get("duration") or v.get("duration"))
    except (TypeError, ValueError):
        r.reasons.append("길이를 읽을 수 없음")

    # 키프레임 간격 — 2초 고정 필수. 어긋나면 유튜브 화질 등급 하락 + concat 전환 지점 깨짐.
    try:
        kf = keyframe_times(path)
    except Exception as e:  # noqa: BLE001
        r.reasons.append(f"키프레임 정보를 읽을 수 없음: {e}")
        kf = []
    if kf:
        target, tol = float(spec["keyframe_interval"]), float(spec["keyframe_tolerance"])
        start_pts = float(v.get("start_time") or 0)
        if abs(kf[0] - start_pts) > 0.01:
            r.reasons.append(f"첫 프레임이 키프레임이 아님 (첫 키프레임 {kf[0]:.2f}초)")
        gaps = [b - a for a, b in zip(kf, kf[1:])]
        bad = [g for g in gaps if abs(g - target) > tol]
        if bad:
            worst = max(bad, key=lambda g: abs(g - target))
            r.reasons.append(f"키프레임 {worst:.1f}초, {target:g}초여야 함 (불규칙 간격 {len(bad)}곳)")
        if not gaps and (r.duration or 0) > target + tol:
            r.reasons.append(f"키프레임이 1개뿐 — {target:g}초 간격이어야 함")
    elif not r.reasons:
        r.reasons.append("키프레임을 찾을 수 없음")

    if r.duration and not (50 <= r.duration <= 70):
        r.warnings.append(f"길이 {r.duration:.1f}초 — 설계 기준은 1분 클립")

    r.info = {
        "codec": v.get("codec_name"), "profile": v.get("profile"), "width": w, "height": h,
        "fps": v.get("r_frame_rate"), "format": fmt.get("format_name"),
    }
    r.ok = not r.reasons
    return r


def validate_audio(path: Path, spec: dict) -> ProbeResult:
    r = ProbeResult(ok=False)
    ext = AUDIO_EXT.get(path.suffix.lower())
    if not ext:
        r.reasons.append(f"확장자 {path.suffix or '(없음)'} — .m4a 또는 .aac(ADTS) 여야 함")
        return r
    try:
        data = probe_json(path)
    except Exception as e:  # noqa: BLE001
        r.reasons.append(f"ffprobe 로 읽을 수 없음: {e}")
        return r

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    auds = [s for s in streams if s.get("codec_type") == "audio"]
    # m4a 의 앨범아트(attached_pic)는 허용 — Liquidsoap 은 오디오 트랙만 copy 한다.
    vids = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]

    if not _container_ok(fmt.get("format_name", ""), ext, spec["containers"]):
        r.reasons.append(f"컨테이너 {fmt.get('format_name')} — M4A 또는 ADTS 여야 함")
    if len(auds) != 1:
        r.reasons.append(f"오디오 스트림 {len(auds)}개 — 정확히 1개여야 함")
    if vids:
        r.reasons.append("영상 트랙이 들어 있음 — 음원 파일에는 오디오만 있어야 함")
    if not auds:
        return r

    a = auds[0]
    if a.get("codec_name") != spec["codec"]:
        r.reasons.append(f"코덱 {a.get('codec_name')} — AAC 여야 함")
    if str(a.get("profile", "")) != spec["profile"]:
        r.reasons.append(f"프로파일 {a.get('profile')} — AAC-{spec['profile']} 여야 함")
    sr = int(a.get("sample_rate", 0) or 0)
    if sr != int(spec["sample_rate"]):
        # 가장 흔하고 치명적인 불일치: 48kHz 가 섞이면 그 곡에서 소리가 깨진다.
        r.reasons.append(f"샘플레이트 {sr} Hz — {spec['sample_rate']} Hz 여야 함 (섞이면 소리가 깨짐)")
    ch = int(a.get("channels", 0) or 0)
    if ch != int(spec["channels"]):
        r.reasons.append(f"채널 {ch} — 스테레오(2) 여야 함")

    want = parse_bitrate(spec["bitrate"])
    tol = float(spec["bitrate_tolerance"])
    br = a.get("bit_rate") or fmt.get("bit_rate")
    if br:
        kbps = int(br) / 1000
        r.bitrate_kbps = kbps
        if abs(kbps - want) > want * tol:
            r.reasons.append(f"비트레이트 {kbps:.0f}k — {want}k 여야 함")
    else:
        r.warnings.append("비트레이트 정보를 읽을 수 없음")

    try:
        r.duration = float(fmt.get("duration") or a.get("duration"))
    except (TypeError, ValueError):
        r.warnings.append("길이를 읽을 수 없음")

    r.info = {"codec": a.get("codec_name"), "profile": a.get("profile"), "sample_rate": sr, "channels": ch, "format": fmt.get("format_name")}
    r.ok = not r.reasons
    return r


def validate(kind: str, path: Path, server: dict) -> ProbeResult:
    spec = server["spec"][kind]
    return validate_video(path, spec) if kind == "video" else validate_audio(path, spec)
