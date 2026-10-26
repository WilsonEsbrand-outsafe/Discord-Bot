# services/enhance.py — 3.4.0 유망주 강화 (+1 ~ +10): 재료는 🔷 강화석 또는 내 선수 카드 몇 장.
# 단계마다 OVR +1 · 높을수록 성공 확률이 낮고, +6 부터는 실패하면 절반 확률로 한 단계 내려간다.
# +5 · +10 에 오르면 특성을 하나씩 고른다 (고르는 순간 히든 능력치 · 잠재력이 오르거나, 에이스는 OVR +2).
ENH_MAX = 10
ENH_MIN_OVR = 70                                    # 기본 OVR 이 이만큼 자라야 강화가 열린다
ENH_CHANCE = (0.95, 0.90, 0.85, 0.75, 0.65, 0.55, 0.45, 0.35, 0.25, 0.15)   # +N → +N+1 성공 확률
ENH_STONES = (1, 1, 2, 2, 3, 3, 4, 5, 6, 8)        # 🔷 강화석으로 할 때 필요한 개수
ENH_PLAYERS = (2, 2, 3, 3, 4, 4, 5, 5, 6, 6)       # 선수 카드로 할 때 필요한 장수
ENH_DROP_FROM, ENH_DROP_P = 6, 0.5                  # +6 이상에서 실패하면 50% 확률로 한 단계 ↓
STAR_OVR, STAR_BONUS = 80, 0.10                     # 재료 선수 평균 OVR 80 이상이면 성공 확률 +10%p
TRAIT_AT = (5, 10)                                  # 이 단계에 오르면 특성 한 칸
TRAITS = {   # key → (이모지, 이름, 설명)
    "ace": ("⭐", "에이스", "OVR +2"),
    "iron": ("🦾", "강철 몸", "부상 빈도 -6"),
    "leader": ("🧠", "강심장", "자신감 +6 (경기력 ↑)"),
    "pro": ("📚", "프로 정신", "프로 의식 +6 (성장 경험치 ↑)"),
    "wonder": ("🌠", "원더키드", "잠재력 +3"),
}


def parse_traits(s: str) -> list[str]:
    return [t for t in (s or "").split(",") if t in TRAITS]


def trait_slots(enh: int) -> int:
    return sum(enh >= lv for lv in TRAIT_AT)


def enh_ovr(enh: int, traits: str) -> int:
    """강화 · 특성으로 오르는 OVR (경기 · 선발 명단에 쓰는 OVR 에 더한다)."""
    return int(enh) + (2 if "ace" in parse_traits(traits) else 0)


def chance(enh: int, star: bool = False) -> float:
    return min(1.0, ENH_CHANCE[enh] + (STAR_BONUS if star else 0))


def badge(enh: int) -> str:
    return f" **+{enh}**" if enh else ""
