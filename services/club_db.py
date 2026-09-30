# services/club_db.py
# 구단: 이름 · 포메이션 · 선발 11명 · 주장 · 친선경기 전적
import asyncio
import math
import random
import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "economy.sqlite3"

CLUB_NAME_MAX = 30

# ───────────── 포메이션 ─────────────
# 슬롯 0 은 항상 GK. 표시 순서 = 슬롯 순서.
FORMATIONS: dict[str, list[str]] = {
    "4-4-2":   ["GK", "LB", "CB", "CB", "RB", "LM", "CM", "CM", "RM", "ST", "ST"],
    "4-3-3":   ["GK", "LB", "CB", "CB", "RB", "CM", "CM", "CM", "LW", "ST", "RW"],
    "4-2-3-1": ["GK", "LB", "CB", "CB", "RB", "DM", "DM", "LW", "AM", "RW", "ST"],
    "3-5-2":   ["GK", "CB", "CB", "CB", "LM", "CM", "CM", "CM", "RM", "ST", "ST"],
    "3-4-3":   ["GK", "CB", "CB", "CB", "LM", "CM", "CM", "RM", "LW", "ST", "RW"],
    "5-3-2":   ["GK", "LWB", "CB", "CB", "CB", "RWB", "CM", "CM", "CM", "ST", "ST"],
}
DEFAULT_FORMATION = "4-4-2"

# 슬롯 → 선수 포지션 그룹(선수 카드는 GK/DF/MF/FW 네 가지뿐이다)
SLOT_GROUP = {
    "GK": "GK",
    "LB": "DF", "CB": "DF", "RB": "DF", "LWB": "DF", "RWB": "DF",
    "LM": "MF", "CM": "MF", "RM": "MF", "DM": "MF", "AM": "MF",
    "LW": "FW", "RW": "FW", "ST": "FW",
}
_LINE = {"GK": 0, "DF": 1, "MF": 2, "FW": 3}


def effective_ovr(ovr: int, player_pos: str, slot: str) -> int:
    """제 포지션이 아니면 능력치가 깎인다: 한 줄 차이 -6, 두 줄 -12, 골키퍼↔필드 -30."""
    want, have = SLOT_GROUP[slot], player_pos
    if want == have:
        return int(ovr)
    if "GK" in (want, have):
        return max(1, int(ovr) - 30)
    return max(1, int(ovr) - 6 * abs(_LINE[want] - _LINE[have]))


def team_rating(lineup: list[dict], captain: Optional[str]) -> dict:
    """lineup: 슬롯 11개 [{slot, player_id, pos, ovr, nation} | {slot, player_id: None}].
    전력 = 11자리 유효 능력치 평균(빈 자리는 30) + 주장 +1 + 같은 국적 3명 이상 묶음마다 +1(최대 +3)."""
    total, filled, nations = 0, 0, {}
    for s in lineup:
        if s.get("player_id"):
            total += effective_ovr(s["ovr"], s["pos"], s["slot"])
            filled += 1
            nations[s["nation"]] = nations.get(s["nation"], 0) + 1
        else:
            total += 30
    base = total / 11
    captain_bonus = 1 if captain and any(s.get("player_id") == captain for s in lineup) else 0
    chem = min(3, sum(1 for n in nations.values() if n >= 3))
    return {"rating": round(base + captain_bonus + chem), "base": round(base, 1), "filled": filled,
            "captain_bonus": captain_bonus, "chem": chem}


def auto_assign(players: list[dict], formation: str) -> list[Optional[str]]:
    """가진 선수로 가장 강한 11명 배치. players: [{player_id, pos, ovr}]. 슬롯별 player_id 목록을 돌려준다.
    모든 (자리, 선수) 조합을 유효 능력치 높은 순으로 채운다. 같으면 제 포지션 선수가 먼저다."""
    slots = FORMATIONS[formation]
    pairs = sorted(
        ((effective_ovr(p["ovr"], p["pos"], s), p["pos"] == SLOT_GROUP[s], p["ovr"], i, p["player_id"])
         for i, s in enumerate(slots) for p in players),
        reverse=True,
    )
    out: list[Optional[str]] = [None] * len(slots)
    used = set()
    for _eff, _natural, _ovr, i, pid in pairs:
        if out[i] is None and pid not in used:
            out[i] = pid
            used.add(pid)
    return out


