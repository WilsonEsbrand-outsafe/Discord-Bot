# services/farm.py — 🌾 /농사 (3.8.1, 테스트 서버 먼저)
# 시작할 때 땅 하나를 고른다 (땅마다 세부 자원이 다르다) → 작물을 심고 동물을 기른다 → 수확물을 합쳐 등급을 올리거나 판다.
# 행동(심기 · 수확 · 모으기 · 합치기)마다 ⚡ 에너지 1. 버는 것은 🪙 농장 코인 — 씨앗 · 확장 · 낫 강화에 쓰고, 원하면 축구 봇 돈으로 환전.
# 축구 봇 돈이 0원이어도 다 할 수 있다 (당근 씨앗은 무료). 돈은 운 요소(상자 행운 열기 · 강화 행운 부적)에만 쓴다.
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
# 작물: key → (이모지, 이름, 씨앗 값(코인), 자라는 시간(초), ★1 판매가(코인)) — 당근 씨앗은 무료라서 코인이 0 이어도 막히지 않는다
CROPS = {
    "carrot": ("🥕", "당근", 0, 5 * 60, 4),
    "potato": ("🥔", "감자", 5, 20 * 60, 14),
    "corn": ("🌽", "옥수수", 20, 60 * 60, 45),
    "tomato": ("🍅", "토마토", 60, 3 * 3600, 120),
    "strawberry": ("🍓", "딸기", 150, 8 * 3600, 280),
    "watermelon": ("🍉", "수박", 400, 20 * 3600, 700),
}
# 동물: key → (이모지, 이름, 입양 값(코인), 만드는 물건, 만드는 시간(초)) — 땅 한 칸을 차지하고 계속 만든다
ANIMALS = {
    "chicken": ("🐔", "닭", 300, "egg", 60 * 60),
    "sheep": ("🐑", "양", 1_200, "wool", 4 * 3600),
    "cow": ("🐄", "소", 4_000, "milk", 10 * 3600),
}
PRODUCTS = {"egg": ("🥚", "달걀", 30), "wool": ("🧶", "양털", 110), "milk": ("🥛", "우유", 260)}
ITEMS = {**{k: (e, n, v) for k, (e, n, _, _, v) in CROPS.items()}, **PRODUCTS}   # 창고에 들어가는 것: key → (이모지, 이름, ★1 판매가)
ANIMAL_PER_PLOTS = 3          # 동물은 땅 3칸당 1마리
ANIMAL_REFUND = 0.5           # 동물을 내보내면 입양 값의 절반

# 합치기: 같은 물건 · 같은 등급 3개 → 한 등급 위 1개 (★5 까지). 등급이 오를수록 3개 값보다 조금씩 더 비싸다
TIER_MAX, MERGE_NEED = 5, 3
TIER_MULT = (1, 3.5, 12.5, 45, 165)


def value(key: str, tier: int) -> int:
    return round(ITEMS[key][2] * TIER_MULT[tier - 1])


# ───────────── 에너지 · 땅 확장 ─────────────
ENERGY_MAX, ENERGY_SEC = 100, 120     # 100 까지 · 2분에 1 (물 별에 따라 빨라진다)
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


# PR #118 의 첫 /농사 (밭 6칸, 축구 봇 돈으로 씨앗) — 새 시스템으로 바꾸면서 심어 둔 씨앗 값을 돌려준다
_OLD_SEED = {"carrot": 10_000, "potato": 30_000, "corn": 100_000, "tomato": 300_000, "strawberry": 1_000_000,
             "watermelon": 3_000_000}


