# services/sponsor_db.py — 스폰서 투자: 기간 계약 → 만기 정산 / 중도 해지(수수료)
import asyncio
import random
from typing import Optional

from services.economy_db import EconomyDB

DAY = 86400

# 기간별 기본 수익률(만기 총수익). 짧은 계약을 복리로 굴려도 긴 계약보다 못하게 — 길게 묶을수록 유리하다.
TERMS = {1: 0.003, 7: 0.025, 30: 0.12, 90: 0.45, 365: 4.00}

# 스폰서: key → (이모지, 이름, 성향, 실적 배율 하한, 상한, 소개)
# 만기 수익 = 원금 × 기본 수익률 × 실적 배율(하한~상한 균등 랜덤). 배율이 음수면 원금 손실(최대 원금 전액).
SPONSORS = {
    "bank":   ("🏦", "골든뱅크",          "안정형",  1.0, 1.0, "약속한 수익을 정확히 드립니다. 원금 보장!"),
    "boots":  ("👟", "스트라이크 스포츠", "균형형",  0.8, 1.4, "꾸준히 팔리는 축구화 브랜드"),
    "energy": ("🥤", "번개에너지",        "성장형",  0.4, 2.0, "요즘 뜨는 에너지 드링크"),
    "tv":     ("📺", "풋볼TV",            "도전형", -0.2, 2.8, "중계권 대박이냐 쪽박이냐"),
    "rocket": ("🚀", "로켓코인",          "투기형", -1.5, 4.3, "원금이 사라질 수도, 몇 배가 될 수도"),
}

MIN_AMOUNT = 10_000
MAX_AMOUNT = 10_000_000   # 계약 1건당 — 이자로 돈이 무한히 불어나지 않게
MAX_ACTIVE = 5            # 동시에 진행 중인 계약 수
CANCEL_FEE = 0.05         # 중도 해지: 이자 없이 원금의 5% 수수료


def settle_amount(sponsor: str, days: int, amount: int, rng=random) -> tuple[int, float]:
    """만기 지급액(원금 포함)과 실적 배율."""
    lo, hi = SPONSORS[sponsor][3:5]
    r = rng.uniform(lo, hi)
    gain = max(-amount, round(amount * TERMS[days] * r))
    return amount + gain, r


def cancel_refund(amount: int) -> int:
    return amount - int(amount * CANCEL_FEE)


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
            con.execute("CREATE INDEX IF NOT EXISTS idx_sponsor_user ON sponsor_contracts(user_id, status)")
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

    async def open(self, user_id: int, sponsor: str, days: int, amount: int, now_ts: int) -> dict:
        """계약 체결: 잔액에서 원금을 빼고 계약을 만든다. 실패 시 reason: balance / full."""
        def fn(con):
            bal = self._balance(con, user_id)
            active = con.execute("SELECT COUNT(*) FROM sponsor_contracts WHERE user_id=? AND status='active'",
                                 (user_id,)).fetchone()[0]
            if active >= MAX_ACTIVE:
                return {"ok": False, "reason": "full", "active": active}
            if bal < amount:
                return {"ok": False, "reason": "balance", "balance": bal}
            con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (amount, user_id))
            cid = con.execute(
                "INSERT INTO sponsor_contracts(user_id, sponsor, days, amount, start_ts, end_ts) VALUES(?,?,?,?,?,?)",
                (user_id, sponsor, days, amount, now_ts, now_ts + days * DAY),
            ).lastrowid
            return {"ok": True, "id": cid, "end_ts": now_ts + days * DAY, "balance": bal - amount}
        return await self._tx(fn)

    async def active(self, user_id: int) -> list[dict]:
        def fn(con):
            rows = con.execute(
                "SELECT id, sponsor, days, amount, start_ts, end_ts FROM sponsor_contracts "
                "WHERE user_id=? AND status='active' ORDER BY end_ts", (user_id,)).fetchall()
            return [dict(zip(("id", "sponsor", "days", "amount", "start_ts", "end_ts"), r)) for r in rows]
        return await self._tx(fn)

    async def settle(self, user_id: int, now_ts: int, rng=random) -> tuple[list[dict], Optional[int]]:
        """만기가 지난 계약을 전부 정산해 지급한다. (정산 목록, 새 잔액 — 정산이 없으면 None)"""
        def fn(con):
            rows = con.execute(
                "SELECT id, sponsor, days, amount FROM sponsor_contracts "
                "WHERE user_id=? AND status='active' AND end_ts<=? ORDER BY end_ts", (user_id, now_ts)).fetchall()
            done = []
            for cid, sponsor, days, amount in rows:
                payout, perf = settle_amount(sponsor, days, amount, rng)
                con.execute("UPDATE sponsor_contracts SET status='settled', payout=?, perf=? WHERE id=?",
                            (payout, perf, cid))
                con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (payout, user_id))
                done.append({"id": cid, "sponsor": sponsor, "days": days, "amount": amount,
                             "payout": payout, "perf": perf})
            return done, (self._balance(con, user_id) if done else None)
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
