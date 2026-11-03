"""라이브 YAML → Liquidsoap 스크립트(.liq) 렌더링 (DESIGN 4.3).

대상 버전: Liquidsoap 2.2.x (Ubuntu 24.04 apt = 2.2.4). 함수 시그니처는 `liquidsoap -h <함수>` 로
설치된 버전에서 직접 확인했다: playlist(check_next, mode, loop, reload_mode), output.harbor,
switch, fallback, random, rotate, single, source.on_track, file.write, json.stringify, time.local.

오디오 출력 방식은 audio.crossfade 값으로 갈린다:
  crossfade == 0 (기본): %ffmpeg(format="adts", %audio.copy)
      업로드된 AAC 프레임을 디코딩 없이 그대로 넘긴다. CPU 최소.
      대신 모든 음원이 정확히 같은 스펙(AAC-LC 128k 44.1kHz 스테레오)이어야 한다 (DESIGN 2.2).
  crossfade > 0: crossfade() → %ffmpeg(format="adts", %audio(codec="aac", ...))
      crossfade() 는 PCM 에서만 동작하므로(2.2.4 타입: source(audio=pcm)) 이 라이브는
      오디오를 디코딩해 섞은 뒤 AAC 로 재인코딩한다. 영상은 어느 쪽이든 copy 그대로다.

시간대 편성:
  time.local() 은 프로세스의 TZ 환경변수를 따른다. audio.py 가 TZ=<지역명>(예: America/Chicago)으로
  실행하므로 서머타임은 OS tzdata 가 처리한다. 스크립트에 UTC 오프셋 숫자는 절대 들어가지 않는다.

무중단 교체:
  playlist(reload_mode="watch") 가 폴더를 감시해 파일 추가·삭제를 재시작 없이 반영한다.
"""

from __future__ import annotations

from pathlib import Path

from .config import LiveConfig
from .paths import ASSETS_DIR, Paths

SILENCE_FILE = ASSETS_DIR / "silence_10s.m4a"

_MODE = {
    # mode → (playlist mode, loop, check_next 사용)
    "random": ("random", True, False),
    "random_norepeat": ("randomize", True, True),
    "sequential": ("normal", False, False),
    "sequential_loop": ("normal", True, False),
}


