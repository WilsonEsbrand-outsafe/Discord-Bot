# services/economy_db.py
import sqlite3
import asyncio
from pathlib import Path
from typing import Optional, Tuple

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "economy.sqlite3"

# ── 훈련 밸런스 ─────────────────────────────────────────────
TRAIN_MAX_LEVEL = 10
TRAIN_DAILY_LIMIT = 30
# Lv.N → Lv.N+1 에 필요한 XP. 레벨이 오를수록 크게 늘어난다 (만렙까지 합계 10,410 XP ≈ 하루 약 65 XP 기준 5개월 남짓).
TRAIN_XP_NEED = (60, 150, 300, 500, 800, 1200, 1700, 2400, 3300)

# 스카우트: 훈련의 상위 버전 — 하루 15회, 최대 Lv.5.
# 만렙까지 합계 4,920 XP ≈ 하루 약 30 XP 기준 5개월 남짓.
SCOUT_MAX_LEVEL = 5
SCOUT_DAILY_LIMIT = 15
SCOUT_XP_NEED = (120, 600, 1200, 3000)   # 하루 약 30 XP 기준 4일 · 20일 · 40일 · 100일

# 직관: 스카우트 → 훈련을 모두 마친 뒤 열리는 세 번째 반복 콘텐츠. 하루 5회, 최대 Lv.5, 아이템을 얻는다.
WATCH_MAX_LEVEL = 5
WATCH_DAILY_LIMIT = 5
WATCH_XP_NEED = (30, 120, 300, 700)       # 하루 약 12 XP 기준 3일 · 10일 · 25일 · 60일

# 레벨·경험치·일일 횟수를 쓰는 반복 콘텐츠 규칙: 테이블 → (만렙, 하루 횟수, 필요 XP 표)
GRIND_RULES = {
    "training": (TRAIN_MAX_LEVEL, TRAIN_DAILY_LIMIT, TRAIN_XP_NEED),
    "scouting": (SCOUT_MAX_LEVEL, SCOUT_DAILY_LIMIT, SCOUT_XP_NEED),
    "spectating": (WATCH_MAX_LEVEL, WATCH_DAILY_LIMIT, WATCH_XP_NEED),
}

# 아이템: key → (이모지, 이름, 설명)
ITEMS = {
    "muffler":     ("🧣", "응원 머플러",     "다음 5경기 동안 구단 전력 +3 (친선경기 · 공식경기)"),
    "scout_reset": ("🧳", "스카우트 리셋권", f"오늘 스카우트 +{SCOUT_DAILY_LIMIT}회"),
    "train_reset": ("🔄", "훈련 리셋권",     f"오늘 훈련 +{TRAIN_DAILY_LIMIT}회"),
}
MUFFLER_USES, MUFFLER_BONUS = 5, 3


def give_item(con, user_id: int, item: str, qty: int = 1) -> None:
    """같은 트랜잭션 안에서 아이템 지급 (직관 드롭 등)."""
    con.execute("INSERT INTO inventory(user_id, item, qty) VALUES(?,?,?) "
                "ON CONFLICT(user_id, item) DO UPDATE SET qty = qty + excluded.qty", (int(user_id), item, int(qty)))

# 출석: 누적 출석 일수가 이 날에 닿으면 보너스 (빠져도 초기화되지 않는다)
ATTEND_BONUS = {7: 50_000, 14: 100_000, 30: 300_000, 50: 500_000, 100: 1_000_000, 200: 2_000_000, 365: 5_000_000}

TRANSFER_DAILY_LIMIT = 5_000_000   # 하루(KST) 보낼 수 있는 송금 총액

# 파산: 잔액이 마이너스일 때만. 선수 카드·스폰서 원금으로 먼저 갚고 남은 빚은 탕감.
BANKRUPT_COOLDOWN = 30 * 86400     # 30일에 한 번
BANKRUPT_BET_BAN  = 3 * 86400      # 파산 후 3일간 베팅 게임 금지


def _kst_day(ts: int) -> int:
    return (int(ts) + 9 * 3600) // 86400


def grind_xp_need(table: str, level: int) -> int:
    max_level, _, need = GRIND_RULES[table]
    return need[min(int(level), max_level - 1) - 1]


