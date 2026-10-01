# services/club_db.py
# 구단: 이름 · 포메이션 · 선발 11명 · 주장 · 감독 · 친선경기 · 공식경기 · 유망주
import asyncio
import datetime
import math
import random
import re
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Optional

from services.economy_db import rookie_until
from services.player_market_db import pot_grade_for_value

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "economy.sqlite3"

CLUB_NAME_MAX = 30

# ───────────── 포메이션 ─────────────
# 슬롯 0 은 항상 GK. 표시 순서 = 슬롯 순서.
FORMATIONS: dict[str, list[str]] = {
    "4-4-2":   ["GK", "LB", "CB", "CB", "RB", "LM", "CM", "CM", "RM", "ST", "ST"],
    "4-3-3":   ["GK", "LB", "CB", "CB", "RB", "CM", "CM", "CM", "LW", "ST", "RW"],
    "4-2-3-1": ["GK", "LB", "CB", "CB", "RB", "DM", "DM", "LW", "AM", "RW", "ST"],
    "3-5-2":   ["GK", "CB", "CB", "CB", "LM", "CM", "CM", "CM", "RM", "ST", "ST"],
    "3-4-3":   ["GK", "CB", "CB", "CB", "LM", "CM", "CM", "RM", "LW", "ST", "RW"],
    "5-3-2":   ["GK", "LWB", "CB", "CB", "CB", "RWB", "CM", "CM", "CM", "ST", "ST"],
}
DEFAULT_FORMATION = "4-4-2"

# 슬롯 → 선수 포지션 그룹(선수 카드는 GK/DF/MF/FW 네 가지뿐이다)
SLOT_GROUP = {
    "GK": "GK",
    "LB": "DF", "CB": "DF", "RB": "DF", "LWB": "DF", "RWB": "DF",
    "LM": "MF", "CM": "MF", "RM": "MF", "DM": "MF", "AM": "MF",
    "LW": "FW", "RW": "FW", "ST": "FW",
}
_LINE = {"GK": 0, "DF": 1, "MF": 2, "FW": 3}


def effective_ovr(ovr: int, player_pos: str, slot: str) -> int:
    """제 포지션이 아니면 능력치가 깎인다: 한 줄 차이 -6, 두 줄 -12, 골키퍼↔필드 -30."""
    want, have = SLOT_GROUP[slot], player_pos
    if want == have:
        return int(ovr)
    if "GK" in (want, have):
        return max(1, int(ovr) - 30)
    return max(1, int(ovr) - 6 * abs(_LINE[want] - _LINE[have]))


def team_rating(lineup: list[dict], captain: Optional[str]) -> dict:
    """lineup: 슬롯 11개 [{slot, player_id, pos, ovr, nation} | {slot, player_id: None}].
    전력 = 11자리 유효 능력치 평균(빈 자리는 30) + 주장 +1 + 같은 국적 3명 이상 묶음마다 +1(최대 +3)."""
    total, filled, nations = 0, 0, {}
    for s in lineup:
        if s.get("player_id"):
            total += effective_ovr(s["ovr"], s["pos"], s["slot"])
            filled += 1
            nations[s["nation"]] = nations.get(s["nation"], 0) + 1
        else:
            total += 30
    base = total / 11
    captain_bonus = 1 if captain and any(s.get("player_id") == captain for s in lineup) else 0
    chem = min(3, sum(1 for n in nations.values() if n >= 3))
    return {"rating": round(base + captain_bonus + chem), "base": round(base, 1), "filled": filled,
            "captain_bonus": captain_bonus, "chem": chem}


def auto_assign(players: list[dict], formation: str) -> list[Optional[str]]:
    """가진 선수로 가장 강한 11명 배치. players: [{player_id, pos, ovr}]. 슬롯별 player_id 목록을 돌려준다.
    모든 (자리, 선수) 조합을 유효 능력치 높은 순으로 채운다. 같으면 제 포지션 선수가 먼저다."""
    slots = FORMATIONS[formation]
    pairs = sorted(
        ((effective_ovr(p["ovr"], p["pos"], s), p["pos"] == SLOT_GROUP[s], p["ovr"], i, p["player_id"])
         for i, s in enumerate(slots) for p in players),
        reverse=True,
    )
    out: list[Optional[str]] = [None] * len(slots)
    used = set()
    for _eff, _natural, _ovr, i, pid in pairs:
        if out[i] is None and pid not in used:
            out[i] = pid
            used.add(pid)
    return out


# 2.2 승률 조정: 전력 차가 기대 득점에 주는 영향을 줄였다 (/20 → /40, 기대 득점 0.3~3.5).
# 전력 차 0 → 승 37% · 10 → 약 50% · 20 → 약 68% · 35 → 약 85% (이전엔 10 → 70%, 20 → 91%, 35 → 100%).
XG_BASE, XG_SCALE, XG_MIN, XG_MAX = 1.35, 40, 0.3, 3.5


def expected_goals(r_home: float, r_away: float) -> tuple[float, float]:
    d = (r_home - r_away) / XG_SCALE
    clamp = lambda x: max(XG_MIN, min(XG_MAX, x))   # noqa: E731
    return clamp(XG_BASE * math.exp(d)), clamp(XG_BASE * math.exp(-d))


def win_probs(r_home: float, r_away: float) -> tuple[float, float, float]:
    """(홈 승, 무, 원정 승) 확률 — 포아송 분포를 10골까지 더한다."""
    lh, la = expected_goals(r_home, r_away)
    ph = [math.exp(-lh) * lh ** k / math.factorial(k) for k in range(11)]
    pa = [math.exp(-la) * la ** k / math.factorial(k) for k in range(11)]
    win = sum(ph[i] * pa[j] for i in range(11) for j in range(11) if i > j)
    lose = sum(ph[i] * pa[j] for i in range(11) for j in range(11) if i < j)
    draw = sum(ph[i] * pa[i] for i in range(11))
    total = win + draw + lose
    return win / total, draw / total, lose / total


def simulate_match(home: dict, away: dict, rng: random.Random = random) -> dict:
    """home/away: {name, rating, xi:[{name, pos, ovr}]}. 전력 차이로 기대 득점을 정하고 포아송으로 굴린다."""
    xg_h, xg_a = expected_goals(home["rating"], away["rating"])

    def poisson(lam: float) -> int:
        l, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= rng.random()
            if p <= l:
                return k
            k += 1

    weight = {"FW": 6, "MF": 3, "DF": 1, "GK": 0}
    assist_w = {"FW": 3, "MF": 5, "DF": 2, "GK": 0}
    goals = []
    for side, team, n in (("home", home, poisson(xg_h)), ("away", away, poisson(xg_a))):
        shooters = [p for p in team["xi"] if weight[p["pos"]]] or team["xi"]
        for _ in range(min(n, 9)):
            # conf_mult: 유망주 자신감 — 자신감이 높을수록 골 · 도움 기회를 더 많이 가져간다 (다른 선수는 1)
            who = rng.choices(shooters, weights=[weight[p["pos"]] * p["ovr"] * p.get("conf_mult", 1) or 1
                                                 for p in shooters])[0] if shooters else {"name": "자책골"}
            g = {"side": side, "minute": rng.randint(1, 90), "scorer": who["name"], "scorer_id": who.get("player_id")}
            mates = [p for p in team["xi"] if p is not who and assist_w[p["pos"]]]
            if mates and rng.random() < 0.75:   # 골 4개 중 3개 정도에 도움
                a = rng.choices(mates, weights=[assist_w[p["pos"]] * p["ovr"] * p.get("conf_mult", 1) for p in mates])[0]
                g["assist"], g["assist_id"] = a["name"], a.get("player_id")
            goals.append(g)
    goals.sort(key=lambda g: g["minute"])
    return {"home": sum(g["side"] == "home" for g in goals), "away": sum(g["side"] == "away" for g in goals),
            "goals": goals}



# ───────────── 90분 문자중계 ─────────────
# 장면 → (중계 줄 앞 이모지, 문구)
_MISS = "{p}, 1대1 찬스를 놓칩니다… 아쉬워요!"   # 유망주 경험치 - (옐로카드도)
_CHANCES = (
    ("🧤", "{p}의 중거리 슛! 골키퍼가 몸을 날려 막아냅니다!"),
    ("🥅", "{p}의 헤더가 골대를 강타합니다!"),
    ("😩", _MISS),
    ("🧱", "{p}의 프리킥이 벽에 걸립니다."),
    ("💨", "{p}의 슛이 골문을 살짝 벗어납니다."),
    ("🚩", "{p}, 오프사이드 깃발이 올라갑니다."),
)
_CARDS = (("🟨", "{p}에게 옐로카드! 거친 태클이었어요."), ("🟨", "{p}, 시간 끌기로 경고를 받습니다."))
_GOAL_CALLS = ("골!!! {p}!!", "{p}의 슛— 들어갑니다!!", "{p}가 해냅니다! 골!!", "그림 같은 골! {p}!!")