def simulate_match(home: dict, away: dict, rng: random.Random = random) -> dict:
    """home/away: {name, rating, xi:[{name, pos, ovr}]}. 전력 차이로 기대 득점을 정하고 포아송으로 굴린다."""
    diff = (home["rating"] - away["rating"]) / 20
    xg_h, xg_a = 1.35 * math.exp(diff), 1.35 * math.exp(-diff)

    def poisson(lam: float) -> int:
        l, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= rng.random()
            if p <= l:
                return k
            k += 1

    weight = {"FW": 6, "MF": 3, "DF": 1, "GK": 0}
    goals = []
    for side, team, n in (("home", home, poisson(xg_h)), ("away", away, poisson(xg_a))):
        shooters = [p for p in team["xi"] if weight[p["pos"]]] or team["xi"]
        for _ in range(min(n, 9)):
            who = rng.choices(shooters, weights=[weight[p["pos"]] * p["ovr"] or 1 for p in shooters])[0] \
                if shooters else {"name": "자책골"}
            goals.append({"side": side, "minute": rng.randint(1, 90), "scorer": who["name"]})
    goals.sort(key=lambda g: g["minute"])
    return {"home": sum(g["side"] == "home" for g in goals), "away": sum(g["side"] == "away" for g in goals),
            "goals": goals}


