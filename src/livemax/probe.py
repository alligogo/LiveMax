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
        r.reasons.append(f"비디오 스트림 {len(vids)}개 — 정확히 1개여야 함")
    if auds:
        r.reasons.append(f"오디오 트랙 {len(auds)}개 있음 — 비디오 클립에는 오디오가 없어야 함")
    if not vids:
        return r

    v = vids[0]
    if v.get("codec_name") != spec["codec"]:
        r.reasons.append(f"코덱 {v.get('codec_name')} — {spec['codec'].upper()} 여야 함")
    if str(v.get("profile", "")) != spec["profile"]:
        r.reasons.append(f"프로파일 {v.get('profile')} — {spec['profile']} 여야 함")
    w, h = v.get("width"), v.get("height")
    allowed = [tuple(x) for x in spec["resolutions"]]
    if (w, h) not in allowed:
        names = ", ".join(f"{a}x{b}" for a, b in allowed)
        r.reasons.append(f"해상도 {w}x{h} — {names} 중 하나여야 함")

    # 송출은 올린 파일 그대로(copy) — 해상도·fps·비트레이트는 파일을 따른다. 고정 fps 만 확인.
    rfps, afps = _fps(v.get("r_frame_rate")), _fps(v.get("avg_frame_rate"))
    fps_ok = rfps is not None and any(abs(rfps - f) <= 0.01 for f in spec["fps"])
    if not fps_ok or (afps is not None and rfps is not None and abs(afps - rfps) > 0.05):
        r.reasons.append(f"프레임 {v.get('r_frame_rate')} (평균 {v.get('avg_frame_rate')}) — "
                         f"{'/'.join(str(f) for f in spec['fps'])}fps 중 하나로 고정이어야 함")

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

    # 키프레임 간격(2초) 전수 검사는 파일 전체를 훑어 CPU·디스크를 많이 써서 뺐다.
    # 변환기(LiveMax-Convert)로 만든 파일만 올린다는 전제 — 책임은 올리는 사람 몫. 머리 정보 검사는 그대로.

    # 이음새 검사: 클립을 이어붙일 때(같은 클립 반복 포함) 빈틈·겹침이 생기지 않으려면
    # 시작 시각이 0 이고, 길이가 프레임 수 × 프레임 간격과 맞아야 한다 (어긋나면 전환 때 멈칫하거나 튐)
    st = float(v.get("start_time") or 0)
    if abs(st) > 0.05:
        r.reasons.append(f"비디오 시작 시각 {st:.2f}초 — 0 이어야 함 (클립 전환 때 멈칫함, 변환기로 다시 만드세요)")
    try:
        frames, sdur = int(v.get("nb_frames") or 0), float(v.get("duration") or 0)
    except (TypeError, ValueError):
        frames, sdur = 0, 0.0
    if frames and sdur and rfps:
        drift = abs(sdur - frames / rfps)
        if drift > 1.5 / rfps:
            r.reasons.append(f"길이 {sdur:.3f}초가 프레임 {frames}개({frames / rfps:.3f}초)와 다름 — 클립 전환 때 튈 수 있음")

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
        r.reasons.append("비디오 트랙이 들어 있음 — 음악 파일에는 오디오만 있어야 함")
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

    lo, hi = parse_bitrate(spec["bitrate_min"]), parse_bitrate(spec["bitrate_max"])
    tol = float(spec["bitrate_tolerance"])
    br = a.get("bit_rate") or fmt.get("bit_rate")
    if br:
        kbps = int(br) / 1000
        r.bitrate_kbps = kbps
        if not (lo * (1 - tol) <= kbps <= hi * (1 + tol)):
            r.reasons.append(f"비트레이트 {kbps:.0f}k — {lo}k ~ {hi}k 여야 함")
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
