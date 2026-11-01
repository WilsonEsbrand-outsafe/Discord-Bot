# services/bot_clubs.py — 3.7.0 🤖 봇 구단: 이적시장 · 트레이드를 살리는 AI 감독 10명.
# 규완님: "봇은 이적시장 활성화에만 도움 주면 돼" — 스스로 경기 · 팩은 안 하고 랭킹에도 안 나온다.
# 봇은 지갑(wallets) · 보유 선수(pm_holdings) · 선발 명단(club_lineup, 누굴 팔지 고르는 데만)을 가진다.
# user_id 는 BOT_IDS (디스코드 ID 와 겹치지 않는 작은 수). 공식경기는 상대로만 나온다 (규완님 11/02) — 순위 · 랭킹엔 안 나온다.
#
# 움직이는 방식 (사람처럼):
#   · 감독마다 접속하는 시간대가 있고(아침형 · 저녁형 · 올빼미형 …), 접속 간격도 들쭉날쭉하다 · 쉬는 날도 있다.
#   · 접속하면 할 만한 일만 한다 (아무것도 안 하고 나갈 때도 많다):
#       1) 받은 트레이드 검토 — 받는 가치가 주는 가치(선발이면 더 아낀다)보다 크면 수락,
#          조금 모자라면 역제안, 많이 모자라면 거절 (감독 말투로 DM)
#       2) 보강 — 가장 약한 포지션을 확실히 올려 줄 선수를 가성비로 (아무도 안 가진 선수 · 유저 매물 ·
#          유저가 가진 선수가 훨씬 낫다면 직접 이적 제안)
#       3) 싸게 나온 매물은 필요 없어도 사 뒀다가 되판다 (알뜰 · 부자 구단)
#       4) 정리 — 남는 · 늙은 선수를 시세보다 조금 비싸게 내놓고, 오래 안 팔린 매물은 내린다
#   · 성향(부자 · 셀링 · 알뜰 · 급한 구단)마다 노리는 선수(나이 · OVR) · 쓰는 돈 · 파는 값이 다르다.
# 돈은 하루 일과 수입(DAILY_INCOME, BUDGET_CAP 까지) · 선수를 판 돈이 전부 — 돈을 무한히 찍지 않는다.
import random
from typing import Optional

