# services/spam.py — 3.1.1 명령어 연타 막기: 같은 명령어는 SAME_GAP 초 안에 다시 못 쓰고,
# 모든 명령어를 합쳐 BURST_WINDOW 초 안에 BURST_MAX 번을 넘기면 잠깐 막는다.
# 자체 도배 방지가 있는 한 방 게임(/페널티킥 · /야구 · /농구 · /ufc)은 그쪽 멘트를 쓰도록 뺀다.
import random
import time
from collections import deque
from typing import Optional

SAME_GAP = 1.5
BURST_WINDOW, BURST_MAX = 10.0, 6
OWN_GUARD = {"페널티킥", "야구", "농구", "ufc"}
LINES = ("🚦 잠깐만요! 명령어를 너무 빨리 입력했어요. 조금 천천히 다시 해 주세요.",
         "⏳ 숨 좀 돌리고 가요! 잠시 뒤에 다시 입력해 주세요.",
         "🙅 연타는 안 돼요! 잠시 뒤에 다시 해 주세요.")

_last: dict[tuple[int, str], float] = {}
_recent: dict[int, deque] = {}


def check(user_id: int, name: str, now: Optional[float] = None) -> bool:
    """이번 명령어를 받아도 되나. 막히면 False (막힌 입력은 기록하지 않는다)."""
    if name in OWN_GUARD:
        return True
    now = time.monotonic() if now is None else now
    if now - _last.get((user_id, name), -1e9) < SAME_GAP:
        return False
    q = _recent.setdefault(user_id, deque())
    while q and now - q[0] >= BURST_WINDOW:
        q.popleft()
    if len(q) >= BURST_MAX:
        return False
    q.append(now)
    _last[(user_id, name)] = now
    if len(_last) > 50_000:   # 오래된 기록 정리
        for k in [k for k, t in _last.items() if now - t > 60]:
            del _last[k]
    return True


def line() -> str:
    return random.choice(LINES)
