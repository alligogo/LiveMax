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


# 지우는 중인 곡 — 웹의 곡 삭제와 supervisor 의 주기적 목록 갱신이 겹쳐도 다시 목록에 들어가지 않게 공유
DELETING: set[str] = set()


def next_path(live: Live, paths: Paths) -> Path:
    """Liquidsoap 이 미리 골라 둔 다음 곡 (곡 삭제 보호용)."""
    return paths.run_dir / f"{live.id}.next.json"


def playlist_lines(live: Live, paths: Paths, exclude: set[str] | None = None) -> str:
    """선택한 음악 폴더들의 전체 곡 (폴더 순서 → 파일 이름 순). exclude: 지우려는 곡을 먼저 목록에서 뺄 때."""
    from . import catalog

    files: list[str] = []
    for folder in live.audio_folders:
        files.extend(str(p) for p in catalog.list_files(paths, "audio", folder))
    skip = set(exclude or ()) | DELETING
    return "".join(f"{f}\n" for f in files if "\n" not in f and f not in skip)


def write_m3u(live: Live, paths: Paths, exclude: set[str] | None = None) -> bool:
    """내용이 바뀌었을 때만 덮어쓴다. 바뀌었으면 True.

    rename 으로 교체하면 Liquidsoap 의 파일 감시가 첫 교체 뒤로 끊긴다 (2.2.4 실측) → 같은 파일에 한 번에 덮어쓴다.
    """
    p = m3u_path(live, paths)
    text = playlist_lines(live, paths, exclude)
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
    w("settings.log.stdout.set(false)")  # 화면 출력은 버려지므로 만들지 않는다
    w("settings.log.file.set(true)")
    w(f"settings.log.file.path.set({q(paths.logs / f'audio-{live.id}.log')})")
    w("settings.log.level.set(2)")
    # 소리를 처리하는 단위 0.04초 → 0.2초: 1초에 깨어나는 횟수 25 → 5 (실측 CPU 약 -56%, 곡 전환·소리 이상 없음)
    w(f"settings.frame.duration.set({float(settings.get('audio_frame_seconds', 0.2)):.2f})")  # 문제(심각·치명)만 기록 — 곡마다 남기던 기록은 뺌 (지금 곡은 nowplaying.json 으로 충분)
    w('settings.harbor.bind_addrs.set(["127.0.0.1"])')
    w("settings.server.telnet.set(false)")
    w("settings.init.allow_root.set(true)")
    w("")
    w(f"live_id = {q(live.id)}")
    w(f"nowplaying_path = {q(np_path)}")
    w(f"m3u_path = {q(m3u)}")
    w(f"next_path = {q(next_path(live, paths))}")
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
    w("    # 다음 곡을 기록 — 웹에서 이 곡을 지우려 하면 거부한다 (재생 직전 곡이 사라지지 않게)")
    w('    file.write(data=json.stringify({file=uri, at=time()}), next_path)')
    w("    true")
    w("  end")
    w("end")
    w("def on_new_track(m) =")
    w('  uri = m["filename"]')
    w('  np = json.stringify({live=live_id, file=uri, title=m["title"], artist=m["artist"], started=time()})')
    w("  file.write(data=np, nowplaying_path)")
    w("end")
    w("")
    args = [f'id={q(f"{live.id}_pl")}', f"mode={q(pl_mode)}", f"loop={'true' if loop else 'false'}",
            'reload_mode="watch"']
    args.append("check_next=not_recent")  # 중복방지가 꺼져 있어도 창이 0 이라 모두 통과, '다음 곡' 기록용
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
    # 음악 완충: 곡 읽기·곡 바뀔 때 파일 기록(nowplaying/next)을 '음악 시계'로 따로 돌리고, 내보내기와 사이에 버퍼를 둔다.
    # → 디스크가 순간 느려져 음악 쪽이 1~2초 밀려도 ffmpeg 로 나가는 소리는 끊기지 않는다
    #   (실측: 같은 시계면 'clock.main: We must catchup 1.80 seconds' 로 내보내기가 멈추고, 나누면 내보내기 쪽은 0회)
    buf = float(settings.get("audio_buffer_seconds", 2.0))
    if buf > 0:
        w(f"clock.assign_new(id={q(f'{live.id}_music')}, [radio])")
        w(f"radio = buffer(id={q(f'{live.id}_buf')}, buffer={buf:.1f}, max={max(buf * 3, 6.0):.1f}, radio)")
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


