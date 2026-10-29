# services/fishing.py — 🎣 낚시 (2.8): 물고기 40종 (2.82: 30 → 40) · 입질 대기 · 싯가 · 거래 한도 · 먹이기 · 요리 · 수족관 · 도감 · 주간 최대어
# 찌는 한 번에 하나. 던지면 10초~10분 뒤 입질 → 5분 안에 [잡기!]를 못 누르면 도망. 상태는 DB(economy.sqlite3)에 남아
# 봇이 재시작해도 이어진다. 오래 기다린 입질일수록 희귀한 물고기가 걸린다.
# 돈이 풀리는 곳은 판매뿐이다 — 보내기(유저 간 트레이드)만 하루 20포인트 한도 (2.82b: 판매는 한도 없음) (흔함 1 · 보통 1 · 희귀 2 · 영웅 3 · 전설 5).
import math
import random
import sqlite3
import time
from typing import Optional

from services.economy_db import EconomyDB, give_item, _kst_day

# ───────────── 물고기 ─────────────
GRADES = ("흔함", "보통", "희귀", "영웅", "전설")
GRADE_EMOJI = ("⚪", "🟢", "🔵", "🟣", "🟡")
GRADE_POINTS = (1, 1, 2, 3, 5)            # 보내기 때 깎이는 거래 포인트
GRADE_XP = (2, 4, 10, 25, 60)             # 낚시 경험치 (+ 크기 보너스) — 2.9.1: 5 · 10 · 25 · 60 · 150 에서 약 40%로
GRADE_XP_OLD = (5, 10, 25, 60, 150)       # 2.9.1 공개 전 서버 (release.preview_291)
GRADE_SIGMA = (0.08, 0.12, 0.18, 0.25, 0.35)   # 싯가 하루 변동 폭 (±)
# 키 → (이모지, 이름, 등급, 평균 크기 cm, 기준가 원). 기준가 = 평균 크기 · 싯가 1배 · 신선할 때 값.
FISH = {   # (이모지, 이름, 등급, 평균 cm, 기준가) — 2.82: 기준가 등급별 ×1 · ×2 · ×4 · ×7 · ×12
    "crucian": ("🐟", "붕어", 0, 25, 5_000), "minnow": ("🐟", "피라미", 0, 12, 3_000),
    "goby": ("🐟", "망둥어", 0, 15, 3_500), "gizzard": ("🐟", "전어", 0, 20, 4_500),
    "mackerel": ("🐟", "고등어", 0, 35, 6_000), "saury": ("🐟", "꽁치", 0, 30, 4_000),
    "sardine": ("🐟", "정어리", 0, 18, 3_000), "anchovy": ("🐟", "멸치", 0, 10, 2_500),
    "smelt": ("🐟", "빙어", 0, 12, 3_500), "rockfish": ("🐟", "볼락", 0, 22, 7_000),
    "jacopever": ("🐠", "우럭", 1, 35, 28_000), "flounder": ("🐠", "광어", 1, 50, 36_000),
    "hairtail": ("🐠", "갈치", 1, 90, 32_000), "mullet": ("🐠", "숭어", 1, 55, 24_000),
    "mandarin": ("🐠", "쏘가리", 1, 35, 40_000), "catfish": ("🐠", "메기", 1, 60, 22_000),
    "sole": ("🐠", "가자미", 1, 30, 26_000), "squid": ("🦑", "오징어", 1, 30, 30_000),
    "seabream": ("🐡", "참돔", 2, 55, 180_000), "seabass": ("🐡", "농어", 2, 70, 160_000),
    "yellowtail": ("🐡", "방어", 2, 80, 220_000), "salmon": ("🐡", "연어", 2, 75, 200_000),
    "croaker": ("🐡", "민어", 2, 85, 240_000), "eel": ("🐍", "장어", 2, 70, 192_000),
    "grouper": ("🦈", "다금바리", 3, 80, 1_050_000), "tuna": ("🦈", "참다랑어", 3, 200, 1_400_000),
    "swordfish": ("🦈", "황새치", 3, 250, 1_190_000), "sturgeon": ("🦈", "철갑상어", 3, 150, 1_260_000),
    "goldcarp": ("🐉", "황금 잉어", 4, 90, 8_400_000), "coelacanth": ("🦕", "실러캔스", 4, 180, 10_800_000),
    # 2.82 새 물고기 10종
    "herring": ("🐟", "청어", 0, 28, 4_500), "loach": ("🐟", "미꾸라지", 0, 14, 3_000),
    "spanish": ("🐟", "삼치", 0, 60, 7_500),
    "octopus": ("🐙", "문어", 1, 60, 44_000), "pufferfish": ("🐡", "복어", 1, 35, 38_000),
    "trout": ("🐠", "송어", 1, 45, 28_000),
    "blowfish": ("🐡", "자주복", 2, 50, 260_000), "lobster": ("🦞", "랍스터", 2, 40, 280_000),
    "marlin": ("🦈", "청새치", 3, 280, 1_610_000),
    "oarfish": ("🐉", "산갈치", 4, 300, 14_400_000),
    # 3.1.0 새 물고기 15종 (공개 전엔 테스트 서버에서만 잡힘)
    "filefish": ("🐟", "쥐치", 0, 20, 4_000), "sandfish": ("🐟", "도루묵", 0, 20, 3_500),
    "horsemackerel": ("🐟", "전갱이", 0, 30, 5_000), "sandlance": ("🐟", "양미리", 0, 15, 2_500),
    "scorpionfish": ("🐟", "쏨뱅이", 0, 22, 6_500), "greenling": ("🐟", "노래미", 0, 30, 6_000),
    "cod": ("🐠", "대구", 1, 70, 30_000), "monkfish": ("🐡", "아귀", 1, 60, 34_000),
    "crab": ("🦀", "꽃게", 1, 20, 36_000), "webfoot": ("🐙", "주꾸미", 1, 15, 24_000),
    "abalone": ("🐚", "전복", 1, 10, 42_000),
    "knifejaw": ("🐡", "돌돔", 2, 50, 230_000), "kingcrab": ("🦀", "킹크랩", 2, 60, 270_000),
    "sunfish": ("🐡", "개복치", 3, 250, 1_330_000),
    "whaleshark": ("🐋", "고래상어", 4, 900, 12_000_000),
    # 3.4.0 새 물고기 45종 (공개 전엔 테스트 서버에서만 잡힘) — 100종
    "bluegill": ("🐟", "블루길", 0, 18, 3_000), "bass": ("🐟", "배스", 0, 35, 6_500),
    "dace": ("🐟", "황어", 0, 35, 5_000), "chub": ("🐟", "끄리", 0, 30, 4_000),
    "sillago": ("🐟", "보리멸", 0, 20, 4_500), "shrimp": ("🦐", "새우", 0, 8, 3_000),
    "clam": ("🐚", "바지락", 0, 4, 2_500), "pollack": ("🐟", "명태", 0, 45, 5_500),
    "halfbeak": ("🐟", "학꽁치", 0, 25, 4_000), "sweetfish": ("🐟", "은어", 0, 18, 7_000),
    "skate": ("🐠", "홍어", 1, 70, 40_000), "conger": ("🐍", "붕장어", 1, 60, 30_000),
    "blackbream": ("🐠", "감성돔", 1, 40, 42_000), "tilefish": ("🐠", "옥돔", 1, 40, 38_000),
    "cuttlefish": ("🦑", "갑오징어", 1, 25, 32_000), "urchin": ("🐚", "성게", 1, 8, 36_000),
    "seacucumber": ("🥒", "해삼", 1, 20, 34_000), "yellowcroaker": ("🐠", "참조기", 1, 25, 28_000),
    "snakehead": ("🐍", "가물치", 1, 70, 32_000), "carp": ("🐟", "잉어", 1, 60, 24_000),
    "amberjack": ("🐡", "잿방어", 2, 100, 260_000), "redgrouper": ("🐡", "붉바리", 2, 45, 280_000),
    "spotbass": ("🐡", "점농어", 2, 65, 170_000), "longtooth": ("🐡", "능성어", 2, 60, 240_000),
    "kingsalmon": ("🐡", "왕연어", 2, 90, 250_000), "giantoctopus": ("🐙", "대왕문어", 2, 120, 230_000),
    "snowcrab": ("🦀", "대게", 2, 50, 260_000), "mahimahi": ("🐡", "만새기", 2, 100, 180_000),
    "spinylobster": ("🦞", "닭새우", 2, 30, 270_000),
    "bigeye": ("🦈", "눈다랑어", 3, 180, 1_300_000), "sailfish": ("🦈", "돛새치", 3, 300, 1_500_000),
    "hammerhead": ("🦈", "귀상어", 3, 350, 1_450_000), "manta": ("🦈", "쥐가오리", 3, 400, 1_350_000),
    "giantgrouper": ("🦈", "자이언트 그루퍼", 3, 200, 1_550_000), "arapaima": ("🐊", "피라루쿠", 3, 250, 1_400_000),
    "napoleon": ("🐠", "나폴레옹피시", 3, 150, 1_600_000), "trevally": ("🦈", "자이언트 트레발리", 3, 130, 1_250_000),
    "giantsquid": ("🦑", "대왕오징어", 4, 1200, 11_000_000), "goldtuna": ("🐉", "황금 참다랑어", 4, 250, 13_000_000),
    "dragonfish": ("🐉", "용고기", 4, 200, 14_000_000), "phoenixkoi": ("🐉", "불사조 비단잉어", 4, 100, 15_000_000),
    "moonfish": ("🌙", "달빛 개복치", 4, 300, 10_000_000), "rainbowfish": ("🌈", "무지개 물고기", 4, 60, 9_000_000),
    "leviathan": ("🐉", "리바이어던", 4, 2000, 18_000_000), "megalodon": ("🦈", "메갈로돈", 4, 1500, 20_000_000),
}
NEW_310 = {"filefish", "sandfish", "horsemackerel", "sandlance", "scorpionfish", "greenling", "cod", "monkfish", "crab",
           "webfoot", "abalone", "knifejaw", "kingcrab", "sunfish", "whaleshark"}
