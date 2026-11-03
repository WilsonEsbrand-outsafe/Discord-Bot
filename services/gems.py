# services/gems.py — 3.7.2 💎 원석 (release.preview_372 서버의 /스카우트)
# 규완님: 스카우트는 돈을 바로 주지 않고, 스카우트로만 얻는 새 재화를 준다 — 돈으로 환전되면서 그것만의 가치가 있는 것.
# 원석은 5종류. 종류마다 시세가 기준가의 50%~150% 안에서 10분마다 따로 오르내린다 (기준가 쪽으로 조금씩 돌아오는 랜덤 워크).
# /원석 에서 지금 시세로 돈으로 환전하거나 💠 세공(낮은 원석 2개 → 윗 단계 도전)한다. 종류마다 가질 수 있는 개수 제한이 있다.
import random
from typing import Optional

# key → (이모지, 이름, 기준가, 스카우트 한 번에 나올 비중, 가질 수 있는 최대 개수)
GEMS = {
    "rough":    ("🪨", "원석",        12_000, 46, 1000),
    "emerald":  ("🟢", "에메랄드",    30_000, 30, 500),
    "sapphire": ("🔵", "사파이어",    75_000, 15, 250),
    "ruby":     ("🔴", "루비",       180_000, 6, 120),
    "diamond":  ("💎", "다이아몬드", 450_000, 3, 60),
}
ORDER = tuple(GEMS)
PER_SCOUT = (2, 4)                   # 3.7.2 규완님 "한 번에 여러 개" — 스카우트 한 번에 2~4개 (+ 레벨 − 1)
PRICE_LO, PRICE_HI = 0.5, 1.5        # 시세 범위 (기준가 배수)
TICK = 600                           # 10분마다 시세가 바뀐다
VOL, PULL = 0.08, 0.10               # 한 번에 움직이는 폭(표준편차) · 기준가로 돌아오는 힘
MAX_CATCHUP = 288                    # 오래 아무도 안 봤으면 최근 이틀치만 굴린다


# 💠 세공: 같은 원석 CRAFT_COST 개 → 한 단계 위 원석 하나에 도전. 실패하면 재료는 사라진다.
# 가끔(CRAFT_JACKPOT) 두 단계 위가 나온다 (다이아몬드 바로 아래면 그냥 성공).
CRAFT_COST = 2
CRAFT_CHANCE = {"rough": 0.75, "emerald": 0.72, "sapphire": 0.70, "ruby": 0.68}
CRAFT_JACKPOT = 0.03


def cap(key: str) -> int:
    return GEMS[key][4]


def label(key: str) -> str:
    e, n = GEMS[key][0], GEMS[key][1]
    return f"{e} {n}"


def roll_gems(level: int, rng=random, single: bool = False) -> dict:
    """스카우트 한 번 → {종류: 개수}. 한 번에 여러 개: 2~4개 + (레벨 − 1) (Lv.1 2~4개 … Lv.5 6~8개).
    single (3.7.3, release.ctx_373): 규완님 "종류가 여러 개가 아니라" — 한 종류만 골라 그 원석을 여러 개."""
    n = rng.randint(*PER_SCOUT) + max(1, int(level)) - 1
    if single:
        return {rng.choices(ORDER, weights=[GEMS[g][3] for g in ORDER])[0]: n}
    out: dict = {}
    for _ in range(n):
        k = rng.choices(ORDER, weights=[GEMS[g][3] for g in ORDER])[0]
        out[k] = out.get(k, 0) + 1
    return out


def expected_value(level: int = 1) -> float:
    tot = sum(GEMS[g][3] for g in ORDER)
    return (sum(PER_SCOUT) / 2 + max(1, int(level)) - 1) * sum(GEMS[g][2] * GEMS[g][3] for g in ORDER) / tot


def step(price: int, base: int, rng=random) -> int:
    p = price + (base - price) * PULL + price * rng.gauss(0, VOL)
    p = max(base * PRICE_LO, min(base * PRICE_HI, p))
    return int(round(p, -2))


