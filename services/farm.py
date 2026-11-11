# services/farm.py — 🌾 /농사 (3.8.1, 테스트 서버 먼저)
# 시작할 때 땅 하나를 고른다 (땅마다 세부 자원이 다르다) → 작물을 심고 동물을 기른다 → 수확물을 합쳐 등급을 올리거나 판다.
# 행동(심기 · 수확 · 모으기 · 합치기)마다 ⚡ 에너지 1. 버는 것은 🪙 농장 코인 — 씨앗 · 확장 · 낫 강화에 쓰고, 원하면 축구 봇 돈으로 환전.
# 축구 봇 돈이 0원이어도 다 할 수 있다 (당근 씨앗은 무료). 돈은 운 요소(상자 행운 열기 · 강화 행운 부적)에만 쓴다.
# 다른 사람 농장은 인수 제안으로 살 수 있고, 농장은 여러 개 가질 수 있다 (아래 '인수').
# 씨앗 · 동물은 서버마다 하나 있는 '농사 상점' 스레드에서 코인으로 사 두고 (farm_seeds), 농장에서 심고 입양한다.
import random
from typing import Optional

from services.economy_db import EconomyDB, _kst_day

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
CROPS = {
    "carrot": ("🥕", "당근", 0, 10, 4),
    "potato": ("🥔", "감자", 5, 15, 14),
    "wheat": ("🌾", "밀", 10, 30, 25),
    "corn": ("🌽", "옥수수", 20, 60, 45),
    "cabbage": ("🥬", "배추", 35, 90, 75),
    "tomato": ("🍅", "토마토", 60, 150, 120),
    "pumpkin": ("🎃", "호박", 100, 4 * 60, 190),
    "strawberry": ("🍓", "딸기", 150, 6 * 60, 280),
    "grape": ("🍇", "포도", 250, 9 * 60, 450),
    "watermelon": ("🍉", "수박", 400, 15 * 60, 700),
}
# 동물: key → (이모지, 이름, 값(코인), 만드는 물건, 만드는 시간(초)) — 땅 한 칸을 차지하고 계속 만든다
ANIMALS = {
    "chicken": ("🐔", "닭", 300, "egg", 30),
    "duck": ("🦆", "오리", 600, "feather", 45),
    "bee": ("🐝", "벌", 1_000, "honey", 75),
    "sheep": ("🐑", "양", 1_600, "wool", 2 * 60),
    "goat": ("🐐", "염소", 2_500, "cheese", 3 * 60),
    "pig": ("🐖", "돼지", 4_000, "truffle", 5 * 60),
    "cow": ("🐄", "소", 6_000, "milk", 8 * 60),
}
PRODUCTS = {"egg": ("🥚", "달걀", 30), "feather": ("🪶", "깃털", 50), "honey": ("🍯", "꿀", 80), "wool": ("🧶", "양털", 120),
            "cheese": ("🧀", "치즈", 180), "truffle": ("🍄", "송로버섯", 280), "milk": ("🥛", "우유", 400)}
ITEMS = {**{k: (e, n, v) for k, (e, n, _, _, v) in CROPS.items()}, **PRODUCTS}   # 창고에 들어가는 것: key → (이모지, 이름, ★1 판매가)
ANIMAL_PER_PLOTS = 3          # 동물은 땅 3칸당 1마리


def shop_price(key: str) -> Optional[int]:
    """농사 상점에서 파는 값 (코인). 무료 씨앗(당근)은 팔지 않는다 — 씨앗 없이 바로 심는다. 모르는 것은 None."""
    if key in CROPS:
        return CROPS[key][2] or None
    return ANIMALS[key][2] if key in ANIMALS else None
ANIMAL_REFUND = 0.5           # 동물을 내보내면 입양 값의 절반

