# services/onboarding.py — 🎓 2.8 입문 튜토리얼 (새 유저 필수 · 약 10분)
# 2.5 가입 관문(구단생성 먼저)을 대신한다: 2.8 이 적용된 서버에서 구단이 없는 새 유저는 /튜토리얼을 끝내야 다른 명령어를 쓴다.
# 순서: 구단 → 선수단 · 자동편성 → 유망주 → 일일활동 → NPC 친선경기 → (선택) 둘러보기 → 알림 설정 → 마무리 보상 상자.
# 진행 단계는 DB 에 저장(중간에 나가도 이어서). 보상은 계정당 한 번 · 다시 보기는 언제든(실습 · 보상 없이).
# 루키 미션과 겹치는 것(구단 창단 · 첫 경기)은 튜토리얼에서 달성되고, 마무리 때 루키 보상까지 한 번에 받는다.
import json
import random
import sqlite3
import time

from services.club_db import (
    DEFAULT_FORMATION, FORMATIONS, SLOT_GROUP, auto_assign, match_highlights, simulate_match, team_rating,
)
from services.economy_db import EconomyDB, give_item, rookie_start

TARGET_MIN = 10                       # 끝까지 약 10분
# (키, 이모지, 제목, 실습 필요 여부)
STEPS = (
    ("club", "🏟️", "구단 만들기", True),
    ("squad", "👥", "선수단 · 자동편성", True),
    ("prospect", "🌟", "유망주", False),
    ("daily", "📅", "일일활동", True),
    ("match", "⚽", "첫 경기 (연습 상대)", True),
    ("tour", "🎮", "둘러보기 (선택)", False),
    ("notify", "🔔", "알림 설정", False),   # 2.9.1 — 마무리 바로 앞이라 진행 중이던 유저의 단계 번호가 밀리지 않는다
    ("finish", "🎁", "마무리 보상 상자", True),
)
STEP_KEYS = [s[0] for s in STEPS]
# 마무리 보상 상자 (계정당 한 번) — 돈은 적게, 새 기능을 바로 써 볼 수 있는 아이템 위주
FINAL_MONEY = 300_000
FINAL_ITEMS = {"muffler": 2, "bait": 5, "potion": 2, "torch": 1, "scout_reset": 1}
DEFAULT_MANAGER, DEFAULT_MEDIC = "rookie", "intern"   # 자동 설정: 초보 감독 · 인턴 트레이너 (무료)
NPC_OVR = 42                                          # 연습 상대 — 아마추어 스쿼드(약 50)보다 조금 약하게
NPC_NAME = "🐣 연습 상대 FC"


def npc_team() -> dict:
    """튜토리얼 연습 상대 (고정 · 경기 기록은 내 구단에만)."""
    rng = random.Random("npc-tutorial")
    first = ("김", "이", "박", "최", "정", "강", "조", "윤", "장", "임", "한")
    lineup = [{"slot": slot, "index": i, "player_id": f"NPC:{i}", "name": f"{first[i]}연습", "pos": SLOT_GROUP[slot],
               "ovr": NPC_OVR + rng.randint(-2, 2), "nation": "대한민국"}
              for i, slot in enumerate(FORMATIONS[DEFAULT_FORMATION])]
    team = {"name": NPC_NAME, "lineup": lineup, "captain": None}
    team.update(team_rating(lineup, None))
    return team


