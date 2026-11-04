"""타이머: 방송 세션 시작 시각 기준 종료, 반복 시 새 방송 예약, 부팅 시 이어 가기/새 세션, 일일 재시작 건너뛰기."""

import time

import pytest

from conftest import make_live


class FakeProc:
    def __init__(self):
        self.desired = False
        self.st = {"state": "stopped"}
        self.events, self.restarted = [], []

    def start(self):
        self.desired = True
        self.st["state"] = "running"

    def stop(self, reason="정지"):
        self.desired = False
        self.st = {"state": "stopped", "message": reason}

    def set(self, **kw):
        self.st.update(kw)

    def event(self, level, msg):
        self.events.append(msg)

    def restart(self, reason, audio_too=False):
        self.restarted.append(reason)


@pytest.fixture
def sup(paths, settings, monkeypatch):
    from livemax import secrets, supervisor

    s = supervisor.Supervisor(paths, settings)
    procs = {}
    monkeypatch.setattr(s, "proc", lambda lid: procs.setdefault(lid, FakeProc()))
    s.procs = procs
    secrets.write_secret(paths.stream_key("live1"), "fake-0000-0000-0000-9z9z")
    return s


def _save(sup, **kw):
    lv = make_live(**kw)
    sup.store.save(lv)
    return lv


def test_duration_timer_ends_and_disables(sup):
    _save(sup, timer_mode="duration", timer_minutes=60)
    sup.start("live1")
    ses = sup.session("live1")
    ses["started_at"] -= 3601
    sup._write_session("live1", **ses)
    sup._timers()
    p = sup.procs["live1"]
    assert not p.desired and p.st["message"] == "타이머 종료"
    assert sup.store.get("live1").enabled is False and sup.session("live1") == {}


def test_repeat_schedules_new_broadcast(sup):
    _save(sup, timer_mode="duration", timer_minutes=60, timer_repeat=True, timer_gap_minutes=2)
    sup.start("live1")
    sup._write_session("live1", started_at=time.time() - 3601, alive_at=time.time())
    sup._timers()
    nxt = sup.session("live1")["next_start_at"]
    assert 100 < nxt - time.time() <= 120 and sup.store.get("live1").enabled
    lv = sup.store.get("live1")
    assert sup.timer_info(lv)["next_start_at"] == nxt
    sup._write_session("live1", next_start_at=time.time() - 1)
    sup._timers()
    ses = sup.session("live1")
    assert sup.procs["live1"].desired and abs(ses["started_at"] - time.time()) < 5  # 새 세션


def test_restart_keeps_session(sup):
    _save(sup, timer_mode="duration", timer_minutes=60)
    sup.start("live1")
    t0 = sup.session("live1")["started_at"]
    sup.restart("live1")
    assert sup.session("live1")["started_at"] == t0 and sup.procs["live1"].restarted == ["사용자 재시작"]


def test_boot_resume_or_fresh(sup):
    _save(sup, timer_mode="duration", timer_minutes=600)
    now = time.time()
    sup._write_session("live1", started_at=now - 3600, alive_at=now - 60)   # 1분 전까지 살아 있었음 → 이어서
    sup.start_all()
    assert sup.session("live1")["started_at"] == now - 3600
    sup.procs["live1"].desired = False
    sup._write_session("live1", started_at=now - 3600, alive_at=now - 3000)  # 오래 꺼져 있었음 → 새 방송
    sup.start_all()
    assert sup.session("live1")["started_at"] > now - 5


def test_boot_after_timer_expired_without_repeat(sup):
    _save(sup, timer_mode="duration", timer_minutes=60)
    now = time.time()
    sup._write_session("live1", started_at=now - 7200, alive_at=now - 30)
    sup.start_all()
    assert not sup.procs["live1"].desired and sup.store.get("live1").enabled is False


def test_planned_restart_skipped_with_timer(sup, settings, monkeypatch):
    from datetime import datetime, timezone

    _save(sup, timer_mode="duration", timer_minutes=710)
    sup.start("live1")
    now = datetime.now(timezone.utc)
    monkeypatch.setitem(settings["restart"], "base_utc", now.strftime("%H:%M"))
    sup._planned_restarts()
    assert sup.procs["live1"].restarted == []
    lv = sup.store.get("live1")
    lv.timer_mode = "off"
    sup.store.save(lv)
    sup._planned_restarts()
    assert sup.procs["live1"].restarted == ["일일 재시작"]


def test_stop_clears_session(sup):
    _save(sup, timer_mode="duration", timer_minutes=60, timer_repeat=True)
    sup.start("live1")
    sup.stop("live1")
    assert sup.session("live1") == {} and sup.store.get("live1").enabled is False


def test_remove_purges_leftovers(sup, paths):
    from livemax import db

    _save(sup)
    for suffix in (".liq", ".m3u", ".nowplaying.json", ".stunnel.conf"):
        (paths.run_dir / f"live1{suffix}").write_text("x")
    (paths.concat_dir / "live1").mkdir(parents=True)
    (paths.concat_dir / "live1" / "a.ffconcat").write_text("x")
    for pre in ("live", "audio", "tls"):
        (paths.logs / f"{pre}-live1.log").write_text("x")
    (paths.logs / "live-live10.log").write_text("다른 라이브")
    with db.session(paths.db) as c:
        db.add_event(c, "error", "live", "x", "live1")
    sup.remove("live1")
    assert not list(paths.run_dir.glob("live1.*")) and not (paths.concat_dir / "live1").exists()
    assert not list(paths.logs.glob("*-live1.log")) and (paths.logs / "live-live10.log").exists()
    with db.session(paths.db) as c:
        assert c.execute("SELECT COUNT(*) FROM events WHERE live_id='live1'").fetchone()[0] == 0
    assert not paths.stream_key("live1").exists() and sup.store.get("live1") is None