BOT_CLUBS = {   # id → (엠블럼, 이름, 성향, 포메이션, 생활 패턴)
    101: ("🦁", "로열 레온 FC", "rich", "4-3-3", "evening"),
    102: ("🦅", "아퀼라 유나이티드", "rich", "4-2-3-1", "night"),
    103: ("🌱", "그린필드 아카데미", "seller", "4-4-2", "morning"),
    104: ("🐺", "볼프스 유스", "seller", "4-3-3", "evening"),
    105: ("🪙", "코인 시티", "bargain", "4-4-2", "office"),
    106: ("🦊", "폭스헤이븐", "bargain", "3-5-2", "night"),
    107: ("⚓", "포트 마리너스", "needy", "5-3-2", "morning"),
    108: ("🌋", "볼케이노 SC", "needy", "4-2-3-1", "evening"),
    109: ("🏔️", "알파인 레인저스", "seller", "3-4-3", "office"),
    110: ("🌊", "오션사이드 FC", "rich", "4-4-2", "night"),
}
BOT_IDS = tuple(BOT_CLUBS)
PERSONALITY = {
    # min_ovr/max_age: 노리는 선수 · spend: 한 명에 쓰는 돈(잔액 대비) · pay: 시세 대비 최고 지불 · gain: 이 정도 좋아져야 산다
    # sell: 내놓는 값(시세 대비) · offer: 이적 제안 값(시세 대비) · keep: 내 선수 아끼는 정도 · squad: 목표 인원(아마추어 빼고)
    "rich":    {"label": "부자 구단", "min_ovr": 78, "max_age": 31, "spend": 0.45, "pay": 1.30, "gain": 3,
                "sell": (1.15, 1.35), "offer": (1.15, 1.35), "keep": 1.25, "squad": 18, "act": 0.85},
    "seller":  {"label": "셀링 구단", "min_ovr": 0, "max_age": 23, "spend": 0.25, "pay": 1.05, "gain": 2,
                "sell": (1.20, 1.45), "offer": (1.00, 1.15), "keep": 1.05, "squad": 14, "act": 0.75},
    "bargain": {"label": "알뜰 구단", "min_ovr": 0, "max_age": 33, "spend": 0.20, "pay": 0.92, "gain": 2,
                "sell": (1.05, 1.20), "offer": (0.95, 1.05), "keep": 1.10, "squad": 15, "act": 0.6},
    "needy":   {"label": "급한 구단", "min_ovr": 70, "max_age": 30, "spend": 0.40, "pay": 1.20, "gain": 1,
                "sell": (1.10, 1.25), "offer": (1.10, 1.25), "keep": 1.15, "squad": 16, "act": 0.9},
}
# 생활 패턴: (KST 시작시, 끝시) 접속 시간대 목록 · 평균 접속 간격(분)
SCHEDULES = {
    "morning": (((7, 10), (12, 13), (19, 22)), 75),
    "evening": (((12, 13), (18, 24)), 60),
    "night":   (((14, 16), (21, 24), (0, 3)), 70),
    "office":  (((8, 9), (12, 13), (18, 21)), 90),
}
START_BUDGET = 200_000_000
BUDGET_CAP = 300_000_000
DAILY_INCOME = (4_000_000, 9_000_000)   # 하루 일과로 버는 돈 (그날 첫 접속 때) — 부지런함(act)을 곱한다
REST_PROB = 0.12             # 쉬는 날 (하루 종일 접속 안 함)
COUNTER_MIN = 0.80           # 받는 가치가 주는 가치의 이만큼은 되면 거절 대신 역제안
LINES = {   # 감독 말투 — 제안 · 수락 · 거절 · 역제안
    "rich":    {"offer": ("우리 구단에 딱 필요한 선수예요. 돈은 걱정 마세요.", "이 정도면 충분히 후한 조건일 겁니다."),
                "accept": ("좋습니다, 거래하죠.", "환영합니다! 좋은 거래였어요."),
                "reject": ("이 조건으론 어렵겠네요.", "우리 핵심 선수라 그 값엔 못 보내요."),
                "counter": ("조금만 더 얹어 주시면 바로 사인할게요.",)},
    "seller":  {"offer": ("이 선수, 우리 팀에서 더 크게 키워 볼게요!", "어린 선수 좋아하시나요? 저희는 좋아합니다."),
                "accept": ("거래 성사! 잘 키워 주세요.", "좋은 값이네요, 받겠습니다."),
                "reject": ("아직 더 오를 선수라서요…", "그 값이면 안 팔래요."),
                "counter": ("이 값이면 바로 보내 드릴게요.",)},
    "bargain": {"offer": ("예산이 빠듯해서… 이 정도로 어떠세요?", "싸게 사서 오래 쓰는 게 저희 철학입니다."),
                "accept": ("좋아요, 이 값이면 남는 장사네요.", "콜!"),
                "reject": ("음… 그건 좀 비싸네요.", "다음에 더 좋은 조건으로 오세요!"),
                "counter": ("여기서 조금만 맞춰 주시죠.",)},
    "needy":   {"offer": ("급해요! 이 자리 꼭 메워야 해서요.", "이번 주 경기 전에 꼭 데려오고 싶어요!"),
                "accept": ("살았다! 고맙습니다.", "바로 선발로 쓸게요!"),
                "reject": ("지금 그 선수 빠지면 우리 팀 무너져요.", "미안하지만 안 되겠어요."),
                "counter": ("이 조건이면 지금 바로 할게요!",)},
}
START_PLAYERS = 11
LIST_MAX = 3                 # 동시에 올려 두는 매물 수
LIST_STALE = 36 * 3600       # 이만큼 안 팔린 매물은 내린다
OFFER_COOLDOWN = 86400       # 같은 유저에게 봇 제안은 하루 한 번까지 (봇 전체)
SESSION_MAX = 3              # 한 번 접속해서 하는 일 (트레이드 응답 · 명단 정리 빼고)
FORMATION_NEED = {}          # 포메이션 → {포지션: 선발 수} (아래에서 채운다)
_GROUP = {"GK": "GK", "LB": "DF", "CB": "DF", "RB": "DF", "LWB": "DF", "RWB": "DF", "LM": "MF", "CM": "MF",
          "RM": "MF", "DM": "MF", "AM": "MF", "LW": "FW", "RW": "FW", "ST": "FW"}


