"""라이브 설정 → Liquidsoap 스크립트 (v2: 시간대 편성 제거).

대상: Liquidsoap 2.2.x (Ubuntu 24.04 = 2.2.4, `liquidsoap -h <함수>` 로 시그니처 확인).
  crossfade == 0 (기본): %ffmpeg(format="adts", %audio.copy) — AAC 프레임을 디코딩 없이 그대로 (CPU 최소)
  crossfade > 0: crossfade() 는 PCM 에서만 동작 → 이 라이브만 디코딩·섞기·AAC 재인코딩 (영상은 copy)
여러 폴더를 골라도 곡 단위로 섞는다: 선택한 폴더들의 전체 곡 목록을 run/<id>.m3u 로 만들고
playlist(m3u, reload_mode="watch") 하나로 재생한다. (v2.1 까지는 폴더마다 playlist 를 만들고 random() 으로
골라서 3곡짜리 폴더가 39곡짜리 폴더와 같은 50% 비중을 받았다.) 곡 추가·삭제는 supervisor 가 m3u 를 갱신하면
Liquidsoap 이 재시작 없이 다시 읽는다.
중복방지: 최근 N곡(설정값, 단 전체 곡 수의 절반까지)은 다시 고르지 않는다.
harbor 는 기본 0.0.0.0 이므로 반드시 127.0.0.1 로 묶는다.
"""

from __future__ import annotations

from pathlib import Path

from .config import Live
from .paths import ASSETS_DIR, Paths

SILENCE_FILE = ASSETS_DIR / "silence_10s.m4a"
_MODE = {  # → (playlist mode, loop, 중복방지)
    "random_norepeat": ("randomize", True, True),
    "random": ("random", True, False),
    "sequential_loop": ("normal", True, False),
    "sequential": ("normal", False, False),
}


def q(s: str | Path) -> str:
    """Liquidsoap 문자열. '#{' 는 보간되며 막을 방법이 없어(2.2.4 확인) 거부한다."""
    s = str(s)
    if "#{" in s:
        raise ValueError(f"Liquidsoap 문자열에 '#{{' 사용 불가: {s!r}")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def m3u_path(live: Live, paths: Paths) -> Path:
    return paths.run_dir / f"{live.id}.m3u"


def playlist_lines(live: Live, paths: Paths) -> str:
    """선택한 음악 폴더들의 전체 곡 (폴더 순서 → 파일 이름 순)."""
    from . import catalog

    files: list[str] = []
    for folder in live.audio_folders:
        files.extend(str(p) for p in catalog.list_files(paths, "audio", folder))
    return "".join(f"{f}\n" for f in files if "\n" not in f)


def write_m3u(live: Live, paths: Paths) -> bool:
    """내용이 바뀌었을 때만 덮어쓴다. 바뀌었으면 True.

    rename 으로 교체하면 Liquidsoap 의 파일 감시가 첫 교체 뒤로 끊긴다 (2.2.4 실측) → 같은 파일에 한 번에 덮어쓴다.
    """
    p = m3u_path(live, paths)
    text = playlist_lines(live, paths)
    try:
        if p.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    with open(p, "r+b" if p.exists() else "wb") as f:
        f.write(data)  # 한 번의 write → 새 내용, 그 뒤 남는 부분만 잘라냄 (빈 파일이 보이는 순간 없음)
        f.truncate(len(data))
    return True