# ---------------------------------------------------------------- 채널별 묶음 (B안, DESIGN 11-D 19)
# 라이브마다 Liquidsoap 을 하나씩 띄우면 같은 엔진(언어 실행기·라이브러리)이 라이브 수만큼 메모리에 올라간다
# (실측 10개: 따로 835MB → 묶음 152MB). 같은 채널의 라이브들을 프로그램 하나에 넣되, 라이브마다의 재생 목록·
# 중복방지·시계·버퍼·출력 포트는 그대로 따로 둔다 → ffmpeg 쪽은 바뀌는 것이 없다 (각자 127.0.0.1:<port>).
# 위험은 '그 프로그램이 죽으면 그 채널 라이브들 음악이 같이 멈춤' — 그래서 채널 단위(최대 10개)로만 묶는다.

def group_key(live: Live) -> str:
    """같은 Liquidsoap 에 들어갈 묶음: 같은 채널. 채널이 없으면 혼자."""
    return f"ch-{live.channel_id}" if live.channel_id else f"solo-{live.id}"


GROUP_MAX_DEFAULT = 10


def group_keys(lives: list[Live], max_n: int = GROUP_MAX_DEFAULT) -> dict[str, str]:
    """라이브 id → 묶음 이름. 같은 채널을 라이브 번호 순으로 최대 max_n 개씩 (DESIGN 11-D 19).
    채널 하나에 라이브가 몰려도(예: 50개) 묶음 하나가 전부를 떠안지 않게. 첫 묶음 이름은 예전과 같은 'ch-<채널>' →
    10개 이하 채널은 이름·로그 파일이 그대로. 번호 순이라 새 라이브(큰 번호)는 마지막 묶음 뒤에 붙고 앞 묶음은 안 흔들린다."""
    from .catalog import natural_key  # catalog → probe 를 끌어오므로 여기서만
    max_n = max(1, int(max_n or GROUP_MAX_DEFAULT))
    out: dict[str, str] = {}
    by_ch: dict[str, list[Live]] = {}
    for lv in lives:
        if lv.channel_id:
            by_ch.setdefault(lv.channel_id, []).append(lv)
        else:
            out[lv.id] = f"solo-{lv.id}"
    for cid, lst in by_ch.items():
        for i, lv in enumerate(sorted(lst, key=lambda x: natural_key(x.id))):
            k = i // max_n
            out[lv.id] = f"ch-{cid}" if k == 0 else f"ch-{cid}-{k + 1}"
    return out


def group_log_name(key: str) -> str:
    return f"audio-g-{key}"


# ---------------------------------------------------------------- 무중단 묶음 (v2.13, DESIGN 11-F)
# v2.11~12 는 묶음 구성이 바뀌면(라이브 추가·삭제·재생 방식 변경) 묶음 프로그램을 통째로 껐다 켰다. 그러면 이미 송출 중이던
# 같은 묶음 라이브들의 ffmpeg 음악 연결이 끊기고, ffmpeg 는 죽지 않은 채 '소리 없는 영상'만 계속 보냈다 (41번째 라이브
# 추가 때 실제 사고 — 감시는 정상으로 봄). 그래서 묶음 프로그램은 한 번 켜면 그대로 두고, 프로그램이 1초마다
# run/<묶음>.members.json 을 읽어 새 항목만 올리고 빠진 항목만 내린다 (2.2.4 실측: 올리는 동안 기존 라이브 공백 0.4초 미만).
#   - 묶음마다 harbor 포트 하나, 라이브마다 '매번 새 이름'의 주소(mount) — 같은 이름·포트를 다시 쓰면 2.2.4 에서 안 열린다
#     (내린 출력의 이름이 남음). 설정이 바뀐 라이브는 새 이름으로 다시 올리고 옛 것은 내린다.
#   - 2.2.4 는 출력만 내릴 수 있고 그 앞의 재생 목록은 못 내린다 → 내린 라이브 몫이 조금 남는다 (실측 약 1.3MB/회).
#     라이브 삭제·음악 설정 변경 때만 생기고, 묶음이 다시 켜질 때(업데이트·재부팅·채널 전체 정지) 없어진다.