def gate_state(con: sqlite3.Connection, user_id: int) -> str:
    """bot.py 관문용 (동기). ok / tutorial(입문을 끝내야 함).
    구단이 있는데 입문 기록이 없는 유저(2.8 전 가입) · 2.5 전부터 있던 유저(rookie.start_ts=0)는 ok."""
    try:
        row = con.execute("SELECT done_ts FROM onboarding WHERE user_id=?", (user_id,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row:
        return "ok" if row[0] else "tutorial"
    try:
        if con.execute("SELECT 1 FROM clubs WHERE user_id=?", (user_id,)).fetchone():
            return "ok"
        r = con.execute("SELECT start_ts FROM rookie WHERE user_id=?", (user_id,)).fetchone()
        return "ok" if r and r[0] == 0 else "tutorial"
    except sqlite3.OperationalError:
        return "ok"


class OnboardingDB(EconomyDB):
    def _init_db(self):
        super()._init_db()
        con = self._connect()
        try:
            con.execute("CREATE TABLE IF NOT EXISTS onboarding (user_id INTEGER PRIMARY KEY, step INTEGER NOT NULL DEFAULT 0, "
                        "practiced TEXT NOT NULL DEFAULT '[]', started_ts INTEGER, done_ts INTEGER, rewarded INTEGER DEFAULT 0)")
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _row(con, uid) -> dict | None:
        r = con.execute("SELECT step, practiced, started_ts, done_ts, rewarded FROM onboarding WHERE user_id=?", (uid,)).fetchone()
        if not r:
            return None
        return {"step": r[0], "practiced": json.loads(r[1]), "started_ts": r[2], "done_ts": r[3], "rewarded": bool(r[4])}

    async def state(self, uid: int) -> dict:
        """{row(없으면 None), gate(ok/tutorial)}."""
        return await self._tx(lambda con: {"row": self._row(con, uid), "gate": gate_state(con, uid)})

    async def begin(self, uid: int, now_ts: int) -> dict:
        def fn(con):
            con.execute("INSERT OR IGNORE INTO onboarding(user_id, step, started_ts) VALUES(?, 0, ?)", (uid, now_ts))
            rookie_start(con, uid, now_ts)   # 신인 기간(부스트 7일)도 여기서 시작
            return self._row(con, uid)
        return await self._tx(fn)

    async def set_step(self, uid: int, step: int) -> dict:
        def fn(con):
            con.execute("UPDATE onboarding SET step=? WHERE user_id=? AND done_ts IS NULL", (step, uid))
            return self._row(con, uid)
        return await self._tx(fn)

    async def practiced(self, uid: int, key: str) -> dict:
        def fn(con):
            row = self._row(con, uid)
            if row and key not in row["practiced"]:
                con.execute("UPDATE onboarding SET practiced=? WHERE user_id=?", (json.dumps(row["practiced"] + [key]), uid))
            return self._row(con, uid)
        return await self._tx(fn)

    async def setup_club(self, uid: int) -> dict:
        """구단을 만든 직후 자동 설정: 선발 · 주장(선발 최고 OVR) · 기본 포메이션 · 초보 감독 · 인턴 트레이너.
        DM 알림은 기본값이 모두 OFF — 튜토리얼 '알림 설정' 단계에서 직접 켠다 (2.9.1)."""
        from services.club_db import ClubDB

        def fn(con):
            squad = ClubDB._squad(con, uid)
            placed = auto_assign(list(squad.values()), DEFAULT_FORMATION)
            con.execute("DELETE FROM club_lineup WHERE user_id=?", (uid,))
            con.executemany("INSERT INTO club_lineup(user_id, slot, player_id) VALUES(?, ?, ?)",
                            [(uid, i, pid) for i, pid in enumerate(placed) if pid])
            xi = [squad[p] for p in placed if p]
            cap = max(xi, key=lambda p: p["ovr"]) if xi else None
            con.execute("UPDATE clubs SET formation=?, captain=?, manager=COALESCE(manager, ?), medic=COALESCE(medic, ?) "
                        "WHERE user_id=?", (DEFAULT_FORMATION, cap["player_id"] if cap else None, DEFAULT_MANAGER, DEFAULT_MEDIC, uid))
            return {"captain": cap["name"] if cap else None, "filled": len(xi)}
        return await self._tx(fn)

    async def npc_match(self, uid: int, team: dict, rng=random) -> dict:
        """연습 경기: 결과를 내 구단 전적에만 남긴다 (루키 미션 '첫 경기' 달성)."""
        npc = npc_team()
        h = {"name": team["name"], "rating": team["rating"], "xi": [s for s in team["lineup"] if s.get("player_id")]}
        a = {"name": npc["name"], "rating": npc["rating"], "xi": npc["lineup"]}
        result = simulate_match(h, a, rng)
        hl = match_highlights(result, h, a, rng)
        col = "wins" if result["home"] > result["away"] else ("draws" if result["home"] == result["away"] else "losses")
        await self._tx(lambda con: con.execute(f"UPDATE clubs SET {col}={col}+1 WHERE user_id=?", (uid,)))
        return {"home": result["home"], "away": result["away"], "highlights": hl, "h": h, "a": a}

    async def finish(self, uid: int, now_ts: int) -> dict:
        """마무리: 보상 상자(한 번) + 끝 표시. {first, money, items, balance}."""
        def fn(con):
            row = self._row(con, uid)
            if not row:
                return {"ok": False}
            first = not row["rewarded"]
            if first:
                con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (uid,))
                con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (FINAL_MONEY, uid))
                for k, n in FINAL_ITEMS.items():
                    give_item(con, uid, k, n)
            con.execute("UPDATE onboarding SET rewarded=1, done_ts=COALESCE(done_ts, ?), step=? WHERE user_id=?",
                        (now_ts, len(STEPS) - 1, uid))
            bal = int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (uid,)).fetchone()[0])
            return {"ok": True, "first": first, "balance": bal}
        return await self._tx(fn)


def now() -> int:
    return int(time.time())
