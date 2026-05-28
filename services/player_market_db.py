# services/player_market_db.py
from __future__ import annotations

import asyncio
import math
import random
import sqlite3
import time
from collections import Counter
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

# 이적시장 파라미터
LISTING_DURATION    = 72 * 3600   # 72시간 후 만료
TRADE_EXPIRE        = 86400       # 트레이드 제안 24시간 후 만료
INSTANT_SELL_DELAY  = 12 * 3600   # 12시간 후 즉시판매 가능
INSTANT_SELL_RATE     = 0.70      # 즉시판매 수령 비율 — 시장 오픈 중 (기준가의 70%)
INSTANT_SELL_RATE_OFF = 0.50      # 즉시판매 수령 비율 — 시장 외 시간 급전 (기준가의 50%)
TRANSFER_FEE_RATE   = 0.05        # 이적시장 거래 수수료 5%

# 시장 변동폭
TICK_CAP = 0.05  # ±5%
SIGMA = 0.020    # 체감: 보통 ±1~3%, 가끔 큰 틱

# 방향(추세/되돌림)
MOMENTUM = 0.08
MEAN_REVERT = 0.25
P_UP_MIN, P_UP_MAX = 0.20, 0.80

# 악재 확률(월)
ADVERSE_PROB = 0.01

# 성장 확률(월 1회)
GROWTH_PROB_BY_AGE = [
    (18, 0.40),  # 17~18 (전성기 전 폭발 성장)
    (20, 0.35),  # 19~20
    (22, 0.28),  # 21~22
    (24, 0.20),  # 23~24
    (26, 0.12),  # 25~26
    (28, 0.05),  # 27~28
    (999, 0.00), # 29+ (성장 없음)
]

def growth_penalty(gap: int) -> float:
    if gap >= 10:
        return 1.0
    if 5 <= gap <= 9:
        return 0.7
    if 1 <= gap <= 4:
        return 0.4
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

# ───────────── 아마추어 스타터 스쿼드 (구단 생성 시 자동 지급) ─────────────
# GK 2 · DF 5 · MF 6 · FW 5 = 18명
# base_value=0, pot_grade='아마추어' — 이적시장 가치 없는 더미 카드
AMATEUR_SQUAD: list[dict] = [
    # GK
    {"player_id": "AMT_GK_01", "name": "막아라", "position": "GK"},
    {"player_id": "AMT_GK_02", "name": "손장군", "position": "GK"},
    # DF
    {"player_id": "AMT_DF_01", "name": "안들어", "position": "DF"},
    {"player_id": "AMT_DF_02", "name": "철벽준", "position": "DF"},
    {"player_id": "AMT_DF_03", "name": "담장원", "position": "DF"},
    {"player_id": "AMT_DF_04", "name": "버팀민", "position": "DF"},
    {"player_id": "AMT_DF_05", "name": "막을수", "position": "DF"},
    # MF
    {"player_id": "AMT_MF_01", "name": "연결하", "position": "MF"},
    {"player_id": "AMT_MF_02", "name": "패스왕", "position": "MF"},
    {"player_id": "AMT_MF_03", "name": "달려봐", "position": "MF"},
    {"player_id": "AMT_MF_04", "name": "중원진", "position": "MF"},
    {"player_id": "AMT_MF_05", "name": "열심이", "position": "MF"},
    {"player_id": "AMT_MF_06", "name": "따라가", "position": "MF"},
    # FW
    {"player_id": "AMT_FW_01", "name": "슛돌이", "position": "FW"},
    {"player_id": "AMT_FW_02", "name": "골넣어", "position": "FW"},
    {"player_id": "AMT_FW_03", "name": "빗나강", "position": "FW"},
    {"player_id": "AMT_FW_04", "name": "기적이", "position": "FW"},
    {"player_id": "AMT_FW_05", "name": "차봐요", "position": "FW"},
]

# ───────────── 팩 5종(가격/확률) ─────────────
PACKS = {
    # price     : 팩 구입 비용 (= 가우시안 분포 중심가)
    # min_price : 팩 풀 하한 — price × 0.50
    # max_price : 팩 풀 상한 — price × 1.50
    # 풀 사이즈가 변해도 절대 가격 기준이라 영향 없음
    "브론즈":    {"price":    50_000, "min_price":    25_000, "max_price":     75_000},
    "실버":      {"price":   150_000, "min_price":    75_000, "max_price":    225_000},
    "골드":      {"price":   500_000, "min_price":   250_000, "max_price":    750_000},
    "플래티넘":  {"price": 2_000_000, "min_price": 1_000_000, "max_price":  3_000_000},
    "다이아몬드": {"price": 5_000_000, "min_price": 2_500_000, "max_price":  7_500_000},
    "아이콘":    {"price": 10_000_000, "min_price": 5_000_000, "max_price": 15_000_000},
    "얼티밋":    {"price": 18_000_000, "min_price": 9_000_000, "max_price": 27_000_000},
}
PACK_MAX_PULLS = 10
POOL_SIZE = 1_000   # 시장에 상시 유지할 활성 선수 수