def members_path(paths: Paths, key: str) -> Path:
    return paths.run_dir / f"{key}.members.json"


def audio_sig(live: Live) -> str:
    """이 값이 바뀌면 그 라이브 음악만 새 주소로 다시 올린다 (재생 방식·중복방지 수·크로스페이드)."""
    import hashlib

    pl_mode, loop, norepeat = _MODE[live.audio_mode]
    raw = f"{pl_mode}|{loop}|{live.norepeat_count if norepeat else 0}|{float(live.crossfade):.1f}"
    return hashlib.sha1(raw.encode()).hexdigest()[:8]


def member_entry(live: Live, paths: Paths, token: str, port: int) -> dict:
    """members.json 한 줄. token: 프로그램 안에서 겹치지 않는 이름 (예: live13_2) — 소스 이름·주소에 쓴다."""
    pl_mode, loop, norepeat = _MODE[live.audio_mode]
    for v in (token, m3u_path(live, paths), paths.run_dir):
        q(v)  # '#{' 가 들어간 경로는 거부 (Liquidsoap 문자열 보간)
    return {"id": token, "live": live.id, "mount": f"{token}.aac", "port": int(port),
            "m3u": str(m3u_path(live, paths)), "np": str(paths.run_dir / f"{live.id}.nowplaying.json"),
            "next": str(next_path(live, paths)), "mode": pl_mode, "loop": bool(loop),
            "norepeat": int(live.norepeat_count if norepeat else 0), "cf": float(live.crossfade)}


def write_members(paths: Paths, key: str, entries: list[dict]) -> bool:
    """내용이 바뀌었을 때만 통째로 바꿔 쓴다 (rename — 프로그램은 감시가 아니라 1초마다 읽으므로 안전). 바뀌었으면 True."""
    import json

    p = members_path(paths, key)
    text = json.dumps(sorted(entries, key=lambda e: e["id"]), ensure_ascii=False)
    try:
        if p.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)
    return True


