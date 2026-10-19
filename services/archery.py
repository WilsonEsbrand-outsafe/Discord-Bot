# services/archery.py — 🏹 양궁 (2.8): 착탄 계산 · 점수 · 배당 · 기대값 시뮬레이션
# 한 판 = 6발(2엔드 × 3발). 발마다 조준 게이지가 왼쪽 끝 → 오른쪽 끝으로 한 번 지나가고, 가운데에서 [쏘기!]를 누르면 정확하다.
# 판정은 서버가 게이지 시작 시각과 버튼을 누른 시각(디스코드가 받은 시각)으로만 계산한다 — 메시지 수정이 늦어도 상관없다.
# 착탄 = 타이밍 오차 + 손떨림(궁수) + 바람(적응력만큼 줄어듦, 화살마다 바뀜) + 운.
#   보통 타이밍(오차 평균 0~1 균등)일 때 흩어짐(분산)의 30% 가 타이밍, 70% 가 나머지(운 · 궁수 · 바람)가 되게 맞췄다.
# 배당: 합계 점수 배당 + X(정중앙) 개수 보너스. 타이밍이 완벽해도 기대값이 0 이하가 되게 LUCK 을 시뮬레이션으로 정했다.
import math
import random
from dataclasses import dataclass

ARROWS, ENDS = 6, 2
SWEEP = 3.0                  # 바늘이 왼쪽 끝 → 오른쪽 끝까지 가는 시간 (초) — 가운데 = 1.5초
SWEEP_FOCUS = 4.5            # 🧪 집중 물약: 바늘이 느려진다 (같은 손 오차가 더 작은 오차가 된다)
DISPLAY_LAG = 0.3            # 게이지 그림이 화면에 뜨기까지 평균 지연 (초) — 누른 시각에서 빼고 판정한다
LATE = 1.0                   # 오른쪽 끝을 지나고 이만큼 더 안 누르면 자동 발사 (오차 최대)
TIMING_K = 2.65              # 타이밍 오차 1(끝) = 과녁 2.65칸 빗나감
TREMOR_K = 0.5               # 손떨림 100 = 표준편차 0.5칸
WIND_K = 0.12                # 바람 1m/s = 0.12칸 (적응력 100 이면 70% 줄어듦)
WIND_ADAPT = 0.7
WIND_MAX = 6.0               # 화살마다 바람 0~6 m/s · 방향 랜덤
LUCK = 1.68                  # 운: 표준편차 (칸) — 완벽한 타이밍 기대값 약 -8.5% (test_archery 에서 검산)
BOW_TREMOR = 0.7             # 🏹 고급 활: 손떨림 -30%
WINDBREAK = 0.5              # 🧥 바람막이: 바람 영향 -50%
X_R = 0.5                    # X(정중앙) 반지름 · 10점 = 반지름 1 · 칸 하나 = 1

# 합계 점수 → 순이익 배수 (위에서부터 처음 맞는 줄)
SCORE_TABLE = ((57, 1.5), (53, 0.4), (49, 0.0), (46, -0.6), (0, -1.0))
X_BONUS = {3: 1, 4: 6, 5: 40, 6: 300}   # X 개수 보너스 (점수 배당에 더한다)
MAX_PAYOUT = 5_000_000_000   # 한 판 최대 순이익 50억원 — 베팅 상한이 없어서 X 5 · 6개(40 · 300배)가 큰 베팅에 걸릴 때만 막는 안전장치

# 궁수 풀: (국기, 이름, 손떨림 0~100(낮을수록 좋음), 바람 적응력 0~100, 소개) — 소개는 능력치에 맞춘다
ARCHERS = (
    ("🇰🇷", "김정확", 12, 70, "흔들림 없는 손, 국가대표 출신"),
    ("🇰🇷", "박바람", 30, 92, "바람을 읽는 눈은 세계 최고"),
    ("🇰🇷", "이신궁", 18, 55, "올림픽 금메달 2관왕"),
    ("🇺🇸", "브래디 엘리슨", 35, 60, "베테랑의 안정감"),
    ("🇮🇹", "마우로 네스폴리", 40, 45, "실내 대회 강자, 바람엔 약해요"),
    ("🇯🇵", "후루카와 다카", 28, 65, "꾸준함의 대명사"),
    ("🇳🇱", "스테버 바일러", 45, 80, "바닷바람 속에서 자랐어요"),
    ("🇲🇽", "알레한드라 발렌시아", 32, 50, "결승에 강한 승부사"),
    ("🇫🇷", "장 샤를 발라동", 55, 40, "기복이 심한 천재"),
    ("🇮🇳", "디피카 쿠마리", 38, 58, "세계랭킹 1위 출신"),
    ("🇬🇧", "로빈 후드", 60, 75, "숲에서는 백발백중… 이라는 소문"),
    ("🇹🇷", "메테 가조즈", 25, 48, "젊은 피, 손은 안정적"),
    ("🇨🇳", "리 자룬", 42, 62, "체력이 좋아 마지막 발에 강해요"),
    ("🇩🇪", "리자 운루", 48, 68, "날씨를 탓하지 않는 선수"),
    ("🇧🇷", "마르쿠스 다우메이다", 50, 35, "긴장하면 손이 떨려요"),
    ("🇦🇺", "테일러 워스", 65, 85, "강풍 속 우승 경험, 손은 조금 떨려요"),
)
MOODS = (("😆 최상", -10), ("🙂 좋음", -5), ("😐 보통", 0), ("😫 나쁨", 8))   # 오늘 컨디션 → 손떨림 가감


