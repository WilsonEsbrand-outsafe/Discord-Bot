# services/burning.py — 3.9.1 (코드 이름 384) 수능 기념 버닝 이벤트 (11/21 규완님 '대수능 기념 2026-11-21 ~ 2026-11-30 00:00까지
# 버닝 이벤트, ex) 농사 버프 등등 효과 부여, FC 온라인 버닝 참고'). 기간 동안 아래 효과가 모두에게 (공개 전엔 테스트 서버 사람만).
# 11/21 규완님 '수능 기념 버닝 이벤트에 /낚시를 넣고, 플레이타임이나 특정 조건을 달성할 때마다 보상' → 낚시 효과 + 버닝 미션 (/버닝).
# 효과 종류 · 세기, 미션 · 보상은 내 기본값.
import calendar
import sqlite3
import time
from typing import Optional

import release

START = calendar.timegm((2026, 11, 20, 15, 0, 0))   # 2026-11-21 00:00 KST
END = calendar.timegm((2026, 11, 29, 15, 0, 0))     # 2026-11-30 00:00 KST
NAME = "수능 기념 버닝 이벤트"
BUFFS = {   # key → 배수
    "farm_grow": 2.0,      # 농작물 두 배 빨리 자람
    "farm_energy": 2.0,    # 농사 에너지 두 배 빨리 참
    "farm_sell": 1.5,      # 농사 판매 코인 1.5배
    "attend": 2.0,         # 출석 보상 두 배
    "fish_sell": 1.5,      # 물고기 판매 1.5배
    "fish_xp": 2.0,        # 낚시 경험치 두 배
}
TEXT = ("농작물이 두 배 빨리 자라요", "농사 에너지가 두 배 빨리 차요", "농사 판매 코인 1.5배", "출석 보상 두 배",
        "물고기 판매 1.5배", "낚시 경험치 두 배")

# ───────────── 버닝 미션 ─────────────
# (키, 이름, 세는 것, 목표, 보상 돈). 세는 것: play(봇을 쓴 시간, 분) · attend(기간 중 출석한 날) · fish(낚은 물고기) · harvest(농사 거두기)
MISSIONS = (
    ("play30", "플레이 30분", "play", 30, 5_000_000),
    ("play60", "플레이 1시간", "play", 60, 10_000_000),
    ("play180", "플레이 3시간", "play", 180, 30_000_000),
    ("play300", "플레이 5시간", "play", 300, 50_000_000),
    ("attend3", "출석 3일", "attend", 3, 10_000_000),
    ("attend6", "출석 6일", "attend", 6, 30_000_000),
    ("attend9", "출석 9일 (매일)", "attend", 9, 100_000_000),
    ("fish50", "물고기 50마리 낚기", "fish", 50, 10_000_000),
    ("fish200", "물고기 200마리 낚기", "fish", 200, 30_000_000),
    ("harvest50", "농사 50번 거두기", "harvest", 50, 10_000_000),
    ("harvest200", "농사 200번 거두기", "harvest", 200, 30_000_000),
)
UNIT = {"play": "분", "attend": "일", "fish": "마리", "harvest": "번"}


def on(now: float, uid: Optional[int] = None) -> bool:
    """지금 버닝 중인가 (uid 를 주면 그 사람이 효과를 받는가 — 공개 전엔 테스트 서버 사람만)."""
    if not START <= now < END:
        return False
    if release.released_384(now):
        return True
    from services import lobby   # 테스트 서버 멤버는 /로비 루프가 채운다
    return uid is not None and int(uid) in lobby.TESTERS


def mult(key: str, uid: Optional[int], now: float) -> float:
    return BUFFS.get(key, 1.0) if on(now, uid) else 1.0


