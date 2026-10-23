# services/dribble.py — ⚽ /드리블 (3.1.0): 키코의 솔로 드리블 — 수비수마다 왼쪽 · 오른쪽, 제칠수록 성공 확률은 내려가고 배당은 오른다
import random

MIN_BET = 1_000
EDGE = 0.95                  # 어디서 멈춰도 기대값 약 -5% (배당 ≈ 0.95 / 누적 성공 확률, 보기 좋게 내림)
ODDS = (0.65, 0.60, 0.55, 0.50, 0.45, 0.40, 0.35)   # n번째 수비수를 제칠 확률 — 갈수록 어려워진다
MULTS = (1.45, 2.4, 4.4, 8.8, 19.5, 49, 140)        # n명 제쳤을 때 받는 돈 = 베팅 × MULTS[n-1] (원금 포함)
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


def tackled(passed: int, rng=random) -> bool:
    """passed 명 제친 뒤 다음 수비수에게 막히나 (막는 쪽을 읽혔나)."""
    return rng.random() >= ODDS[passed]


def payout(amount: int, passed: int) -> int:
    """passed 명 제치고 멈췄을 때 받는 돈 (원금 포함, 순이익 최대 MAX_PAYOUT). 0명이면 원금 그대로."""
    if passed <= 0:
        return amount
    return min(int(amount * MULTS[passed - 1]), amount + MAX_PAYOUT)