# 합치기: 같은 물건 · 같은 등급 3개 → 한 등급 위 1개 (★5 까지). 등급이 오를수록 3개 값보다 조금씩 더 비싸다
TIER_MAX, MERGE_NEED = 5, 3
TIER_MULT = (1, 3.5, 12.5, 45, 165)


def value(key: str, tier: int) -> int:
    return round(ITEMS[key][2] * TIER_MULT[tier - 1])


# ───────────── 에너지 · 땅 확장 ─────────────
ENERGY_MAX, ENERGY_SEC = 100, 10      # 100 까지 · 10초에 1 (물 별에 따라 빨라진다)
PLOTS_BASE, PLOTS_MAX = 6, 24
START_CHESTS = {"wood": 1}            # 시작 선물
RATE = 1_000                          # 환전: 농장 코인 1 = 축구 봇 돈 1,000원 (코인 → 돈 한쪽으로만)


def expand_cost(plots: int) -> int:
    """지금 plots 칸일 때 한 칸 넓히는 값 (코인): 150 · 400 · 750 · 1,200 …"""
    k = plots - PLOTS_BASE + 1
    return 50 * k * (k + 2)


# ───────────── ⚒️ 낫 강화 ─────────────
SICKLE_MAX = 10
SICKLE_CHANCE = (0.95, 0.90, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.25, 0.20)   # +N → +N+1 성공 확률 (실패해도 안 내려간다)
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
CHEST_TABLE = {
    "wood": ((40, "coins", 20, 60), (30, "energy", 10, 15), (20, "whet", 1, 1), (10, "item", 1, 1)),
    "silver": ((35, "coins", 100, 300), (25, "energy", 25, 40), (25, "whet", 1, 2), (15, "item", 1, 1)),
    "gold": ((30, "coins", 600, 1_500), (20, "energy", 60, 100), (30, "whet", 3, 5), (20, "item", 1, 1)),
}
CHEST_ITEMS = {   # 상자에서 나오는 물건: (등급, 후보)
    "wood": (2, ("carrot", "potato", "corn", "egg")),
    "silver": (2, ("tomato", "strawberry", "wool")),
    "gold": (3, ("tomato", "strawberry", "wool")),
}
DAILY_CHEST = "wood"                  # 하루(KST) 첫 거두기에 나무 상자 하나
LUCKY_FEE = {"wood": 100_000, "silver": 500_000, "gold": 2_000_000}   # 🍀 행운 열기(축구 봇 돈): 보상마다 두 번 뽑아 더 좋은 쪽
EXPAND_CHEST = "silver"               # 땅을 넓히면 은 상자 하나


def chest_p(ore: int) -> float:
    """수확 · 모으기 한 번에 상자가 나올 확률 (광맥 별 1 = 3% … 별 5 = 9%)."""
    return 0.03 + 0.015 * (ore - 1)


def _roll_reward(tier: str, rng) -> dict:
    table = CHEST_TABLE[tier]
    _, kind, lo, hi = rng.choices(table, weights=[t[0] for t in table])[0]
    n = rng.randint(lo, hi)
    if kind == "item":
        t, pool = CHEST_ITEMS[tier]
        key = rng.choice(pool)
        return {"kind": "item", "key": key, "tier": t, "n": n, "worth": value(key, t) * n}
    return {"kind": kind, "n": n, "worth": n * {"coins": 1, "energy": 8, "whet": 250}[kind]}


def roll_chest(tier: str, lucky: bool, rng=random) -> list[dict]:
    """상자 보상. lucky 면 보상마다 두 번 뽑아 값이 큰 쪽."""
    out = []
    for _ in range(CHEST_REWARDS[tier]):
        r = _roll_reward(tier, rng)
        if lucky:
            r = max(r, _roll_reward(tier, rng), key=lambda x: x["worth"])
        out.append(r)
    return out