# ── 저장 ──
def init_tables(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS gem_bag (user_id INTEGER, gem TEXT, qty INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY(user_id, gem))")
    con.execute("CREATE TABLE IF NOT EXISTS gem_prices (gem TEXT PRIMARY KEY, price INTEGER NOT NULL, prev INTEGER NOT NULL, "
                "tick INTEGER NOT NULL)")


def prices(con, now_ts: int, rng=random) -> dict:
    """{종류: (지금 시세, 직전 시세)} — 볼 때마다 지난 10분 칸만큼 시세를 굴려 저장한다."""
    init_tables(con)
    tick = int(now_ts) // TICK
    out = {}
    for g in ORDER:
        base = GEMS[g][2]
        row = con.execute("SELECT price, prev, tick FROM gem_prices WHERE gem=?", (g,)).fetchone()
        if row is None:
            price, prev, last = base, base, tick
            con.execute("INSERT INTO gem_prices(gem, price, prev, tick) VALUES(?,?,?,?)", (g, price, prev, tick))
        else:
            price, prev, last = int(row[0]), int(row[1]), int(row[2])
        if tick > last:
            for _ in range(min(tick - last, MAX_CATCHUP)):
                prev, price = price, step(price, base, rng)
            con.execute("UPDATE gem_prices SET price=?, prev=?, tick=? WHERE gem=?", (price, prev, tick, g))
        out[g] = (price, prev)
    return out


def next_tick_ts(now_ts: int) -> int:
    return (int(now_ts) // TICK + 1) * TICK


def bag(con, uid: int) -> dict:
    init_tables(con)
    rows = con.execute("SELECT gem, qty FROM gem_bag WHERE user_id=? AND qty>0", (int(uid),)).fetchall()
    return {g: int(q) for g, q in rows if g in GEMS}


def give(con, uid: int, gems: dict) -> dict:
    """원석 지급 — 종류마다 최대 개수(cap)까지만 담고, 실제로 담긴 개수 {종류: n} 와 못 담은 개수를 돌려준다."""
    init_tables(con)
    have = bag(con, uid)
    got, lost = {}, {}
    for g, n in gems.items():
        room = max(0, cap(g) - have.get(g, 0))
        k = min(int(n), room)
        if k:
            con.execute("INSERT INTO gem_bag(user_id, gem, qty) VALUES(?,?,?) "
                        "ON CONFLICT(user_id, gem) DO UPDATE SET qty = qty + excluded.qty", (int(uid), g, k))
            got[g] = k
        if int(n) > k:
            lost[g] = int(n) - k
    return {"got": got, "lost": lost}


def craft(con, uid: int, gem: str, times: int = 1, rng=random) -> dict:
    """gem 을 CRAFT_COST 개씩 써서 윗 단계에 times 번 도전 → {ok, tries, made: {종류: n}, fail, used, reason}.
    reason: top(다이아몬드는 세공 불가) · few(재료 부족) · full(윗 단계가 가득 참)."""
    init_tables(con)
    if gem not in CRAFT_CHANCE:
        return {"ok": False, "reason": "top"}
    i = ORDER.index(gem)
    up, up2 = ORDER[i + 1], ORDER[min(i + 2, len(ORDER) - 1)]
    have = bag(con, uid)
    tries = min(int(times), have.get(gem, 0) // CRAFT_COST)
    if tries <= 0:
        return {"ok": False, "reason": "few", "have": have.get(gem, 0)}
    made, fail, done = {}, 0, 0
    for _ in range(tries):
        if have.get(up, 0) + made.get(up, 0) >= cap(up) and (up2 == up or have.get(up2, 0) + made.get(up2, 0) >= cap(up2)):
            break
        done += 1
        r = rng.random()
        if r < CRAFT_CHANCE[gem]:
            out = up2 if (r < CRAFT_JACKPOT and up2 != up and have.get(up2, 0) + made.get(up2, 0) < cap(up2)) else up
            if have.get(out, 0) + made.get(out, 0) >= cap(out):
                out = up2 if out == up else up
            made[out] = made.get(out, 0) + 1
        else:
            fail += 1
    if done == 0:
        return {"ok": False, "reason": "full", "up": up}
    used = done * CRAFT_COST
    con.execute("UPDATE gem_bag SET qty = qty - ? WHERE user_id=? AND gem=?", (used, int(uid), gem))
    give(con, uid, made)
    return {"ok": True, "tries": done, "made": made, "fail": fail, "used": used, "gem": gem, "up": up}


# 3.7.2 규완님: 원석은 돈으로도 살 수 있다 (지금 시세 그대로) — 다이아몬드만 못 산다 (스카우트 · 세공으로만)
BUYABLE = ("rough", "emerald", "sapphire", "ruby")
BUY_STEPS = (1, 10, 100)


def buy(con, uid: int, gem: str, qty: int, now_ts: int) -> dict:
    """gem 을 지금 시세로 qty 개 산다 (보유 한도 · 잔액만큼까지 줄여서). qty <= 0 이면 살 수 있는 만큼.
    → {ok, gem, n, price, total, balance} / 실패 reason: no(다이아몬드 등) · full(가득 참) · money(잔액 부족)."""
    if gem not in BUYABLE:
        return {"ok": False, "reason": "no"}
    price = prices(con, now_ts)[gem][0]
    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(uid),))
    bal = int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (int(uid),)).fetchone()[0])
    room = cap(gem) - bag(con, uid).get(gem, 0)
    if room <= 0:
        return {"ok": False, "reason": "full", "price": price}
    afford = max(0, bal) // price
    if afford <= 0:
        return {"ok": False, "reason": "money", "price": price, "balance": bal}
    n = min(room, afford, int(qty) if int(qty) > 0 else room)
    total = n * price
    con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (total, int(uid)))
    give(con, uid, {gem: n})
    return {"ok": True, "gem": gem, "n": n, "price": price, "total": total, "balance": bal - total}


def sell(con, uid: int, gem: Optional[str], now_ts: int) -> dict:
    """gem 종류를 전부 (None 이면 모든 종류를) 지금 시세로 환전 → {sold: {종류: (개수, 시세)}, total, balance}."""
    p = prices(con, now_ts)
    have = bag(con, uid)
    keys = [gem] if gem else list(have)
    sold, total = {}, 0
    for g in keys:
        n = have.get(g, 0)
        if n <= 0:
            continue
        sold[g] = (n, p[g][0])
        total += n * p[g][0]
        con.execute("UPDATE gem_bag SET qty=0 WHERE user_id=? AND gem=?", (int(uid), g))
    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(uid),))
    if total:
        con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (total, int(uid)))
    bal = con.execute("SELECT balance FROM wallets WHERE user_id=?", (int(uid),)).fetchone()[0]
    return {"sold": sold, "total": total, "balance": int(bal)}
