# services/dribble.py — ⚽ /드리블 (3.1.0): 키코의 솔로 드리블 — 수비수마다 왼쪽 · 오른쪽 (반반), 맞힐수록 배당이 오른다
import random

MIN_BET = 1_000
EDGE = 0.95                  # 어디서 멈춰도 기대값 -5% (배당 = 2^n × 0.95, 보기 좋게 반올림)
MULTS = (1.9, 3.8, 7.6, 15, 30, 60, 120)   # n명 제쳤을 때 받는 돈 = 베팅 × MULTS[n-1] (원금 포함)
STAGES = (                   # (이모지, 이름, 위치)
    ("🏃", "1번 수비수", "하프라인"),
    ("🏃", "2번 수비수", "센터서클 앞"),
    ("🏃", "3번 수비수", "상대 진영"),
    ("🧱", "수비형 미드필더", "페널티 박스 앞 30m"),
    ("🛡️", "센터백", "페널티 박스 앞"),
    ("🛡️", "주장 센터백", "페널티 박스 안"),
    ("🧤", "골키퍼", "골문 앞"),
)
SIDES = ("왼쪽", "오른쪽")
MAX_PAYOUT = 5_000_000_000   # 한 판 최대 순이익 50억원 (양궁과 같은 안전장치)


def step(rng=random) -> int:
    """수비수가 막는 쪽 (0 왼쪽 · 1 오른쪽). 다른 쪽을 고르면 제친다."""
    return rng.randrange(2)


def payout(amount: int, passed: int) -> int:
    """passed 명 제치고 멈췄을 때 받는 돈 (원금 포함, 순이익 최대 MAX_PAYOUT). 0명이면 원금 그대로."""
    if passed <= 0:
        return amount
    return min(int(amount * MULTS[passed - 1]), amount + MAX_PAYOUT)
