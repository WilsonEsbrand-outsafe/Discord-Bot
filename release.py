# release.py — 단계 배포: 새 패치는 테스트 서버에 먼저, 다른 서버는 RELEASE_TS(정기점검)에 한꺼번에.
# 다음 패치 때: PREVIEW_ONLY(새 명령어) · LEGACY_ONLY(없어질 명령어) · RELEASE_TS 를 바꾼다.
# 지금: 3.1.0 — 10/23 16:30 KST 모든 서버 공개 (점검 16:30 ~ 16:35) · 2.10.1 — 10/22 10:00 KST (점검 10:00 ~ 10:05) · 2.9.1 은 10/21 14:15 KST · 2.8 은 2026-10-20 00:00 KST 공개 완료
#       (2.5 는 2026-10-02 00:00 · 2.6 은 2026-10-03 12:00 · 2.7 은 2026-10-12 17:00 KST 공개 완료)
import calendar  # RELEASE_TS = calendar.timegm((년, 월, 일, 시(UTC), 분, 0)) · None = 아직 테스트 서버에만
import contextvars
import time
from typing import Optional

TEST_GUILD = 1374213619793006704
RELEASE_TS: Optional[int] = calendar.timegm((2026, 10, 19, 15, 0, 0))   # 2.8 공개: 2026-10-20 00:00 KST
MAINT_OPEN_END = calendar.timegm((2100, 1, 1, 0, 0, 0))   # 끝 시각을 정하지 않은 무기한 점검
MAINTENANCE = (calendar.timegm((2026, 10, 29, 6, 58, 0)), MAINT_OPEN_END)   # 10/29 15:58 KST ~ 무기한 — 3.6.0 오류 확인 긴급 점검 (끝나면 끝 시각을 넣는다)
# 3.6.0 정기점검은 10/29 13:25 ~ 13:30 KST (04:25 ~ 04:30 UTC) 였다
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


# 2.10.1 — 테스트 서버 먼저. 서버마다 나눌 수 있는 것(던전 · 상점 · 화면 · 구단/유망주/명문 메뉴 · 낚시/내베팅 공개)은
# preview_2101(guild), 모든 서버가 같이 쓰는 것(이적시장 24시간 · 스폰서 재계약 중단)은 RELEASE_2101_TS 부터. None = 아직
RELEASE_2101_TS: Optional[int] = calendar.timegm((2026, 10, 22, 1, 0, 0))   # 2026-10-22 10:00 KST (점검 시작과 함께)
LEGACY_2101: set[str] = {"시장", "매각", "구단명변경", "구단꾸미기", "구단삭제", "유망주생성", "유망주은퇴", "유망주삭제",
                         "영구결번", "스폰서", "스폰서계약"}   # 2.10.1 로 없어지는 명령어 — 공개 전까지 다른 서버엔 남는다


def released_2101(now: Optional[float] = None) -> bool:
    return RELEASE_2101_TS is not None and _now(now) >= RELEASE_2101_TS


def preview_2101(guild_id: Optional[int], now: Optional[float] = None) -> bool:
    return guild_id == TEST_GUILD or released_2101(now)


# 2.10.2 — 테스트 서버 먼저. preview_2102(guild): 베팅 금액 입력 뒤 응답 고정(UFC 베팅 실패 수정). None = 아직
RELEASE_2102_TS: Optional[int] = calendar.timegm((2026, 10, 22, 4, 10, 0))   # 2026-10-22 13:10 KST 무점검 공개 (버그 수정)


def released_2102(now: Optional[float] = None) -> bool:
    return RELEASE_2102_TS is not None and _now(now) >= RELEASE_2102_TS


def preview_2102(guild_id: Optional[int], now: Optional[float] = None) -> bool:
    return guild_id == TEST_GUILD or released_2102(now)


# 3.1.0 — 테스트 서버 먼저. preview_310(guild): 명령어 화면에 키코(축구 봇 캐릭터) 디자인 — 로고 파랑 · 키코 푸터 · 한마디. None = 아직
RELEASE_310_TS: Optional[int] = calendar.timegm((2026, 10, 23, 7, 30, 0))   # 2026-10-23 16:30 KST (점검 시작과 함께)


def released_310(now: Optional[float] = None) -> bool:
    return RELEASE_310_TS is not None and _now(now) >= RELEASE_310_TS


def preview_310(guild_id: Optional[int], now: Optional[float] = None) -> bool:
    return guild_id == TEST_GUILD or released_310(now)


PREVIEW_310: set[str] = {"드리블"}   # 3.1.0 새 명령어 — 공개 전엔 테스트 서버에만 보인다
PREVIEW_360: set[str] = {"2군", "유스경기"}     # 3.6.0 새 명령어 — 공개 전엔 테스트 서버에만 보인다


# 3.1.1 — 테스트 서버 먼저. preview_311(guild): /구단 화면에서 편성 · 감독 · 의료진 · 시설을 바로 바꾼다. None = 아직
RELEASE_311_TS: Optional[int] = calendar.timegm((2026, 10, 24, 21, 30, 0))   # 3.2.0 공개: 2026-10-25 06:30 KST (점검 시작과 함께)


def released_311(now: Optional[float] = None) -> bool:
    return RELEASE_311_TS is not None and _now(now) >= RELEASE_311_TS