NEW_340 = set(list(FISH)[55:])
BY_GRADE_340 = [[k for k, f in FISH.items() if f[2] == g] for g in range(5)]          # 3.4.0 공개 서버: 100종
BY_GRADE_310 = [[k for k in b if k not in NEW_340] for b in BY_GRADE_340]             # 3.1.0 공개 서버: 55종
BY_GRADE = [[k for k in b if k not in NEW_310] for b in BY_GRADE_310]                # 3.1.0 전 서버: 40종


# 3.6.0 도감 보상: 등급 하나를 다 모으면 (돈, 🤖 자동 낚시) 한 번 · 5 = 100종 전체 완성
DEX_REWARDS = ((3_000_000, 0), (5_000_000, 0), (10_000_000, 0), (30_000_000, 1), (100_000_000, 2), (200_000_000, 3))


def by_grade(new: bool = False, v340: bool = False) -> list[list[str]]:
    """잡히는 · 시세 · 도감에 나오는 물고기 (등급별). new = 3.1.0 공개 서버 · v340 = 3.4.0 공개 서버."""
    return BY_GRADE_340 if v340 else BY_GRADE_310 if new else BY_GRADE

# 걸린 시간(입질까지 기다린 시간)별 등급 확률 % — 10초와 10분 사이는 직선으로 잇는다
BITE_MIN, BITE_MAX = 10, 600
ODDS_FAST = (62, 28, 8, 1.7, 0.3)
ODDS_SLOW = (24, 30, 26, 15, 5)
ODDS_FAST_310 = (66, 27, 6, 0.9, 0.1)   # 3.1.0: 희귀 · 영웅 · 전설 확률 하향 (평균 수입 약 절반)
ODDS_SLOW_310 = (34, 34, 22, 8, 2)
ODDS_FAST_340 = (65.5, 27, 6.25, 1.1, 0.15)   # 3.4.0: 희귀 · 영웅 · 전설 확률 소폭 상향
ODDS_SLOW_340 = (33, 34, 22.2, 8.5, 2.3)
ODDS_FAST_350 = (63, 27, 8, 1.7, 0.3)   # 3.5.0: 희귀 · 영웅 · 전설 확률 상향
ODDS_SLOW_350 = (28, 32, 25, 11, 4)
CATCH_WINDOW = 300            # 입질 뒤 이 시간(초) 안에 [잡기!] — 낚싯대로 늘어난다
JUNK_PROB = 0.12              # 물고기 대신 잡템
JUNK = {   # 아이템 → 비중 (old_boot = 낡은 장화, 아무것도 아님)
    "old_boot": 20, "bait": 30, "spice": 22, "skip_piece": 15, "bait_gold": 8, "fish_charm": 3, "box": 2,
}
BAG_MAX = 100                 # 어망(보관) 한도 — 3.1.0 공개 서버는 낚시 레벨별 (bag_max)
BAG_310 = (50, 5)             # 3.1.0: Lv.1 50칸 · 레벨마다 +5 (Lv.11 100 · Lv.30 195)
DAY_FISH_MULT = 2.0           # 3.1.0 오늘의 물고기: 그 종은 하루 동안 판매가 2배
TRADE_DAILY = 20              # 하루 보내기 포인트
RESET_282B_TS = 1792544400    # 2026-10-21 10:00 KST — 2.82b 때 이날 쓴 포인트를 한 번 초기화
CHARM_BONUS = 5               # 🍀 낚시 부적 1장 = 오늘 한도 +5
FRESH_DROP, FRESH_DROP_ICE, FRESH_MIN = 0.10, 0.05, 0.5   # 하루 신선도 -10% (아이스박스 -5%) · 최저 50%
MARKET_MIN, MARKET_MAX, MARKET_REVERT = 0.5, 2.0, 0.25    # 싯가 배수 0.5~2.0 · 매일 평균(1)으로 25% 회귀
SUPPLY_HIT, SUPPLY_HEAL, SUPPLY_MIN = 0.02, 0.20, 0.3     # 판매 한 마리 -2% · 매일 부족분 20% 회복
AQUARIUM_MAX = 10
LEVEL_MAX = 30
LEVEL_RARE_STEP = 0.006       # 레벨 1 오를 때마다 '기다린 시간' 효과 +0.6% (Lv.30 = +17.4%)

# 낚싯대 (가방에 있으면 가장 좋은 것이 자동 적용): 아이템 → (크기 보너스, 입질 뒤 기다려 주는 시간 초)
RODS = {"rod_carbon": (0.05, 420), "rod_pro": (0.10, 600), "rod_legend": (0.15, 900)}
BAIT_JUNK = 0.06              # 🪱 일반 미끼: 이번 한 번 잡템 확률 12% → 6%
BAIT_GOLD_T = 0.15            # ✨ 고급 미끼: 이번 한 번 '기다린 시간' 효과 +15%