def bump(con, uid: int, key: str, n: int, now: float) -> None:
    """버닝 기간이면 그 사람의 key 를 n 만큼 센다 (같은 트랜잭션 안에서). 공개 전에도 모두 센다 — 이벤트는 11/21 00:00 부터라서
    (낚시도 기록에서 기간 안의 것을 센다). 보상을 받는 건 공개 뒤 (테스트 서버 사람은 먼저)."""
    if n <= 0 or not START <= now < END:
        return
    try:
        con.execute("INSERT INTO burn_count(user_id, key, n) VALUES(?, ?, ?) "
                    "ON CONFLICT(user_id, key) DO UPDATE SET n = n + excluded.n", (int(uid), key, int(n)))
    except sqlite3.OperationalError:   # 표가 아직 없다 (EconomyDB 가 만든다)
        pass


# 플레이 시간: / 명령어 · 버튼 · 메뉴를 쓴 '분' 수 (1분 안에 한 번이라도 쓰면 1분). bot.py 가 touch 하고 cogs/burning 이 1분마다 저장.
_SEEN: dict[int, int] = {}
_PENDING: dict[int, int] = {}


def touch(uid: int, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    if not START <= now < END:
        return
    m = int(now // 60)
    if _SEEN.get(uid) != m:
        _SEEN[uid] = m
        _PENDING[uid] = _PENDING.get(uid, 0) + 1


def take_pending() -> dict[int, int]:
    global _PENDING
    out, _PENDING = _PENDING, {}
    return out


def counts(con, uid: int) -> dict[str, int]:
    """미션에서 세는 값들. 물고기는 낚시 기록(fish_log)에서 기간 안의 것만 센다."""
    out = {k: 0 for k in UNIT}
    try:
        out.update({k: int(n) for k, n in con.execute("SELECT key, n FROM burn_count WHERE user_id=?", (int(uid),))})
    except sqlite3.OperationalError:
        pass
    try:
        out["fish"] = int(con.execute("SELECT COUNT(*) FROM fish_log WHERE user_id=? AND ts >= ? AND ts < ?",
                                      (int(uid), START, END)).fetchone()[0])
    except sqlite3.OperationalError:   # 낚시 표가 아직 없다
        pass
    return out


def status(con, uid: int) -> list[dict]:
    have = counts(con, uid)
    try:
        got = {m for (m,) in con.execute("SELECT mission FROM burn_claims WHERE user_id=?", (int(uid),))}
    except sqlite3.OperationalError:
        got = set()
    return [{"key": k, "name": name, "what": what, "goal": goal, "money": money, "value": min(have[what], goal),
             "done": have[what] >= goal, "claimed": k in got} for k, name, what, goal, money in MISSIONS]


def claim(con, uid: int, now: float) -> dict:
    """다 채운 · 안 받은 미션 보상을 한 번에 → {claimed: [이름], money, balance}. 버닝이 끝나도 3일 동안은 받을 수 있다."""
    if not can_claim(uid, now):
        return {"claimed": [], "money": 0, "balance": None}
    rows = [m for m in status(con, uid) if m["done"] and not m["claimed"]]
    money = sum(m["money"] for m in rows)
    for m in rows:
        con.execute("INSERT INTO burn_claims(user_id, mission, ts) VALUES(?, ?, ?)", (int(uid), m["key"], int(now)))
    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(uid),))
    if money:
        con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (money, int(uid)))
    bal = con.execute("SELECT balance FROM wallets WHERE user_id=?", (int(uid),)).fetchone()[0]
    return {"claimed": [m["name"] for m in rows], "money": money, "balance": int(bal)}


CLAIM_GRACE = 3 * 86400   # 끝난 뒤에도 사흘 동안은 다 채운 보상을 받을 수 있다


def can_claim(uid: int, now: float) -> bool:
    if not START <= now < END + CLAIM_GRACE:
        return False
    from services import lobby
    return release.released_384(now) or int(uid) in lobby.TESTERS


def add_play(con, pending: dict[int, int], now: float) -> None:
    """touch 로 모인 플레이 시간(분)을 저장."""
    for uid, n in pending.items():
        bump(con, uid, "play", n, now)