def train_xp_need(level: int) -> int:
    return grind_xp_need("training", level)


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
            # 2.2: 연속 보너스 → 누적 출석 일수 보너스. 처음 추가될 때 지금까지의 연속 일수로 시작한다.
            try:
                con.execute("ALTER TABLE daily_claims ADD COLUMN total_days INTEGER NOT NULL DEFAULT 0")
                con.execute("UPDATE daily_claims SET total_days = streak")
            except Exception:
                pass
            # 하루 송금 누적액 (KST 날짜별)
            con.execute("CREATE TABLE IF NOT EXISTS transfer_daily (user_id INTEGER PRIMARY KEY, day_key INTEGER, sent INTEGER)")
            # 파산 기록: 마지막 파산 시각 · 횟수
            con.execute("CREATE TABLE IF NOT EXISTS bankruptcy (user_id INTEGER PRIMARY KEY, last_ts INTEGER, times INTEGER)")
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
            # 훈련 레벨/일일 횟수 (구버전 테이블 마이그레이션)
            for col in ("level INTEGER NOT NULL DEFAULT 1", "xp INTEGER NOT NULL DEFAULT 0",
                        "day_key INTEGER NOT NULL DEFAULT 0", "day_count INTEGER NOT NULL DEFAULT 0"):
                try:
                    con.execute(f"ALTER TABLE training ADD COLUMN {col}")
                except Exception:
                    pass
            con.execute("UPDATE training SET level=?, xp=0 WHERE level>?", (TRAIN_MAX_LEVEL, TRAIN_MAX_LEVEL))
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS scouting (
                    user_id INTEGER PRIMARY KEY,
                    last_play_ts INTEGER NOT NULL DEFAULT 0,
                    level INTEGER NOT NULL DEFAULT 1,
                    xp INTEGER NOT NULL DEFAULT 0,
                    day_key INTEGER NOT NULL DEFAULT 0,
                    day_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS spectating (
                    user_id INTEGER PRIMARY KEY,
                    last_play_ts INTEGER NOT NULL DEFAULT 0,
                    level INTEGER NOT NULL DEFAULT 1,
                    xp INTEGER NOT NULL DEFAULT 0,
                    day_key INTEGER NOT NULL DEFAULT 0,
                    day_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # 리셋권: 그날(bonus_day) 추가로 할 수 있는 횟수
            for t in GRIND_RULES:
                for col in ("bonus_day", "bonus_count"):
                    try:
                        con.execute(f"ALTER TABLE {t} ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
                    except sqlite3.OperationalError:
                        pass
            # 아이템: 가방(보유 수량)과 사용 중인 효과(남은 횟수)
            con.execute("CREATE TABLE IF NOT EXISTS inventory (user_id INTEGER, item TEXT, qty INTEGER, PRIMARY KEY(user_id, item))")
            con.execute("CREATE TABLE IF NOT EXISTS buffs (user_id INTEGER, item TEXT, uses INTEGER, PRIMARY KEY(user_id, item))")
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
                    base_away REAL NOT NULL DEFAULT 2.1,
                    -- The Odds API 실제 배당이 반영됐는지. 0이면 아직 기본값이다.
                    odds_applied INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # 기존 DB 마이그레이션 — odds_applied 컬럼이 없으면 추가
            try:
                con.execute("ALTER TABLE toto_matches ADD COLUMN odds_applied INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass

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

    async def _tx(self, fn):
        """BEGIN IMMEDIATE 로 fn(con) 실행 후 커밋 (실패 시 롤백)."""
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
            return await self._run(work)

    @staticmethod
    def _sent_today(con, user_id: int, day: int) -> int:
        row = con.execute("SELECT day_key, sent FROM transfer_daily WHERE user_id=?", (user_id,)).fetchone()
        return int(row[1]) if row and row[0] == day else 0

    async def transfer_remaining(self, user_id: int, now_ts: int) -> int:
        """오늘 더 보낼 수 있는 송금액."""
        return await self._tx(lambda con: TRANSFER_DAILY_LIMIT - self._sent_today(con, user_id, _kst_day(now_ts)))

    async def transfer(self, from_user: int, to_user: int, amount: int, now_ts: int = 0) -> Optional[str]:
        """송금. 실패하면 이유를 돌려준다. 하루(KST) 송금 총액은 TRANSFER_DAILY_LIMIT 까지."""
        if amount <= 0:
            return "금액은 1 이상이어야 합니다."
        if from_user == to_user:
            return "자기 자신에게는 송금할 수 없습니다."
        day = _kst_day(now_ts)

        def fn(con):
            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (from_user,))
            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (to_user,))
            sent = self._sent_today(con, from_user, day)
            if sent + amount > TRANSFER_DAILY_LIMIT:
                return f"오늘 송금 한도를 넘어요. (하루 {TRANSFER_DAILY_LIMIT:,}원 · 남은 한도 {TRANSFER_DAILY_LIMIT - sent:,}원)"
            bal = int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (from_user,)).fetchone()[0])
            if bal < amount:
                return "잔액이 부족합니다."
            con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (amount, from_user))
            con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (amount, to_user))
            con.execute("INSERT OR REPLACE INTO transfer_daily(user_id, day_key, sent) VALUES(?,?,?)",
                        (from_user, day, sent + amount))
            return None
        try:
            return await self._tx(fn)
        except Exception as e:
            return f"DB 오류: {type(e).__name__}"

    async def claim_daily(self, user_id: int, reward: int, now_ts: int) -> Tuple[bool, int, int, int, int]:
        """
        매일 00:00(KST) 기준 하루 1회.
        반환: (성공여부, 새잔액(성공시), 남은초(실패시), 누적 출석 일수, 보너스)
        보너스는 누적 출석 일수가 ATTEND_BONUS 의 날에 닿을 때 (빠져도 초기화 없음).
        """
        today = _kst_day(now_ts)

        def fn(con):
            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (user_id,))
            con.execute("INSERT OR IGNORE INTO daily_claims(user_id, last_claim_ts) VALUES(?, 0)", (user_id,))
            last, total = con.execute("SELECT last_claim_ts, total_days FROM daily_claims WHERE user_id=?",
                                      (user_id,)).fetchone()
            if last and _kst_day(last) == today:
                return (False, 0, (today + 1) * 86400 - (now_ts + 9 * 3600), int(total), 0)
            total = int(total) + 1
            bonus = ATTEND_BONUS.get(total, 0)
            con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (reward + bonus, user_id))
            con.execute("UPDATE daily_claims SET last_claim_ts=?, total_days=? WHERE user_id=?", (now_ts, total, user_id))
            bal = con.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,)).fetchone()[0]
            return (True, int(bal), 0, total, bonus)
        return await self._tx(fn)

    async def today_status(self, user_id: int, now_ts: int) -> dict:
        """튜토리얼 체크리스트용: 오늘 출석 여부 · 스카우트/훈련/직관 횟수 · 구단 유무."""
        today = _kst_day(now_ts)

        def fn(con):
            row = con.execute("SELECT last_claim_ts FROM daily_claims WHERE user_id=?", (user_id,)).fetchone()
            out = {"attended": bool(row and row[0] and _kst_day(row[0]) == today)}
            for table in GRIND_RULES:
                r = con.execute(f"SELECT day_key, day_count FROM {table} WHERE user_id=?", (user_id,)).fetchone()
                out[table] = int(r[1]) if r and r[0] == today else 0
            try:
                out["club"] = bool(con.execute("SELECT 1 FROM clubs WHERE user_id=?", (user_id,)).fetchone())
            except sqlite3.OperationalError:
                out["club"] = False
            return out
        return await self._tx(fn)

    async def club_name(self, user_id: int) -> Optional[str]:
        """구단 이름 (구단이 없거나 구단 테이블이 아직 없으면 None)."""
        def fn(con):
            try:
                row = con.execute("SELECT club_name FROM clubs WHERE user_id=?", (user_id,)).fetchone()
            except sqlite3.OperationalError:
                return None
            return row[0] if row else None
        return await self._tx(fn)

    # ───────────── 파산 ─────────────
    async def bet_ban_until(self, user_id: int, now_ts: int) -> int:
        """파산 후 베팅 금지가 풀리는 시각 (금지 중이 아니면 0)."""
        def fn(con):
            row = con.execute("SELECT last_ts FROM bankruptcy WHERE user_id=?", (user_id,)).fetchone()
            until = int(row[0]) + BANKRUPT_BET_BAN if row else 0
            return until if until > now_ts else 0
        return await self._tx(fn)

    async def declare_bankruptcy(self, user_id: int, now_ts: int) -> dict:
        """파산: 선수 카드(아마추어 제외)를 즉시판매가로 넘기고, 진행 중 스폰서 계약 원금을 돌려받아 빚을 갚는다.
        그래도 남는 빚은 0으로 탕감. 실패 reason: not_negative / cooldown(until)."""
        def has(con, table):
            return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()

        def fn(con):
            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (user_id,))
            bal = int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,)).fetchone()[0])
            if bal >= 0:
                return {"ok": False, "reason": "not_negative", "balance": bal}
            row = con.execute("SELECT last_ts, times FROM bankruptcy WHERE user_id=?", (user_id,)).fetchone()
            if row and now_ts - int(row[0]) < BANKRUPT_COOLDOWN:
                return {"ok": False, "reason": "cooldown", "until": int(row[0]) + BANKRUPT_COOLDOWN}

            cards = cards_value = 0
            if has(con, "pm_holdings") and has(con, "pm_players"):
                rows = con.execute(
                    "SELECT h.qty, p.base_value, p.retired FROM pm_holdings h JOIN pm_players p ON p.player_id=h.player_id "
                    "WHERE h.user_id=? AND h.qty>0 AND h.player_id NOT LIKE 'AMT_%'", (user_id,)).fetchall()
                for qty, base, retired in rows:
                    cards += int(qty)
                    cards_value += int(int(base) * (0.3 if int(retired) else 0.5)) * int(qty)
                con.execute("DELETE FROM pm_holdings WHERE user_id=? AND player_id NOT LIKE 'AMT_%'", (user_id,))
            sponsor = 0
            if has(con, "sponsor_contracts"):
                sponsor = int(con.execute("SELECT COALESCE(SUM(amount), 0) FROM sponsor_contracts "
                                          "WHERE user_id=? AND status='active'", (user_id,)).fetchone()[0])
                con.execute("UPDATE sponsor_contracts SET status='cancelled', payout=amount "
                            "WHERE user_id=? AND status='active'", (user_id,))
            after = bal + cards_value + sponsor
            forgiven = max(0, -after)
            new_bal = max(0, after)
            con.execute("UPDATE wallets SET balance=? WHERE user_id=?", (new_bal, user_id))
            con.execute("INSERT INTO bankruptcy(user_id, last_ts, times) VALUES(?, ?, 1) "
                        "ON CONFLICT(user_id) DO UPDATE SET last_ts=excluded.last_ts, times=times+1", (user_id, now_ts))
            return {"ok": True, "debt": -bal, "cards": cards, "cards_value": cards_value, "sponsor": sponsor,
                    "forgiven": forgiven, "balance": new_bal, "ban_until": now_ts + BANKRUPT_BET_BAN}
        return await self._tx(fn)

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
                            kickoff_ts=excluded.kickoff_ts
                        -- base_* 는 갱신하지 않음: 재등록(자동 임포트) 때마다
                        -- The Odds API로 받아둔 실제 배당이 기본값으로 덮여쓰이던 버그.
                        -- 실제 배당 반영은 toto_update_base_odds()가 담당한다.
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
                        SET base_home=?, base_draw=?, base_away=?, odds_applied=1
                        WHERE match_id=?
                        """,
                        (float(base_home), float(base_draw), float(base_away), match_id),
                    )
                    con.commit()
                finally:
                    con.close()
            return await self._run(work)

    async def toto_missing_odds(self, match_ids: list) -> set:
        """주어진 경기 중 아직 실제 배당이 안 붙은 것들의 match_id 집합.

        비어 있으면 The Odds API 를 호출할 이유가 없다. 예전엔 이미 배당이
        다 붙어 있어도 자동 등록 때마다 유료 호출을 했다.
        """
        ids = [str(m) for m in (match_ids or [])]
        if not ids:
            return set()
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    marks = ",".join("?" * len(ids))
                    rows = con.execute(
                        f"SELECT match_id FROM toto_matches "
                        f"WHERE odds_applied=0 AND match_id IN ({marks})",
                        ids,
                    ).fetchall()
                    return {str(r[0]) for r in rows}
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
        """해당 이벤트 알림이 켜져 있는지 확인. 미설정 시 기본값 False."""
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute(
                        "SELECT enabled FROM notification_settings WHERE user_id=? AND event_key=?",
                        (int(user_id), event_key),
                    ).fetchone()
                    return bool(row[0]) if row is not None else False
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
        """유저의 전체 알림 설정 반환. 미설정 키는 False(기본값)."""
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
        return {key: saved.get(key, False) for key in NOTIFY_EVENTS}

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
                        ("scouting",              "user_id"),
                        ("spectating",            "user_id"),
                        ("inventory",             "user_id"),
                        ("buffs",                 "user_id"),
                        ("clubs",                 "user_id"),
                        ("club_lineup",           "user_id"),
                        ("club_bonus",            "user_id"),
                        ("club_official",         "user_id"),
                        ("quiz_stats",            "user_id"),
                        ("quiz_results",          "user_id"),
                        ("sponsor_contracts",     "user_id"),
                        ("transfer_daily",        "user_id"),
                        ("bankruptcy",            "user_id"),
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

    # ✅ 훈련: 하루 횟수 제한 + 레벨(성공률·보상 증가)
    async def play_training(self, user_id: int, now_ts: int, roll, cooldown_sec: int = 30) -> dict:
        """훈련은 그날 스카우트를 전부(15회) 마쳐야 열린다."""
        return await self._play_grind("training", user_id, now_ts, roll, cooldown_sec,
                                      require=("scouting", SCOUT_DAILY_LIMIT))

    async def play_scout(self, user_id: int, now_ts: int, roll, cooldown_sec: int = 60) -> dict:
        return await self._play_grind("scouting", user_id, now_ts, roll, cooldown_sec)

    async def play_watch(self, user_id: int, now_ts: int, roll, cooldown_sec: int = 60) -> dict:
        """직관은 그날 훈련을 전부(30회) 마쳐야 열린다 (훈련은 스카우트 15회 뒤) — 스카우트 → 훈련 → 직관."""
        return await self._play_grind("spectating", user_id, now_ts, roll, cooldown_sec,
                                      require=("training", TRAIN_DAILY_LIMIT))

    # ───────────── 아이템 ─────────────
    async def inventory(self, user_id: int) -> tuple[dict[str, int], dict[str, int]]:
        """(가방 {아이템: 수량}, 사용 중 효과 {아이템: 남은 횟수})."""
        def fn(con):
            inv = {i: int(q) for i, q in con.execute("SELECT item, qty FROM inventory WHERE user_id=? AND qty>0", (user_id,))}
            buffs = {i: int(u) for i, u in con.execute("SELECT item, uses FROM buffs WHERE user_id=? AND uses>0", (user_id,))}
            return inv, buffs
        return await self._tx(fn)

    async def give_item(self, user_id: int, item: str, qty: int = 1) -> None:
        await self._tx(lambda con: give_item(con, user_id, item, qty))

    async def use_item(self, user_id: int, item: str, now_ts: int) -> dict:
        """아이템 사용. 실패 reason: none(없음). 성공 시 효과 설명용 값."""
        day = _kst_day(now_ts)

        def fn(con):
            row = con.execute("SELECT qty FROM inventory WHERE user_id=? AND item=?", (user_id, item)).fetchone()
            if not row or int(row[0]) <= 0:
                return {"ok": False, "reason": "none"}
            con.execute("UPDATE inventory SET qty = qty - 1 WHERE user_id=? AND item=?", (user_id, item))
            if item == "muffler":
                con.execute("INSERT INTO buffs(user_id, item, uses) VALUES(?, 'muffler', ?) "
                            "ON CONFLICT(user_id, item) DO UPDATE SET uses = uses + excluded.uses", (user_id, MUFFLER_USES))
                uses = con.execute("SELECT uses FROM buffs WHERE user_id=? AND item='muffler'", (user_id,)).fetchone()[0]
                return {"ok": True, "uses": int(uses)}
            table, extra = {"scout_reset": ("scouting", SCOUT_DAILY_LIMIT), "train_reset": ("training", TRAIN_DAILY_LIMIT)}[item]
            con.execute(f"INSERT OR IGNORE INTO {table}(user_id, last_play_ts) VALUES(?, 0)", (user_id,))
            bday, bcount = con.execute(f"SELECT bonus_day, bonus_count FROM {table} WHERE user_id=?", (user_id,)).fetchone()
            total = (int(bcount) if bday == day else 0) + extra
            con.execute(f"UPDATE {table} SET bonus_day=?, bonus_count=? WHERE user_id=?", (day, total, user_id))
            return {"ok": True, "extra": total}
        return await self._tx(fn)

    async def consume_buff(self, user_id: int, item: str) -> bool:
        """사용 중인 효과를 1회 소모 (남아 있으면 True)."""
        def fn(con):
            return con.execute("UPDATE buffs SET uses = uses - 1 WHERE user_id=? AND item=? AND uses>0",
                               (user_id, item)).rowcount > 0
        return await self._tx(fn)

    async def _play_grind(self, table: str, user_id: int, now_ts: int, roll, cooldown_sec: int,
                          require: tuple | None = None) -> dict:
        """
        레벨·경험치·일일 횟수가 있는 반복 콘텐츠(훈련·스카우트) 공용.
        roll(level, con) -> (delta, xp_gain, info) 를 트랜잭션 안에서 호출해 결과를 반영한다.
        (con 을 넘기는 건 스카우트가 같은 트랜잭션 안에서 선수 카드를 지급하기 위해서다.)
        반환 dict: ok, level, xp, need, used, limit, leveled, new_bal, delta, info
        ok=False 면 reason 이 "cooldown"(remaining 초), "limit"(오늘 횟수 소진),
        "locked"(require=(테이블, 횟수) 를 오늘 아직 못 채움 — req_used / req_limit).
        경험치는 음수가 될 수 있지만 레벨 안에서 0 아래로는 내려가지 않는다(레벨 다운 없음).
        """
        max_level, limit, _ = GRIND_RULES[table]
        day = (now_ts + 9 * 3600) // 86400  # KST 날짜 키
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    con.execute("BEGIN IMMEDIATE;")
                    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (user_id,))
                    con.execute(f"INSERT OR IGNORE INTO {table}(user_id, last_play_ts) VALUES(?, 0)", (user_id,))
                    level, xp, day_key, used, last, bonus_day, bonus = con.execute(
                        f"SELECT level, xp, day_key, day_count, last_play_ts, bonus_day, bonus_count FROM {table} "
                        "WHERE user_id=?", (user_id,)
                    ).fetchone()
                    if day_key != day:
                        used = 0
                    cap = limit + (int(bonus) if bonus_day == day else 0)   # 리셋권으로 늘어난 오늘 횟수
                    base = {"ok": False, "level": level, "xp": xp, "need": grind_xp_need(table, level),
                            "used": used, "limit": cap}
                    if require:
                        req_table, req_limit = require
                        row = con.execute(f"SELECT day_key, day_count FROM {req_table} WHERE user_id=?",
                                          (user_id,)).fetchone()
                        req_used = int(row[1]) if row and row[0] == day else 0
                        if req_used < req_limit:
                            con.execute("ROLLBACK;")
                            return {**base, "reason": "locked", "req_used": req_used, "req_limit": req_limit}
                    if used >= cap:
                        con.execute("ROLLBACK;")
                        return {**base, "reason": "limit"}
                    if now_ts - last < cooldown_sec:
                        con.execute("ROLLBACK;")
                        return {**base, "reason": "cooldown", "remaining": cooldown_sec - (now_ts - last)}

                    delta, xp_gain, info = roll(level, con)
                    xp = max(0, xp + int(xp_gain))
                    leveled = 0
                    while level < max_level and xp >= grind_xp_need(table, level):
                        xp -= grind_xp_need(table, level)
                        level += 1
                        leveled += 1
                    if level >= max_level:
                        xp = 0
                    used += 1

                    con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (int(delta), user_id))
                    con.execute(
                        f"UPDATE {table} SET level=?, xp=?, day_key=?, day_count=?, last_play_ts=? WHERE user_id=?",
                        (level, xp, day, used, now_ts, user_id),
                    )
                    new_bal = con.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,)).fetchone()[0]
                    con.execute("COMMIT;")
                    return {"ok": True, "level": level, "xp": xp, "need": grind_xp_need(table, level),
                            "used": used, "limit": cap, "leveled": leveled,
                            "new_bal": int(new_bal), "delta": int(delta), "info": info}
                except Exception:
                    try:
                        con.execute("ROLLBACK;")
                    except Exception:
                        pass
                    raise
                finally:
                    con.close()

            return await self._run(work)
