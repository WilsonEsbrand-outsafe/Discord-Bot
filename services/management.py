# services/management.py — 🏟️ 구단 경영 (2.8): 매일 00시(KST) 관중 수입 정산 + 팬 만족도
# 순이익 = 관중 × 티켓 단가 − 선수 유지비(선발 11명) − 시설 유지비(시설에 쓴 돈의 0.05%/일). 비용은 그날 수입까지만 (마이너스 없음).
# 관중 = min(경기장 수용 한도, (2,000 + 400 × (전력 − 50)) × (0.5 + 만족도/100) × 티켓 배율). 한도를 넘으면 매진(만족도 +1).
# 팬 만족도(0~100, 처음 50)는 매일 목표치 쪽으로 20%씩: 50 + 최근 10경기 (승−패)×3(±20) + 시설 레벨 합(최대 20)
#   + 티켓(저가 +10 · 보통 0 · 고가 −15) + 수족관 점수 ÷ 16(최대 10). 70↑ 스폰서 +3% · 명문 수입 +5%, 85↑ +5% · +10%.
# 정산 대상: 공개 전에는 2.8 서버에서 경영 화면을 한 번 연 구단만, 공개 뒤에는 모든 구단 (내 구단 = 스쿼드 A 기준).
import json
from typing import Optional

import release
from services.club_db import FACILITY_COSTS, ClubDB, kst_day
from services.economy_db import EconomyDB
from services.fishing import FishingDB

CAPACITY = (2_000, 4_000, 7_000, 12_000, 20_000, 30_000)
BASE_DEMAND, DEMAND_PER_RATING = 2_000, 400
TICKETS = {   # key → (이모지, 이름, 1인당 구단 몫, 수요 배율, 만족도 목표 보정)
    "low": ("💸", "저가", 200, 1.3, 10),
    "normal": ("🎟️", "보통", 300, 1.0, 0),
    "high": ("💎", "고가", 450, 0.7, -15),
}
DEFAULT_TICKET = "normal"
SAT_START, SAT_MOVE, SELLOUT_SAT = 50.0, 0.20, 1
FORM_STEP, FORM_CAP, FAC_CAP, AQUA_DIV, AQUA_CAP = 3, 20, 20, 16, 10
WAGE, WAGE_EXP, WAGE_FLOOR = 200, 1.3, 40
FAC_UPKEEP = 0.0005
BONUS = ((85, 0.05, 0.10), (70, 0.03, 0.05))   # (만족도 이상, 스폰서 플러스 수익 +, 명문 구단 하루 수입 +)


def demand(rating: float, sat: float, ticket: str) -> int:
    return max(0, int((BASE_DEMAND + DEMAND_PER_RATING * (rating - 50)) * (0.5 + sat / 100) * TICKETS[ticket][3]))


def wages(ovrs: list[int]) -> int:
    return int(sum(WAGE * max(0, o - WAGE_FLOOR) ** WAGE_EXP for o in ovrs))


def facility_spent(levels: dict[str, int]) -> int:
    return sum(sum(FACILITY_COSTS[:lv]) for lv in levels.values())


def form_score(recent: str) -> int:
    return max(-FORM_CAP, min(FORM_CAP, (recent.count("W") - recent.count("L")) * FORM_STEP))