def _pack_weight(player_price: int, pack_price: int) -> float:
    """팩 가격 기준 대칭 가우시안 가중치.
    - peak  : player_price == pack_price → 1.0
    - 양쪽 모두 sigma = pack_price × 0.20 (경계 ±50%에서 동일하게 ~4%)
    """
    p = max(1.0, float(player_price))
    P = max(1.0, float(pack_price))
    sigma = P * 0.20
    return math.exp(-0.5 * ((p - P) / sigma) ** 2)

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

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS pm_listings (
                    listing_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id       INTEGER NOT NULL,
                    player_id       TEXT    NOT NULL,
                    qty             INTEGER NOT NULL DEFAULT 1,
                    price_per       INTEGER NOT NULL,
                    listed_at       INTEGER NOT NULL,
                    expires_at      INTEGER NOT NULL,
                    instant_sell_at INTEGER NOT NULL,
                    status          TEXT    NOT NULL DEFAULT 'active'
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_pm_listings_seller ON pm_listings(seller_id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_pm_listings_status ON pm_listings(status)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_pm_listings_pid    ON pm_listings(player_id)")

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS pm_trades (
                    trade_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposer_id   INTEGER NOT NULL,
                    receiver_id   INTEGER NOT NULL,
                    proposer_cash INTEGER NOT NULL DEFAULT 0,
                    receiver_cash INTEGER NOT NULL DEFAULT 0,
                    status        TEXT    NOT NULL DEFAULT 'pending',
                    created_at    INTEGER NOT NULL,
                    expires_at    INTEGER NOT NULL
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_pm_trades_proposer ON pm_trades(proposer_id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_pm_trades_receiver ON pm_trades(receiver_id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_pm_trades_status   ON pm_trades(status)")

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS pm_trade_items (
                    item_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id  INTEGER NOT NULL,
                    side      TEXT    NOT NULL,
                    player_id TEXT    NOT NULL,
                    qty       INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY(trade_id) REFERENCES pm_trades(trade_id)
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_pm_trade_items_tid ON pm_trade_items(trade_id)")

            # ── 아마추어 선수 시드 (INSERT OR IGNORE → 멱등)
            for _p in AMATEUR_SQUAD:
                con.execute(
                    """
                    INSERT OR IGNORE INTO pm_players
                        (player_id, name, nation, position, age, ovr, pot,
                         pot_grade, base_value, retired, created_month, updated_ts)
                    VALUES (?, ?, '대한민국', ?, 22, 50, 50, '아마추어', 0, 0, 0, 0)
                    """,
                    (_p["player_id"], _p["name"], _p["position"]),
                )

            # ── 기존 구단주 마이그레이션: clubs 테이블의 모든 유저에게 아마추어 스쿼드 일괄 지급
            _clubs_exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='clubs'"
            ).fetchone()
            if _clubs_exists:
                con.execute("""
                    INSERT OR IGNORE INTO pm_holdings (user_id, player_id, qty)
                    SELECT c.user_id, ap.player_id, 1
                    FROM clubs c
                    CROSS JOIN (
                        SELECT player_id FROM pm_players WHERE pot_grade = '아마추어'
                    ) ap
                """)

            con.commit()
        finally:
            con.close()

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    # ───────────────── 아마추어 스쿼드 지급 ─────────────────

    async def give_amateur_squad(self, user_id: int) -> int:
        """구단 생성 시 아마추어 스타터 스쿼드 18명을 보유 목록에 추가합니다.
        이미 보유 중인 선수는 스킵(INSERT OR IGNORE). 추가된 선수 수 반환."""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    added = 0
                    for _p in AMATEUR_SQUAD:
                        res = con.execute(
                            "INSERT OR IGNORE INTO pm_holdings (user_id, player_id, qty) VALUES (?, ?, 1)",
                            (int(user_id), _p["player_id"]),
                        )
                        added += res.rowcount
                    con.commit()
                    return added
                finally:
                    con.close()
            return await asyncio.to_thread(work)

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

        await self.ensure_active_pool(now_ts, target=POOL_SIZE)

    async def recalculate_price_ranges(self, now_ts: int) -> int:
        """모든 활성 선수의 floor/ceil을 현재 공식으로 재계산합니다.
        가격이 새 범위를 벗어나면 클리핑합니다.
        Returns: 업데이트된 선수 수"""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    rows = con.execute(
                        """
                        SELECT p.player_id, p.base_value, m.price
                        FROM pm_players p
                        JOIN pm_market m ON m.player_id = p.player_id
                        WHERE p.retired = 0
                        """
                    ).fetchall()
                    count = 0
                    for pid, base_value, price in rows:
                        floor_p, ceil_p = self._compute_floor_ceil(int(base_value))
                        clipped = max(floor_p, min(ceil_p, int(price)))
                        con.execute(
                            "UPDATE pm_market SET floor_price=?, ceil_price=?, price=?, last_update_ts=? WHERE player_id=?",
                            (floor_p, ceil_p, clipped, int(now_ts), str(pid)),
                        )
                        count += 1
                    con.commit()
                    return count
                finally:
                    con.close()
            return await self._run(work)

    async def reset_system_pool(self, now_ts: int) -> tuple[int, int]:
        """유저가 보유하지 않은 일반 선수를 전부 삭제하고 새로 스폰합니다.
        AMT_ 아마추어 선수는 항상 보존됩니다.
        Returns: (삭제된 선수 수, 새로 활성화된 선수 수)"""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    # 보존 목록: 유저 보유 중인 선수 + 아마추어 더미
                    held = {row[0] for row in con.execute(
                        "SELECT DISTINCT player_id FROM pm_holdings WHERE qty > 0"
                    ).fetchall()}
                    amt = {p["player_id"] for p in AMATEUR_SQUAD}
                    keep = held | amt

                    # keep 외 전부 삭제 (CASCADE → pm_market, pm_price_history 자동 정리)
                    all_pids = [row[0] for row in con.execute(
                        "SELECT player_id FROM pm_players"
                    ).fetchall()]
                    to_delete = [pid for pid in all_pids if pid not in keep]

                    for pid in to_delete:
                        con.execute("DELETE FROM pm_players WHERE player_id=?", (pid,))
                    con.commit()
                    return len(to_delete)
                finally:
                    con.close()
            deleted = await asyncio.to_thread(work)

        # lock 밖에서 새 선수 스폰 (ensure_active_pool이 lock을 다시 획득함)
        await self.ensure_active_pool(now_ts, target=POOL_SIZE)

        # 새로 생성된 활성 일반 선수 수 집계
        async with self._lock:
            def count_work():
                con = self._connect()
                try:
                    row = con.execute(
                        "SELECT COUNT(*) FROM pm_players WHERE retired=0 AND player_id NOT LIKE 'AMT_%'"
                    ).fetchone()
                    return int(row[0]) if row else 0
                finally:
                    con.close()
            spawned = await asyncio.to_thread(count_work)

        return deleted, spawned

    async def market_status(self, now_ts: int) -> MarketStatus:
        open_ = self._is_market_open(now_ts)
        if open_:
            return MarketStatus(True, self._next_tick_boundary(now_ts), "시장 오픈(거래 가능)")
        return MarketStatus(False, self._next_market_change_ts(now_ts), "시장 클로즈(거래 불가)")

    # ───────────────── 가치 계산/스폰 ─────────────────
    def _compute_base_value(self, age: int, ovr: int, pot: int) -> int:
        # ── OVR 지수 곡선 (OVR 55 기준 22k, 10 오를 때마다 ~2.1배)
        # OVR 65→~200k / 75→~800k / 85→~3M / 90→~7M / 95→~20M
        ovr_f = max(0, ovr - 55)
        core = int(22_000 * (1.20 ** ovr_f))

        # ── 잠재 프리미엄
        # 기존에는 gap * 0.030이었으나, 잠재력이 반영된 초기 가격과
        # 이후 성장 후 가격 사이의 괴리를 줄이기 위해 계수를 2배로 높임.
        # (예: OVR=72 POT=86 선수가 700k→900k 드리프트 발생 → 처음부터 900k 부근으로 시작)
        gap = max(0, pot - ovr)
        if age <= 21:
            pot_mult = 1.0 + gap * 0.060   # 기존 0.030
        elif age <= 26:
            pot_mult = 1.0 + gap * 0.036   # 기존 0.018
        else:
            pot_mult = 1.0 + gap * 0.016   # 기존 0.008

        # ── 노화 페널티 (30세부터 매년 12% 감가)
        if age >= 30:
            age_factor = max(0.20, 1.0 - (age - 29) * 0.12)
        else:
            age_factor = 1.0

        v = int(core * pot_mult * age_factor)
        return max(10_000, v)

    def _compute_floor_ceil(self, base_value: int) -> Tuple[int, int]:
        floor_p = int(base_value * 0.70)
        ceil_p  = int(base_value * 1.55)
        return max(1_000, floor_p), max(floor_p + 1_000, ceil_p)

    def _new_player_id(self, now_ts: int) -> str:
        return f"P{now_ts}{random.randint(10_000_000, 99_999_999)}"

    def _spawn_player(self, month_index: int, now_ts: int, force_grade: Optional[str] = None) -> Dict:
        age = random.randint(17, 22)

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

        # OVR: POT 기준 15~30 아래에서 시작 (등급이 높을수록 더 높은 출발점)
        gap_start = random.randint(15, 30)
        ovr = max(50, pot - gap_start)

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

    async def ensure_active_pool(self, now_ts: int, target: int = POOL_SIZE) -> None:
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

    async def count_pack_pool(self) -> dict:
        """각 팩 등급별 현재 풀 내 선수 수 반환 {pack_name: count}"""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    result = {}
                    for pack_name, pack_data in PACKS.items():
                        min_p = int(pack_data.get("min_price", 0) or 0)
                        max_p = pack_data.get("max_price", None)
                        if max_p is not None:
                            row = con.execute(
                                """
                                SELECT COUNT(*) FROM pm_players p
                                LEFT JOIN pm_market m ON m.player_id = p.player_id
                                WHERE p.retired = 0 AND p.player_id NOT LIKE 'AMT_%'
                                  AND COALESCE(m.price, p.base_value) BETWEEN ? AND ?
                                """,
                                (min_p, int(max_p)),
                            ).fetchone()
                        else:
                            row = con.execute(
                                """
                                SELECT COUNT(*) FROM pm_players p
                                LEFT JOIN pm_market m ON m.player_id = p.player_id
                                WHERE p.retired = 0 AND p.player_id NOT LIKE 'AMT_%'
                                  AND COALESCE(m.price, p.base_value) >= ?
                                """,
                                (min_p,),
                            ).fetchone()
                        result[pack_name] = int(row[0]) if row else 0
                    return result
                finally:
                    con.close()
            return await self._run(work)

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

    async def list_holdings(self, user_id: int, limit: int = 20, offset: int = 0):
        limit = max(1, min(50, int(limit)))
        offset = max(0, int(offset))
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    return con.execute(
                        """
                        SELECT h.player_id, p.name, p.nation, p.position, p.age, p.ovr, p.pot_grade, p.retired,
                               h.qty, COALESCE(m.price, 0)
                        FROM pm_holdings h
                        JOIN pm_players p ON p.player_id=h.player_id
                        LEFT JOIN pm_market m ON m.player_id=h.player_id
                        WHERE h.user_id=? AND h.qty>0
                        ORDER BY (h.qty * COALESCE(m.price, 0)) DESC
                        LIMIT ? OFFSET ?
                        """,
                        (int(user_id), int(limit), int(offset)),
                    ).fetchall()
                finally:
                    con.close()
            return await self._run(work)

    async def count_holdings(self, user_id: int) -> int:
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute(
                        "SELECT COUNT(*) FROM pm_holdings WHERE user_id=? AND qty>0",
                        (int(user_id),),
                    ).fetchone()
                    return int(row[0]) if row else 0
                finally:
                    con.close()
            return await self._run(work)

    async def portfolio_value(self, user_id: int) -> int:
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute(
                        """
                        SELECT COALESCE(SUM(h.qty * COALESCE(m.price, 0)), 0)
                        FROM pm_holdings h
                        LEFT JOIN pm_market m ON m.player_id=h.player_id
                        WHERE h.user_id=? AND h.qty>0
                        """,
                        (int(user_id),),
                    ).fetchone()
                    return int(row[0]) if row else 0
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

        # holdings 먼저 업데이트 → 성공 후 잔액 차감 (돈 먼저 빠지는 버그 방지)
        try:
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
        except Exception:
            return False, "거래 처리 중 오류가 발생했습니다. 잔액은 차감되지 않았습니다."

        await add_balance(user_id, -cost)
        return True, f"✅ 구매 완료: `{pid}` **{name}** x{qty} / 총 {cost:,}원"

    async def sell_to_market(self, *, user_id: int, player_id: str, qty: int, now_ts: int, add_balance):
        pid = (player_id or "").strip()
        qty = int(qty)
        if qty <= 0:
            return False, "수량은 1 이상이어야 합니다."

        row = await self.get_player(pid)
        if not row:
            return False, "선수를 찾을 수 없습니다."

        (_pid, name, nation, pos, age, ovr, potg, basev, retired, price, *_rest) = row

        have = await self.get_holding(user_id, pid)
        if have < qty:
            return False, f"보유 수량이 부족합니다. 보유: {have} / 판매 요청: {qty}"

        # 은퇴 선수: 기준가의 30% 방출 (수수료 없음, 시장 시간 무관)
        if int(retired) == 1:
            compensation = int(int(basev) * 0.30) * qty
            async with self._lock:
                def work_ret():
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
                await self._run(work_ret)
            await add_balance(user_id, compensation)
            return True, (
                f"✅ 은퇴 선수 방출: `{pid}` **{name}** x{qty}\n"
                f"기준가 {int(basev):,}원 × 30% → **{compensation:,}원** 수령"
            )

        # 활성 선수: 시장 오픈 시간에만 거래 가능
        if not self._is_market_open(now_ts):
            st = await self.market_status(now_ts)
            return False, f"지금은 시장이 닫혀 있습니다. (다음 변경: <t:{st.next_change_ts}:f>)"

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
        return True, f"✅ 판매 완료: `{pid}` **{name}** x{qty} / 총 {gross:,}원 (수수료 {fee:,}) → 실수령 {net:,}원"

    async def buy_pack(self, *, user_id: int, pack_type: str, pulls: int, now_ts: int, get_balance, add_balance):
        pack_type = (pack_type or "").strip()
        pulls = max(1, min(PACK_MAX_PULLS, int(pulls)))

        if pack_type not in PACKS:
            return False, "존재하지 않는 팩입니다. (브론즈/실버/골드/플래티넘/아이콘)", None

        pack = PACKS[pack_type]
        pack_price = int(pack["price"])
        total_cost = pack_price * pulls

        bal = await get_balance(user_id)
        if bal < total_cost:
            return False, f"잔액이 부족합니다. 필요: {total_cost:,} / 보유: {bal:,}", None

        # 선차감 (풀 비면 환불)
        await add_balance(user_id, -total_cost)

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    # 절대 가격 범위 기반으로 풀 필터링 (풀 크기 변화에 무관)
                    min_p = int(pack.get("min_price", 0) or 0)
                    max_p = pack.get("max_price", None)

                    if max_p is not None:
                        rows = con.execute(
                            """
                            SELECT p.player_id, COALESCE(m.price, p.base_value) AS cur_price
                            FROM pm_players p
                            LEFT JOIN pm_market m ON m.player_id = p.player_id
                            WHERE p.retired = 0 AND p.player_id NOT LIKE 'AMT_%'
                              AND COALESCE(m.price, p.base_value) BETWEEN ? AND ?
                            ORDER BY cur_price DESC
                            """,
                            (min_p, int(max_p)),
                        ).fetchall()
                    else:
                        rows = con.execute(
                            """
                            SELECT p.player_id, COALESCE(m.price, p.base_value) AS cur_price
                            FROM pm_players p
                            LEFT JOIN pm_market m ON m.player_id = p.player_id
                            WHERE p.retired = 0 AND p.player_id NOT LIKE 'AMT_%'
                              AND COALESCE(m.price, p.base_value) >= ?
                            ORDER BY cur_price DESC
                            """,
                            (min_p,),
                        ).fetchall()

                    # 해당 등급 선수가 없으면 → 환불 처리 (폴백 없음)
                    if not rows:
                        return ("EMPTY_TIER", [])

                    # 팩 가격 기준 비대칭 가우시안 가중치 부여
                    players = [(str(r[0]), int(r[1])) for r in rows]
                    weights = [_pack_weight(pp, pack_price) for _, pp in players]

                    results: List[Tuple[str, int]] = []
                    for _ in range(pulls):
                        chosen_pid, chosen_price = random.choices(players, weights=weights, k=1)[0]
                        results.append((chosen_pid, chosen_price))
                        con.execute(
                            """
                            INSERT INTO pm_holdings(user_id, player_id, qty)
                            VALUES(?, ?, 1)
                            ON CONFLICT(user_id, player_id) DO UPDATE SET qty=qty+1
                            """,
                            (int(user_id), str(chosen_pid)),
                        )

                    con.commit()
                    return ("OK", results)
                finally:
                    con.close()

            status, results = await self._run(work)

        if status in ("EMPTY", "EMPTY_TIER"):
            await add_balance(user_id, total_cost)
            if status == "EMPTY_TIER":
                return False, (
                    f"**{pack_type}팩** 등급({int(pack.get('min_price',0)):,}원~"
                    + (f"{int(pack['max_price']):,}원" if pack.get("max_price") else "∞")
                    + ")에 해당하는 선수가 현재 없습니다.\n"
                    "💸 구입 비용이 전액 환불됐습니다."
                ), None
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

    async def run_month_if_due(self, now_ts: int) -> Tuple[int, dict]:
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute("SELECT start_ts, month_index, last_month_ts FROM pm_game_time WHERE id=1").fetchone()
                    if not row:
                        return 0, {}
                    _start_ts, month_index, last_month_ts = int(row[0]), int(row[1]), int(row[2])

                    if (now_ts - last_month_ts) < REAL_SECONDS_PER_MONTH:
                        return 0, {}

                    months_due = int((now_ts - last_month_ts) // REAL_SECONDS_PER_MONTH)
                    if months_due <= 0:
                        return 0, {}

                    # 월 처리 전 초기 상태 캡처 (알림 비교용)
                    initial_rows = con.execute(
                        "SELECT player_id, name, ovr FROM pm_players WHERE retired=0"
                    ).fetchall()
                    initial_data = {str(r[0]): {"name": str(r[1]), "ovr": int(r[2])} for r in initial_rows}
                    retired_this_run: set = set()

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
                                inc = 2 if random.random() < 0.25 else 1
                                ovr = min(pot, ovr + inc)

                            # 은퇴
                            if random.random() < retire_prob(age):
                                retired_this_run.add(str(pid))
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

                        # 풀 유지(POOL_SIZE명)
                        active = con.execute("SELECT COUNT(*) FROM pm_players WHERE retired=0").fetchone()[0]
                        need = max(0, POOL_SIZE - int(active))

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

                    # ── 알림용 이벤트 수집 ──
                    final_rows = con.execute(
                        "SELECT player_id, ovr FROM pm_players WHERE retired=0"
                    ).fetchall()
                    final_ovr = {str(r[0]): int(r[1]) for r in final_rows}

                    events: list = []
                    for pid_str, init in initial_data.items():
                        if pid_str in retired_this_run:
                            events.append({"event": "retire", "pid": pid_str, "name": init["name"]})
                        elif pid_str in final_ovr and final_ovr[pid_str] > init["ovr"]:
                            events.append({
                                "event": "growth", "pid": pid_str, "name": init["name"],
                                "ovr_before": init["ovr"], "ovr_after": final_ovr[pid_str],
                            })

                    # 보유자 → 이벤트 매핑
                    user_events: dict = {}
                    for evt in events:
                        holders = con.execute(
                            "SELECT user_id FROM pm_holdings WHERE player_id=? AND qty > 0",
                            (evt["pid"],),
                        ).fetchall()
                        for (uid,) in holders:
                            user_events.setdefault(int(uid), []).append(evt)

                    return int(months_due), user_events
                finally:
                    con.close()

            return await self._run(work)

    # ───────────────── 이적시장 ─────────────────

    async def create_listing(
        self, *, seller_id: int, player_id: str, qty: int, price_per: int, now_ts: int
    ) -> Tuple[bool, str]:
        """선수를 이적시장에 등록 (보유에서 차감)"""
        pid = (player_id or "").strip()
        qty = int(qty)
        price_per = int(price_per)

        if qty <= 0:
            return False, "수량은 1 이상이어야 합니다."
        if price_per <= 0:
            return False, "가격은 1원 이상이어야 합니다."

        row = await self.get_player(pid)
        if not row:
            return False, "선수를 찾을 수 없습니다."

        (_pid, name, _nat, _pos, _age, _ovr, _potg, _basev, retired, *_rest) = row
        if int(retired) == 1:
            return False, "은퇴 선수는 이적시장에 등록할 수 없습니다. `/방출` 명령어를 사용하세요."

        have = await self.get_holding(seller_id, pid)
        if have < qty:
            return False, f"보유 수량이 부족합니다. 보유: {have} / 등록 요청: {qty}"

        expires_at      = now_ts + LISTING_DURATION
        instant_sell_at = now_ts + INSTANT_SELL_DELAY

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("BEGIN IMMEDIATE;")
                    con.execute(
                        "UPDATE pm_holdings SET qty=qty-? WHERE user_id=? AND player_id=?",
                        (qty, int(seller_id), pid),
                    )
                    cur = con.execute(
                        """
                        INSERT INTO pm_listings(seller_id, player_id, qty, price_per,
                                                listed_at, expires_at, instant_sell_at, status)
                        VALUES(?, ?, ?, ?, ?, ?, ?, 'active')
                        """,
                        (int(seller_id), pid, qty, price_per, now_ts, expires_at, instant_sell_at),
                    )
                    lid = cur.lastrowid
                    con.commit()
                    return lid
                except Exception:
                    try: con.execute("ROLLBACK;")
                    except Exception: pass
                    raise
                finally:
                    con.close()
            lid = await self._run(work)

        return True, (
            f"✅ 이적시장 등록 완료\n"
            f"매물 번호: **#{lid}** | **{name}** x{qty}\n"
            f"등록가: **{price_per:,}원** / 장\n"
            f"⏳ 12시간 후 즉시판매 가능 · 72시간 후 자동 만료(선수 반환)"
        )

    async def get_listings(self, q: str = "", limit: int = 10, offset: int = 0) -> List:
        """이적시장 활성 매물 조회"""
        q = (q or "").strip()
        limit  = max(1, min(25, int(limit)))
        offset = max(0, int(offset))
        now_ts = int(time.time())

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    if not q:
                        return con.execute(
                            """
                            SELECT l.listing_id, l.seller_id, l.player_id, l.qty, l.price_per,
                                   l.listed_at, l.expires_at, l.instant_sell_at,
                                   p.name, p.nation, p.position, p.age, p.ovr, p.pot_grade, p.base_value
                            FROM pm_listings l
                            JOIN pm_players p ON p.player_id=l.player_id
                            WHERE l.status='active' AND l.expires_at > ?
                            ORDER BY l.listed_at DESC
                            LIMIT ? OFFSET ?
                            """,
                            (now_ts, limit, offset),
                        ).fetchall()

                    like = f"%{q}%"
                    return con.execute(
                        """
                        SELECT l.listing_id, l.seller_id, l.player_id, l.qty, l.price_per,
                               l.listed_at, l.expires_at, l.instant_sell_at,
                               p.name, p.nation, p.position, p.age, p.ovr, p.pot_grade, p.base_value
                        FROM pm_listings l
                        JOIN pm_players p ON p.player_id=l.player_id
                        WHERE l.status='active' AND l.expires_at > ?
                          AND (p.name LIKE ? OR p.nation LIKE ? OR p.position LIKE ?
                               OR p.pot_grade LIKE ? OR l.player_id LIKE ?)
                        ORDER BY l.price_per ASC
                        LIMIT ? OFFSET ?
                        """,
                        (now_ts, like, like, like, like, like, limit, offset),
                    ).fetchall()
                finally:
                    con.close()
            return await self._run(work)

    async def count_listings(self, q: str = "") -> int:
        """이적시장 활성 매물 수"""
        q = (q or "").strip()
        now_ts = int(time.time())
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    if not q:
                        row = con.execute(
                            "SELECT COUNT(*) FROM pm_listings WHERE status='active' AND expires_at > ?",
                            (now_ts,),
                        ).fetchone()
                    else:
                        like = f"%{q}%"
                        row = con.execute(
                            """
                            SELECT COUNT(*) FROM pm_listings l
                            JOIN pm_players p ON p.player_id=l.player_id
                            WHERE l.status='active' AND l.expires_at > ?
                              AND (p.name LIKE ? OR p.nation LIKE ? OR p.position LIKE ?
                                   OR p.pot_grade LIKE ? OR l.player_id LIKE ?)
                            """,
                            (now_ts, like, like, like, like, like),
                        ).fetchone()
                    return int(row[0]) if row else 0
                finally:
                    con.close()
            return await self._run(work)

    async def get_my_listings(self, user_id: int) -> List:
        """내 이적시장 활성 매물 조회"""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    return con.execute(
                        """
                        SELECT l.listing_id, l.player_id, l.qty, l.price_per,
                               l.listed_at, l.expires_at, l.instant_sell_at,
                               p.name, p.nation, p.position, p.age, p.ovr, p.pot_grade, p.base_value
                        FROM pm_listings l
                        JOIN pm_players p ON p.player_id=l.player_id
                        WHERE l.seller_id=? AND l.status='active'
                        ORDER BY l.listed_at DESC
                        """,
                        (int(user_id),),
                    ).fetchall()
                finally:
                    con.close()
            return await self._run(work)

    async def buy_listing(
        self, *, listing_id: int, buyer_id: int, qty: int, now_ts: int, get_balance, add_balance
    ) -> Tuple[bool, str, dict | None]:
        """이적시장 매물 구매 (수수료 5%). 성공 시 3번째 원소에 알림용 dict 반환."""
        qty = int(qty)
        if qty <= 0:
            return False, "수량은 1 이상이어야 합니다.", None

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute(
                        """
                        SELECT l.seller_id, l.player_id, l.qty, l.price_per, l.expires_at,
                               p.name
                        FROM pm_listings l
                        JOIN pm_players p ON p.player_id=l.player_id
                        WHERE l.listing_id=? AND l.status='active'
                        """,
                        (int(listing_id),),
                    ).fetchone()
                    if not row:
                        return None, "매물을 찾을 수 없습니다. (이미 판매됐거나 취소된 매물)"

                    seller_id, pid, avail_qty, price_per, expires_at, name = row

                    if int(buyer_id) == int(seller_id):
                        return None, "자신의 매물은 구매할 수 없습니다."
                    if now_ts > int(expires_at):
                        return None, "만료된 매물입니다."
                    if qty > int(avail_qty):
                        return None, f"요청 수량이 초과됩니다. 남은 수량: **{int(avail_qty)}장**"

                    total_cost  = int(price_per) * qty
                    fee         = int(total_cost * TRANSFER_FEE_RATE)
                    seller_gets = total_cost - fee
                    remaining   = int(avail_qty) - qty

                    con.execute("BEGIN IMMEDIATE;")
                    if remaining <= 0:
                        con.execute(
                            "UPDATE pm_listings SET status='sold', qty=0 WHERE listing_id=?",
                            (int(listing_id),),
                        )
                    else:
                        con.execute(
                            "UPDATE pm_listings SET qty=? WHERE listing_id=?",
                            (remaining, int(listing_id)),
                        )
                    con.execute(
                        """
                        INSERT INTO pm_holdings(user_id, player_id, qty)
                        VALUES(?, ?, ?)
                        ON CONFLICT(user_id, player_id) DO UPDATE SET qty=qty+excluded.qty
                        """,
                        (int(buyer_id), str(pid), qty),
                    )
                    con.commit()
                    return (int(seller_id), str(pid), qty, total_cost, fee, seller_gets, name), None
                except Exception:
                    try: con.execute("ROLLBACK;")
                    except Exception: pass
                    raise
                finally:
                    con.close()
            result, err = await self._run(work)

        if err:
            return False, err, None

        seller_id, pid, qty_bought, total_cost, fee, seller_gets, name = result

        # 잔액 반영 (잔액 확인은 lock 밖에서 — 실패 시 holdings rollback 불가이므로 순서 중요)
        bal = await get_balance(buyer_id)
        if bal < total_cost:
            # 재고를 이미 차감했으므로 롤백
            async with self._lock:
                def rollback():
                    con = self._connect()
                    try:
                        con.execute("BEGIN IMMEDIATE;")
                        con.execute(
                            "UPDATE pm_holdings SET qty=qty-? WHERE user_id=? AND player_id=?",
                            (qty_bought, int(buyer_id), pid),
                        )
                        # 매물 복원
                        remaining_now = int(avail_qty) - qty_bought  # type: ignore
                        if remaining_now <= 0:
                            con.execute(
                                "UPDATE pm_listings SET status='active', qty=? WHERE listing_id=?",
                                (qty_bought, int(listing_id)),
                            )
                        else:
                            con.execute(
                                "UPDATE pm_listings SET qty=qty+? WHERE listing_id=?",
                                (qty_bought, int(listing_id)),
                            )
                        con.commit()
                    except Exception:
                        try: con.execute("ROLLBACK;")
                        except Exception: pass
                    finally:
                        con.close()
                await self._run(rollback)
            return False, f"잔액이 부족합니다. 필요: {total_cost:,} / 보유: {bal:,}", None

        await add_balance(buyer_id, -total_cost)
        await add_balance(seller_id, seller_gets)
        return True, (
            f"✅ 이적 구매 완료: **{name}** x{qty_bought}\n"
            f"총액: **{total_cost:,}원** (플랫폼 수수료 {fee:,}원)\n"
            f"판매자 수령: **{seller_gets:,}원**"
        ), {"seller_id": int(seller_id), "name": name, "qty": qty_bought, "price": int(price_per), "seller_gets": int(seller_gets)}

    async def cancel_listing(self, *, listing_id: int, seller_id: int) -> Tuple[bool, str]:
        """이적시장 매물 취소 (선수 보유 반환)"""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute(
                        """
                        SELECT l.seller_id, l.player_id, l.qty, p.name
                        FROM pm_listings l
                        JOIN pm_players p ON p.player_id=l.player_id
                        WHERE l.listing_id=? AND l.status='active'
                        """,
                        (int(listing_id),),
                    ).fetchone()
                    if not row:
                        return False, "매물을 찾을 수 없습니다. (이미 판매됐거나 취소된 매물)"

                    s_id, pid, qty, name = row
                    if int(s_id) != int(seller_id):
                        return False, "본인의 매물만 취소할 수 있습니다."

                    con.execute("BEGIN IMMEDIATE;")
                    con.execute(
                        "UPDATE pm_listings SET status='cancelled' WHERE listing_id=?",
                        (int(listing_id),),
                    )
                    con.execute(
                        """
                        INSERT INTO pm_holdings(user_id, player_id, qty)
                        VALUES(?, ?, ?)
                        ON CONFLICT(user_id, player_id) DO UPDATE SET qty=qty+excluded.qty
                        """,
                        (int(s_id), str(pid), int(qty)),
                    )
                    con.commit()
                    return True, f"✅ 매물 취소 완료: **{name}** x{int(qty)} 보유 반환"
                except Exception:
                    try: con.execute("ROLLBACK;")
                    except Exception: pass
                    raise
                finally:
                    con.close()
            return await self._run(work)

    async def instant_sell_listing(
        self, *, listing_id: int, seller_id: int, now_ts: int, add_balance
    ) -> Tuple[bool, str]:
        """매각 — 이적시장 등록 후 12시간 대기, 항상 기준가 × 70%.
        시장 오픈/외 무관하게 70% 고정.
        """
        rate = INSTANT_SELL_RATE  # 항상 70%

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute(
                        """
                        SELECT l.seller_id, l.player_id, l.qty, l.instant_sell_at,
                               p.name, p.base_value
                        FROM pm_listings l
                        JOIN pm_players p ON p.player_id=l.player_id
                        WHERE l.listing_id=? AND l.status='active'
                        """,
                        (int(listing_id),),
                    ).fetchone()
                    if not row:
                        return None, "매물을 찾을 수 없습니다. (이미 판매됐거나 취소된 매물)"

                    s_id, pid, qty, instant_sell_at, name, base_value = row
                    if int(s_id) != int(seller_id):
                        return None, "본인의 매물만 즉시판매할 수 있습니다."
                    if now_ts < int(instant_sell_at):
                        rem = int(instant_sell_at) - now_ts
                        h = rem // 3600
                        m = (rem % 3600) // 60
                        return None, f"아직 즉시판매 가능 시간이 아닙니다.\n남은 시간: **{h}시간 {m}분**"

                    payout  = int(int(base_value) * rate) * int(qty)
                    fee_amt = int(int(base_value) * (1.0 - rate)) * int(qty)

                    con.execute("BEGIN IMMEDIATE;")
                    con.execute(
                        "UPDATE pm_listings SET status='sold', qty=0 WHERE listing_id=?",
                        (int(listing_id),),
                    )
                    con.commit()
                    return (int(s_id), int(qty), payout, fee_amt, name, int(base_value)), None
                except Exception:
                    try: con.execute("ROLLBACK;")
                    except Exception: pass
                    raise
                finally:
                    con.close()
            result, err = await self._run(work)

        if err:
            return False, err

        _s_id, qty, payout, fee_amt, name, base_value = result
        rate_pct = int(rate * 100)
        fee_pct  = 100 - rate_pct
        await add_balance(seller_id, payout)
        return True, (
            f"✅ 매각 완료: **{name}** x{qty}\n"
            f"기준가: **{base_value:,}원** × {rate_pct}% × {qty}장\n"
            f"수수료({fee_pct}%): **{fee_amt:,}원**\n"
            f"실수령: **{payout:,}원**"
        )

    async def direct_instant_sell(
        self, *, user_id: int, player_id: str, qty: int, now_ts: int, add_balance
    ) -> Tuple[bool, str, int]:  # (ok, msg, payout)
        """보유 선수 즉시 매각 (매물 등록·대기 없음).
        시장 시간 무관, 항상 기준가 × 50% 지급.
        아마추어 선수 및 은퇴 선수는 불가.
        """
        rate = INSTANT_SELL_RATE_OFF  # 항상 50%

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    # 선수 정보
                    row = con.execute(
                        "SELECT name, base_value, retired FROM pm_players WHERE player_id=?",
                        (str(player_id),),
                    ).fetchone()
                    if not row:
                        return None, "선수를 찾을 수 없습니다."
                    name, base_value, retired = row
                    if str(player_id).startswith("AMT_"):
                        return None, "아마추어 선수는 매각할 수 없습니다."
                    if int(retired) == 1:
                        return None, "은퇴 선수는 `/방출` 명령어를 사용해 주세요."

                    # 보유 수량 확인
                    have_row = con.execute(
                        "SELECT qty FROM pm_holdings WHERE user_id=? AND player_id=?",
                        (int(user_id), str(player_id)),
                    ).fetchone()
                    have = int(have_row[0]) if have_row else 0
                    if have < qty:
                        return None, f"보유 수량이 부족합니다. (보유: {have}장 / 요청: {qty}장)"

                    payout  = int(int(base_value) * rate) * qty
                    fee_amt = int(int(base_value) * (1.0 - rate)) * qty

                    con.execute("BEGIN IMMEDIATE;")
                    new_qty = have - qty
                    if new_qty <= 0:
                        con.execute(
                            "DELETE FROM pm_holdings WHERE user_id=? AND player_id=?",
                            (int(user_id), str(player_id)),
                        )
                    else:
                        con.execute(
                            "UPDATE pm_holdings SET qty=? WHERE user_id=? AND player_id=?",
                            (new_qty, int(user_id), str(player_id)),
                        )
                    con.commit()
                    return (name, int(base_value), payout, fee_amt), None
                except Exception:
                    try: con.execute("ROLLBACK;")
                    except Exception: pass
                    raise
                finally:
                    con.close()
            result, err = await self._run(work)

        if err:
            return False, err, 0

        name, base_value, payout, fee_amt = result
        await add_balance(user_id, payout)
        return True, (
            f"✅ **{name}** x{qty} — 기준가 {base_value:,}원 × 50% → **{payout:,}원**"
        ), payout

    # ───────────────── 트레이드 ─────────────────

    async def create_trade(
        self, *,
        proposer_id: int, receiver_id: int,
        proposer_pids: List[str], receiver_pids: List[str],
        proposer_cash: int, receiver_cash: int,
        now_ts: int, get_balance,
    ) -> Tuple[bool, any, dict]:
        """트레이드 제안 생성 — 제안자 선수는 즉시 escrow(보유에서 차감)"""
        prop_qty = Counter(proposer_pids)   # {pid: qty}
        recv_qty = Counter(receiver_pids)

        # 사전 검증 (lock 밖)
        prop_details: List[Tuple[str, str, int]] = []
        for pid, qty in prop_qty.items():
            row = await self.get_player(pid)
            if not row:
                return False, f"선수를 찾을 수 없습니다: `{pid}`", {}
            name, retired = row[1], int(row[8])
            if retired == 1:
                return False, f"은퇴 선수는 트레이드할 수 없습니다: **{name}**", {}
            have = await self.get_holding(proposer_id, pid)
            if have < qty:
                return False, f"보유 수량 부족: **{name}** (보유 {have} / 필요 {qty})", {}
            prop_details.append((pid, name, qty))

        recv_details: List[Tuple[str, str, int]] = []
        for pid, qty in recv_qty.items():
            row = await self.get_player(pid)
            if not row:
                return False, f"선수를 찾을 수 없습니다: `{pid}`", {}
            name, retired = row[1], int(row[8])
            if retired == 1:
                return False, f"은퇴 선수는 트레이드할 수 없습니다: **{name}**", {}
            recv_details.append((pid, name, qty))

        if proposer_cash > 0:
            bal = await get_balance(proposer_id)
            if bal < proposer_cash:
                return False, f"현금 잔액 부족 (보유 {bal:,} / 필요 {proposer_cash:,})", {}

        expires_at = now_ts + TRADE_EXPIRE

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    # 재검증 (lock 안)
                    for pid, qty in prop_qty.items():
                        row = con.execute(
                            "SELECT qty FROM pm_holdings WHERE user_id=? AND player_id=?",
                            (int(proposer_id), pid),
                        ).fetchone()
                        have = int(row[0]) if row else 0
                        if have < qty:
                            return None, f"보유 수량이 변경됐습니다. 다시 시도하세요. ({pid})"

                    con.execute("BEGIN IMMEDIATE;")
                    # 제안자 선수 차감 (escrow)
                    for pid, qty in prop_qty.items():
                        con.execute(
                            "UPDATE pm_holdings SET qty=qty-? WHERE user_id=? AND player_id=?",
                            (qty, int(proposer_id), pid),
                        )
                    # trade 레코드
                    cur = con.execute(
                        """
                        INSERT INTO pm_trades(proposer_id, receiver_id, proposer_cash, receiver_cash,
                                              status, created_at, expires_at)
                        VALUES(?, ?, ?, ?, 'pending', ?, ?)
                        """,
                        (int(proposer_id), int(receiver_id),
                         int(proposer_cash), int(receiver_cash), now_ts, expires_at),
                    )
                    tid = cur.lastrowid
                    for pid, qty in prop_qty.items():
                        con.execute(
                            "INSERT INTO pm_trade_items(trade_id, side, player_id, qty) VALUES(?, 'proposer', ?, ?)",
                            (tid, pid, qty),
                        )
                    for pid, qty in recv_qty.items():
                        con.execute(
                            "INSERT INTO pm_trade_items(trade_id, side, player_id, qty) VALUES(?, 'receiver', ?, ?)",
                            (tid, pid, qty),
                        )
                    con.commit()
                    return tid, None
                except Exception:
                    try: con.execute("ROLLBACK;")
                    except Exception: pass
                    raise
                finally:
                    con.close()
            tid, err = await self._run(work)

        if err:
            return False, err, {}
        details = {"proposer_items": prop_details, "receiver_items": recv_details}
        return True, tid, details

    async def accept_trade(
        self, *, trade_id: int, receiver_id: int, now_ts: int, get_balance, add_balance
    ) -> Tuple[bool, str, int | None]:
        """트레이드 수락 — 선수 교환 + 현금 처리. 성공 시 3번째 원소에 proposer_id 반환"""
        # 정보 조회 (lock 밖)
        async with self._lock:
            def get_info():
                con = self._connect()
                try:
                    trade = con.execute(
                        "SELECT proposer_id, receiver_id, proposer_cash, receiver_cash, status, expires_at FROM pm_trades WHERE trade_id=?",
                        (int(trade_id),),
                    ).fetchone()
                    if not trade:
                        return None, None
                    items = con.execute(
                        "SELECT side, player_id, qty FROM pm_trade_items WHERE trade_id=?",
                        (int(trade_id),),
                    ).fetchall()
                    return trade, items
                finally:
                    con.close()
            trade, items = await self._run(get_info)

        if not trade:
            return False, "트레이드를 찾을 수 없습니다.", None

        proposer_id, r_id, prop_cash, recv_cash, status, expires_at = trade
        if int(r_id) != int(receiver_id):
            return False, "본인의 트레이드만 수락할 수 있습니다.", None
        if status != "pending":
            return False, "이미 처리된 트레이드입니다.", None
        if now_ts > int(expires_at):
            return False, "만료된 트레이드입니다.", None

        proposer_items = [(pid, qty) for side, pid, qty in items if side == "proposer"]
        receiver_items = [(pid, qty) for side, pid, qty in items if side == "receiver"]

        # 현금 검증
        if int(prop_cash) > 0:
            bal = await get_balance(proposer_id)
            if bal < int(prop_cash):
                return False, f"제안자의 현금이 부족합니다. (필요 {int(prop_cash):,} / 보유 {bal:,})", None
        if int(recv_cash) > 0:
            bal = await get_balance(receiver_id)
            if bal < int(recv_cash):
                return False, f"현금이 부족합니다. (필요 {int(recv_cash):,} / 보유 {bal:,})", None

        # 실행 (lock 안)
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    # 상태 재확인
                    s = con.execute("SELECT status FROM pm_trades WHERE trade_id=?", (int(trade_id),)).fetchone()
                    if not s or s[0] != "pending":
                        return None, "이미 처리된 트레이드입니다."

                    # 수신자 보유 검증
                    for pid, qty in receiver_items:
                        row = con.execute(
                            "SELECT qty FROM pm_holdings WHERE user_id=? AND player_id=?",
                            (int(receiver_id), pid),
                        ).fetchone()
                        have = int(row[0]) if row else 0
                        if have < qty:
                            nr = con.execute("SELECT name FROM pm_players WHERE player_id=?", (pid,)).fetchone()
                            name = nr[0] if nr else pid
                            return None, f"보유 수량 부족: **{name}** (보유 {have} / 필요 {qty})"

                    con.execute("BEGIN IMMEDIATE;")
                    # 수신자 선수 차감
                    for pid, qty in receiver_items:
                        con.execute(
                            "UPDATE pm_holdings SET qty=qty-? WHERE user_id=? AND player_id=?",
                            (qty, int(receiver_id), pid),
                        )
                    # 제안자 escrow 선수 → 수신자
                    for pid, qty in proposer_items:
                        con.execute(
                            "INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(?, ?, ?) ON CONFLICT(user_id, player_id) DO UPDATE SET qty=qty+excluded.qty",
                            (int(receiver_id), pid, qty),
                        )
                    # 수신자 선수 → 제안자
                    for pid, qty in receiver_items:
                        con.execute(
                            "INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(?, ?, ?) ON CONFLICT(user_id, player_id) DO UPDATE SET qty=qty+excluded.qty",
                            (int(proposer_id), pid, qty),
                        )
                    con.execute("UPDATE pm_trades SET status='accepted' WHERE trade_id=?", (int(trade_id),))
                    con.commit()
                    return True, None
                except Exception:
                    try: con.execute("ROLLBACK;")
                    except Exception: pass
                    raise
                finally:
                    con.close()
            ok_r, err = await self._run(work)

        if err or not ok_r:
            return False, err or "처리 중 오류가 발생했습니다.", None

        # 현금 이동
        if int(prop_cash) > 0:
            await add_balance(proposer_id, -int(prop_cash))
            await add_balance(receiver_id, int(prop_cash))
        if int(recv_cash) > 0:
            await add_balance(receiver_id, -int(recv_cash))
            await add_balance(proposer_id, int(recv_cash))

        return True, "✅ 트레이드 체결 완료! 선수들이 교환됐습니다.", int(proposer_id)

    async def reject_trade(self, *, trade_id: int, receiver_id: int) -> Tuple[bool, str, int | None]:
        """트레이드 거절 — 제안자 선수 반환. 성공 시 3번째 원소에 proposer_id 반환"""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    trade = con.execute(
                        "SELECT proposer_id, receiver_id, status FROM pm_trades WHERE trade_id=?",
                        (int(trade_id),),
                    ).fetchone()
                    if not trade:
                        return False, "트레이드를 찾을 수 없습니다.", None
                    proposer_id, r_id, status = trade
                    if int(r_id) != int(receiver_id):
                        return False, "본인의 트레이드만 거절할 수 있습니다.", None
                    if status != "pending":
                        return False, "이미 처리된 트레이드입니다.", None

                    items = con.execute(
                        "SELECT player_id, qty FROM pm_trade_items WHERE trade_id=? AND side='proposer'",
                        (int(trade_id),),
                    ).fetchall()
                    con.execute("BEGIN IMMEDIATE;")
                    for pid, qty in items:
                        con.execute(
                            "INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(?, ?, ?) ON CONFLICT(user_id, player_id) DO UPDATE SET qty=qty+excluded.qty",
                            (int(proposer_id), pid, int(qty)),
                        )
                    con.execute("UPDATE pm_trades SET status='rejected' WHERE trade_id=?", (int(trade_id),))
                    con.commit()
                    return True, "트레이드가 거절됐습니다. 제안자에게 선수들이 반환됩니다.", int(proposer_id)
                except Exception:
                    try: con.execute("ROLLBACK;")
                    except Exception: pass
                    raise
                finally:
                    con.close()
            return await self._run(work)

    async def cancel_trade(self, *, trade_id: int, proposer_id: int) -> Tuple[bool, str]:
        """트레이드 취소 — 제안자 선수 반환"""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    trade = con.execute(
                        "SELECT proposer_id, status FROM pm_trades WHERE trade_id=?",
                        (int(trade_id),),
                    ).fetchone()
                    if not trade:
                        return False, "트레이드를 찾을 수 없습니다."
                    p_id, status = trade
                    if int(p_id) != int(proposer_id):
                        return False, "본인이 제안한 트레이드만 취소할 수 있습니다."
                    if status != "pending":
                        return False, "이미 처리된 트레이드입니다."

                    items = con.execute(
                        "SELECT player_id, qty FROM pm_trade_items WHERE trade_id=? AND side='proposer'",
                        (int(trade_id),),
                    ).fetchall()
                    con.execute("BEGIN IMMEDIATE;")
                    for pid, qty in items:
                        con.execute(
                            "INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(?, ?, ?) ON CONFLICT(user_id, player_id) DO UPDATE SET qty=qty+excluded.qty",
                            (int(p_id), pid, int(qty)),
                        )
                    con.execute("UPDATE pm_trades SET status='cancelled' WHERE trade_id=?", (int(trade_id),))
                    con.commit()
                    return True, "✅ 트레이드 취소 완료. 선수들이 보유 목록으로 반환됩니다."
                except Exception:
                    try: con.execute("ROLLBACK;")
                    except Exception: pass
                    raise
                finally:
                    con.close()
            return await self._run(work)

    async def get_my_pending_trades(self, user_id: int) -> List:
        """대기 중인 트레이드 목록 (제안자 or 수신자) — 아이템 정보 포함"""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    trades = con.execute(
                        """
                        SELECT trade_id, proposer_id, receiver_id, proposer_cash, receiver_cash, expires_at
                        FROM pm_trades
                        WHERE (proposer_id=? OR receiver_id=?) AND status='pending'
                        ORDER BY created_at DESC
                        """,
                        (int(user_id), int(user_id)),
                    ).fetchall()
                    result = []
                    for t in trades:
                        items = con.execute(
                            """
                            SELECT ti.side, COALESCE(p.name, ti.player_id), ti.qty
                            FROM pm_trade_items ti
                            LEFT JOIN pm_players p ON ti.player_id = p.player_id
                            WHERE ti.trade_id=?
                            ORDER BY ti.side, p.name
                            """,
                            (int(t[0]),),
                        ).fetchall()
                        result.append((*t, items))
                    return result
                finally:
                    con.close()
            return await self._run(work)

    async def get_ranking(self, limit: int = 10) -> list:
        """잔액 + 보유 선수 현재가 합산 랭킹"""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    return con.execute(
                        """
                        SELECT w.user_id,
                               w.balance,
                               COALESCE(SUM(h.qty * mk.price), 0) AS player_value,
                               w.balance + COALESCE(SUM(h.qty * mk.price), 0) AS total
                        FROM wallets w
                        LEFT JOIN pm_holdings h  ON w.user_id  = h.user_id  AND h.qty > 0
                        LEFT JOIN pm_market   mk ON h.player_id = mk.player_id
                        GROUP BY w.user_id
                        ORDER BY total DESC
                        LIMIT ?
                        """,
                        (int(limit),),
                    ).fetchall()
                finally:
                    con.close()
            return await self._run(work)

    async def bulk_release_retired(self, user_id: int) -> Tuple[int, int, list]:
        """보유한 은퇴 선수 전체 방출 → (방출 수, 총 수령액, 상세 목록)"""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    rows = con.execute(
                        """
                        SELECT h.player_id, h.qty, p.base_value, p.name
                        FROM pm_holdings h
                        JOIN pm_players p ON h.player_id = p.player_id
                        WHERE h.user_id=? AND p.retired=1 AND h.qty > 0
                        """,
                        (int(user_id),),
                    ).fetchall()
                    if not rows:
                        return 0, 0, []

                    total_payout = 0
                    details = []
                    con.execute("BEGIN IMMEDIATE;")
                    for pid, qty, base_value, name in rows:
                        payout = int(int(base_value) * 0.30) * int(qty)
                        total_payout += payout
                        details.append({"name": str(name), "qty": int(qty), "payout": payout})
                        con.execute(
                            "DELETE FROM pm_holdings WHERE user_id=? AND player_id=?",
                            (int(user_id), str(pid)),
                        )
                    con.commit()
                    return len(rows), total_payout, details
                except Exception:
                    try: con.execute("ROLLBACK;")
                    except Exception: pass
                    raise
                finally:
                    con.close()
            return await self._run(work)

    async def delete_user(self, user_id: int) -> dict:
        """유저의 모든 player_market DB 데이터 삭제. 삭제된 행 수 반환."""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("BEGIN IMMEDIATE;")
                    results = {}
                    # 보유 선수
                    cur = con.execute("DELETE FROM pm_holdings WHERE user_id=?", (int(user_id),))
                    if cur.rowcount: results["pm_holdings"] = cur.rowcount
                    # 이적시장 매물
                    cur = con.execute("DELETE FROM pm_listings WHERE seller_id=?", (int(user_id),))
                    if cur.rowcount: results["pm_listings"] = cur.rowcount
                    # 트레이드 (제안자 또는 수신자)
                    trade_ids = [r[0] for r in con.execute(
                        "SELECT trade_id FROM pm_trades WHERE proposer_id=? OR receiver_id=?",
                        (int(user_id), int(user_id)),
                    ).fetchall()]
                    if trade_ids:
                        for tid in trade_ids:
                            con.execute("DELETE FROM pm_trade_items WHERE trade_id=?", (int(tid),))
                        con.execute(
                            "DELETE FROM pm_trades WHERE proposer_id=? OR receiver_id=?",
                            (int(user_id), int(user_id)),
                        )
                        results["pm_trades"] = len(trade_ids)
                    con.commit()
                    return results
                except Exception:
                    try: con.execute("ROLLBACK;")
                    except Exception: pass
                    raise
                finally:
                    con.close()
            return await self._run(work)

    async def expire_trades(self, now_ts: int) -> int:
        """만료된 트레이드 처리 — 제안자 선수 자동 반환"""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    rows = con.execute(
                        "SELECT trade_id, proposer_id FROM pm_trades WHERE status='pending' AND expires_at <= ?",
                        (int(now_ts),),
                    ).fetchall()
                    if not rows:
                        return 0

                    con.execute("BEGIN IMMEDIATE;")
                    for tid, proposer_id in rows:
                        items = con.execute(
                            "SELECT player_id, qty FROM pm_trade_items WHERE trade_id=? AND side='proposer'",
                            (int(tid),),
                        ).fetchall()
                        for pid, qty in items:
                            con.execute(
                                "INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(?, ?, ?) ON CONFLICT(user_id, player_id) DO UPDATE SET qty=qty+excluded.qty",
                                (int(proposer_id), pid, int(qty)),
                            )
                        con.execute("UPDATE pm_trades SET status='expired' WHERE trade_id=?", (int(tid),))
                    con.commit()
                    return len(rows)
                except Exception:
                    try: con.execute("ROLLBACK;")
                    except Exception: pass
                    raise
                finally:
                    con.close()
            return await self._run(work)

    async def expire_listings(self, now_ts: int) -> list:
        """만료된 매물 처리 (선수 자동 반환) → 만료된 매물 정보 목록 반환"""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    rows = con.execute(
                        """
                        SELECT l.listing_id, l.seller_id, l.player_id, l.qty,
                               COALESCE(p.name, l.player_id) AS name
                        FROM pm_listings l
                        LEFT JOIN pm_players p ON l.player_id = p.player_id
                        WHERE l.status='active' AND l.expires_at <= ?
                        """,
                        (int(now_ts),),
                    ).fetchall()
                    if not rows:
                        return []

                    con.execute("BEGIN IMMEDIATE;")
                    for lid, s_id, pid, qty, _name in rows:
                        con.execute(
                            "UPDATE pm_listings SET status='expired' WHERE listing_id=?",
                            (int(lid),),
                        )
                        con.execute(
                            """
                            INSERT INTO pm_holdings(user_id, player_id, qty)
                            VALUES(?, ?, ?)
                            ON CONFLICT(user_id, player_id) DO UPDATE SET qty=qty+excluded.qty
                            """,
                            (int(s_id), str(pid), int(qty)),
                        )
                    con.commit()
                    return [
                        {"seller_id": int(r[1]), "name": str(r[4]), "qty": int(r[3])}
                        for r in rows
                    ]
                except Exception:
                    try: con.execute("ROLLBACK;")
                    except Exception: pass
                    raise
                finally:
                    con.close()
            return await self._run(work)
