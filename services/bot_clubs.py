# services/bot_clubs.py — 3.7.0 🤖 봇 구단: 이적시장 · 트레이드가 조용해서 늘 사고파는 상대를 둔다.
# 봇은 유저처럼 지갑(wallets)과 보유 선수(pm_holdings)를 갖는다 — user_id 는 BOT_IDS (디스코드 ID 와 겹치지 않는 작은 수).
# 하는 일 (시장 열린 시간, 10분마다 봇마다 확률로):
#   · 매물 사기: 이적시장 매물 중 성격에 맞고 가격이 맞으면 산다 (올린 유저는 상대가 없어도 팔린다)
#   · 선수 팔기: 가진 선수를 이적시장에 올린다
#   · 이적 제안: 유저가 가진 선수를 현금으로 사겠다고 트레이드 제안 (유저는 /트레이드목록 에서 수락 · 거절)
# 봇 돈은 예산 안에서만 — 매주 월요일 BUDGET_WEEKLY 를 받고 BUDGET_CAP 을 넘지 않는다 (돈을 무한히 찍지 않는다).
import random
import sqlite3
import time
from typing import Optional

BOT_CLUBS = {   # id → (엠블럼, 이름, 성격)
    101: ("🦁", "로열 레온 FC", "rich"),
    102: ("🦅", "아퀼라 유나이티드", "rich"),
    103: ("🌱", "그린필드 아카데미", "seller"),
    104: ("🐺", "볼프스 유스", "seller"),
    105: ("🪙", "코인 시티", "bargain"),
    106: ("🦊", "폭스헤이븐", "bargain"),
    107: ("⚓", "포트 마리너스", "needy"),
    108: ("🌋", "볼케이노 SC", "needy"),
    109: ("🏔️", "알파인 레인저스", "seller"),
    110: ("🌊", "오션사이드 FC", "rich"),
}
BOT_IDS = tuple(BOT_CLUBS)
PERSONALITY = {   # 성격 → 사는 조건 · 사는 최고가(기준가 대비) · 파는 가격(기준가 대비) · 제안 가격(시세 대비)
    "rich":    {"label": "부자 구단", "min_ovr": 82, "max_age": 99, "buy_max": 1.30, "sell": (1.25, 1.45), "offer": (1.10, 1.30)},
    "seller":  {"label": "셀링 구단", "min_ovr": 0,  "max_age": 23, "buy_max": 1.00, "sell": (1.15, 1.35), "offer": (1.00, 1.12)},
    "bargain": {"label": "알뜰 구단", "min_ovr": 0,  "max_age": 99, "buy_max": 0.85, "sell": (1.05, 1.20), "offer": (0.95, 1.05)},
    "needy":   {"label": "급한 구단", "min_ovr": 70, "max_age": 31, "buy_max": 1.15, "sell": (1.10, 1.30), "offer": (1.05, 1.20)},
}
START_BUDGET = 200_000_000
BUDGET_WEEKLY, BUDGET_CAP = 50_000_000, 300_000_000
START_PLAYERS = 8
ACT_PROB = 0.35              # 틱마다 봇 하나가 움직일 확률
OFFER_PROB = 0.12            # 움직일 때 이적 제안까지 할 확률
LIST_MAX = 3                 # 봇 하나가 동시에 올려 두는 매물 수
OFFER_COOLDOWN = 86400       # 같은 유저에게 봇 제안은 하루 한 번까지
SQUAD_MAX = 18               # 봇이 이보다 많이 가지면 사지 않는다


def is_bot(uid) -> bool:
    try:
        return int(uid) in BOT_CLUBS
    except (TypeError, ValueError):
        return False


def bot_name(uid: int) -> str:
    e, n, _ = BOT_CLUBS[int(uid)]
    return f"{e} {n}"


def week_key(ts: int) -> int:
    return (int(ts) + 9 * 3600) // 86400 // 7


def init_tables(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS bot_club_state (bot_id INTEGER PRIMARY KEY, week INTEGER NOT NULL DEFAULT 0)")
    con.execute("CREATE TABLE IF NOT EXISTS bot_offer_log (bot_id INTEGER, user_id INTEGER, player_id TEXT, ts INTEGER)")
    con.execute("CREATE INDEX IF NOT EXISTS bot_offer_log_user ON bot_offer_log(user_id, ts)")


def bootstrap(con, now_ts: int, rng=random) -> int:
    """봇 구단을 처음 만든다 — 지갑 START_BUDGET · 아무도 안 가진 선수 START_PLAYERS 명. → 새로 만든 봇 수."""
    init_tables(con)
    made = 0
    for bid in BOT_IDS:
        if con.execute("SELECT 1 FROM bot_club_state WHERE bot_id=?", (bid,)).fetchone():
            continue
        con.execute("INSERT INTO bot_club_state(bot_id, week) VALUES(?, ?)", (bid, week_key(now_ts)))
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (bid,))
        con.execute("UPDATE wallets SET balance=? WHERE user_id=?", (START_BUDGET, bid))
        free = [r[0] for r in con.execute(
            "SELECT player_id FROM pm_players WHERE retired=0 AND player_id NOT LIKE 'AMT_%' "
            "AND player_id NOT IN (SELECT player_id FROM pm_holdings WHERE qty>0) "
            "AND player_id NOT IN (SELECT player_id FROM pm_listings WHERE status='active')")]
        for pid in rng.sample(free, min(START_PLAYERS, len(free))):
            con.execute("INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(?,?,1) "
                        "ON CONFLICT(user_id, player_id) DO UPDATE SET qty=qty+1", (bid, pid))
        made += 1
    return made