def preview_311(guild_id: Optional[int], now: Optional[float] = None) -> bool:
    return guild_id == TEST_GUILD or released_311(now)


LEGACY_311: set[str] = {"감독", "시설", "의료진", "선발", "주장", "포메이션", "자동편성"}   # 3.1.1 /구단 으로 합친 명령어


# 3.2.1 — 테스트 서버 먼저. preview_321(guild): /토토 UFC 목록에 ESPN 대진표에 실제로 잡힌 대진(두 선수 모두)만 · 야구 · 농구. None = 아직
RELEASE_321_TS: Optional[int] = calendar.timegm((2026, 10, 26, 0, 20, 0))   # 3.3.0 공개: 2026-10-26 09:20 KST (점검 시작과 함께)


def released_321(now: Optional[float] = None) -> bool:
    return RELEASE_321_TS is not None and _now(now) >= RELEASE_321_TS


def preview_321(guild_id: Optional[int], now: Optional[float] = None) -> bool:
    return guild_id == TEST_GUILD or released_321(now)


# 3.4.0 — 테스트 서버 먼저. preview_340(guild): 남은 시간 5초마다 글자로 · 낚시(새 물고기 · 랜덤 시세 · 확률 · 낚싯대 내구도 · 장비 상향 ·
# 요리 확률) · F1 토토 순위 배당 완화 · 구단 경영 화면 · 유망주 강화. None = 아직
RELEASE_340_TS: Optional[int] = calendar.timegm((2026, 10, 26, 4, 20, 0))   # 3.4.0 공개: 2026-10-26 13:20 KST (점검 시작과 함께)


def released_340(now: Optional[float] = None) -> bool:
    return RELEASE_340_TS is not None and _now(now) >= RELEASE_340_TS


def preview_340(guild_id: Optional[int], now: Optional[float] = None) -> bool:
    return guild_id == TEST_GUILD or released_340(now)



# 3.4.1 — 테스트 서버 먼저. preview_341(guild): 페널티킥 1배(골 · 선방)를 줄이고 2배 · 5배를 늘림. None = 아직
RELEASE_341_TS: Optional[int] = calendar.timegm((2026, 10, 26, 7, 30, 0))   # 3.4.1 공개: 2026-10-26 16:30 KST (점검 시작과 함께)


def released_341(now: Optional[float] = None) -> bool:
    return RELEASE_341_TS is not None and _now(now) >= RELEASE_341_TS


def preview_341(guild_id: Optional[int], now: Optional[float] = None) -> bool:
    return guild_id == TEST_GUILD or released_341(now)


# 3.5.0 — 테스트 서버 먼저. preview_350(guild): 파산 탕감 0% 10% · 30~70% 75% · 100% 15%. None = 아직
RELEASE_350_TS: Optional[int] = calendar.timegm((2026, 10, 27, 3, 20, 0))   # 3.5.0 공개: 2026-10-27 12:20 KST (점검 시작과 함께)


def released_350(now: Optional[float] = None) -> bool:
    return RELEASE_350_TS is not None and _now(now) >= RELEASE_350_TS


def preview_350(guild_id: Optional[int], now: Optional[float] = None) -> bool:
    return guild_id == TEST_GUILD or released_350(now)


# 3.6.0 — 테스트 서버 먼저. preview_360(guild): 유망주 판수 제한 · 같은 상대 감소 없앰 · 1배 더 줄이기 · 스카우트 보상 ·
# 낚시 크기 랭킹 · 공식경기 스킵 · 주간 랭킹 · 2군 등. None = 아직
RELEASE_360_TS: Optional[int] = calendar.timegm((2026, 10, 29, 4, 25, 0))   # 3.6.0 공개: 2026-10-29 13:25 KST (점검 시작과 함께)


def released_360(now: Optional[float] = None) -> bool:
    return RELEASE_360_TS is not None and _now(now) >= RELEASE_360_TS


def preview_360(guild_id: Optional[int], now: Optional[float] = None) -> bool:
    return guild_id == TEST_GUILD or released_360(now)


# 지금 처리 중인 상호작용의 서버 — bot.py 가 / 명령어 · 버튼 · 메뉴마다 넣는다 (DB 안쪽처럼 서버를 넘겨받지 못하는 곳용).
# 백그라운드 작업에선 None.
GUILD: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("guild", default=None)


def ctx_360() -> bool:
    """3.6.0 이 공개된 서버에서 온 상호작용인가 (2군 · 1군 아마추어 최소 OVR — 구단 명단을 만드는 DB 안쪽에서 쓴다)."""
    return preview_360(GUILD.get())


def maintenance(now: Optional[float] = None) -> bool:
    return MAINTENANCE is not None and MAINTENANCE[0] <= _now(now) < MAINTENANCE[1]


def hidden_commands(guild_id: Optional[int], now: Optional[float] = None) -> set[str]:
    """이 서버의 / 목록에서 뺄 명령어."""
    out = set(LEGACY_ONLY if preview(guild_id, now) else PREVIEW_ONLY)
    out = out | LEGACY_2101 if preview_2101(guild_id, now) else out
    out = out | LEGACY_311 if preview_311(guild_id, now) else out
    out = out if preview_360(guild_id, now) else out | PREVIEW_360
    return out if preview_310(guild_id, now) else out | PREVIEW_310
