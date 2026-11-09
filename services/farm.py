# services/farm.py — 🌱 /농사 (3.8.1, 테스트 서버 먼저): 밭에 씨앗을 심고, 시간이 지나면 수확해서 판다
from typing import Optional

from services.economy_db import EconomyDB

PLOTS = 6   # 밭 칸 수
# 작물 키 → (이모지, 이름, 씨앗 값, 자라는 시간(초), 수확하면 받는 돈)
CROPS = {
    "carrot": ("🥕", "당근", 10_000, 30 * 60, 15_000),
    "potato": ("🥔", "감자", 30_000, 60 * 60, 48_000),
    "corn": ("🌽", "옥수수", 100_000, 3 * 3600, 170_000),
    "tomato": ("🍅", "토마토", 300_000, 6 * 3600, 540_000),
    "strawberry": ("🍓", "딸기", 1_000_000, 12 * 3600, 1_900_000),
    "watermelon": ("🍉", "수박", 3_000_000, 24 * 3600, 6_000_000),
}


class FarmDB(EconomyDB):
    """밭 데이터 — 지갑과 같은 DB 라서 씨앗 값 · 수확 돈이 한 트랜잭션으로 끝난다."""

    def _init_db(self):
        super()._init_db()
        con = self._connect()
        try:
            con.execute("CREATE TABLE IF NOT EXISTS farm_plots (user_id INTEGER NOT NULL, slot INTEGER NOT NULL, "
                        "crop TEXT NOT NULL, planted_ts INTEGER NOT NULL, PRIMARY KEY(user_id, slot))")
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _plots(con, uid: int, now_ts: int) -> list[Optional[dict]]:
        out: list[Optional[dict]] = [None] * PLOTS
        for slot, crop, ts in con.execute("SELECT slot, crop, planted_ts FROM farm_plots WHERE user_id=?", (uid,)):
            if 0 <= slot < PLOTS and crop in CROPS:
                ready_ts = int(ts) + CROPS[crop][3]
                out[slot] = {"crop": crop, "planted": int(ts), "ready_ts": ready_ts, "ready": now_ts >= ready_ts}
        return out

    @staticmethod
    def _bal(con, uid: int) -> int:
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (uid,))
        return int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (uid,)).fetchone()[0])

    async def field(self, uid: int, now_ts: int) -> dict:
        def fn(con):
            return {"plots": self._plots(con, uid, now_ts), "balance": self._bal(con, uid)}
        return await self._tx(fn)

    async def plant(self, uid: int, crop: str, now_ts: int, all_empty: bool = False) -> dict:
        """빈 밭 한 칸(all_empty 면 빈 밭 모두)에 씨앗을 심는다. reason: crop / full / balance."""
        def fn(con):
            if crop not in CROPS:
                return {"ok": False, "reason": "crop"}
            empty = [i for i, p in enumerate(self._plots(con, uid, now_ts)) if p is None]
            if not empty:
                return {"ok": False, "reason": "full"}
            bal = self._bal(con, uid)
            price = CROPS[crop][2]
            n = min(len(empty), bal // price if price else len(empty)) if all_empty else 1
            if n <= 0 or bal < price * n:
                return {"ok": False, "reason": "balance", "price": price, "balance": bal}
            for slot in empty[:n]:
                con.execute("INSERT OR REPLACE INTO farm_plots(user_id, slot, crop, planted_ts) VALUES(?,?,?,?)",
                            (uid, slot, crop, now_ts))
            con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (price * n, uid))
            return {"ok": True, "crop": crop, "count": n, "cost": price * n, "balance": bal - price * n}
        return await self._tx(fn)

    async def harvest(self, uid: int, now_ts: int) -> dict:
        """다 자란 작물을 모두 수확해서 판다. → {ok, crops: [(crop, 받은 돈)], total, balance}. reason: none."""
        def fn(con):
            got = []
            for slot, p in enumerate(self._plots(con, uid, now_ts)):
                if p and p["ready"]:
                    got.append((p["crop"], CROPS[p["crop"]][4]))
                    con.execute("DELETE FROM farm_plots WHERE user_id=? AND slot=?", (uid, slot))
            if not got:
                return {"ok": False, "reason": "none"}
            total = sum(x[1] for x in got)
            self._bal(con, uid)
            con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (total, uid))
            return {"ok": True, "crops": got, "total": total, "balance": self._bal(con, uid)}
        return await self._tx(fn)