class FarmDB(EconomyDB):
    """농장 데이터 — 지갑과 같은 DB 라서 환전 · 행운 값이 한 트랜잭션으로 끝난다."""

    def _init_db(self):
        super()._init_db()
        con = self._connect()
        try:
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='farm_plots'").fetchone():
                for uid, crop in con.execute("SELECT user_id, crop FROM farm_plots").fetchall():
                    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (uid,))
                    con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (_OLD_SEED.get(crop, 0), uid))
                con.execute("DROP TABLE farm_plots")
            con.execute("CREATE TABLE IF NOT EXISTS farms (user_id INTEGER PRIMARY KEY, land TEXT NOT NULL, "
                        "coins INTEGER NOT NULL DEFAULT 0, energy REAL NOT NULL, energy_ts INTEGER NOT NULL, "
                        "plots INTEGER NOT NULL, sickle INTEGER NOT NULL DEFAULT 0, whet INTEGER NOT NULL DEFAULT 0, "
                        "c_wood INTEGER NOT NULL DEFAULT 0, c_silver INTEGER NOT NULL DEFAULT 0, "
                        "c_gold INTEGER NOT NULL DEFAULT 0, plant_n INTEGER NOT NULL DEFAULT 0, daily_day INTEGER NOT NULL DEFAULT 0, "
                        "created_ts INTEGER NOT NULL)")
            if "daily_day" not in {r[1] for r in con.execute("PRAGMA table_info(farms)")}:
                con.execute("ALTER TABLE farms ADD COLUMN daily_day INTEGER NOT NULL DEFAULT 0")
            con.execute("CREATE TABLE IF NOT EXISTS farm_fields (user_id INTEGER NOT NULL, slot INTEGER NOT NULL, "
                        "kind TEXT NOT NULL, key TEXT NOT NULL, ready_ts INTEGER NOT NULL, PRIMARY KEY(user_id, slot))")
            con.execute("CREATE TABLE IF NOT EXISTS farm_items (user_id INTEGER NOT NULL, key TEXT NOT NULL, "
                        "tier INTEGER NOT NULL, qty INTEGER NOT NULL, PRIMARY KEY(user_id, key, tier))")
            con.execute("CREATE TABLE IF NOT EXISTS farm_threads (user_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, "
                        "thread_id INTEGER NOT NULL, PRIMARY KEY(user_id, guild_id))")
            con.execute("CREATE INDEX IF NOT EXISTS farm_threads_tid ON farm_threads(thread_id)")
            con.commit()
        finally:
            con.close()

    # ───────────── 읽기 ─────────────
    @staticmethod
    def _farm(con, uid: int, now_ts: int) -> Optional[dict]:
        r = con.execute("SELECT land, coins, energy, energy_ts, plots, sickle, whet, c_wood, c_silver, c_gold, plant_n, "
                        "daily_day FROM farms WHERE user_id=?", (uid,)).fetchone()
        if not r or r[0] not in LANDS:
            return None
        res = LANDS[r[0]][3]
        every = ENERGY_SEC * time_mult(res["water"])
        e = float(r[2])
        if e < ENERGY_MAX:
            e = min(float(ENERGY_MAX), e + max(0, now_ts - int(r[3])) / every)
        fields: list[Optional[dict]] = [None] * int(r[4])
        for slot, kind, key, ready in con.execute("SELECT slot, kind, key, ready_ts FROM farm_fields WHERE user_id=?", (uid,)):
            if 0 <= slot < len(fields) and key in (CROPS if kind == "crop" else ANIMALS):
                fields[slot] = {"kind": kind, "key": key, "ready_ts": int(ready), "ready": now_ts >= int(ready)}
        items = [(k, int(t), int(q)) for k, t, q in con.execute(
            "SELECT key, tier, qty FROM farm_items WHERE user_id=? AND qty>0", (uid,)) if k in ITEMS]
        order = list(ITEMS)
        items.sort(key=lambda x: (order.index(x[0]), x[1]))
        return {"land": r[0], "res": res, "coins": int(r[1]), "energy_f": e, "energy": int(e),
                "energy_next": None if e >= ENERGY_MAX else int(now_ts + (1 - (e - int(e))) * every) + 1,
                "energy_every": every, "plots": int(r[4]), "sickle": int(r[5]), "whet": int(r[6]),
                "chests": {"wood": int(r[7]), "silver": int(r[8]), "gold": int(r[9])}, "plant_n": int(r[10]),
                "fields": fields, "items": items, "daily_day": int(r[11]),
                "animals": sum(1 for p in fields if p and p["kind"] == "animal"), "animal_cap": int(r[4]) // ANIMAL_PER_PLOTS}

    @staticmethod
    def _spend(con, uid: int, f: dict, n: int, now_ts: int):
        """에너지 n 을 쓴다 (지금까지 찬 만큼을 반영해서 저장)."""
        f["energy_f"] -= n
        f["energy"] = int(f["energy_f"])
        con.execute("UPDATE farms SET energy=?, energy_ts=? WHERE user_id=?", (f["energy_f"], int(now_ts), uid))

    @staticmethod
    def _give(con, uid: int, key: str, tier: int, n: int):
        con.execute("INSERT INTO farm_items(user_id, key, tier, qty) VALUES(?,?,?,?) "
                    "ON CONFLICT(user_id, key, tier) DO UPDATE SET qty = qty + excluded.qty", (uid, key, tier, n))

    @staticmethod
    def _wallet(con, uid: int) -> int:
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (uid,))
        return int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (uid,)).fetchone()[0])

    async def get(self, uid: int, now_ts: int) -> Optional[dict]:
        """농장 상태 (없으면 None). balance = 축구 봇 돈."""
        def fn(con):
            f = self._farm(con, uid, now_ts)
            if f:
                f["balance"] = self._wallet(con, uid)
            return f
        return await self._tx(fn)

    async def create(self, uid: int, land: str, now_ts: int) -> dict:
        """땅을 골라 농장을 연다. reason: land / exists."""
        def fn(con):
            if land not in LANDS:
                return {"ok": False, "reason": "land"}
            if con.execute("SELECT 1 FROM farms WHERE user_id=?", (uid,)).fetchone():
                return {"ok": False, "reason": "exists"}
            con.execute("INSERT INTO farms(user_id, land, energy, energy_ts, plots, c_wood, created_ts) VALUES(?,?,?,?,?,?,?)",
                        (uid, land, ENERGY_MAX, int(now_ts), PLOTS_BASE, START_CHESTS.get("wood", 0), int(now_ts)))
            return {"ok": True}
        return await self._tx(fn)

    # ───────────── 스레드 ─────────────
    async def set_thread(self, uid: int, guild_id: int, thread_id: int):
        await self._tx(lambda con: con.execute(
            "INSERT OR REPLACE INTO farm_threads(user_id, guild_id, thread_id) VALUES(?,?,?)", (uid, guild_id, thread_id)))

    async def get_thread(self, uid: int, guild_id: int) -> Optional[int]:
        def fn(con):
            r = con.execute("SELECT thread_id FROM farm_threads WHERE user_id=? AND guild_id=?", (uid, guild_id)).fetchone()
            return int(r[0]) if r else None
        return await self._tx(fn)

    async def thread_owner(self, thread_id: int) -> Optional[int]:
        def fn(con):
            r = con.execute("SELECT user_id FROM farm_threads WHERE thread_id=?", (thread_id,)).fetchone()
            return int(r[0]) if r else None
        return await self._tx(fn)

    # ───────────── 심기 · 입양 ─────────────
    async def set_plant_n(self, uid: int, n: int):
        """한 번에 심을 칸 수 (0 = 빈 땅 모두)."""
        await self._tx(lambda con: con.execute("UPDATE farms SET plant_n=? WHERE user_id=?", (max(0, int(n)), uid)))

    async def plant(self, uid: int, key: str, now_ts: int) -> dict:
        """작물 심기 (plant_n 칸 · 에너지와 코인이 되는 만큼) 또는 동물 입양 (한 마리). 한 칸에 에너지 1.
        reason: none / key / full / energy / coins(price) / cap(동물 한도)."""
        def fn(con):
            f = self._farm(con, uid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            if key not in CROPS and key not in ANIMALS:
                return {"ok": False, "reason": "key"}
            empty = [i for i, p in enumerate(f["fields"]) if p is None]
            if not empty:
                return {"ok": False, "reason": "full"}
            if f["energy"] < 1:
                return {"ok": False, "reason": "energy"}
            animal = key in ANIMALS
            if animal:
                price, sec = ANIMALS[key][2], ANIMALS[key][4] * time_mult(f["res"]["grass"])
                if f["animals"] >= f["animal_cap"]:
                    return {"ok": False, "reason": "cap", "cap": f["animal_cap"]}
                n = 1
            else:
                price, sec = CROPS[key][2], CROPS[key][3] * time_mult(f["res"]["soil"])
                n = min(f["plant_n"] or len(empty), len(empty), f["energy"])
                if price:
                    n = min(n, f["coins"] // price)
            if n <= 0 or f["coins"] < price * n:
                return {"ok": False, "reason": "coins", "price": price}
            ready = int(now_ts + sec)
            for slot in empty[:n]:
                con.execute("INSERT OR REPLACE INTO farm_fields(user_id, slot, kind, key, ready_ts) VALUES(?,?,?,?,?)",
                            (uid, slot, "animal" if animal else "crop", key, ready))
            con.execute("UPDATE farms SET coins = coins - ? WHERE user_id=?", (price * n, uid))
            self._spend(con, uid, f, n, now_ts)
            return {"ok": True, "key": key, "animal": animal, "count": n, "cost": price * n, "ready_ts": ready}
        return await self._tx(fn)

    # ───────────── 수확 · 모으기 ─────────────
    async def harvest(self, uid: int, now_ts: int, rng=random) -> dict:
        """다 자란 작물 수확 + 동물이 만든 것 모으기 (에너지가 되는 만큼 · 하나에 1).
        낫: 하나 더 나올 확률 · ★2 로 나올 확률. 광맥: 상자가 나올 확률. 하루(KST) 첫 거두기엔 나무 상자 하나 (daily).
        → {got: {(key, tier): 수}, extra, high, chests: [tier], daily, count, left(에너지가 모자라 남은 수)}.
        reason: none / nothing / energy."""
        def fn(con):
            f = self._farm(con, uid, now_ts)
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
                    con.execute("DELETE FROM farm_fields WHERE user_id=? AND slot=?", (uid, slot))
                else:
                    key = ANIMALS[p["key"]][3]
                    con.execute("UPDATE farm_fields SET ready_ts=? WHERE user_id=? AND slot=?",
                                (int(now_ts + ANIMALS[p["key"]][4] * time_mult(f["res"]["grass"])), uid, slot))
                tier = 2 if rng.random() < high_p(f["sickle"]) else 1
                qty = 2 if rng.random() < extra_p(f["sickle"]) else 1
                high += tier == 2
                extra += qty == 2
                got[(key, tier)] = got.get((key, tier), 0) + qty
                if rng.random() < chest_p(f["res"]["ore"]):
                    chests.append(rng.choices(list(CHESTS), weights=CHEST_WEIGHTS)[0])
            for (key, tier), qty in got.items():
                self._give(con, uid, key, tier, qty)
            daily = f["daily_day"] != _kst_day(now_ts)
            if daily:
                con.execute("UPDATE farms SET daily_day=? WHERE user_id=?", (_kst_day(now_ts), uid))
            for c in chests + ([DAILY_CHEST] if daily else []):
                con.execute(f"UPDATE farms SET c_{c} = c_{c} + 1 WHERE user_id=?", (uid,))
            self._spend(con, uid, f, n, now_ts)
            return {"ok": True, "got": got, "extra": extra, "high": high, "chests": chests, "daily": daily, "count": n,
                    "left": len(ready) - n}
        return await self._tx(fn)

    # ───────────── 합치기 · 판매 ─────────────
    async def merge(self, uid: int, key: str, tier: int, now_ts: int) -> dict:
        """같은 물건 3개 → 한 등급 위 1개 (되는 만큼 · 한 번에 에너지 1). reason: none / max / few / energy."""
        def fn(con):
            f = self._farm(con, uid, now_ts)
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
            con.execute("UPDATE farm_items SET qty = qty - ? WHERE user_id=? AND key=? AND tier=?", (n * MERGE_NEED, uid, key, tier))
            self._give(con, uid, key, tier + 1, n)
            self._spend(con, uid, f, n, now_ts)
            return {"ok": True, "key": key, "tier": tier + 1, "made": n, "used": n * MERGE_NEED}
        return await self._tx(fn)

    async def sell(self, uid: int, now_ts: int, picks: Optional[list[tuple[str, int]]] = None) -> dict:
        """창고의 물건을 코인으로 판다 (picks = [(key, tier)] · None 이면 전부). reason: none / empty."""
        def fn(con):
            f = self._farm(con, uid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            sold = [(k, t, q) for k, t, q in f["items"] if picks is None or (k, t) in picks]
            if not sold:
                return {"ok": False, "reason": "empty"}
            total = sum(value(k, t) * q for k, t, q in sold)
            for k, t, _ in sold:
                con.execute("DELETE FROM farm_items WHERE user_id=? AND key=? AND tier=?", (uid, k, t))
            con.execute("UPDATE farms SET coins = coins + ? WHERE user_id=?", (total, uid))
            return {"ok": True, "sold": sold, "coins": total, "total": f["coins"] + total}
        return await self._tx(fn)

    async def sell_animal(self, uid: int, slot: int, now_ts: int) -> dict:
        """동물을 내보낸다 — 입양 값의 절반을 돌려받고 땅이 빈다. reason: none / slot."""
        def fn(con):
            f = self._farm(con, uid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            p = f["fields"][slot] if 0 <= slot < len(f["fields"]) else None
            if not p or p["kind"] != "animal":
                return {"ok": False, "reason": "slot"}
            back = int(ANIMALS[p["key"]][2] * ANIMAL_REFUND)
            con.execute("DELETE FROM farm_fields WHERE user_id=? AND slot=?", (uid, slot))
            con.execute("UPDATE farms SET coins = coins + ? WHERE user_id=?", (back, uid))
            return {"ok": True, "key": p["key"], "coins": back}
        return await self._tx(fn)

    # ───────────── 땅 확장 ─────────────
    async def expand(self, uid: int, now_ts: int) -> dict:
        """땅 한 칸 확장 (코인) + 은 상자 하나. reason: none / max / coins(cost)."""
        def fn(con):
            f = self._farm(con, uid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            if f["plots"] >= PLOTS_MAX:
                return {"ok": False, "reason": "max"}
            cost = expand_cost(f["plots"])
            if f["coins"] < cost:
                return {"ok": False, "reason": "coins", "cost": cost}
            con.execute(f"UPDATE farms SET coins = coins - ?, plots = plots + 1, c_{EXPAND_CHEST} = c_{EXPAND_CHEST} + 1 "
                        "WHERE user_id=?", (cost, uid))
            return {"ok": True, "plots": f["plots"] + 1, "cost": cost, "chest": EXPAND_CHEST}
        return await self._tx(fn)

    # ───────────── 낫 강화 ─────────────
    async def enhance(self, uid: int, now_ts: int, use_whet: bool = False, charm: bool = False, rng=random) -> dict:
        """낫 강화 — 코인 또는 숫돌. charm = 🍀 행운 부적(축구 봇 돈)으로 성공 확률 +10%p. 실패해도 단계는 그대로.
        reason: none / max / coins(cost) / whet(need) / money(fee)."""
        def fn(con):
            f = self._farm(con, uid, now_ts)
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
                con.execute("UPDATE farms SET whet = whet - ? WHERE user_id=?", (need, uid))
            else:
                con.execute("UPDATE farms SET coins = coins - ? WHERE user_id=?", (cost, uid))
            if charm:
                con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (fee, uid))
            p = min(1.0, SICKLE_CHANCE[lv] + (CHARM_BONUS if charm else 0))
            success = rng.random() < p
            if success:
                con.execute("UPDATE farms SET sickle = sickle + 1 WHERE user_id=?", (uid,))
            return {"ok": True, "success": success, "level": lv + success, "chance": p, "fee": fee if charm else 0}
        return await self._tx(fn)

    # ───────────── 상자 ─────────────
    async def open_chest(self, uid: int, tier: str, now_ts: int, lucky: bool = False, rng=random) -> dict:
        """상자 하나 열기. lucky = 🍀 행운 열기(축구 봇 돈): 보상마다 두 번 뽑아 더 좋은 쪽.
        reason: none / tier / empty / money(fee)."""
        def fn(con):
            f = self._farm(con, uid, now_ts)
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
            con.execute(f"UPDATE farms SET c_{tier} = c_{tier} - 1 WHERE user_id=?", (uid,))
            rewards = roll_chest(tier, lucky, rng)
            for r in rewards:
                if r["kind"] == "coins":
                    con.execute("UPDATE farms SET coins = coins + ? WHERE user_id=?", (r["n"], uid))
                elif r["kind"] == "whet":
                    con.execute("UPDATE farms SET whet = whet + ? WHERE user_id=?", (r["n"], uid))
                elif r["kind"] == "energy":   # 상자 에너지는 한도를 넘겨서도 쌓인다
                    f["energy_f"] += r["n"]
                    con.execute("UPDATE farms SET energy=?, energy_ts=? WHERE user_id=?", (f["energy_f"], int(now_ts), uid))
                else:
                    self._give(con, uid, r["key"], r["tier"], r["n"])
            return {"ok": True, "tier": tier, "rewards": rewards, "fee": fee}
        return await self._tx(fn)

    # ───────────── 환전 ─────────────
    async def exchange(self, uid: int, coins: int, now_ts: int) -> dict:
        """농장 코인 → 축구 봇 돈 (코인 1 = RATE 원). reason: none / amount / coins."""
        def fn(con):
            f = self._farm(con, uid, now_ts)
            if not f:
                return {"ok": False, "reason": "none"}
            if coins <= 0:
                return {"ok": False, "reason": "amount"}
            if f["coins"] < coins:
                return {"ok": False, "reason": "coins", "have": f["coins"]}
            self._wallet(con, uid)
            con.execute("UPDATE farms SET coins = coins - ? WHERE user_id=?", (coins, uid))
            con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (coins * RATE, uid))
            return {"ok": True, "coins": coins, "won": coins * RATE, "balance": self._wallet(con, uid), "left": f["coins"] - coins}
        return await self._tx(fn)
