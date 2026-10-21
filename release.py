# release.py — 단계 배포: 새 패치는 테스트 서버에 먼저, 다른 서버는 RELEASE_TS(정기점검)에 한꺼번에.
# 다음 패치 때: PREVIEW_ONLY(새 명령어) · LEGACY_ONLY(없어질 명령어) · RELEASE_TS 를 바꾼다.
# 지금: 2.9.1 — 테스트 서버 먼저, 10/21 14:15 KST 모든 서버 공개 (점검 14:15 ~ 14:20) · 2.8 은 2026-10-20 00:00 KST 공개 완료
#       (2.5 는 2026-10-02 00:00 · 2.6 은 2026-10-03 12:00 · 2.7 은 2026-10-12 17:00 KST 공개 완료)
import calendar  # RELEASE_TS = calendar.timegm((년, 월, 일, 시(UTC), 분, 0)) · None = 아직 테스트 서버에만
import time
from typing import Optional

TEST_GUILD = 1374213619793006704
RELEASE_TS: Optional[int] = calendar.timegm((2026, 10, 19, 15, 0, 0))   # 2.8 공개: 2026-10-20 00:00 KST
MAINTENANCE = (calendar.timegm((2026, 10, 21, 5, 15, 0)), calendar.timegm((2026, 10, 21, 5, 20, 0)))   # 정기점검 2.9.1 (10/21 14:15 ~ 14:20 KST) — 명령어 막음 (2.82b 는 10:12 ~ 10:15)
# (2.8 점검은 RELEASE_TS ± 600초였다. 공개를 다시 단계 배포할 땐 MAINTENANCE 를 RELEASE_TS 에 맞춘다)

PREVIEW_ONLY: set[str] = {"낚시", "낚시시세", "양궁", "던전", "오늘", "게임"}   # 공개 전엔 테스트 서버에만 보이는 명령어 (2.8 새 명령어는 만들 때마다 추가)
LEGACY_ONLY: set[str] = set()    # 공개 전까지만 다른 서버에 남는 명령어


def _now(now: Optional[float]) -> float:
    return time.time() if now is None else now


def released(now: Optional[float] = None) -> bool:
    return RELEASE_TS is not None and _now(now) >= RELEASE_TS


def preview(guild_id: Optional[int], now: Optional[float] = None) -> bool:
    """이 서버에 새 패치가 적용됐나 (테스트 서버는 언제나)."""
    return guild_id == TEST_GUILD or released(now)


# 2.9.1 — 서버마다 나눌 수 있는 것(낚시 · 토토 화면 · 튜토리얼)은 테스트 서버에 먼저, 나눌 수 없는 것
# (일반 선수 나이 · 은퇴, 토토 무효 처리 — 모든 서버가 같이 쓰는 데이터)은 RELEASE_291_TS 부터 켠다. None = 아직
RELEASE_291_TS: Optional[int] = calendar.timegm((2026, 10, 21, 5, 15, 0))   # 2026-10-21 14:15 KST (점검 시작과 함께)


def released_291(now: Optional[float] = None) -> bool:
    return RELEASE_291_TS is not None and _now(now) >= RELEASE_291_TS


def preview_291(guild_id: Optional[int], now: Optional[float] = None) -> bool:
    return guild_id == TEST_GUILD or released_291(now)


def maintenance(now: Optional[float] = None) -> bool:
    return MAINTENANCE is not None and MAINTENANCE[0] <= _now(now) < MAINTENANCE[1]


def hidden_commands(guild_id: Optional[int], now: Optional[float] = None) -> set[str]:
    """이 서버의 / 목록에서 뺄 명령어."""
    return LEGACY_ONLY if preview(guild_id, now) else PREVIEW_ONLY
