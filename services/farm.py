# services/farm.py — 🌾 /농사 (3.8.1, 테스트 서버 먼저)
# 시작할 때 땅 하나를 고른다 (땅마다 세부 자원이 다르다) → 작물을 심고 동물을 기른다 → 수확물을 합쳐 등급을 올리거나 판다.
# 행동(심기 · 수확 · 모으기 · 합치기)마다 ⚡ 에너지 1. 버는 것은 🪙 농장 코인 — 씨앗 · 확장 · 낫 강화에 쓰고, 원하면 축구 봇 돈으로 환전.
# 축구 봇 돈이 0원이어도 다 할 수 있다 (당근 씨앗은 무료). 돈은 운 요소(상자 행운 열기 · 강화 행운 부적)에만 쓴다.
# 다른 사람 농장은 인수 제안으로 살 수 있고, 농장은 여러 개 가질 수 있다 (아래 '인수').
# 씨앗 · 동물은 서버마다 하나 있는 '농사 상점' 스레드에서 코인으로 사 두고 (farm_seeds), 농장에서 심고 입양한다.
import random
from typing import Optional

import release
from services import burning, lobby
from services.economy_db import EconomyDB, _kst_day, give_item

# ───────────── 땅 ─────────────
# 세부 자원(별 1~5, 땅마다 합 12): 비옥도 = 작물 성장 · 목초 = 동물 생산 · 물 = 에너지 회복 · 광맥 = 상자 발견
RES = {"soil": ("🌱", "비옥도", "작물이 빨리 자라요"), "grass": ("🌿", "목초", "동물이 빨리 만들어요"),
       "water": ("💧", "물", "에너지가 빨리 차요"), "ore": ("⛏️", "광맥", "상자가 자주 나와요")}
LANDS = {   # key → (이모지, 이름, 한 줄 특징, {자원: 별})
    "plain": ("🌾", "황금 평야", "흙이 기름져서 작물이 빨리 자라요", {"soil": 5, "grass": 2, "water": 3, "ore": 2}),
    "meadow": ("🐄", "푸른 목초지", "풀이 좋아서 동물이 빨리 만들어요", {"soil": 2, "grass": 5, "water": 3, "ore": 2}),
    "river": ("🏞️", "강변 마을", "물이 풍부해서 에너지가 빨리 차요", {"soil": 3, "grass": 3, "water": 5, "ore": 1}),
    "mountain": ("⛰️", "바위 산지", "땅속에 상자가 많이 묻혀 있어요", {"soil": 2, "grass": 2, "water": 3, "ore": 5}),
}
STAR_STEP = 0.10   # 별 3 이 기준 — 별 하나마다 걸리는 시간 ∓10%


def time_mult(stars: int) -> float:
    """자원 별 → 걸리는 시간 배수 (별 5 = ×0.8 · 별 3 = ×1 · 별 1 = ×1.2)."""
    return 1 - STAR_STEP * (stars - 3)


# ───────────── 작물 · 동물 · 물건 ─────────────
# 작물: key → (이모지, 이름, 씨앗 값(코인), 자라는 시간(초), ★1 판매가(코인)) — 당근은 무료(씨앗 없이 심는다)라서 코인이 0 이어도 막히지 않는다
# 11/11 규완님 '낚시와 다르게 템포가 굉장히 빠른 게임' → 시간을 분 단위로 · 종류를 늘림
# 11/11 규완님 '시간을 더더 대폭 줄여도 될 거 같아' → 10초 ~ 15분
# 11/11 규완님 '게임 템포 더 빠르게' → 다시 절반 (5초 ~ 7분 30초)
CROPS = {
    "carrot": ("🥕", "당근", 0, 5, 4),
    "potato": ("🥔", "감자", 5, 8, 14),
    "wheat": ("🌾", "밀", 10, 15, 25),
    "corn": ("🌽", "옥수수", 20, 30, 45),
    "cabbage": ("🥬", "배추", 35, 45, 75),
    "tomato": ("🍅", "토마토", 60, 75, 120),
    "pumpkin": ("🎃", "호박", 100, 2 * 60, 190),
    "strawberry": ("🍓", "딸기", 150, 3 * 60, 280),
    "grape": ("🍇", "포도", 250, 270, 450),
    "watermelon": ("🍉", "수박", 400, 450, 700),
    # 3.8.2 규완님 '농작물 추가' (값 · 시간은 내 기본값, 원래 작물 사이사이와 수박 위로)
    "garlic": ("🧄", "마늘", 15, 20, 35),
    "pepper": ("🌶️", "고추", 80, 90, 150),
    "peach": ("🍑", "복숭아", 600, 600, 1_000),
    "pineapple": ("🍍", "파인애플", 900, 750, 1_500),
    "mango": ("🥭", "망고", 1_400, 900, 2_300),
}
NEW_CROPS_382 = ("garlic", "pepper", "peach", "pineapple", "mango")   # 3.8.2 공개 전엔 테스트 서버 상점에만
# 동물: key → (이모지, 이름, 값(코인), 만드는 물건, 만드는 시간(초)) — 땅 한 칸을 차지하고 계속 만든다
ANIMALS = {
    "chicken": ("🐔", "닭", 300, "egg", 15),
    "duck": ("🦆", "오리", 600, "feather", 23),
    "bee": ("🐝", "벌", 1_000, "honey", 38),
    "sheep": ("🐑", "양", 1_600, "wool", 60),
    "goat": ("🐐", "염소", 2_500, "cheese", 90),
    "pig": ("🐖", "돼지", 4_000, "truffle", 150),
    "cow": ("🐄", "소", 6_000, "milk", 4 * 60),
    # 3.8.4 규완님 '동물 추가' (값 · 시간은 내 기본값, 원래 동물 사이사이와 소 위로)
    "silkworm": ("🐛", "누에", 450, "silk", 18),
    "goose": ("🪿", "거위", 1_300, "goose_egg", 48),
    "llama": ("🦙", "라마", 3_200, "scarf", 2 * 60),
    "bison": ("🦬", "들소", 9_000, "butter", 330),
    "peacock": ("🦚", "공작", 14_000, "fan", 450),
}
NEW_ANIMALS_384 = ("silkworm", "goose", "llama", "bison", "peacock")   # 3.8.4 공개 전엔 테스트 서버 상점에만
PRODUCTS = {"egg": ("🥚", "달걀", 30), "feather": ("🪶", "깃털", 50), "honey": ("🍯", "꿀", 80), "wool": ("🧶", "양털", 120),
            "cheese": ("🧀", "치즈", 180), "truffle": ("🍄", "송로버섯", 280), "milk": ("🥛", "우유", 400),
            "silk": ("🧵", "비단", 40), "goose_egg": ("🪺", "거위 알", 100), "scarf": ("🧣", "라마 털목도리", 230),
            "butter": ("🧈", "버터", 600), "fan": ("🪭", "공작 부채", 900)}
# 3.8.4 규완님 '숫돌 주괴는 씨앗 50개 당 하나로 소모되며 인챈트 시 그 씨앗의 가치, 완성품의 가치가 대폭 상승'
# 인챈트한 씨앗은 '감자_e' 처럼 따로 쌓이고 (farm_seeds), 심으면 그 칸에서 '인챈트 감자'가 나온다. 값은 ENCH_MULT 배 (내 기본값)
ENCH, ENCH_MULT, ENCH_SEEDS, INGOT_NEED = "_e", 5, 50, 9   # 숫돌 9개 → 숫돌 주괴 1개, 주괴 1개 → 씨앗 50개 인챈트
ENCH_SLOW = 1.1   # 11/21 규완님 '인챈트 된 씨앗은 가격이 5배인 대신 진짜 살짝 조금의 페널티' → 자라는 시간 10% 더 (내 기본값)
# 이어서 '페널티로 이번 버닝에 적용되지 않게' → '10% 늦게 자라는 거도 적용해줘': 둘 다.
# 인챈트 씨앗을 심은 칸은 10% 늦게 자라고 버닝 '두 배 빨리 자람'을 받지 않으며, 인챈트 작물은 버닝 '판매 1.5배'를 받지 않는다.
ECROPS = {k + ENCH: (e, f"인챈트 {n}", v * ENCH_MULT) for k, (e, n, _, _, v) in CROPS.items()}
# 3.9.2 희귀 변이 (11/23 규완님 D 36): 거둘 때 가끔 황금 작물 · 황금 생산품이 나온다 (인챈트 작물은 빼고). 값 2배 · 확률은 내 기본값
MUT, MUT_P, MUT_MULT = "_g", 0.02, 2
MITEMS = {k + MUT: (e, f"황금 {n}", v * MUT_MULT)
          for k, (e, n, v) in {**{k: (e, n, v) for k, (e, n, _, _, v) in CROPS.items()}, **PRODUCTS}.items()}
ITEMS = {**{k: (e, n, v) for k, (e, n, _, _, v) in CROPS.items()}, **PRODUCTS, **ECROPS, **MITEMS}   # 창고에 들어가는 것: key → (이모지, 이름, ★1 판매가)
ANIMAL_PER_PLOTS = 3          # 동물은 땅 3칸당 1마리

# 아이템 (11/12 규완님이 고른 1 · 3 · 4 · 6): key → (이름, 값(코인), 한 줄 설명) — 농사 상점에서 사서 농장의 [아이템]에서 쓴다
# 값과 효과 세기는 내 기본값
TOOLS = {
    "fert": ("비료", 20, "심을 때 한 칸에 하나씩 쓰면 그 칸이 절반 시간에 자라요"),
    "drink": ("에너지 음료", 60, "마시면 에너지가 30 차요"),
    "feed": ("동물 사료", 50, "주면 모든 동물이 다음 생산품을 절반 시간에 만들어요"),
    "scarecrow": ("허수아비", 200, "세우면 10분 동안 거둘 때 ★2가 나올 확률이 15%p 올라가요"),
    "pfeed": ("고급 사료", 40, "소, 들소, 공작은 생산품 하나를 만들 때마다 하나씩 먹어요"),   # 3.9.2
}
FERT_MULT = 0.5               # 비료: 자라는 시간 × 0.5
DRINK_ENERGY = 30             # 에너지 음료: 에너지 +30 (11/12 규완님: 100 을 넘겨서도 찬다 — 100 한도는 저절로 찰 때만)
FEED_MULT = 0.5               # 동물 사료: 남은 생산 시간 × 0.5
SCARE_SEC, SCARE_BONUS = 600, 0.15   # 허수아비: 10분 · ★2 확률 +15%p (세워 둔 동안 또 세우면 10분 늘어난다)


TOOL_PRICE_391 = 5   # 3.9.1 (11/21 규완님 '농사 아이템 코인 가격도 좀 높여줘') — 상점 아이템 값 5배 (배수는 내 기본값)


def tool_price(key: str, v391: Optional[bool] = None) -> int:
    """상점 아이템 값 (코인). v391 을 안 주면 지금 상호작용 서버로 본다 (release.ctx_384)."""
    v = release.ctx_384() if v391 is None else v391
    return TOOLS[key][1] * (TOOL_PRICE_391 if v else 1)


def shop_price(key: str) -> Optional[int]:
    """농사 상점에서 파는 값 (코인). 무료 씨앗(당근)은 팔지 않는다 — 씨앗 없이 바로 심는다. 모르는 것은 None."""
    if key in CROPS:
        return CROPS[key][2] or None
    if key in TOOLS:
        return tool_price(key)
    return ANIMALS[key][2] if key in ANIMALS else None
ANIMAL_REFUND = 0.5           # 동물을 내보내면 입양 값의 절반
NAME_MAX = 20                 # 농장 이름 글자 수

# 합치기: 같은 물건 · 같은 등급 3개 → 한 등급 위 1개 (★5 까지). 등급이 오를수록 3개 값보다 조금씩 더 비싸다
TIER_MAX, MERGE_NEED = 5, 3
TIER_MULT = (1, 3.5, 12.5, 45, 165)


def value(key: str, tier: int) -> int:
    return round(ITEMS[key][2] * TIER_MULT[tier - 1])


# ───────────── 에너지 · 땅 확장 ─────────────
ENERGY_MAX, ENERGY_SEC = 100, 5       # 100 까지 · 5초에 1 (물 별에 따라 빨라진다)
ENERGY_COST_382 = 2                   # 3.8.2 규완님 '에너지 소비량 늘리기' — 심기 · 거두기 · 합치기 한 번(칸)에 1 → 2
PLOTS_BASE, PLOTS_MAX = 6, 24
ENERGY_PER_PLOT_382 = 10      # 3.8.2 (11/17 규완님 '기본 에너지를 특정 레벨에 맞춰서 올려줘'): 넓힌 땅 한 칸마다 최대 에너지 +10


def energy_max(plots: int, v382: bool) -> int:
    """최대 에너지 — 3.8.2 농장은 땅이 넓을수록 크다 (6칸 100 … 24칸 280)."""
    return ENERGY_MAX + (max(0, int(plots) - PLOTS_BASE) * ENERGY_PER_PLOT_382 if v382 else 0)
START_CHESTS = {"wood": 1}            # 시작 선물
RATE = 1_000                          # 환전: 농장 코인 1 = 축구 봇 돈 1,000원 (코인 → 돈 한쪽으로만)

# ───────────── 3.9.2 농사 돈 줄이기 (11/23 규완님이 고른 것) ─────────────
# 공개 전엔 테스트 서버 사람만 (lobby.TESTERS) — 농사는 서버를 넘나들어 사람 기준으로 본다
RATE_392 = 100                        # 환전 1코인 = 100원
ENCH_MULT_392 = 3                     # 인챈트 값 5배 → 3배
AUTO_CAP_392 = 10                     # 동물이 거두지 않아도 쌓는 생산품은 마리당 10개까지, 그 뒤로는 거둬야 다시 만든다
SPECIES_CAP_392 = 5                   # 같은 동물은 한 사람당 5마리까지 (넘는 동물은 만들지 않고, 내보내면 입양 값을 다 돌려준다)
PFEED_ANIMALS = ("cow", "bison", "peacock")   # 비싼 동물은 생산품 하나에 고급 사료 하나를 먹는다
# 11/24 규완님 '유지비 코인만 환불': 공개 전에 본섭에서 잘못 걷힌 농장 유지비 (11/23 09:02, 11/24 00:00 KST 두 번)
REFUNDS_392 = (("refund-392-upkeep-a", 930254019975344218, 400_000), ("refund-392-upkeep-b", 1146659618790051960, 840_000))
# 11/26 4.0.1 공개 직후(12:30 KST) 걷힌 유지비 (서버 로그 '[농장 유지비]') — restore_coins_401 이 코인을 되돌린 뒤 돌려준다
UPKEEP_REFUND_401 = ((725662872327159838, 62), (733634487073636352, 142_973), (930254019975344218, 420_000),
                     (1146659618790051960, 420_000), (1356105918793777354, 360))
UPKEEP_392 = 20_000                   # 농장 유지비: 두 번째 농장부터 하루 (순서 − 1) × 2만 코인 (7개면 하루 42만 코인)


# 3.9.2 아르바이트생 (11/23 규완님 '코인을 지속적으로 소비할 수 있는 시스템, 아르바이트생'):
# 농장마다 한 명. 한 시간마다 시급(코인)을 먼저 내고, 코인이 모자라면 그만둔다. 일은 농장 에너지를 써서 한다 (주인과 같은 규칙).
# key → (이름, 시급(코인), 몇 초마다 일하나, 하는 일). 하는 일: harvest 거두기 · replant 마지막에 심은 것 다시 심기 · sell 창고 팔기
STAFF = {
    "rookie": ("초보 알바", 5_000, 600, ("harvest",)),
    "skilled": ("숙련 알바", 15_000, 300, ("harvest", "replant")),
    "veteran": ("베테랑 알바", 30_000, 120, ("harvest", "replant", "sell")),
}
STAFF_JOBS = {"harvest": "다 자란 것 거두기", "replant": "마지막에 심은 것 다시 심기", "sell": "창고 물건 팔기"}


# 3.9.2 농장 레벨 · 도감 · 주간 납품 의뢰 (11/23 규완님 D 27 28 29 '코인이 목적이 아니더라도 농사를 계속'). 수치는 내 기본값.
# 레벨: 사람마다 하나. 거둔 물건 하나에 경험치 1, 납품 의뢰 하나에 ORDER_XP. 레벨이 오를 때마다 은 상자, 5레벨마다 금 상자 (코인은 주지 않는다).
LEVEL_MAX, ORDER_XP = 50, 300
ORDER_N = 3                                   # 한 주(월요일 KST 시작)에 의뢰 세 개
ORDER_COINS = (3_000, 10_000, 30_000)         # 의뢰마다 물건 값 합이 대략 이만큼 되게 개수를 정한다 (★1 값 기준)
ORDER_CHEST = ("wood", "silver", "gold")      # 의뢰마다 보상 상자


def level_need(lv: int) -> int:
    """lv → lv+1 에 필요한 경험치."""
    return 100 * int(lv)


def level_of(xp: int) -> tuple[int, int, int]:
    """경험치 → (레벨, 이번 레벨에서 모은 경험치, 다음 레벨까지 필요한 경험치). 최대 레벨이면 필요 0."""
    lv, xp = 1, int(xp)
    while lv < LEVEL_MAX and xp >= level_need(lv):
        xp -= level_need(lv)
        lv += 1
    return lv, xp, 0 if lv >= LEVEL_MAX else level_need(lv)