def _formation_need(formation: str) -> dict:
    if formation not in FORMATION_NEED:
        from services.club_db import FORMATIONS
        need = {"GK": 0, "DF": 0, "MF": 0, "FW": 0}
        for s in FORMATIONS[formation]:
            need[_GROUP[s]] += 1
        FORMATION_NEED[formation] = need
    return FORMATION_NEED[formation]


def is_bot(uid) -> bool:
    try:
        return int(uid) in BOT_CLUBS
    except (TypeError, ValueError):
        return False


def bot_name(uid: int) -> str:
    e, n, *_ = BOT_CLUBS[int(uid)]
    return f"{e} {n}"


def kind(bid: int) -> str:
    return BOT_CLUBS[int(bid)][2]


def week_key(ts: int) -> int:
    return (int(ts) + 9 * 3600) // 86400 // 7


def _kst_hour(ts: float) -> float:
    return ((int(ts) + 9 * 3600) % 86400) / 3600


def online(bid: int, ts: float) -> bool:
    h = _kst_hour(ts)
    return any(a <= h < b for a, b in SCHEDULES[BOT_CLUBS[bid][4]][0])


def kst_day(ts: float) -> int:
    return (int(ts) + 9 * 3600) // 86400


def rest_day(bid: int, day: int) -> bool:
    """쉬는 날 — 감독 · 날짜로 정해진다 (같은 날은 늘 같은 답)."""
    return random.Random(bid * 100_003 + day).random() < REST_PROB


def next_visit(bid: int, now: int, rng=random) -> int:
    """다음 접속 시각 — 접속 시간대 안이면 평균 간격의 들쭉날쭉한 간격, 밖이면 다음 시간대 시작 무렵. 쉬는 날은 건너뛴다."""
    windows, mean = SCHEDULES[BOT_CLUBS[bid][4]]
    gap = int(max(15, rng.expovariate(1 / mean)) * 60)
    t = now + gap
    if online(bid, t) and not rest_day(bid, kst_day(t)):
        return t
    for step in range(1, 8 * 24 * 4):   # 15분씩 앞으로 — 다음 시간대 시작 + 0~40분
        u = now + step * 900
        if online(bid, u) and not rest_day(bid, kst_day(u)):
            j = u + rng.randint(0, 40 * 60)
            return j if online(bid, j) else u
    return t