def render(live: Live, paths: Paths, settings: dict) -> str:
    pl_mode, loop, norepeat = _MODE[live.audio_mode]
    aspec = settings["spec"]["audio"]
    np_path = paths.run_dir / f"{live.id}.nowplaying.json"
    m3u = m3u_path(live, paths)
    o: list[str] = []
    w = o.append
    w(f"# 자동 생성 — 직접 수정하지 말 것 ({live.id}, LiveMax)")
    w("settings.log.stdout.set(true)")
    w("settings.log.file.set(true)")
    w(f"settings.log.file.path.set({q(paths.logs / f'audio-{live.id}.log')})")
    w("settings.log.level.set(3)")
    w('settings.harbor.bind_addrs.set(["127.0.0.1"])')
    w("settings.server.telnet.set(false)")
    w("settings.init.allow_root.set(true)")
    w("")
    w(f"live_id = {q(live.id)}")
    w(f"nowplaying_path = {q(np_path)}")
    w(f"m3u_path = {q(m3u)}")
    w(f"norepeat_n = {live.norepeat_count if norepeat else 0}")
    w("history = ref([])")
    w("rejects = ref(0)")
    w("def library_size() =")
    w('  lines = string.split(separator="\\n", file.contents(m3u_path))')
    w('  list.length(list.filter(fun (l) -> l != "", lines))')
    w("end")
    w("def not_recent(r) =")
    w("  uri = request.uri(r)")
    w("  # 곡이 적으면 창을 줄인다 (3곡이면 최근 1곡만) — 창이 너무 크면 고를 곡이 없어 같은 곡이 반복됨")
    w("  window = min(norepeat_n, library_size() / 2)")
    w("  if list.mem(uri, list.prefix(window, history())) and rejects() < 200 then")
    w("    rejects := rejects() + 1")
    w("    false")
    w("  else")
    w("    rejects := 0")
    w("    # 고르는 순간 기록한다 (다음 곡은 지금 곡이 재생되기 전에 미리 골라지므로 재생 시점 기록은 한 박자 늦음)")
    w("    h = list.add(uri, history())")
    w("    history := if list.length(h) > norepeat_n then list.prefix(norepeat_n, h) else h end")
    w("    true")
    w("  end")
    w("end")
    w("def on_new_track(m) =")
    w('  uri = m["filename"]')
    w('  log.important("now playing: #{uri}")')
    w('  np = json.stringify({live=live_id, file=uri, title=m["title"], artist=m["artist"], started=time()})')
    w("  file.write(data=np, nowplaying_path)")
    w("end")
    w("")
    args = [f'id={q(f"{live.id}_pl")}', f"mode={q(pl_mode)}", f"loop={'true' if loop else 'false'}",
            'reload_mode="watch"']
    if norepeat:
        args.append("check_next=not_recent")
    w(f"music = playlist({', '.join(args)}, m3u_path)")
    w(f"silence = single(id={q(f'{live.id}_silence')}, {q(SILENCE_FILE)})")
    # track_sensitive=false: 음악이 준비되는 즉시 무음에서 넘어간다 (시작 때 무음 10초를 끝까지 틀지 않게)
    w(f"radio = fallback(id={q(f'{live.id}_fallback')}, track_sensitive=false, [music, silence])")
    if live.crossfade > 0:
        cf = float(live.crossfade)
        w(f"radio = crossfade(id={q(f'{live.id}_xf')}, duration={cf + 2:.1f}, fade_in={cf:.1f}, fade_out={cf:.1f}, radio)")
        enc = (f'%ffmpeg(format="adts", %audio(codec="aac", b={q(aspec["bitrate"])}, '
               f'samplerate={int(aspec["sample_rate"])}, channels={int(aspec["channels"])}))')
    else:
        enc = '%ffmpeg(format="adts", %audio.copy)'
    w("radio = source.on_track(radio, on_new_track)")
    w(f"output.harbor({enc}, id={q(f'{live.id}_out')}, port={live.harbor_port}, mount={q(f'{live.id}.aac')},")
    w('  format="audio/aac", fallible=true, radio)')
    return "\n".join(o) + "\n"


def write(live: Live, paths: Paths, settings: dict) -> Path:
    write_m3u(live, paths)
    p = paths.run_dir / f"{live.id}.liq"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(render(live, paths, settings), encoding="utf-8")
    tmp.replace(p)
    return p
