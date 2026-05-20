# services/player_market_db.py
from __future__ import annotations

import asyncio
import random
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple, Dict

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "economy.sqlite3"
KST_OFFSET = 9 * 3600

# ─────────────────────────────────────────────
# 설계 파라미터(확정)
# ─────────────────────────────────────────────
MARKET_OPEN_HOUR = 9
MARKET_CLOSE_HOUR = 23
TICK_SECONDS = 600  # 10분

REAL_SECONDS_PER_MONTH = 2 * 3600  # 2시간 = 1개월
MONTHS_PER_YEAR = 12

SELL_FEE_RATE = 0.05

# 시장 변동폭
TICK_CAP = 0.13  # ±13%
SIGMA = 0.035    # 체감: 보통 ±2~6%, 가끔 큰 틱

# 방향(추세/되돌림)
MOMENTUM = 0.08
MEAN_REVERT = 0.25
P_UP_MIN, P_UP_MAX = 0.20, 0.80

# 악재 확률(월)
ADVERSE_PROB = 0.01

# 성장 확률(월 1회)
GROWTH_PROB_BY_AGE = [
    (18, 0.06),  # 17~18
    (20, 0.05),  # 19~20
    (22, 0.04),  # 21~22
    (24, 0.03),  # 23~24
    (26, 0.02),  # 25~26
    (28, 0.01),  # 27~28
    (999, 0.00), # 29+
]

def growth_penalty(gap: int) -> float:
    if gap >= 10:
        return 1.0
    if 5 <= gap <= 9:
        return 0.6
    if 1 <= gap <= 4:
        return 0.3
    return 0.0

# 은퇴 확률(월)
def retire_prob(age: int) -> float:
    if age < 33:
        return 0.0
    if age <= 34:
        return 0.005
    if age <= 36:
        return 0.010
    if age <= 38:
        return 0.020
    if age <= 40:
        return 0.040
    return 0.080

# 포지션
POSITIONS = ["GK", "DF", "MF", "FW"]

# ───────────── 팩 5종(가격/확률) ─────────────
PACKS = {
    "브론즈": {
        "price": 20000,
        "weights": [("S", 0.002), ("A", 0.018), ("B", 0.15), ("C", 0.55), ("D", 0.28)],
    },
    "실버": {
        "price": 50000,
        "weights": [("S", 0.008), ("A", 0.05), ("B", 0.24), ("C", 0.48), ("D", 0.222)],
    },
    "골드": {
        "price": 120000,
        "weights": [("S", 0.02), ("A", 0.10), ("B", 0.35), ("C", 0.40), ("D", 0.13)],
    },
    "플래티넘": {
        "price": 250000,
        "weights": [("S", 0.05), ("A", 0.18), ("B", 0.45), ("C", 0.27), ("D", 0.05)],
    },
    "아이콘": {
        "price": 500000,
        "weights": [("S", 0.10), ("A", 0.28), ("B", 0.45), ("C", 0.15), ("D", 0.02)],
    },
}
PACK_MAX_PULLS = 10

# ───────────── 국적 풀(가중치) ─────────────
# 숫자는 상대 비율(총합 1.0 필요 없음)
NATIONS = [
    ("대한민국", 8), ("일본", 6), ("중국", 5),

    ("잉글랜드", 8), ("프랑스", 7), ("독일", 7), ("스페인", 7), ("이탈리아", 6),
    ("포르투갈", 4), ("네덜란드", 4), ("벨기에", 3), ("크로아티아", 3), ("세르비아", 3),

    ("스웨덴", 2), ("노르웨이", 2), ("덴마크", 2), ("폴란드", 3), ("체코", 2),
    ("스위스", 2), ("오스트리아", 2), ("터키", 3), ("그리스", 2),

    ("브라질", 8), ("아르헨티나", 7), ("우루과이", 2), ("콜롬비아", 3), ("칠레", 2), ("페루", 2),

    ("멕시코", 3), ("미국", 3), ("캐나다", 2),

    ("모로코", 3), ("알제리", 2), ("튀니지", 2), ("이집트", 2),
    ("나이지리아", 3), ("가나", 2), ("세네갈", 2), ("카메룬", 2), ("코트디부아르", 2),

    ("호주", 2),
]

def pick_weighted_nation() -> str:
    total = sum(w for _, w in NATIONS)
    r = random.uniform(0, float(total))
    acc = 0.0
    for nation, w in NATIONS:
        acc += float(w)
        if r <= acc:
            return nation
    return NATIONS[-1][0]

# ───────────── 이름 풀(전부 한글 표기) ─────────────
KOR_LAST = ["김","이","박","최","정","강","조","윤","장","임","한","오","서","신","권","황","안","송","전","홍"]
KOR_FIRST = ["민준","서준","도윤","예준","시우","하준","지호","지후","준우","현우","유준","지훈"]

JPN_LAST = ["사토","스즈키","다나카","와타나베","이토","야마모토","나카무라","고바야시","가토","요시다"]
JPN_FIRST = ["하루토","유토","소타","렌","하야토","카이토","다이키","켄타","슌","유이치"]

