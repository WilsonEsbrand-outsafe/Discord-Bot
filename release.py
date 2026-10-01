# release.py — 단계 배포: 새 패치는 테스트 서버에 먼저, 다른 서버는 RELEASE_TS(정기점검)에 한꺼번에.
# 다음 패치 때: PREVIEW_ONLY(새 명령어) · LEGACY_ONLY(없어질 명령어) · RELEASE_TS 를 바꾼다.
import calendar
import time
from typing import Optional

TEST_GUILD = 1374213619793006704
RELEASE_TS = calendar.timegm((2026, 10, 1, 15, 0, 0))     # 2026-10-02 00:00 KST
MAINTENANCE = (RELEASE_TS - 300, RELEASE_TS + 300)         # 정기점검 23:55 ~ 00:05 KST — 명령어 막음

PREVIEW_ONLY = {"루키미션", "명문구단", "스쿼드", "시설", "구단꾸미기"}   # 공개 전엔 테스트 서버에만
LEGACY_ONLY = {"사용"}                                                  # 공개 전까지만 다른 서버에 남음


def _now(now: Optional[float]) -> float:
    return time.time() if now is None else now


def released(now: Optional[float] = None) -> bool:
    return _now(now) >= RELEASE_TS


def preview(guild_id: Optional[int], now: Optional[float] = None) -> bool:
    """이 서버에 새 패치가 적용됐나 (테스트 서버는 언제나)."""
    return guild_id == TEST_GUILD or released(now)


def maintenance(now: Optional[float] = None) -> bool:
    return MAINTENANCE[0] <= _now(now) < MAINTENANCE[1]


def hidden_commands(guild_id: Optional[int], now: Optional[float] = None) -> set[str]:
    """이 서버의 / 목록에서 뺄 명령어."""
    return LEGACY_ONLY if preview(guild_id, now) else PREVIEW_ONLY