# ───────────── 인수 (3.8.1 — 명문구단처럼: 금액을 제안하면 주인이 수락 · 적정 인수 금액 이상이면 바로) ─────────────
# 농장은 여러 개 가질 수 있다 (인수해도 원래 내 농장은 그대로). 인수하면 코인 · 창고 · 밭 · 동물 · 낫 · 상자가 통째로 넘어온다.
FARM_FAIR = 3.0               # 적정 인수 금액 기본 = 농장 가치 × 3
FARM_ASK = (1.5, 5.0)         # 주인이 정할 수 있는 적정 인수 금액 범위 (농장 가치 ×)
FARM_OFFER_HOURS = 24         # 인수 제안은 24시간 뒤 만료
FARM_WORTH_MIN = 1_000        # 농장 가치 최소 (코인) — 막 연 농장도 적정 인수 금액이 너무 낮지 않게


def farm_worth(f: dict) -> int:
    """농장 가치 (코인): 코인 + 창고 + 사 둔 씨앗 · 동물 + 기르는 동물 + 넓힌 땅 + 낫 강화에 든 값 (최소 FARM_WORTH_MIN)."""
    w = f["coins"] + sum(value(k, t) * q for k, t, q in f["items"])
    w += sum((shop_price(k) or 0) * q for k, q in f["seeds"].items())
    w += sum(ANIMALS[p["key"]][2] for p in f["fields"] if p and p["kind"] == "animal")
    w += sum(expand_cost(p) for p in range(PLOTS_BASE, f["plots"]))
    w += sum(sickle_cost(lv) for lv in range(f["sickle"]))
    return max(FARM_WORTH_MIN, w)


def farm_ask_range(worth: int) -> tuple[int, int]:
    """주인이 정할 수 있는 적정 인수 금액 (원)."""
    return round(worth * RATE * FARM_ASK[0]), round(worth * RATE * FARM_ASK[1])