CHN_LAST = ["왕","리","장","류","천","양","자오","우","저우","쉬"]
CHN_FIRST = ["웨이","하오","지에","밍","준","레이","천위","하오위","즈하오","위천"]

ENG_LAST = ["스미스","존슨","윌리엄스","브라운","존스","밀러","데이비스","윌슨","무어","테일러"]
ENG_FIRST = ["제임스","잭","해리","찰리","토마스","조지","올리버","루카스","메이슨","라이언"]

FRA_LAST = ["마르탱","뒤부아","베르나르","르루아","모로","프티","튀람","파바르","지라르","로랑"]
FRA_FIRST = ["뤼카","레오","마티외","테오","앙투안","줄리앙","가브리엘","라파엘","위고","니콜라"]

GER_LAST = ["슈미트","뮐러","슈나이더","피셔","베버","마이어","바그너","호프만","슐츠","코흐"]
GER_FIRST = ["요나스","루카스","레온","펠릭스","막스","니코","팀","파울","모리츠","에밀"]

SPA_LAST = ["가르시아","로페스","마르티네스","산체스","페레스","고메스","디아스","로메로","토레스","루이스"]
SPA_FIRST = ["카를로스","후안","미겔","세르히오","다비드","알바로","하비에르","루카스","파블로","마리오"]

ITA_LAST = ["로시","루소","페라리","에스포지토","비안키","로마노","콜롬보","리치","그레코","콘티"]
ITA_FIRST = ["마르코","루카","마테오","안드레아","페데리코","로렌초","다비데","니콜로","알레산드로","파올로"]

POR_LAST = ["실바","산투스","코스타","소자","페레이라","올리베이라","카르발류","페르난데스","리마","고메스"]
POR_FIRST = ["주앙","미겔","티아구","디오구","브루누","루이스","안드레","파울루","페드루","하파엘"]

NED_LAST = ["더용","얀선","둠프리스","반다이크","바커","피서","스미트","마이어르","보스","뮐더"]
NED_FIRST = ["단","셈","밀란","루카스","노아","핀","예세","리암","툰","타이스"]

BEL_LAST = ["페터스","얀센스","마스","야콥스","메르턴스","빌럼스","클라스","호선스","바우터스","람브레흐츠"]
BEL_FIRST = ["아르튀르","루이","루카스","노아","쥘","빅토르","밀란","토마스","엘리아스","막심"]

CRO_LAST = ["모드리치","코바치치","페리시치","브로조비치","라키티치","비다","칼리니치","오르시치","유라노비치","파살리치"]
CRO_FIRST = ["이반","루카","마테오","마르코","안테","요시프","마리오","필리프","니콜라","도마고이"]

SRB_LAST = ["요비치","미트로비치","타디치","코스티치","밀린코비치","파블로비치","블라호비치","사비치","이바노비치","스토야노비치"]
SRB_FIRST = ["니콜라","마르코","밀로시","스테판","알렉산다르","루카","두샨","이반","필리프","네마냐"]

SWE_LAST = ["안데르손","요한손","칼손","닐손","에릭손","라르손","올손","페르손","스벤손","구스타프손"]
SWE_FIRST = ["에릭","오스카","윌리엄","엘리아스","후고","빅토르","안톤","루카스","노아","알빈"]

DEN_LAST = ["닐센","옌센","한센","페데르센","안데르센","크리스텐센","라르센","쇠렌센","라스무센","요르겐센"]
DEN_FIRST = ["마티아스","마즈","루카스","올리버","노아","에밀","프레데리크","빅토르","엘리아스","윌리엄"]

NOR_LAST = ["한센","요한센","올센","라르센","안데르센","페데르센","닐센","크리스티안센","베르그","하우겐"]
NOR_FIRST = ["에릭","마그누스","산데르","노아","오스카","에밀","요나스","루카스","엘리아스","마르틴"]

POL_LAST = ["노바크","코발스키","비시니에프스키","보이치크","카치마레크","마주르","레반도프스키","지엘린스키","시만스키","다브로프스키"]
POL_FIRST = ["야쿠프","카츠페르","마테우시","파베우","미하우","얀","표트르","토마시","아담","필리프"]

TUR_LAST = ["일마즈","카야","데미르","첼리크","일디즈","샤힌","아이든","아르슬란","코르크마즈","오즈데미르"]
TUR_FIRST = ["메흐메트","아흐메트","무스타파","엠레","유수프","부라크","케렘","오잔","하칸","잔"]

GRE_LAST = ["파파도풀로스","게오르기우","니콜라우","이오아누","파파스","코스타스","디미트리우","키르기아코스","사마라스","카라구니스"]
GRE_FIRST = ["요르고스","니코스","디미트리스","코스타스","야니스","알렉시스","스타브로스","안드레아스","파나기오티스","미할리스"]

BRA_LAST = ["실바","산투스","올리베이라","소자","코스타","페레이라","리마","고메스","히베이루","카르발류"]
BRA_FIRST = ["가브리엘","루카스","마테우스","비니시우스","하파엘","브루누","페드루","다니엘","카이우","엔히키"]

