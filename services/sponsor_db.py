# services/sponsor_db.py — 스폰서 투자: 기간 계약 → 만기 정산(자동 재계약) / 중도 해지(수수료)
import asyncio
import random
from typing import Optional

from services.economy_db import EconomyDB

DAY = 86400

# 기간별 기본 수익률(만기 총수익). 짧은 계약을 복리로 굴려도 긴 계약보다 못하게 — 길게 묶을수록 유리하다.
# 2.2: 90일 · 365일 패키지는 새로 맺을 수 없다 (이미 맺은 계약의 정산용으로만 남긴다).
TERMS = {1: 0.003, 7: 0.025, 30: 0.12, 90: 0.45, 365: 4.00}
OPEN_TERMS = (1, 7, 30)

# 스폰서: key → (이모지, 이름, 성향, 실적 배율 하한, 상한, 소개)
# 만기 수익 = 원금 × 기본 수익률 × 실적 배율(하한~상한 균등 랜덤). 배율이 음수면 원금 손실(최대 원금 전액).
SPONSORS = {
    "bank":   ("🏦", "골든뱅크",          "안정형",  1.0, 1.0, "약속한 수익을 정확히 드립니다. 원금 보장!"),
    "boots":  ("👟", "스트라이크 스포츠", "균형형",  0.8, 1.4, "꾸준히 팔리는 축구화 브랜드"),
    "energy": ("🥤", "번개에너지",        "성장형",  0.4, 2.0, "요즘 뜨는 에너지 드링크"),
    "tv":     ("📺", "풋볼TV",            "도전형", -0.2, 2.8, "중계권 대박이냐 쪽박이냐"),
    "rocket": ("🚀", "로켓코인",          "투기형", -1.5, 4.3, "원금이 사라질 수도, 몇 배가 될 수도"),
}

# 스폰서 등급: 그 스폰서와 만기까지 채운 계약 일수 합(신뢰도)으로 오른다.
# (필요 신뢰도, 이름, 계약 한도, 수익 보너스) — 보너스는 플러스 수익에만 붙는다.
GRADES = [
    (0,    "🥉 신규",       50_000_000, 0.00),
    (30,   "🥈 파트너",    100_000_000, 0.05),
    (120,  "🥇 골드",      200_000_000, 0.10),
    (365,  "💎 플래티넘",  300_000_000, 0.15),
    (1000, "👑 VIP",       500_000_000, 0.25),
]
REP_MIN_AMOUNT = 1_000_000   # 이 금액 이상 계약만 신뢰도가 쌓인다 (1만원 계약으로 등급 올리기 방지)

# 구단 연계: 계약할 때의 구단 전력으로 수익 보너스 (계약에 고정)
CLUB_BONUS = [(80, 0.10), (70, 0.06), (60, 0.03)]

MIN_AMOUNT = 10_000
MAX_AMOUNT = GRADES[-1][2]   # 등급별 한도 중 최대 — 실제 한도는 등급이 정한다
MAX_ACTIVE = 5               # 동시에 진행 중인 계약 수
CANCEL_FEE = 0.05            # 중도 해지: 이자 없이 원금의 5% 수수료


def grade_of(rep: int) -> tuple[int, tuple]:
    """(등급 번호, GRADES 행)."""
    idx = max(i for i, g in enumerate(GRADES) if rep >= g[0])
    return idx, GRADES[idx]


def club_bonus(rating: Optional[float]) -> float:
    return next((b for need, b in CLUB_BONUS if rating is not None and rating >= need), 0.0)


def settle_amount(sponsor: str, days: int, amount: int, bonus: float = 0.0, rng=random) -> tuple[int, float]:
    """만기 지급액(원금 포함)과 실적 배율. bonus 는 플러스 수익에만 곱한다."""
    lo, hi = SPONSORS[sponsor][3:5]
    r = rng.uniform(lo, hi)
    gain = amount * TERMS[days] * r
    if gain > 0:
        gain *= 1 + bonus
    return amount + max(-amount, round(gain)), r