def farm_fair(worth: int, ask: Optional[int] = None) -> int:
    """적정 인수 금액 (원) — 주인이 정한 값(지금 가치의 범위 안으로), 안 정했으면 농장 가치 × FARM_FAIR."""
    if not ask:
        return round(worth * RATE * FARM_FAIR)
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
              "created_ts INTEGER NOT NULL, ask INTEGER)"),
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
            shop_cols = {r[1] for r in con.execute("PRAGMA table_info(farm_shops)")}
            if "msg_id" not in shop_cols:
                con.execute("ALTER TABLE farm_shops ADD COLUMN msg_id INTEGER")
            if "ver" not in shop_cols:
                con.execute("ALTER TABLE farm_shops ADD COLUMN ver INTEGER NOT NULL DEFAULT 0")
            con.execute("CREATE INDEX IF NOT EXISTS farms_owner ON farms(owner)")
            con.execute("CREATE INDEX IF NOT EXISTS farm_threads_tid ON farm_threads(thread_id)")
            con.execute("CREATE TABLE IF NOT EXISTS farm_offers (id INTEGER PRIMARY KEY AUTOINCREMENT, farm_id INTEGER NOT NULL, "
                        "buyer_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, amount INTEGER NOT NULL, ts INTEGER NOT NULL, "
                        "status TEXT NOT NULL DEFAULT 'open')")
            con.execute("CREATE INDEX IF NOT EXISTS farm_offers_farm ON farm_offers(farm_id, status)")
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
                        "daily_day, owner, ask FROM farms WHERE id=?", (int(fid),)).fetchone()
        if not r or r[0] not in LANDS:
            return None
        res = LANDS[r[0]][3]
        every = ENERGY_SEC * time_mult(res["water"])
        e = float(r[2])
        if e < ENERGY_MAX:
            e = min(float(ENERGY_MAX), e + max(0, now_ts - int(r[3])) / every)
        fields: list[Optional[dict]] = [None] * int(r[4])
        for slot, kind, key, ready in con.execute("SELECT slot, kind, key, ready_ts FROM farm_fields WHERE farm_id=?", (fid,)):
            if 0 <= slot < len(fields) and key in (CROPS if kind == "crop" else ANIMALS):
                fields[slot] = {"kind": kind, "key": key, "ready_ts": int(ready), "ready": now_ts >= int(ready)}
        items = [(k, int(t), int(q)) for k, t, q in con.execute(
            "SELECT key, tier, qty FROM farm_items WHERE farm_id=? AND qty>0", (fid,)) if k in ITEMS]
        order = list(ITEMS)
        items.sort(key=lambda x: (order.index(x[0]), x[1]))
        seeds = {k: int(q) for k, q in con.execute("SELECT key, qty FROM farm_seeds WHERE farm_id=? AND qty>0", (fid,))
                 if shop_price(k)}
        f = {"id": int(fid), "seeds": seeds, "owner": int(r[12]), "land": r[0], "res": res, "coins": int(r[1]), "energy_f": e, "energy": int(e),
             "energy_next": None if e >= ENERGY_MAX else int(now_ts + (1 - (e - int(e))) * every) + 1,
             "energy_every": every, "plots": int(r[4]), "sickle": int(r[5]), "whet": int(r[6]),
             "chests": {"wood": int(r[7]), "silver": int(r[8]), "gold": int(r[9])}, "plant_n": int(r[10]),
             "fields": fields, "items": items, "daily_day": int(r[11]), "ask": int(r[13]) if r[13] else None,
             "animals": sum(1 for p in fields if p and p["kind"] == "animal"), "animal_cap": int(r[4]) // ANIMAL_PER_PLOTS}
        f["worth"] = farm_worth(f)
        f["fair"] = farm_fair(f["worth"], f["ask"])
        return f

    @classmethod
    def _mine(cls, con, uid: int, fid: int, now_ts: int) -> Optional[dict]:
        """uid 가 주인인 농장 fid (아니면 None)."""
        f = cls._farm(con, fid, now_ts)
        return f if f and f["owner"] == int(uid) else None

    @staticmethod
    def _spend(con, fid: int, f: dict, n: int, now_ts: int):
        """에너지 n 을 쓴다 (지금까지 찬 만큼을 반영해서 저장)."""
        f["energy_f"] -= n
        f["energy"] = int(f["energy_f"])
        con.execute("UPDATE farms SET energy=?, energy_ts=? WHERE id=?", (f["energy_f"], int(now_ts), fid))

    @staticmethod
    def _give(con, fid: int, key: str, tier: int, n: int):
        con.execute("INSERT INTO farm_items(farm_id, key, tier, qty) VALUES(?,?,?,?) "
                    "ON CONFLICT(farm_id, key, tier) DO UPDATE SET qty = qty + excluded.qty", (fid, key, tier, n))

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
            cur = con.execute("INSERT INTO farms(owner, land, energy, energy_ts, plots, c_wood, created_ts) VALUES(?,?,?,?,?,?,?)",
                              (int(uid), land, ENERGY_MAX, int(now_ts), PLOTS_BASE, START_CHESTS.get("wood", 0), int(now_ts)))
            return {"ok": True, "id": int(cur.lastrowid)}
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
            if price is None:
                return {"ok": False, "reason": "key"}
            if n <= 0:
                return {"ok": False, "reason": "amount"}
            if f["coins"] < price * n:
                return {"ok": False, "reason": "coins", "cost": price * n, "have": f["coins"]}
            con.execute("UPDATE farms SET coins = coins - ? WHERE id=?", (price * n, fid))
            con.execute("INSERT INTO farm_seeds(farm_id, key, qty) VALUES(?,?,?) "
                        "ON CONFLICT(farm_id, key) DO UPDATE SET qty = qty + excluded.qty", (fid, key, n))
            return {"ok": True, "key": key, "count": n, "cost": price * n, "coins": f["coins"] - price * n,
                    "have": f["seeds"].get(key, 0) + n}
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
            if bought and have <= 0:
                return {"ok": False, "reason": "seeds"}
            empty = [i for i, p in enumerate(f["fields"]) if p is None]
            if not empty:
                return {"ok": False, "reason": "full"}
            if f["energy"] < 1:
                return {"ok": False, "reason": "energy"}
            animal = key in ANIMALS
            if animal:
                sec = ANIMALS[key][4] * time_mult(f["res"]["grass"])
                if f["animals"] >= f["animal_cap"]:
                    return {"ok": False, "reason": "cap", "cap": f["animal_cap"]}
                n = 1
            else:
                sec = CROPS[key][3] * time_mult(f["res"]["soil"])
                n = min(f["plant_n"] or len(empty), len(empty), f["energy"])
                if bought:
                    n = min(n, have)
            ready = int(now_ts + sec)
            for slot in empty[:n]:
                con.execute("INSERT OR REPLACE INTO farm_fields(farm_id, slot, kind, key, ready_ts) VALUES(?,?,?,?,?)",
                            (fid, slot, "animal" if animal else "crop", key, ready))
            if bought:
                con.execute("UPDATE farm_seeds SET qty = qty - ? WHERE farm_id=? AND key=?", (n, fid, key))
            self._spend(con, fid, f, n, now_ts)
            return {"ok": True, "key": key, "animal": animal, "count": n, "ready_ts": ready, "left": have - n if bought else None}
        return await self._tx(fn)

    # ───────────── 수확 · 모으기 ─────────────
    async def harvest(self, uid: int, fid: int, now_ts: int, rng=random) -> dict:
        """다 자란 작물 수확 + 동물이 만든 것 모으기 (에너지가 되는 만큼 · 하나에 1).
        낫: 하나 더 나올 확률 · ★2 로 나올 확률. 광맥: 상자가 나올 확률. 하루(KST) 첫 거두기엔 나무 상자 하나 (daily).
        → {got: {(key, tier): 수}, extra, high, chests: [tier], daily, count, left(에너지가 모자라 남은 수)}.
        reason: none / nothing / energy."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            ready = [(i, p) for i, p in enumerate(f["fields"]) if p and p["ready"]]
            if not ready:
                return {"ok": False, "reason": "nothing"}
            n = min(len(ready), f["energy"])
            if n <= 0:
                return {"ok": False, "reason": "energy"}
            got: dict = {}
            extra = high = 0
            chests = []
            for slot, p in ready[:n]:
                if p["kind"] == "crop":
                    key = p["key"]
                    con.execute("DELETE FROM farm_fields WHERE farm_id=? AND slot=?", (fid, slot))
                else:
                    key = ANIMALS[p["key"]][3]
                    con.execute("UPDATE farm_fields SET ready_ts=? WHERE farm_id=? AND slot=?",
                                (int(now_ts + ANIMALS[p["key"]][4] * time_mult(f["res"]["grass"])), fid, slot))
                tier = 2 if rng.random() < high_p(f["sickle"]) else 1
                qty = 2 if rng.random() < extra_p(f["sickle"]) else 1
                high += tier == 2
                extra += qty == 2
                got[(key, tier)] = got.get((key, tier), 0) + qty
                if rng.random() < chest_p(f["res"]["ore"]):
                    chests.append(rng.choices(list(CHESTS), weights=CHEST_WEIGHTS)[0])
            for (key, tier), qty in got.items():
                self._give(con, fid, key, tier, qty)
            daily = f["daily_day"] != _kst_day(now_ts)
            if daily:
                con.execute("UPDATE farms SET daily_day=? WHERE id=?", (_kst_day(now_ts), fid))
            for c in chests + ([DAILY_CHEST] if daily else []):
                con.execute(f"UPDATE farms SET c_{c} = c_{c} + 1 WHERE id=?", (fid,))
            self._spend(con, fid, f, n, now_ts)
            return {"ok": True, "got": got, "extra": extra, "high": high, "chests": chests, "daily": daily, "count": n,
                    "left": len(ready) - n}
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
            n = min(have // MERGE_NEED, f["energy"])
            if n <= 0:
                return {"ok": False, "reason": "energy"}
            con.execute("UPDATE farm_items SET qty = qty - ? WHERE farm_id=? AND key=? AND tier=?", (n * MERGE_NEED, fid, key, tier))
            self._give(con, fid, key, tier + 1, n)
            self._spend(con, fid, f, n, now_ts)
            return {"ok": True, "key": key, "tier": tier + 1, "made": n, "used": n * MERGE_NEED}
        return await self._tx(fn)

    async def sell(self, uid: int, fid: int, now_ts: int, picks: Optional[list[tuple[str, int]]] = None) -> dict:
        """창고의 물건을 코인으로 판다 (picks = [(key, tier)] · None 이면 전부). reason: none / empty."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            sold = [(k, t, q) for k, t, q in f["items"] if picks is None or (k, t) in picks]
            if not sold:
                return {"ok": False, "reason": "empty"}
            total = sum(value(k, t) * q for k, t, q in sold)
            for k, t, _ in sold:
                con.execute("DELETE FROM farm_items WHERE farm_id=? AND key=? AND tier=?", (fid, k, t))
            con.execute("UPDATE farms SET coins = coins + ? WHERE id=?", (total, fid))
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
            back = int(ANIMALS[p["key"]][2] * ANIMAL_REFUND)
            con.execute("DELETE FROM farm_fields WHERE farm_id=? AND slot=?", (fid, slot))
            con.execute("UPDATE farms SET coins = coins + ? WHERE id=?", (back, fid))
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
            con.execute(f"UPDATE farms SET coins = coins - ?, plots = plots + 1, c_{EXPAND_CHEST} = c_{EXPAND_CHEST} + 1 "
                        "WHERE id=?", (cost, fid))
            return {"ok": True, "plots": f["plots"] + 1, "cost": cost, "chest": EXPAND_CHEST}
        return await self._tx(fn)

    # ───────────── 낫 강화 ─────────────
    async def enhance(self, uid: int, fid: int, now_ts: int, use_whet: bool = False, charm: bool = False, rng=random) -> dict:
        """낫 강화 — 코인 또는 숫돌. charm = 🍀 행운 부적(축구 봇 돈)으로 성공 확률 +10%p. 실패해도 단계는 그대로.
        reason: none / max / coins(cost) / whet(need) / money(fee)."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            lv = f["sickle"]
            if lv >= SICKLE_MAX:
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
                con.execute("UPDATE farms SET coins = coins - ? WHERE id=?", (cost, fid))
            if charm:
                con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (fee, uid))
            p = min(1.0, SICKLE_CHANCE[lv] + (CHARM_BONUS if charm else 0))
            success = rng.random() < p
            if success:
                con.execute("UPDATE farms SET sickle = sickle + 1 WHERE id=?", (fid,))
            return {"ok": True, "success": success, "level": lv + success, "chance": p, "fee": fee if charm else 0}
        return await self._tx(fn)

    # ───────────── 상자 ─────────────
    async def open_chest(self, uid: int, fid: int, tier: str, now_ts: int, lucky: bool = False, rng=random) -> dict:
        """상자 하나 열기. lucky = 🍀 행운 열기(축구 봇 돈): 보상마다 두 번 뽑아 더 좋은 쪽.
        reason: none / tier / empty / money(fee)."""
        def fn(con):
            f = self._mine(con, uid, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
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
            rewards = roll_chest(tier, lucky, rng)
            for r in rewards:
                if r["kind"] == "coins":
                    con.execute("UPDATE farms SET coins = coins + ? WHERE id=?", (r["n"], fid))
                elif r["kind"] == "whet":
                    con.execute("UPDATE farms SET whet = whet + ? WHERE id=?", (r["n"], fid))
                elif r["kind"] == "energy":   # 상자 에너지는 한도를 넘겨서도 쌓인다
                    f["energy_f"] += r["n"]
                    con.execute("UPDATE farms SET energy=?, energy_ts=? WHERE id=?", (f["energy_f"], int(now_ts), fid))
                else:
                    self._give(con, fid, r["key"], r["tier"], r["n"])
            return {"ok": True, "tier": tier, "rewards": rewards, "fee": fee}
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
            self._wallet(con, uid)
            con.execute("UPDATE farms SET coins = coins - ? WHERE id=?", (coins, fid))
            con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (coins * RATE, uid))
            return {"ok": True, "coins": coins, "won": coins * RATE, "balance": self._wallet(con, uid), "left": f["coins"] - coins}
        return await self._tx(fn)

    # ───────────── 인수 ─────────────
    def _farm_transfer(self, con, fid: int, buyer: int, owner: int, amount: int) -> dict:
        """buyer 가 amount 를 내고 농장 fid 를 가져간다 (돈은 주인에게). 잔액이 모자라면 reason: balance."""
        bal = self._wallet(con, buyer)
        if bal < amount:
            return {"ok": False, "reason": "balance", "cost": amount}
        self._wallet(con, owner)
        con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (amount, buyer))
        con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (amount, owner))
        con.execute("UPDATE farms SET owner=?, ask=NULL WHERE id=?", (buyer, fid))
        con.execute("UPDATE farm_offers SET status='closed' WHERE farm_id=? AND status='open'", (fid,))   # 남은 제안 정리
        return {"ok": True, "id": fid, "cost": amount, "prev_owner": owner, "balance": bal - amount}

    async def offer_farm(self, buyer: int, fid: int, amount: int, now_ts: int) -> dict:
        """인수 제안. 적정 인수 금액(farm_fair) 이상이면 바로 인수 (auto=True), 아니면 주인에게 제안을 남긴다
        (같은 사람이 같은 농장에 다시 내면 새 제안으로 바뀐다 · 24시간 뒤 만료 · 돈은 수락될 때 빠진다).
        실패 reason: gone(농장 없음) / mine / amount / balance."""
        def fn(con):
            f = self._farm(con, fid, now_ts)
            if not f:
                return {"ok": False, "reason": "gone"}
            if f["owner"] == int(buyer):
                return {"ok": False, "reason": "mine"}
            if amount <= 0:
                return {"ok": False, "reason": "amount"}
            if self._wallet(con, int(buyer)) < amount:
                return {"ok": False, "reason": "balance", "cost": amount}
            if amount >= f["fair"]:
                return {**self._farm_transfer(con, fid, int(buyer), f["owner"], int(amount)), "auto": True, "fair": f["fair"]}
            con.execute("UPDATE farm_offers SET status='closed' WHERE farm_id=? AND buyer_id=? AND status='open'", (fid, int(buyer)))
            cur = con.execute("INSERT INTO farm_offers(farm_id, buyer_id, owner_id, amount, ts) VALUES(?,?,?,?,?)",
                              (fid, int(buyer), f["owner"], int(amount), int(now_ts)))
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
        수락할 때 제안한 사람의 잔액이 모자라면 balance (그 제안은 닫힌다)."""
        def fn(con):
            r = con.execute("SELECT farm_id, buyer_id, owner_id, amount, ts, status FROM farm_offers WHERE id=?",
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
            t = self._farm_transfer(con, fid, buyer, int(owner), amount)
            if not t["ok"]:
                con.execute("UPDATE farm_offers SET status='closed' WHERE id=?", (int(offer_id),))
                return {**t, **base, "ok": False}
            con.execute("UPDATE farm_offers SET status='accepted' WHERE id=?", (int(offer_id),))
            return {"ok": True, "accepted": True, **base, "balance": self._wallet(con, int(owner))}
        return await self._tx(fn)