# 요리 (24시간 효과): 키 → (이모지, 이름, 재료 [(등급, 마리)], 양념 수, 효과 설명)
RECIPES = {
    "grill": ("🍢", "생선구이", [(0, 2)], 0, "낚시 경험치 +30%"),
    "sashimi": ("🍣", "모둠회", [(1, 2)], 1, "스카우트 · 훈련 · 직관 돈 보상 +10%"),
    "stew": ("🍲", "매운탕", [(2, 1)], 1, "희귀한 물고기가 더 잘 걸려요 (기다린 시간 효과 +10%)"),
    "eel_rice": ("🍱", "보양 정식", [(3, 1)], 2, "던전 파티 체력 +20"),
    "feast": ("👑", "용궁 만찬", [(4, 1)], 3, "위 네 가지 효과 모두"),
}
# 3.1.1 요리 (release.preview_311 서버): 물고기를 직접 고르고 · 재료가 두 배 · 확률로 성공 (실패하면 재료는 타서 사라진다)
RECIPES_311_NEEDS = {"grill": [(0, 4)], "sashimi": [(1, 4)], "stew": [(2, 2)], "eel_rice": [(3, 2)], "feast": [(4, 2)]}
COOK_CHANCE_311 = {"grill": 0.85, "sashimi": 0.80, "stew": 0.75, "eel_rice": 0.70, "feast": 0.65}
# 3.1.1 낚싯대가 좋을수록 희귀한 물고기가 아주 조금 더 ('기다린 시간' 효과 +)
ROD_RARE_311 = {"rod_carbon": 0.02, "rod_pro": 0.04, "rod_legend": 0.06}
# 3.4.0 (release.preview_340 서버): 요리 성공 확률 ↑ · 낚싯대 · 미끼 효과 ↑ · 낚싯대 내구도
COOK_CHANCE_340 = {"grill": 0.95, "sashimi": 0.92, "stew": 0.90, "eel_rice": 0.87, "feast": 0.85}
RODS_340 = {"rod_carbon": (0.08, 480), "rod_pro": (0.15, 720), "rod_legend": (0.25, 1080)}
ROD_RARE_340 = {"rod_carbon": 0.04, "rod_pro": 0.08, "rod_legend": 0.12}
BAIT_JUNK_340, BAIT_GOLD_T_340 = 0.03, 0.25
ROD_DUR = {"rod_carbon": 120, "rod_pro": 300, "rod_legend": 800}   # 내구도 — 한 번 낚을 때마다 1 (자동 낚시는 안 닳는다)
# 대어(영웅 · 전설)가 걸리면 가끔 줄이 끊어져 도망 + 낚싯대에 금이 간다 (내구도 -10). 좋은 낚싯대일수록 덜 끊긴다
LINE_SNAP = {None: 0.06, "rod_carbon": 0.04, "rod_pro": 0.02, "rod_legend": 0.01}
SNAP_DAMAGE = 10
COOK_HOURS = 24
FEAST_GIVES = ("grill", "sashimi", "stew", "eel_rice")
# 먹이기: 유망주 성장 경험치 (하루 유망주 · 선수마다 한 마리) — 경기 출전 한 번 = 10
FEED_PROSPECT_XP = (2, 3, 5, 8, 15)
FEED_FULL_TEAM = 11           # 오늘 선발 11명 모두 먹이면 오늘 경기 전력 +1 (🍽️ 든든한 식단)
FEED_TEAM_BONUS = 1
# 3.1.0 🤖 자동 낚시: 상점에선 안 팔고 [잡기!] 때 0.1% 로 얻는다. 사용 후 24시간 동안 5~15분마다 한 마리
# (입질이 가장 빠른 찌와 같은 확률 · 어망이 차면 쉰다)
AUTO_HOURS, AUTO_EVERY, AUTO_DROP = 24, (300, 900), 0.001
SKIP_PIECES = 5               # 스킵권 조각 5개 → 스킵권 1장 (스카우트 · 훈련 · 직관 중 하나)


def bag_max(level: int, new: bool = False) -> int:
    """어망 칸 수. new = 3.1.0 공개 서버 (낚시 레벨별)."""
    return BAG_310[0] + BAG_310[1] * (max(1, int(level)) - 1) if new else BAG_MAX


def day_fish(now_ts: int, v340: bool = False) -> str:
    """오늘의 물고기 (3.1.0) — KST 날짜로 고정. 비싼 물고기일수록 덜 뽑힌다 (가중치 1/√기준가). v340 = 3.4.0 100종 중에서."""
    rng = random.Random(f"fish-day-{_kst_day(now_ts)}")
    keys = list(FISH) if v340 else [k for k in FISH if k not in NEW_340]
    return rng.choices(keys, weights=[1 / math.sqrt(FISH[k][4]) for k in keys])[0]


def level_need(level: int) -> int:
    """Lv.N → N+1 경험치. 2.10.1: 80 → 120 (레벨 간격 1.5배, 흔한 물고기 기준 Lv.2 까지 약 60마리)."""
    return 120 * level


def add_xp(level: int, xp: int, gain: int) -> tuple[int, int, int]:
    up = 0
    xp += gain
    while level < LEVEL_MAX and xp >= level_need(level):
        xp -= level_need(level)
        level += 1
        up += 1
    return level, (0 if level >= LEVEL_MAX else xp), up


def grade_odds(wait: float, bonus: float = 0.0, new: bool = False, v340: bool = False, v350: bool = False) -> list[float]:
    """기다린 시간(초) + 효과 보너스 → 등급 확률 (합 1). 10초 → 10분을 0 → 1 로 보고 직선으로 잇는다. new = 3.1.0 확률 · v340 = 3.4.0 · v350 = 3.5.0."""
    t = (min(max(wait, BITE_MIN), BITE_MAX) - BITE_MIN) / (BITE_MAX - BITE_MIN) + bonus
    fast, slow = (ODDS_FAST_350, ODDS_SLOW_350) if v350 else (ODDS_FAST_340, ODDS_SLOW_340) if v340 else (ODDS_FAST_310, ODDS_SLOW_310) if new else (ODDS_FAST, ODDS_SLOW)
    w = [max(0.0, a + (b - a) * t) for a, b in zip(fast, slow)]
    s = sum(w)
    return [x / s for x in w]


def roll_size(key: str, rng=random, rod_bonus: float = 0.0) -> float:
    """크기 cm — 평균 ±15% 정규분포 (평균의 55%~170%) × 낚싯대 보너스."""
    avg = FISH[key][3]
    return round(min(avg * 1.7, max(avg * 0.55, rng.gauss(avg, avg * 0.15))) * (1 + rod_bonus), 1)


def size_mult(key: str, size: float) -> float:
    return size / FISH[key][3]


def catch_xp(key: str, size: float, new: bool = True) -> int:
    """등급 경험치 + 크기 보너스 (평균보다 큰 만큼, 최대 +50%). new=False 면 2.9.1 전 경험치."""
    base = (GRADE_XP if new else GRADE_XP_OLD)[FISH[key][2]]
    return base + round(base * max(0.0, min(0.5, size_mult(key, size) - 1)))


def freshness(caught_ts: int, now_ts: int, icebox: bool = False) -> float:
    days = max(0, _kst_day(now_ts) - _kst_day(caught_ts))
    return max(FRESH_MIN, 1 - (FRESH_DROP_ICE if icebox else FRESH_DROP) * days)


def fish_price(key: str, size: float, mult: float, supply: float, fresh: float) -> int:
    """판매가 = 기준가 × 크기 × 싯가 × 서버 판매량 × 신선도."""
    return max(10, int(round(FISH[key][4] * size_mult(key, size) * mult * supply * fresh, -1)))


def market_step(mult: float, supply: float, grade: int, rng) -> tuple[float, float]:
    """하루 지남: 싯가는 평균으로 25% 돌아가고 등급별 폭만큼 흔들린다 · 판매량 부족분은 20% 회복."""
    m = 1 + (mult - 1) * (1 - MARKET_REVERT)
    m *= 1 + rng.uniform(-GRADE_SIGMA[grade], GRADE_SIGMA[grade])
    return round(min(MARKET_MAX, max(MARKET_MIN, m)), 4), round(supply + (1 - supply) * SUPPLY_HEAL, 4)


def week_key(ts: int) -> int:
    """KST 월요일 00시 기준 주 번호."""
    return (_kst_day(ts) + 3) // 7   # 1970-01-01 은 목요일


