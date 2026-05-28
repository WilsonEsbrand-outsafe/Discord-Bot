# services/economy_db.py
import sqlite3
import asyncio
from pathlib import Path
from typing import Optional, Tuple

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "economy.sqlite3"


class EconomyDB:
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
                CREATE TABLE IF NOT EXISTS wallets (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_claims (
                    user_id       INTEGER PRIMARY KEY,
                    last_claim_ts INTEGER NOT NULL DEFAULT 0,
                    streak        INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # 기존 DB 마이그레이션 — streak 컬럼이 없으면 추가
            try:
                con.execute("ALTER TABLE daily_claims ADD COLUMN streak INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS penalty_kick (
                    user_id INTEGER PRIMARY KEY,
                    last_play_ts INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # ✅ 훈련(쿨타임)
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS training (
                    user_id INTEGER PRIMARY KEY,
                    last_play_ts INTEGER NOT NULL DEFAULT 0
                )
                """
            )
                        # ───────────── 토토 ─────────────
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS toto_matches (
                    match_id TEXT PRIMARY KEY,
                    home TEXT NOT NULL,
                    away TEXT NOT NULL,
                    kickoff_ts INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',  -- open/closed/settled
                    result TEXT DEFAULT NULL,             -- '1'/'X'/'2'
                    base_home REAL NOT NULL DEFAULT 1.4,
                    base_draw REAL NOT NULL DEFAULT 2.9,
                    base_away REAL NOT NULL DEFAULT 2.1
                )
                """
            )

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS toto_bets (
                    bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    match_id TEXT NOT NULL,
                    pick TEXT NOT NULL,                   -- '1'/'X'/'2'
                    amount INTEGER NOT NULL,
                    odds_locked REAL NOT NULL,
                    placed_ts INTEGER NOT NULL,
                    settled INTEGER NOT NULL DEFAULT 0,   -- 0/1
                    payout INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(user_id, match_id),
                    FOREIGN KEY(match_id) REFERENCES toto_matches(match_id) ON DELETE CASCADE
                )
                """
            )

            con.execute("CREATE INDEX IF NOT EXISTS idx_toto_matches_status ON toto_matches(status)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_toto_bets_match ON toto_bets(match_id)")

            # ───────────── 알림 설정 ─────────────
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_settings (
                    user_id   INTEGER NOT NULL,
                    event_key TEXT    NOT NULL,
                    enabled   INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (user_id, event_key)
                )
                """
            )

            con.commit()
        finally:
            con.close()

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def get_balance(self, user_id: int) -> int:
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute(
                        "SELECT balance FROM wallets WHERE user_id=?",
                        (user_id,),
                    ).fetchone()
                    if row is None:
                        con.execute("INSERT INTO wallets(user_id, balance) VALUES(?, 0)", (user_id,))
                        con.commit()
                        return 0
                    return int(row[0])
                finally:
                    con.close()
            return await self._run(work)

    async def add_balance(self, user_id: int, amount: int) -> int:
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (user_id,))
                    con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (amount, user_id))
                    con.commit()
                    row = con.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,)).fetchone()
                    return int(row[0]) if row else 0
                finally:
                    con.close()
            return await self._run(work)

    async def set_balance(self, user_id: int, new_balance: int) -> int:
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (user_id,))
                    con.execute("UPDATE wallets SET balance=? WHERE user_id=?", (new_balance, user_id))
                    con.commit()
                    return new_balance
                finally:
                    con.close()
            return await self._run(work)

    async def transfer(self, from_user: int, to_user: int, amount: int) -> Optional[str]:
        if amount <= 0:
            return "금액은 1 이상이어야 합니다."
        if from_user == to_user:
            return "자기 자신에게는 송금할 수 없습니다."

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("BEGIN IMMEDIATE;")
                    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (from_user,))
                    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (to_user,))

                    row = con.execute("SELECT balance FROM wallets WHERE user_id=?", (from_user,)).fetchone()
                    bal = int(row[0]) if row else 0
                    if bal < amount:
                        con.execute("ROLLBACK;")
                        return "잔액이 부족합니다."

                    con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (amount, from_user))
                    con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (amount, to_user))
                    con.execute("COMMIT;")
                    return None
                except Exception as e:
                    try:
                        con.execute("ROLLBACK;")
                    except Exception:
                        pass
                    return f"DB 오류: {type(e).__name__}"
                finally:
                    con.close()
            return await self._run(work)

    async def claim_daily(self, user_id: int, reward: int, now_ts: int) -> Tuple[bool, int, int, int, int]:
        """
        매일 00:00(KST) 기준 하루 1회.
        반환: (성공여부, 새잔액(성공시), 남은초(실패시), 현재스트릭, 스트릭보너스)
        스트릭 보너스: 7일 +50,000 / 14일 +150,000 / 30일 +500,000
        """
        KST_OFFSET = 9 * 3600
        now_kst = now_ts + KST_OFFSET
        today_key = now_kst // 86400  # 날짜 키(한국 기준)

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("BEGIN IMMEDIATE;")
                    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (user_id,))
                    con.execute("INSERT OR IGNORE INTO daily_claims(user_id, last_claim_ts) VALUES(?, 0)", (user_id,))

                    row = con.execute(
                        "SELECT last_claim_ts, streak FROM daily_claims WHERE user_id=?",
                        (user_id,),
                    ).fetchone()
                    last         = int(row[0]) if row else 0
                    prev_streak  = int(row[1]) if row and row[1] is not None else 0

                    last_kst = last + KST_OFFSET
                    last_key = last_kst // 86400

                    # 같은 날짜(한국 기준)면 실패
                    if last_key == today_key:
                        next_midnight_kst = (today_key + 1) * 86400
                        remaining = next_midnight_kst - now_kst
                        con.execute("ROLLBACK;")
                        return (False, 0, int(remaining), 0, 0)

                    # 스트릭 계산
                    if last == 0:
                        new_streak = 1
                    elif today_key - last_key == 1:   # 연속 출석
                        new_streak = prev_streak + 1
                    else:                              # 하루 이상 건너뜀
                        new_streak = 1

                    # 스트릭 보너스 (마일스톤 달성 시)
                    _STREAK_BONUS = {7: 50_000, 14: 150_000, 30: 500_000}
                    streak_bonus  = _STREAK_BONUS.get(new_streak, 0)
                    total_reward  = reward + streak_bonus

                    con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (total_reward, user_id))
                    con.execute(
                        "UPDATE daily_claims SET last_claim_ts=?, streak=? WHERE user_id=?",
                        (now_ts, new_streak, user_id),
                    )
                    con.execute("COMMIT;")

                    new_bal = con.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,)).fetchone()
                    return (True, int(new_bal[0]) if new_bal else 0, 0, new_streak, streak_bonus)

                except Exception:
                    try:
                        con.execute("ROLLBACK;")
                    except Exception:
                        pass
                    raise
                finally:
                    con.close()

            return await self._run(work)

    async def play_penalty_kick(
        self, user_id: int, delta: int, now_ts: int, cooldown_sec: int = 0
    ) -> Tuple[bool, int, int]:
        """
        페널티킥 결과 반영
        cooldown_sec=0 이면 쿨타임 없음
        반환: (성공여부, 새잔액(성공시), 남은초(실패시))
        """
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("BEGIN IMMEDIATE;")
                    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (user_id,))
                    con.execute("INSERT OR IGNORE INTO penalty_kick(user_id, last_play_ts) VALUES(?, 0)", (user_id,))

                    # ✅ 쿨타임이 0이면 체크 안 함
                    if cooldown_sec > 0:
                        row = con.execute(
                            "SELECT last_play_ts FROM penalty_kick WHERE user_id=?",
                            (user_id,),
                        ).fetchone()
                        last = int(row[0]) if row else 0
                        diff = now_ts - last
                        if diff < cooldown_sec:
                            remaining = cooldown_sec - diff
                            con.execute("ROLLBACK;")
                            return (False, 0, int(remaining))

                    cur = con.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,)).fetchone()
                    cur_bal = int(cur[0]) if cur else 0

                    # ✅ 잔액 음수 허용
                    new_bal = cur_bal + int(delta)

                    con.execute("UPDATE wallets SET balance=? WHERE user_id=?", (new_bal, user_id))
                    con.execute("UPDATE penalty_kick SET last_play_ts=? WHERE user_id=?", (now_ts, user_id))
                    con.execute("COMMIT;")
                    return (True, new_bal, 0)

                except Exception:
                    try:
                        con.execute("ROLLBACK;")
                    except Exception:
                        pass
                    raise
                finally:
                    con.close()

            return await self._run(work)

    # ───────────── 토토 DB 메서드 ─────────────

    async def toto_upsert_match(
        self,
        match_id: str,
        home: str,
        away: str,
        kickoff_ts: int,
        base_home: float = 1.4,
        base_draw: float = 2.9,
        base_away: float = 2.1,
    ) -> None:
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute(
                        """
                        INSERT INTO toto_matches(match_id, home, away, kickoff_ts, status, base_home, base_draw, base_away)
                        VALUES(?, ?, ?, ?, 'open', ?, ?, ?)
                        ON CONFLICT(match_id) DO UPDATE SET
                            home=excluded.home,
                            away=excluded.away,
                            kickoff_ts=excluded.kickoff_ts,
                            base_home=excluded.base_home,
                            base_draw=excluded.base_draw,
                            base_away=excluded.base_away
                        """,
                        (match_id, home, away, int(kickoff_ts), float(base_home), float(base_draw), float(base_away)),
                    )
                    con.commit()
                finally:
                    con.close()
            return await self._run(work)

    async def toto_list_open_matches(self, now_ts: int, limit: int = 10):
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    rows = con.execute(
                        """
                        SELECT match_id, home, away, kickoff_ts, base_home, base_draw, base_away
                        FROM toto_matches
                        WHERE status='open' AND kickoff_ts > ? + 600
                        ORDER BY kickoff_ts ASC
                        LIMIT ?
                        """,
                        (int(now_ts), int(limit)),
                    ).fetchall()
                    return rows
                finally:
                    con.close()
            return await self._run(work)

    async def toto_get_match(self, match_id: str):
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute(
                        """
                        SELECT match_id, home, away, kickoff_ts, status, result, base_home, base_draw, base_away
                        FROM toto_matches
                        WHERE match_id=?
                        """,
                        (match_id,),
                    ).fetchone()
                    return row
                finally:
                    con.close()
            return await self._run(work)

    async def toto_get_bet(self, user_id: int, match_id: str):
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute(
                        """
                        SELECT bet_id, pick, amount, odds_locked, settled, payout
                        FROM toto_bets
                        WHERE user_id=? AND match_id=?
                        """,
                        (int(user_id), match_id),
                    ).fetchone()
                    return row
                finally:
                    con.close()
            return await self._run(work)

    async def toto_get_match_pool(self, match_id: str):
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    rows = con.execute(
                        """
                        SELECT pick, COALESCE(SUM(amount), 0)
                        FROM toto_bets
                        WHERE match_id=?
                        GROUP BY pick
                        """,
                        (match_id,),
                    ).fetchall()
                    d = {"1": 0, "X": 0, "2": 0}
                    for p, s in rows:
                        if p in d:
                            d[p] = int(s)
                    return d
                finally:
                    con.close()
            return await self._run(work)

    def toto_compute_dynamic_odds(
        self,
        *,
        base_home: float,
        base_draw: float,
        base_away: float,
        pool_home: int,
        pool_draw: int,
        pool_away: int,
        alpha: float = 0.25,
        smoothing: int = 50,
        cap_pct: float = 0.20,
    ):
        # ✅ 베팅이 0이면, 기본 배당 그대로 반환
        if int(pool_home) + int(pool_draw) + int(pool_away) == 0:
            return (round(float(base_home), 2), round(float(base_draw), 2), round(float(base_away), 2))
        
        # 기본 확률(배당 -> 확률 -> 정규화)
        p0_h = 1.0 / float(base_home)
        p0_d = 1.0 / float(base_draw)
        p0_a = 1.0 / float(base_away)
        s0 = p0_h + p0_d + p0_a
        p0_h, p0_d, p0_a = p0_h / s0, p0_d / s0, p0_a / s0

        # 참여자 확률(스무딩)
        h = int(pool_home) + smoothing
        d = int(pool_draw) + smoothing
        a = int(pool_away) + smoothing
        t = h + d + a
        p1_h, p1_d, p1_a = h / t, d / t, a / t

        # 섞기
        ph = (1.0 - alpha) * p0_h + alpha * p1_h
        pd = (1.0 - alpha) * p0_d + alpha * p1_d
        pa = (1.0 - alpha) * p0_a + alpha * p1_a

        # 확률 -> 배당
        oh = 1.0 / ph
        od = 1.0 / pd
        oa = 1.0 / pa

        # 변동 폭 제한(±cap_pct)
        def clamp(o, base):
            lo = base * (1.0 - cap_pct)
            hi = base * (1.0 + cap_pct)
            return max(lo, min(hi, o))

        oh = clamp(oh, float(base_home))
        od = clamp(od, float(base_draw))
        oa = clamp(oa, float(base_away))

        # 보기 좋게 소수 2자리
        return (round(oh, 2), round(od, 2), round(oa, 2))

    async def toto_place_bet(
        self,
        *,
        user_id: int,
        match_id: str,
        pick: str,
        amount: int,
        odds_locked: float,
        now_ts: int,
    ) -> Optional[str]:
        if pick not in ("1", "X", "2"):
            return "픽은 1 / X / 2 중 하나여야 합니다."
        if amount <= 0:
            return "금액은 1 이상이어야 합니다."

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("BEGIN IMMEDIATE;")

                    # 지갑 보장
                    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(user_id),))

                    # 경기 확인
                    m = con.execute(
                        "SELECT status, kickoff_ts FROM toto_matches WHERE match_id=?",
                        (match_id,),
                    ).fetchone()
                    if not m:
                        con.execute("ROLLBACK;")
                        return "경기를 찾을 수 없습니다."
                    status, kickoff_ts = m[0], int(m[1])
                    if status != "open":
                        con.execute("ROLLBACK;")
                        return "이미 마감된 경기입니다."
                    
                    if int(now_ts) >= kickoff_ts - 600:
                        con.execute("ROLLBACK;")
                        return "경기 시작 10분 전부터 베팅이 마감됩니다."

                    if int(now_ts) >= kickoff_ts:
                        con.execute("ROLLBACK;")
                        return "이미 시작한 경기입니다."

                    # 중복 베팅 방지
                    exists = con.execute(
                        "SELECT 1 FROM toto_bets WHERE user_id=? AND match_id=?",
                        (int(user_id), match_id),
                    ).fetchone()
                    if exists:
                        con.execute("ROLLBACK;")
                        return "이미 이 경기에는 베팅했습니다."

                    # 잔액 체크(음수 잔액은 허용하더라도 베팅은 잔액 이상만)
                    bal = con.execute("SELECT balance FROM wallets WHERE user_id=?", (int(user_id),)).fetchone()
                    cur_bal = int(bal[0]) if bal else 0
                    if cur_bal < int(amount):
                        con.execute("ROLLBACK;")
                        return "잔액이 부족합니다."

                    # 베팅금 차감 + 베팅 기록
                    con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (int(amount), int(user_id)))
                    con.execute(
                        """
                        INSERT INTO toto_bets(user_id, match_id, pick, amount, odds_locked, placed_ts)
                        VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (int(user_id), match_id, pick, int(amount), float(odds_locked), int(now_ts)),
                    )

                    con.execute("COMMIT;")
                    return None
                except Exception:
                    try:
                        con.execute("ROLLBACK;")
                    except Exception:
                        pass
                    raise
                finally:
                    con.close()

            return await self._run(work)

    async def toto_set_result_and_settle(self, match_id: str, result: str, now_ts: int) -> Tuple[bool, str]:
        if result not in ("1", "X", "2"):
            return (False, "결과는 1 / X / 2 중 하나여야 합니다.")

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("BEGIN IMMEDIATE;")

                    m = con.execute(
                        "SELECT status FROM toto_matches WHERE match_id=?",
                        (match_id,),
                    ).fetchone()
                    if not m:
                        con.execute("ROLLBACK;")
                        return (False, "경기를 찾을 수 없습니다.")
                    status = m[0]
                    if status == "settled":
                        con.execute("ROLLBACK;")
                        return (False, "이미 정산된 경기입니다.")

                    # 결과 저장
                    con.execute(
                        "UPDATE toto_matches SET status='settled', result=? WHERE match_id=?",
                        (result, match_id),
                    )

                    # 미정산 베팅들 불러오기
                    bets = con.execute(
                        """
                        SELECT bet_id, user_id, pick, amount, odds_locked
                        FROM toto_bets
                        WHERE match_id=? AND settled=0
                        """,
                        (match_id,),
                    ).fetchall()

                    paid_count = 0
                    total_paid = 0

                    for bet_id, user_id, pick, amount, odds_locked in bets:
                        payout = 0
                        if pick == result:
                            payout = int(int(amount) * float(odds_locked))
                            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(user_id),))
                            con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (payout, int(user_id)))
                            paid_count += 1
                            total_paid += payout

                        con.execute(
                            "UPDATE toto_bets SET settled=1, payout=? WHERE bet_id=?",
                            (int(payout), int(bet_id)),
                        )

                    con.execute("COMMIT;")
                    return (True, f"정산 완료: 적중 {paid_count}명 / 총 지급 {total_paid:,}원")
                except Exception:
                    try:
                        con.execute("ROLLBACK;")
                    except Exception:
                        pass
                    raise
                finally:
                    con.close()

            return await self._run(work)

    async def toto_refund_and_delete_open_match(self, match_id: str) -> Tuple[bool, str]:
        """
        오픈 상태(open)인 경기 삭제 시:
        1) 해당 경기의 모든 베팅금 전액 환불
        2) 경기 + 베팅 삭제
        """
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("BEGIN IMMEDIATE;")

                    row = con.execute(
                        "SELECT status FROM toto_matches WHERE match_id=?",
                        (match_id,),
                    ).fetchone()
                    if not row:
                        con.execute("ROLLBACK;")
                        return (False, "경기를 찾을 수 없습니다.")

                    status = row[0]
                    if status != "open":
                        con.execute("ROLLBACK;")
                        return (False, "오픈 상태인 경기만 삭제/환불할 수 있습니다.")

                    # 베팅 목록
                    bets = con.execute(
                        "SELECT user_id, amount FROM toto_bets WHERE match_id=?",
                        (match_id,),
                    ).fetchall()

                    refunded_users = 0
                    refunded_total = 0

                    # 전액 환불
                    for user_id, amount in bets:
                        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(user_id),))
                        con.execute(
                            "UPDATE wallets SET balance = balance + ? WHERE user_id=?",
                            (int(amount), int(user_id)),
                        )
                        refunded_users += 1
                        refunded_total += int(amount)

                    # 경기 삭제(베팅은 CASCADE로 같이 삭제)
                    con.execute("DELETE FROM toto_matches WHERE match_id=?", (match_id,))

                    con.execute("COMMIT;")
                    return (True, f"환불 {refunded_users}건 / 총 {refunded_total:,}원 환불 후 삭제 완료")
                except Exception:
                    try:
                        con.execute("ROLLBACK;")
                    except Exception:
                        pass
                    raise
                finally:
                    con.close()

            return await self._run(work)
        
    async def toto_cancel_bet(self, user_id: int, match_id: str, now_ts: int) -> Tuple[bool, str]:
        """
        베팅 취소: 오픈(open) 상태 + 킥오프 전이면 가능
        취소 시 베팅금 전액 환불 후 bet 삭제
        """
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("BEGIN IMMEDIATE;")

                    m = con.execute(
                        "SELECT status, kickoff_ts FROM toto_matches WHERE match_id=?",
                        (match_id,),
                    ).fetchone()
                    if not m:
                        con.execute("ROLLBACK;")
                        return (False, "경기를 찾을 수 없습니다.")
                    status, kickoff_ts = m[0], int(m[1])

                    if int(now_ts) >= kickoff_ts - 600:
                        con.execute("ROLLBACK;")
                        return (False, "경기 시작 10분 전부터는 취소할 수 없습니다.")
                    
                    if status != "open":
                        con.execute("ROLLBACK;")
                        return (False, "오픈 상태인 경기만 취소할 수 있습니다.")
                    
                    if int(now_ts) >= kickoff_ts:
                        con.execute("ROLLBACK;")
                        return (False, "경기 시작 후에는 취소할 수 없습니다.")

                    b = con.execute(
                        "SELECT bet_id, amount, settled FROM toto_bets WHERE user_id=? AND match_id=?",
                        (int(user_id), match_id),
                    ).fetchone()
                    if not b:
                        con.execute("ROLLBACK;")
                        return (False, "이 경기에는 베팅한 기록이 없습니다.")

                    bet_id, amount, settled = int(b[0]), int(b[1]), int(b[2])
                    if settled == 1:
                        con.execute("ROLLBACK;")
                        return (False, "이미 정산된 베팅은 취소할 수 없습니다.")

                    # 환불
                    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(user_id),))
                    con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (amount, int(user_id)))

                    # 베팅 삭제
                    con.execute("DELETE FROM toto_bets WHERE bet_id=?", (bet_id,))

                    con.execute("COMMIT;")
                    return (True, f"베팅 취소 완료: {amount:,}원 환불")
                except Exception:
                    try:
                        con.execute("ROLLBACK;")
                    except Exception:
                        pass
                    raise
                finally:
                    con.close()

            return await self._run(work)
    
    async def toto_update_base_odds(
        self,
        match_id: str,
        base_home: float,
        base_draw: float,
        base_away: float,
    ):
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute(
                        """
                        UPDATE toto_matches
                        SET base_home=?, base_draw=?, base_away=?
                        WHERE match_id=?
                        """,
                        (float(base_home), float(base_draw), float(base_away), match_id),
                    )
                    con.commit()
                finally:
                    con.close()
            return await self._run(work)

    async def toto_list_candidates_for_settle(self, now_ts: int, limit: int = 30):
        """
        킥오프가 지난 경기 중 아직 settled가 아닌 것들
        (open/closed 모두 포함)
        """
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    rows = con.execute(
                        """
                        SELECT match_id
                        FROM toto_matches
                        WHERE status != 'settled' AND kickoff_ts <= ?
                        ORDER BY kickoff_ts ASC
                        LIMIT ?
                        """,
                        (int(now_ts), int(limit)),
                    ).fetchall()
                    return [r[0] for r in rows]
                finally:
                    con.close()
            return await self._run(work)

    async def toto_close_started(self, now_ts: int) -> int:
        """
        킥오프가 지난 open 경기를 closed로 바꿔서
        관리상 '시작 전/후'를 명확히 구분
        """
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    cur = con.execute(
                        """
                        UPDATE toto_matches
                        SET status='closed'
                        WHERE status='open' AND kickoff_ts <= ?
                        """,
                        (int(now_ts),),
                    )
                    con.commit()
                    return int(cur.rowcount or 0)
                finally:
                    con.close()
            return await self._run(work)

    async def toto_list_live_matches(self, now_ts: int, limit: int = 20):
        """
        킥오프가 지났고(set kickoff_ts <= now),
        아직 정산되지 않은(status != 'settled') 경기들
        """
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    rows = con.execute(
                        """
                        SELECT match_id, home, away, kickoff_ts, base_home, base_draw, base_away
                        FROM toto_matches
                        WHERE status != 'settled' AND kickoff_ts <= ?
                        ORDER BY kickoff_ts DESC
                        LIMIT ?
                        """,
                        (int(now_ts), int(limit)),
                    ).fetchall()
                    return rows
                finally:
                    con.close()
            return await self._run(work)

    async def toto_get_match_brief(self, match_id: str):
        """
        DM 알림용: 홈/원정/킥오프/결과/상태만 간단히 가져오기
        """
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute(
                        """
                        SELECT match_id, home, away, kickoff_ts, status, result
                        FROM toto_matches
                        WHERE match_id=?
                        """,
                        (match_id,),
                    ).fetchone()
                    return row
                finally:
                    con.close()
            return await self._run(work)

    async def toto_list_bets_for_dm(self, match_id: str):
        """
        DM 알림용: 해당 경기 베팅자 목록 + 정산 결과(payout 포함)
        """
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    rows = con.execute(
                        """
                        SELECT user_id, pick, amount, odds_locked, settled, payout
                        FROM toto_bets
                        WHERE match_id=?
                        """,
                        (match_id,),
                    ).fetchall()
                    return rows
                finally:
                    con.close()
            return await self._run(work)

    async def toto_list_user_bets(self, user_id: int, limit: int = 20):
        """유저의 베팅 내역 (정산 완료 포함, 최신순)"""
        limit = max(1, min(50, int(limit)))
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    return con.execute(
                        """
                        SELECT b.match_id, m.home, m.away, m.kickoff_ts, m.status, m.result,
                               b.pick, b.amount, b.odds_locked, b.settled, b.payout
                        FROM toto_bets b
                        JOIN toto_matches m ON m.match_id=b.match_id
                        WHERE b.user_id=?
                        ORDER BY m.kickoff_ts DESC
                        LIMIT ?
                        """,
                        (int(user_id), int(limit)),
                    ).fetchall()
                finally:
                    con.close()
            return await self._run(work)

    async def toto_list_in_progress(self, now_ts: int, limit: int = 20):
        """
        진행중(킥오프 지남 + 아직 정산 전) 경기 목록
        open -> closed 로 바뀌기 때문에 status='closed'를 대상으로 잡습니다.
        """
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    rows = con.execute(
                        """
                        SELECT match_id, home, away, kickoff_ts, base_home, base_draw, base_away
                        FROM toto_matches
                        WHERE kickoff_ts <= ? AND status='closed'
                        ORDER BY kickoff_ts DESC
                        LIMIT ?
                        """,
                        (int(now_ts), int(limit)),
                    ).fetchall()
                    return rows
                finally:
                    con.close()
            return await self._run(work)

    # ───────────── 알림 설정 ─────────────

    async def notify_enabled(self, user_id: int, event_key: str) -> bool:
        """해당 이벤트 알림이 켜져 있는지 확인. 미설정 시 기본값 True."""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute(
                        "SELECT enabled FROM notification_settings WHERE user_id=? AND event_key=?",
                        (int(user_id), event_key),
                    ).fetchone()
                    return bool(row[0]) if row is not None else True
                finally:
                    con.close()
            return await self._run(work)

    async def set_notify(self, user_id: int, event_key: str, enabled: bool) -> None:
        """알림 설정 저장."""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute(
                        """
                        INSERT INTO notification_settings(user_id, event_key, enabled)
                        VALUES(?, ?, ?)
                        ON CONFLICT(user_id, event_key) DO UPDATE SET enabled=excluded.enabled
                        """,
                        (int(user_id), event_key, 1 if enabled else 0),
                    )
                    con.commit()
                finally:
                    con.close()
            await self._run(work)

    async def get_all_notify(self, user_id: int) -> dict[str, bool]:
        """유저의 전체 알림 설정 반환. 미설정 키는 True(기본값)."""
        from services.notifier import NOTIFY_EVENTS
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    rows = con.execute(
                        "SELECT event_key, enabled FROM notification_settings WHERE user_id=?",
                        (int(user_id),),
                    ).fetchall()
                    return {k: bool(v) for k, v in rows}
                finally:
                    con.close()
            saved = await self._run(work)
        return {key: saved.get(key, True) for key in NOTIFY_EVENTS}

    async def delete_user(self, user_id: int) -> dict:
        """유저의 모든 economy DB 데이터 삭제. 삭제된 행 수 반환."""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("BEGIN IMMEDIATE;")
                    results = {}
                    for table, col in [
                        ("wallets",               "user_id"),
                        ("daily_claims",          "user_id"),
                        ("penalty_kick",          "user_id"),
                        ("training",              "user_id"),
                        ("notification_settings", "user_id"),
                        ("toto_bets",             "user_id"),
                    ]:
                        try:
                            cur = con.execute(f"DELETE FROM {table} WHERE {col}=?", (int(user_id),))
                            if cur.rowcount:
                                results[table] = cur.rowcount
                        except Exception:
                            pass
                    con.commit()
                    return results
                except Exception:
                    try: con.execute("ROLLBACK;")
                    except Exception: pass
                    raise
                finally:
                    con.close()
            return await self._run(work)

    # ✅ 훈련 전용(쿨타임)
    async def play_training(self, user_id: int, delta: int, now_ts: int, cooldown_sec: int = 30) -> Tuple[bool, int, int]:
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("BEGIN IMMEDIATE;")
                    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (user_id,))
                    con.execute("INSERT OR IGNORE INTO training(user_id, last_play_ts) VALUES(?, 0)", (user_id,))

                    row = con.execute(
                        "SELECT last_play_ts FROM training WHERE user_id=?",
                        (user_id,),
                    ).fetchone()
                    last = int(row[0]) if row else 0
                    diff = now_ts - last

                    if diff < cooldown_sec:
                        remaining = cooldown_sec - diff
                        con.execute("ROLLBACK;")
                        return (False, 0, int(remaining))

                    cur = con.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,)).fetchone()
                    cur_bal = int(cur[0]) if cur else 0
                    new_bal = cur_bal + int(delta)

                    con.execute("UPDATE wallets SET balance=? WHERE user_id=?", (new_bal, user_id))
                    con.execute("UPDATE training SET last_play_ts=? WHERE user_id=?", (now_ts, user_id))
                    con.execute("COMMIT;")
                    return (True, new_bal, 0)

                except Exception:
                    try:
                        con.execute("ROLLBACK;")
                    except Exception:
                        pass
                    raise
                finally:
                    con.close()

            return await self._run(work)