def init_tables(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS bot_club_state (bot_id INTEGER PRIMARY KEY, week INTEGER NOT NULL DEFAULT 0, "
                "next_ts INTEGER NOT NULL DEFAULT 0)")
    try:
        con.execute("ALTER TABLE bot_club_state ADD COLUMN next_ts INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    try:
        con.execute("ALTER TABLE bot_club_state ADD COLUMN day INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    con.execute("CREATE TABLE IF NOT EXISTS bot_offer_log (bot_id INTEGER, user_id INTEGER, player_id TEXT, ts INTEGER)")
    con.execute("CREATE INDEX IF NOT EXISTS bot_offer_log_user ON bot_offer_log(user_id, ts)")
    con.execute("CREATE TABLE IF NOT EXISTS bot_trade_seen (trade_id INTEGER PRIMARY KEY, ts INTEGER)")


def _owned_sql() -> str:
    from services.player_market_db import _OWNED_SQL
    return _OWNED_SQL


def ensure_club(con, bid: int, now_ts: int) -> None:
    """봇의 구단 (공식경기 상대로만 나온다 — 봇이 먼저 경기를 걸지는 않고, 순위 · 랭킹엔 안 나온다) · 아마추어 스쿼드."""
    e, name, _k, formation, _s = BOT_CLUBS[bid]
    if not con.execute("SELECT 1 FROM clubs WHERE user_id=?", (bid,)).fetchone():
        con.execute("INSERT INTO clubs(user_id, club_name, created_ts) VALUES(?, ?, ?)", (bid, name, int(now_ts)))
    con.execute("UPDATE clubs SET emblem=?, formation=?, club_name=? WHERE user_id=?", (e, formation, name, bid))
    from services.player_market_db import AMATEUR_SQUAD
    for p in AMATEUR_SQUAD:
        con.execute("INSERT OR IGNORE INTO pm_holdings(user_id, player_id, qty) VALUES(?, ?, 1)", (bid, p["player_id"]))


def bootstrap(con, now_ts: int, rng=random) -> int:
    """봇 구단을 처음 만든다 — 지갑 START_BUDGET · 구단 · 아무도 안 가진 선수 START_PLAYERS 명. → 새로 만든 봇 수."""
    init_tables(con)
    made = 0
    for bid in BOT_IDS:
        ensure_club(con, bid, now_ts)
        if con.execute("SELECT 1 FROM bot_club_state WHERE bot_id=?", (bid,)).fetchone():
            continue
        con.execute("INSERT INTO bot_club_state(bot_id, week, next_ts) VALUES(?, ?, ?)",
                    (bid, week_key(now_ts), next_visit(bid, int(now_ts), rng)))
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (bid,))
        con.execute("UPDATE wallets SET balance=? WHERE user_id=?", (START_BUDGET, bid))
        p = PERSONALITY[kind(bid)]
        free = [r[0] for r in con.execute(
            f"SELECT player_id FROM pm_players WHERE retired=0 AND player_id NOT LIKE 'AMT_%' AND age<=? AND ovr>=? "
            f"AND player_id NOT IN ({_owned_sql()})", (p["max_age"], min(p["min_ovr"], 72)))]
        for pid in rng.sample(free, min(START_PLAYERS, len(free))):
            con.execute("INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(?,?,1) "
                        "ON CONFLICT(user_id, player_id) DO UPDATE SET qty=1", (bid, pid))
        set_lineup(con, bid)
        made += 1
    return made


def daily_routine(con, bid: int, now_ts: int, rng=random) -> int:
    """그날 첫 접속이면 하루 일과 수입 (BUDGET_CAP 까지) → 번 돈 (이미 했으면 0)."""
    init_tables(con)
    day = kst_day(now_ts)
    r = con.execute("SELECT day FROM bot_club_state WHERE bot_id=?", (bid,)).fetchone()
    if not r or int(r[0]) >= day:
        return 0
    con.execute("UPDATE bot_club_state SET day=? WHERE bot_id=?", (day, bid))
    bal = balance(con, bid)
    earn = max(0, min(BUDGET_CAP - bal, int(rng.uniform(*DAILY_INCOME) * PERSONALITY[kind(bid)]["act"])))
    earn = int(round(earn, -4))
    if earn:
        con.execute("UPDATE wallets SET balance=balance+? WHERE user_id=?", (earn, bid))
    return earn


def due_bots(con, now_ts: int) -> list[int]:
    init_tables(con)
    return [int(b) for (b,) in con.execute("SELECT bot_id FROM bot_club_state WHERE next_ts<=?", (int(now_ts),))]


def schedule(con, bid: int, now_ts: int, rng=random) -> None:
    con.execute("UPDATE bot_club_state SET next_ts=? WHERE bot_id=?", (next_visit(bid, int(now_ts), rng), bid))


def balance(con, uid: int) -> int:
    r = con.execute("SELECT balance FROM wallets WHERE user_id=?", (int(uid),)).fetchone()
    return int(r[0]) if r else 0


# ───────────── 구단 살펴보기 ─────────────
def squad(con, bid: int) -> list[dict]:
    rows = con.execute(
        "SELECT p.player_id, p.name, p.position, p.ovr, p.age, p.pot, COALESCE(m.price, p.base_value), p.base_value "
        "FROM pm_holdings h JOIN pm_players p ON p.player_id=h.player_id LEFT JOIN pm_market m ON m.player_id=h.player_id "
        "WHERE h.user_id=? AND h.qty>0 AND p.retired=0", (int(bid),)).fetchall()
    return [{"player_id": r[0], "name": r[1], "pos": r[2], "ovr": int(r[3]), "age": int(r[4]), "pot": int(r[5]),
             "price": int(r[6]), "base": int(r[7]), "amateur": str(r[0]).startswith("AMT_")} for r in rows]


def set_lineup(con, bid: int) -> list[str]:
    """가장 강한 11명으로 선발 명단."""
    from services.club_db import auto_assign
    formation = BOT_CLUBS[bid][3]
    placed = auto_assign(squad(con, bid), formation)
    con.execute("DELETE FROM club_lineup WHERE user_id=?", (bid,))
    con.executemany("INSERT INTO club_lineup(user_id, slot, player_id) VALUES(?, ?, ?)",
                    [(bid, i, pid) for i, pid in enumerate(placed) if pid])
    return [pid for pid in placed if pid]


def needs(con, bid: int) -> list[tuple]:
    """포지션별 가장 약한 선발 OVR → [(포지션, 약한 선발 OVR, 선발 수가 모자란가)] 약한 순."""
    sq = squad(con, bid)
    need = _formation_need(BOT_CLUBS[bid][3])
    out = []
    for pos, n in need.items():
        mine = sorted((p["ovr"] for p in sq if p["pos"] == pos), reverse=True)
        weakest = mine[n - 1] if len(mine) >= n else 0
        out.append((pos, weakest, len(mine) < n))
    return sorted(out, key=lambda x: (not x[2], x[1]))


def _fits(k: str, ovr: int, age: int) -> bool:
    p = PERSONALITY[k]
    return int(ovr) >= p["min_ovr"] and int(age) <= p["max_age"]


def _value(k: str, gain: int, age: int, pot: int, ovr: int, price: int) -> float:
    """보강 점수 — 좋아지는 정도(+어린 선수는 잠재력) 를 돈 1억당으로. 셀링 구단은 잠재력을 더 본다."""
    upside = max(0, pot - ovr) * (0.5 if k == "seller" else 0.15) if age <= 23 else 0
    return (gain + upside) / max(0.05, price / 100_000_000)


def find_upgrade(con, bid: int, now_ts: int, allowed=None) -> Optional[dict]:
    """가장 약한 자리를 확실히 올려 줄 선수 중 가성비가 가장 좋은 쪽.
    → {"how": "free"|"listing"|"offer", ...} 또는 None (돈이 없거나 살 만한 선수가 없으면)."""
    k = kind(bid)
    p = PERSONALITY[k]
    if len([x for x in squad(con, bid) if not x["amateur"]]) >= p["squad"] + 4:
        return None
    bal = balance(con, bid)
    cap = int(bal * p["spend"])
    best = None
    owned = _owned_sql()
    for pos, weakest, short in needs(con, bid)[:2]:
        want = weakest + (0 if short else p["gain"])
        cands = []
        # 아무도 안 가진 선수 (시장에서 시세로)
        for pid, ovr, age, pot, price in con.execute(
                f"SELECT p.player_id, p.ovr, p.age, p.pot, COALESCE(m.price, p.base_value) FROM pm_players p "
                f"LEFT JOIN pm_market m ON m.player_id=p.player_id WHERE p.retired=0 AND p.position=? AND p.ovr>? "
                f"AND p.player_id NOT LIKE 'AMT_%' AND p.player_id NOT IN ({owned}) ORDER BY p.ovr DESC LIMIT 60",
                (pos, want)):
            cands.append(("free", {"player_id": pid}, ovr, age, pot, int(price), int(price)))
        # 유저 매물
        for lid, seller, pid, price, ovr, age, pot, mkt in con.execute(
                "SELECT l.listing_id, l.seller_id, l.player_id, l.price_per, p.ovr, p.age, p.pot, COALESCE(m.price, p.base_value) "
                "FROM pm_listings l JOIN pm_players p ON p.player_id=l.player_id LEFT JOIN pm_market m ON m.player_id=l.player_id "
                "WHERE l.status='active' AND l.expires_at>? AND l.qty>0 AND p.position=? AND p.ovr>?",
                (int(now_ts), pos, want)):
            if is_bot(seller) or (allowed is not None and not allowed(int(seller))):
                continue
            if int(price) > int(mkt) * p["pay"]:
                continue
            cands.append(("listing", {"listing_id": lid, "seller_id": int(seller)}, ovr, age, pot, int(price), int(mkt)))
        # 유저가 가진 선수 (매물 아님) — 훨씬 좋을 때만 이적 제안
        since = int(now_ts) - OFFER_COOLDOWN
        for uid, pid, ovr, age, pot, mkt in con.execute(
                "SELECT h.user_id, h.player_id, p.ovr, p.age, p.pot, COALESCE(m.price, p.base_value) FROM pm_holdings h "
                "JOIN pm_players p ON p.player_id=h.player_id LEFT JOIN pm_market m ON m.player_id=h.player_id "
                "WHERE h.qty>0 AND p.retired=0 AND p.position=? AND p.ovr>? AND h.player_id NOT LIKE 'AMT_%' "
                "AND h.user_id NOT IN (SELECT user_id FROM bot_offer_log WHERE ts>?)", (pos, want + 3, since)):
            if is_bot(uid) or (allowed is not None and not allowed(int(uid))):
                continue
            cands.append(("offer", {"user_id": int(uid), "player_id": pid}, ovr, age, pot, int(mkt), int(mkt)))
        for how, ref, ovr, age, pot, cost, mkt in cands:
            if not _fits(k, ovr, age) or cost <= 0:
                continue
            pay = int(round(mkt * random.uniform(*p["offer"]), -3)) if how == "offer" else cost
            if pay > cap:
                continue
            score = _value(k, int(ovr) - weakest, int(age), int(pot), int(ovr), pay) * (1.25 if short else 1.0)
            if how == "offer":
                score *= 0.8   # 수락할지 모르니 조금 덜 친다
            if best is None or score > best["score"]:
                best = {"how": how, **ref, "price": pay, "score": score, "pos": pos, "ovr": int(ovr),
                        "player_id": ref.get("player_id") or con.execute(
                            "SELECT player_id FROM pm_listings WHERE listing_id=?", (ref.get("listing_id"),)).fetchone()[0]}
    return best


def pick_to_sell(con, bid: int, rng=random) -> Optional[tuple]:
    """내놓을 선수 → (player_id, 가격) 또는 None. 선발에 못 드는 남는 선수 · 늙은 선수 · (셀링 구단) 값 오른 유망주."""
    k = kind(bid)
    p = PERSONALITY[k]
    if con.execute("SELECT COUNT(*) FROM pm_listings WHERE seller_id=? AND status='active'", (bid,)).fetchone()[0] >= LIST_MAX:
        return None
    lineup = {r[0] for r in con.execute("SELECT player_id FROM club_lineup WHERE user_id=?", (bid,))}
    listed = {r[0] for r in con.execute("SELECT player_id FROM pm_listings WHERE seller_id=? AND status='active'", (bid,))}
    sq = [x for x in squad(con, bid) if not x["amateur"] and x["player_id"] not in listed]
    bench = [x for x in sq if x["player_id"] not in lineup]
    cands = []
    if len(sq) > p["squad"]:
        cands += [(x, 3 + (p["squad"] - x["ovr"] / 10)) for x in bench]
    cands += [(x, 2 + x["age"] - 31) for x in sq if x["age"] >= 32]
    if k == "seller":
        cands += [(x, 2 + x["price"] / max(1, x["base"])) for x in bench if x["price"] > x["base"] * 1.15]
    if not cands:
        return None
    x = max(cands, key=lambda c: c[1])[0]
    return x["player_id"], int(round(x["price"] * rng.uniform(*p["sell"]), -3))


def stale_listings(con, bid: int, now_ts: int) -> list[int]:
    return [int(r[0]) for r in con.execute(
        "SELECT listing_id FROM pm_listings WHERE seller_id=? AND status='active' AND listed_at<?",
        (bid, int(now_ts) - LIST_STALE))]


def judge_trade(con, bid: int, trade_id: int) -> Optional[bool]:
    """받은 트레이드 제안 → True 수락 · False 거절 · None (못 보는 제안)."""
    v = trade_value(con, bid, trade_id)
    return None if v is None else v[0] >= v[1]


def trade_value(con, bid: int, trade_id: int) -> Optional[tuple]:
    """받은 트레이드 제안 → (받는 가치, 주는 가치) · None (못 보는 제안). 돈이 모자라면 받는 가치 0.
    받는 가치 = 현금 + 받는 선수 시세 (내 약한 자리를 채우면 ×1.15, 아니면 ×0.8)
    주는 가치 = 현금 + 주는 선수 시세 × 아끼는 정도 (선발이면 ×1.2 더)."""
    t = con.execute("SELECT proposer_id, receiver_id, proposer_cash, receiver_cash, status FROM pm_trades WHERE trade_id=?",
                    (int(trade_id),)).fetchone()
    if not t or int(t[1]) != bid or t[4] != "pending":
        return None
    p = PERSONALITY[kind(bid)]
    lineup = {r[0] for r in con.execute("SELECT player_id FROM club_lineup WHERE user_id=?", (bid,))}
    weak = {pos: w for pos, w, _ in needs(con, bid)}
    get, give = int(t[2]), int(t[3])
    if give > balance(con, bid):
        return 0, max(1, give)
    for side, pid, qty, pos, ovr, age, price in con.execute(
            "SELECT ti.side, ti.player_id, ti.qty, p.position, p.ovr, p.age, COALESCE(m.price, p.base_value) "
            "FROM pm_trade_items ti JOIN pm_players p ON p.player_id=ti.player_id LEFT JOIN pm_market m ON m.player_id=ti.player_id "
            "WHERE ti.trade_id=?", (int(trade_id),)):
        v = int(price) * int(qty)
        if side == "proposer":
            useful = int(ovr) > weak.get(pos, 99) and _fits(kind(bid), ovr, age)
            get += int(v * (1.15 if useful else 0.8))
        else:
            give += int(v * p["keep"] * (1.2 if pid in lineup else 1.0))
    return get, give


def pending_trades(con, bid: int) -> list[int]:
    return [int(r[0]) for r in con.execute(
        "SELECT trade_id FROM pm_trades WHERE receiver_id=? AND status='pending' ORDER BY trade_id", (bid,))]


def log_offer(con, bid: int, uid: int, pid: str, now_ts: int) -> None:
    con.execute("INSERT INTO bot_offer_log(bot_id, user_id, player_id, ts) VALUES(?,?,?,?)", (bid, uid, pid, int(now_ts)))
    con.execute("DELETE FROM bot_offer_log WHERE ts<?", (int(now_ts) - 7 * 86400,))


def trade_items(con, trade_id: int) -> tuple:
    """(제안자 선수 목록, 수신자 선수 목록, 제안자 현금, 수신자 현금, 제안자)."""
    t = con.execute("SELECT proposer_id, proposer_cash, receiver_cash FROM pm_trades WHERE trade_id=?", (int(trade_id),)).fetchone()
    prop, recv = [], []
    for side, pid, qty in con.execute("SELECT side, player_id, qty FROM pm_trade_items WHERE trade_id=?", (int(trade_id),)):
        (prop if side == "proposer" else recv).extend([pid] * int(qty))
    return prop, recv, int(t[1]), int(t[2]), int(t[0])


def line(bid: int, key: str, rng=random) -> str:
    return rng.choice(LINES[kind(bid)][key])


def find_bargain(con, bid: int, now_ts: int, allowed=None) -> Optional[dict]:
    """시세보다 한참 싸게 나온 유저 매물 (필요 없어도 사 뒀다가 되판다) — 알뜰 · 부자 구단만."""
    k = kind(bid)
    if k not in ("bargain", "rich"):
        return None
    cap = int(balance(con, bid) * 0.15)
    best = None
    for lid, seller, pid, price, mkt in con.execute(
            "SELECT l.listing_id, l.seller_id, l.player_id, l.price_per, COALESCE(m.price, p.base_value) FROM pm_listings l "
            "JOIN pm_players p ON p.player_id=l.player_id LEFT JOIN pm_market m ON m.player_id=l.player_id "
            "WHERE l.status='active' AND l.expires_at>? AND l.qty>0 AND p.retired=0", (int(now_ts),)):
        if is_bot(seller) or (allowed is not None and not allowed(int(seller))):
            continue
        if int(price) <= int(mkt) * 0.8 and int(price) <= cap:
            d = int(mkt) / max(1, int(price))
            if best is None or d > best["deal"]:
                best = {"how": "listing", "listing_id": int(lid), "seller_id": int(seller), "player_id": pid,
                        "price": int(price), "deal": d}
    return best