def q(s: str | Path) -> str:
    """Liquidsoap 문자열 리터럴.

    Liquidsoap 은 "...#{expr}..." 를 보간하며 '\\#' 로 막을 수 없다 (2.2.4 에서 확인).
    그래서 '#{' 가 들어간 값은 아예 거부한다. (폴더 이름 규칙상 '#' 은 애초에 허용되지 않음)
    """
    s = str(s)
    if "#{" in s:
        raise ValueError(f"Liquidsoap 문자열에 '#{{' 사용 불가: {s!r}")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(live: LiveConfig, paths: Paths, server: dict) -> str:
    pl_mode, loop, use_norepeat = _MODE[live.audio_mode]
    log_level = 4 if str(server.get("logging", {}).get("level", "DEBUG")).upper() == "DEBUG" else 3
    out: list[str] = []
    w = out.append

    w(f"# 자동 생성 파일 — 직접 수정하지 말 것. 원본: config/{live.id}.yaml (livemax.liquidsoap)")
    w("# Liquidsoap 2.2.x 기준. 오디오는 copy 로만 통과시킨다 (재인코딩 없음).")
    w("")
    w("settings.log.stdout.set(true)")
    w("settings.log.file.set(true)")
    w(f"settings.log.file.path.set({q(paths.log_dir / f'audio-{live.id}.log')})")
    w(f"settings.log.level.set({log_level})")
    w("# harbor 는 기본이 0.0.0.0 — 반드시 로컬에만 바인드 (외부 노출 금지)")
    w('settings.harbor.bind_addrs.set(["127.0.0.1"])')
    w("settings.server.telnet.set(false)")
    w("")
    w(f"live_id = {q(live.id)}")
    w(f"nowplaying_path = {q(paths.nowplaying_file(live.id))}")
    w("")

    # --- 중복 방지 ---
    w("# 최근 N곡 중복 방지 (random_norepeat). 폴더 곡 수가 N 이하일 때 무한 거부에 빠지지 않도록 안전밸브를 둔다.")
    w(f"norepeat_n = {live.norepeat_count if use_norepeat else 0}")
    w("history = ref([])")
    w("rejects = ref(0)")
    w("def not_recent(r) =")
    w("  uri = request.uri(r)")
    w("  if list.mem(uri, history()) and rejects() < 50 then")
    w("    rejects := rejects() + 1")
    w('    log.debug("norepeat: 최근 재생곡 건너뜀 #{uri}")')
    w("    false")
    w("  else")
    w("    rejects := 0")
    w("    true")
    w("  end")
    w("end")
    w("")
    w("def on_new_track(m) =")
    w('  uri = m["filename"]')
    w("  if norepeat_n > 0 then")
    w("    h = list.add(uri, history())")
    w("    history := if list.length(h) > norepeat_n then list.prefix(norepeat_n, h) else h end")
    w("  end")
    w('  log.important("now playing: #{uri}")')
    w('  np = json.stringify({live=live_id, file=uri, title=m["title"], artist=m["artist"], started=time()})')
    w("  file.write(data=np, nowplaying_path)")
    w("end")
    w("")

    # --- 폴더별 playlist (같은 폴더는 구간이 달라도 하나의 소스를 공유) ---
    folder_src: dict[str, str] = {}
    w("# 폴더 감시 playlist — 파일 추가·제거가 재시작 없이 반영된다")
    for i, folder in enumerate(sorted(live.audio_folders())):
        name = f"pl_{i}"
        folder_src[folder] = name
        args = [
            f'id={q(f"{live.id}_pl_{i}")}',
            f"mode={q(pl_mode)}",
            f"loop={'true' if loop else 'false'}",
            'reload_mode="watch"',
        ]
        if use_norepeat:
            args.append("check_next=not_recent")
        w(f"# {folder}")
        w(f"{name} = playlist({', '.join(args)}, {q(paths.audio / folder)})")
    w("")

    # --- 구간별 소스 ---
    combine = "rotate" if pl_mode == "normal" else "random"
    seg_names: list[str] = []
    for i, seg in enumerate(live.schedule):
        srcs = [folder_src[f] for f in seg.audio]
        name = f"seg_{i}"
        seg_names.append(name)
        if len(srcs) == 1:
            w(f"{name} = {srcs[0]}  # {seg.start} {seg.audio}")
        else:
            w(f"{name} = {combine}([{', '.join(srcs)}])  # {seg.start} {seg.audio}")
    w("")

    # --- 시간대 편성 ---
    w(f"# 시간대 편성 — TZ={live.timezone} 기준 현지 시각. 곡이 끝날 때 다음 구간으로 넘어간다.")
    w("def in_window(f, e) =")
    w("  fun () -> begin")
    w("    t = time.local()")
    w("    now = t.hour * 60 + t.min")
    w("    if f == e then true")
    w("    elsif f < e then now >= f and now < e")
    w("    else now >= f or now < e end")
    w("  end")
    w("end")
    branches = []
    n = len(live.schedule)
    for i, seg in enumerate(live.schedule):
        nxt = live.schedule[(i + 1) % n].start_min
        branches.append(f"(in_window({seg.start_min}, {nxt}), {seg_names[i]})")
    w("scheduled = switch(id=" + q(f"{live.id}_schedule") + ", track_sensitive=true, [")
    w("  " + ",\n  ".join(branches))
    w("])")
    w("")

    # --- 폴백: 폴더가 비었을 때 무음 (사전 인코딩된 동일 스펙 파일, 저장소에 포함) ---
    w("# 재생할 곡이 없을 때 무음. silence_10s.m4a 는 서버가 아니라 저장소에 미리 인코딩되어 들어 있다.")
    w(f"silence = single(id={q(f'{live.id}_silence')}, {q(SILENCE_FILE)})")
    w(f"radio = fallback(id={q(f'{live.id}_fallback')}, track_sensitive=true, [scheduled, silence])")
    aspec = server["spec"]["audio"]
    if live.crossfade > 0:
        cf = float(live.crossfade)
        w(f"# 크로스페이드 {cf:g}초 — PCM 디코딩 → 섞기 → AAC 재인코딩 (이 라이브만 오디오 인코딩 발생)")
        w(f"radio = crossfade(id={q(f'{live.id}_crossfade')}, duration={cf + 2:.1f}, fade_in={cf:.1f}, fade_out={cf:.1f}, radio)")
        encoder = (f'%ffmpeg(format="adts", %audio(codec="aac", b={q(aspec["bitrate"])}, '
                   f'samplerate={int(aspec["sample_rate"])}, channels={int(aspec["channels"])}))')
    else:
        encoder = '%ffmpeg(format="adts", %audio.copy)'
    w("radio = source.on_track(radio, on_new_track)")
    w("")
    w(f"# ffmpeg(live@{live.id}) 가 http://127.0.0.1:{live.harbor_port}/{live.id}.aac 로 받아 copy 먹싱한다")
    w("output.harbor(")
    w(f"  {encoder},")
    w(f"  id={q(f'{live.id}_out')},")
    w(f"  port={live.harbor_port},")
    w(f"  mount={q(f'{live.id}.aac')},")
    w('  format="audio/aac",')
    w("  fallible=true,")
    w("  radio")
    w(")")
    return "\n".join(out) + "\n"


def write(live: LiveConfig, paths: Paths, server: dict) -> Path:
    p = paths.liq_file(live.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(render(live, paths, server), encoding="utf-8")
    tmp.replace(p)
    return p