def match_highlights(result: dict, home: dict, away: dict, rng: random.Random = random) -> list[dict]:
    """골 + 골이 아닌 장면(선방·골대·경고)을 섞은 90분 하이라이트. [{minute, side, icon, text, goal}] 시간순.
    골이 아닌 장면엔 player_id 와 kind(card · miss · None)도 붙인다 — 유망주 경험치 감점용."""
    def pick(team):
        xi = team["xi"] or [{"name": team["name"], "pos": "MF", "ovr": 50}]
        return rng.choice([p for p in xi if p["pos"] != "GK"] or xi)

    out = [{"minute": g["minute"], "side": g["side"], "goal": True, "icon": "⚽",
            "text": rng.choice(_GOAL_CALLS).format(p=g["scorer"]) + (f" (도움 {g['assist']})" if g.get("assist") else "")}
           for g in result["goals"]]
    for _ in range(rng.randint(5, 8)):
        side = rng.choice(("home", "away"))
        team = home if side == "home" else away
        card = rng.random() < 0.2
        icon, tpl = rng.choice(_CARDS) if card else rng.choice(_CHANCES)
        who = pick(team)
        out.append({"minute": rng.randint(2, 89), "side": side, "goal": False, "icon": icon,
                    "text": tpl.format(p=who["name"]), "player_id": who.get("player_id"),
                    "kind": "card" if card else ("miss" if tpl == _MISS else None)})
    out.sort(key=lambda h: (h["minute"], not h["goal"]))
    return out


# ───────────── 감독 ─────────────
# key → (이모지, 이름, 선호 포메이션(None=상관없음), 기본 보너스, 선호 포메이션 추가 보너스, 영입비, 소개)
MANAGERS = {
    "rookie":  ("🐣", "초보 감독",        None,      1, 0, 300_000,    "열정만큼은 누구보다 뜨겁습니다"),
    "tiki":    ("🧠", "티키타카 박사",    "4-3-3",   1, 2, 3_000_000,  "점유율이 곧 승리"),
    "press":   ("⚡", "게겐프레싱 교수",  "4-2-3-1", 1, 2, 3_000_000,  "공을 잃으면 5초 안에 되찾는다"),
    "counter": ("🏹", "역습의 달인",      "4-4-2",   1, 2, 3_000_000,  "내주고, 한 방에 끝낸다"),
    "wing":    ("🪽", "측면 공격 마니아", "3-4-3",   1, 2, 3_000_000,  "측면을 지배하는 자가 경기를 지배한다"),
    "wall":    ("🧱", "철벽 수비왕",      "5-3-2",   1, 2, 3_000_000,  "무실점이 최고의 공격"),
    "legend":  ("👑", "전설의 명장",      None,      4, 0, 20_000_000, "어떤 전술이든 우승으로 만든다"),
}


# ───────────── 의료진 ─────────────
# key → (이모지, 이름, 치료능력 %, 영입비, 소개). 치료능력 = 유망주 부상 결장 기간 단축.
MEDICS = {
    "intern": ("🩹", "인턴 트레이너",      15, 500_000,    "파스 붙이기는 자신 있어요"),
    "physio": ("💆", "베테랑 물리치료사",  30, 3_000_000,  "뭉친 근육은 10분이면 풀어 드립니다"),
    "doctor": ("🩺", "스포츠 의학 박사",   45, 10_000_000, "재활 프로그램의 대가"),
    "legend": ("🏥", "전설의 메디컬 팀",   60, 30_000_000, "부러진 뼈도 금방 붙입니다"),
}


# ───────────── 시설 (2.5) ─────────────
# key → (이모지, 이름, 효과 문구({} = 레벨 × 단위), 레벨당 단위). 레벨 0~5, 다음 레벨 비용은 FACILITY_COSTS[지금 레벨].
FACILITIES = {
    "stadium":  ("🏟️", "경기장",        "공식경기 적중 상금 +{}%", 4),
    "training": ("🏋️", "훈련장",        "유망주 경험치 +{}%", 10),
    "youth":    ("🎓", "유스 아카데미", "새 유망주 잠재력 +{}", 1),
    "medical":  ("🏥", "메디컬 센터",   "유망주 부상 확률 -{}%", 10),
}
FACILITY_COSTS = (10_000_000, 30_000_000, 100_000_000, 300_000_000, 1_000_000_000)
FACILITY_MAX = len(FACILITY_COSTS)


def facility_effect(key: str, level: int) -> str:
    return FACILITIES[key][2].format(level * FACILITIES[key][3])


# ───────────── 구단 꾸미기 (2.5) ─────────────
EMBLEMS = ("🦁", "🐯", "🦅", "🐺", "🐉", "🦈", "🐻", "🦊", "🐝", "🦄",
           "⚡", "🔥", "⭐", "👑", "🛡️", "⚔️", "🌙", "☀️", "🌊", "💎")
EMBLEM_PRICE = 10_000_000
STADIUM_PRICE = 30_000_000
STADIUM_NAME_MAX = 20


# ───────────── 명문 구단 (2.5) ─────────────
# 인수하면 스쿼드 B 로 경기에 쓸 수 있다. 선수는 가상 선수이고 능력치는 영원히 고정 (구단 key 로 시드를 고정해 만든다).
# 주인이 없으면 표시 가격, 주인이 있으면 지금 가격의 1.5배를 내고 빼앗는다 (낸 돈은 전 주인에게). 한 사람에 한 구단.
# 규모 → (기본 가격, 하루 수입, 평균 OVR)
ELITE_SIZES = {"메가": (1_000_000_000, 10_000_000, 84), "빅": (500_000_000, 5_000_000, 80),
               "미드": (200_000_000, 2_000_000, 76)}
ELITE_CLUBS = {   # key → (엠블럼, 이름, 규모)
    "royal": ("🦁", "로열 런던", "메가"), "blancos": ("👑", "마드리드 블랑코스", "메가"),
    "redstar": ("🔴", "뮌헨 레드스타", "빅"), "diavoli": ("😈", "밀라노 디아볼리", "빅"),
    "catalunya": ("🔵", "카탈루냐 FC", "빅"), "lumiere": ("🗼", "파리 루미에르", "미드"),
    "bianconeri": ("🦓", "토리노 비앙코네리", "미드"), "mersey": ("🐦", "머지사이드 레즈", "미드"),
    "ruhr": ("🐝", "루르 옐로우", "미드"), "ideal": ("❌", "암스테르담 아이디얼", "미드"),
}
ELITE_TAKEOVER = 1.5
ELITE_FORMATION = "4-3-3"
ELITE_ID = "EL:"
_EL_FIRST = ("루카스", "마르코", "다니엘", "알렉스", "라파엘", "안드레", "세르히오", "니콜라스", "에밀", "파블로",
             "레오", "주앙", "이반", "하비", "마테오", "올리버", "휴고", "카이", "엔조", "토비")
_EL_LAST = ("실바", "무어", "코스타", "베르그", "로시", "가르시아", "산토스", "페레스", "노박", "클라인",
            "모레노", "리베라", "브룩스", "하트", "로렌", "바르가스", "에릭손", "다비드", "포르테", "레만")
_EL_NATIONS = ("잉글랜드", "스페인", "독일", "이탈리아", "프랑스", "브라질", "아르헨티나", "포르투갈", "네덜란드", "벨기에")


def elite_price(key: str, owner: Optional[int], price: int) -> int:
    """지금 사려면 내야 하는 돈."""
    return round(price * ELITE_TAKEOVER) if owner else price


def elite_team(key: str) -> dict:
    """명문 구단의 고정 선발 11명과 전력. get_team 과 같은 모양 (경기에 그대로 쓴다)."""
    emblem, name, size = ELITE_CLUBS[key]
    avg = ELITE_SIZES[size][2]
    rng = random.Random(f"elite-{key}")   # 시드 고정 — 몇 번을 만들어도 같은 선수
    lineup = []
    for i, slot in enumerate(FORMATIONS[ELITE_FORMATION]):
        lineup.append({"slot": slot, "index": i, "player_id": f"{ELITE_ID}{key}:{i}",
                       "name": f"{rng.choice(_EL_FIRST)} {rng.choice(_EL_LAST)}", "pos": SLOT_GROUP[slot],
                       "ovr": avg + rng.randint(-4, 4), "nation": rng.choice(_EL_NATIONS)})
    team = {"key": key, "name": name, "emblem": emblem, "size": size, "formation": ELITE_FORMATION, "captain": None,
            "lineup": lineup, "manager": None, "medic": None, "manager_bonus": 0, "squad": "B", "stadium": None}
    team.update(team_rating(lineup, None))
    return team


def manager_bonus(manager: Optional[str], formation: str) -> int:
    if manager not in MANAGERS:
        return 0
    _, _, fav, base, extra, _, _ = MANAGERS[manager]
    return base + (extra if fav == formation else 0)


# ───────────── 공식경기 ─────────────
# 돈을 걸고 내 경기 결과(승 · 무 · 패)를 예측한다. 배당 = 전력으로 계산한 확률의 역수 × 0.95 (하우스 5%).
OFFICIAL_MIN_BET = 1_000
OFFICIAL_MARGIN = 0.95
OFFICIAL_ODDS_CAP = 30.0


def official_odds(probs: tuple[float, float, float]) -> dict[str, float]:
    """(승, 무, 패) 확률 → {"W": 배당, "D": 배당, "L": 배당}. 1.01 ~ 30배."""
    return {k: round(min(OFFICIAL_ODDS_CAP, max(1.01, OFFICIAL_MARGIN / max(p, 1e-9))), 2)
            for k, p in zip("WDL", probs)}


def season_key(ts: int) -> int:
    """KST 기준 월 시즌 (예: 202610)."""
    t = time.gmtime(int(ts) + 9 * 3600)
    return t.tm_year * 100 + t.tm_mon


def kst_day(ts: int) -> int:
    return (int(ts) + 9 * 3600) // 86400