def week_of(now_ts: float) -> int:
    return (_kst_day(int(now_ts)) + 3) // 7   # 1970-01-01 은 목요일 → 월요일에 바뀐다


def week_end(now_ts: float) -> int:
    return ((week_of(now_ts) + 1) * 7 - 3) * 86400 - 9 * 3600


def base_items() -> list[str]:
    """도감 · 의뢰에 쓰는 기본 물건 (작물 · 생산품)."""
    return list(CROPS) + list(PRODUCTS)


# 3.9.2 계절 · 날씨 · 동물 이름과 친밀도 · 꾸미기 · 농부끼리 교환 (11/23 규완님 D 33 34 35 37 40). 수치는 내 기본값.
# 계절: 한 주마다 봄 → 여름 → 가을 → 겨울. 제철 작물은 SEASON_FAST 만큼 빨리 자란다.
SEASONS = (("봄", ("carrot", "cabbage", "strawberry", "peach")), ("여름", ("corn", "tomato", "watermelon", "pineapple", "mango")),
           ("가을", ("potato", "wheat", "pumpkin", "grape")), ("겨울", ("garlic", "pepper")))
SEASON_FAST = 0.8
# 날씨: 하루(KST)마다 모든 농장에 똑같이. key → (이름, 설명, 뽑힐 무게)
WEATHER = {"sun": ("맑음", "특별한 일이 없어요", 50), "rain": ("비", "작물이 20% 빨리 자라요", 20),
           "frost": ("서리", "작물이 20% 늦게 자라요", 10), "rainbow": ("무지개", "황금 물건이 두 배 잘 나와요", 10),
           "wind": ("바람", "거둘 때 상자가 두 배 잘 나와요", 10)}
WEATHER_GROW = {"rain": 0.8, "frost": 1.2}
LOVE_MAX = 100                 # 동물 친밀도: 그 동물을 거둘 때마다 +1, 100 이면 만드는 시간 20% 빠르게
LOVE_FAST = 0.2
PET_NAME_MAX = 12
# 꾸미기: 농장 코인으로 사서 (한 번 사면 내 농장 어디에나) 농장마다 하나를 걸어 둔다. 보기만 바뀐다.
DECOS = {"fence": ("나무 울타리", 50_000), "flower": ("꽃밭", 100_000), "windmill": ("풍차", 300_000),
         "well": ("우물", 500_000), "barn": ("빨간 헛간", 1_000_000), "statue": ("황금 키코 동상", 5_000_000)}
TRADE_HOURS = 24               # 교환 제안은 24시간 뒤 만료 (내놓은 물건은 돌려준다)


def season_of(now_ts: float) -> tuple[str, tuple]:
    return SEASONS[week_of(now_ts) % len(SEASONS)]


def weather_of(now_ts: float) -> str:
    keys = list(WEATHER)
    return random.Random(f"weather:{_kst_day(int(now_ts))}").choices(keys, weights=[WEATHER[k][2] for k in keys])[0]


def grow_mult(key: str, now_ts: float) -> float:
    """3.9.2 작물이 자라는 시간 배수 (계절 × 날씨)."""
    return (SEASON_FAST if key in season_of(now_ts)[1] else 1.0) * WEATHER_GROW.get(weather_of(now_ts), 1.0)


def love_mult(love: int) -> float:
    return 1 - LOVE_FAST * min(LOVE_MAX, max(0, int(love))) / LOVE_MAX


def v392(uid: int, now_ts: Optional[float] = None) -> bool:
    """3.9.2 농사 규칙. 공개 전엔 테스트 서버 안에서 테스트 서버 멤버만 (11/24 규완님 '일부가 본섭에 패치된 거 같은데':
    예전엔 테스트 서버 멤버면 다른 서버에서도 켜졌다 → 이제 상호작용이 온 서버도 본다)."""
    return release.released_392(now_ts) or (int(uid) in lobby.TESTERS and release.ctx_392())


def rate(uid: int, now_ts: Optional[float] = None) -> int:
    """환전 비율 (코인 1개 = 원)."""
    return RATE_392 if v392(uid, now_ts) else RATE


def upkeep(n_farms: int) -> int:
    """농장 n 개의 하루 유지비 (코인)."""
    return sum(UPKEEP_392 * (i - 1) for i in range(1, int(n_farms) + 1))


def expand_cost(plots: int) -> int:
    """지금 plots 칸일 때 한 칸 넓히는 값 (코인): 150 · 400 · 750 · 1,200 …"""
    k = plots - PLOTS_BASE + 1
    return 50 * k * (k + 2)


# ───────────── ⚒️ 낫 강화 ─────────────
SICKLE_MAX = 10
SICKLE_MAX_384 = 15                   # 3.8.4 규완님 '낫 강화 +15까지' (공개 전엔 테스트 서버만 — cog 가 cap 으로 넘긴다)
SICKLE_CHANCE = (0.95, 0.90, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.25, 0.20,
                 0.15, 0.12, 0.10, 0.08, 0.05)   # +N → +N+1 성공 확률 (실패해도 안 내려간다) · +10 부터는 3.8.4 (내 기본값)
CHARM_BONUS = 0.10                    # 🍀 행운 부적(축구 봇 돈): 성공 확률 +10%p
CHARM_FEE = 300_000                   # × (다음 강화 단계)


def sickle_cost(level: int) -> int:
    """+level → +level+1 강화 값 (코인)."""
    return 150 * (level + 1) ** 2


def sickle_whet(level: int) -> int:
    """코인 대신 🪨 숫돌로 강화할 때 필요한 개수."""
    return level + 1


def extra_p(level: int) -> float:
    """수확할 때 하나 더 나올 확률."""
    return 0.05 + 0.02 * level


def high_p(level: int) -> float:
    """수확할 때 ★2 로 나올 확률."""
    return 0.02 + 0.01 * level


# ───────────── 🎁 상자 ─────────────
CHESTS = {"wood": ("📦", "나무 상자"), "silver": ("🎁", "은 상자"), "gold": ("👑", "금 상자")}
CHEST_WEIGHTS = (80, 17, 3)           # 수확하다 상자가 나올 때 나무 · 은 · 금
CHEST_REWARDS = {"wood": 1, "silver": 2, "gold": 3}   # 상자 하나에서 나오는 보상 수
# 보상 표: (비중, 종류, 최소, 최대) — coins 코인 · energy 에너지 · whet 숫돌 · item 물건(CHEST_ITEMS 에서 하나)
# 11/11 규완님 '상자 코인 획득량 소폭 감소' → 코인을 0.8배로 (나무 20~60 → 16~48 · 은 100~300 → 80~240 · 금 600~1,500 → 480~1,200)
CHEST_TABLE = {
    "wood": ((40, "coins", 16, 48), (30, "energy", 10, 15), (20, "whet", 1, 1), (10, "item", 1, 1)),
    "silver": ((35, "coins", 80, 240), (25, "energy", 25, 40), (25, "whet", 1, 2), (15, "item", 1, 1)),
    "gold": ((30, "coins", 480, 1_200), (20, "energy", 60, 100), (30, "whet", 3, 5), (20, "item", 1, 1)),
}
CHEST_ITEMS = {   # 상자에서 나오는 물건: (등급, 후보)
    "wood": (2, ("carrot", "potato", "corn", "egg")),
    "silver": (2, ("tomato", "strawberry", "wool")),
    "gold": (3, ("tomato", "strawberry", "wool")),
}
# 3.8.2 규완님 '현재 농사 경제 상황에 맞춘 상자 대폭 버프' (내 기본값): 보상 수 +1, 코인 약 6~8배, 더 높은 등급 물건,
# 상자가 조금 더 자주 (광맥 별 1 = 5% … 별 5 = 13%), 은 · 금 상자 비중도 올림
CHEST_WEIGHTS_382 = (70, 24, 6)
CHEST_REWARDS_382 = {"wood": 2, "silver": 3, "gold": 4}
CHEST_TABLE_382 = {
    "wood": ((40, "coins", 120, 300), (25, "energy", 20, 30), (20, "whet", 1, 2), (15, "item", 1, 2)),
    "silver": ((35, "coins", 600, 1_500), (20, "energy", 40, 60), (25, "whet", 2, 4), (20, "item", 1, 2)),
    "gold": ((30, "coins", 4_000, 10_000), (20, "energy", 100, 150), (25, "whet", 5, 8), (25, "item", 1, 2)),
}
CHEST_ITEMS_382 = {
    "wood": (2, ("tomato", "pumpkin", "strawberry", "wool", "cheese")),
    "silver": (3, ("grape", "watermelon", "peach", "truffle", "milk")),
    "gold": (4, ("peach", "pineapple", "mango", "truffle", "milk")),
}
DAILY_CHEST = "wood"                  # 하루(KST) 첫 거두기에 나무 상자 하나
LUCKY_FEE = {"wood": 100_000, "silver": 500_000, "gold": 2_000_000}   # 🍀 행운 열기(축구 봇 돈): 보상마다 두 번 뽑아 더 좋은 쪽
EXPAND_CHEST = "silver"               # 땅을 넓히면 은 상자 하나


def chest_p(ore: int, v382: bool = False) -> float:
    """수확 · 모으기 한 번에 상자가 나올 확률 (광맥 별 1 = 3% … 별 5 = 9% · 3.8.2 는 5% … 13%)."""
    return 0.05 + 0.02 * (ore - 1) if v382 else 0.03 + 0.015 * (ore - 1)


def _roll_reward(tier: str, rng, v382: bool = False) -> dict:
    table = (CHEST_TABLE_382 if v382 else CHEST_TABLE)[tier]
    _, kind, lo, hi = rng.choices(table, weights=[t[0] for t in table])[0]
    n = rng.randint(lo, hi)
    if kind == "item":
        t, pool = (CHEST_ITEMS_382 if v382 else CHEST_ITEMS)[tier]
        key = rng.choice(pool)
        return {"kind": "item", "key": key, "tier": t, "n": n, "worth": value(key, t) * n}
    return {"kind": kind, "n": n, "worth": n * {"coins": 1, "energy": 8, "whet": 250}[kind]}


def roll_chest(tier: str, lucky: bool, rng=random, v382: bool = False) -> list[dict]:
    """상자 보상. lucky 면 보상마다 두 번 뽑아 값이 큰 쪽. v382 = 3.8.2 상자 (농장 주인이 3.8.2 코인 지갑을 쓰면)."""
    out = []
    for _ in range((CHEST_REWARDS_382 if v382 else CHEST_REWARDS)[tier]):
        r = _roll_reward(tier, rng, v382)
        if lucky:
            r = max(r, _roll_reward(tier, rng, v382), key=lambda x: x["worth"])
        out.append(r)
    return out


# ───────────── 인수 (3.8.1 — 명문구단처럼: 금액을 제안하면 주인이 수락 · 적정 인수 금액 이상이면 바로) ─────────────
# 농장은 여러 개 가질 수 있다 (인수해도 원래 내 농장은 그대로). 인수하면 코인 · 창고 · 밭 · 동물 · 낫 · 상자가 통째로 넘어온다.
# 11/11 규완님 '농사 인수를 코인으로 하게 (많은 코인이 인수 시 필요하게)' → 금액은 농장 코인. 산 사람은 내 농장 하나의 코인으로 내고,
# 판 사람도 무조건 코인으로 받는다 (11/11 규완님) — 남은 농장에, 없으면 다음에 여는 농장에.
FARM_FAIR = 3.0               # 적정 인수 금액 기본 = 농장 가치 × 3 (코인)
FARM_ASK = (1.5, 5.0)         # 주인이 정할 수 있는 적정 인수 금액 범위 (농장 가치 ×)
FARM_OFFER_HOURS = 24         # 인수 제안은 24시간 뒤 만료
FARM_WORTH_MIN = 1_000        # 농장 가치 최소 (코인) — 막 연 농장도 적정 인수 금액이 너무 낮지 않게
NEW_FARM_COST = 1_000_000     # 11/12 규완님 '100만 코인 모으면 밭 하나 더' — 농장 하나의 코인으로 새 농장(밭)을 하나 더 연다
FARM_MAX_391 = 7              # 3.9.1 (11/21 규완님 '농장 개수 제한 7개로') — 새 밭 · 인수로 더 늘릴 수 없다 (이미 더 가진 건 그대로)


def farm_cap(uid: int, now_ts: float) -> Optional[int]:
    """한 사람이 가질 수 있는 농장 수 (3.9.1 공개 전엔 테스트 서버 사람만 7개, 아니면 제한 없음 = None)."""
    return FARM_MAX_391 if release.released_384(now_ts) or int(uid) in lobby.TESTERS else None


def _over_cap(con, uid: int, now_ts: float) -> Optional[dict]:
    cap = farm_cap(uid, now_ts)
    if cap is None:
        return None
    n = int(con.execute("SELECT COUNT(*) FROM farms WHERE owner=?", (int(uid),)).fetchone()[0])
    return {"ok": False, "reason": "max", "max": cap} if n >= cap else None
# 11/12 규완님: 정식 출시(3.8.1 공개) 때 베타 농사 기록은 지우고, 베타 참여자에게
# '베타 농장 가치의 50% + 금상자 은상자 나무상자 각각 한 개, 축구 봇 돈 3000만원' (beta_reset)
BETA_RATE = 0.5                                      # 사람마다 베타 농장 가치(가진 농장 전부 + 맡아 둔 코인) × → 새 농장 코인
BETA_CHESTS = {"gold": 1, "silver": 1, "wood": 1}    # 새 농장을 열 때 같이 (시작 선물 나무 상자와 따로)
BETA_WON = 30_000_000                                # 축구 봇 돈 — 초기화할 때 가방에 💵 농사 베타 보상금(beta_cash)으로
                                                     # (11/12 규완님 '3000만원 지갑에 바로 넣지 말고 가방에 넣어줘')
_FARM_DATA = ("farms", "farm_fields", "farm_items", "farm_seeds", "farm_threads", "farm_offers", "farm_held")   # 초기화하는 표 (상점은 둔다)


def farm_worth(f: dict) -> int:
    """농장 가치 (코인): 농장 코인(3.8.2 코인 지갑은 사람 것이라 빼고) + 창고 + 사 둔 씨앗 · 동물 + 기르는 동물 + 넓힌 땅 + 낫 강화에 든 값 (최소 FARM_WORTH_MIN)."""
    w = f.get("farm_coins", f["coins"]) + sum(value(k, t) * q for k, t, q in f["items"])
    if not f.get("bag"):   # 3.8.4 보관함을 하나로 합친 사람은 씨앗 · 동물 · 아이템이 사람 것이라 뺀다
        w += sum((shop_price(k) or 0) * q for k, q in {**f["seeds"], **f.get("tools", {})}.items())
        w += sum(CROPS[k][2] * ENCH_MULT * q for k, q in f.get("eseeds", {}).items())   # 3.8.4 인챈트한 씨앗
    w += sum(ANIMALS[p["key"]][2] for p in f["fields"] if p and p["kind"] == "animal")
    w += sum(expand_cost(p) for p in range(PLOTS_BASE, f["plots"]))
    w += sum(sickle_cost(lv) for lv in range(f["sickle"]))
    return max(FARM_WORTH_MIN, w)


def farm_ask_range(worth: int) -> tuple[int, int]:
    """주인이 정할 수 있는 적정 인수 금액 (코인)."""
    return round(worth * FARM_ASK[0]), round(worth * FARM_ASK[1])


def farm_fair(worth: int, ask: Optional[int] = None) -> int:
    """적정 인수 금액 (코인) — 주인이 정한 값(지금 가치의 범위 안으로), 안 정했으면 농장 가치 × FARM_FAIR."""
    if not ask:
        return round(worth * FARM_FAIR)
    lo, hi = farm_ask_range(worth)
    return min(hi, max(lo, int(ask)))


# PR #118 의 첫 /농사 (밭 6칸, 축구 봇 돈으로 씨앗) — 새 시스템으로 바꾸면서 심어 둔 씨앗 값을 돌려준다
_OLD_SEED = {"carrot": 10_000, "potato": 30_000, "corn": 100_000, "tomato": 300_000, "strawberry": 1_000_000,
             "watermelon": 3_000_000}