def weekly_budget(con, now_ts: int) -> None:
    """월요일마다 봇 예산 +BUDGET_WEEKLY (BUDGET_CAP 까지)."""
    init_tables(con)
    wk = week_key(now_ts)
    for bid, last in con.execute("SELECT bot_id, week FROM bot_club_state").fetchall():
        if int(last) >= wk:
            continue
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (bid,))
        con.execute("UPDATE wallets SET balance=MIN(?, balance + ?) WHERE user_id=?",
                    (BUDGET_CAP, BUDGET_WEEKLY * (wk - int(last)), bid))
        con.execute("UPDATE bot_club_state SET week=? WHERE bot_id=?", (wk, bid))


def balance(con, uid: int) -> int:
    r = con.execute("SELECT balance FROM wallets WHERE user_id=?", (int(uid),)).fetchone()
    return int(r[0]) if r else 0


def squad_size(con, bid: int) -> int:
    return int(con.execute("SELECT COALESCE(SUM(qty),0) FROM pm_holdings WHERE user_id=? AND qty>0", (bid,)).fetchone()[0])


def fits(kind: str, ovr: int, age: int) -> bool:
    p = PERSONALITY[kind]
    return int(ovr) >= p["min_ovr"] and int(age) <= p["max_age"]


def pick_listing(con, bid: int, now_ts: int, allowed_seller=None, rng=random) -> Optional[tuple]:
    """봇이 살 만한 매물 → (listing_id, price_per) 또는 None. allowed_seller(seller_id) 가 False 면 건너뛴다 (테스트 서버 먼저)."""
    kind = BOT_CLUBS[bid][2]
    if squad_size(con, bid) >= SQUAD_MAX:
        return None
    bal = balance(con, bid)
    rows = con.execute(
        "SELECT l.listing_id, l.seller_id, l.price_per, p.ovr, p.age, p.base_value FROM pm_listings l "
        "JOIN pm_players p ON p.player_id=l.player_id WHERE l.status='active' AND l.expires_at>? AND l.qty>0",
        (int(now_ts),)).fetchall()
    good = [(lid, price) for lid, seller, price, ovr, age, base in rows
            if not is_bot(seller) and (allowed_seller is None or allowed_seller(int(seller)))
            and fits(kind, ovr, age) and int(price) <= int(base) * PERSONALITY[kind]["buy_max"] and int(price) <= bal]
    return rng.choice(good) if good else None


def pick_to_list(con, bid: int, rng=random) -> Optional[tuple]:
    """봇이 이적시장에 올릴 선수 → (player_id, 가격) 또는 None."""
    kind = BOT_CLUBS[bid][2]
    if con.execute("SELECT COUNT(*) FROM pm_listings WHERE seller_id=? AND status='active'", (bid,)).fetchone()[0] >= LIST_MAX:
        return None
    rows = con.execute(
        "SELECT h.player_id, p.base_value FROM pm_holdings h JOIN pm_players p ON p.player_id=h.player_id "
        "WHERE h.user_id=? AND h.qty>0 AND p.retired=0", (bid,)).fetchall()
    if not rows:
        return None
    pid, base = rng.choice(rows)
    lo, hi = PERSONALITY[kind]["sell"]
    return pid, int(round(int(base) * rng.uniform(lo, hi), -3))


def pick_offer(con, bid: int, now_ts: int, allowed_user=None, rng=random) -> Optional[tuple]:
    """봇이 유저에게 낼 이적 제안 → (user_id, player_id, 제안 금액) 또는 None."""
    kind = BOT_CLUBS[bid][2]
    if squad_size(con, bid) >= SQUAD_MAX:
        return None
    bal = balance(con, bid)
    since = int(now_ts) - OFFER_COOLDOWN
    rows = con.execute(
        "SELECT h.user_id, h.player_id, p.ovr, p.age, COALESCE(m.price, p.base_value) FROM pm_holdings h "
        "JOIN pm_players p ON p.player_id=h.player_id LEFT JOIN pm_market m ON m.player_id=h.player_id "
        "WHERE h.qty>0 AND p.retired=0 AND h.player_id NOT LIKE 'AMT_%' "
        "AND h.user_id NOT IN (SELECT user_id FROM bot_offer_log WHERE ts>?)", (since,)).fetchall()
    lo, hi = PERSONALITY[kind]["offer"]
    good = [(uid, pid, int(round(int(price) * rng.uniform(lo, hi), -3))) for uid, pid, ovr, age, price in rows
            if not is_bot(uid) and (allowed_user is None or allowed_user(int(uid))) and fits(kind, ovr, age)]
    good = [g for g in good if 0 < g[2] <= bal]
    return rng.choice(good) if good else None


def log_offer(con, bid: int, uid: int, pid: str, now_ts: int) -> None:
    con.execute("INSERT INTO bot_offer_log(bot_id, user_id, player_id, ts) VALUES(?,?,?,?)", (bid, uid, pid, int(now_ts)))
    con.execute("DELETE FROM bot_offer_log WHERE ts<?", (int(now_ts) - 7 * 86400,))
