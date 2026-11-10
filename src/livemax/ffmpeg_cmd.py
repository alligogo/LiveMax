"""ffmpeg 송출 명령 생성 + 재인코딩 차단 가드 (DESIGN 1.1, 11-1, 11-2).

이 프로젝트의 성패를 가르는 단 하나의 규칙: ffmpeg 는 먹싱만 한다.
    -c:v copy -c:a copy
drawtext, overlay, scale 등 영상 필터를 하나라도 넣는 순간 재인코딩이 되어 CPU 가 10배로 뛴다.

assert_copy_only() 는 실행 직전에 명령줄 전체를 검사해 필터·인코더·스케일 옵션이 하나라도
있으면 예외를 던진다. 누군가 실수로 옵션을 추가해도 송출이 시작되지 않는다.

참고: ADTS AAC → FLV 에 필요한 aac_adtstoasc 는 "비트스트림 필터"로, 헤더 형식만 바꾸며
디코딩·인코딩을 하지 않는다. FLV 먹서가 자동으로 넣으므로 명령줄에 쓰지 않는다.
"""

from __future__ import annotations

import shutil
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


AUDIO_RECONNECT = ("-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_at_eof", "1",
                   "-reconnect_on_network_error", "1", "-reconnect_on_http_error", "4xx,5xx", "-reconnect_delay_max", "15")


class ReencodeForbidden(RuntimeError):
    pass


# 디코딩·필터·재인코딩을 유발하는 옵션들 (스트림 지정자 포함: -vf, -filter:v, -b:v ...)
_FORBIDDEN_OPTS = {
    "-vf", "-af", "-filter", "-filter_complex", "-lavfi", "-filter_script", "-filter_complex_script",
    "-s", "-r", "-aspect", "-pix_fmt", "-vsync", "-fps_mode", "-ar", "-ac", "-sample_fmt",
    "-b", "-maxrate", "-minrate", "-bufsize", "-crf", "-qp", "-q", "-qscale", "-preset", "-tune",
    "-profile", "-level", "-g", "-keyint_min", "-x264opts", "-x264-params", "-force_key_frames",
    "-vol", "-sws_flags",
}
_FORBIDDEN_WORDS = ("drawtext", "overlay", "scale", "subtitles", "ass=", "pad=", "crop=", "fade", "volume", "loudnorm")
_CODEC_OPTS = {"-c", "-codec", "-vcodec", "-acodec", "-scodec"}


def _base_opt(arg: str) -> str:
    # '-filter:v' → '-filter', '-c:v:0' → '-c', '-b:v' → '-b'
    return arg.split(":", 1)[0]


def assert_copy_only(args: list[str]) -> None:
    """명령줄에 재인코딩 요소가 있으면 ReencodeForbidden."""
    problems: list[str] = []
    codecs_seen = 0
    for i, a in enumerate(args):
        if not a.startswith("-") or a in ("-",):
            continue
        base = _base_opt(a)
        if base in _FORBIDDEN_OPTS:
            problems.append(f"금지 옵션 {a}")
        if base in _CODEC_OPTS:
            codecs_seen += 1
            val = args[i + 1] if i + 1 < len(args) else ""
            if val != "copy":
                problems.append(f"{a} {val} — copy 만 허용")
    for w in _FORBIDDEN_WORDS:
        # 입력 파일 경로에 우연히 단어가 들어갈 수 있으므로 "옵션 값" 위치만 본다 (-i 값 제외)
        for i, a in enumerate(args[:-1]):
            if a.startswith("-") and _base_opt(a) not in ("-i", "-progress") and w in args[i + 1].lower():
                problems.append(f"필터 키워드 '{w}' ({a} {args[i + 1]})")
    if codecs_seen == 0 or ("-c:v" not in args and "-c" not in args):
        problems.append("-c:v copy 가 명시되지 않음")
    if "-c:a" not in args and "-c" not in args:
        problems.append("-c:a copy 가 명시되지 않음")
    if problems:
        raise ReencodeForbidden("재인코딩 금지 위반: " + "; ".join(problems))


def build(concat_list: Path, audio_url: str, output_url: str, rw_timeout_s: int = 15,
          extra_out: list[str] | None = None) -> list[str]:
    """송출 명령. 출력 URL 에는 스트림 키가 들어가므로 이 리스트를 절대 그대로 로그에 찍지 말 것."""
    args = [
        FFMPEG, "-hide_banner", "-nostdin",
        "-loglevel", "info",
        "-nostats", "-progress", "pipe:1", "-stats_period", "2",
        # 영상: 24시간치 concat 목록을 실시간 속도로 읽음
        "-re", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        # 오디오: Liquidsoap harbor (로컬).
        # 음악 프로그램이 죽었다 살아나도 ffmpeg 가 스스로 다시 붙는다 (v2.13). 이 옵션이 없으면 음악 연결이 끊긴 순간
        # ffmpeg 는 죽지 않고 '소리 없는 영상'만 계속 보낸다 (6.1 실측 — 감시에는 정상으로 보임).
        # 다시 붙는 동안 영상도 같이 멈췄다가 이어져 시각이 어긋나지 않는다. 간격 0·1·3·7·15초로 약 26초까지 시도.
        *AUDIO_RECONNECT,
        # 로컬 Liquidsoap 의 ADTS AAC — 기본 탐색(최대 5초) 대신 0.5초만 보고 바로 시작 (재시작 공백 단축)
        "-thread_queue_size", "1024", "-rw_timeout", str(rw_timeout_s * 1_000_000),
        "-analyzeduration", "500000", "-probesize", "65536", "-i", audio_url,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "copy",
        "-max_muxing_queue_size", "1024",
        *(extra_out or []),  # 출력 프로토콜 옵션 (예: TLS 중계 시 -rtmp_tcurl)
        "-f", "flv", "-flvflags", "no_duration_filesize",
        output_url,
    ]
    assert_copy_only(args)
    return args


def redacted(args: list[str], secret: str) -> str:
    """로그용 명령줄 문자열 (스트림 키 마스킹)."""
    from .secrets import mask

    return " ".join(a.replace(secret, mask(secret)) if secret else a for a in args)