@dataclass
class Archer:
    flag: str
    name: str
    tremor: int
    adapt: int
    intro: str
    mood: str


def pick_archer(rng=random) -> Archer:
    flag, name, tremor, adapt, intro = rng.choice(ARCHERS)
    mood, d = rng.choice(MOODS)
    return Archer(flag, name, max(0, min(100, tremor + d)), adapt, intro, mood)


def roll_wind(rng=random) -> tuple[float, float]:
    """(세기 m/s, 방향 rad) — 화살마다 새로."""
    return round(rng.uniform(0, WIND_MAX), 1), rng.uniform(0, 2 * math.pi)


def timing_error(press_dt: float | None, sweep: float = SWEEP) -> float:
    """게이지가 시작되고 press_dt 초 뒤에 눌렀을 때 오차 0(가운데) ~ 1(끝 · 안 누름)."""
    if press_dt is None:
        return 1.0
    return min(1.0, abs(press_dt - sweep / 2) / (sweep / 2))


def needle(press_dt: float, sweep: float = SWEEP) -> float:
    """바늘 위치 -1(왼쪽 끝) ~ 1(오른쪽 끝)."""
    return max(-1.0, min(1.0, press_dt / (sweep / 2) - 1))


def shot(err: float, archer: Archer, wind: tuple[float, float], rng=random, bow: bool = False, windbreak: bool = False
         ) -> tuple[float, float]:
    """착탄 좌표 (과녁 칸 단위, 가운데 0,0)."""
    a = rng.uniform(0, 2 * math.pi)
    x, y = TIMING_K * err * math.cos(a), TIMING_K * err * math.sin(a)
    st = TREMOR_K * archer.tremor / 100 * (BOW_TREMOR if bow else 1)
    x, y = x + rng.gauss(0, st), y + rng.gauss(0, st)
    speed, ang = wind
    w = WIND_K * speed * (1 - WIND_ADAPT * archer.adapt / 100) * (WINDBREAK if windbreak else 1)
    x, y = x + w * math.cos(ang), y + w * math.sin(ang)
    return x + rng.gauss(0, LUCK), y + rng.gauss(0, LUCK)


def score(x: float, y: float) -> tuple[int, bool]:
    """(점수 0~10, X 여부)."""
    r = math.hypot(x, y)
    return (0 if r >= 10 else 10 - int(r)), r < X_R


def payout(total: int, xs: int) -> float:
    """순이익 배수 = 합계 점수 배당 + X 개수 보너스."""
    return next(m for th, m in SCORE_TABLE if total >= th) + X_BONUS.get(xs, 0)


def mult_label(m: float) -> str:
    if m > 0:
        return f"{m:g}배 수익"
    return "본전" if m == 0 else f"{-m:g}배 손실"


def simulate(n: int, err_fn, rng=None, bow=False, windbreak=False, archer: Archer | None = None) -> dict:
    """기대값 시뮬레이션 — err_fn(rng) = 발마다 타이밍 오차. {ev, mean, x, dist(배수→확률)}."""
    rng = rng or random.Random(2028)
    total_ev = total_score = total_x = 0.0
    dist: dict[float, int] = {}
    for _ in range(n):
        a = archer or pick_archer(rng)
        s = xs = 0
        for _ in range(ARROWS):
            sc, x = score(*shot(err_fn(rng), a, roll_wind(rng), rng, bow, windbreak))
            s, xs = s + sc, xs + x
        m = payout(s, xs)
        total_ev, total_score, total_x = total_ev + m, total_score + s, total_x + xs
        dist[m] = dist.get(m, 0) + 1
    return {"ev": total_ev / n, "mean": total_score / n, "x": total_x / n, "dist": {k: v / n for k, v in sorted(dist.items())}}


def timing_share(err_mean_sq: float = 1 / 3) -> float:
    """보통 타이밍(오차 제곱 평균 = 1/3, 즉 0~1 균등)일 때 흩어짐 중 타이밍 몫 (평균 궁수 · 평균 바람)."""
    t = TIMING_K ** 2 * err_mean_sq / 2
    tremor = (TREMOR_K * sum(a[2] for a in ARCHERS) / len(ARCHERS) / 100) ** 2
    adapt = sum(a[3] for a in ARCHERS) / len(ARCHERS) / 100
    wind = (WIND_K * (1 - WIND_ADAPT * adapt)) ** 2 * (WIND_MAX ** 2 / 3) / 2
    return t / (t + tremor + wind + LUCK ** 2)