_DYN = r"""# 자동 생성 — 직접 수정하지 말 것 (음악 묶음 @KEY@, LiveMax). 라이브 목록은 members.json 에서 읽는다
settings.log.stdout.set(false)
settings.log.file.set(true)
settings.log.file.path.set(@LOG@)
settings.log.level.set(2)
settings.frame.duration.set(@FRAME@)
settings.harbor.bind_addrs.set(["127.0.0.1"])
settings.server.telnet.set(false)
settings.init.allow_root.set(true)

members_path = @MEMBERS@
silence_file = @SILENCE@

# 라이브 하나의 음악: 재생 목록 + 중복방지 + '다음 곡' 기록 (라이브별 스크립트 render 와 같은 동작)
def mk_base(m) =
  history = ref([])
  rejects = ref(0)
  def library_size() =
    lines = string.split(separator="\n", file.contents(m.m3u))
    list.length(list.filter(fun (l) -> l != "", lines))
  end
  def not_recent(r) =
    uri = request.uri(r)
    # 곡이 적으면 창을 줄인다 (3곡이면 최근 1곡만) — 창이 너무 크면 고를 곡이 없어 같은 곡이 반복됨
    window = min(m.norepeat, library_size() / 2)
    if list.mem(uri, list.prefix(window, history())) and rejects() < 200 then
      rejects := rejects() + 1
      false
    else
      rejects := 0
      h = list.add(uri, history())
      history := if list.length(h) > m.norepeat then list.prefix(m.norepeat, h) else h end
      file.write(data=json.stringify({file=uri, at=time()}), m.next)
      true
    end
  end
  music = playlist(id=m.id ^ "_pl", mode=m.mode, loop=m.loop, reload_mode="watch", check_next=not_recent, m.m3u)
  silence = single(id=m.id ^ "_silence", silence_file)
  # track_sensitive=false: 음악이 준비되는 즉시 무음에서 넘어간다
  fallback(id=m.id ^ "_fallback", track_sensitive=false, [music, silence])
end
def on_new_track(m) =
  fun (md) -> begin
    np = json.stringify({live=m.live, file=md["filename"], title=md["title"], artist=md["artist"], started=time()})
    file.write(data=np, m.np)
  end
end
# 그대로 내보내기 (기본 — AAC 를 디코딩 없이)
def mk_copy(m) =
  radio = source.on_track(mk_base(m), on_new_track(m))
@TAIL@  out = output.harbor(@ENC_COPY@, id=m.id ^ "_out", port=m.port, mount=m.mount,
    format="audio/aac", fallible=true, radio)
  fun () -> out.shutdown()
end
# 크로스페이드 (이 라이브만 디코딩·섞기·AAC 재인코딩)
def mk_xf(m) =
  radio = crossfade(id=m.id ^ "_xf", duration=m.cf + 2., fade_in=m.cf, fade_out=m.cf, mk_base(m))
  radio = source.on_track(radio, on_new_track(m))
@TAIL@  out = output.harbor(@ENC_XF@, id=m.id ^ "_out", port=m.port, mount=m.mount,
    format="audio/aac", fallible=true, radio)
  fun () -> out.shutdown()
end

# 1초마다 목록을 읽어: 빠진 항목은 내리고, 새 항목은 올린다. 이미 도는 항목은 건드리지 않는다
running = ref([])
def sync() =
  try
    let json.parse (ms : [{id: string, live: string, mount: string, port: int, m3u: string, np: string, next: string, mode: string, loop: bool, norepeat: int, cf: float}]) = file.contents(members_path)
    want = list.map(fun (m) -> m.id, ms)
    gone = list.filter(fun (r) -> not list.mem(fst(r), want), running())
    list.iter(fun (r) -> begin
      log.important(label="group", "REMOVE #{fst(r)}")
      stop = snd(r)
      stop()
    end, gone)
    running := list.filter(fun (r) -> list.mem(fst(r), want), running())
    list.iter(fun (m) -> begin
      if not list.assoc.mem(m.id, running()) then
        try
          stop = if m.cf > 0. then mk_xf(m) else mk_copy(m) end
          running := list.add((m.id, stop), running())
          log.important(label="group", "ADD #{m.id}")
        catch err do
          log.severe(label="group", "ADD FAILED #{m.id}: #{err}")
        end
      end
    end, ms)
  catch err do
    log.severe(label="group", "members read failed: #{err}")
  end
end
sync()
thread.run(every=1., sync)
"""


def render_dynamic(paths: Paths, settings: dict, key: str) -> str:
    """묶음 프로그램 스크립트 — 라이브 목록은 들어 있지 않다 (members.json 에서 읽음). 서버 설정이 같으면 늘 같은 내용."""
    aspec = settings["spec"]["audio"]
    buf = float(settings.get("audio_buffer_seconds", 2.0))
    # 음악 완충(render 와 같음): 곡 읽기를 '음악 시계'로 따로 돌리고 내보내기 사이에 버퍼
    tail = (f'  clock.assign_new(id=m.id ^ "_music", [radio])\n'
            f'  radio = buffer(id=m.id ^ "_buf", buffer={buf:.1f}, max={max(buf * 3, 6.0):.1f}, radio)\n') if buf > 0 else ""
    rep = {
        "@KEY@": key,
        "@LOG@": q(paths.logs / (group_log_name(key) + ".log")),
        "@FRAME@": f"{float(settings.get('audio_frame_seconds', 0.2)):.2f}",
        "@MEMBERS@": q(members_path(paths, key)),
        "@SILENCE@": q(SILENCE_FILE),
        "@TAIL@": tail,
        "@ENC_COPY@": '%ffmpeg(format="adts", %audio.copy)',
        "@ENC_XF@": (f'%ffmpeg(format="adts", %audio(codec="aac", b={q(aspec["bitrate"])}, '
                     f'samplerate={int(aspec["sample_rate"])}, channels={int(aspec["channels"])}))'),
    }
    text = _DYN
    for k, v in rep.items():
        text = text.replace(k, v)
    return text


def write_dynamic(paths: Paths, settings: dict, key: str, text: str | None = None) -> Path:
    p = paths.run_dir / f"{key}.group.liq"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text if text is not None else render_dynamic(paths, settings, key), encoding="utf-8")
    tmp.replace(p)
    return p