def cancel_refund(amount: int) -> int:
    return amount - int(amount * CANCEL_FEE)


_COLS = ("id", "user_id", "sponsor", "days", "amount", "start_ts", "end_ts", "club_bonus", "auto")


class SponsorDB(EconomyDB):
    def _init_db(self):
        super()._init_db()
        con = self._connect()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS sponsor_contracts (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id  INTEGER NOT NULL,
                    sponsor  TEXT    NOT NULL,
                    days     INTEGER NOT NULL,
                    amount   INTEGER NOT NULL,
                    start_ts INTEGER NOT NULL,
                    end_ts   INTEGER NOT NULL,
                    status   TEXT    NOT NULL DEFAULT 'active',   -- active / settled / cancelled
                    payout   INTEGER,
                    perf     REAL
                )
                """
            )
            cols = {r[1] for r in con.execute("PRAGMA table_info(sponsor_contracts)")}
            if "club_bonus" not in cols:
                con.execute("ALTER TABLE sponsor_contracts ADD COLUMN club_bonus REAL NOT NULL DEFAULT 0")
            if "auto" not in cols:
                con.execute("ALTER TABLE sponsor_contracts ADD COLUMN auto INTEGER NOT NULL DEFAULT 0")
            con.execute("CREATE INDEX IF NOT EXISTS idx_sponsor_user ON sponsor_contracts(user_id, status)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_sponsor_due ON sponsor_contracts(status, end_ts)")
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

    @staticmethod
    def _balance(con, user_id: int) -> int:
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (user_id,))
        return int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,)).fetchone()[0])

    @staticmethod
    def _rep(con, user_id: int) -> dict[str, int]:
        rows = con.execute("SELECT sponsor, SUM(days) FROM sponsor_contracts WHERE user_id=? AND status='settled' "
                           "AND amount>=? GROUP BY sponsor", (user_id, REP_MIN_AMOUNT)).fetchall()
        return {s: int(n) for s, n in rows}

    @staticmethod
    def _insert(con, user_id, sponsor, days, amount, now_ts, club_b, auto) -> int:
        con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (amount, user_id))
        return con.execute(
            "INSERT INTO sponsor_contracts(user_id, sponsor, days, amount, start_ts, end_ts, club_bonus, auto) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (user_id, sponsor, days, amount, now_ts, now_ts + days * DAY, club_b, int(auto)),
        ).lastrowid

    async def reputation(self, user_id: int) -> dict[str, int]:
        return await self._tx(lambda con: self._rep(con, user_id))

    async def open(self, user_id: int, sponsor: str, days: int, amount: int, now_ts: int,
                   club_b: float = 0.0, auto: bool = False) -> dict:
        """계약 체결: 잔액에서 원금을 빼고 계약을 만든다. 실패 시 reason: full / limit / balance."""
        def fn(con):
            bal = self._balance(con, user_id)
            rep = self._rep(con, user_id).get(sponsor, 0)
            _, (_, gname, limit, gbonus) = grade_of(rep)
            active = con.execute("SELECT COUNT(*) FROM sponsor_contracts WHERE user_id=? AND status='active'",
                                 (user_id,)).fetchone()[0]
            if active >= MAX_ACTIVE:
                return {"ok": False, "reason": "full"}
            if amount > limit:
                return {"ok": False, "reason": "limit", "grade": gname, "limit": limit}
            if bal < amount:
                return {"ok": False, "reason": "balance", "balance": bal}
            cid = self._insert(con, user_id, sponsor, days, amount, now_ts, club_b, auto)
            return {"ok": True, "id": cid, "end_ts": now_ts + days * DAY, "balance": bal - amount,
                    "grade": gname, "grade_bonus": gbonus, "club_bonus": club_b}
        return await self._tx(fn)

    async def active(self, user_id: int) -> list[dict]:
        def fn(con):
            rows = con.execute(f"SELECT {','.join(_COLS)} FROM sponsor_contracts "
                               "WHERE user_id=? AND status='active' ORDER BY end_ts", (user_id,)).fetchall()
            return [dict(zip(_COLS, r)) for r in rows]
        return await self._tx(fn)

    async def settle(self, now_ts: int, user_id: Optional[int] = None, rng=random) -> dict[int, dict]:
        """만기가 지난 계약을 정산해 지급한다 (user_id=None 이면 전체 유저).
        자동 재계약이 켜진 계약은 지급액에서 원금(최대 이전 원금 · 등급 한도)을 떼어 같은 조건으로 다시 맺는다.
        반환: {user_id: {"done": [정산 목록], "balance": 새 잔액}}"""
        def fn(con):
            q = f"SELECT {','.join(_COLS)} FROM sponsor_contracts WHERE status='active' AND end_ts<=?"
            args = [now_ts]
            if user_id is not None:
                q += " AND user_id=?"
                args.append(user_id)
            out: dict[int, dict] = {}
            for row in con.execute(q + " ORDER BY end_ts", args).fetchall():
                c = dict(zip(_COLS, row))
                uid, sponsor = c["user_id"], c["sponsor"]
                gidx, (_, gname, limit, gbonus) = grade_of(self._rep(con, uid).get(sponsor, 0))
                payout, perf = settle_amount(sponsor, c["days"], c["amount"], gbonus + c["club_bonus"], rng)
                con.execute("UPDATE sponsor_contracts SET status='settled', payout=?, perf=? WHERE id=?",
                            (payout, perf, c["id"]))
                con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (payout, uid))
                new_gidx, (_, new_gname, new_limit, _) = grade_of(self._rep(con, uid).get(sponsor, 0))
                renewed = None
                if c["auto"] and c["days"] in OPEN_TERMS:   # 없어진 기간(90·365일)은 재계약하지 않는다
                    amt = min(c["amount"], payout, new_limit)
                    if amt >= MIN_AMOUNT:
                        renewed = self._insert(con, uid, sponsor, c["days"], amt, now_ts, c["club_bonus"], True)
                out.setdefault(uid, {"done": []})["done"].append(
                    {**c, "payout": payout, "perf": perf, "bonus": gbonus + c["club_bonus"],
                     "renewed": renewed, "renew_amount": amt if renewed else 0,
                     "grade_up": new_gname if new_gidx > gidx else None})
            for uid, v in out.items():
                v["balance"] = self._balance(con, uid)
            return out
        return await self._tx(fn)

    async def set_auto(self, user_id: int, cid: int, on: bool) -> bool:
        def fn(con):
            return con.execute("UPDATE sponsor_contracts SET auto=? WHERE id=? AND user_id=? AND status='active'",
                               (int(on), cid, user_id)).rowcount > 0
        return await self._tx(fn)

    async def cancel(self, user_id: int, cid: int, now_ts: int) -> dict:
        """중도 해지: 원금에서 수수료를 떼고 돌려준다. 이미 만기면 reason=matured (정산으로 처리)."""
        def fn(con):
            row = con.execute("SELECT sponsor, days, amount, end_ts FROM sponsor_contracts "
                              "WHERE id=? AND user_id=? AND status='active'", (cid, user_id)).fetchone()
            if not row:
                return {"ok": False, "reason": "missing"}
            sponsor, days, amount, end_ts = row
            if end_ts <= now_ts:
                return {"ok": False, "reason": "matured"}
            refund = cancel_refund(amount)
            con.execute("UPDATE sponsor_contracts SET status='cancelled', payout=? WHERE id=?", (refund, cid))
            con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (refund, user_id))
            return {"ok": True, "sponsor": sponsor, "days": days, "amount": amount, "refund": refund,
                    "balance": self._balance(con, user_id)}
        return await self._tx(fn)