ARG_LAST = ["곤살레스","로페스","로메로","디아스","페레스","알바레스","토레스","수아레스","베니테스","카브레라"]
ARG_FIRST = ["마르틴","니콜라스","후안","루카스","프랑코","레안드로","디에고","마테오","에밀리아노","로렌소"]

MEX_LAST = ["에르난데스","가르시아","마르티네스","로페스","곤살레스","페레스","산체스","라미레스","토레스","플로레스"]
MEX_FIRST = ["후안","카를로스","루이스","하비에르","미겔","디에고","호세","알레한드로","페르난도","마르코"]

USA_LAST = ["존슨","스미스","브라운","존스","밀러","데이비스","윌슨","무어","테일러","앤더슨"]
USA_FIRST = ["이선","노아","리암","메이슨","로건","제이컵","루카스","에이든","벤자민","제임스"]

NGA_LAST = ["오코예","아데바요","이헤아나초","은디디","오시멘","오메루오","오코차","오비","우조호","이워비"]
NGA_FIRST = ["치네두","에메카","이페아니","빅터","새뮤얼","존","피터","데이비드","켈레치","추쿠"]

SEN_LAST = ["은디아예","디오프","사르","바","게예","은돔","파예","시소코","카네","디아"]
SEN_FIRST = ["아마두","이브라히마","우스만","무사","바바카르","셰이크","마마두","이스마일라","사디오","알리우"]

MAR_LAST = ["엘암라니","벤나니","엘이드리시","아이트알리","암라바트","하키미","지예시","베나티아","사이스","엘카비"]
MAR_FIRST = ["유세프","아슈라프","하킴","소피안","아민","카림","오마르","함자","빌랄","아나스"]

NAME_POOLS = {
    "대한민국": (KOR_LAST, KOR_FIRST),
    "일본": (JPN_LAST, JPN_FIRST),
    "중국": (CHN_LAST, CHN_FIRST),
    "잉글랜드": (ENG_LAST, ENG_FIRST),
    "프랑스": (FRA_LAST, FRA_FIRST),
    "독일": (GER_LAST, GER_FIRST),
    "스페인": (SPA_LAST, SPA_FIRST),
    "이탈리아": (ITA_LAST, ITA_FIRST),
    "포르투갈": (POR_LAST, POR_FIRST),
    "네덜란드": (NED_LAST, NED_FIRST),
    "벨기에": (BEL_LAST, BEL_FIRST),
    "크로아티아": (CRO_LAST, CRO_FIRST),
    "세르비아": (SRB_LAST, SRB_FIRST),
    "스웨덴": (SWE_LAST, SWE_FIRST),
    "덴마크": (DEN_LAST, DEN_FIRST),
    "노르웨이": (NOR_LAST, NOR_FIRST),
    "폴란드": (POL_LAST, POL_FIRST),
    "터키": (TUR_LAST, TUR_FIRST),
    "그리스": (GRE_LAST, GRE_FIRST),
    "브라질": (BRA_LAST, BRA_FIRST),
    "아르헨티나": (ARG_LAST, ARG_FIRST),
    "멕시코": (MEX_LAST, MEX_FIRST),
    "미국": (USA_LAST, USA_FIRST),
    "나이지리아": (NGA_LAST, NGA_FIRST),
    "세네갈": (SEN_LAST, SEN_FIRST),
    "모로코": (MAR_LAST, MAR_FIRST),
}

def random_name_by_nation(nation: str) -> str:
    last_pool, first_pool = NAME_POOLS.get(nation, (ENG_LAST, ENG_FIRST))
    if nation == "대한민국":
        return f"{random.choice(last_pool)}{random.choice(first_pool)}"
    return f"{random.choice(first_pool)} {random.choice(last_pool)}"

def pot_grade_for_value(pot: int) -> str:
    if pot >= 90: return "S"
    if pot >= 84: return "A"
    if pot >= 78: return "B"
    if pot >= 72: return "C"
    return "D"

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

@dataclass
class MarketStatus:
    is_open: bool
    next_change_ts: int
    reason: str

