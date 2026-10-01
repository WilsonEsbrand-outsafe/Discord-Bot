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
            who = rng.choices(shooters, weights=[weight[p["pos"]] * p["ovr"] or 1 for p in shooters])[0] \
                if shooters else {"name": "자책골"}
            g = {"side": side, "minute": rng.randint(1, 90), "scorer": who["name"], "scorer_id": who.get("player_id")}
            mates = [p for p in team["xi"] if p is not who and assist_w[p["pos"]]]
            if mates and rng.random() < 0.75:   # 골 4개 중 3개 정도에 도움
                a = rng.choices(mates, weights=[assist_w[p["pos"]] * p["ovr"] for p in mates])[0]
                g["assist"], g["assist_id"] = a["name"], a.get("player_id")
            goals.append(g)
    goals.sort(key=lambda g: g["minute"])
    return {"home": sum(g["side"] == "home" for g in goals), "away": sum(g["side"] == "away" for g in goals),
            "goals": goals}



# ───────────── 90분 문자중계 ─────────────
_CHANCES = (
    "{p}의 중거리 슛! 골키퍼가 몸을 날려 막아냅니다!",
    "{p}의 헤더가 골대를 강타합니다!",
    "{p}, 1대1 찬스를 놓칩니다… 아쉬워요!",
    "{p}의 프리킥이 벽에 걸립니다.",
    "{p}의 슛이 골문을 살짝 벗어납니다.",
    "{p}, 오프사이드 깃발이 올라갑니다.",
)
_CARDS = ("{p}에게 옐로카드! 거친 태클이었어요.", "{p}, 시간 끌기로 경고를 받습니다.")
_GOAL_CALLS = ("골!!! {p}!!", "{p}의 슛— 들어갑니다!!", "{p}가 해냅니다! 골!!", "그림 같은 골! {p}!!")


def match_highlights(result: dict, home: dict, away: dict, rng: random.Random = random) -> list[dict]:
    """골 + 골이 아닌 장면(선방·골대·경고)을 섞은 90분 하이라이트. [{minute, side, text, goal}] 시간순."""
    def pick(team):
        xi = team["xi"] or [{"name": team["name"], "pos": "MF", "ovr": 50}]
        return rng.choice([p for p in xi if p["pos"] != "GK"] or xi)["name"]

    out = [{"minute": g["minute"], "side": g["side"], "goal": True,
            "text": rng.choice(_GOAL_CALLS).format(p=g["scorer"]) + (f" (도움 {g['assist']})" if g.get("assist") else "")}
           for g in result["goals"]]
    for _ in range(rng.randint(5, 8)):
        side = rng.choice(("home", "away"))
        team = home if side == "home" else away
        tpl = rng.choice(_CARDS) if rng.random() < 0.2 else rng.choice(_CHANCES)
        out.append({"minute": rng.randint(2, 89), "side": side, "goal": False, "text": tpl.format(p=pick(team))})
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
PROSPECT_XP = {"app": 10, "goal": 6, "assist": 4, "W": 5, "D": 2, "L": 0}
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

# 스테로이드 주사기(가방 아이템 'steroid') — 2.4부터 유망주에게만: 결과 → 확률
STEROID_TABLE = {
    "ovr": 0.35,      # 💪 OVR +1~3
    "pot": 0.25,      # 🌱 잠재력 +2~5
    "awaken": 0.03,   # ⭐ 각성: OVR +3 · 잠재력 +3
    "none": 0.20,     # 😐 효과 없음
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
           "retired_ts", "retire_reason", "retired_number")
_P_SELECT = f"SELECT {', '.join(_P_COLS)} FROM prospects"