class ClubDB:
    def __init__(self):
        self._lock = asyncio.Lock()
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        return con

    def _init_db(self):
        con = self._connect()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS clubs (
                    user_id INTEGER PRIMARY KEY,
                    club_name TEXT NOT NULL,
                    created_ts INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            for col in (f"formation TEXT NOT NULL DEFAULT '{DEFAULT_FORMATION}'", "captain TEXT",
                        "wins INTEGER NOT NULL DEFAULT 0", "draws INTEGER NOT NULL DEFAULT 0",
                        "losses INTEGER NOT NULL DEFAULT 0"):
                try:
                    con.execute(f"ALTER TABLE clubs ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS club_lineup (
                    user_id INTEGER NOT NULL,
                    slot INTEGER NOT NULL,
                    player_id TEXT NOT NULL,
                    PRIMARY KEY (user_id, slot),
                    UNIQUE (user_id, player_id)
                )
                """
            )
            # 구단 생성 보너스는 평생 한 번 — 삭제 후 재생성으로 보너스를 반복해 받지 못하게 한다.
            con.execute("CREATE TABLE IF NOT EXISTS club_bonus (user_id INTEGER PRIMARY KEY)")
            con.execute("INSERT OR IGNORE INTO club_bonus(user_id) SELECT user_id FROM clubs")
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

    # ───────────── 구단 ─────────────
    async def get_club(self, user_id: int) -> Optional[dict]:
        def fn(con):
            row = con.execute(
                "SELECT club_name, created_ts, formation, captain, wins, draws, losses FROM clubs WHERE user_id=?",
                (int(user_id),),
            ).fetchone()
            if not row:
                return None
            keys = ("name", "created_ts", "formation", "captain", "wins", "draws", "losses")
            club = dict(zip(keys, row))
            if club["formation"] not in FORMATIONS:
                club["formation"] = DEFAULT_FORMATION
            return club
        return await self._tx(fn)

    async def create_club(self, user_id: int, club_name: str, now_ts: int) -> tuple[bool, str, bool]:
        """(성공, 메시지, 보너스 지급 여부)."""
        club_name = (club_name or "").strip()
        if not club_name:
            return False, "구단 이름이 비어 있습니다.", False
        if len(club_name) > CLUB_NAME_MAX:
            return False, f"구단 이름은 {CLUB_NAME_MAX}자 이내여야 합니다.", False

        def fn(con):
            if con.execute("SELECT 1 FROM clubs WHERE user_id=?", (int(user_id),)).fetchone():
                return False, "이미 구단이 있습니다.", False
            con.execute("INSERT INTO clubs(user_id, club_name, created_ts) VALUES(?, ?, ?)",
                        (int(user_id), club_name, int(now_ts)))
            bonus = con.execute("INSERT OR IGNORE INTO club_bonus(user_id) VALUES(?)", (int(user_id),)).rowcount > 0
            return True, "구단 생성 완료", bonus
        return await self._tx(fn)

    async def rename_club(self, user_id: int, new_name: str) -> tuple[bool, str]:
        new_name = (new_name or "").strip()
        if not new_name:
            return False, "구단 이름이 비어 있습니다."
        if len(new_name) > CLUB_NAME_MAX:
            return False, f"구단 이름은 {CLUB_NAME_MAX}자 이내여야 합니다."

        def fn(con):
            cur = con.execute("UPDATE clubs SET club_name=? WHERE user_id=?", (new_name, int(user_id)))
            if not cur.rowcount:
                return False, "구단이 없습니다. `/구단생성`을 먼저 해주세요."
            return True, "구단명 변경 완료"
        return await self._tx(fn)

    async def delete_club(self, user_id: int) -> bool:
        """구단·선발 명단·아마추어 스쿼드를 지운다. 돈과 일반 선수 카드는 그대로 둔다."""
        def fn(con):
            if not con.execute("DELETE FROM clubs WHERE user_id=?", (int(user_id),)).rowcount:
                return False
            con.execute("DELETE FROM club_lineup WHERE user_id=?", (int(user_id),))
            con.execute("DELETE FROM pm_holdings WHERE user_id=? AND player_id LIKE 'AMT_%'", (int(user_id),))
            return True
        return await self._tx(fn)

    # ───────────── 선수 명단 ─────────────
    @staticmethod
    def _squad(con, user_id: int) -> dict:
        """경기에 뛸 수 있는 보유 선수 (은퇴 제외) {player_id: {...}}."""
        rows = con.execute(
            """
            SELECT p.player_id, p.name, p.position, p.ovr, p.nation
            FROM pm_holdings h JOIN pm_players p ON p.player_id = h.player_id
            WHERE h.user_id=? AND h.qty > 0 AND p.retired = 0
            """,
            (int(user_id),),
        ).fetchall()
        return {r[0]: {"player_id": r[0], "name": r[1], "pos": r[2], "ovr": int(r[3]), "nation": r[4]} for r in rows}

    @staticmethod
    def _lineup(con, user_id: int, formation: str, squad: dict) -> list[dict]:
        """슬롯 11개. 팔거나 은퇴해서 더는 못 뛰는 선수는 명단에서 빼고 빈자리로 둔다."""
        slots = FORMATIONS[formation]
        rows = dict(con.execute("SELECT slot, player_id FROM club_lineup WHERE user_id=?", (int(user_id),)).fetchall())
        out = []
        for i, slot in enumerate(slots):
            pid = rows.get(i)
            if pid and pid in squad:
                out.append({"slot": slot, "index": i, **squad[pid]})
            else:
                if pid:
                    con.execute("DELETE FROM club_lineup WHERE user_id=? AND slot=?", (int(user_id), i))
                out.append({"slot": slot, "index": i, "player_id": None})
        con.execute("DELETE FROM club_lineup WHERE user_id=? AND slot>=?", (int(user_id), len(slots)))
        return out

    async def get_team(self, user_id: int) -> Optional[dict]:
        """구단 + 선발 11명 + 전력. 구단이 없으면 None."""
        def fn(con):
            row = con.execute(
                "SELECT club_name, created_ts, formation, captain, wins, draws, losses FROM clubs WHERE user_id=?",
                (int(user_id),),
            ).fetchone()
            if not row:
                return None
            club = dict(zip(("name", "created_ts", "formation", "captain", "wins", "draws", "losses"), row))
            if club["formation"] not in FORMATIONS:
                club["formation"] = DEFAULT_FORMATION
            squad = self._squad(con, user_id)
            lineup = self._lineup(con, user_id, club["formation"], squad)
            if club["captain"] and club["captain"] not in {s.get("player_id") for s in lineup}:
                con.execute("UPDATE clubs SET captain=NULL WHERE user_id=?", (int(user_id),))
                club["captain"] = None
            club["lineup"] = lineup
            club["squad_size"] = len(squad)
            club.update(team_rating(lineup, club["captain"]))
            return club
        return await self._tx(fn)

    async def squad(self, user_id: int) -> list[dict]:
        return await self._tx(lambda con: sorted(self._squad(con, user_id).values(), key=lambda p: -p["ovr"]))

    async def set_formation(self, user_id: int, formation: str) -> tuple[bool, str]:
        """포메이션을 바꾸고, 지금 선발 11명을 새 자리에 맞게 다시 앉힌다."""
        if formation not in FORMATIONS:
            return False, "없는 포메이션입니다."

        def fn(con):
            club = con.execute("SELECT formation FROM clubs WHERE user_id=?", (int(user_id),)).fetchone()
            if not club:
                return False, "구단이 없습니다. `/구단생성`을 먼저 해주세요."
            squad = self._squad(con, user_id)
            current = [pid for (pid,) in con.execute("SELECT player_id FROM club_lineup WHERE user_id=?", (int(user_id),))
                       if pid in squad]
            placed = auto_assign([squad[p] for p in current], formation)
            con.execute("DELETE FROM club_lineup WHERE user_id=?", (int(user_id),))
            con.executemany("INSERT INTO club_lineup(user_id, slot, player_id) VALUES(?, ?, ?)",
                            [(int(user_id), i, pid) for i, pid in enumerate(placed) if pid])
            con.execute("UPDATE clubs SET formation=? WHERE user_id=?", (formation, int(user_id)))
            return True, f"포메이션을 **{formation}**(으)로 바꿨습니다."
        return await self._tx(fn)

    async def set_slot(self, user_id: int, slot_index: int, player_id: Optional[str]) -> tuple[bool, str]:
        """slot_index 자리에 선수를 넣는다(None 이면 비운다). 다른 자리에 있던 같은 선수는 그 자리에서 빠진다."""
        def fn(con):
            club = con.execute("SELECT formation FROM clubs WHERE user_id=?", (int(user_id),)).fetchone()
            if not club:
                return False, "구단이 없습니다. `/구단생성`을 먼저 해주세요."
            slots = FORMATIONS.get(club[0], FORMATIONS[DEFAULT_FORMATION])
            if not 0 <= slot_index < len(slots):
                return False, "없는 자리입니다."
            con.execute("DELETE FROM club_lineup WHERE user_id=? AND slot=?", (int(user_id), slot_index))
            if player_id is None:
                return True, f"**{slots[slot_index]}** 자리를 비웠습니다."
            squad = self._squad(con, user_id)
            if player_id not in squad:
                return False, "보유 중인 현역 선수만 넣을 수 있습니다."
            con.execute("DELETE FROM club_lineup WHERE user_id=? AND player_id=?", (int(user_id), player_id))
            con.execute("INSERT INTO club_lineup(user_id, slot, player_id) VALUES(?, ?, ?)",
                        (int(user_id), slot_index, player_id))
            p = squad[player_id]
            eff = effective_ovr(p["ovr"], p["pos"], slots[slot_index])
            note = "" if eff == p["ovr"] else f" (제 포지션 아님: {p['ovr']} → {eff})"
            return True, f"**{slots[slot_index]}** 자리에 **{p['name']}** 배치{note}"
        return await self._tx(fn)

    async def auto_lineup(self, user_id: int) -> tuple[bool, str]:
        def fn(con):
            club = con.execute("SELECT formation FROM clubs WHERE user_id=?", (int(user_id),)).fetchone()
            if not club:
                return False, "구단이 없습니다. `/구단생성`을 먼저 해주세요."
            formation = club[0] if club[0] in FORMATIONS else DEFAULT_FORMATION
            squad = self._squad(con, user_id)
            if not squad:
                return False, "뛸 수 있는 선수가 없습니다."
            placed = auto_assign(list(squad.values()), formation)
            con.execute("DELETE FROM club_lineup WHERE user_id=?", (int(user_id),))
            con.executemany("INSERT INTO club_lineup(user_id, slot, player_id) VALUES(?, ?, ?)",
                            [(int(user_id), i, pid) for i, pid in enumerate(placed) if pid])
            return True, "가장 강한 11명으로 자동 편성했습니다."
        return await self._tx(fn)

    async def set_captain(self, user_id: int, player_id: str) -> tuple[bool, str]:
        def fn(con):
            if not con.execute("SELECT 1 FROM clubs WHERE user_id=?", (int(user_id),)).fetchone():
                return False, "구단이 없습니다. `/구단생성`을 먼저 해주세요."
            row = con.execute(
                "SELECT p.name FROM club_lineup l JOIN pm_players p ON p.player_id=l.player_id "
                "WHERE l.user_id=? AND l.player_id=?", (int(user_id), player_id),
            ).fetchone()
            if not row:
                return False, "선발 11명 중에서만 주장을 뽑을 수 있습니다."
            con.execute("UPDATE clubs SET captain=? WHERE user_id=?", (player_id, int(user_id)))
            return True, f"**{row[0]}**을(를) 주장으로 임명했습니다. (전력 +1)"
        return await self._tx(fn)

    async def record_match(self, home_id: int, away_id: int, home_goals: int, away_goals: int) -> None:
        def fn(con):
            for uid, gf, ga in ((home_id, home_goals, away_goals), (away_id, away_goals, home_goals)):
                col = "wins" if gf > ga else ("draws" if gf == ga else "losses")
                con.execute(f"UPDATE clubs SET {col}={col}+1 WHERE user_id=?", (int(uid),))
        await self._tx(fn)