class FishingDB(EconomyDB):
    """낚시 데이터 — 지갑 · 가방과 같은 DB 라서 판매 · 지급이 한 트랜잭션으로 끝난다."""

    def _init_db(self):
        super()._init_db()
        con = self._connect()
        try:
            con.execute("CREATE TABLE IF NOT EXISTS fish_state (user_id INTEGER PRIMARY KEY, cast_ts INTEGER, "
                        "bite_ts INTEGER, window INTEGER, bait TEXT, channel_id INTEGER, message_id INTEGER, "
                        "level INTEGER NOT NULL DEFAULT 1, xp INTEGER NOT NULL DEFAULT 0, caught INTEGER NOT NULL DEFAULT 0)")
            con.execute("CREATE TABLE IF NOT EXISTS fish_bag (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, "
                        "species TEXT, size REAL, caught_ts INTEGER, displayed INTEGER NOT NULL DEFAULT 0)")
            con.execute("CREATE INDEX IF NOT EXISTS fish_bag_user ON fish_bag(user_id)")
            con.execute("CREATE TABLE IF NOT EXISTS fish_dex_reward (user_id INTEGER, grade INTEGER, PRIMARY KEY(user_id, grade))")
            con.execute("CREATE TABLE IF NOT EXISTS fish_dex (user_id INTEGER, species TEXT, cnt INTEGER, best REAL, "
                        "PRIMARY KEY(user_id, species))")
            con.execute("CREATE TABLE IF NOT EXISTS fish_log (user_id INTEGER, species TEXT, size REAL, ts INTEGER, week INTEGER)")
            con.execute("CREATE INDEX IF NOT EXISTS fish_log_week ON fish_log(week)")
            con.execute("CREATE TABLE IF NOT EXISTS fish_market (species TEXT PRIMARY KEY, day_key INTEGER, mult REAL, "
                        "supply REAL, prev REAL)")
            con.execute("CREATE TABLE IF NOT EXISTS fish_trade (user_id INTEGER, day_key INTEGER, used INTEGER, "
                        "bonus INTEGER, PRIMARY KEY(user_id, day_key))")
            con.execute("CREATE TABLE IF NOT EXISTS fish_fed (user_id INTEGER, target TEXT, day_key INTEGER, "
                        "PRIMARY KEY(user_id, target, day_key))")
            con.execute("CREATE TABLE IF NOT EXISTS fish_buffs (user_id INTEGER, kind TEXT, until INTEGER, "
                        "PRIMARY KEY(user_id, kind))")
            # 2.82b 한 번만: 판매로 쓴 10/21 거래 포인트 초기화 (부적 보너스는 그대로)
            con.execute("CREATE TABLE IF NOT EXISTS fish_auto (user_id INTEGER PRIMARY KEY, until INTEGER, next_ts INTEGER)")
            con.execute("CREATE TABLE IF NOT EXISTS fish_migrations (key TEXT PRIMARY KEY)")
            con.execute("CREATE TABLE IF NOT EXISTS fish_rod (user_id INTEGER, rod TEXT, dur INTEGER, PRIMARY KEY(user_id, rod))")
            if con.execute("INSERT OR IGNORE INTO fish_migrations VALUES('reset-trade-2.82b')").rowcount:
                con.execute("UPDATE fish_trade SET used=0 WHERE day_key=?", (_kst_day(RESET_282B_TS),))
            con.commit()
        finally:
            con.close()

    # ───────────── 공용 ─────────────
    @staticmethod
    def _state(con, uid: int) -> dict:
        con.execute("INSERT OR IGNORE INTO fish_state(user_id) VALUES(?)", (uid,))
        r = con.execute("SELECT cast_ts, bite_ts, window, bait, channel_id, message_id, level, xp, caught "
                        "FROM fish_state WHERE user_id=?", (uid,)).fetchone()
        return dict(zip(("cast_ts", "bite_ts", "window", "bait", "channel_id", "message_id", "level", "xp", "caught"), r))

    @staticmethod
    def _qty(con, uid: int, item: str) -> int:
        r = con.execute("SELECT qty FROM inventory WHERE user_id=? AND item=?", (uid, item)).fetchone()
        return int(r[0]) if r else 0

    @classmethod
    def _rod(cls, con, uid: int) -> Optional[str]:
        have = [k for k in RODS if cls._qty(con, uid, k) > 0]
        return max(have, key=lambda k: RODS[k][0]) if have else None

    @staticmethod
    def _dur(con, uid: int, rod: str) -> int:
        """3.4.0 낚싯대 남은 내구도 (기록이 없으면 새것)."""
        r = con.execute("SELECT dur FROM fish_rod WHERE user_id=? AND rod=?", (uid, rod)).fetchone()
        return int(r[0]) if r else ROD_DUR[rod]

    @classmethod
    def _wear(cls, con, uid: int, rod: Optional[str], dmg: int) -> Optional[dict]:
        """3.4.0 낚싯대가 닳는다 — 0 이 되면 부러져 가방에서 하나 사라진다 (다음 것은 새것). → {rod, dur, max, broke}."""
        if not rod or dmg <= 0:
            return None
        dur = cls._dur(con, uid, rod) - dmg
        if dur > 0:
            con.execute("INSERT OR REPLACE INTO fish_rod VALUES(?,?,?)", (uid, rod, dur))
            return {"rod": rod, "dur": dur, "max": ROD_DUR[rod], "broke": False}
        con.execute("UPDATE inventory SET qty = qty - 1 WHERE user_id=? AND item=?", (uid, rod))
        con.execute("DELETE FROM inventory WHERE user_id=? AND item=? AND qty<=0", (uid, rod))
        con.execute("DELETE FROM fish_rod WHERE user_id=? AND rod=?", (uid, rod))
        return {"rod": rod, "dur": 0, "max": ROD_DUR[rod], "broke": True}

    @staticmethod
    def _buffs(con, uid: int, now_ts: int) -> set[str]:
        kinds = {k for k, u in con.execute("SELECT kind, until FROM fish_buffs WHERE user_id=?", (uid,)) if u > now_ts}
        if "feast" in kinds:
            kinds |= set(FEAST_GIVES)
        return kinds

    @staticmethod
    def _market(con, now_ts: int, rng=None) -> dict[str, dict]:
        """오늘 싯가 {종: {mult, supply, prev}} — 날이 바뀌었으면 그날까지 하루씩 굴린다 (날짜별 시드라 언제 불러도 같다)."""
        today = _kst_day(now_ts)
        out = {}
        for key, (_, _, grade, _, _) in FISH.items():
            row = con.execute("SELECT day_key, mult, supply, prev FROM fish_market WHERE species=?", (key,)).fetchone()
            if row is None:
                day, mult, supply, prev = today, 1.0, 1.0, 1.0
                con.execute("INSERT INTO fish_market VALUES(?,?,?,?,?)", (key, day, mult, supply, prev))
            else:
                day, mult, supply, prev = row
                if day < today:
                    for d in range(max(day, today - 30) + 1, today + 1):   # 오래 비었으면 최근 30일만
                        prev = mult
                        mult, supply = market_step(mult, supply, grade, rng or random.Random(f"fish-{key}-{d}"))
                    con.execute("UPDATE fish_market SET day_key=?, mult=?, supply=?, prev=? WHERE species=?",
                                (today, mult, supply, prev, key))
            out[key] = {"mult": mult, "supply": supply, "prev": prev}
        return out

    @staticmethod
    def _trade(con, uid: int, now_ts: int) -> tuple[int, int]:
        """(오늘 쓴 포인트, 오늘 한도)."""
        r = con.execute("SELECT used, bonus FROM fish_trade WHERE user_id=? AND day_key=?", (uid, _kst_day(now_ts))).fetchone()
        return (int(r[0]), TRADE_DAILY + int(r[1])) if r else (0, TRADE_DAILY)

    @staticmethod
    def _spend_points(con, uid: int, now_ts: int, pts: int) -> None:
        con.execute("INSERT INTO fish_trade(user_id, day_key, used, bonus) VALUES(?,?,?,0) "
                    "ON CONFLICT(user_id, day_key) DO UPDATE SET used = used + excluded.used",
                    (uid, _kst_day(now_ts), pts))

    def _price_row(self, con, fish: dict, market: dict, now_ts: int, icebox: bool, new: bool = False, v340: bool = False) -> int:
        """v340 = 3.4.0: 판매량에 따라 내려가지 않는다 (싯가는 날마다 랜덤으로만)."""
        m = market[fish["species"]]
        day = DAY_FISH_MULT if new and fish["species"] == day_fish(now_ts, v340) else 1.0   # 3.1.0 오늘의 물고기
        return fish_price(fish["species"], fish["size"], m["mult"] * day, 1.0 if v340 else m["supply"],
                          freshness(fish["caught_ts"], now_ts, icebox))

    @staticmethod
    def _level(con, uid: int) -> int:
        r = con.execute("SELECT level FROM fish_state WHERE user_id=?", (uid,)).fetchone()
        return int(r[0]) if r else 1

    @staticmethod
    def _fish_rows(con, uid: int, ids: Optional[list[int]] = None) -> list[dict]:
        q = "SELECT id, species, size, caught_ts, displayed FROM fish_bag WHERE user_id=?"
        args: list = [uid]
        if ids is not None:
            q += f" AND id IN ({','.join('?' * len(ids))})"
            args += ids
        return [dict(zip(("id", "species", "size", "caught_ts", "displayed"), r)) for r in con.execute(q + " ORDER BY id", args)]

    # ───────────── 찌 던지기 · 입질 · 잡기 ─────────────
    async def status(self, uid: int, now_ts: int) -> dict:
        def fn(con):
            s = self._state(con, uid)
            s["bag"] = con.execute("SELECT COUNT(*) FROM fish_bag WHERE user_id=?", (uid,)).fetchone()[0]
            s["used"], s["limit"] = self._trade(con, uid, now_ts)
            s["buffs"] = sorted(self._buffs(con, uid, now_ts))
            s["rod"] = self._rod(con, uid)
            s["rod_dur"] = (self._dur(con, uid, s["rod"]), ROD_DUR[s["rod"]]) if s["rod"] else None   # 3.4.0
            s["bait_qty"] = {k: self._qty(con, uid, k) for k in ("bait", "bait_gold")}
            s["need"] = level_need(s["level"])
            return s
        return await self._tx(fn)

    async def cast(self, uid: int, now_ts: int, bait: Optional[str], channel_id: int, rng=random, new: bool = False,
                   v340: bool = False) -> dict:
        """찌 던지기. 실패 reason: busy(이미 던짐) / full(어망 가득) / no_bait."""
        def fn(con):
            s = self._state(con, uid)
            if s["cast_ts"]:
                return {"ok": False, "reason": "busy", **s}
            if con.execute("SELECT COUNT(*) FROM fish_bag WHERE user_id=?", (uid,)).fetchone()[0] >= bag_max(s["level"], new):
                return {"ok": False, "reason": "full", "max": bag_max(s["level"], new)}
            if bait:
                if self._qty(con, uid, bait) <= 0:
                    return {"ok": False, "reason": "no_bait"}
                con.execute("UPDATE inventory SET qty = qty - 1 WHERE user_id=? AND item=?", (uid, bait))
            rod = self._rod(con, uid)
            window = (RODS_340 if v340 else RODS)[rod][1] if rod else CATCH_WINDOW
            bite = now_ts + rng.randint(BITE_MIN, BITE_MAX)
            con.execute("UPDATE fish_state SET cast_ts=?, bite_ts=?, window=?, bait=?, channel_id=?, message_id=NULL "
                        "WHERE user_id=?", (now_ts, bite, window, bait, channel_id, uid))
            return {"ok": True, "bite_ts": bite, "window": window, "rod": rod}
        return await self._tx(fn)

    async def set_message(self, uid: int, message_id: int) -> None:
        await self._tx(lambda con: con.execute("UPDATE fish_state SET message_id=? WHERE user_id=?", (message_id, uid)))

    async def active_casts(self) -> list[dict]:
        def fn(con):
            return [dict(zip(("user_id", "cast_ts", "bite_ts", "window", "channel_id", "message_id"), r)) for r in con.execute(
                "SELECT user_id, cast_ts, bite_ts, window, channel_id, message_id FROM fish_state WHERE cast_ts IS NOT NULL")]
        return await self._tx(fn)

    async def reel(self, uid: int, now_ts: int, rng=random, new_xp: bool = True, new_310: bool = False,
                   new_311: bool = False, new_340: bool = False, new_350: bool = False) -> dict:
        """[잡기!]. 결과 kind: early(아직 입질 전 — 찌만 회수) / fled(늦음) / junk / fish. 실패 reason: idle."""
        def fn(con):
            s = self._state(con, uid)
            if not s["cast_ts"]:
                return {"ok": False, "reason": "idle"}
            con.execute("UPDATE fish_state SET cast_ts=NULL, bite_ts=NULL, bait=NULL, message_id=NULL WHERE user_id=?", (uid,))
            if now_ts < s["bite_ts"]:
                return {"ok": True, "kind": "early"}
            if now_ts > s["bite_ts"] + s["window"]:
                return {"ok": True, "kind": "fled", "wait": s["bite_ts"] - s["cast_ts"]}
            r = self._catch(con, uid, s, s["bite_ts"] - s["cast_ts"], s["bait"], now_ts, rng, new_xp, new_310, new_311,
                            new_340, wear=new_340, new_350=new_350)
            if new_310 and rng.random() < AUTO_DROP:   # 3.1.0 🤖 자동 낚시 (아주 가끔)
                give_item(con, uid, "auto_fish")
                r["auto_drop"] = True
            return r
        return await self._tx(fn)

    def _catch(self, con, uid: int, s: dict, wait: float, bait: Optional[str], now_ts: int, rng,
               new_xp: bool = True, new_310: bool = False, new_311: bool = False, new_340: bool = False,
               wear: bool = False, new_350: bool = False) -> dict:
        """입질을 잡은 한 번 — 잡템 또는 물고기를 어망에 넣는다 ([잡기!] · 자동 낚시 공용).
        new_340 = 3.4.0 확률 · 물고기 · 장비 효과 · wear = 낚싯대가 닳고 대어는 가끔 줄이 끊긴다 ([잡기!] 만).
        new_350 = 3.5.0 희귀 · 영웅 · 전설 확률 상향."""
        buffs = self._buffs(con, uid, now_ts)
        rod = self._rod(con, uid)
        junk_p = (BAIT_JUNK_340 if new_340 else BAIT_JUNK) if bait == "bait" else JUNK_PROB
        if rng.random() < junk_p:
            item = rng.choices(list(JUNK), weights=list(JUNK.values()))[0]
            if item != "old_boot":
                give_item(con, uid, item)
            return {"ok": True, "kind": "junk", "item": item, "wait": wait, "rod": self._wear(con, uid, rod, 1) if wear else None}
        bonus = min(0.174, LEVEL_RARE_STEP * (s["level"] - 1)) \
            + ((BAIT_GOLD_T_340 if new_340 else BAIT_GOLD_T) if bait == "bait_gold" else 0) \
            + (0.10 if "stew" in buffs else 0)
        if new_311 and rod:   # 3.1.1 좋은 낚싯대일수록 희귀 확률 살짝 ↑ (3.4.0 두 배)
            bonus += (ROD_RARE_340 if new_340 else ROD_RARE_311)[rod]
        odds = grade_odds(wait, bonus, new_310, new_340, new_350 and new_340)
        grade = rng.choices(range(5), weights=odds)[0]
        key = rng.choice(by_grade(new_310, new_340)[grade])
        if wear and grade >= 3 and rng.random() < LINE_SNAP[rod]:   # 3.4.0 대어에 줄이 끊어졌다!
            return {"ok": True, "kind": "snap", "species": key, "grade": grade, "wait": wait,
                    "rod": self._wear(con, uid, rod, SNAP_DAMAGE)}
        size = roll_size(key, rng, (RODS_340 if new_340 else RODS)[rod][0] if rod else 0)
        xp = catch_xp(key, size, new_xp)
        if "grill" in buffs:
            xp = round(xp * 1.3)
        level, lxp, up = add_xp(s["level"], s["xp"], xp)
        con.execute("UPDATE fish_state SET level=?, xp=?, caught=caught+1 WHERE user_id=?", (level, lxp, uid))
        cur = con.execute("INSERT INTO fish_bag(user_id, species, size, caught_ts) VALUES(?,?,?,?)", (uid, key, size, now_ts))
        best = con.execute("SELECT best FROM fish_dex WHERE user_id=? AND species=?", (uid, key)).fetchone()
        new_species = best is None
        con.execute("INSERT INTO fish_dex(user_id, species, cnt, best) VALUES(?,?,1,?) ON CONFLICT(user_id, species) "
                    "DO UPDATE SET cnt = cnt + 1, best = MAX(best, excluded.best)", (uid, key, size))
        con.execute("INSERT INTO fish_log VALUES(?,?,?,?,?)", (uid, key, size, now_ts, week_key(now_ts)))
        market = self._market(con, now_ts)
        price = fish_price(key, size, market[key]["mult"], 1.0 if new_340 else market[key]["supply"], 1.0)
        return {"ok": True, "kind": "fish", "id": cur.lastrowid, "species": key, "grade": grade, "size": size,
                "xp": xp, "level": level, "leveled": up, "wait": wait, "odds": odds, "price": price,
                "new": new_species, "record": bool(best and size > best[0]),
                "rod": self._wear(con, uid, rod, 1) if wear else None}

    # ───────────── 🤖 자동 낚시 (3.1.0) ─────────────
    async def use_auto(self, uid: int, now_ts: int) -> dict:
        """자동 낚시 사용 — 24시간 켜짐 (켜져 있으면 남은 시간에 24시간 더)."""
        def fn(con):
            if self._qty(con, uid, "auto_fish") <= 0:
                return {"ok": False, "reason": "none"}
            con.execute("UPDATE inventory SET qty = qty - 1 WHERE user_id=? AND item='auto_fish'", (uid,))
            r = con.execute("SELECT until, next_ts FROM fish_auto WHERE user_id=?", (uid,)).fetchone()
            if r and r[0] > now_ts:
                until, nxt = r[0] + AUTO_HOURS * 3600, r[1]
            else:
                until, nxt = now_ts + AUTO_HOURS * 3600, now_ts + random.randint(*AUTO_EVERY)
            con.execute("INSERT OR REPLACE INTO fish_auto VALUES(?,?,?)", (uid, until, nxt))
            return {"ok": True, "until": until, "next": nxt}
        return await self._tx(fn)

    async def auto_run(self, uid: int, now_ts: int, rng=random, new: bool = False, v340: bool = False,
                       v350: bool = False) -> dict:
        """밀린 자동 낚시를 한꺼번에 처리 → {until, next, fish: [...], junk: [...], full: 어망이 차서 쉰 횟수}."""
        def fn(con):
            r = con.execute("SELECT until, next_ts FROM fish_auto WHERE user_id=?", (uid,)).fetchone()
            out = {"until": 0, "next": None, "fish": [], "junk": [], "full": 0}
            if not r:
                return out
            until, nxt = r
            while nxt <= min(now_ts, until):
                s = self._state(con, uid)
                if con.execute("SELECT COUNT(*) FROM fish_bag WHERE user_id=?", (uid,)).fetchone()[0] >= bag_max(s["level"], new):
                    out["full"] += 1
                else:
                    c = self._catch(con, uid, s, BITE_MIN, None, nxt, rng, True, new, new_340=v340, new_350=v350)
                    (out["fish"] if c["kind"] == "fish" else out["junk"]).append(c)
                nxt += rng.randint(*AUTO_EVERY)
            if nxt > until:
                con.execute("DELETE FROM fish_auto WHERE user_id=?", (uid,))
            else:
                con.execute("UPDATE fish_auto SET next_ts=? WHERE user_id=?", (nxt, uid))
            out.update(until=until if nxt <= until else 0, next=nxt if nxt <= until else None)
            return out
        return await self._tx(fn)

    async def auto_users(self) -> list[int]:
        return await self._tx(lambda con: [r[0] for r in con.execute("SELECT user_id FROM fish_auto")])

    # ───────────── 어망 · 시세 · 판매 · 보내기 ─────────────
    async def bag(self, uid: int, now_ts: int, new: bool = False, v340: bool = False) -> dict:
        """어망 [{id, species, size, caught_ts, displayed, price, fresh}] + 거래 한도 · 어망 칸 수."""
        def fn(con):
            market = self._market(con, now_ts)
            ice = self._qty(con, uid, "icebox") > 0
            rows = self._fish_rows(con, uid)
            for f in rows:
                f["fresh"] = freshness(f["caught_ts"], now_ts, ice)
                f["price"] = self._price_row(con, f, market, now_ts, ice, new, v340)
            used, limit = self._trade(con, uid, now_ts)
            return {"fish": rows, "used": used, "limit": limit, "icebox": ice, "max": bag_max(self._level(con, uid), new)}
        return await self._tx(fn)

    async def market(self, now_ts: int, new: bool = False, v340: bool = False) -> dict[str, dict]:
        def fn(con):
            m = self._market(con, now_ts)
            today = day_fish(now_ts, v340) if new else None
            for k, v in m.items():
                v["price"] = fish_price(k, FISH[k][3], v["mult"] * (DAY_FISH_MULT if k == today else 1.0),
                                        1.0 if v340 else v["supply"], 1.0)
            return m
        return await self._tx(fn)

    async def sell(self, uid: int, ids: Optional[list[int]], now_ts: int, new: bool = False, v340: bool = False) -> dict:
        """판매 — 거래 포인트를 쓰지 않는다 (2.82b: 포인트는 유저 간 보내기에만). ids=None 이면 전시 중이 아닌 물고기 전부 (3.1.0).
        실패 reason: none(없음 · 전시 중)."""
        def fn(con):
            rows = [f for f in self._fish_rows(con, uid, ids) if not f["displayed"]]
            if not rows:
                return {"ok": False, "reason": "none"}
            market = self._market(con, now_ts)
            ice = self._qty(con, uid, "icebox") > 0
            total = 0
            for f in rows:   # 한 마리씩 팔 때마다 그 종 시세가 내려간다 (3.4.0 공개 서버는 안 내려간다 — 랜덤 시세만)
                total += self._price_row(con, f, market, now_ts, ice, new, v340)
                if not v340:
                    m = market[f["species"]]
                    m["supply"] = max(SUPPLY_MIN, m["supply"] * (1 - SUPPLY_HIT))
                    con.execute("UPDATE fish_market SET supply=? WHERE species=?", (m["supply"], f["species"]))
                con.execute("DELETE FROM fish_bag WHERE id=?", (f["id"],))
            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (uid,))
            con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (total, uid))
            bal = con.execute("SELECT balance FROM wallets WHERE user_id=?", (uid,)).fetchone()[0]
            return {"ok": True, "count": len(rows), "gain": total, "balance": int(bal)}
        return await self._tx(fn)

    async def give(self, uid: int, to_uid: int, ids: list[int], now_ts: int, new: bool = False) -> dict:
        """다른 유저에게 보내기 (트레이드) — 보내는 쪽 거래 포인트를 쓴다. reason: self / none / limit / full."""
        if uid == to_uid:
            return {"ok": False, "reason": "self"}

        def fn(con):
            rows = [f for f in self._fish_rows(con, uid, ids) if not f["displayed"]]
            if not rows:
                return {"ok": False, "reason": "none"}
            used, limit = self._trade(con, uid, now_ts)
            pts = sum(GRADE_POINTS[FISH[f["species"]][2]] for f in rows)
            if used + pts > limit:
                return {"ok": False, "reason": "limit", "need": pts, "left": limit - used}
            have = con.execute("SELECT COUNT(*) FROM fish_bag WHERE user_id=?", (to_uid,)).fetchone()[0]
            if have + len(rows) > bag_max(self._level(con, to_uid), new):
                return {"ok": False, "reason": "full"}
            for f in rows:
                con.execute("UPDATE fish_bag SET user_id=? WHERE id=?", (to_uid, f["id"]))
            self._spend_points(con, uid, now_ts, pts)
            return {"ok": True, "count": len(rows), "points": pts, "used": used + pts, "limit": limit,
                    "names": [FISH[f["species"]][1] for f in rows]}
        return await self._tx(fn)

    async def release_fish(self, uid: int, ids: list[int]) -> int:
        """놓아주기 — 한도 없이 어망 비우기 (돈 · 경험치 없음)."""
        def fn(con):
            q = f"DELETE FROM fish_bag WHERE user_id=? AND displayed=0 AND id IN ({','.join('?' * len(ids))})"
            return con.execute(q, [uid, *ids]).rowcount
        return await self._tx(fn) if ids else 0

    async def use_charm(self, uid: int, now_ts: int) -> dict:
        def fn(con):
            if self._qty(con, uid, "fish_charm") <= 0:
                return {"ok": False, "reason": "none"}
            con.execute("UPDATE inventory SET qty = qty - 1 WHERE user_id=? AND item='fish_charm'", (uid,))
            con.execute("INSERT INTO fish_trade(user_id, day_key, used, bonus) VALUES(?,?,0,?) "
                        "ON CONFLICT(user_id, day_key) DO UPDATE SET bonus = bonus + excluded.bonus",
                        (uid, _kst_day(now_ts), CHARM_BONUS))
            used, limit = self._trade(con, uid, now_ts)
            return {"ok": True, "used": used, "limit": limit}
        return await self._tx(fn)

    async def combine_pieces(self, uid: int, rng=random) -> dict:
        """스킵권 조각 5개 → 스킵권 1장 (랜덤)."""
        def fn(con):
            if self._qty(con, uid, "skip_piece") < SKIP_PIECES:
                return {"ok": False, "reason": "none", "have": self._qty(con, uid, "skip_piece")}
            con.execute("UPDATE inventory SET qty = qty - ? WHERE user_id=? AND item='skip_piece'", (SKIP_PIECES, uid))
            item = rng.choice(("scout_skip", "train_skip", "watch_skip"))
            give_item(con, uid, item)
            return {"ok": True, "item": item}
        return await self._tx(fn)

    # ───────────── 먹이기 · 요리 · 수족관 ─────────────
    async def feed(self, uid: int, fish_id: int, target: str, now_ts: int) -> dict:
        """target: 'P{유망주 id}' 또는 선발 선수 player_id. 하루 대상마다 한 마리.
        reason: none / fed(오늘 이미) / not_starter. 한도(판매 포인트)는 쓰지 않는다."""
        day = _kst_day(now_ts)

        def fn(con):
            rows = [f for f in self._fish_rows(con, uid, [fish_id]) if not f["displayed"]]
            if not rows:
                return {"ok": False, "reason": "none"}
            f = rows[0]
            grade = FISH[f["species"]][2]
            out = {"ok": True, "species": f["species"], "grade": grade}
            if target.startswith("P"):
                pid = int(target[1:])
                row = con.execute("SELECT name, xp FROM prospects WHERE id=? AND user_id=? AND retired_ts=0",
                                  (pid, uid)).fetchone()
                if not row:
                    return {"ok": False, "reason": "not_starter"}
                if con.execute("SELECT 1 FROM fish_fed WHERE user_id=? AND target=? AND day_key=?", (uid, target, day)).fetchone():
                    return {"ok": False, "reason": "fed"}
                con.execute("UPDATE prospects SET xp = xp + ? WHERE id=?", (FEED_PROSPECT_XP[grade], pid))
                out.update(kind="prospect", name=row[0], xp=FEED_PROSPECT_XP[grade])
            else:
                if not con.execute("SELECT 1 FROM club_lineup WHERE user_id=? AND player_id=?", (uid, target)).fetchone():
                    return {"ok": False, "reason": "not_starter"}
                if con.execute("SELECT 1 FROM fish_fed WHERE user_id=? AND target=? AND day_key=?", (uid, target, day)).fetchone():
                    return {"ok": False, "reason": "fed"}
                out["kind"] = "player"
            con.execute("INSERT INTO fish_fed VALUES(?,?,?)", (uid, target, day))
            con.execute("DELETE FROM fish_bag WHERE id=?", (fish_id,))
            out["fed_today"] = self._fed_starters(con, uid, day)
            return out
        return await self._tx(fn)

    @staticmethod
    def _fed_starters(con, uid: int, day: int) -> int:
        return con.execute("SELECT COUNT(*) FROM fish_fed f JOIN club_lineup l ON l.user_id=f.user_id AND l.player_id=f.target "
                           "WHERE f.user_id=? AND f.day_key=?", (uid, day)).fetchone()[0]

    async def team_meal_bonus(self, uid: int, now_ts: int) -> int:
        """오늘 선발 11명을 모두 먹였으면 경기 전력 +1."""
        def fn(con):
            try:
                return FEED_TEAM_BONUS if self._fed_starters(con, uid, _kst_day(now_ts)) >= FEED_FULL_TEAM else 0
            except sqlite3.OperationalError:
                return 0
        return await self._tx(fn)

    async def fed_today(self, uid: int, now_ts: int) -> set[str]:
        day = _kst_day(now_ts)
        return await self._tx(lambda con: {t for (t,) in con.execute(
            "SELECT target FROM fish_fed WHERE user_id=? AND day_key=?", (uid, day))})

    async def cook(self, uid: int, recipe: str, now_ts: int, ids: Optional[list[int]] = None, new: bool = False,
                   rng=random, v340: bool = False) -> dict:
        """요리: 어망에서 재료 등급 물고기를 (가장 싼 것부터) 쓰고 양념을 쓴다. reason: fish(물고기 부족) / spice / pick.
        new = 3.1.1: 재료 두 배(RECIPES_311_NEEDS) · ids 로 쓸 물고기를 직접 고른다 · COOK_CHANCE_311 확률로 성공
        (실패하면 재료는 타서 사라지고 효과는 없다 → burnt=True)."""
        _, _, needs, spice, _ = RECIPES[recipe]
        if new:
            needs = RECIPES_311_NEEDS[recipe]

        def fn(con):
            market = self._market(con, now_ts)
            ice = self._qty(con, uid, "icebox") > 0
            rows = [f for f in self._fish_rows(con, uid) if not f["displayed"]]
            use = []
            if ids is not None:   # 직접 고른 물고기: 등급 · 마릿수가 정확히 맞아야
                pick = [f for f in rows if f["id"] in set(ids)]
                for grade, n in needs:
                    got = [f for f in pick if FISH[f["species"]][2] == grade]
                    if len(got) != n or len(pick) != sum(k for _, k in needs):
                        return {"ok": False, "reason": "pick", "grade": grade, "need": n, "have": len(got)}
                use = pick
            for grade, n in needs if ids is None else ():
                pool = sorted((f for f in rows if FISH[f["species"]][2] == grade and f not in use),
                              key=lambda f: self._price_row(con, f, market, now_ts, ice))
                if len(pool) < n:
                    return {"ok": False, "reason": "fish", "grade": grade, "need": n, "have": len(pool)}
                use += pool[:n]
            if self._qty(con, uid, "spice") < spice:
                return {"ok": False, "reason": "spice", "need": spice, "have": self._qty(con, uid, "spice")}
            for f in use:
                con.execute("DELETE FROM fish_bag WHERE id=?", (f["id"],))
            if spice:
                con.execute("UPDATE inventory SET qty = qty - ? WHERE user_id=? AND item='spice'", (spice, uid))
            used = [FISH[f["species"]][1] for f in use]
            if new and rng.random() >= (COOK_CHANCE_340 if v340 else COOK_CHANCE_311)[recipe]:
                return {"ok": True, "burnt": True, "used": used}
            until = now_ts + COOK_HOURS * 3600
            con.execute("INSERT OR REPLACE INTO fish_buffs VALUES(?,?,?)", (uid, recipe, until))
            return {"ok": True, "burnt": False, "until": until, "used": used}
        return await self._tx(fn)

    async def buffs(self, uid: int, now_ts: int) -> dict[str, int]:
        return await self._tx(lambda con: {k: u for k, u in con.execute(
            "SELECT kind, until FROM fish_buffs WHERE user_id=? AND until>?", (uid, now_ts))})

    async def display(self, uid: int, fish_id: int, on: bool) -> dict:
        """수족관 전시 / 내리기. 전시 중인 물고기는 팔거나 먹일 수 없다. reason: none / full."""
        def fn(con):
            if not self._fish_rows(con, uid, [fish_id]):
                return {"ok": False, "reason": "none"}
            if on and con.execute("SELECT COUNT(*) FROM fish_bag WHERE user_id=? AND displayed=1", (uid,)).fetchone()[0] >= AQUARIUM_MAX:
                return {"ok": False, "reason": "full"}
            con.execute("UPDATE fish_bag SET displayed=? WHERE id=?", (int(on), fish_id))
            return {"ok": True}
        return await self._tx(fn)

    async def aquarium_score(self, uid: int) -> int:
        """수족관 점수 — 전시한 물고기 등급 점수 합 (흔함 1 · 보통 2 · 희귀 4 · 영웅 8 · 전설 16). 구단 팬 만족도에 쓴다."""
        def fn(con):
            try:
                rows = con.execute("SELECT species FROM fish_bag WHERE user_id=? AND displayed=1", (uid,)).fetchall()
            except sqlite3.OperationalError:
                return 0
            return sum(2 ** FISH[s][2] for (s,) in rows if s in FISH)
        return await self._tx(fn)

    # ───────────── 3.6.0 도감 보상 ─────────────
    async def dex_rewards(self, uid: int) -> set[int]:
        """이미 받은 도감 보상 (등급 번호 · 5 = 전체 완성)."""
        return await self._tx(lambda con: {int(g) for (g,) in con.execute(
            "SELECT grade FROM fish_dex_reward WHERE user_id=?", (uid,))})

    async def claim_dex(self, uid: int, v340: bool = True) -> list[tuple[int, int, int]]:
        """다 모은 등급 보상을 한 번씩 받는다 → [(등급, 돈, 자동 낚시 개수)] (등급 5 = 전체 완성)."""
        pool = by_grade(True, v340)

        def fn(con):
            have = {s for (s,) in con.execute("SELECT species FROM fish_dex WHERE user_id=?", (uid,))}
            done = {int(g) for (g,) in con.execute("SELECT grade FROM fish_dex_reward WHERE user_id=?", (uid,))}
            out = []
            full = all(k in have for b in pool for k in b)
            for g in range(6):
                if g in done or not (full if g == 5 else all(k in have for k in pool[g])):
                    continue
                money, robots = DEX_REWARDS[g]
                con.execute("INSERT INTO fish_dex_reward(user_id, grade) VALUES(?, ?)", (uid, g))
                con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (uid,))
                con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (money, uid))
                if robots:
                    con.execute("INSERT INTO inventory(user_id, item, qty) VALUES(?, 'auto_fish', ?) "
                                "ON CONFLICT(user_id, item) DO UPDATE SET qty = qty + excluded.qty", (uid, robots))
                out.append((g, money, robots))
            return out
        return await self._tx(fn)

    # ───────────── 도감 · 주간 최대어 ─────────────
    async def dex(self, uid: int) -> dict[str, tuple[int, float]]:
        return await self._tx(lambda con: {s: (int(c), float(b)) for s, c, b in con.execute(
            "SELECT species, cnt, best FROM fish_dex WHERE user_id=?", (uid,))})

    async def recent(self, uid: int, limit: int = 15) -> list[dict]:
        """최근에 낚은 물고기 (2.9.1 낚시 기록) — 판 · 보낸 물고기도 남는다. 최신 순."""
        return await self._tx(lambda con: [
            {"species": sp, "size": float(size), "ts": int(ts)} for sp, size, ts in con.execute(
                "SELECT species, size, ts FROM fish_log WHERE user_id=? ORDER BY ts DESC, rowid DESC LIMIT ?", (uid, limit))
            if sp in FISH])

    async def weekly_top(self, now_ts: int, limit: int = 5, per_user: bool = True) -> list[dict]:
        """이번 주 최대어 — 평균 크기 대비 가장 큰 물고기 (종마다 크기가 달라서 비율로 겨룬다). 유저당 1마리.
        per_user=False (3.6.0): 유저당 1마리가 아니라 이번 주 낚은 물고기 전체를 크기 순으로."""
        wk = week_key(now_ts)

        def fn(con):
            best: dict[int, tuple] = {}
            every: list[tuple] = []
            for uid, sp, size, ts in con.execute("SELECT user_id, species, size, ts FROM fish_log WHERE week=?", (wk,)):
                if sp not in FISH:
                    continue
                score = (FISH[sp][2], size_mult(sp, size))   # 등급 먼저, 그다음 크기 비율
                every.append((uid, (score, sp, size, ts)))
                if uid not in best or score > best[uid][0]:
                    best[uid] = (score, sp, size, ts)
            top = sorted(best.items() if per_user else every, key=lambda kv: kv[1][0], reverse=True)[:limit]
            return [{"user_id": u, "species": sp, "size": size, "ratio": sc[1], "ts": ts} for u, (sc, sp, size, ts) in top]
        return await self._tx(fn)


def odds_line(odds: list[float]) -> str:
    return " · ".join(f"{GRADE_EMOJI[g]} {p * 100:.1f}%" for g, p in enumerate(odds))


def expected_value(wait: float, new: bool = False) -> float:
    """기다린 시간별 한 마리 평균 기준가 (잡템 제외) — 밸런스 확인용."""
    odds, pool = grade_odds(wait, new=new), by_grade(new)
    return sum(p * sum(FISH[k][4] for k in pool[g]) / len(pool[g]) for g, p in enumerate(odds))


def fish_name(key: str) -> str:
    e, n, g, _, _ = FISH[key]
    return f"{e} {n}"


def now() -> int:
    return int(time.time())


assert len(FISH) == 100 and [len(b) for b in BY_GRADE] == [13, 11, 8, 5, 3] and [len(b) for b in BY_GRADE_310] == [19, 16, 10, 6, 4]
assert [len(b) for b in BY_GRADE_340] == [29, 26, 19, 14, 12]
assert all(math.isclose(sum(o), 100) for o in (ODDS_FAST, ODDS_SLOW, ODDS_FAST_310, ODDS_SLOW_310, ODDS_FAST_340, ODDS_SLOW_340,
                                              ODDS_FAST_350, ODDS_SLOW_350))