def _prospect(row, now_ts: int) -> dict:
    """DB 행 → 화면용 dict. 나이는 은퇴했으면 은퇴한 날 기준."""
    p = dict(zip(_P_COLS, row))
    p.update(pid=f"{PROSPECT_ID}{p['id']}", group=SLOT_GROUP[p["position"]],
             age=prospect_age(p["created_ts"], p["retired_ts"] or now_ts),
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
                        "losses INTEGER NOT NULL DEFAULT 0", "manager TEXT"):
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
            # 현역 유망주는 한 명만
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_prospects_active ON prospects(user_id) WHERE retired_ts=0")
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
        """경기에 뛸 수 있는 보유 선수 (은퇴 제외) + 현역 유망주 {player_id: {...}}."""
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
        if p:
            squad[p["pid"]] = {"player_id": p["pid"], "name": p["name"], "pos": p["group"], "ovr": p["ovr"],
                               "nation": p["nation"], "number": p["number"], "prospect": True}
        return squad

    @staticmethod
    def _lineup(con, user_id: int, formation: str, squad: dict) -> list[dict]:
        """슬롯 11개. 팔거나 은퇴해서 더는 못 뛰는 선수는 명단에서 빼고 빈자리로 둔다."""
        slots = FORMATIONS[formation]
        rows = dict(con.execute("SELECT slot, player_id FROM club_lineup WHERE user_id=?", (int(user_id),)).fetchall())
        out = []
        for i, slot in enumerate(slots):
            pid = rows.get(i)
            if pid and pid in squad:
                out.append({"slot": slot, "index": i, **squad[pid]})
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
                "SELECT club_name, created_ts, formation, captain, wins, draws, losses, manager FROM clubs WHERE user_id=?",
                (int(user_id),),
            ).fetchone()
            if not row:
                return None
            club = dict(zip(("name", "created_ts", "formation", "captain", "wins", "draws", "losses", "manager"), row))
            if club["formation"] not in FORMATIONS:
                club["formation"] = DEFAULT_FORMATION
            squad = self._squad(con, user_id)
            lineup = self._lineup(con, user_id, club["formation"], squad)
            if club["captain"] and club["captain"] not in {s.get("player_id") for s in lineup}:
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

    # ───────────── 감독 ─────────────
    async def hire_manager(self, user_id: int, key: str) -> dict:
        """감독 영입: 영입비를 내고 감독을 바꾼다. 실패 reason: no_club / same / balance."""
        fee = MANAGERS[key][5]

        def fn(con):
            row = con.execute("SELECT manager FROM clubs WHERE user_id=?", (int(user_id),)).fetchone()
            if not row:
                return {"ok": False, "reason": "no_club"}
            if row[0] == key:
                return {"ok": False, "reason": "same"}
            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(user_id),))
            bal = int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (int(user_id),)).fetchone()[0])
            if bal < fee:
                return {"ok": False, "reason": "balance", "balance": bal, "fee": fee}
            con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (fee, int(user_id)))
            con.execute("UPDATE clubs SET manager=? WHERE user_id=?", (key, int(user_id)))
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

    async def create_prospect(self, user_id: int, info: dict, now_ts: int, rng=random) -> dict:
        """유망주 생성 (PROSPECT_PRICE). info 는 prospect_input 이 정리한 값.
        OVR 50~58 · 잠재력 75~94 에서 시작. 실패 reason: exists(현역 유망주 있음) / retired_number / balance."""
        def fn(con):
            if self._active_prospect(con, user_id, now_ts):
                return {"ok": False, "reason": "exists"}
            if con.execute("SELECT 1 FROM prospects WHERE user_id=? AND number=? AND retired_number=1",
                           (int(user_id), info["number"])).fetchone():
                return {"ok": False, "reason": "retired_number"}
            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(user_id),))
            bal = int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (int(user_id),)).fetchone()[0])
            if bal < PROSPECT_PRICE:
                return {"ok": False, "reason": "balance", "balance": bal}
            con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (PROSPECT_PRICE, int(user_id)))
            ovr, pot = rng.randint(50, 58), rng.randint(75, 94)
            cur = con.execute(
                "INSERT INTO prospects(user_id, name, nation, position, number, birthday, foot, height, ovr, pot, aged, "
                "peak_ovr, peak_age, created_ts) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (int(user_id), info["name"], info["nation"], info["position"], info["number"], info["birthday"],
                 info["foot"], info["height"], ovr, pot, PROSPECT_START_AGE, ovr, PROSPECT_START_AGE, int(now_ts)))
            return {"ok": True, "balance": bal - PROSPECT_PRICE, **self._prospect_by_id(con, cur.lastrowid, now_ts)}
        return await self._tx(fn)

    async def record_prospects(self, sides: list[tuple[list[dict], int, int]], goals: list[dict], now_ts: int) -> list[dict]:
        """경기에 뛴 유망주 기록: 출전 · 골 · 도움, 그리고 성장 경험치 (하루 PROSPECT_DAILY_GROWTH 경기까지,
        30세까지, 잠재력까지). sides: [(선발 xi, 득점, 실점)] — 양 팀 모두. 뛴 유망주마다 결과 dict."""
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
                    if grew:
                        xp += PROSPECT_XP["app"] + g * PROSPECT_XP["goal"] + a * PROSPECT_XP["assist"] + PROSPECT_XP[res]
                        while ovr < p["pot"] and xp >= prospect_xp_need(ovr):
                            xp -= prospect_xp_need(ovr)
                            ovr += 1
                        if ovr >= p["pot"]:
                            xp = 0
                    peak = (ovr, p["age"]) if ovr > p["peak_ovr"] else (p["peak_ovr"], p["peak_age"])
                    con.execute(
                        "UPDATE prospects SET apps=apps+1, goals=goals+?, assists=assists+?, ovr=?, xp=?, "
                        "peak_ovr=?, peak_age=?, day_key=?, day_n=? WHERE id=?",
                        (g, a, ovr, xp, *peak, day, played + 1, p["id"]))
                    out.append({"user_id": owner[0], "name": p["name"], "number": p["number"], "goals": g, "assists": a,
                                "ovr0": p["ovr"], "ovr": ovr, "grew": grew})
            return out
        return await self._tx(fn)

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
            pot = max(pot, ovr)
            peak = (ovr, p["age"]) if ovr > p["peak_ovr"] else (p["peak_ovr"], p["peak_age"])
            con.execute("UPDATE prospects SET ovr=?, pot=?, peak_ovr=?, peak_age=? WHERE id=?", (ovr, pot, *peak, p["id"]))
            if kind == "retire":
                con.execute("UPDATE prospects SET retired_ts=?, retire_reason='steroid' WHERE id=?", (int(now_ts), p["id"]))
            return {"ok": True, "kind": kind, "name": p["name"], "number": p["number"], "age": p["age"],
                    "ovr0": p["ovr"], "pot0": p["pot"], "ovr": ovr, "pot": pot, "pot_grade": pot_grade_for_value(pot)}
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