# ───────────── 유망주 (2.4) ─────────────
# 나만의 선수. 이적시장 · 팩 · 스카우트 · 시세와 섞이지 않게 pm_players 가 아니라 prospects 에 둔다.
# 보유 카드(pm_holdings)도 아니라서 판매 · 매물 · 트레이드는 애초에 닿지 않고, 구단 명단(_squad)에만 합쳐진다.
PROSPECT_PRICE = 5_000_000
PROSPECT_ID = "YP"               # 선발 명단 · 경기에서 쓰는 선수 ID: YP{번호}
PROSPECT_START_AGE = 17
PROSPECT_YEAR = 7 * 86400        # 유망주 1살 = 실제 7일 (일반 선수는 실제 하루에 1살)
PROSPECT_PRIME_END = 30          # 30세까지 성장 · 31세부터 해마다 OVR -1~3
PROSPECT_RETIRE_AGE = 40         # 40세가 되면 은퇴
PROSPECT_DAILY_GROWTH = 20       # 하루(KST)에 성장 경험치가 쌓이는 경기 수 (기록은 매 경기)
# 경기당 성장 경험치. 음수는 감점 — 경험치는 0 밑으로 내려가지 않는다 (OVR 은 안 떨어진다).
# rout = 3골 차 이상 대패(패배 -5 에 더해서) · card = 옐로카드 · miss = 1대1 찬스 놓침 (중계 장면 그대로)
PROSPECT_XP = {"app": 10, "goal": 6, "assist": 4, "W": 5, "D": 2, "L": -5, "rout": -5, "card": -3, "miss": -2}
PROSPECT_XP_LABEL = {"L": "패배", "rout": "대패", "card": "경고", "miss": "찬스 놓침"}
PROSPECT_NAME_MAX = 12
PROSPECT_FEET = ("오른발", "왼발", "양발")
PROSPECT_HEIGHT = (150, 210)
PROSPECT_POSITIONS = {   # 세부 포지션 → 이름 (경기에선 SLOT_GROUP 의 GK/DF/MF/FW 로 뛴다)
    "ST": "스트라이커", "LW": "왼쪽 윙어", "RW": "오른쪽 윙어", "AM": "공격형 미드필더", "CM": "중앙 미드필더",
    "LM": "왼쪽 미드필더", "RM": "오른쪽 미드필더", "DM": "수비형 미드필더", "LB": "왼쪽 풀백", "CB": "센터백",
    "RB": "오른쪽 풀백", "LWB": "왼쪽 윙백", "RWB": "오른쪽 윙백", "GK": "골키퍼",
}
PROSPECT_ATTRS = {       # 포지션 그룹 → (세부 능력치, OVR 대비 가감)
    "FW": (("속력", 4), ("슛", 8), ("패스", -3), ("드리블", 5), ("수비", -25), ("피지컬", 0)),
    "MF": (("속력", 0), ("슛", -2), ("패스", 7), ("드리블", 5), ("수비", -8), ("피지컬", -2)),
    "DF": (("속력", -2), ("슛", -20), ("패스", -4), ("드리블", -8), ("수비", 8), ("피지컬", 6)),
    "GK": (("다이빙", 3), ("핸들링", 2), ("킥", -10), ("반응", 4), ("스피드", -25), ("위치선정", 2)),
}

# 히든 능력치 (1~20, 화면엔 숫자 대신 등급만):
#   자신감   — 처음 10. 골 · 도움 · 승리면 오르고 패배 · 대패 · 경고 · 찬스 놓침이면 떨어진다. 높을수록 골 · 도움 기회 ↑
#   부상 빈도 — 처음 3~14. 경기마다 부상 확률 = 0.5% + 부상 빈도 × 0.15%. 스테로이드 부작용으로 오른다
#   프로 의식 — 처음 4~17. 경기 성장 경험치(+일 때) × (0.7 + 프로 의식 × 0.03) — 10 이면 그대로
HIDDEN_TIERS = ("매우 낮음", "낮음", "보통", "높음", "매우 높음")
HIDDEN_SINCE = 1790830067   # 2026-10-01 04:47:47 UTC — 히든 능력치 배포(재시작) 시각. 이전 유망주는 기본값이었다


