# release.py — 단계 배포: 새 패치는 테스트 서버에 먼저, 다른 서버는 RELEASE_TS(정기점검)에 한꺼번에.
# 다음 패치 때: PREVIEW_ONLY(새 명령어) · LEGACY_ONLY(없어질 명령어) · RELEASE_TS 를 바꾼다.
# 지금: 2.6 — 2026-10-03 12:00 KST 공개 (2.5 는 2026-10-02 00:00 KST 공개 완료)
import calendar  # RELEASE_TS = calendar.timegm((년, 월, 일, 시(UTC), 분, 0)) · None = 아직 테스트 서버에만
import time
from typing import Optional

TEST_GUILD = 1374213619793006704
RELEASE_TS: Optional[int] = calendar.timegm((2026, 10, 3, 3, 0, 0))   # 2026-10-03 12:00 KST
MAINTENANCE = (RELEASE_TS - 300, RELEASE_TS + 300) if RELEASE_TS else None   # 정기점검 11:55 ~ 12:05 KST — 명령어 막음

PREVIEW_ONLY: set[str] = set()   # 공개 전엔 테스트 서버에만 보이는 명령어
LEGACY_ONLY: set[str] = set()    # 공개 전까지만 다른 서버에 남는 명령어


def _now(now: Optional[float]) -> float:
    return time.time() if now is None else now


def released(now: Optional[float] = None) -> bool:
    return RELEASE_TS is not None and _now(now) >= RELEASE_TS


def preview(guild_id: Optional[int], now: Optional[float] = None) -> bool:
    """이 서버에 새 패치가 적용됐나 (테스트 서버는 언제나)."""
    return guild_id == TEST_GUILD or released(now)


def maintenance(now: Optional[float] = None) -> bool:
    return MAINTENANCE is not None and MAINTENANCE[0] <= _now(now) < MAINTENANCE[1]


def hidden_commands(guild_id: Optional[int], now: Optional[float] = None) -> set[str]:
    """이 서버의 / 목록에서 뺄 명령어."""
    return LEGACY_ONLY if preview(guild_id, now) else PREVIEW_ONLY