_TABLES = {   # 농장마다 번호(id) · 주인(owner) — 밭 · 창고 · 스레드는 농장 번호(farm_id)에 달린다
    "farms": ("CREATE TABLE IF NOT EXISTS farms (id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, land TEXT NOT NULL, "
              "coins INTEGER NOT NULL DEFAULT 0, energy REAL NOT NULL, energy_ts INTEGER NOT NULL, "
              "plots INTEGER NOT NULL, sickle INTEGER NOT NULL DEFAULT 0, whet INTEGER NOT NULL DEFAULT 0, "
              "c_wood INTEGER NOT NULL DEFAULT 0, c_silver INTEGER NOT NULL DEFAULT 0, "
              "c_gold INTEGER NOT NULL DEFAULT 0, plant_n INTEGER NOT NULL DEFAULT 0, daily_day INTEGER NOT NULL DEFAULT 0, "
              "created_ts INTEGER NOT NULL, ask INTEGER, fert_on INTEGER NOT NULL DEFAULT 1, "
              "scare_until INTEGER NOT NULL DEFAULT 0)"),
    "farm_fields": ("CREATE TABLE IF NOT EXISTS farm_fields (farm_id INTEGER NOT NULL, slot INTEGER NOT NULL, "
                    "kind TEXT NOT NULL, key TEXT NOT NULL, ready_ts INTEGER NOT NULL, PRIMARY KEY(farm_id, slot))"),
    "farm_items": ("CREATE TABLE IF NOT EXISTS farm_items (farm_id INTEGER NOT NULL, key TEXT NOT NULL, "
                   "tier INTEGER NOT NULL, qty INTEGER NOT NULL, PRIMARY KEY(farm_id, key, tier))"),
    "farm_threads": ("CREATE TABLE IF NOT EXISTS farm_threads (farm_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, "
                     "thread_id INTEGER NOT NULL, PRIMARY KEY(farm_id, guild_id))"),
    # 농사 상점에서 사 둔 씨앗 · 동물 (심거나 입양하면 줄어든다)
    "farm_seeds": ("CREATE TABLE IF NOT EXISTS farm_seeds (farm_id INTEGER NOT NULL, key TEXT NOT NULL, "
                   "qty INTEGER NOT NULL, PRIMARY KEY(farm_id, key))"),
    # 서버마다 하나 있는 농사 상점 스레드
    "farm_shops": ("CREATE TABLE IF NOT EXISTS farm_shops (guild_id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, "
                   "msg_id INTEGER, ver INTEGER NOT NULL DEFAULT 0)"),
}


