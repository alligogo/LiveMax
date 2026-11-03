"""라이브 설정 → Liquidsoap 스크립트 (v2: 시간대 편성 제거).

대상: Liquidsoap 2.2.x (Ubuntu 24.04 = 2.2.4, `liquidsoap -h <함수>` 로 시그니처 확인).
  crossfade == 0 (기본): %ffmpeg(format="adts", %audio.copy) — AAC 프레임을 디코딩 없이 그대로 (CPU 최소)
  crossfade > 0: crossfade() 는 PCM 에서만 동작 → 이 라이브만 디코딩·섞기·AAC 재인코딩 (영상은 copy)
폴더 감시(reload_mode="watch")로 곡 추가·삭제가 재시작 없이 반영된다.
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


def render(live: Live, paths: Paths, settings: dict) -> str:
    pl_mode, loop, norepeat = _MODE[live.audio_mode]
    aspec = settings["spec"]["audio"]
    np_path = paths.run_dir / f"{live.id}.nowplaying.json"
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
    w(f"norepeat_n = {live.norepeat_count if norepeat else 0}")
    w("history = ref([])")
    w("rejects = ref(0)")
    w("def not_recent(r) =")
    w("  uri = request.uri(r)")
    w("  if list.mem(uri, history()) and rejects() < 50 then")
    w("    rejects := rejects() + 1")
    w("    false")
    w("  else")
    w("    rejects := 0")
    w("    true")
    w("  end")
    w("end")
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
    names = []
    for i, folder in enumerate(live.audio_folders):
        args = [f'id={q(f"{live.id}_pl_{i}")}', f"mode={q(pl_mode)}", f"loop={'true' if loop else 'false'}",
                'reload_mode="watch"']
        if norepeat:
            args.append("check_next=not_recent")
        w(f"pl_{i} = playlist({', '.join(args)}, {q(paths.audio / folder)})")
        names.append(f"pl_{i}")
    combine = "rotate" if pl_mode == "normal" else "random"
    w(f"music = {names[0]}" if len(names) == 1 else f"music = {combine}([{', '.join(names)}])")
    w(f"silence = single(id={q(f'{live.id}_silence')}, {q(SILENCE_FILE)})")
    w(f"radio = fallback(id={q(f'{live.id}_fallback')}, track_sensitive=true, [music, silence])")
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
    p = paths.run_dir / f"{live.id}.liq"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(render(live, paths, settings), encoding="utf-8")
    tmp.replace(p)
    return p
