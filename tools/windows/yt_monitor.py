# -*- coding: utf-8 -*-
"""LiveMax 유튜브 측정기 (PC 용, LiveMax 서버와 무관)

자는 동안 켜 두면 1분마다 유튜브에 물어서 라이브별 상태를 기록한다.
  - 방송 상태 (실시간 / 종료 …)
  - 스트림 상태 (좋음 / 보통 / 나쁨 / 데이터 없음) + 유튜브가 지적하는 문제
  - 동시 시청자 수
기록: yt_logs/YYYY-MM-DD.csv (1분마다 한 줄씩), yt_logs/YYYY-MM-DD_변화.txt (상태가 바뀐 순간만)
끝낼 때(Ctrl+C) 라이브별 요약을 보여 준다.

설정: 같은 폴더의 yt_monitor_config.json (yt_monitor_config.example.json 을 복사해서 채움)
  - client_id / client_secret : 구글 클라우드의 OAuth 2.0 클라이언트
  - channels[].refresh_token  : 채널마다 받아 둔 리프레시 토큰 (권한에 youtube.readonly 필요)
  - channels[].lives[]        : 라이브 이름과 유튜브 영상 ID (watch?v= 뒤, 주소를 통째로 넣어도 됨)
파이썬 기본 기능만 사용 (추가 설치 없음). 토큰·키는 이 PC 의 설정 파일에만 있다.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "yt_monitor_config.json"
LOGDIR = HERE / "yt_logs"
API = "https://www.googleapis.com/youtube/v3"
HEALTH_KO = {"good": "좋음", "ok": "보통", "bad": "나쁨", "noData": "데이터 없음", "revoked": "취소됨"}
LIFE_KO = {"live": "실시간", "complete": "종료", "ready": "준비", "testing": "테스트", "liveStarting": "시작 중",
           "testStarting": "테스트 시작", "created": "생성됨", "revoked": "취소됨"}


def video_id(s: str) -> str:
    s = s.strip()
    m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/)([\w-]{11})", s)
    return m.group(1) if m else s


def http_json(url: str, data: dict | None = None, token: str | None = None) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            detail = {}
        raise RuntimeError(f"HTTP {e.code} {json.dumps(detail, ensure_ascii=False)[:300]}") from None


class Channel:
    def __init__(self, cfg: dict, client_id: str, client_secret: str):
        self.name = cfg.get("name", "채널")
        self.refresh = cfg["refresh_token"]
        self.cid, self.secret = client_id, client_secret
        self.lives = [{"name": x["name"], "id": video_id(x["video_id"])} for x in cfg.get("lives", [])]
        self.token, self.exp = None, 0.0

    def access(self) -> str:
        if self.token and time.time() < self.exp - 60:
            return self.token
        try:
            r = http_json("https://oauth2.googleapis.com/token", {
                "client_id": self.cid, "client_secret": self.secret,
                "refresh_token": self.refresh, "grant_type": "refresh_token"})
        except RuntimeError as e:
            if "invalid_grant" in str(e):
                raise RuntimeError(f"[{self.name}] 토큰이 만료됐거나 취소됨 — OAuth 동의 화면이 '테스트' 상태면 7일마다 만료됩니다. "
                                   "'앱 게시(프로덕션)' 후 토큰을 다시 받으세요") from None
            raise
        self.token, self.exp = r["access_token"], time.time() + int(r.get("expires_in", 3600))
        return self.token

    def poll(self) -> list[dict]:
        """라이브마다 {name, id, life, health, issues, viewers}. API 3회 (채널당)."""
        if not self.lives:
            return []
        tok = self.access()
        ids = ",".join(x["id"] for x in self.lives)
        b = http_json(f"{API}/liveBroadcasts?part=status,contentDetails&id={ids}", token=tok)
        bc = {i["id"]: i for i in b.get("items", [])}
        sids = sorted({i.get("contentDetails", {}).get("boundStreamId") for i in bc.values()} - {None})
        st = {}
        if sids:
            s = http_json(f"{API}/liveStreams?part=status&id={','.join(sids)}", token=tok)
            st = {i["id"]: i for i in s.get("items", [])}
        v = http_json(f"{API}/videos?part=liveStreamingDetails&id={ids}", token=tok)
        vd = {i["id"]: i.get("liveStreamingDetails", {}) for i in v.get("items", [])}
        out = []
        for x in self.lives:
            bi = bc.get(x["id"])
            life = bi["status"].get("lifeCycleStatus", "?") if bi else "없음"
            sid = bi.get("contentDetails", {}).get("boundStreamId") if bi else None
            hs = (st.get(sid) or {}).get("status", {}).get("healthStatus", {}) if sid else {}
            issues = [f"{i.get('severity', '')}:{i.get('type', '')}" for i in hs.get("configurationIssues", [])]
            viewers = vd.get(x["id"], {}).get("concurrentViewers")
            out.append({"name": x["name"], "id": x["id"], "life": life, "health": hs.get("status", "-"),
                        "issues": issues, "viewers": int(viewers) if viewers else 0})
        return out


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if not CONFIG.exists():
        print(f"설정 파일이 없습니다: {CONFIG}\n  yt_monitor_config.example.json 을 복사해 이름을 yt_monitor_config.json 으로 바꾸고 채우세요.")
        input("Enter 로 종료")
        return
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    chans = [Channel(c, cfg["client_id"], cfg["client_secret"]) for c in cfg.get("channels", [])]
    interval = max(30, int(cfg.get("interval_seconds", 60)))
    units = sum(3 for c in chans if c.lives) * (86400 // interval)
    LOGDIR.mkdir(exist_ok=True)
    print(f"유튜브 측정 시작 — {len(chans)}개 채널, 라이브 {sum(len(c.lives) for c in chans)}개, {interval}초마다")
    print(f"하루 API 사용량 약 {units:,} (무료 한도 10,000{' — 넘습니다! interval_seconds 를 늘리세요' if units > 10000 else ''})")
    print("끝낼 때 Ctrl+C → 요약이 나옵니다\n")

    last: dict[str, tuple] = {}
    stats: dict[str, dict] = {}
    t_start = time.time()
    try:
        while True:
            now = datetime.now()
            day = now.strftime("%Y-%m-%d")
            csv_path, ev_path = LOGDIR / f"{day}.csv", LOGDIR / f"{day}_변화.txt"
            new = not csv_path.exists()
            rows = []
            for ch in chans:
                try:
                    rows += [(ch.name, r) for r in ch.poll()]
                except Exception as e:  # noqa: BLE001
                    msg = f"{now:%H:%M:%S} [{ch.name}] 확인 실패: {e}"
                    print(msg)
                    with open(ev_path, "a", encoding="utf-8") as f:
                        f.write(msg + "\n")
            with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["시각", "채널", "라이브", "영상ID", "방송", "스트림", "시청자", "유튜브 지적"])
                for chn, r in rows:
                    w.writerow([f"{now:%H:%M:%S}", chn, r["name"], r["id"], LIFE_KO.get(r["life"], r["life"]),
                                HEALTH_KO.get(r["health"], r["health"]), r["viewers"], " ".join(r["issues"])])
            line = []
            for chn, r in rows:
                key = f"{chn}/{r['name']}"
                s = stats.setdefault(key, {"n": 0, "good": 0, "bad": 0, "nodata": 0, "notlive": 0, "vmax": 0, "vsum": 0})
                s["n"] += 1
                s["good"] += r["health"] in ("good", "ok")
                s["bad"] += r["health"] == "bad"
                s["nodata"] += r["health"] == "noData"
                s["notlive"] += r["life"] != "live"
                s["vmax"] = max(s["vmax"], r["viewers"])
                s["vsum"] += r["viewers"]
                cur = (r["life"], r["health"], tuple(r["issues"]))
                if last.get(key) and last[key] != cur:
                    msg = (f"{now:%H:%M:%S} {key}: {LIFE_KO.get(last[key][0], last[key][0])}/{HEALTH_KO.get(last[key][1], last[key][1])}"
                           f" → {LIFE_KO.get(r['life'], r['life'])}/{HEALTH_KO.get(r['health'], r['health'])}"
                           f"{' ' + ' '.join(r['issues']) if r['issues'] else ''}")
                    print("  ⚠ " + msg)
                    with open(ev_path, "a", encoding="utf-8") as fe:
                        fe.write(msg + "\n")
                last[key] = cur
                mark = "✓" if r["life"] == "live" and r["health"] in ("good", "ok") else "✗"
                line.append(f"{mark}{r['name']}({HEALTH_KO.get(r['health'], r['health'])},👁{r['viewers']})")
            print(f"{now:%H:%M:%S}  " + "  ".join(line))
            time.sleep(interval)
    except KeyboardInterrupt:
        hrs = (time.time() - t_start) / 3600
        print(f"\n================ 요약 ({hrs:.1f}시간) ================")
        for key, s in stats.items():
            n = max(1, s["n"])
            print(f"  {key:24s} 정상 {s['good'] * 100 / n:5.1f}% · 나쁨 {s['bad']}회 · 데이터없음 {s['nodata']}회 · "
                  f"실시간아님 {s['notlive']}회 · 시청자 최고 {s['vmax']} / 평균 {s['vsum'] / n:.0f}")
        print(f"\n기록: {LOGDIR}")
        input("Enter 로 종료")


if __name__ == "__main__":
    os.chdir(HERE)
    main()