def sat_target(recent: str, levels: dict[str, int], ticket: str, aqua: int) -> tuple[float, dict]:
    parts = {"form": form_score(recent), "facility": min(FAC_CAP, sum(levels.values())), "ticket": TICKETS[ticket][4],
             "aquarium": min(AQUA_CAP, aqua // AQUA_DIV)}
    return max(0.0, min(100.0, 50 + sum(parts.values()))), parts


def fan_bonus(sat: Optional[float]) -> tuple[float, float]:
    """(스폰서 플러스 수익 +, 명문 구단 하루 수입 +)."""
    return next(((s, e) for need, s, e in BONUS if sat is not None and sat >= need), (0.0, 0.0))


def day_report(rating: float, ovrs: list[int], levels: dict[str, int], sat: float, ticket: str) -> dict:
    """하루 정산 계산 (돈 · 만족도는 안 바꾼다)."""
    cap = CAPACITY[min(levels.get("stadium", 0), len(CAPACITY) - 1)]
    want = demand(rating, sat, ticket)
    fans = min(cap, want)
    gross = fans * TICKETS[ticket][2]
    wage, upkeep = wages(ovrs), int(facility_spent(levels) * FAC_UPKEEP)
    cost = min(gross, wage + upkeep)
    return {"capacity": cap, "demand": want, "fans": fans, "sellout": want >= cap, "gross": gross, "wage": wage,
            "upkeep": upkeep, "cost": cost, "net": gross - cost, "ticket": ticket, "rating": rating}


class MgmtDB(EconomyDB):
    def __init__(self):
        super().__init__()
        self.clubs = ClubDB()
        self.fish = FishingDB()

    def _init_db(self):
        super()._init_db()
        con = self._connect()
        try:
            con.execute("CREATE TABLE IF NOT EXISTS club_mgmt (user_id INTEGER PRIMARY KEY, ticket TEXT NOT NULL DEFAULT 'normal', "
                        "sat REAL NOT NULL DEFAULT 50, settled_day INTEGER NOT NULL DEFAULT 0, last TEXT)")
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _row(con, uid) -> Optional[dict]:
        r = con.execute("SELECT ticket, sat, settled_day, last FROM club_mgmt WHERE user_id=?", (uid,)).fetchone()
        if not r:
            return None
        return {"ticket": r[0] if r[0] in TICKETS else DEFAULT_TICKET, "sat": float(r[1]), "settled_day": int(r[2]),
                "last": json.loads(r[3]) if r[3] else None}

    async def join(self, uid: int, now_ts: int) -> dict:
        """경영 시작 (첫 정산은 다음 00시). 이미 있으면 그대로."""
        def fn(con):
            con.execute("INSERT OR IGNORE INTO club_mgmt(user_id, sat, settled_day) VALUES(?,?,?)", (uid, SAT_START, kst_day(now_ts)))
            return self._row(con, uid)
        return await self._tx(fn)

    async def state(self, uid: int) -> Optional[dict]:
        return await self._tx(lambda con: self._row(con, uid))

    async def set_ticket(self, uid: int, ticket: str) -> bool:
        if ticket not in TICKETS:
            return False
        return await self._tx(lambda con: con.execute("UPDATE club_mgmt SET ticket=? WHERE user_id=?", (ticket, uid)).rowcount > 0)

    async def inputs(self, uid: int) -> Optional[dict]:
        """정산 · 화면에 쓰는 값: 전력 · 선발 OVR · 시설 · 최근 10경기 · 수족관 점수. 구단이 없으면 None."""
        team = await self.clubs.get_team(uid)
        if not team:
            return None
        levels = await self.clubs.facilities(uid)
        aqua = await self.fish.aquarium_score(uid)

        def fn(con):
            r = con.execute("SELECT recent FROM club_form WHERE user_id=?", (uid,)).fetchone()
            return r[0] if r else ""
        recent = await self._tx(fn)
        return {"rating": float(team["rating"]), "ovrs": [int(p["ovr"]) for p in team["lineup"] if p.get("player_id")],
                "levels": levels, "recent": recent, "aqua": int(aqua)}

    async def preview(self, uid: int) -> Optional[dict]:
        """경영 화면: 지금 상태로 오늘 밤 정산하면 + 만족도 목표치."""
        st = await self.state(uid)
        inp = await self.inputs(uid)
        if not st or not inp:
            return None
        target, parts = sat_target(inp["recent"], inp["levels"], st["ticket"], inp["aqua"])
        return {**st, "inputs": inp, "target": target, "parts": parts,
                "today": day_report(inp["rating"], inp["ovrs"], inp["levels"], st["sat"], st["ticket"])}

    async def settle(self, now_ts: int, uid: Optional[int] = None) -> list[dict]:
        """하루가 지난 구단 정산 (며칠 밀려도 한 번만). [{user_id, ...day_report, sat_before, sat, target}]."""
        today = kst_day(now_ts)

        def due(con):
            if release.released(now_ts):   # 공개 뒤에는 모든 구단이 경영에 들어온다 (첫 정산은 다음 날)
                con.execute("INSERT OR IGNORE INTO club_mgmt(user_id, sat, settled_day) SELECT user_id, ?, ? FROM clubs",
                            (SAT_START, today))
            q = "SELECT user_id FROM club_mgmt WHERE settled_day<?" + (" AND user_id=?" if uid is not None else "")
            return [r[0] for r in con.execute(q, (today,) + ((uid,) if uid is not None else ()))]
        out = []
        for u in await self._tx(due):
            inp = await self.inputs(u)

            def pay(con, u=u, inp=inp):
                st = self._row(con, u)
                if not st or st["settled_day"] >= today:
                    return None
                if not inp:   # 구단을 없앴으면 정산 없이 날짜만
                    con.execute("UPDATE club_mgmt SET settled_day=? WHERE user_id=?", (today, u))
                    return None
                rep = day_report(inp["rating"], inp["ovrs"], inp["levels"], st["sat"], st["ticket"])
                target, parts = sat_target(inp["recent"], inp["levels"], st["ticket"], inp["aqua"])
                sat = st["sat"] + SAT_MOVE * (target - st["sat"]) + (SELLOUT_SAT if rep["sellout"] else 0)
                sat = round(max(0.0, min(100.0, sat)), 1)
                rep.update(user_id=u, sat_before=st["sat"], sat=sat, target=target, parts=parts, day=today)
                con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (u,))
                con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (rep["net"], u))
                con.execute("UPDATE club_mgmt SET sat=?, settled_day=?, last=? WHERE user_id=?", (sat, today, json.dumps(rep), u))
                return rep
            r = await self._tx(pay)
            if r:
                out.append(r)
        return out

    async def fan_bonus(self, uid: int) -> tuple[float, float]:
        st = await self.state(uid)
        return fan_bonus(st["sat"] if st else None)

    async def elite_bonus(self, uid: int, amount: int) -> int:
        """명문 구단 하루 수입에 팬 만족도 보너스를 더 준다 → 더 준 돈."""
        extra = int(amount * (await self.fan_bonus(uid))[1])
        if extra:
            await self.add_balance(uid, extra)
        return extra