def hidden_tier(v: int) -> str:
    return HIDDEN_TIERS[min(4, max(0, (int(v) - 1) // 4))]


# 부상: 등급 → (이름, 확률, 결장 시간(시간) 범위, 부상 이름들). 의료진 치료능력만큼 결장이 줄어든다.
# 심각한 부상은 OVR -1~3 · 잠재력 -2~5, 그리고 부상이 PROSPECT_CHRONIC 번 쌓일 때마다 고질병 OVR -1 · 잠재력 -2.
INJURY_BASE, INJURY_PER_PRONE = 0.005, 0.0015
INJURIES = {
    "minor":    ("경미", 0.70, (6, 12),   ("발목 염좌", "근육 뭉침", "가벼운 타박상")),
    "moderate": ("중상", 0.25, (24, 48),  ("햄스트링 부상", "무릎 인대 염좌", "갈비뼈 타박상")),
    "severe":   ("심각", 0.05, (72, 120), ("십자인대 파열", "발목 골절", "아킬레스건 부상")),
}
PROSPECT_CHRONIC = 3

# 스테로이드 주사기(가방 아이템 'steroid') — 2.4부터 유망주에게만: 결과 → 확률
STEROID_TABLE = {
    "ovr": 0.35,      # 💪 OVR +1~3
    "pot": 0.25,      # 🌱 잠재력 +2~5
    "awaken": 0.03,   # ⭐ 각성: OVR +3 · 잠재력 +3
    "none": 0.12,     # 😐 효과 없음
    "fragile": 0.08,  # 🦴 부작용: 부상 빈도 +3~5
    "doping": 0.12,   # 🚨 약물 검출: 징계 후유증으로 OVR -2~4
    "retire": 0.05,   # ⚰️ 부작용으로 은퇴
}


def prospect_xp_need(ovr: int) -> int:
    """OVR +1 에 필요한 경험치 — 높을수록 많다 (OVR 50 → 20, 70 → 80, 90 → 140)."""
    return 20 + max(0, int(ovr) - 50) * 3


def prospect_age(created_ts: int, at_ts: int) -> int:
    return PROSPECT_START_AGE + max(0, int(at_ts) - int(created_ts)) // PROSPECT_YEAR


def prospect_attrs(pid: str, group: str, ovr: int) -> list[tuple[str, int]]:
    """세부 능력치 6개 = OVR + 포지션 가감 + 선수마다 고정된 ±4. OVR 이 오르면 같이 오른다."""
    rng = random.Random(zlib.crc32(f"attrs:{pid}".encode()))
    return [(n, max(20, min(99, int(ovr) + b + rng.randint(-4, 4)))) for n, b in PROSPECT_ATTRS[group]]


def parse_birthday(text: str) -> Optional[str]:
    """'3-15' '03/15' '3.15' '0315' '3월 15일' → '03-15'. 없는 날짜면 None (2월 29일은 된다)."""
    t = (text or "").strip()
    m = re.fullmatch(r"(\d{1,2})\s*(?:[-./]|월|\s)\s*(\d{1,2})\s*일?", t) or re.fullmatch(r"(\d{2})(\d{2})", t)
    if not m:
        return None
    try:
        d = datetime.date(2000, int(m[1]), int(m[2]))
    except ValueError:
        return None
    return f"{d.month:02d}-{d.day:02d}"


def prospect_input(name: str, nation: str, position: str, number: int, birthday: str,
                   foot: str = "오른발", height: int = 180) -> tuple[Optional[dict], str]:
    """유망주 생성 입력 검사 → (정리된 값, '') 또는 (None, 안내 문구). 마크다운 기호는 지운다."""
    clean = lambda s: re.sub(r"[*_~`|<>@#:\\]", "", s or "").strip()   # noqa: E731
    name, nation, bday = clean(name), clean(nation), parse_birthday(birthday)
    if not 1 <= len(name) <= PROSPECT_NAME_MAX:
        return None, f"이름은 1~{PROSPECT_NAME_MAX}자로 정해 주세요."
    if not 1 <= len(nation) <= 12:
        return None, "국적은 1~12자로 정해 주세요."
    if position not in PROSPECT_POSITIONS:
        return None, "포지션을 목록에서 골라 주세요."
    if not 1 <= int(number) <= 99:
        return None, "등번호는 1~99번 중에서 골라 주세요."
    if not bday:
        return None, "생일은 `3-15` 또는 `3월 15일`처럼 적어 주세요."
    if foot not in PROSPECT_FEET:
        return None, "주발은 오른발 · 왼발 · 양발 중에서 골라 주세요."
    if not PROSPECT_HEIGHT[0] <= int(height) <= PROSPECT_HEIGHT[1]:
        return None, f"키는 {PROSPECT_HEIGHT[0]}~{PROSPECT_HEIGHT[1]}cm 로 정해 주세요."
    return {"name": name, "nation": nation, "position": position, "number": int(number), "birthday": bday,
            "foot": foot, "height": int(height)}, ""


_P_COLS = ("id", "user_id", "name", "nation", "position", "number", "birthday", "foot", "height", "ovr", "pot", "xp",
           "aged", "peak_ovr", "peak_age", "apps", "goals", "assists", "day_key", "day_n", "created_ts",
           "retired_ts", "retire_reason", "retired_number",
           "confidence", "proneness", "pro", "injured_until", "injury", "injuries")
_P_SELECT = f"SELECT {', '.join(_P_COLS)} FROM prospects"


def _prospect(row, now_ts: int) -> dict:
    """DB 행 → 화면용 dict. 나이는 은퇴했으면 은퇴한 날 기준."""
    p = dict(zip(_P_COLS, row))
    p.update(pid=f"{PROSPECT_ID}{p['id']}", group=SLOT_GROUP[p["position"]],
             age=prospect_age(p["created_ts"], p["retired_ts"] or now_ts),
             injured=not p["retired_ts"] and p["injured_until"] > int(now_ts),
             weight=round(p["height"] ** 2 * 22.5 / 10_000), pot_grade=pot_grade_for_value(p["pot"]))
    return p


class ClubDB:
    def __init__(self):
        self._lock = asyncio.Lock()
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        return con

    def _init_db(self):
        con = self._connect()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS clubs (
                    user_id INTEGER PRIMARY KEY,
                    club_name TEXT NOT NULL,
                    created_ts INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            for col in (f"formation TEXT NOT NULL DEFAULT '{DEFAULT_FORMATION}'", "captain TEXT",
                        "wins INTEGER NOT NULL DEFAULT 0", "draws INTEGER NOT NULL DEFAULT 0",
                        "losses INTEGER NOT NULL DEFAULT 0", "manager TEXT", "medic TEXT",
                        "emblem TEXT", "stadium TEXT", "squad_b INTEGER NOT NULL DEFAULT 0"):
                try:
                    con.execute(f"ALTER TABLE clubs ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS club_lineup (
                    user_id INTEGER NOT NULL,
                    slot INTEGER NOT NULL,
                    player_id TEXT NOT NULL,
                    PRIMARY KEY (user_id, slot),
                    UNIQUE (user_id, player_id)
                )
                """
            )
            # 구단 생성 보너스는 평생 한 번 — 삭제 후 재생성으로 보너스를 반복해 받지 못하게 한다.
            con.execute("CREATE TABLE IF NOT EXISTS club_bonus (user_id INTEGER PRIMARY KEY)")
            # 공식경기: 월 시즌별 기록 (건 쪽과 상대 모두)
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS club_official (
                    user_id INTEGER NOT NULL,
                    season INTEGER NOT NULL,
                    day_key INTEGER NOT NULL DEFAULT 0,
                    day_count INTEGER NOT NULL DEFAULT 0,
                    points INTEGER NOT NULL DEFAULT 0,
                    w INTEGER NOT NULL DEFAULT 0, d INTEGER NOT NULL DEFAULT 0, l INTEGER NOT NULL DEFAULT 0,
                    gf INTEGER NOT NULL DEFAULT 0, ga INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, season)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS prospects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL, nation TEXT NOT NULL, position TEXT NOT NULL,
                    number INTEGER NOT NULL, birthday TEXT NOT NULL, foot TEXT NOT NULL, height INTEGER NOT NULL,
                    ovr INTEGER NOT NULL, pot INTEGER NOT NULL, xp INTEGER NOT NULL DEFAULT 0,
                    aged INTEGER NOT NULL,                              -- 노화를 반영한 마지막 나이
                    peak_ovr INTEGER NOT NULL, peak_age INTEGER NOT NULL,
                    apps INTEGER NOT NULL DEFAULT 0, goals INTEGER NOT NULL DEFAULT 0, assists INTEGER NOT NULL DEFAULT 0,
                    day_key INTEGER NOT NULL DEFAULT 0, day_n INTEGER NOT NULL DEFAULT 0,   -- 오늘 뛴 경기 수
                    created_ts INTEGER NOT NULL,
                    retired_ts INTEGER NOT NULL DEFAULT 0,              -- 0 = 현역
                    retire_reason TEXT,                                 -- self · age · steroid
                    retired_number INTEGER NOT NULL DEFAULT 0           -- 1 = 영구결번
                )
                """
            )
            # 히든 능력치 · 부상 (2.4 후반 추가 — 그 전에 만든 유망주는 기본값)
            for col in ("confidence INTEGER NOT NULL DEFAULT 10", "proneness INTEGER NOT NULL DEFAULT 8",
                        "pro INTEGER NOT NULL DEFAULT 10", "injured_until INTEGER NOT NULL DEFAULT 0", "injury TEXT",
                        "injuries INTEGER NOT NULL DEFAULT 0"):
                try:
                    con.execute(f"ALTER TABLE prospects ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass
            # 히든 능력치 전에 만든 유망주는 기본값(부상 빈도 8 · 프로 의식 10)이었다 → 한 번만 새 유망주처럼 랜덤으로.
            # 그 뒤 스테로이드 부작용으로 오른 부상 빈도(8 초과분)는 그대로 얹는다.
            con.execute("CREATE TABLE IF NOT EXISTS club_migrations (name TEXT PRIMARY KEY)")
            if con.execute("INSERT OR IGNORE INTO club_migrations(name) VALUES('prospect_hidden_random')").rowcount:
                for pid, prone in con.execute("SELECT id, proneness FROM prospects WHERE created_ts < ?",
                                              (HIDDEN_SINCE,)).fetchall():
                    con.execute("UPDATE prospects SET proneness=?, pro=? WHERE id=?",
                                (min(20, random.randint(3, 14) + max(0, prone - 8)), random.randint(4, 17), pid))
            # 현역 유망주는 한 명만
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_prospects_active ON prospects(user_id) WHERE retired_ts=0")
            # 시설 (구단을 지웠다 다시 만들어도 남는다) · 명문 구단
            con.execute("CREATE TABLE IF NOT EXISTS club_facilities (user_id INTEGER PRIMARY KEY, "
                        + ", ".join(f"{k} INTEGER NOT NULL DEFAULT 0" for k in FACILITIES) + ")")
            con.execute("CREATE TABLE IF NOT EXISTS elite_clubs (key TEXT PRIMARY KEY, owner_id INTEGER, "
                        "price INTEGER NOT NULL, bought_ts INTEGER NOT NULL DEFAULT 0, income_day INTEGER NOT NULL DEFAULT 0)")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_elite_owner ON elite_clubs(owner_id)")
            con.executemany("INSERT OR IGNORE INTO elite_clubs(key, price) VALUES(?, ?)",
                            [(k, ELITE_SIZES[size][0]) for k, (_, _, size) in ELITE_CLUBS.items()])
            con.execute("INSERT OR IGNORE INTO club_bonus(user_id) SELECT user_id FROM clubs")
            con.commit()
        finally:
            con.close()

    async def _tx(self, fn):
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("BEGIN IMMEDIATE;")
                    out = fn(con)
                    con.execute("COMMIT;")
                    return out
                except Exception:
                    con.execute("ROLLBACK;")
                    raise
                finally:
                    con.close()
            return await asyncio.to_thread(work)

    # ───────────── 구단 ─────────────
    async def get_club(self, user_id: int) -> Optional[dict]:
        def fn(con):
            row = con.execute(
                "SELECT club_name, created_ts, formation, captain, wins, draws, losses FROM clubs WHERE user_id=?",
                (int(user_id),),
            ).fetchone()
            if not row:
                return None
            keys = ("name", "created_ts", "formation", "captain", "wins", "draws", "losses")
            club = dict(zip(keys, row))
            if club["formation"] not in FORMATIONS:
                club["formation"] = DEFAULT_FORMATION
            return club
        return await self._tx(fn)

    async def create_club(self, user_id: int, club_name: str, now_ts: int) -> tuple[bool, str, bool]:
        """(성공, 메시지, 보너스 지급 여부)."""
        club_name = (club_name or "").strip()
        if not club_name:
            return False, "구단 이름이 비어 있습니다.", False
        if len(club_name) > CLUB_NAME_MAX:
            return False, f"구단 이름은 {CLUB_NAME_MAX}자 이내여야 합니다.", False

        def fn(con):
            if con.execute("SELECT 1 FROM clubs WHERE user_id=?", (int(user_id),)).fetchone():
                return False, "이미 구단이 있습니다.", False
            con.execute("INSERT INTO clubs(user_id, club_name, created_ts) VALUES(?, ?, ?)",
                        (int(user_id), club_name, int(now_ts)))
            bonus = con.execute("INSERT OR IGNORE INTO club_bonus(user_id) VALUES(?)", (int(user_id),)).rowcount > 0
            return True, "구단 생성 완료", bonus
        return await self._tx(fn)

    async def rename_club(self, user_id: int, new_name: str) -> tuple[bool, str]:
        new_name = (new_name or "").strip()
        if not new_name:
            return False, "구단 이름이 비어 있습니다."
        if len(new_name) > CLUB_NAME_MAX:
            return False, f"구단 이름은 {CLUB_NAME_MAX}자 이내여야 합니다."

        def fn(con):
            cur = con.execute("UPDATE clubs SET club_name=? WHERE user_id=?", (new_name, int(user_id)))
            if not cur.rowcount:
                return False, "구단이 없습니다. `/구단생성`을 먼저 해주세요."
            return True, "구단명 변경 완료"
        return await self._tx(fn)

    async def delete_club(self, user_id: int) -> bool:
        """구단·선발 명단·아마추어 스쿼드를 지운다. 돈과 일반 선수 카드는 그대로 둔다."""
        def fn(con):
            if not con.execute("DELETE FROM clubs WHERE user_id=?", (int(user_id),)).rowcount:
                return False
            con.execute("DELETE FROM club_lineup WHERE user_id=?", (int(user_id),))
            con.execute("DELETE FROM pm_holdings WHERE user_id=? AND player_id LIKE 'AMT_%'", (int(user_id),))
            return True
        return await self._tx(fn)

    # ───────────── 선수 명단 ─────────────
    @staticmethod
    def _squad(con, user_id: int) -> dict:
        """경기에 뛸 수 있는 보유 선수 (은퇴 제외) + 현역 유망주(부상 중이면 제외) {player_id: {...}}."""
        rows = con.execute(
            """
            SELECT p.player_id, p.name, p.position, p.ovr, p.nation
            FROM pm_holdings h JOIN pm_players p ON p.player_id = h.player_id
            WHERE h.user_id=? AND h.qty > 0 AND p.retired = 0
            """,
            (int(user_id),),
        ).fetchall()
        squad = {r[0]: {"player_id": r[0], "name": r[1], "pos": r[2], "ovr": int(r[3]), "nation": r[4]} for r in rows}
        p = ClubDB._active_prospect(con, user_id, int(time.time()))
        if p and not p["injured"]:
            squad[p["pid"]] = {"player_id": p["pid"], "name": p["name"], "pos": p["group"], "ovr": p["ovr"],
                               "nation": p["nation"], "number": p["number"], "prospect": True,
                               "conf_mult": 0.6 + p["confidence"] * 0.04}   # 자신감 10 → 1.0
        return squad

    @staticmethod
    def _lineup(con, user_id: int, formation: str, squad: dict, injured: Optional[dict] = None) -> list[dict]:
        """슬롯 11개. 팔거나 은퇴해서 더는 못 뛰는 선수는 명단에서 빼고 빈자리로 둔다.
        부상 중인 유망주(injured)는 자리를 지키되 회복할 때까지 빈자리로 계산한다."""
        slots = FORMATIONS[formation]
        rows = dict(con.execute("SELECT slot, player_id FROM club_lineup WHERE user_id=?", (int(user_id),)).fetchall())
        out = []
        for i, slot in enumerate(slots):
            pid = rows.get(i)
            if pid and pid in squad:
                out.append({"slot": slot, "index": i, **squad[pid]})
            elif injured and pid == injured["pid"]:
                out.append({"slot": slot, "index": i, "player_id": None, "injured": injured["name"], "injured_id": pid})
            else:
                if pid:
                    con.execute("DELETE FROM club_lineup WHERE user_id=? AND slot=?", (int(user_id), i))
                out.append({"slot": slot, "index": i, "player_id": None})
        con.execute("DELETE FROM club_lineup WHERE user_id=? AND slot>=?", (int(user_id), len(slots)))
        return out

    async def get_team(self, user_id: int) -> Optional[dict]:
        """구단 + 선발 11명 + 전력. 구단이 없으면 None."""
        def fn(con):
            row = con.execute(
                "SELECT club_name, created_ts, formation, captain, wins, draws, losses, manager, medic, emblem, stadium, "
                "squad_b FROM clubs WHERE user_id=?", (int(user_id),),
            ).fetchone()
            if not row:
                return None
            club = dict(zip(("name", "created_ts", "formation", "captain", "wins", "draws", "losses", "manager", "medic",
                             "emblem", "stadium", "squad_b"), row))
            el = con.execute("SELECT key FROM elite_clubs WHERE owner_id=?", (int(user_id),)).fetchone()
            club["elite"] = el[0] if el else None
            if club["formation"] not in FORMATIONS:
                club["formation"] = DEFAULT_FORMATION
            squad = self._squad(con, user_id)
            p = self._active_prospect(con, user_id, int(time.time()))
            lineup = self._lineup(con, user_id, club["formation"], squad, p if p and p["injured"] else None)
            # 부상 중인 유망주는 주장 완장도 지킨다 (보너스는 복귀해야)
            if club["captain"] and club["captain"] not in {s.get("player_id") or s.get("injured_id") for s in lineup}:
                con.execute("UPDATE clubs SET captain=NULL WHERE user_id=?", (int(user_id),))
                club["captain"] = None
            club["lineup"] = lineup
            club["squad_size"] = len(squad)
            club["retired_numbers"] = [n for (n,) in con.execute(
                "SELECT number FROM prospects WHERE user_id=? AND retired_number=1 ORDER BY number", (int(user_id),))]
            club.update(team_rating(lineup, club["captain"]))
            # 감독 보너스는 선발이 한 명이라도 있을 때만 (빈 팀 전력은 그대로 30)
            club["manager_bonus"] = manager_bonus(club["manager"], club["formation"]) if club["filled"] else 0
            club["rating"] += club["manager_bonus"]
            return club
        return await self._tx(fn)

    async def match_team(self, user_id: int) -> Optional[dict]:
        """경기에 나갈 팀: 스쿼드 B(명문 구단)를 골랐고 아직 주인이면 그 구단, 아니면 내 구단(스쿼드 A)."""
        team = await self.get_team(user_id)
        if team and team["squad_b"] and team["elite"]:
            b = elite_team(team["elite"])
            b["stadium"] = team["stadium"]
            return b
        return team

    async def set_squad(self, user_id: int, use_b: bool) -> dict:
        """경기에 쓸 스쿼드 고르기. 실패 reason: no_club / no_elite(명문 구단 없음)."""
        def fn(con):
            if not con.execute("SELECT 1 FROM clubs WHERE user_id=?", (int(user_id),)).fetchone():
                return {"ok": False, "reason": "no_club"}
            el = con.execute("SELECT key FROM elite_clubs WHERE owner_id=?", (int(user_id),)).fetchone()
            if use_b and not el:
                return {"ok": False, "reason": "no_elite"}
            con.execute("UPDATE clubs SET squad_b=? WHERE user_id=?", (int(use_b), int(user_id)))
            return {"ok": True, "elite": el[0] if el else None}
        return await self._tx(fn)

    # ───────────── 시설 · 꾸미기 ─────────────
    @staticmethod
    def _facility(con, user_id: int, key: str) -> int:
        row = con.execute(f"SELECT {key} FROM club_facilities WHERE user_id=?", (int(user_id),)).fetchone()
        return int(row[0]) if row else 0

    async def facilities(self, user_id: int) -> dict[str, int]:
        return await self._tx(lambda con: {k: self._facility(con, user_id, k) for k in FACILITIES})

    @staticmethod
    def _pay(con, user_id: int, fee: int) -> Optional[int]:
        """돈을 낸다 → 남은 잔액, 모자라면 None (아무것도 안 바뀜)."""
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(user_id),))
        bal = int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (int(user_id),)).fetchone()[0])
        if bal < fee:
            return None
        con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (fee, int(user_id)))
        return bal - fee

    async def upgrade_facility(self, user_id: int, key: str) -> dict:
        """시설 한 단계 업그레이드. 실패 reason: no_club / max / balance."""
        def fn(con):
            if not con.execute("SELECT 1 FROM clubs WHERE user_id=?", (int(user_id),)).fetchone():
                return {"ok": False, "reason": "no_club"}
            lv = self._facility(con, user_id, key)
            if lv >= FACILITY_MAX:
                return {"ok": False, "reason": "max"}
            fee = FACILITY_COSTS[lv]
            bal = self._pay(con, user_id, fee)
            if bal is None:
                return {"ok": False, "reason": "balance", "fee": fee}
            con.execute("INSERT OR IGNORE INTO club_facilities(user_id) VALUES(?)", (int(user_id),))
            con.execute(f"UPDATE club_facilities SET {key}=? WHERE user_id=?", (lv + 1, int(user_id)))
            return {"ok": True, "level": lv + 1, "fee": fee, "balance": bal}
        return await self._tx(fn)

    async def decorate(self, user_id: int, col: str, value: str) -> dict:
        """구단 꾸미기: col = emblem(EMBLEMS 중) / stadium(경기장 이름). 실패 reason: no_club / same / balance."""
        fee = EMBLEM_PRICE if col == "emblem" else STADIUM_PRICE

        def fn(con):
            row = con.execute(f"SELECT {col} FROM clubs WHERE user_id=?", (int(user_id),)).fetchone()
            if not row:
                return {"ok": False, "reason": "no_club"}
            if row[0] == value:
                return {"ok": False, "reason": "same"}
            bal = self._pay(con, user_id, fee)
            if bal is None:
                return {"ok": False, "reason": "balance", "fee": fee}
            con.execute(f"UPDATE clubs SET {col}=? WHERE user_id=?", (value, int(user_id)))
            return {"ok": True, "fee": fee, "balance": bal}
        return await self._tx(fn)

    # ───────────── 명문 구단 ─────────────
    async def elite_list(self) -> list[dict]:
        def fn(con):
            rows = con.execute("SELECT key, owner_id, price, bought_ts FROM elite_clubs").fetchall()
            out = []
            for key, owner, price, bought in rows:
                if key not in ELITE_CLUBS:
                    continue
                emblem, name, size = ELITE_CLUBS[key]
                out.append({"key": key, "emblem": emblem, "name": name, "size": size, "owner_id": owner,
                            "price": int(price), "cost": elite_price(key, owner, int(price)), "bought_ts": bought,
                            "income": ELITE_SIZES[size][1], "rating": elite_team(key)["rating"]})
            order = list(ELITE_CLUBS)
            return sorted(out, key=lambda c: order.index(c["key"]))
        return await self._tx(fn)

    async def buy_elite(self, user_id: int, key: str, now_ts: int) -> dict:
        """명문 구단 인수. 주인이 있으면 지금 가격 × 1.5 를 내고, 그 돈은 전 주인에게.
        실패 reason: no_club / mine / one(이미 다른 명문 구단 주인) / balance."""
        def fn(con):
            if not con.execute("SELECT 1 FROM clubs WHERE user_id=?", (int(user_id),)).fetchone():
                return {"ok": False, "reason": "no_club"}
            owner, price = con.execute("SELECT owner_id, price FROM elite_clubs WHERE key=?", (key,)).fetchone()
            if owner == int(user_id):
                return {"ok": False, "reason": "mine"}
            if con.execute("SELECT 1 FROM elite_clubs WHERE owner_id=?", (int(user_id),)).fetchone():
                return {"ok": False, "reason": "one"}
            cost = elite_price(key, owner, int(price))
            bal = self._pay(con, user_id, cost)
            if bal is None:
                return {"ok": False, "reason": "balance", "cost": cost}
            if owner:
                con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (owner,))
                con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (cost, owner))
            con.execute("UPDATE elite_clubs SET owner_id=?, price=?, bought_ts=?, income_day=? WHERE key=?",
                        (int(user_id), cost, int(now_ts), kst_day(now_ts), key))
            return {"ok": True, "cost": cost, "prev_owner": owner, "balance": bal}
        return await self._tx(fn)

    async def settle_elite_income(self, now_ts: int) -> list[dict]:
        """하루(KST)가 지날 때마다 명문 구단 주인에게 규모별 수입. [{owner_id, key, days, amount}]."""
        today = kst_day(now_ts)

        def fn(con):
            out = []
            for key, owner, day in con.execute(
                    "SELECT key, owner_id, income_day FROM elite_clubs WHERE owner_id IS NOT NULL AND income_day<?",
                    (today,)).fetchall():
                if key not in ELITE_CLUBS:
                    continue
                days = today - int(day)
                amount = days * ELITE_SIZES[ELITE_CLUBS[key][2]][1]
                con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (owner,))
                con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (amount, owner))
                con.execute("UPDATE elite_clubs SET income_day=? WHERE key=?", (today, key))
                out.append({"owner_id": owner, "key": key, "days": days, "amount": amount})
            return out
        return await self._tx(fn)

    async def squad(self, user_id: int) -> list[dict]:
        return await self._tx(lambda con: sorted(self._squad(con, user_id).values(), key=lambda p: -p["ovr"]))

    async def set_formation(self, user_id: int, formation: str) -> tuple[bool, str]:
        """포메이션을 바꾸고, 지금 선발 11명을 새 자리에 맞게 다시 앉힌다."""
        if formation not in FORMATIONS:
            return False, "없는 포메이션입니다."

        def fn(con):
            club = con.execute("SELECT formation FROM clubs WHERE user_id=?", (int(user_id),)).fetchone()
            if not club:
                return False, "구단이 없습니다. `/구단생성`을 먼저 해주세요."
            squad = self._squad(con, user_id)
            current = [pid for (pid,) in con.execute("SELECT player_id FROM club_lineup WHERE user_id=?", (int(user_id),))
                       if pid in squad]
            placed = auto_assign([squad[p] for p in current], formation)
            con.execute("DELETE FROM club_lineup WHERE user_id=?", (int(user_id),))
            con.executemany("INSERT INTO club_lineup(user_id, slot, player_id) VALUES(?, ?, ?)",
                            [(int(user_id), i, pid) for i, pid in enumerate(placed) if pid])
            con.execute("UPDATE clubs SET formation=? WHERE user_id=?", (formation, int(user_id)))
            return True, f"포메이션을 **{formation}**(으)로 바꿨습니다."
        return await self._tx(fn)

    async def set_slot(self, user_id: int, slot_index: int, player_id: Optional[str]) -> tuple[bool, str]:
        """slot_index 자리에 선수를 넣는다(None 이면 비운다). 다른 자리에 있던 같은 선수는 그 자리에서 빠진다."""
        def fn(con):
            club = con.execute("SELECT formation FROM clubs WHERE user_id=?", (int(user_id),)).fetchone()
            if not club:
                return False, "구단이 없습니다. `/구단생성`을 먼저 해주세요."
            slots = FORMATIONS.get(club[0], FORMATIONS[DEFAULT_FORMATION])
            if not 0 <= slot_index < len(slots):
                return False, "없는 자리입니다."
            con.execute("DELETE FROM club_lineup WHERE user_id=? AND slot=?", (int(user_id), slot_index))
            if player_id is None:
                return True, f"**{slots[slot_index]}** 자리를 비웠습니다."
            squad = self._squad(con, user_id)
            if player_id not in squad:
                if str(player_id).startswith(PROSPECT_ID):
                    return False, "부상 중이거나 은퇴한 유망주는 넣을 수 없습니다. (부상은 복귀한 뒤에)"
                return False, "보유 중인 현역 선수만 넣을 수 있습니다."
            con.execute("DELETE FROM club_lineup WHERE user_id=? AND player_id=?", (int(user_id), player_id))
            con.execute("INSERT INTO club_lineup(user_id, slot, player_id) VALUES(?, ?, ?)",
                        (int(user_id), slot_index, player_id))
            p = squad[player_id]
            eff = effective_ovr(p["ovr"], p["pos"], slots[slot_index])
            note = "" if eff == p["ovr"] else f" (제 포지션 아님: {p['ovr']} → {eff})"
            return True, f"**{slots[slot_index]}** 자리에 **{p['name']}** 배치{note}"
        return await self._tx(fn)

    async def auto_lineup(self, user_id: int) -> tuple[bool, str]:
        def fn(con):
            club = con.execute("SELECT formation FROM clubs WHERE user_id=?", (int(user_id),)).fetchone()
            if not club:
                return False, "구단이 없습니다. `/구단생성`을 먼저 해주세요."
            formation = club[0] if club[0] in FORMATIONS else DEFAULT_FORMATION
            squad = self._squad(con, user_id)
            if not squad:
                return False, "뛸 수 있는 선수가 없습니다."
            placed = auto_assign(list(squad.values()), formation)
            con.execute("DELETE FROM club_lineup WHERE user_id=?", (int(user_id),))
            con.executemany("INSERT INTO club_lineup(user_id, slot, player_id) VALUES(?, ?, ?)",
                            [(int(user_id), i, pid) for i, pid in enumerate(placed) if pid])
            return True, "가장 강한 11명으로 자동 편성했습니다."
        return await self._tx(fn)

    async def set_captain(self, user_id: int, player_id: str) -> tuple[bool, str]:
        def fn(con):
            if not con.execute("SELECT 1 FROM clubs WHERE user_id=?", (int(user_id),)).fetchone():
                return False, "구단이 없습니다. `/구단생성`을 먼저 해주세요."
            in_xi = con.execute("SELECT 1 FROM club_lineup WHERE user_id=? AND player_id=?",
                                (int(user_id), player_id)).fetchone()
            p = self._squad(con, user_id).get(player_id)   # 유망주는 pm_players 에 없어서 명단에서 이름을 찾는다
            if not in_xi or not p:
                return False, "선발 11명 중에서만 주장을 뽑을 수 있습니다."
            con.execute("UPDATE clubs SET captain=? WHERE user_id=?", (player_id, int(user_id)))
            return True, f"**{p['name']}**을(를) 주장으로 임명했습니다. (전력 +1)"
        return await self._tx(fn)

    @staticmethod
    def _add_record(con, home_id: int, away_id: int, home_goals: int, away_goals: int) -> None:
        """/구단 전적(승 · 무 · 패)을 두 구단 모두에 반영한다."""
        for uid, gf, ga in ((home_id, home_goals, away_goals), (away_id, away_goals, home_goals)):
            col = "wins" if gf > ga else ("draws" if gf == ga else "losses")
            con.execute(f"UPDATE clubs SET {col}={col}+1 WHERE user_id=?", (int(uid),))

    async def record_match(self, home_id: int, away_id: int, home_goals: int, away_goals: int) -> None:
        await self._tx(lambda con: self._add_record(con, home_id, away_id, home_goals, away_goals))

    # ───────────── 감독 · 의료진 ─────────────
    async def hire_manager(self, user_id: int, key: str) -> dict:
        """감독 영입: 영입비를 내고 감독을 바꾼다. 실패 reason: no_club / same / balance."""
        return await self._hire(user_id, "manager", key, MANAGERS[key][5])

    async def hire_medic(self, user_id: int, key: str) -> dict:
        """의료진 영입 — 감독과 같은 방식."""
        return await self._hire(user_id, "medic", key, MEDICS[key][3])

    async def _hire(self, user_id: int, col: str, key: str, fee: int) -> dict:
        def fn(con):
            row = con.execute(f"SELECT {col} FROM clubs WHERE user_id=?", (int(user_id),)).fetchone()
            if not row:
                return {"ok": False, "reason": "no_club"}
            if row[0] == key:
                return {"ok": False, "reason": "same"}
            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(user_id),))
            bal = int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (int(user_id),)).fetchone()[0])
            if bal < fee:
                return {"ok": False, "reason": "balance", "balance": bal, "fee": fee}
            con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (fee, int(user_id)))
            con.execute(f"UPDATE clubs SET {col}=? WHERE user_id=?", (key, int(user_id)))
            return {"ok": True, "prev": row[0], "fee": fee, "balance": bal - fee}
        return await self._tx(fn)

    # ───────────── 공식경기 ─────────────
    async def official_opponents(self, user_id: int) -> list[int]:
        """선발이 있는 다른 유저 구단 목록 (공식경기 상대 후보)."""
        def fn(con):
            return [r[0] for r in con.execute(
                "SELECT DISTINCT c.user_id FROM clubs c JOIN club_lineup l ON l.user_id=c.user_id WHERE c.user_id<>?",
                (int(user_id),))]
        return await self._tx(fn)

    async def record_official(self, user_id: int, opp_id: int, gf: int, ga: int, now_ts: int,
                              amount: int, pick: str, odds: float) -> dict:
        """공식경기 결과 기록 + 베팅 정산. 예측(pick)이 맞으면 순이익 = 베팅 × (배당 - 1), 틀리면 베팅금을 잃는다.
        승점 · 득실(/공식순위)과 /구단 전적은 건 쪽과 상대 모두에 기록한다 (돈은 건 쪽만)."""
        season = season_key(now_ts)
        res = "W" if gf > ga else ("D" if gf == ga else "L")
        delta = round(amount * (odds - 1)) if pick == res else -amount

        def fn(con):
            nonlocal delta
            if delta > 0:   # 경기장: 적중 상금 + 레벨 × 4%
                delta = round(delta * (1 + self._facility(con, user_id, "stadium") * FACILITIES["stadium"][3] / 100))
            for uid, f, a in ((user_id, gf, ga), (opp_id, ga, gf)):
                r = "W" if f > a else ("D" if f == a else "L")
                con.execute("INSERT OR IGNORE INTO club_official(user_id, season) VALUES(?, ?)", (int(uid), season))
                con.execute(
                    "UPDATE club_official SET points=points+?, w=w+?, d=d+?, l=l+?, gf=gf+?, ga=ga+? "
                    "WHERE user_id=? AND season=?",
                    ({"W": 3, "D": 1, "L": 0}[r], r == "W", r == "D", r == "L", f, a, int(uid), season))
            self._add_record(con, user_id, opp_id, gf, ga)
            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(user_id),))
            con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (delta, int(user_id)))
            bal = con.execute("SELECT balance FROM wallets WHERE user_id=?", (int(user_id),)).fetchone()[0]
            rec = con.execute("SELECT points, w, d, l, gf, ga FROM club_official WHERE user_id=? AND season=?",
                              (int(user_id), season)).fetchone()
            return {"result": res, "delta": delta, "balance": int(bal),
                    **dict(zip(("points", "w", "d", "l", "gf", "ga"), rec))}
        return await self._tx(fn)

    async def official_table(self, now_ts: int, limit: int = 10) -> list[dict]:
        """이번 시즌 순위: 승점 → 득실차 → 다득점."""
        season = season_key(now_ts)

        def fn(con):
            rows = con.execute(
                "SELECT o.user_id, c.club_name, o.points, o.w, o.d, o.l, o.gf, o.ga FROM club_official o "
                "JOIN clubs c ON c.user_id=o.user_id WHERE o.season=? AND (o.w+o.d+o.l)>0 "
                "ORDER BY o.points DESC, (o.gf-o.ga) DESC, o.gf DESC LIMIT ?", (season, limit)).fetchall()
            return [dict(zip(("user_id", "name", "points", "w", "d", "l", "gf", "ga"), r)) for r in rows]
        return await self._tx(fn)

    # ───────────── 유망주 ─────────────
    @staticmethod
    def _active_prospect(con, user_id: int, now_ts: int) -> Optional[dict]:
        """현역 유망주 (없으면 None). 부를 때마다 지난 나이만큼 노화를 반영하고, 은퇴 나이가 됐으면 은퇴시킨다."""
        row = con.execute(f"{_P_SELECT} WHERE user_id=? AND retired_ts=0", (int(user_id),)).fetchone()
        if not row:
            return None
        p = _prospect(row, now_ts)
        if p["age"] > p["aged"]:
            for age in range(p["aged"] + 1, p["age"] + 1):
                if age > PROSPECT_PRIME_END:
                    p["ovr"] = max(40, p["ovr"] - random.randint(1, 3))
            p["aged"] = p["age"]
            con.execute("UPDATE prospects SET ovr=?, aged=? WHERE id=?", (p["ovr"], p["aged"], p["id"]))
        if p["age"] >= PROSPECT_RETIRE_AGE:   # 40번째 생일에 은퇴한 것으로 남긴다
            at = p["created_ts"] + (PROSPECT_RETIRE_AGE - PROSPECT_START_AGE) * PROSPECT_YEAR
            con.execute("UPDATE prospects SET retired_ts=?, retire_reason='age' WHERE id=?", (at, p["id"]))
            return None
        return p

    @staticmethod
    def _prospect_by_id(con, prospect_id: int, now_ts: int) -> dict:
        return _prospect(con.execute(f"{_P_SELECT} WHERE id=?", (int(prospect_id),)).fetchone(), now_ts)

    async def prospects(self, user_id: int, now_ts: int) -> dict:
        """{"active": 현역 유망주 | None, "retired": 은퇴한 유망주(최근 순)}."""
        def fn(con):
            active = self._active_prospect(con, user_id, now_ts)
            rows = con.execute(f"{_P_SELECT} WHERE user_id=? AND retired_ts>0 ORDER BY retired_ts DESC, id DESC",
                               (int(user_id),)).fetchall()
            return {"active": active, "retired": [_prospect(r, now_ts) for r in rows]}
        return await self._tx(fn)

    @staticmethod
    def _prospect_price(con, user_id: int, now_ts: int, rookie: bool = True) -> int:
        """생성비 — 신인 부스트 기간의 첫 유망주는 반값 (2.5). rookie=False 면 (아직 2.5 전인 서버) 반값 없음."""
        if not rookie:
            return PROSPECT_PRICE
        try:
            row = con.execute("SELECT start_ts FROM rookie WHERE user_id=?", (int(user_id),)).fetchone()
        except sqlite3.OperationalError:
            row = None
        first = not con.execute("SELECT 1 FROM prospects WHERE user_id=?", (int(user_id),)).fetchone()
        return PROSPECT_PRICE // 2 if first and row and now_ts < rookie_until(row[0]) else PROSPECT_PRICE

    async def prospect_price(self, user_id: int, now_ts: int, rookie: bool = True) -> int:
        return await self._tx(lambda con: self._prospect_price(con, user_id, now_ts, rookie))

    async def create_prospect(self, user_id: int, info: dict, now_ts: int, rng=random, rookie: bool = True) -> dict:
        """유망주 생성 (PROSPECT_PRICE). info 는 prospect_input 이 정리한 값.
        OVR 50~58 · 잠재력 75~94 에서 시작. 실패 reason: exists(현역 유망주 있음) / retired_number / balance."""
        def fn(con):
            if self._active_prospect(con, user_id, now_ts):
                return {"ok": False, "reason": "exists"}
            if con.execute("SELECT 1 FROM prospects WHERE user_id=? AND number=? AND retired_number=1",
                           (int(user_id), info["number"])).fetchone():
                return {"ok": False, "reason": "retired_number"}
            price = self._prospect_price(con, user_id, now_ts, rookie)
            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(user_id),))
            bal = int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (int(user_id),)).fetchone()[0])
            if bal < price:
                return {"ok": False, "reason": "balance", "balance": bal, "price": price}
            con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (price, int(user_id)))
            ovr, pot = rng.randint(50, 58), min(99, rng.randint(75, 94) + self._facility(con, user_id, "youth"))
            prone, pro = rng.randint(3, 14), rng.randint(4, 17)   # 히든 능력치 (자신감은 10에서 시작)
            cur = con.execute(
                "INSERT INTO prospects(user_id, name, nation, position, number, birthday, foot, height, ovr, pot, aged, "
                "peak_ovr, peak_age, created_ts, proneness, pro) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (int(user_id), info["name"], info["nation"], info["position"], info["number"], info["birthday"],
                 info["foot"], info["height"], ovr, pot, PROSPECT_START_AGE, ovr, PROSPECT_START_AGE, int(now_ts),
                 prone, pro))
            return {"ok": True, "balance": bal - price, "price": price, **self._prospect_by_id(con, cur.lastrowid, now_ts)}
        return await self._tx(fn)

    async def record_prospects(self, sides: list[tuple[list[dict], int, int]], goals: list[dict], now_ts: int,
                               events: list[dict] = (), rng=random) -> list[dict]:
        """경기에 뛴 유망주 기록: 출전 · 골 · 도움, 성장 경험치 ± (하루 PROSPECT_DAILY_GROWTH 경기까지,
        30세까지, 잠재력까지, 프로 의식 배율), 자신감 변화, 부상. sides: [(선발 xi, 득점, 실점)] — 양 팀 모두.
        events: 중계 장면(경고 · 찬스 놓침). 뛴 유망주마다 결과 dict
        (xp: 이번 경기 경험치, 성장 대상이 아니면 None · minus: 감점 사유 키 · injury: 부상 dict | None)."""
        if not any(str(s.get("player_id") or "").startswith(PROSPECT_ID) for xi, _, _ in sides for s in xi):
            return []
        day = kst_day(now_ts)

        def fn(con):
            out = []
            for xi, gf, ga in sides:
                res = "W" if gf > ga else ("D" if gf == ga else "L")
                for s in xi:
                    pid = str(s.get("player_id") or "")
                    if not pid.startswith(PROSPECT_ID):
                        continue
                    owner = con.execute("SELECT user_id FROM prospects WHERE id=?", (int(pid[len(PROSPECT_ID):]),)).fetchone()
                    p = owner and self._active_prospect(con, owner[0], now_ts)
                    if not p or p["pid"] != pid:
                        continue
                    g = sum(x.get("scorer_id") == pid for x in goals)
                    a = sum(x.get("assist_id") == pid for x in goals)
                    played = p["day_n"] if p["day_key"] == day else 0
                    ovr, xp = p["ovr"], p["xp"]
                    grew = played < PROSPECT_DAILY_GROWTH and p["age"] <= PROSPECT_PRIME_END and ovr < p["pot"]
                    parts = {"app": 1, "goal": g, "assist": a, res: 1, "rout": int(ga - gf >= 3),
                             "card": sum(e.get("player_id") == pid and e.get("kind") == "card" for e in events),
                             "miss": sum(e.get("player_id") == pid and e.get("kind") == "miss" for e in events)}
                    delta = sum(PROSPECT_XP[k] * n for k, n in parts.items())
                    if delta > 0:   # 프로 의식: 10 이면 그대로, 높을수록 더 많이 (감점에는 안 붙는다)
                        delta = round(delta * (0.7 + p["pro"] * 0.03)
                                      * (1 + self._facility(con, owner[0], "training") * FACILITIES["training"][3] / 100))
                    if grew:
                        xp = max(0, xp + delta)
                        while ovr < p["pot"] and xp >= prospect_xp_need(ovr):
                            xp -= prospect_xp_need(ovr)
                            ovr += 1
                        if ovr >= p["pot"]:
                            xp = 0
                    peak = (ovr, p["age"]) if ovr > p["peak_ovr"] else (p["peak_ovr"], p["peak_age"])
                    conf = min(20, max(1, p["confidence"] + (g > 0) + (a > 0) + (res == "W") - (res == "L")
                                       - parts["rout"] - parts["card"] - parts["miss"]))
                    injury, pot = None, p["pot"]
                    medical = 1 - self._facility(con, owner[0], "medical") * FACILITIES["medical"][3] / 100
                    if rng.random() < (INJURY_BASE + p["proneness"] * INJURY_PER_PRONE) * medical:
                        injury = self._injure(con, owner[0], p, ovr, pot, now_ts, rng)
                        ovr, pot = injury["ovr"], injury["pot"]
                    con.execute(
                        "UPDATE prospects SET apps=apps+1, goals=goals+?, assists=assists+?, ovr=?, pot=?, xp=?, "
                        "peak_ovr=?, peak_age=?, day_key=?, day_n=?, confidence=?, injured_until=?, injury=?, "
                        "injuries=? WHERE id=?",
                        (g, a, ovr, pot, xp, *peak, day, played + 1, conf,
                         injury["until"] if injury else p["injured_until"],
                         f"{injury['name']} ({injury['grade']})" if injury else p["injury"],
                         p["injuries"] + bool(injury), p["id"]))
                    out.append({"user_id": owner[0], "name": p["name"], "number": p["number"], "goals": g, "assists": a,
                                "ovr0": p["ovr"], "ovr": ovr, "grew": grew, "xp": delta if grew else None,
                                "minus": [k for k, n in parts.items() if n and PROSPECT_XP[k] < 0],
                                "conf0": p["confidence"], "conf": conf, "injury": injury})
            return out
        return await self._tx(fn)

    @staticmethod
    def _injure(con, user_id: int, p: dict, ovr: int, pot: int, now_ts: int, rng) -> dict:
        """부상 굴림: 등급 · 결장 시간(의료진 치료능력만큼 단축) · 심각하면 OVR/잠재력 하락 ·
        PROSPECT_CHRONIC 번째 부상마다 고질병(OVR -1 · 잠재력 -2)."""
        grade = rng.choices(list(INJURIES), weights=[v[1] for v in INJURIES.values()])[0]
        label, _, (lo, hi), names = INJURIES[grade]
        medic = con.execute("SELECT medic FROM clubs WHERE user_id=?", (int(user_id),)).fetchone()
        heal = MEDICS[medic[0]][2] if medic and medic[0] in MEDICS else 0
        secs = round(rng.randint(lo, hi) * 3600 * (100 - heal) / 100)
        chronic = (p["injuries"] + 1) % PROSPECT_CHRONIC == 0
        new_ovr, new_pot = ovr, pot
        if grade == "severe":
            new_ovr, new_pot = new_ovr - rng.randint(1, 3), new_pot - rng.randint(2, 5)
        if chronic:
            new_ovr, new_pot = new_ovr - 1, new_pot - 2
        new_ovr = max(40, new_ovr)
        return {"name": rng.choice(names), "grade": label, "kind": grade, "until": int(now_ts) + secs,
                "hours": max(1, round(secs / 3600)), "heal": heal, "chronic": chronic,
                "ovr0": ovr, "ovr": new_ovr, "pot0": pot, "pot": max(new_pot, new_ovr)}

    async def use_steroid(self, user_id: int, now_ts: int, rng=random) -> dict:
        """가방의 스테로이드 주사기 1개를 내 현역 유망주에게. 결과 kind 는 STEROID_TABLE 의 키.
        실패 reason: none(주사기 없음) / no_prospect(현역 유망주 없음) — 주사기는 그대로."""
        def fn(con):
            row = con.execute("SELECT qty FROM inventory WHERE user_id=? AND item='steroid'", (int(user_id),)).fetchone()
            if not row or int(row[0]) <= 0:
                return {"ok": False, "reason": "none"}
            p = self._active_prospect(con, user_id, now_ts)
            if not p:
                return {"ok": False, "reason": "no_prospect"}
            con.execute("UPDATE inventory SET qty = qty - 1 WHERE user_id=? AND item='steroid'", (int(user_id),))
            kind = rng.choices(list(STEROID_TABLE), weights=list(STEROID_TABLE.values()))[0]
            ovr, pot = p["ovr"], p["pot"]
            if kind == "ovr":
                ovr = min(99, ovr + rng.randint(1, 3))
            elif kind == "pot":
                pot = min(99, pot + rng.randint(2, 5))
            elif kind == "awaken":
                ovr, pot = min(99, ovr + 3), min(99, pot + 3)
            elif kind == "doping":
                ovr = max(40, ovr - rng.randint(2, 4))
            prone = min(20, p["proneness"] + rng.randint(3, 5)) if kind == "fragile" else p["proneness"]
            pot = max(pot, ovr)
            peak = (ovr, p["age"]) if ovr > p["peak_ovr"] else (p["peak_ovr"], p["peak_age"])
            con.execute("UPDATE prospects SET ovr=?, pot=?, peak_ovr=?, peak_age=?, proneness=? WHERE id=?",
                        (ovr, pot, *peak, prone, p["id"]))
            if kind == "retire":
                con.execute("UPDATE prospects SET retired_ts=?, retire_reason='steroid' WHERE id=?", (int(now_ts), p["id"]))
            return {"ok": True, "kind": kind, "name": p["name"], "number": p["number"], "age": p["age"],
                    "ovr0": p["ovr"], "pot0": p["pot"], "ovr": ovr, "pot": pot, "pot_grade": pot_grade_for_value(pot),
                    "prone0": p["proneness"], "prone": prone}
        return await self._tx(fn)

    async def retire_prospect(self, user_id: int, prospect_id: int, now_ts: int, retire_number: bool) -> Optional[dict]:
        """현역 유망주 은퇴 (+ 등번호 영구결번). 이미 은퇴했거나 다른 선수면 None."""
        def fn(con):
            p = self._active_prospect(con, user_id, now_ts)
            if not p or p["id"] != int(prospect_id):
                return None
            con.execute("UPDATE prospects SET retired_ts=?, retire_reason='self', retired_number=? WHERE id=?",
                        (int(now_ts), int(bool(retire_number)), p["id"]))
            return self._prospect_by_id(con, p["id"], now_ts)
        return await self._tx(fn)

    async def delete_prospect(self, user_id: int, prospect_id: int, now_ts: int) -> Optional[dict]:
        """현역 유망주를 기록 없이 지운다 (명예의 전당에 안 남고 환불도 없다). 이미 없거나 다른 선수면 None.
        선발 명단 · 주장 자리는 다음 조회 때 _lineup / get_team 이 비운다."""
        def fn(con):
            p = self._active_prospect(con, user_id, now_ts)
            if not p or p["id"] != int(prospect_id):
                return None
            con.execute("DELETE FROM prospects WHERE id=?", (p["id"],))
            return p
        return await self._tx(fn)

    async def retire_number(self, user_id: int, prospect_id: int, now_ts: int) -> dict:
        """은퇴한 내 유망주의 등번호를 영구결번. 실패 reason: none(내 은퇴 선수 아님) / done(이미 영구결번) /
        taken(그 번호는 이미 영구결번) / wearing(현역 유망주가 그 번호를 달고 있음)."""
        def fn(con):
            row = con.execute(f"{_P_SELECT} WHERE id=? AND user_id=? AND retired_ts>0",
                              (int(prospect_id), int(user_id))).fetchone()
            if not row:
                return {"ok": False, "reason": "none"}
            p = _prospect(row, now_ts)
            if p["retired_number"]:
                return {"ok": False, "reason": "done", **p}
            if con.execute("SELECT 1 FROM prospects WHERE user_id=? AND number=? AND retired_number=1",
                           (int(user_id), p["number"])).fetchone():
                return {"ok": False, "reason": "taken", **p}
            active = self._active_prospect(con, user_id, now_ts)
            if active and active["number"] == p["number"]:
                return {"ok": False, "reason": "wearing", **p}
            con.execute("UPDATE prospects SET retired_number=1 WHERE id=?", (p["id"],))
            return {"ok": True, **p, "retired_number": 1}
        return await self._tx(fn)
