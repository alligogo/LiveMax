"""유튜브 방송 상태 확인 (DESIGN 6.2-3).

서버는 멀쩡한데 유튜브 쪽에서 방송이 끝나는 경우가 실제로 가장 잦다.
YouTube Data API v3 videos.list(part=snippet,liveStreamingDetails) — 호출당 1 unit.
라이브 10개 × 5분 주기 = 하루 2,880 unit (기본 할당 10,000 의 29%).

필요한 것: secrets/youtube_api.key (API 키), 라이브 YAML 의 youtube.video_id.
둘 중 하나라도 없으면 확인을 건너뛴다 (None 반환).
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

log = logging.getLogger("livemax.youtube")
API = "https://www.googleapis.com/youtube/v3/videos"


def live_status(video_id: str, api_key: str, timeout: int = 15) -> dict | None:
    """{'live': bool, 'content': 'live'|'none'|'upcoming', 'viewers': int|None, 'actual_end': str|None}"""
    if not video_id or not api_key:
        return None
    qs = urllib.parse.urlencode({"part": "snippet,liveStreamingDetails", "id": video_id, "key": api_key})
    try:
        with urllib.request.urlopen(f"{API}?{qs}", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        # 예외 메시지에 URL(=API 키)이 들어갈 수 있으므로 타입만 남긴다
        log.warning("YouTube API 호출 실패 (%s)", type(e).__name__)
        return None
    items = data.get("items") or []
    if not items:
        return {"live": False, "content": "missing", "viewers": None, "actual_end": None}
    it = items[0]
    content = it.get("snippet", {}).get("liveBroadcastContent", "none")
    lsd = it.get("liveStreamingDetails", {}) or {}
    viewers = lsd.get("concurrentViewers")
    return {
        "live": content == "live" and not lsd.get("actualEndTime"),
        "content": content,
        "viewers": int(viewers) if viewers else None,
        "actual_end": lsd.get("actualEndTime"),
    }