class FarmDB(EconomyDB):
    """농장 데이터 — 지갑과 같은 DB 라서 환전 · 행운 값 · 인수 대금이 한 트랜잭션으로 끝난다.
    농장마다 번호(farms.id)가 있고 주인(owner)은 바뀔 수 있다. 농장을 바꾸는 일은 모두 (주인 id, 농장 번호) 로 부르고,
    주인이 아니면 reason: none."""

    def _init_db(self):
        super()._init_db()
        con = self._connect()
        try:
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='farm_plots'").fetchone():
                for uid, crop in con.execute("SELECT user_id, crop FROM farm_plots").fetchall():
                    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (uid,))
                    con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (_OLD_SEED.get(crop, 0), uid))
                con.execute("DROP TABLE farm_plots")
            self._to_multi(con)
            for ddl in _TABLES.values():
                con.execute(ddl)
            cols = {r[1] for r in con.execute("PRAGMA table_info(farms)")}
            if "daily_day" not in cols:
                con.execute("ALTER TABLE farms ADD COLUMN daily_day INTEGER NOT NULL DEFAULT 0")
            if "ask" not in cols:
                con.execute("ALTER TABLE farms ADD COLUMN ask INTEGER")
            if "fert_on" not in cols:
                con.execute("ALTER TABLE farms ADD COLUMN fert_on INTEGER NOT NULL DEFAULT 1")
            if "scare_until" not in cols:
                con.execute("ALTER TABLE farms ADD COLUMN scare_until INTEGER NOT NULL DEFAULT 0")
            if "last_plant" not in cols:   # 3.8.4 마지막으로 심은 작물 · 입양한 동물 (다시 심기 버튼)
                con.execute("ALTER TABLE farms ADD COLUMN last_plant TEXT")
            if "ingot" not in cols:   # 3.8.4 숫돌 주괴
                con.execute("ALTER TABLE farms ADD COLUMN ingot INTEGER NOT NULL DEFAULT 0")
            if "ench" not in {r[1] for r in con.execute("PRAGMA table_info(farm_fields)")}:   # 3.8.4 인챈트 씨앗을 심은 칸
                con.execute("ALTER TABLE farm_fields ADD COLUMN ench INTEGER NOT NULL DEFAULT 0")
            if "auto" not in {r[1] for r in con.execute("PRAGMA table_info(farm_fields)")}:   # 3.9.2 거두지 않고 쌓은 생산품 수
                con.execute("ALTER TABLE farm_fields ADD COLUMN auto INTEGER NOT NULL DEFAULT 0")
            fcols = {r[1] for r in con.execute("PRAGMA table_info(farm_fields)")}
            if "pet" not in fcols:   # 3.9.2 동물 이름 · 친밀도
                con.execute("ALTER TABLE farm_fields ADD COLUMN pet TEXT")
            if "love" not in fcols:
                con.execute("ALTER TABLE farm_fields ADD COLUMN love INTEGER NOT NULL DEFAULT 0")
            if "deco" not in cols:   # 3.9.2 걸어 둔 꾸미기
                con.execute("ALTER TABLE farms ADD COLUMN deco TEXT")
            con.execute("CREATE TABLE IF NOT EXISTS farm_decos (owner INTEGER NOT NULL, key TEXT NOT NULL, PRIMARY KEY(owner, key))")
            con.execute("CREATE TABLE IF NOT EXISTS farm_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, from_uid INTEGER NOT NULL, "
                        "from_fid INTEGER NOT NULL, to_uid INTEGER NOT NULL, give_key TEXT NOT NULL, give_tier INTEGER NOT NULL, "
                        "give_qty INTEGER NOT NULL, want_key TEXT NOT NULL, want_qty INTEGER NOT NULL, ts INTEGER NOT NULL, "
                        "status TEXT NOT NULL DEFAULT 'open')")
            con.execute("CREATE TABLE IF NOT EXISTS farm_exch (owner INTEGER PRIMARY KEY, day_key INTEGER NOT NULL, won INTEGER NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS farm_staff (farm_id INTEGER PRIMARY KEY, tier TEXT NOT NULL, hired_ts INTEGER NOT NULL, "
                        "paid_until INTEGER NOT NULL, next_ts INTEGER NOT NULL, work INTEGER NOT NULL DEFAULT 0, "
                        "wages INTEGER NOT NULL DEFAULT 0)")
            con.execute("CREATE TABLE IF NOT EXISTS farm_upkeep (owner INTEGER PRIMARY KEY, day_key INTEGER NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS farm_prog (owner INTEGER PRIMARY KEY, xp INTEGER NOT NULL DEFAULT 0)")
            con.execute("CREATE TABLE IF NOT EXISTS farm_dex (owner INTEGER NOT NULL, key TEXT NOT NULL, tier INTEGER NOT NULL, "
                        "ts INTEGER NOT NULL, PRIMARY KEY(owner, key))")
            con.execute("CREATE TABLE IF NOT EXISTS farm_orders (owner INTEGER NOT NULL, week INTEGER NOT NULL, idx INTEGER NOT NULL, "
                        "key TEXT NOT NULL, qty INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(owner, week, idx))")
            if "name" not in cols:   # 3.8.2 농장 이름 (주인이 정한다, 없으면 'ㅇㅇ의 농장')
                con.execute("ALTER TABLE farms ADD COLUMN name TEXT")
            shop_cols = {r[1] for r in con.execute("PRAGMA table_info(farm_shops)")}
            if "msg_id" not in shop_cols:
                con.execute("ALTER TABLE farm_shops ADD COLUMN msg_id INTEGER")
            if "ver" not in shop_cols:
                con.execute("ALTER TABLE farm_shops ADD COLUMN ver INTEGER NOT NULL DEFAULT 0")
            con.execute("CREATE INDEX IF NOT EXISTS farms_owner ON farms(owner)")
            con.execute("CREATE INDEX IF NOT EXISTS farm_threads_tid ON farm_threads(thread_id)")
            con.execute("CREATE TABLE IF NOT EXISTS farm_offers (id INTEGER PRIMARY KEY AUTOINCREMENT, farm_id INTEGER NOT NULL, "
                        "buyer_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, amount INTEGER NOT NULL, ts INTEGER NOT NULL, "
                        "status TEXT NOT NULL DEFAULT 'open', pay_fid INTEGER)")
            if "pay_fid" not in {r[1] for r in con.execute("PRAGMA table_info(farm_offers)")}:
                # 11/11 인수가 축구 봇 돈(원) → 농장 코인으로: 원으로 낸 옛 제안은 닫고, 원으로 정한 적정 인수 금액은 지운다
                con.execute("ALTER TABLE farm_offers ADD COLUMN pay_fid INTEGER")
                con.execute("UPDATE farm_offers SET status='closed' WHERE status='open'")
                con.execute("UPDATE farms SET ask=NULL")
            con.execute("CREATE INDEX IF NOT EXISTS farm_offers_farm ON farm_offers(farm_id, status)")
            # 농장을 판 사람이 남은 농장이 없을 때 받은 인수 대금 (코인) — 다음에 여는 농장에 들어간다
            # (_TABLES 에 넣지 않는다: user_id 열이 있으면 _to_multi 가 옛 표로 본다 → 열 이름도 owner)
            con.execute("CREATE TABLE IF NOT EXISTS farm_held (owner INTEGER PRIMARY KEY, coins INTEGER NOT NULL)")
            # 3.8.4 사람마다 보관함 하나 — 숫돌 · 상자 · 숫돌 주괴 (farm_stash) 와 사 둔 씨앗 · 동물 · 아이템 (farm_bag)
            con.execute("CREATE TABLE IF NOT EXISTS farm_stash (owner INTEGER PRIMARY KEY, whet INTEGER NOT NULL DEFAULT 0, "
                        "c_wood INTEGER NOT NULL DEFAULT 0, c_silver INTEGER NOT NULL DEFAULT 0, c_gold INTEGER NOT NULL DEFAULT 0, "
                        "ingot INTEGER NOT NULL DEFAULT 0)")
            con.execute("CREATE TABLE IF NOT EXISTS farm_bag (owner INTEGER NOT NULL, key TEXT NOT NULL, qty INTEGER NOT NULL, "
                        "PRIMARY KEY(owner, key))")
            # 3.8.2 사람마다 코인 지갑 하나 (농장마다 따로 두지 않는다)
            con.execute("CREATE TABLE IF NOT EXISTS farm_purse (owner INTEGER PRIMARY KEY, coins INTEGER NOT NULL)")
            # 3.9.2 코인 흐름 기록 (돈 흐름 기록과 같이, 30일 보관)
            con.execute("CREATE TABLE IF NOT EXISTS coin_log (owner INTEGER NOT NULL, delta INTEGER NOT NULL, "
                        "coins INTEGER NOT NULL, ts INTEGER NOT NULL)")
            con.execute("CREATE INDEX IF NOT EXISTS coin_log_ts ON coin_log(ts)")
            con.execute("CREATE TRIGGER IF NOT EXISTS coin_log_upd AFTER UPDATE OF coins ON farm_purse "
                        "WHEN NEW.coins != OLD.coins BEGIN INSERT INTO coin_log(owner, delta, coins, ts) "
                        "VALUES(NEW.owner, NEW.coins - OLD.coins, NEW.coins, CAST(strftime('%s','now') AS INTEGER)); END")
            # 11/12 베타 보상 상자 · 쿠폰 숫돌도 맡아 둔다 (beta = 베타 보상)
            held_cols = {r[1] for r in con.execute("PRAGMA table_info(farm_held)")}
            for c in ("c_wood", "c_silver", "c_gold", "whet", "beta"):
                if c not in held_cols:
                    con.execute(f"ALTER TABLE farm_held ADD COLUMN {c} INTEGER NOT NULL DEFAULT 0")
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _to_multi(con):
        """3.8.1 농장을 여러 개 가질 수 있게: 사람마다 농장 하나(user_id)였던 옛 표 → 농장 번호(id) + 주인(owner).
        옛 농장의 번호는 원래 주인 id 그대로라서 밭 · 창고 · 스레드는 값을 그대로 옮긴다.
        한 트랜잭션이라 중간에 실패하면 아무것도 바뀌지 않는다."""
        def cols(t):
            return [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
        old = [t for t in _TABLES if "user_id" in cols(t)]
        if not old:
            return
        con.commit()
        con.execute("BEGIN")
        try:
            for t in old:
                con.execute(f"ALTER TABLE {t} RENAME TO {t}_one")
                con.execute(_TABLES[t])
                keep = ", ".join(c for c in cols(f"{t}_one") if c != "user_id")
                new = "id, owner" if t == "farms" else "farm_id"
                con.execute(f"INSERT INTO {t}({new}, {keep}) SELECT {'user_id, user_id' if t == 'farms' else 'user_id'}, {keep} "
                            f"FROM {t}_one")
                con.execute(f"DROP TABLE {t}_one")
            con.commit()
        except Exception:
            con.rollback()
            raise

    # ───────────── 읽기 ─────────────
    @staticmethod
    def _farm(con, fid: int, now_ts: int) -> Optional[dict]:
        r = con.execute("SELECT land, coins, energy, energy_ts, plots, sickle, whet, c_wood, c_silver, c_gold, plant_n, "
                        "daily_day, owner, ask, fert_on, scare_until, name, ingot, last_plant, deco FROM farms WHERE id=?", (int(fid),)).fetchone()
        if not r or r[0] not in LANDS:
            return None
        res = LANDS[r[0]][3]
        purse = FarmDB._purse(con, int(r[12]))   # 3.8.2: 코인 지갑을 쓰는 주인이면 코인은 지갑 (농장 코인은 0)
        every = (ENERGY_SEC * time_mult(res["water"]) / lobby.mult("farm_energy", int(r[12]))   # 3.8.2 /로비 공약
                 / burning.mult("farm_energy", int(r[12]), now_ts))                         # 3.8.4 수능 버닝
        e = float(r[2])
        emax = energy_max(int(r[4]), purse is not None)
        if e < emax:
            e = min(float(emax), e + max(0, now_ts - int(r[3])) / every)
        if purse is not None:
            FarmDB._auto_animals(con, int(fid), res, int(r[5]), now_ts, owner=int(r[12]))
        fields: list[Optional[dict]] = [None] * int(r[4])
        for slot, kind, key, ready, ench, pet, love in con.execute(
                "SELECT slot, kind, key, ready_ts, ench, pet, love FROM farm_fields WHERE farm_id=?", (fid,)):
            if 0 <= slot < len(fields) and key in (CROPS if kind == "crop" else ANIMALS):
                fields[slot] = {"kind": kind, "key": key, "ready_ts": int(ready), "ready": now_ts >= int(ready),
                                "ench": bool(ench) and kind == "crop", "pet": pet, "love": int(love or 0)}
        items = [(k, int(t), int(q)) for k, t, q in con.execute(
            "SELECT key, tier, qty FROM farm_items WHERE farm_id=? AND qty>0", (fid,)) if k in ITEMS]
        order = list(ITEMS)
        items.sort(key=lambda x: (order.index(x[0]), x[1]))
        stash = FarmDB._sweep(con, int(r[12]))   # 3.8.4: 보관함을 하나로 합친 주인이면 농장마다 따로 쌓인 것을 보관함으로
        if stash is None:
            rows = con.execute("SELECT key, qty FROM farm_seeds WHERE farm_id=? AND qty>0", (fid,)).fetchall()
        else:
            rows = con.execute("SELECT key, qty FROM farm_bag WHERE owner=? AND qty>0", (int(r[12]),)).fetchall()
        bought = {k: int(q) for k, q in rows if shop_price(k)}
        eseeds = {k[:-len(ENCH)]: int(q) for k, q in rows if k.endswith(ENCH) and k[:-len(ENCH)] in CROPS}   # 3.8.4 인챈트한 씨앗
        seeds = {k: q for k, q in bought.items() if k not in TOOLS}
        tools = {k: bought[k] for k in TOOLS if k in bought}   # 상점에서 사 둔 아이템 (같은 표 farm_seeds)
        if stash is not None:
            r = list(r)
            r[6], r[7], r[8], r[9], r[17] = stash["whet"], stash["c_wood"], stash["c_silver"], stash["c_gold"], stash["ingot"]
        f = {"id": int(fid), "name": r[16] or None, "deco": r[19] if r[19] in DECOS else None, "seeds": seeds, "eseeds": eseeds, "ingot": int(r[17] or 0), "bag": stash is not None,
             "last_plant": r[18] if r[18] in CROPS or r[18] in ANIMALS else None, "tools": tools, "fert_on": bool(r[14]), "scare_until": int(r[15] or 0),
             "owner": int(r[12]), "land": r[0], "res": res, "coins": int(r[1]) if purse is None else purse,
             "farm_coins": int(r[1]), "v382": purse is not None, "cost": ENERGY_COST_382 if purse is not None else 1,
             "energy_f": e, "energy": int(e),
             "energy_next": None if e >= emax else int(now_ts + (1 - (e - int(e))) * every) + 1, "energy_max": emax,
             "energy_every": every, "plots": int(r[4]), "sickle": int(r[5]), "whet": int(r[6]),
             "chests": {"wood": int(r[7]), "silver": int(r[8]), "gold": int(r[9])}, "plant_n": int(r[10]),
             "fields": fields, "items": items, "daily_day": int(r[11]), "ask": int(r[13]) if r[13] else None,
             "animals": sum(1 for p in fields if p and p["kind"] == "animal"), "animal_cap": int(r[4]) // ANIMAL_PER_PLOTS}
        f["worth"] = farm_worth(f)
        f["fair"] = farm_fair(f["worth"], f["ask"])
        return f

    # ───────────── 3.8.4 보관함 하나로 (규완님 '숫돌, 상자, 씨앗, 동물, 아이템 통합') ─────────────
    # 코인 지갑(3.8.2)처럼 사람마다 하나. 농장에 쌓이는 곳(farms 의 whet · c_* · ingot 칸, farm_seeds)은 그대로 두고 받은 편지함처럼 쓰고,
    # 농장을 읽을 때마다 그 사람 농장 전부의 것을 보관함으로 옮긴다 (빼 쓴 것은 음수로 남았다가 같이 옮겨진다).
    _STASH = ("whet", "c_wood", "c_silver", "c_gold", "ingot")

    @classmethod
    def _sweep(cls, con, owner: int) -> Optional[dict]:
        """owner 의 보관함 (합치지 않은 사람이면 None). 그 사람 농장들에 쌓인 것을 먼저 옮긴다."""
        if not con.execute("SELECT 1 FROM farm_stash WHERE owner=?", (int(owner),)).fetchone():
            return None
        cols = ", ".join(cls._STASH)
        for row in con.execute(f"SELECT id, {cols} FROM farms WHERE owner=?", (int(owner),)).fetchall():
            if any(row[1:]):
                con.execute(f"UPDATE farm_stash SET {', '.join(f'{c} = {c} + ?' for c in cls._STASH)} WHERE owner=?",
                            (*[int(x) for x in row[1:]], int(owner)))
                con.execute(f"UPDATE farms SET {', '.join(f'{c} = 0' for c in cls._STASH)} WHERE id=?", (int(row[0]),))
        for fid, key, qty in con.execute("SELECT s.farm_id, s.key, s.qty FROM farm_seeds s JOIN farms f ON f.id = s.farm_id "
                                         "WHERE f.owner=? AND s.qty != 0", (int(owner),)).fetchall():
            con.execute("INSERT INTO farm_bag(owner, key, qty) VALUES(?,?,?) ON CONFLICT(owner, key) DO UPDATE SET qty = qty + excluded.qty",
                        (int(owner), key, int(qty)))
            con.execute("UPDATE farm_seeds SET qty = 0 WHERE farm_id=? AND key=?", (int(fid), key))
        r = con.execute(f"SELECT {cols} FROM farm_stash WHERE owner=?", (int(owner),)).fetchone()
        return dict(zip(cls._STASH, (int(x) for x in r)))

    @staticmethod
    def _seed_add(con, fid: int, key: str, n: int):
        """농장 fid 의 사 둔 씨앗 · 동물 · 아이템 ± (보관함을 합친 사람은 다음에 읽을 때 보관함으로 옮겨진다)."""
        con.execute("INSERT INTO farm_seeds(farm_id, key, qty) VALUES(?,?,?) "
                    "ON CONFLICT(farm_id, key) DO UPDATE SET qty = qty + excluded.qty", (int(fid), key, int(n)))

    async def upgrade_384(self, uid: int) -> bool:
        """uid 의 숫돌 · 상자 · 씨앗 · 동물 · 아이템을 보관함 하나로 합친다 (이미 합쳤으면 False)."""
        def fn(con):
            if con.execute("SELECT 1 FROM farm_stash WHERE owner=?", (int(uid),)).fetchone():
                return False
            con.execute("INSERT INTO farm_stash(owner) VALUES(?)", (int(uid),))
            self._sweep(con, uid)
            return True
        return await self._tx(fn)

    async def upgrade_all_384(self) -> int:
        """3.8.4 공개: 농장이 있는 모든 사람의 보관함을 합친다 → 합친 사람 수."""
        def fn(con):
            n = 0
            for (o,) in con.execute("SELECT DISTINCT owner FROM farms").fetchall():
                if not con.execute("SELECT 1 FROM farm_stash WHERE owner=?", (int(o),)).fetchone():
                    con.execute("INSERT INTO farm_stash(owner) VALUES(?)", (int(o),))
                    self._sweep(con, int(o))
                    n += 1
            return n
        return await self._tx(fn)

    async def set_name(self, uid: int, fid: int, name: str, now_ts: int) -> dict:
        """농장 이름 정하기 (주인만, 11/17 규완님 'ㅇㅇ의 농장이 너무 많아지잖아. 구별할 수 있게 이름을 설정해').
        빈 이름이면 원래대로('ㅇㅇ의 농장'). reason: none / long."""
        name = " ".join(str(name).split())
        def fn(con):
            if not self._mine(con, uid, fid, now_ts):
                return {"ok": False, "reason": "none"}
            if len(name) > NAME_MAX:
                return {"ok": False, "reason": "long"}
            con.execute("UPDATE farms SET name=? WHERE id=?", (name or None, int(fid)))
            return {"ok": True, "name": name or None}
        return await self._tx(fn)

    @classmethod
    def _mine(cls, con, uid: int, fid: int, now_ts: int) -> Optional[dict]:
        """uid 가 주인인 농장 fid (아니면 None)."""
        f = cls._farm(con, fid, now_ts)
        return f if f and f["owner"] == int(uid) else None

    # ───────────── 3.8.2 코인 지갑 (규완님 '농장 인수에 코인이 분리되는데 통합하기') ─────────────
    # 사람마다 코인 하나 (farm_purse). 지갑이 있는 사람은 농장마다 코인을 따로 두지 않는다 (farms.coins = 0).
    # 공개 전엔 테스트 서버에서 농사를 쓴 사람만 지갑으로 옮긴다 (upgrade) — 지갑이 있는 주인의 농장은 3.8.2 규칙 (에너지 · 상자).
    @staticmethod
    def _purse(con, owner: int) -> Optional[int]:
        r = con.execute("SELECT coins FROM farm_purse WHERE owner=?", (int(owner),)).fetchone()
        return int(r[0]) if r else None

    @classmethod
    def _coins(cls, con, fid: int) -> int:
        """농장 fid 에서 쓸 수 있는 코인 (지갑이 있는 주인이면 지갑)."""
        r = con.execute("SELECT owner, coins FROM farms WHERE id=?", (int(fid),)).fetchone()
        if not r:
            return 0
        p = cls._purse(con, int(r[0]))
        return int(r[1]) if p is None else p

    @classmethod
    def _add_coins(cls, con, fid: int, delta: int):
        """농장 fid 의 코인 ± (지갑이 있는 주인이면 지갑에)."""
        r = con.execute("SELECT owner FROM farms WHERE id=?", (int(fid),)).fetchone()
        if r and cls._purse(con, int(r[0])) is not None:
            con.execute("UPDATE farm_purse SET coins = coins + ? WHERE owner=?", (int(delta), int(r[0])))
        else:
            con.execute("UPDATE farms SET coins = coins + ? WHERE id=?", (int(delta), int(fid)))

    @staticmethod
    def _upgrade(con, uid: int) -> Optional[int]:
        """uid 의 농장 코인과 맡아 둔 코인을 지갑 하나로 합친다 (이미 지갑이면 그대로). → 옮긴 코인 (이미 지갑이면 None)."""
        if con.execute("SELECT 1 FROM farm_purse WHERE owner=?", (int(uid),)).fetchone():
            return None
        farms = int(con.execute("SELECT COALESCE(SUM(coins), 0) FROM farms WHERE owner=?", (int(uid),)).fetchone()[0])
        held = con.execute("SELECT coins FROM farm_held WHERE owner=?", (int(uid),)).fetchone()
        held = int(held[0]) if held else 0
        con.execute("INSERT INTO farm_purse(owner, coins) VALUES(?, ?)", (int(uid), farms + held))
        con.execute("UPDATE farms SET coins = 0 WHERE owner=?", (int(uid),))
        con.execute("UPDATE farm_held SET coins = 0 WHERE owner=?", (int(uid),))
        return farms + held

    async def upgrade(self, uid: int) -> Optional[int]:
        return await self._tx(lambda con: self._upgrade(con, uid))

    async def upgrade_all(self) -> int:
        """3.8.2 공개: 농장 · 맡아 둔 코인이 있는 모든 사람을 지갑으로 → 옮긴 사람 수."""
        def fn(con):
            owners = {int(r[0]) for r in con.execute("SELECT owner FROM farms UNION SELECT owner FROM farm_held")}
            return sum(1 for o in owners if self._upgrade(con, o) is not None)
        return await self._tx(fn)

    @staticmethod
    def _spend(con, fid: int, f: dict, n: int, now_ts: int):
        """에너지 n 을 쓴다 (지금까지 찬 만큼을 반영해서 저장)."""
        f["energy_f"] -= n
        f["energy"] = int(f["energy_f"])
        con.execute("UPDATE farms SET energy=?, energy_ts=? WHERE id=?", (f["energy_f"], int(now_ts), fid))

    @staticmethod
    def _over_species(con, owner: int) -> set:
        """3.9.2 같은 동물 한도(SPECIES_CAP_392)를 넘는 동물 칸들 {(농장 번호, 칸)} — 먼저 연 농장 · 앞 칸부터 한도 안으로 친다."""
        seen: dict = {}
        out = set()
        for fid, slot, key in con.execute("SELECT f.farm_id, f.slot, f.key FROM farm_fields f JOIN farms m ON m.id = f.farm_id "
                                          "WHERE m.owner=? AND f.kind='animal' ORDER BY m.created_ts, m.id, f.slot", (int(owner),)):
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > SPECIES_CAP_392:
                out.add((int(fid), int(slot)))
        return out

    @staticmethod
    def _pfeed_have(con, owner: int) -> int:
        r = con.execute("SELECT qty FROM farm_bag WHERE owner=? AND key='pfeed'", (int(owner),)).fetchone()
        s = con.execute("SELECT COALESCE(SUM(s.qty), 0) FROM farm_seeds s JOIN farms f ON f.id = s.farm_id "
                        "WHERE f.owner=? AND s.key='pfeed'", (int(owner),)).fetchone()
        return (int(r[0]) if r else 0) + int(s[0])

    @staticmethod
    def _auto_animals(con, fid: int, res: dict, sickle: int, now_ts: int, rng=random, owner: Optional[int] = None):
        """3.8.2 (11/17 규완님 '거두기를 누르지 않아도 생성될 수 있으면'): 동물 생산품은 다 만들어지는 대로 창고에 들어간다.
        농장을 볼 때마다 그동안 만든 개수를 계산해 넣는다 (에너지 · 상자 없음, ★2 확률은 거둘 때와 같음).
        3.9.2 (v392 주인): 거두지 않고 쌓는 건 마리당 AUTO_CAP_392 개까지 (그 뒤로는 다 만든 채로 기다린다 → 거두면 다시),
        같은 동물 한도를 넘는 동물은 만들지 않고, 소 · 들소 · 공작은 하나 만들 때마다 고급 사료 하나 (없으면 멈춘다)."""
        hp = high_p(sickle)
        new = owner is not None and v392(owner, now_ts)
        over = FarmDB._over_species(con, owner) if new else set()
        feed = FarmDB._pfeed_have(con, owner) if new else 0
        used_feed = 0
        for slot, key, ready, auto, love in con.execute("SELECT slot, key, ready_ts, auto, love FROM farm_fields WHERE farm_id=? "
                                                        "AND kind='animal' AND ready_ts<=?", (fid, int(now_ts))).fetchall():
            if key not in ANIMALS:
                continue
            every = max(1, int(ANIMALS[key][4] * time_mult(res["grass"]) * (love_mult(love) if new else 1)))
            n = 1 + (int(now_ts) - int(ready)) // every
            if new:
                if (int(fid), int(slot)) in over:
                    continue
                n = min(n, AUTO_CAP_392 - int(auto))
                if key in PFEED_ANIMALS:
                    n = min(n, feed - used_feed)
                if n <= 0:
                    continue
                if key in PFEED_ANIMALS:
                    used_feed += n
                con.execute("UPDATE farm_fields SET auto = auto + ?, love = MIN(?, love + ?) WHERE farm_id=? AND slot=?",
                            (n, LOVE_MAX, n, fid, slot))   # 3.9.2 친밀도: 만들 때마다 +1
            two = sum(1 for _ in range(n) if rng.random() < hp)
            prod = ANIMALS[key][3]
            if n - two:
                FarmDB._give(con, fid, prod, 1, n - two)
            if two:
                FarmDB._give(con, fid, prod, 2, two)
            con.execute("UPDATE farm_fields SET ready_ts=? WHERE farm_id=? AND slot=?", (int(ready) + n * every, fid, slot))
        if used_feed:
            FarmDB._use_pfeed(con, owner, used_feed)

    @staticmethod
    def _use_pfeed(con, owner: int, n: int):
        """고급 사료 n 개를 먹인다 — 보관함(farm_bag)부터, 모자라면 농장마다 사 둔 것에서."""
        r = con.execute("SELECT qty FROM farm_bag WHERE owner=? AND key='pfeed'", (int(owner),)).fetchone()
        take = min(n, int(r[0]) if r else 0)
        if take:
            con.execute("UPDATE farm_bag SET qty = qty - ? WHERE owner=? AND key='pfeed'", (take, int(owner)))
        n -= take
        for fid, q in con.execute("SELECT s.farm_id, s.qty FROM farm_seeds s JOIN farms f ON f.id = s.farm_id "
                                  "WHERE f.owner=? AND s.key='pfeed' AND s.qty > 0", (int(owner),)).fetchall():
            if n <= 0:
                break
            t = min(n, int(q))
            con.execute("UPDATE farm_seeds SET qty = qty - ? WHERE farm_id=? AND key='pfeed'", (t, int(fid)))
            n -= t

    @staticmethod
    def _give(con, fid: int, key: str, tier: int, n: int):
        con.execute("INSERT INTO farm_items(farm_id, key, tier, qty) VALUES(?,?,?,?) "
                    "ON CONFLICT(farm_id, key, tier) DO UPDATE SET qty = qty + excluded.qty", (fid, key, tier, n))
        if n > 0:   # 3.9.2 도감: 처음 얻은 물건 · 가장 높은 등급
            con.execute("INSERT INTO farm_dex(owner, key, tier, ts) SELECT owner, ?, ?, CAST(strftime('%s','now') AS INTEGER) "
                        "FROM farms WHERE id=? ON CONFLICT(owner, key) DO UPDATE SET tier = MAX(tier, excluded.tier)",
                        (key, int(tier), int(fid)))

    # ───────────── 3.9.2 농장 레벨 ─────────────
    @staticmethod
    def _xp_have(con, owner: int) -> int:
        r = con.execute("SELECT xp FROM farm_prog WHERE owner=?", (int(owner),)).fetchone()
        return int(r[0]) if r else 0

    @classmethod
    def _add_xp(cls, con, owner: int, fid: int, n: int) -> list[int]:
        """경험치 +n. 오른 레벨마다 상자를 농장 fid 에 (5레벨마다 금 상자, 나머지는 은 상자). → 새로 오른 레벨들."""
        if n <= 0:
            return []
        before = level_of(cls._xp_have(con, owner))[0]
        con.execute("INSERT INTO farm_prog(owner, xp) VALUES(?, ?) ON CONFLICT(owner) DO UPDATE SET xp = xp + excluded.xp",
                    (int(owner), int(n)))
        after = level_of(cls._xp_have(con, owner))[0]
        ups = list(range(before + 1, after + 1))
        for lv in ups:
            c = "gold" if lv % 5 == 0 else "silver"
            con.execute(f"UPDATE farms SET c_{c} = c_{c} + 1 WHERE id=?", (int(fid),))
        return ups

    async def level(self, owner: int) -> dict:
        """→ {xp, level, cur, need}."""
        def fn(con):
            xp = self._xp_have(con, owner)
            lv, cur, need = level_of(xp)
            return {"xp": xp, "level": lv, "cur": cur, "need": need}
        return await self._tx(fn)

    # ───────────── 3.9.2 도감 ─────────────
    async def dex(self, owner: int) -> dict[str, int]:
        """owner 가 얻어 본 물건 → 가장 높은 등급. 지금 창고에 있는 것도 넣는다 (도감이 생기기 전에 얻은 것)."""
        def fn(con):
            con.execute("INSERT INTO farm_dex(owner, key, tier, ts) SELECT f.owner, i.key, MAX(i.tier), CAST(strftime('%s','now') AS INTEGER) "
                        "FROM farm_items i JOIN farms f ON f.id = i.farm_id WHERE f.owner=? AND i.qty > 0 GROUP BY i.key "
                        "ON CONFLICT(owner, key) DO UPDATE SET tier = MAX(tier, excluded.tier)", (int(owner),))
            return {k: int(t) for k, t in con.execute("SELECT key, tier FROM farm_dex WHERE owner=?", (int(owner),)) if k in ITEMS}
        return await self._tx(fn)

    # ───────────── 3.9.2 주간 납품 의뢰 ─────────────
    @staticmethod
    def _orders(con, owner: int, now_ts: int) -> list[dict]:
        wk = week_of(now_ts)
        rows = con.execute("SELECT idx, key, qty, done FROM farm_orders WHERE owner=? AND week=? ORDER BY idx", (int(owner), wk)).fetchall()
        if not rows:
            seen = [k for (k,) in con.execute("SELECT key FROM farm_dex WHERE owner=?", (int(owner),)) if k in base_items()]
            pool = seen if len(seen) >= ORDER_N else list(CROPS)[:5] + ["egg"]
            rng = random.Random(f"{int(owner)}:{wk}")
            keys = rng.sample(pool, ORDER_N)
            rows = []
            for i, k in enumerate(keys):
                q = max(5, min(300, round(ORDER_COINS[i] / max(1, ITEMS[k][2]))))
                con.execute("INSERT INTO farm_orders(owner, week, idx, key, qty) VALUES(?,?,?,?,?)", (int(owner), wk, i, k, q))
                rows.append((i, k, q, 0))
        return [{"idx": int(i), "key": k, "qty": int(q), "done": bool(d), "chest": ORDER_CHEST[int(i)]} for i, k, q, d in rows]

    async def orders(self, owner: int, now_ts: int) -> list[dict]:
        return await self._tx(lambda con: self._orders(con, owner, now_ts))

    async def deliver(self, uid: int, fid: int, idx: int, now_ts: int) -> dict:
        """의뢰 idx 에 농장 fid 창고의 물건을 낸다 (등급은 상관없이 낮은 등급부터 하나씩 센다).
        보상: 상자 하나 + 경험치 ORDER_XP. reason: none / idx / done / few(have, need)."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            o = next((x for x in self._orders(con, uid, now_ts) if x["idx"] == int(idx)), None)
            if not o:
                return {"ok": False, "reason": "idx"}
            if o["done"]:
                return {"ok": False, "reason": "done"}
            have = sorted((t, q) for k, t, q in f["items"] if k == o["key"])
            if sum(q for _, q in have) < o["qty"]:
                return {"ok": False, "reason": "few", "have": sum(q for _, q in have), "need": o["qty"], "key": o["key"]}
            left = o["qty"]
            for t, q in have:
                if left <= 0:
                    break
                use = min(q, left)
                con.execute("UPDATE farm_items SET qty = qty - ? WHERE farm_id=? AND key=? AND tier=?", (use, int(fid), o["key"], t))
                left -= use
            con.execute("DELETE FROM farm_items WHERE farm_id=? AND qty <= 0", (int(fid),))
            con.execute("UPDATE farm_orders SET done=1 WHERE owner=? AND week=? AND idx=?", (int(uid), week_of(now_ts), int(idx)))
            con.execute(f"UPDATE farms SET c_{o['chest']} = c_{o['chest']} + 1 WHERE id=?", (int(fid),))
            ups = self._add_xp(con, uid, fid, ORDER_XP)
            return {"ok": True, "key": o["key"], "qty": o["qty"], "chest": o["chest"], "xp": ORDER_XP, "levelup": ups}
        return await self._tx(fn)

    @staticmethod
    def _wallet(con, uid: int) -> int:
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (uid,))
        return int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (uid,)).fetchone()[0])

    @staticmethod
    def _open_offers(con, fid: int, now_ts: int) -> int:
        since = int(now_ts) - FARM_OFFER_HOURS * 3600
        return int(con.execute("SELECT COUNT(*) FROM farm_offers f JOIN farms m ON m.id=f.farm_id WHERE f.farm_id=? "
                               "AND f.status='open' AND f.owner_id=m.owner AND f.ts>?", (fid, since)).fetchone()[0])

    async def get(self, fid: int, now_ts: int) -> Optional[dict]:
        """농장 fid 의 상태 (없으면 None). balance = 주인의 축구 봇 돈 · offers = 받은 인수 제안 수."""
        def fn(con):
            f = self._farm(con, fid, now_ts)
            if f:
                f["balance"] = self._wallet(con, f["owner"])
                f["offers"] = self._open_offers(con, fid, now_ts)
            return f
        return await self._tx(fn)

    async def farms_of(self, uid: int, now_ts: int) -> list[dict]:
        """uid 가 가진 농장들 (먼저 연 순)."""
        def fn(con):
            ids = [int(r[0]) for r in con.execute("SELECT id FROM farms WHERE owner=? ORDER BY created_ts, id", (int(uid),))]
            return [f for f in (self._farm(con, i, now_ts) for i in ids) if f]
        return await self._tx(fn)

    async def create(self, uid: int, land: str, now_ts: int) -> dict:
        """땅을 골라 첫 농장을 연다 → id. 농장이 이미 있으면 reason: exists (id = 가진 농장 하나) · 없는 땅이면 land.
        (농장을 더 가지려면 인수한다)"""
        def fn(con):
            if land not in LANDS:
                return {"ok": False, "reason": "land"}
            have = con.execute("SELECT id FROM farms WHERE owner=? ORDER BY created_ts, id", (int(uid),)).fetchone()
            if have:
                return {"ok": False, "reason": "exists", "id": int(have[0])}
            h = con.execute("SELECT coins, c_wood, c_silver, c_gold, whet, beta FROM farm_held WHERE owner=?",
                            (int(uid),)).fetchone() or (0, 0, 0, 0, 0, 0)
            held, wood, silver, gold, whet, beta = (int(x) for x in h)
            purse = self._purse(con, uid)
            if purse is not None and held:   # 3.8.2 코인 지갑: 맡아 둔 코인은 지갑으로
                con.execute("UPDATE farm_purse SET coins = coins + ? WHERE owner=?", (held, int(uid)))
            cur = con.execute("INSERT INTO farms(owner, land, coins, energy, energy_ts, plots, c_wood, c_silver, c_gold, whet, "
                              "created_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                              (int(uid), land, 0 if purse is not None else held, ENERGY_MAX, int(now_ts), PLOTS_BASE, START_CHESTS.get("wood", 0) + wood,
                               silver, gold, whet, int(now_ts)))
            con.execute("DELETE FROM farm_held WHERE owner=?", (int(uid),))
            chests = {k: n for k, n in (("gold", gold), ("silver", silver), ("wood", wood)) if n}
            return {"ok": True, "id": int(cur.lastrowid), "held": held, "chests": chests, "whet": whet, "beta": bool(beta)}
        return await self._tx(fn)

    async def create_more(self, uid: int, land: str, pay_fid: int, now_ts: int) -> dict:
        """내 농장 pay_fid 의 코인 NEW_FARM_COST 를 내고 새 농장을 하나 더 연다 → id.
        reason: land / pay(내 농장이 아님) / coins(cost, have)."""
        def fn(con):
            if land not in LANDS:
                return {"ok": False, "reason": "land"}
            p = con.execute("SELECT owner FROM farms WHERE id=?", (int(pay_fid),)).fetchone()
            if not p or int(p[0]) != int(uid):
                return {"ok": False, "reason": "pay"}
            full = _over_cap(con, uid, now_ts)   # 3.9.1 농장은 7개까지
            if full:
                return full
            p = (p[0], self._coins(con, int(pay_fid)))
            if int(p[1]) < NEW_FARM_COST:
                return {"ok": False, "reason": "coins", "cost": NEW_FARM_COST, "have": int(p[1])}
            self._add_coins(con, int(pay_fid), -(NEW_FARM_COST))
            cur = con.execute("INSERT INTO farms(owner, land, energy, energy_ts, plots, c_wood, created_ts) VALUES(?,?,?,?,?,?,?)",
                              (int(uid), land, ENERGY_MAX, int(now_ts), PLOTS_BASE, START_CHESTS.get("wood", 0), int(now_ts)))
            return {"ok": True, "id": int(cur.lastrowid), "cost": NEW_FARM_COST, "left": int(p[1]) - NEW_FARM_COST}
        return await self._tx(fn)

    # ───────────── 정식 출시: 베타 농사 기록 초기화 ─────────────
    async def beta_reset(self, now_ts: int) -> dict:
        """베타 농사 기록을 모두 지우고(farm_beta_* 표에 그대로 백업) 베타 참여자에게 보상한다. 한 번만 (두 번째부턴 reason: done).
        보상: 축구 봇 돈 BETA_WON 은 가방에(beta_cash) 바로 · 코인(베타 농장 가치 × BETA_RATE) · BETA_CHESTS 는 맡아 뒀다가 새 농장에.
        공개 전에 쿠폰으로 받아 맡아 둔 숫돌은 그대로 남긴다. 농사 상점(farm_shops)은 지우지 않는다."""
        def fn(con):
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='farm_beta_rewards'").fetchone():
                return {"ok": False, "reason": "done"}
            worth: dict[int, int] = {}
            for fid, owner in con.execute("SELECT id, owner FROM farms").fetchall():
                f = self._farm(con, fid, now_ts)
                worth[int(owner)] = worth.get(int(owner), 0) + (farm_worth(f) if f else 0)
            whet = {}
            for owner, coins, w in con.execute("SELECT owner, coins, whet FROM farm_held").fetchall():
                if coins:   # 농장을 팔고 맡아 둔 코인도 베타 기록 (숫돌만 맡아 둔 사람은 쿠폰이라 참여가 아니다)
                    worth[int(owner)] = worth.get(int(owner), 0) + int(coins)
                if w:
                    whet[int(owner)] = int(w)
            for t in _FARM_DATA:
                con.execute(f"CREATE TABLE farm_beta_{t} AS SELECT * FROM {t}")
                con.execute(f"DELETE FROM {t}")
            con.execute("CREATE TABLE farm_beta_rewards (owner INTEGER PRIMARY KEY, worth INTEGER NOT NULL, "
                        "coins INTEGER NOT NULL, won INTEGER NOT NULL, ts INTEGER NOT NULL)")
            for owner, w in worth.items():
                coins = round(w * BETA_RATE)
                con.execute("INSERT INTO farm_held(owner, coins, c_wood, c_silver, c_gold, whet, beta) VALUES(?,?,?,?,?,?,1)",
                            (owner, coins, BETA_CHESTS["wood"], BETA_CHESTS["silver"], BETA_CHESTS["gold"],
                             whet.pop(owner, 0)))
                give_item(con, owner, "beta_cash", BETA_WON)
                con.execute("INSERT INTO farm_beta_rewards(owner, worth, coins, won, ts) VALUES(?,?,?,?,?)",
                            (owner, w, coins, BETA_WON, int(now_ts)))
            for owner, w in whet.items():   # 베타엔 농장이 없었지만 쿠폰 숫돌을 맡아 둔 사람
                con.execute("INSERT INTO farm_held(owner, coins, whet) VALUES(?, 0, ?)", (owner, w))
            return {"ok": True, "people": len(worth), "coins": sum(round(w * BETA_RATE) for w in worth.values())}
        return await self._tx(fn)

    async def take_beta_threads(self) -> list[tuple[int, int]]:
        """베타 농장 스레드 (guild_id, thread_id) 를 꺼내고 기록에서 뺀다 — 15:00 베타가 끝나면 디스코드에서 지운다.
        베타 초기화가 이미 끝났으면 빈 목록 (그 뒤의 스레드는 정식 출시 농장이다)."""
        def fn(con):
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='farm_beta_rewards'").fetchone():
                return []
            rows = [(int(g), int(t)) for g, t in con.execute("SELECT guild_id, thread_id FROM farm_threads")]
            con.execute("CREATE TABLE IF NOT EXISTS farm_beta_threads_gone AS SELECT * FROM farm_threads WHERE 0")
            con.execute("INSERT INTO farm_beta_threads_gone SELECT * FROM farm_threads")   # 백업 (초기화 때 farm_threads 는 이미 비어 있다)
            con.execute("DELETE FROM farm_threads")
            return rows
        return await self._tx(fn)

    # ───────────── 스레드 (농장마다 서버에 하나) ─────────────
    async def set_thread(self, fid: int, guild_id: int, thread_id: int):
        await self._tx(lambda con: con.execute(
            "INSERT OR REPLACE INTO farm_threads(farm_id, guild_id, thread_id) VALUES(?,?,?)", (fid, guild_id, thread_id)))

    async def get_thread(self, fid: int, guild_id: int) -> Optional[int]:
        def fn(con):
            r = con.execute("SELECT thread_id FROM farm_threads WHERE farm_id=? AND guild_id=?", (fid, guild_id)).fetchone()
            return int(r[0]) if r else None
        return await self._tx(fn)

    async def thread_farm(self, thread_id: int) -> Optional[int]:
        """스레드 → 농장 번호."""
        def fn(con):
            r = con.execute("SELECT farm_id FROM farm_threads WHERE thread_id=?", (thread_id,)).fetchone()
            return int(r[0]) if r else None
        return await self._tx(fn)

    async def thread_owner(self, thread_id: int) -> Optional[int]:
        """스레드 → 그 농장의 지금 주인."""
        def fn(con):
            r = con.execute("SELECT f.owner FROM farm_threads t JOIN farms f ON f.id=t.farm_id WHERE t.thread_id=?",
                            (thread_id,)).fetchone()
            return int(r[0]) if r else None
        return await self._tx(fn)

    async def threads_of(self, fid: int) -> list[int]:
        """농장 fid 의 스레드들 (서버마다)."""
        return await self._tx(lambda con: [int(r[0]) for r in con.execute(
            "SELECT thread_id FROM farm_threads WHERE farm_id=?", (fid,))])

    async def shop_threads(self) -> dict[int, tuple[int, Optional[int], int]]:
        """서버 → (농사 상점 스레드, 상점 글, 상점 화면 버전)."""
        return await self._tx(lambda con: {int(g): (int(t), int(m) if m else None, int(v)) for g, t, m, v in con.execute(
            "SELECT guild_id, thread_id, msg_id, ver FROM farm_shops")})

    async def set_shop_thread(self, guild_id: int, thread_id: int, msg_id: Optional[int] = None, ver: int = 0):
        await self._tx(lambda con: con.execute(
            "INSERT OR REPLACE INTO farm_shops(guild_id, thread_id, msg_id, ver) VALUES(?,?,?,?)",
            (guild_id, thread_id, msg_id, ver)))

    # ───────────── 사기 · 심기 · 입양 ─────────────
    async def set_plant_n(self, uid: int, fid: int, n: int):
        """한 번에 심을 칸 수 (0 = 빈 땅 모두)."""
        await self._tx(lambda con: con.execute("UPDATE farms SET plant_n=? WHERE id=? AND owner=?", (max(0, int(n)), fid, uid)))

    async def buy(self, uid: int, fid: int, key: str, n: int, now_ts: int) -> dict:
        """농사 상점: 씨앗 · 동물 n 개를 농장 fid 의 코인으로 사 둔다. reason: none / key / amount / coins(cost, have)."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            price = shop_price(key)
            if price is None or (key == "feed" and release.ctx_392()):   # 3.9.2 동물 사료는 상점에서 뺐다 (11/25 규완님)
                return {"ok": False, "reason": "key"}
            if n <= 0:
                return {"ok": False, "reason": "amount"}
            if f["coins"] < price * n:
                return {"ok": False, "reason": "coins", "cost": price * n, "have": f["coins"]}
            self._add_coins(con, fid, -(price * n))
            con.execute("INSERT INTO farm_seeds(farm_id, key, qty) VALUES(?,?,?) "
                        "ON CONFLICT(farm_id, key) DO UPDATE SET qty = qty + excluded.qty", (fid, key, n))
            return {"ok": True, "key": key, "count": n, "cost": price * n, "coins": f["coins"] - price * n,
                    "have": {**f["seeds"], **f["tools"]}.get(key, 0) + n}
        return await self._tx(fn)

    async def plant(self, uid: int, fid: int, key: str, now_ts: int) -> dict:
        """작물 심기 (plant_n 칸 · 에너지와 사 둔 씨앗이 되는 만큼) 또는 사 둔 동물 입양 (한 마리). 한 칸에 에너지 1.
        무료 씨앗(당근)은 씨앗 없이 심는다. reason: none / key / full / energy / seeds(사 둔 것이 없음) / cap(동물 한도)."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            if key not in CROPS and key not in ANIMALS:
                return {"ok": False, "reason": "key"}
            bought, have = shop_price(key) is not None, f["seeds"].get(key, 0)
            ehave = f["eseeds"].get(key, 0) if key in CROPS else 0   # 3.8.4 인챈트한 씨앗부터 심는다
            if bought and have + ehave <= 0:
                return {"ok": False, "reason": "seeds"}
            empty = [i for i, p in enumerate(f["fields"]) if p is None]
            if not empty:
                return {"ok": False, "reason": "full"}
            cost = f["cost"]   # 칸 하나에 쓰는 에너지 (3.8.2 는 2)
            if f["energy"] < cost:
                return {"ok": False, "reason": "energy"}
            animal = key in ANIMALS
            if animal:
                sec = ANIMALS[key][4] * time_mult(f["res"]["grass"])
                if f["animals"] >= f["animal_cap"]:
                    return {"ok": False, "reason": "cap", "cap": f["animal_cap"]}
                if v392(uid, now_ts) and int(con.execute(
                        "SELECT COUNT(*) FROM farm_fields f JOIN farms m ON m.id = f.farm_id WHERE m.owner=? AND f.kind='animal' "
                        "AND f.key=?", (int(uid), key)).fetchone()[0]) >= SPECIES_CAP_392:   # 3.9.2 같은 동물 한 사람당 5마리
                    return {"ok": False, "reason": "species", "cap": SPECIES_CAP_392}
                n = 1
            else:
                sec = (CROPS[key][3] * time_mult(f["res"]["soil"]) / lobby.mult("farm_grow", uid)   # 3.8.2 /로비 공약
                       / burning.mult("farm_grow", uid, now_ts))                                    # 3.8.4 수능 버닝
                if v392(uid, now_ts):
                    sec *= grow_mult(key, now_ts)                                                  # 3.9.2 계절 · 날씨
                n = min(f["plant_n"] or len(empty), len(empty), f["energy"] // cost)
                if bought:
                    n = min(n, have + ehave)
            en = min(n, ehave)   # 인챈트 씨앗으로 심는 칸
            ready = int(now_ts + sec)
            # 비료 (켜 두면): 작물 한 칸에 하나씩, 그 칸은 절반 시간에 자란다
            fert = min(n, f["tools"].get("fert", 0)) if not animal and f["fert_on"] else 0
            fert_ready = int(now_ts + sec * FERT_MULT)
            for i, slot in enumerate(empty[:n]):
                at = fert_ready if i < fert else ready
                if i < en:   # 인챈트 씨앗은 10% 늦게 자라고 (ENCH_SLOW) 버닝 '두 배 빨리 자람'을 받지 않는다
                    at = int(now_ts + (at - now_ts) * burning.mult("farm_grow", uid, now_ts) * ENCH_SLOW)
                con.execute("INSERT OR REPLACE INTO farm_fields(farm_id, slot, kind, key, ready_ts, ench) VALUES(?,?,?,?,?,?)",
                            (fid, slot, "animal" if animal else "crop", key, at, 1 if i < en else 0))
            if en:
                self._seed_add(con, fid, key + ENCH, -en)
            if bought and n - en:
                self._seed_add(con, fid, key, -(n - en))
            if fert:
                self._seed_add(con, fid, "fert", -fert)
            self._spend(con, fid, f, n * cost, now_ts)
            con.execute("UPDATE farms SET last_plant=? WHERE id=?", (key, fid))
            return {"ok": True, "key": key, "animal": animal, "count": n, "ready_ts": ready, "left": have - (n - en) if bought else None, "ench": en, "ench_left": ehave - en,
                    "fert": fert, "fert_ready": fert_ready, "fert_left": f["tools"].get("fert", 0) - fert}
        return await self._tx(fn)

    # ───────────── 수확 · 모으기 ─────────────
    async def harvest(self, uid: int, fid: int, now_ts: int, rng=random, slots: Optional[set] = None) -> dict:
        """다 자란 작물 수확 + 동물이 만든 것 모으기 (에너지가 되는 만큼 · 하나에 1).
        낫: 하나 더 나올 확률 · ★2 로 나올 확률. 광맥: 상자가 나올 확률. 하루(KST) 첫 거두기엔 나무 상자 하나 (daily).
        → {got: {(key, tier): 수}, extra, high, chests: [tier], daily, count, left(에너지가 모자라 남은 수)}.
        reason: none / nothing / energy."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            ready = [(i, p) for i, p in enumerate(f["fields"]) if p and p["ready"] and (slots is None or i in slots)]   # slots: 칸 버튼으로 그 칸만
            new = v392(uid, now_ts)
            hungry = capped = False
            if new:   # 3.9.2 같은 동물 한도를 넘는 동물은 거두지 못하고, 소 · 들소 · 공작은 고급 사료가 있어야 거둔다
                over = self._over_species(con, uid)
                feed = self._pfeed_have(con, uid)
                keep = []
                for i, p in ready:
                    if p["kind"] == "animal" and (fid, i) in over:
                        capped = True
                        continue
                    if p["kind"] == "animal" and p["key"] in PFEED_ANIMALS:
                        if feed <= 0:
                            hungry = True
                            continue
                        feed -= 1
                    keep.append((i, p))
                ready = keep
            if not ready:
                return {"ok": False, "reason": "nofeed" if hungry else "capped" if capped else "nothing"}
            n = min(len(ready), f["energy"] // f["cost"])
            if n <= 0:
                return {"ok": False, "reason": "energy"}
            got: dict = {}
            extra = high = mut = 0
            wx = weather_of(now_ts) if new else "sun"   # 3.9.2 날씨
            chests = []
            scare = f["scare_until"] > now_ts
            hp = high_p(f["sickle"]) + (SCARE_BONUS if scare else 0)
            for slot, p in ready[:n]:
                if p["kind"] == "crop":
                    key = p["key"] + (ENCH if p.get("ench") else "")   # 3.8.4 인챈트 씨앗을 심은 칸은 인챈트 작물
                    con.execute("DELETE FROM farm_fields WHERE farm_id=? AND slot=?", (fid, slot))
                else:
                    key = ANIMALS[p["key"]][3]
                    love = min(LOVE_MAX, p.get("love", 0) + 1) if new else p.get("love", 0)   # 3.9.2 친밀도
                    con.execute("UPDATE farm_fields SET ready_ts=?, auto=0, love=? WHERE farm_id=? AND slot=?",
                                (int(now_ts + ANIMALS[p["key"]][4] * time_mult(f["res"]["grass"]) * (love_mult(love) if new else 1)),
                                 love, fid, slot))
                tier = 2 if rng.random() < hp else 1
                qty = 2 if rng.random() < extra_p(f["sickle"]) else 1
                if new and not key.endswith(ENCH) and key + MUT in ITEMS and rng.random() < MUT_P * (2 if wx == "rainbow" else 1):   # 3.9.2 희귀 변이
                    key += MUT
                    mut += 1
                high += tier == 2
                extra += qty == 2
                got[(key, tier)] = got.get((key, tier), 0) + qty
                if rng.random() < chest_p(f["res"]["ore"], f["v382"]) * lobby.mult("farm_chest", uid) * (2 if wx == "wind" else 1):
                    chests.append(rng.choices(list(CHESTS), weights=CHEST_WEIGHTS_382 if f["v382"] else CHEST_WEIGHTS)[0])
            for (key, tier), qty in got.items():
                self._give(con, fid, key, tier, qty)
            if new:
                fed = sum(1 for _, p in ready[:n] if p["kind"] == "animal" and p["key"] in PFEED_ANIMALS)
                if fed:
                    self._use_pfeed(con, uid, fed)
            daily = f["daily_day"] != _kst_day(now_ts)
            if daily:
                con.execute("UPDATE farms SET daily_day=? WHERE id=?", (_kst_day(now_ts), fid))
            for c in chests + ([DAILY_CHEST] if daily else []):
                con.execute(f"UPDATE farms SET c_{c} = c_{c} + 1 WHERE id=?", (fid,))
            self._spend(con, fid, f, n * f["cost"], now_ts)
            burning.bump(con, uid, "harvest", n, now_ts)   # 3.9.1 버닝 미션 '농사 거두기'
            ups = self._add_xp(con, uid, fid, sum(got.values())) if new else []   # 3.9.2 농장 레벨
            return {"ok": True, "got": got, "extra": extra, "high": high, "chests": chests, "daily": daily, "count": n,
                    "left": len(ready) - n, "scare": scare, "mut": mut, "levelup": ups}
        return await self._tx(fn)

    # ───────────── 아이템 (농사 상점에서 사 둔 것) ─────────────
    async def set_fert(self, uid: int, fid: int, on: bool, now_ts: int) -> bool:
        """심을 때 비료를 쓸지 (주인만)."""
        def fn(con):
            if not self._mine(con, uid, fid, now_ts):
                return False
            con.execute("UPDATE farms SET fert_on=? WHERE id=?", (1 if on else 0, fid))
            return True
        return await self._tx(fn)

    async def use_tool(self, uid: int, fid: int, key: str, now_ts: int, n: int = 1) -> dict:
        """아이템 하나 쓰기 — drink(에너지 +30) · feed(동물 남은 시간 절반) · scarecrow(10분 ★2 확률 +). 비료는 심을 때 쓴다.
        reason: none / key / have(없음) / animals(기다리는 동물이 없음)."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            if key not in TOOLS or key in ("fert", "pfeed"):
                return {"ok": False, "reason": "key"}
            if f["tools"].get(key, 0) <= 0:
                return {"ok": False, "reason": "have"}
            used = max(1, min(int(n), f["tools"][key])) if key == "drink" else 1   # 3.8.2 에너지 음료는 여러 개 한 번에
            out = {"ok": True, "key": key, "left": f["tools"][key] - used, "used": used}
            if key == "drink":
                e = f["energy_f"] + DRINK_ENERGY * used   # 한도(ENERGY_MAX)를 넘겨서도 (저절로 찰 때만 한도까지)
                con.execute("UPDATE farms SET energy=?, energy_ts=? WHERE id=?", (e, int(now_ts), fid))
                out.update(energy=int(e), gained=int(e) - f["energy"])
            elif key == "feed":
                wait = [(i, p) for i, p in enumerate(f["fields"]) if p and p["kind"] == "animal" and not p["ready"]]
                if not wait:
                    return {"ok": False, "reason": "animals"}
                for i, p in wait:
                    con.execute("UPDATE farm_fields SET ready_ts=? WHERE farm_id=? AND slot=?",
                                (int(now_ts + (p["ready_ts"] - now_ts) * FEED_MULT), fid, i))
                out.update(count=len(wait))
            else:
                until = max(int(now_ts), f["scare_until"]) + SCARE_SEC
                con.execute("UPDATE farms SET scare_until=? WHERE id=?", (until, fid))
                out.update(until=until)
            self._seed_add(con, fid, key, -used)
            return out
        return await self._tx(fn)

    # ───────────── 합치기 · 판매 ─────────────
    async def merge(self, uid: int, fid: int, key: str, tier: int, now_ts: int) -> dict:
        """같은 물건 3개 → 한 등급 위 1개 (되는 만큼 · 한 번에 에너지 1). reason: none / max / few / energy."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            if key not in ITEMS or not 1 <= tier < TIER_MAX:
                return {"ok": False, "reason": "max"}
            have = next((q for k, t, q in f["items"] if k == key and t == tier), 0)
            if have < MERGE_NEED:
                return {"ok": False, "reason": "few"}
            n = min(have // MERGE_NEED, f["energy"] // f["cost"])
            if n <= 0:
                return {"ok": False, "reason": "energy"}
            con.execute("UPDATE farm_items SET qty = qty - ? WHERE farm_id=? AND key=? AND tier=?", (n * MERGE_NEED, fid, key, tier))
            self._give(con, fid, key, tier + 1, n)
            self._spend(con, fid, f, n * f["cost"], now_ts)
            return {"ok": True, "key": key, "tier": tier + 1, "made": n, "used": n * MERGE_NEED}
        return await self._tx(fn)

    async def sell(self, uid: int, fid: int, now_ts: int, picks: Optional[list[tuple[str, int]]] = None,
                   n: Optional[int] = None) -> dict:
        """창고의 물건을 코인으로 판다 (picks = [(key, tier)] · None 이면 전부).
        n = 고른 물건마다 팔 개수 (None 이면 가진 것 전부, 가진 것보다 많으면 가진 만큼). reason: none / empty."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            have = [(k, t, q) for k, t, q in f["items"] if picks is None or (k, t) in picks]
            sold = [(k, t, min(q, int(n)) if n else q) for k, t, q in have]
            if not sold:
                return {"ok": False, "reason": "empty"}
            burn = burning.mult("farm_sell", uid, now_ts)   # 3.8.4 수능 버닝 (인챈트 작물은 빼고)
            em = ENCH_MULT_392 / ENCH_MULT if v392(uid, now_ts) else 1   # 3.9.2 인챈트 값 5배 → 3배
            total = int(sum(value(k, t) * q * (em if k.endswith(ENCH) else burn) for k, t, q in sold)
                        * lobby.mult("farm_sell", uid))                                    # 3.8.2 /로비 공약
            for (k, t, q), (_, _, left) in zip(sold, have):
                if q < left:
                    con.execute("UPDATE farm_items SET qty = qty - ? WHERE farm_id=? AND key=? AND tier=?", (q, fid, k, t))
                else:
                    con.execute("DELETE FROM farm_items WHERE farm_id=? AND key=? AND tier=?", (fid, k, t))
            self._add_coins(con, fid, total)
            return {"ok": True, "sold": sold, "coins": total, "total": f["coins"] + total}
        return await self._tx(fn)

    async def sell_animal(self, uid: int, fid: int, slot: int, now_ts: int) -> dict:
        """동물을 내보낸다 — 입양 값의 절반을 돌려받고 땅이 빈다. reason: none / slot."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            p = f["fields"][slot] if 0 <= slot < len(f["fields"]) else None
            if not p or p["kind"] != "animal":
                return {"ok": False, "reason": "slot"}
            full = v392(uid, now_ts) and (int(fid), int(slot)) in self._over_species(con, uid)   # 3.9.2 한도를 넘는 동물은 다 돌려준다
            back = int(ANIMALS[p["key"]][2] * (1 if full else ANIMAL_REFUND))
            con.execute("DELETE FROM farm_fields WHERE farm_id=? AND slot=?", (fid, slot))
            self._add_coins(con, fid, back)
            return {"ok": True, "key": p["key"], "coins": back}
        return await self._tx(fn)

    # ───────────── 땅 확장 ─────────────
    async def expand(self, uid: int, fid: int, now_ts: int) -> dict:
        """땅 한 칸 확장 (코인) + 은 상자 하나. reason: none / max / coins(cost)."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            if f["plots"] >= PLOTS_MAX:
                return {"ok": False, "reason": "max"}
            cost = expand_cost(f["plots"])
            if f["coins"] < cost:
                return {"ok": False, "reason": "coins", "cost": cost}
            self._add_coins(con, fid, -cost)
            con.execute(f"UPDATE farms SET plots = plots + 1, c_{EXPAND_CHEST} = c_{EXPAND_CHEST} + 1 WHERE id=?", (fid,))
            return {"ok": True, "plots": f["plots"] + 1, "cost": cost, "chest": EXPAND_CHEST}
        return await self._tx(fn)

    # ───────────── 낫 강화 ─────────────
    async def enhance(self, uid: int, fid: int, now_ts: int, use_whet: bool = False, charm: bool = False, rng=random,
                      cap: int = SICKLE_MAX) -> dict:
        """낫 강화 — 코인 또는 숫돌. charm = 🍀 행운 부적(축구 봇 돈)으로 성공 확률 +10%p. 실패해도 단계는 그대로.
        reason: none / max / coins(cost) / whet(need) / money(fee)."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            lv = f["sickle"]
            if lv >= cap:
                return {"ok": False, "reason": "max"}
            cost, need, fee = sickle_cost(lv), sickle_whet(lv), CHARM_FEE * (lv + 1)
            if use_whet and f["whet"] < need:
                return {"ok": False, "reason": "whet", "need": need}
            if not use_whet and f["coins"] < cost:
                return {"ok": False, "reason": "coins", "cost": cost}
            if charm and self._wallet(con, uid) < fee:
                return {"ok": False, "reason": "money", "fee": fee}
            if use_whet:
                con.execute("UPDATE farms SET whet = whet - ? WHERE id=?", (need, fid))
            else:
                self._add_coins(con, fid, -(cost))
            if charm:
                con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (fee, uid))
            p = min(1.0, SICKLE_CHANCE[lv] + (CHARM_BONUS if charm else 0))
            success = rng.random() < p
            if success:
                con.execute("UPDATE farms SET sickle = sickle + 1 WHERE id=?", (fid,))
            return {"ok": True, "success": success, "level": lv + success, "chance": p, "fee": fee if charm else 0}
        return await self._tx(fn)

    # ───────────── 3.8.4 숫돌 주괴 · 씨앗 인챈트 ─────────────
    async def make_ingot(self, uid: int, fid: int, now_ts: int, n: Optional[int] = None) -> dict:
        """숫돌 INGOT_NEED 개 → 숫돌 주괴 1개 (n = 만들 개수, None 이면 되는 만큼). reason: none / whet(have)."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            can = f["whet"] // INGOT_NEED
            k = can if n is None else min(int(n), can)
            if k <= 0:
                return {"ok": False, "reason": "whet", "have": f["whet"]}
            con.execute("UPDATE farms SET whet = whet - ?, ingot = ingot + ? WHERE id=?", (k * INGOT_NEED, k, fid))
            return {"ok": True, "made": k, "ingot": f["ingot"] + k, "whet": f["whet"] - k * INGOT_NEED}
        return await self._tx(fn)

    async def enchant(self, uid: int, fid: int, key: str, now_ts: int) -> dict:
        """숫돌 주괴 1개로 사 둔 씨앗 ENCH_SEEDS 개를 인챈트한다. reason: none / key / ingot / seeds(have)."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            if key not in CROPS or not CROPS[key][2]:
                return {"ok": False, "reason": "key"}
            if f["ingot"] <= 0:
                return {"ok": False, "reason": "ingot"}
            have = f["seeds"].get(key, 0)
            if have < ENCH_SEEDS:
                return {"ok": False, "reason": "seeds", "have": have}
            con.execute("UPDATE farms SET ingot = ingot - 1 WHERE id=?", (fid,))
            self._seed_add(con, fid, key, -ENCH_SEEDS)
            self._seed_add(con, fid, key + ENCH, ENCH_SEEDS)
            return {"ok": True, "key": key, "n": ENCH_SEEDS, "ingot": f["ingot"] - 1, "left": have - ENCH_SEEDS,
                    "eseeds": f["eseeds"].get(key, 0) + ENCH_SEEDS}
        return await self._tx(fn)

    # ───────────── 상자 ─────────────
    async def open_chest(self, uid: int, fid: int, tier: str, now_ts: int, lucky: bool = False, rng=random) -> dict:
        """상자 하나 열기. lucky = 🍀 행운 열기(축구 봇 돈): 보상마다 두 번 뽑아 더 좋은 쪽.
        reason: none / tier / empty / money(fee)."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            return self._open_one(con, uid, fid, f, tier, now_ts, lucky, rng)
        return await self._tx(fn)

    async def open_all(self, uid: int, fid: int, now_ts: int, lucky: bool = False, rng=random) -> dict:
        """3.9.2 (11/23 규완님 '상자 한 번에 까기'): 가진 상자를 전부 연다 (행운 열기면 돈이 되는 만큼).
        → {opened: {tier: 수}, rewards: [보상 합친 것], fee, stop(돈이 모자라 멈췄나)}. reason: none / empty."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            if not sum(f["chests"].values()):
                return {"ok": False, "reason": "empty"}
            opened, fee, stop, got = {}, 0, False, {}
            for tier in CHESTS:
                for _ in range(f["chests"][tier]):
                    r = self._open_one(con, uid, fid, f, tier, now_ts, lucky, rng)
                    if not r["ok"]:
                        stop = r["reason"] == "money"
                        break
                    opened[tier] = opened.get(tier, 0) + 1
                    fee += r["fee"]
                    for x in r["rewards"]:
                        k = (x["kind"], x.get("key"), x.get("tier"))
                        got[k] = got.get(k, 0) + x["n"]
                if stop:
                    break
            rewards = [{"kind": k, "key": key, "tier": t, "n": n} for (k, key, t), n in got.items()]
            return {"ok": bool(opened), "reason": "money" if not opened else None, "opened": opened, "rewards": rewards,
                    "fee": fee, "stop": stop}
        return await self._tx(fn)

    def _open_one(self, con, uid: int, fid: int, f: dict, tier: str, now_ts: int, lucky: bool, rng) -> dict:
        """상자 하나 (f 는 연 농장 — 에너지는 f 에 더해 간다)."""
        if True:
            if tier not in CHESTS:
                return {"ok": False, "reason": "tier"}
            if f["chests"][tier] <= 0:
                return {"ok": False, "reason": "empty"}
            fee = LUCKY_FEE[tier] if lucky else 0
            if fee and self._wallet(con, uid) < fee:
                return {"ok": False, "reason": "money", "fee": fee}
            if fee:
                con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (fee, uid))
            con.execute(f"UPDATE farms SET c_{tier} = c_{tier} - 1 WHERE id=?", (fid,))
            rewards = roll_chest(tier, lucky, rng, f["v382"])
            for r in rewards:
                if r["kind"] == "coins":
                    self._add_coins(con, fid, r["n"])
                elif r["kind"] == "whet":
                    con.execute("UPDATE farms SET whet = whet + ? WHERE id=?", (r["n"], fid))
                elif r["kind"] == "energy":   # 상자 에너지는 한도를 넘겨서도 쌓인다
                    f["energy_f"] += r["n"]
                    con.execute("UPDATE farms SET energy=?, energy_ts=? WHERE id=?", (f["energy_f"], int(now_ts), fid))
                else:
                    self._give(con, fid, r["key"], r["tier"], r["n"])
            f["chests"][tier] -= 1
            return {"ok": True, "tier": tier, "rewards": rewards, "fee": fee}

    # ───────────── 3.9.2 동물 이름 ─────────────
    async def name_pet(self, uid: int, fid: int, slot: int, name: str, now_ts: int) -> dict:
        """동물에게 이름 (빈 글이면 지운다). reason: none / slot / long."""
        name = " ".join(str(name).split())
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            p = f["fields"][slot] if 0 <= slot < len(f["fields"]) else None
            if not p or p["kind"] != "animal":
                return {"ok": False, "reason": "slot"}
            if len(name) > PET_NAME_MAX:
                return {"ok": False, "reason": "long", "max": PET_NAME_MAX}
            con.execute("UPDATE farm_fields SET pet=? WHERE farm_id=? AND slot=?", (name or None, int(fid), int(slot)))
            return {"ok": True, "name": name or None, "key": p["key"]}
        return await self._tx(fn)

    # ───────────── 3.9.2 꾸미기 ─────────────
    async def decos(self, owner: int) -> set:
        return await self._tx(lambda con: {k for (k,) in con.execute("SELECT key FROM farm_decos WHERE owner=?", (int(owner),)) if k in DECOS})

    async def deco(self, uid: int, fid: int, key: Optional[str], now_ts: int) -> dict:
        """꾸미기 걸기 (없으면 사서 건다, None 이면 떼기). reason: none / key / coins(cost)."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            if key is None:
                con.execute("UPDATE farms SET deco=NULL WHERE id=?", (int(fid),))
                return {"ok": True, "key": None, "paid": 0}
            if key not in DECOS:
                return {"ok": False, "reason": "key"}
            paid = 0
            if not con.execute("SELECT 1 FROM farm_decos WHERE owner=? AND key=?", (int(uid), key)).fetchone():
                paid = DECOS[key][1]
                if f["coins"] < paid:
                    return {"ok": False, "reason": "coins", "cost": paid}
                self._add_coins(con, fid, -paid)
                con.execute("INSERT INTO farm_decos(owner, key) VALUES(?,?)", (int(uid), key))
            con.execute("UPDATE farms SET deco=? WHERE id=?", (key, int(fid)))
            return {"ok": True, "key": key, "paid": paid}
        return await self._tx(fn)

    # ───────────── 3.9.2 농부끼리 교환 ─────────────
    @staticmethod
    def _take_items(con, fid: int, key: str, qty: int, tier: Optional[int] = None) -> bool:
        """농장 fid 창고에서 key 를 qty 개 뺀다 (tier None 이면 낮은 등급부터). 모자라면 아무것도 안 빼고 False."""
        rows = con.execute("SELECT tier, qty FROM farm_items WHERE farm_id=? AND key=? AND qty>0" + (" AND tier=?" if tier else "")
                           + " ORDER BY tier", (int(fid), key, *((int(tier),) if tier else ()))).fetchall()
        if sum(int(q) for _, q in rows) < qty:
            return False
        left = qty
        got = []
        for t, q in rows:
            use = min(int(q), left)
            if use <= 0:
                break
            con.execute("UPDATE farm_items SET qty = qty - ? WHERE farm_id=? AND key=? AND tier=?", (use, int(fid), key, int(t)))
            got.append((int(t), use))
            left -= use
        con.execute("DELETE FROM farm_items WHERE farm_id=? AND qty <= 0", (int(fid),))
        return got

    def _expire_trades(self, con, now_ts: int):
        for tid, fid, k, t, q in con.execute("SELECT id, from_fid, give_key, give_tier, give_qty FROM farm_trades "
                                             "WHERE status='open' AND ts < ?", (int(now_ts) - TRADE_HOURS * 3600,)).fetchall():
            self._trade_back(con, tid, fid, k, t, q, "expired")

    def _trade_back(self, con, tid, fid, key, tier, qty, status):
        own = con.execute("SELECT owner FROM farms WHERE id=?", (int(fid),)).fetchone()
        frm = con.execute("SELECT from_uid FROM farm_trades WHERE id=?", (int(tid),)).fetchone()
        to = int(fid) if own and frm and int(own[0]) == int(frm[0]) else self._first_farm(con, int(frm[0]) if frm else 0)
        if to:
            self._give(con, to, key, int(tier), int(qty))
        con.execute("UPDATE farm_trades SET status=? WHERE id=?", (status, int(tid)))

    @staticmethod
    def _first_farm(con, owner: int) -> Optional[int]:
        r = con.execute("SELECT id FROM farms WHERE owner=? ORDER BY id LIMIT 1", (int(owner),)).fetchone()
        return int(r[0]) if r else None

    async def offer_trade(self, uid: int, fid: int, to_uid: int, give_key: str, give_tier: int, give_qty: int,
                          want_key: str, want_qty: int, now_ts: int) -> dict:
        """내 창고 물건(give)을 내놓고 상대의 want 를 달라고 제안한다. 내놓은 물건은 바로 빠지고, 거절 · 취소 · 만료되면 돌아온다.
        reason: none / self / target(상대 농장 없음) / key / amount / few."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            if int(to_uid) == int(uid):
                return {"ok": False, "reason": "self"}
            if not self._first_farm(con, to_uid):
                return {"ok": False, "reason": "target"}
            if give_key not in ITEMS or want_key not in ITEMS:
                return {"ok": False, "reason": "key"}
            if give_qty <= 0 or want_qty <= 0:
                return {"ok": False, "reason": "amount"}
            if not self._take_items(con, fid, give_key, int(give_qty), int(give_tier)):
                return {"ok": False, "reason": "few"}
            cur = con.execute("INSERT INTO farm_trades(from_uid, from_fid, to_uid, give_key, give_tier, give_qty, want_key, want_qty, ts) "
                              "VALUES(?,?,?,?,?,?,?,?,?)", (int(uid), int(fid), int(to_uid), give_key, int(give_tier), int(give_qty),
                                                            want_key, int(want_qty), int(now_ts)))
            return {"ok": True, "id": int(cur.lastrowid)}
        return await self._tx(fn)

    async def trades(self, uid: int, now_ts: int) -> dict:
        """→ {in: [받은 제안], out: [보낸 제안]} (열린 것만)."""
        def fn(con):
            self._expire_trades(con, now_ts)
            cols = ("id", "from_uid", "from_fid", "to_uid", "give_key", "give_tier", "give_qty", "want_key", "want_qty", "ts")
            rows = [dict(zip(cols, r)) for r in con.execute(
                f"SELECT {', '.join(cols)} FROM farm_trades WHERE status='open' AND (to_uid=? OR from_uid=?) ORDER BY id",
                (int(uid), int(uid)))]
            return {"in": [r for r in rows if r["to_uid"] == int(uid)], "out": [r for r in rows if r["from_uid"] == int(uid)]}
        return await self._tx(fn)

    async def answer_trade(self, uid: int, fid: int, tid: int, accept: bool, now_ts: int) -> dict:
        """받은 제안 수락(내 농장 fid 창고에서 want 를 내고 give 를 받는다) · 거절, 또는 보낸 제안 취소(accept False).
        reason: none / gone / few."""
        def fn(con):
            self._expire_trades(con, now_ts)
            t = con.execute("SELECT from_uid, from_fid, to_uid, give_key, give_tier, give_qty, want_key, want_qty FROM farm_trades "
                            "WHERE id=? AND status='open'", (int(tid),)).fetchone()
            if not t or int(uid) not in (int(t[0]), int(t[2])):
                return {"ok": False, "reason": "gone"}
            frm, ffid, to, gk, gt, gq, wk, wq = t
            if not accept:
                self._trade_back(con, tid, ffid, gk, gt, gq, "cancelled" if int(uid) == int(frm) else "rejected")
                return {"ok": True, "accepted": False}
            if int(uid) != int(to) or not self._mine(con, uid, fid, now_ts):
                return {"ok": False, "reason": "none"}
            got = self._take_items(con, fid, wk, int(wq))
            if not got:
                return {"ok": False, "reason": "few", "key": wk, "qty": int(wq)}
            back = int(ffid) if (con.execute("SELECT owner FROM farms WHERE id=?", (int(ffid),)).fetchone() or (0,))[0] == int(frm) \
                else self._first_farm(con, frm)
            for tier, q in got:
                if back:
                    self._give(con, back, wk, tier, q)
            self._give(con, fid, gk, int(gt), int(gq))
            con.execute("UPDATE farm_trades SET status='done' WHERE id=?", (int(tid),))
            return {"ok": True, "accepted": True, "from": int(frm), "give": (gk, int(gt), int(gq)), "want": (wk, int(wq))}
        return await self._tx(fn)

    # ───────────── 환전 ─────────────
    async def exchange(self, uid: int, fid: int, coins: int, now_ts: int) -> dict:
        """농장 코인 → 축구 봇 돈 (코인 1 = RATE 원). reason: none / amount / coins."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            if coins <= 0:
                return {"ok": False, "reason": "amount"}
            if f["coins"] < coins:
                return {"ok": False, "reason": "coins", "have": f["coins"]}
            rt = rate(uid, now_ts)
            self._wallet(con, uid)
            self._add_coins(con, fid, -(coins))
            con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (coins * rt, uid))
            return {"ok": True, "coins": coins, "won": coins * rt, "balance": self._wallet(con, uid), "left": f["coins"] - coins}
        return await self._tx(fn)

    # ───────────── 3.9.2 아르바이트생 ─────────────
    async def staff(self, fid: int) -> Optional[dict]:
        """농장 fid 의 아르바이트생 (없으면 None)."""
        def fn(con):
            r = con.execute("SELECT tier, hired_ts, paid_until, next_ts, work, wages FROM farm_staff WHERE farm_id=?", (int(fid),)).fetchone()
            if not r or r[0] not in STAFF:
                return None
            return {"tier": r[0], "hired_ts": int(r[1]), "paid_until": int(r[2]), "next_ts": int(r[3]), "work": int(r[4]),
                    "wages": int(r[5])}
        return await self._tx(fn)

    async def hire(self, uid: int, fid: int, tier: str, now_ts: int) -> dict:
        """아르바이트생 고용 (이미 있으면 바꾼다). 첫 한 시간 시급을 바로 낸다. reason: none / tier / coins(cost)."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            if tier not in STAFF:
                return {"ok": False, "reason": "tier"}
            wage = STAFF[tier][1]
            if f["coins"] < wage:
                return {"ok": False, "reason": "coins", "cost": wage}
            self._add_coins(con, fid, -wage)
            con.execute("INSERT OR REPLACE INTO farm_staff(farm_id, tier, hired_ts, paid_until, next_ts, work, wages) "
                        "VALUES(?,?,?,?,?,0,?)", (int(fid), tier, int(now_ts), int(now_ts) + 3600, int(now_ts) + 5, wage))
            return {"ok": True, "tier": tier, "wage": wage, "coins": f["coins"] - wage}
        return await self._tx(fn)

    async def fire(self, uid: int, fid: int, now_ts: int) -> bool:
        """아르바이트생 내보내기 (이미 낸 시급은 돌려주지 않는다)."""
        def fn(con):
            if not self._mine(con, uid, fid, now_ts):
                return False
            return bool(con.execute("DELETE FROM farm_staff WHERE farm_id=?", (int(fid),)).rowcount)
        return await self._tx(fn)

    async def dismiss_staff_once(self, key: str, now_ts: int) -> list[tuple[int, int, int]]:
        """11/24 규완님: 공개 전에 고용된 아르바이트생을 모두 내보내고 그동안 낸 시급을 주인에게 코인으로 돌려준다 (한 번만, gift_once).
        → [(주인, 농장, 돌려준 코인)]."""
        def fn(con):
            con.execute("CREATE TABLE IF NOT EXISTS gift_once (key TEXT PRIMARY KEY, ts INTEGER NOT NULL)")
            if not con.execute("INSERT OR IGNORE INTO gift_once(key, ts) VALUES(?, ?)", (key, int(now_ts))).rowcount:
                return []
            out = []
            for fid, wages in con.execute("SELECT farm_id, wages FROM farm_staff").fetchall():
                r = con.execute("SELECT owner FROM farms WHERE id=?", (int(fid),)).fetchone()
                if r and int(wages) > 0:
                    self._add_coins(con, int(fid), int(wages))
                out.append((int(r[0]) if r else 0, int(fid), int(wages) if r else 0))
            con.execute("DELETE FROM farm_staff")
            return out
        return await self._tx(fn)

    async def staff_due(self, now_ts: int) -> list[dict]:
        """일할 차례가 된 아르바이트생들. 시급 낼 때가 됐으면 주인 코인에서 내고, 모자라면 그만둔다 (quit=True).
        → [{fid, owner, tier, jobs, quit}]. 일한 다음엔 staff_done 을 부른다."""
        def fn(con):
            out = []
            for fid, tier, paid, nxt in con.execute("SELECT s.farm_id, s.tier, s.paid_until, s.next_ts FROM farm_staff s "
                                                    "WHERE s.next_ts <= ?", (int(now_ts),)).fetchall():
                own = con.execute("SELECT owner FROM farms WHERE id=?", (int(fid),)).fetchone()
                if not own or tier not in STAFF:
                    con.execute("DELETE FROM farm_staff WHERE farm_id=?", (int(fid),))
                    continue
                owner, (name, wage, every, jobs) = int(own[0]), STAFF[tier]
                paid = int(paid)
                quit_ = False
                while paid <= now_ts:
                    if self._coins(con, int(fid)) < wage:
                        quit_ = True
                        break
                    self._add_coins(con, int(fid), -wage)
                    con.execute("UPDATE farm_staff SET wages = wages + ? WHERE farm_id=?", (wage, int(fid)))
                    paid += 3600
                if quit_:
                    con.execute("DELETE FROM farm_staff WHERE farm_id=?", (int(fid),))
                else:
                    con.execute("UPDATE farm_staff SET paid_until=?, next_ts=? WHERE farm_id=?", (paid, int(now_ts) + every, int(fid)))
                out.append({"fid": int(fid), "owner": owner, "tier": tier, "jobs": jobs, "quit": quit_})
            return out
        return await self._tx(fn)

    async def staff_done(self, fid: int, n: int):
        if n:
            await self._tx(lambda con: con.execute("UPDATE farm_staff SET work = work + ? WHERE farm_id=?", (int(n), int(fid))))

    async def refund_coins_once(self, refunds, now_ts: int) -> list[tuple[int, int]]:
        """(키, 주인, 코인) 마다 한 번만 코인 지갑에 돌려준다 (gift_once 표, 재시작해도 한 번). 지갑이 없으면 첫 농장에.
        → 이번에 준 [(주인, 코인)]."""
        def fn(con):
            con.execute("CREATE TABLE IF NOT EXISTS gift_once (key TEXT PRIMARY KEY, ts INTEGER NOT NULL)")
            out = []
            for key, owner, coins in refunds:
                if not con.execute("INSERT OR IGNORE INTO gift_once(key, ts) VALUES(?, ?)", (key, int(now_ts))).rowcount:
                    continue
                if self._purse(con, owner) is not None:
                    con.execute("UPDATE farm_purse SET coins = coins + ? WHERE owner=?", (int(coins), int(owner)))
                else:
                    r = con.execute("SELECT id FROM farms WHERE owner=? ORDER BY id LIMIT 1", (int(owner),)).fetchone()
                    if not r:
                        continue
                    con.execute("UPDATE farms SET coins = coins + ? WHERE id=?", (int(coins), int(r[0])))
                out.append((int(owner), int(coins)))
            return out
        return await self._tx(fn)

    async def release_over_401(self, now_ts: int) -> Optional[list[tuple[int, int, int]]]:
        """11/26 규완님 결정 '넘는 동물 자동으로 돌려주기': 같은 동물 한도(SPECIES_CAP_392)를 넘는 동물을 한 번만 모두 내보내고
        입양 값을 다 돌려준다 (sell_animal 의 한도 넘는 동물과 같은 값). → [(주인, 마리 수, 코인)] (이미 했으면 None)."""
        def fn(con):
            con.execute("CREATE TABLE IF NOT EXISTS gift_once (key TEXT PRIMARY KEY, ts INTEGER NOT NULL)")
            if not con.execute("INSERT OR IGNORE INTO gift_once(key, ts) VALUES('release-401-over', ?)", (int(now_ts),)).rowcount:
                return None
            out = []
            for (owner,) in con.execute("SELECT DISTINCT m.owner FROM farm_fields f JOIN farms m ON m.id = f.farm_id "
                                        "WHERE f.kind='animal'").fetchall():
                n = coins = 0
                for fid, slot in sorted(self._over_species(con, owner)):
                    r = con.execute("SELECT key FROM farm_fields WHERE farm_id=? AND slot=?", (fid, slot)).fetchone()
                    if not r or r[0] not in ANIMALS:
                        continue
                    back = int(ANIMALS[r[0]][2])
                    con.execute("DELETE FROM farm_fields WHERE farm_id=? AND slot=?", (fid, slot))
                    self._add_coins(con, fid, back)
                    n += 1
                    coins += back
                if n:
                    out.append((int(owner), n, coins))
            return out
        return await self._tx(fn)

    async def restore_coins_401(self, now_ts: int) -> Optional[int]:
        """11/26 규완님 '코인은 왜 1/100을 한 거야': 4.0.1 공개 때 1/100 한 농장 코인을 한 번만 100배로 되돌리고,
        공개 직후(12:30 KST) 걷힌 유지비(UPKEEP_REFUND_401, 원래 단위)를 돌려준다. → 바꾼 코인 지갑 수 (이미 했으면 None)."""
        def fn(con):
            con.execute("CREATE TABLE IF NOT EXISTS gift_once (key TEXT PRIMARY KEY, ts INTEGER NOT NULL)")
            if not con.execute("INSERT OR IGNORE INTO gift_once(key, ts) VALUES('restore-401-coins', ?)", (int(now_ts),)).rowcount:
                return None
            n = 0
            for t, c, where in (("farm_purse", "coins", ""), ("farms", "coins", ""), ("farm_held", "coins", ""),
                                ("farm_offers", "amount", " AND status = 'open'")):
                if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
                    k = con.execute(f"UPDATE {t} SET {c} = {c} * 100 WHERE {c} != 0{where}").rowcount
                    n += k if t == "farm_purse" else 0
            for owner, coins in UPKEEP_REFUND_401:
                if self._purse(con, owner) is not None:
                    con.execute("UPDATE farm_purse SET coins = coins + ? WHERE owner=?", (int(coins), int(owner)))
                else:
                    r = con.execute("SELECT id FROM farms WHERE owner=? ORDER BY id LIMIT 1", (int(owner),)).fetchone()
                    if r:
                        con.execute("UPDATE farms SET coins = coins + ? WHERE id=?", (int(coins), int(r[0])))
            return n
        return await self._tx(fn)

    async def refund_feed_392(self, now_ts: int) -> Optional[list[tuple[int, int, int]]]:
        """3.9.2 공개 때 한 번 (11/25 규완님 '동물 사료 상점에서 빼자' → '코인으로 돌려주고'):
        가진 동물 사료를 모두 없애고 상점 값(개당 tool_price)으로 코인을 돌려준다. → [(주인, 개수, 코인)] (이미 했으면 None)."""
        price = tool_price("feed", True)

        def fn(con):
            con.execute("CREATE TABLE IF NOT EXISTS gift_once (key TEXT PRIMARY KEY, ts INTEGER NOT NULL)")
            if not con.execute("INSERT OR IGNORE INTO gift_once(key, ts) VALUES('refund-392-feed', ?)", (int(now_ts),)).rowcount:
                return None
            have: dict[int, int] = {}
            for owner, q in con.execute("SELECT owner, qty FROM farm_bag WHERE key='feed' AND qty > 0").fetchall():
                have[int(owner)] = have.get(int(owner), 0) + int(q)
            for owner, q in con.execute("SELECT f.owner, s.qty FROM farm_seeds s JOIN farms f ON f.id = s.farm_id "
                                        "WHERE s.key='feed' AND s.qty > 0").fetchall():
                have[int(owner)] = have.get(int(owner), 0) + int(q)
            con.execute("DELETE FROM farm_bag WHERE key='feed'")
            con.execute("DELETE FROM farm_seeds WHERE key='feed'")
            out = []
            for owner, q in have.items():
                if self._purse(con, owner) is not None:
                    con.execute("UPDATE farm_purse SET coins = coins + ? WHERE owner=?", (q * price, owner))
                else:
                    r = con.execute("SELECT id FROM farms WHERE owner=? ORDER BY id LIMIT 1", (owner,)).fetchone()
                    if not r:
                        continue
                    con.execute("UPDATE farms SET coins = coins + ? WHERE id=?", (q * price, int(r[0])))
                out.append((owner, q, q * price))
            return out
        return await self._tx(fn)

    async def pay_upkeep(self, now_ts: int, only: Optional[set] = None) -> list[tuple[int, int, int]]:
        """3.9.2 농장 유지비: 하루(KST) 한 번, 농장이 두 개 이상인 사람의 코인 지갑에서 upkeep(농장 수) 를 낸다.
        코인이 모자라면 있는 만큼만 낸다 (빚은 남기지 않는다). only = 이 사람들만. → [(주인, 낸 코인, 농장 수)]."""
        day = _kst_day(now_ts)

        def fn(con):
            out = []
            for owner, n in con.execute("SELECT owner, COUNT(*) FROM farms GROUP BY owner HAVING COUNT(*) > 1").fetchall():
                owner = int(owner)
                if only is not None and owner not in only:
                    continue
                r = con.execute("SELECT day_key FROM farm_upkeep WHERE owner=?", (owner,)).fetchone()
                if r and int(r[0]) == day:
                    continue
                con.execute("INSERT OR REPLACE INTO farm_upkeep(owner, day_key) VALUES(?,?)", (owner, day))
                purse = self._purse(con, owner)
                if purse is None:
                    continue
                paid = min(max(0, purse), upkeep(int(n)))
                if paid:
                    con.execute("UPDATE farm_purse SET coins = coins - ? WHERE owner=?", (paid, owner))
                out.append((owner, paid, int(n)))
            return out
        return await self._tx(fn)

    # ───────────── 인수 (농장 코인) ─────────────
    @classmethod
    def _pay_check(cls, con, fid: int, buyer: int, amount: int, pay_fid: Optional[int]) -> Optional[dict]:
        """산 사람이 낼 농장(pay_fid)이 그 사람 것이고 코인이 amount 이상인지. 아니면 실패 dict (pay / coins).
        3.8.2 코인 지갑이 있는 사람은 지갑에서 낸다 (pay_fid 는 보지 않는다)."""
        purse = cls._purse(con, buyer)
        if purse is not None:
            if purse < amount:
                return {"ok": False, "reason": "coins", "cost": amount, "have": purse}
            return None
        p = con.execute("SELECT owner, coins FROM farms WHERE id=?", (int(pay_fid or 0),)).fetchone()
        if not p or int(p[0]) != int(buyer) or int(pay_fid) == int(fid):
            return {"ok": False, "reason": "pay"}
        if int(p[1]) < amount:
            return {"ok": False, "reason": "coins", "cost": amount, "have": int(p[1])}
        return None

    def _farm_transfer(self, con, fid: int, buyer: int, owner: int, amount: int, pay_fid: Optional[int]) -> dict:
        """buyer 가 내 농장 pay_fid 의 코인 amount 를 내고 농장 fid 를 가져간다. 실패 reason: pay / coins.
        11/11 규완님 '무조건 코인으로 받게': 판 사람은 같은 코인을 남은 농장(가장 먼저 연 것)에 받는다 (to_fid).
        남은 농장이 없으면 맡아 뒀다가(farm_held) 다음에 여는 농장에 넣는다 (to_fid=None).
        3.8.2 코인 지갑: 산 사람은 지갑에서 내고 인수한 농장의 코인도 지갑으로 합쳐진다. 판 사람도 지갑으로 받는다 (purse=True)."""
        bad = self._pay_check(con, fid, buyer, amount, pay_fid)
        if bad:
            return bad
        b_purse = self._purse(con, buyer) is not None
        if b_purse:
            con.execute("UPDATE farm_purse SET coins = coins - ? WHERE owner=?", (amount, int(buyer)))
            carry = int(con.execute("SELECT coins FROM farms WHERE id=?", (fid,)).fetchone()[0])
            if carry:   # 인수한 농장에 따로 있던 코인도 내 지갑 하나로
                con.execute("UPDATE farm_purse SET coins = coins + ? WHERE owner=?", (carry, int(buyer)))
                con.execute("UPDATE farms SET coins = 0 WHERE id=?", (fid,))
        else:
            con.execute("UPDATE farms SET coins = coins - ? WHERE id=?", (amount, int(pay_fid)))
        con.execute("UPDATE farms SET owner=?, ask=NULL WHERE id=?", (buyer, fid))
        con.execute("UPDATE farm_offers SET status='closed' WHERE farm_id=? AND status='open'", (fid,))   # 남은 제안 정리
        to, to_land, purse = None, None, False
        if self._purse(con, owner) is not None:
            purse = True
            con.execute("UPDATE farm_purse SET coins = coins + ? WHERE owner=?", (amount, int(owner)))
            got_now = self._purse(con, owner)
        else:
            to = con.execute("SELECT id, land FROM farms WHERE owner=? ORDER BY created_ts, id", (int(owner),)).fetchone()
            to_land = to[1] if to else None
            if to:
                to = int(to[0])
                con.execute("UPDATE farms SET coins = coins + ? WHERE id=?", (amount, to))
                got_now = int(con.execute("SELECT coins FROM farms WHERE id=?", (to,)).fetchone()[0])
            else:
                con.execute("INSERT INTO farm_held(owner, coins) VALUES(?, ?) "
                            "ON CONFLICT(owner) DO UPDATE SET coins = coins + excluded.coins", (int(owner), amount))
                got_now = int(con.execute("SELECT coins FROM farm_held WHERE owner=?", (int(owner),)).fetchone()[0])
        left = self._purse(con, buyer) if b_purse else \
            int(con.execute("SELECT coins FROM farms WHERE id=?", (int(pay_fid),)).fetchone()[0])
        return {"ok": True, "id": fid, "cost": amount, "prev_owner": owner, "pay_fid": pay_fid, "left": left,
                "to_fid": to, "to_land": to_land, "to_coins": got_now, "purse": purse}

    async def offer_farm(self, buyer: int, fid: int, amount: int, now_ts: int, pay_fid: Optional[int] = None,
                         auto: bool = True) -> dict:
        """인수 제안 (농장 코인). 적정 인수 금액(farm_fair) 이상이면 바로 인수 (auto=True), 아니면 주인에게 제안을 남긴다
        (같은 사람이 같은 농장에 다시 내면 새 제안으로 바뀐다 · 24시간 뒤 만료 · 코인은 수락될 때 빠진다).
        3.8.2 (auto=False): 바로 인수는 없다 — 금액과 상관없이 주인이 수락해야 인수된다.
        pay_fid = 코인을 낼 내 농장. 실패 reason: gone(농장 없음) / mine / amount / pay(낼 농장이 내 것이 아님) / coins."""
        def fn(con):
            f = self._farm(con, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "gone"}
            if f["owner"] == int(buyer):
                return {"ok": False, "reason": "mine"}
            if amount <= 0:
                return {"ok": False, "reason": "amount"}
            bad = _over_cap(con, buyer, now_ts) or self._pay_check(con, fid, int(buyer), int(amount), pay_fid)   # 3.9.1 7개까지
            if bad:
                return bad
            if auto and amount >= f["fair"]:
                return {**self._farm_transfer(con, fid, int(buyer), f["owner"], int(amount), pay_fid),
                        "auto": True, "fair": f["fair"]}
            con.execute("UPDATE farm_offers SET status='closed' WHERE farm_id=? AND buyer_id=? AND status='open'", (fid, int(buyer)))
            cur = con.execute("INSERT INTO farm_offers(farm_id, buyer_id, owner_id, amount, ts, pay_fid) VALUES(?,?,?,?,?,?)",
                              (fid, int(buyer), f["owner"], int(amount), int(now_ts), int(pay_fid or 0)))
            return {"ok": True, "auto": False, "offer_id": int(cur.lastrowid), "owner_id": f["owner"], "amount": int(amount),
                    "fair": f["fair"], "expires": int(now_ts) + FARM_OFFER_HOURS * 3600}
        return await self._tx(fn)

    async def farm_offers(self, owner: int, fid: int, now_ts: int) -> list[dict]:
        """내 농장 fid 에 들어온 (만료 안 된) 인수 제안 — 금액 큰 순."""
        def fn(con):
            if not self._mine(con, owner, fid, now_ts):
                return []
            since = int(now_ts) - FARM_OFFER_HOURS * 3600
            return [{"id": int(i), "buyer_id": int(b), "amount": int(a), "ts": int(t)} for i, b, a, t in con.execute(
                "SELECT id, buyer_id, amount, ts FROM farm_offers WHERE farm_id=? AND owner_id=? AND status='open' AND ts>? "
                "ORDER BY amount DESC, ts", (fid, int(owner), since))]
        return await self._tx(fn)

    async def set_farm_ask(self, owner: int, fid: int, amount: int, now_ts: int) -> dict:
        """주인이 적정 인수 금액을 정한다 (농장 가치 × 1.5 ~ × 5). reason: none / range(lo, hi)."""
        def fn(con):
            f = self._mine(con, owner, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            lo, hi = farm_ask_range(f["worth"])
            if not lo <= amount <= hi:
                return {"ok": False, "reason": "range", "lo": lo, "hi": hi}
            con.execute("UPDATE farms SET ask=? WHERE id=?", (int(amount), fid))
            return {"ok": True, "ask": int(amount), "lo": lo, "hi": hi}
        return await self._tx(fn)

    async def answer_farm_offer(self, owner: int, offer_id: int, accept: bool, now_ts: int) -> dict:
        """인수 제안 수락 · 거절. 실패 reason: gone(없거나 만료 · 이미 처리 · 주인이 바뀜) ·
        수락할 때 제안한 사람의 농장 코인이 모자라면 coins, 낼 농장이 더는 그 사람 것이 아니면 pay (그 제안은 닫힌다)."""
        def fn(con):
            r = con.execute("SELECT farm_id, buyer_id, owner_id, amount, ts, status, pay_fid FROM farm_offers WHERE id=?",
                            (int(offer_id),)).fetchone()
            cur = con.execute("SELECT owner FROM farms WHERE id=?", (r[0],)).fetchone() if r else None
            if (not r or r[5] != "open" or int(r[2]) != int(owner) or int(r[4]) <= int(now_ts) - FARM_OFFER_HOURS * 3600
                    or not cur or int(cur[0]) != int(owner)):
                return {"ok": False, "reason": "gone"}
            fid, buyer, amount = int(r[0]), int(r[1]), int(r[3])
            base = {"id": fid, "buyer_id": buyer, "amount": amount}
            if not accept:
                con.execute("UPDATE farm_offers SET status='rejected' WHERE id=?", (int(offer_id),))
                return {"ok": True, "accepted": False, **base}
            t = _over_cap(con, buyer, now_ts) or self._farm_transfer(con, fid, buyer, int(owner), amount, r[6])   # 3.9.1 7개까지
            if not t["ok"]:
                con.execute("UPDATE farm_offers SET status='closed' WHERE id=?", (int(offer_id),))
                return {**t, **base, "ok": False}
            con.execute("UPDATE farm_offers SET status='accepted' WHERE id=?", (int(offer_id),))
            return {"ok": True, "accepted": True, **base, "to_fid": t["to_fid"], "to_land": t["to_land"], "to_coins": t["to_coins"]}
        return await self._tx(fn)
