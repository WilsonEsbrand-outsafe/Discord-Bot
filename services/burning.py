# services/burning.py — 3.8.4 수능 기념 버닝 이벤트 (11/21 규완님 '대수능 기념 2026-11-21 ~ 2026-11-30 00:00까지 버닝 이벤트,
# ex) 농사 버프 등등 효과 부여, FC 온라인 버닝 참고'). 기간 동안 아래 효과가 모두에게 (공개 전엔 테스트 서버 사람만).
# 효과 종류 · 세기는 내 기본값.
import calendar
from typing import Optional

import release

START = calendar.timegm((2026, 11, 20, 15, 0, 0))   # 2026-11-21 00:00 KST
END = calendar.timegm((2026, 11, 29, 15, 0, 0))     # 2026-11-30 00:00 KST
NAME = "수능 기념 버닝 이벤트"
BUFFS = {   # key → 배수
    "farm_grow": 2.0,      # 농작물 두 배 빨리 자람
    "farm_energy": 2.0,    # 농사 에너지 두 배 빨리 참
    "farm_sell": 1.5,      # 농사 판매 코인 1.5배
    "attend": 2.0,         # 출석 보상 두 배
}
TEXT = ("농작물이 두 배 빨리 자라요", "농사 에너지가 두 배 빨리 차요", "농사 판매 코인 1.5배", "출석 보상 두 배")


def on(now: float, uid: Optional[int] = None) -> bool:
    """지금 버닝 중인가 (uid 를 주면 그 사람이 효과를 받는가 — 공개 전엔 테스트 서버 사람만)."""
    if not START <= now < END:
        return False
    if release.released_384(now):
        return True
    from services import lobby   # 테스트 서버 멤버는 /로비 루프가 채운다
    return uid is not None and int(uid) in lobby.TESTERS


def mult(key: str, uid: Optional[int], now: float) -> float:
    return BUFFS.get(key, 1.0) if on(now, uid) else 1.0