class PlayerMarketDB:
    def __init__(self):
        self._lock = asyncio.Lock()
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        con = sqlite3.connect(DB_PATH)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        return con

    def _init_db(self):
        con = self._connect()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS pm_game_time (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    start_ts INTEGER NOT NULL,
                    minute_per_month INTEGER NOT NULL,
                    month_index INTEGER NOT NULL DEFAULT 0,
                    last_month_ts INTEGER NOT NULL DEFAULT 0,
                    last_tick_ts INTEGER NOT NULL DEFAULT 0
                )
                """
            )

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS pm_players (
                    player_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    nation TEXT NOT NULL,
                    position TEXT NOT NULL,
                    age INTEGER NOT NULL,
                    ovr INTEGER NOT NULL,
                    pot INTEGER NOT NULL,
                    pot_grade TEXT NOT NULL,
                    base_value INTEGER NOT NULL,
                    retired INTEGER NOT NULL DEFAULT 0,
                    created_month INTEGER NOT NULL DEFAULT 0,
                    updated_ts INTEGER NOT NULL DEFAULT 0
                )
                """
            )

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS pm_market (
                    player_id TEXT PRIMARY KEY,
                    price INTEGER NOT NULL,
                    floor_price INTEGER NOT NULL,
                    ceil_price INTEGER NOT NULL,
                    prev_dir INTEGER NOT NULL DEFAULT 0,
                    last_update_ts INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(player_id) REFERENCES pm_players(player_id) ON DELETE CASCADE
                )
                """
            )

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS pm_price_history (
                    player_id TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    tick_ts INTEGER NOT NULL,
                    PRIMARY KEY(player_id, tick_ts),
                    FOREIGN KEY(player_id) REFERENCES pm_players(player_id) ON DELETE CASCADE
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_pm_price_hist_pid ON pm_price_history(player_id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_pm_price_hist_ts ON pm_price_history(tick_ts)")

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS pm_holdings (
                    user_id INTEGER NOT NULL,
                    player_id TEXT NOT NULL,
                    qty INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(user_id, player_id),
                    FOREIGN KEY(player_id) REFERENCES pm_players(player_id) ON DELETE CASCADE
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_pm_holdings_user ON pm_holdings(user_id)")

            con.commit()
        finally:
            con.close()

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    # ───────────────── 시간/상태 ─────────────────
    def _kst_hour(self, now_ts: int) -> int:
        return int((now_ts + KST_OFFSET) % 86400 // 3600)

    def _is_market_open(self, now_ts: int) -> bool:
        h = self._kst_hour(now_ts)
        return MARKET_OPEN_HOUR <= h < MARKET_CLOSE_HOUR

    def _next_tick_boundary(self, now_ts: int) -> int:
        return (now_ts // TICK_SECONDS + 1) * TICK_SECONDS

    def _next_market_change_ts(self, now_ts: int) -> int:
        kst = now_ts + KST_OFFSET
        day0 = (kst // 86400) * 86400
        open_kst = day0 + MARKET_OPEN_HOUR * 3600
        close_kst = day0 + MARKET_CLOSE_HOUR * 3600
        if kst < open_kst:
            return open_kst - KST_OFFSET
        if kst < close_kst:
            return close_kst - KST_OFFSET
        return (day0 + 86400 + MARKET_OPEN_HOUR * 3600) - KST_OFFSET

    async def ensure_bootstrap(self, now_ts: int) -> None:
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute("SELECT start_ts FROM pm_game_time WHERE id=1").fetchone()
                    if row is None:
                        con.execute(
                            "INSERT INTO pm_game_time(id, start_ts, minute_per_month, month_index, last_month_ts, last_tick_ts) VALUES(1, ?, ?, 0, ?, 0)",
                            (int(now_ts), int(REAL_SECONDS_PER_MONTH // 60), int(now_ts)),
                        )
                        con.commit()
                finally:
                    con.close()
            await self._run(work)

        await self.ensure_active_pool(now_ts, target=300)

    async def market_status(self, now_ts: int) -> MarketStatus:
        open_ = self._is_market_open(now_ts)
        if open_:
            return MarketStatus(True, self._next_tick_boundary(now_ts), "시장 오픈(거래 가능)")
        return MarketStatus(False, self._next_market_change_ts(now_ts), "시장 클로즈(거래 불가)")

    # ───────────────── 가치 계산/스폰 ─────────────────
    def _compute_base_value(self, age: int, ovr: int, pot: int) -> int:
        # OVR^2 기반 + 잠재 갭 프리미엄 + 노화 페널티
        core = (ovr * ovr) * 40
        gap = max(0, pot - ovr)
        youth_bonus = gap * (2500 if age <= 22 else 1200)
        age_penalty = max(0, age - 29) * 3500
        v = int(core + youth_bonus - age_penalty)
        return max(10_000, v)

    def _compute_floor_ceil(self, base_value: int) -> Tuple[int, int]:
        floor_p = int(base_value * 0.30)
        ceil_p = int(base_value * 3.00)
        return max(1_000, floor_p), max(floor_p + 1_000, ceil_p)

    def _new_player_id(self, now_ts: int) -> str:
        return f"P{now_ts}{random.randint(1000, 9999)}"

    def _spawn_player(self, month_index: int, now_ts: int, force_grade: Optional[str] = None) -> Dict:
        age = random.randint(17, 22)
        ovr = random.randint(55, 72)

        if force_grade == "S":
            pot = random.randint(90, 95)
        elif force_grade == "A":
            pot = random.randint(84, 89)
        elif force_grade == "B":
            pot = random.randint(78, 83)
        elif force_grade == "C":
            pot = random.randint(72, 77)
        elif force_grade == "D":
            pot = random.randint(66, 71)
        else:
            r = random.random()
            if r < 0.08:
                pot = random.randint(90, 95)
            elif r < 0.22:
                pot = random.randint(84, 89)
            elif r < 0.55:
                pot = random.randint(78, 83)
            elif r < 0.85:
                pot = random.randint(72, 77)
            else:
                pot = random.randint(66, 71)

        pot = max(pot, ovr)

        nation = pick_weighted_nation()
        name = random_name_by_nation(nation)
        pos = random.choice(POSITIONS)

        base_value = self._compute_base_value(age, ovr, pot)
        floor_p, ceil_p = self._compute_floor_ceil(base_value)

        pid = self._new_player_id(now_ts)
        return {
            "player_id": pid,
            "name": name,
            "nation": nation,
            "position": pos,
            "age": age,
            "ovr": ovr,
            "pot": pot,
            "pot_grade": pot_grade_for_value(pot),
            "base_value": base_value,
            "floor_price": floor_p,
            "ceil_price": ceil_p,
            "price": base_value,
            "created_month": month_index,
            "updated_ts": now_ts,
        }

    async def ensure_active_pool(self, now_ts: int, target: int = 300) -> None:
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute("SELECT month_index FROM pm_game_time WHERE id=1").fetchone()
                    month_index = int(row[0]) if row else 0

                    active = con.execute("SELECT COUNT(*) FROM pm_players WHERE retired=0").fetchone()[0]
                    need = max(0, int(target) - int(active))
                    if need <= 0:
                        return

                    # S 최소 1명 보장(생성 전 기준)
                    s_count = con.execute("SELECT COUNT(*) FROM pm_players WHERE retired=0 AND pot_grade='S'").fetchone()[0]
                    force_s = (int(s_count) == 0)

                    for i in range(need):
                        p = self._spawn_player(month_index, now_ts, force_grade=("S" if (force_s and i == 0) else None))
                        con.execute(
                            """
                            INSERT INTO pm_players(player_id, name, nation, position, age, ovr, pot, pot_grade, base_value, retired, created_month, updated_ts)
                            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                            """,
                            (
                                p["player_id"], p["name"], p["nation"], p["position"],
                                p["age"], p["ovr"], p["pot"], p["pot_grade"], p["base_value"],
                                p["created_month"], p["updated_ts"],
                            ),
                        )
                        con.execute(
                            """
                            INSERT INTO pm_market(player_id, price, floor_price, ceil_price, prev_dir, last_update_ts)
                            VALUES(?, ?, ?, ?, 0, ?)
                            """,
                            (p["player_id"], p["price"], p["floor_price"], p["ceil_price"], int(now_ts)),
                        )
                        con.execute(
                            "INSERT OR IGNORE INTO pm_price_history(player_id, price, tick_ts) VALUES(?, ?, ?)",
                            (p["player_id"], p["price"], int(now_ts)),
                        )

                    con.commit()
                finally:
                    con.close()

            await self._run(work)

    # ───────────────── 조회 ─────────────────
    async def search_players(self, q: str, limit: int = 10):
        q = (q or "").strip()
        limit = max(1, min(25, int(limit)))
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    if not q:
                        return con.execute(
                            """
                            SELECT p.player_id, p.name, p.nation, p.position, p.age, p.ovr, p.pot_grade,
                                   m.price, p.retired
                            FROM pm_players p
                            JOIN pm_market m ON m.player_id=p.player_id
                            WHERE p.retired=0
                            ORDER BY m.price DESC
                            LIMIT ?
                            """,
                            (limit,),
                        ).fetchall()

                    like = f"%{q}%"
                    return con.execute(
                        """
                        SELECT p.player_id, p.name, p.nation, p.position, p.age, p.ovr, p.pot_grade,
                               m.price, p.retired
                        FROM pm_players p
                        JOIN pm_market m ON m.player_id=p.player_id
                        WHERE p.name LIKE ? OR p.nation LIKE ? OR p.position LIKE ? OR p.player_id LIKE ?
                        ORDER BY p.retired ASC, m.price DESC
                        LIMIT ?
                        """,
                        (like, like, like, like, limit),
                    ).fetchall()
                finally:
                    con.close()
            return await self._run(work)

    async def get_player(self, player_id: str):
        pid = (player_id or "").strip()
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    return con.execute(
                        """
                        SELECT p.player_id, p.name, p.nation, p.position, p.age, p.ovr, p.pot_grade, p.base_value, p.retired,
                               m.price, m.floor_price, m.ceil_price, m.last_update_ts
                        FROM pm_players p
                        JOIN pm_market m ON m.player_id=p.player_id
                        WHERE p.player_id=?
                        """,
                        (pid,),
                    ).fetchone()
                finally:
                    con.close()
            return await self._run(work)

    async def get_holding(self, user_id: int, player_id: str) -> int:
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute(
                        "SELECT qty FROM pm_holdings WHERE user_id=? AND player_id=?",
                        (int(user_id), (player_id or "").strip()),
                    ).fetchone()
                    return int(row[0]) if row else 0
                finally:
                    con.close()
            return await self._run(work)

    async def list_holdings(self, user_id: int, limit: int = 25):
        limit = max(1, min(50, int(limit)))
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    return con.execute(
                        """
                        SELECT h.player_id, p.name, p.nation, p.position, p.age, p.ovr, p.pot_grade, p.retired,
                               h.qty, m.price
                        FROM pm_holdings h
                        JOIN pm_players p ON p.player_id=h.player_id
                        JOIN pm_market m ON m.player_id=h.player_id
                        WHERE h.user_id=? AND h.qty>0
                        ORDER BY (h.qty*m.price) DESC
                        LIMIT ?
                        """,
                        (int(user_id), int(limit)),
                    ).fetchall()
                finally:
                    con.close()
            return await self._run(work)

    async def price_history(self, player_id: str, since_ts: int, limit: int = 400):
        limit = max(10, min(1000, int(limit)))
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    return con.execute(
                        """
                        SELECT tick_ts, price
                        FROM pm_price_history
                        WHERE player_id=? AND tick_ts >= ?
                        ORDER BY tick_ts ASC
                        LIMIT ?
                        """,
                        ((player_id or "").strip(), int(since_ts), int(limit)),
                    ).fetchall()
                finally:
                    con.close()
            return await self._run(work)

    # ───────────────── 거래/팩 ─────────────────
    async def buy_from_market(self, *, user_id: int, player_id: str, qty: int, now_ts: int, get_balance, add_balance):
        pid = (player_id or "").strip()
        qty = int(qty)
        if qty <= 0:
            return False, "수량은 1 이상이어야 합니다."

        if not self._is_market_open(now_ts):
            st = await self.market_status(now_ts)
            return False, f"지금은 시장이 닫혀 있습니다. (다음 변경: <t:{st.next_change_ts}:f>)"

        row = await self.get_player(pid)
        if not row:
            return False, "선수를 찾을 수 없습니다."

        (_pid, name, nation, pos, age, ovr, potg, basev, retired, price, *_rest) = row
        if int(retired) == 1:
            return False, "은퇴 선수는 구매할 수 없습니다."

        cost = int(price) * qty
        bal = await get_balance(user_id)
        if bal < cost:
            return False, f"잔액이 부족합니다. 필요: {cost:,} / 보유: {bal:,}"

        await add_balance(user_id, -cost)

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute(
                        """
                        INSERT INTO pm_holdings(user_id, player_id, qty)
                        VALUES(?, ?, ?)
                        ON CONFLICT(user_id, player_id) DO UPDATE SET qty=qty+excluded.qty
                        """,
                        (int(user_id), pid, int(qty)),
                    )
                    con.commit()
                finally:
                    con.close()
            await self._run(work)

        return True, f"✅ 구매 완료: `{pid}` x{qty} / 총 {cost:,}원"

    async def sell_to_market(self, *, user_id: int, player_id: str, qty: int, now_ts: int, add_balance):
        pid = (player_id or "").strip()
        qty = int(qty)
        if qty <= 0:
            return False, "수량은 1 이상이어야 합니다."

        if not self._is_market_open(now_ts):
            st = await self.market_status(now_ts)
            return False, f"지금은 시장이 닫혀 있습니다. (다음 변경: <t:{st.next_change_ts}:f>)"

        row = await self.get_player(pid)
        if not row:
            return False, "선수를 찾을 수 없습니다."

        (_pid, name, nation, pos, age, ovr, potg, basev, retired, price, *_rest) = row
        if int(retired) == 1:
            return False, "은퇴 선수는 시장에 판매할 수 없습니다."

        have = await self.get_holding(user_id, pid)
        if have < qty:
            return False, f"보유 수량이 부족합니다. 보유: {have} / 판매 요청: {qty}"

        gross = int(price) * qty
        fee = int(gross * SELL_FEE_RATE)
        net = gross - fee

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("BEGIN IMMEDIATE;")
                    con.execute(
                        "UPDATE pm_holdings SET qty=qty-? WHERE user_id=? AND player_id=?",
                        (int(qty), int(user_id), pid),
                    )
                    con.execute("COMMIT;")
                except Exception:
                    try: con.execute("ROLLBACK;")
                    except Exception: pass
                    raise
                finally:
                    con.close()
            await self._run(work)

        await add_balance(user_id, net)
        return True, f"✅ 판매 완료: `{pid}` x{qty} / 총 {gross:,}원 (수수료 {fee:,}) → 실수령 {net:,}원"

    async def buy_pack(self, *, user_id: int, pack_type: str, pulls: int, now_ts: int, get_balance, add_balance):
        pack_type = (pack_type or "").strip()
        pulls = max(1, min(PACK_MAX_PULLS, int(pulls)))

        if pack_type not in PACKS:
            return False, "존재하지 않는 팩입니다. (브론즈/실버/골드/플래티넘/아이콘)", None

        pack = PACKS[pack_type]
        price = int(pack["price"])
        total_cost = price * pulls

        bal = await get_balance(user_id)
        if bal < total_cost:
            return False, f"잔액이 부족합니다. 필요: {total_cost:,} / 보유: {bal:,}", None

        # 선차감(실패 거의 없음. active 풀 비면 환불)
        await add_balance(user_id, -total_cost)

        order = ["S", "A", "B", "C", "D"]

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    rows = con.execute(
                        "SELECT player_id, pot_grade FROM pm_players WHERE retired=0"
                    ).fetchall()

                    grade_map: Dict[str, List[str]] = {"S": [], "A": [], "B": [], "C": [], "D": []}
                    for pid, g in rows:
                        g = str(g)
                        if g in grade_map:
                            grade_map[g].append(str(pid))

                    if sum(len(v) for v in grade_map.values()) == 0:
                        return ("EMPTY", [])

                    def pick_grade() -> str:
                        r = random.random()
                        acc = 0.0
                        for g, w in pack["weights"]:
                            acc += float(w)
                            if r <= acc:
                                return str(g)
                        return "D"

                    results: List[Tuple[str, str]] = []
                    for _ in range(pulls):
                        want = pick_grade()
                        start = order.index(want) if want in order else 4
                        chosen = None
                        for gg in order[start:]:
                            if grade_map[gg]:
                                chosen = (random.choice(grade_map[gg]), gg)
                                break
                        if chosen is None:
                            any_player_id = random.choice([pid for lst in grade_map.values() for pid in lst])
                            actual_grade = "D"
                            for grade, pids in grade_map.items():
                                if any_player_id in pids:
                                    actual_grade = grade
                                    break
                            chosen = (any_player_id, actual_grade)
                        results.append(chosen)

                    for pid, _g in results:
                        con.execute(
                            """
                            INSERT INTO pm_holdings(user_id, player_id, qty)
                            VALUES(?, ?, 1)
                            ON CONFLICT(user_id, player_id) DO UPDATE SET qty=qty+1
                            """,
                            (int(user_id), str(pid)),
                        )

                    con.commit()
                    return ("OK", results)
                finally:
                    con.close()

            status, results = await self._run(work)

        if status == "EMPTY":
            await add_balance(user_id, total_cost)
            return False, "선수 풀이 비어 있습니다. (잠시 후 다시 시도)", None

        return True, f"🎁 {pack_type}팩 {pulls}장 개봉 완료! (총 {total_cost:,}원)", results

    # ───────────────── 시장 틱 / 월 처리 ─────────────────
    async def run_tick_if_due(self, now_ts: int) -> bool:
        if not self._is_market_open(now_ts):
            return False
        if (now_ts % TICK_SECONDS) > 4:
            return False

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute("SELECT last_tick_ts FROM pm_game_time WHERE id=1").fetchone()
                    last_tick = int(row[0]) if row else 0
                    if last_tick and (now_ts - last_tick) < (TICK_SECONDS - 5):
                        return False

                    rows = con.execute(
                        """
                        SELECT p.player_id, p.base_value, p.retired,
                               m.price, m.floor_price, m.ceil_price, m.prev_dir
                        FROM pm_players p
                        JOIN pm_market m ON m.player_id=p.player_id
                        WHERE p.retired=0
                        """
                    ).fetchall()

                    for pid, base_value, retired, price, floor_p, ceil_p, prev_dir in rows:
                        base_value = int(base_value)
                        price = int(price)
                        if base_value <= 0 or price <= 0:
                            continue

                        # 평균회귀 + 관성으로 방향 확률
                        d = (float(price) - float(base_value)) / float(base_value)
                        p_up = 0.50
                        p_up += MOMENTUM * (1 if int(prev_dir) > 0 else (-1 if int(prev_dir) < 0 else 0))
                        p_up += (-MEAN_REVERT * d)
                        p_up = clamp(p_up, P_UP_MIN, P_UP_MAX)

                        up = (random.random() < p_up)
                        sign = 1.0 if up else -1.0

                        mag = abs(random.gauss(0, SIGMA))
                        mag = clamp(mag, 0.002, TICK_CAP)
                        delta = sign * mag
                        delta = clamp(delta, -TICK_CAP, TICK_CAP)

                        new_price = int(price * (1.0 + float(delta)))
                        new_price = max(int(floor_p), min(int(ceil_p), new_price))

                        new_dir = 1 if new_price > price else (-1 if new_price < price else 0)

                        con.execute(
                            "UPDATE pm_market SET price=?, prev_dir=?, last_update_ts=? WHERE player_id=?",
                            (int(new_price), int(new_dir), int(now_ts), str(pid)),
                        )
                        con.execute(
                            "INSERT OR IGNORE INTO pm_price_history(player_id, price, tick_ts) VALUES(?, ?, ?)",
                            (str(pid), int(new_price), int(now_ts)),
                        )

                    con.execute("UPDATE pm_game_time SET last_tick_ts=? WHERE id=1", (int(now_ts),))
                    con.commit()
                    return True
                finally:
                    con.close()

            return await self._run(work)

    async def run_month_if_due(self, now_ts: int) -> int:
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute("SELECT start_ts, month_index, last_month_ts FROM pm_game_time WHERE id=1").fetchone()
                    if not row:
                        return 0
                    _start_ts, month_index, last_month_ts = int(row[0]), int(row[1]), int(row[2])

                    if (now_ts - last_month_ts) < REAL_SECONDS_PER_MONTH:
                        return 0

                    months_due = int((now_ts - last_month_ts) // REAL_SECONDS_PER_MONTH)
                    if months_due <= 0:
                        return 0

                    for _ in range(months_due):
                        month_index += 1

                        players = con.execute(
                            "SELECT player_id, age, ovr, pot FROM pm_players WHERE retired=0"
                        ).fetchall()

                        for pid, age, ovr, pot in players:
                            age = int(age); ovr = int(ovr); pot = int(pot)

                            # 악재
                            if random.random() < ADVERSE_PROB:
                                r = random.random()
                                if r < 0.80:
                                    pot = max(ovr, pot - 1)
                                elif r < 0.98:
                                    pot = max(ovr, pot - 2)
                                else:
                                    ovr = max(1, ovr - 1)
                                    pot = max(ovr, pot - 1)

                            # 성장
                            base_p = 0.0
                            for max_age, pr in GROWTH_PROB_BY_AGE:
                                if age <= max_age:
                                    base_p = float(pr)
                                    break

                            gap = max(0, pot - ovr)
                            p_growth = base_p * growth_penalty(int(gap))
                            if p_growth > 0 and random.random() < p_growth:
                                inc = 2 if random.random() < 0.10 else 1
                                ovr = min(pot, ovr + inc)

                            # 은퇴
                            if random.random() < retire_prob(age):
                                con.execute("UPDATE pm_players SET retired=1, updated_ts=? WHERE player_id=?", (int(now_ts), str(pid)))
                                con.execute(
                                    "UPDATE pm_market SET price=0, floor_price=0, ceil_price=0, last_update_ts=? WHERE player_id=?",
                                    (int(now_ts), str(pid)),
                                )
                                con.execute(
                                    "INSERT OR IGNORE INTO pm_price_history(player_id, price, tick_ts) VALUES(?, 0, ?)",
                                    (str(pid), int(now_ts)),
                                )
                                continue

                            # 연 단위 처리
                            if (month_index % MONTHS_PER_YEAR) == 0:
                                age += 1
                                if age >= 30:
                                    pot = max(ovr, pot - random.randint(1, 3))
                                if age >= 33:
                                    ovr = max(1, ovr - random.randint(0, 2))
                                    pot = max(ovr, pot)

                            new_base = self._compute_base_value(age, ovr, pot)
                            floor_p, ceil_p = self._compute_floor_ceil(new_base)

                            con.execute(
                                """
                                UPDATE pm_players
                                SET age=?, ovr=?, pot=?, pot_grade=?, base_value=?, updated_ts=?
                                WHERE player_id=? AND retired=0
                                """,
                                (age, ovr, pot, pot_grade_for_value(pot), int(new_base), int(now_ts), str(pid)),
                            )
                            con.execute(
                                "UPDATE pm_market SET floor_price=?, ceil_price=? WHERE player_id=?",
                                (int(floor_p), int(ceil_p), str(pid)),
                            )

                        # 풀 유지(300명)
                        active = con.execute("SELECT COUNT(*) FROM pm_players WHERE retired=0").fetchone()[0]
                        need = max(0, 300 - int(active))

                        if need > 0:
                            # S 최소 1명 보장(생성 전 기준)
                            s_count = con.execute("SELECT COUNT(*) FROM pm_players WHERE retired=0 AND pot_grade='S'").fetchone()[0]
                            force_s = (int(s_count) == 0)

                            for i in range(need):
                                p = self._spawn_player(month_index, now_ts, force_grade=("S" if (force_s and i == 0) else None))
                                con.execute(
                                    """
                                    INSERT INTO pm_players(player_id, name, nation, position, age, ovr, pot, pot_grade, base_value, retired, created_month, updated_ts)
                                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                                    """,
                                    (
                                        p["player_id"], p["name"], p["nation"], p["position"],
                                        p["age"], p["ovr"], p["pot"], p["pot_grade"], p["base_value"],
                                        p["created_month"], p["updated_ts"],
                                    ),
                                )
                                con.execute(
                                    """
                                    INSERT INTO pm_market(player_id, price, floor_price, ceil_price, prev_dir, last_update_ts)
                                    VALUES(?, ?, ?, ?, 0, ?)
                                    """,
                                    (p["player_id"], p["price"], p["floor_price"], p["ceil_price"], int(now_ts)),
                                )
                                con.execute(
                                    "INSERT OR IGNORE INTO pm_price_history(player_id, price, tick_ts) VALUES(?, ?, ?)",
                                    (p["player_id"], p["price"], int(now_ts)),
                                )

                        last_month_ts += REAL_SECONDS_PER_MONTH

                    con.execute(
                        "UPDATE pm_game_time SET month_index=?, last_month_ts=? WHERE id=1",
                        (int(month_index), int(last_month_ts)),
                    )
                    con.commit()
                    return int(months_due)
                finally:
                    con.close()

            return await self._run(work)
