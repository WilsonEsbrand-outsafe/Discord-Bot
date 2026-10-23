# services/tactics.py — ⚔️ 전술 상성 (2.8): 압박 > 점유 > 역습 > 압박
# 구단마다 기본 전술 하나 + 하프타임 규칙 3칸(지고 있을 때 / 비길 때 / 이기고 있을 때 → 바꿀 전술).
# 선수 × 전술 적응도(0~100): 처음 40, 그 전술로 선발 출전하면 +5, 3일 넘게 안 쓰면 하루 -2 (최저 20).
# 효과: 상성 유리 → 내 기대 득점 +6% × 효과, 불리 → -6% × 효과. 효과 = (0.5 + 0.5 × 팀 적응도/100) × 구성 배율.
# 구성 배율: 전술의 중요 능력치(압박=피지컬 · 점유=패스 · 역습=속력)가 OVR 보다 높은 선수가 많을수록 ↑ (0.8 ~ 1.2).
# 경기는 0~45 · 46~60 · 61~90분 세 구간으로 굴린다. 하프타임에 전술을 바꾸면 46~60분은 새 전술 적응도 × 0.70(감독이 좋으면 ↑).
import json
import math
import random
import time
from typing import Optional

from services.club_db import MANAGERS, SLOT_GROUP, expected_goals, prospect_attrs
from services.economy_db import EconomyDB, _kst_day

TACTICS = {   # key → (이모지, 이름, 중요 능력치, 능력치 이름(화면))
    "press": ("🔥", "압박", "피지컬", "체력"),
    "possession": ("🎯", "점유", "패스", "패스"),
    "counter": ("⚡", "역습", "속력", "스피드"),
}
BEATS = {"press": "possession", "possession": "counter", "counter": "press"}
DEFAULT = "possession"
MATCHUP = 0.06
FIT_START, FIT_GAIN, FIT_MIN, FIT_MAX = 40, 5, 20, 100
FIT_IDLE_DAYS, FIT_DECAY = 3, 2
FIT_DAILY_GAIN = 100            # 하루 적응도 상승 상한 (= 20경기, 유망주 하루 성장 경기 수와 같다)
ELITE_FIT = 70                  # 명문 구단(스쿼드 B) 선수는 적응도 고정
SWITCH_BASE, SWITCH_PER_BONUS, SWITCH_MAX = 0.70, 0.03, 0.85
SEGMENTS = ((1, 45), (46, 60), (61, 90))
RULE_STATES = (("losing", "지고 있을 때"), ("draw", "비기고 있을 때"), ("winning", "이기고 있을 때"))
NOTE_GAIN = 15                  # 📓 전술 노트


def label(t: str) -> str:
    e, n, *_ = TACTICS[t]
    return f"{e} {n}"


def advantage(mine: str, theirs: str) -> int:
    """+1 유리 · -1 불리 · 0 같음."""
    return 1 if BEATS[mine] == theirs else (-1 if BEATS[theirs] == mine else 0)


def key_attr(p: dict, tactic: str) -> int:
    """선수의 그 전술 중요 능력치 — 선수 ID 로 고정된 세부 능력치 (유망주와 같은 공식)."""
    group = p.get("pos") or SLOT_GROUP.get(p.get("slot"), "MF")
    want = TACTICS[tactic][2]
    attrs = dict(prospect_attrs(str(p.get("player_id")), group, int(p["ovr"]), p.get("focus", "")))
    return attrs.get(want, int(p["ovr"]))


def composition(xi: list[dict], tactic: str) -> float:
    field = [p for p in xi if (p.get("pos") or "MF") != "GK"]
    if not field:
        return 1.0
    d = sum(key_attr(p, tactic) - int(p["ovr"]) for p in field) / len(field)
    return max(0.8, min(1.2, 1 + d / 20))


def effect(fit: float, comp: float) -> float:
    return (0.5 + 0.5 * fit / 100) * comp


def decayed(fit: float, last_ts: int, now_ts: int) -> float:
    idle = (_kst_day(now_ts) - _kst_day(last_ts)) if last_ts else 0
    return max(FIT_MIN, fit - FIT_DECAY * max(0, idle - FIT_IDLE_DAYS)) if fit > FIT_MIN else fit


def switch_mult(manager: Optional[str]) -> float:
    base = MANAGERS[manager][3] if manager in MANAGERS else 0
    return min(SWITCH_MAX, SWITCH_BASE + SWITCH_PER_BONUS * base)


def rule_state(mine: int, theirs: int) -> str:
    return "losing" if mine < theirs else ("winning" if mine > theirs else "draw")


def ht_choice(side: dict, mine: int, theirs: int) -> str:
    """하프타임 규칙 → 후반 전술 (규칙이 없으면 그대로)."""
    return side.get("rules", {}).get(rule_state(mine, theirs)) or side["tactic"]


def seg_mult(me: dict, opp: dict, my_t: str, opp_t: str, switched_window: bool) -> float:
    adv = advantage(my_t, opp_t)
    if not adv:
        return 1.0
    fit = me["fit"][my_t] * (me.get("switch", SWITCH_BASE) if switched_window else 1)
    return 1 + adv * MATCHUP * effect(fit, me["comp"][my_t])


def base_mults(home: dict, away: dict) -> tuple[float, float]:
    """기본 전술끼리 90분 내내 붙었을 때의 기대 득점 배율 (공식경기 배당용)."""
    return (seg_mult(home, away, home["tactic"], away["tactic"], False),
            seg_mult(away, home, away["tactic"], home["tactic"], False))


def _poisson(lam: float, rng) -> int:
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1


def _goals(side: str, team: dict, n: int, lo: int, hi: int, rng) -> list[dict]:
    """simulate_match 와 같은 득점자 · 도움 고르기 (분만 구간 안에서)."""
    weight = {"FW": 6, "MF": 3, "DF": 1, "GK": 0}
    assist_w = {"FW": 3, "MF": 5, "DF": 2, "GK": 0}
    shooters = [p for p in team["xi"] if weight[p["pos"]]] or team["xi"]
    out = []
    for _ in range(min(n, 9)):
        who = rng.choices(shooters, weights=[weight[p["pos"]] * p["ovr"] * p.get("conf_mult", 1) or 1
                                             for p in shooters])[0] if shooters else {"name": "자책골"}
        g = {"side": side, "minute": rng.randint(lo, hi), "scorer": who["name"], "scorer_id": who.get("player_id")}
        mates = [p for p in team["xi"] if p is not who and assist_w[p["pos"]]]
        if mates and rng.random() < 0.75:
            a = rng.choices(mates, weights=[assist_w[p["pos"]] * p["ovr"] * p.get("conf_mult", 1) for p in mates])[0]
            g["assist"], g["assist_id"] = a["name"], a.get("player_id")
        out.append(g)
    return out


def _segment(home, away, lo, hi, th, ta, sw_h, sw_a, rng) -> list[dict]:
    xg_h, xg_a = expected_goals(home["rating"], away["rating"])
    share = (hi - lo + 1) / 90
    mh = seg_mult(home, away, th, ta, sw_h)
    ma = seg_mult(away, home, ta, th, sw_a)
    return (_goals("home", home, _poisson(xg_h * share * mh, rng), lo, hi, rng)
            + _goals("away", away, _poisson(xg_a * share * ma, rng), lo, hi, rng))


def first_half(home: dict, away: dict, rng=random) -> dict:
    goals = _segment(home, away, *SEGMENTS[0], home["tactic"], away["tactic"], False, False, rng)
    goals.sort(key=lambda g: g["minute"])
    return {"goals": goals, "home": sum(g["side"] == "home" for g in goals), "away": sum(g["side"] == "away" for g in goals)}


def second_half(home: dict, away: dict, first: dict, th: str, ta: str, rng=random) -> dict:
    sw_h, sw_a = th != home["tactic"], ta != away["tactic"]
    goals = list(first["goals"])
    goals += _segment(home, away, *SEGMENTS[1], th, ta, sw_h, sw_a, rng)
    goals += _segment(home, away, *SEGMENTS[2], th, ta, False, False, rng)
    goals.sort(key=lambda g: g["minute"])
    return {"home": sum(g["side"] == "home" for g in goals), "away": sum(g["side"] == "away" for g in goals),
            "goals": goals, "ht": (first["home"], first["away"]),
            "tactics": {"home": (home["tactic"], th), "away": (away["tactic"], ta)}}


def simulate(home: dict, away: dict, rng=random, home_ht: Optional[str] = None) -> dict:
    """전술이 붙은 경기 한 판. 하프타임은 규칙대로 (home_ht 를 주면 홈은 그 전술)."""
    first = first_half(home, away, rng)
    th = home_ht or ht_choice(home, first["home"], first["away"])
    ta = ht_choice(away, first["away"], first["home"])
    return second_half(home, away, first, th, ta, rng)


class TacticsDB(EconomyDB):
    def _init_db(self):
        super()._init_db()
        con = self._connect()
        try:
            con.execute("CREATE TABLE IF NOT EXISTS club_tactics (user_id INTEGER PRIMARY KEY, tactic TEXT, rules TEXT, "
                        "report INTEGER NOT NULL DEFAULT 0)")
            con.execute("CREATE TABLE IF NOT EXISTS tactic_fit (user_id INTEGER, player_id TEXT, tactic TEXT, fit REAL, "
                        "last_ts INTEGER, day_key INTEGER, day_gain REAL, PRIMARY KEY(user_id, player_id, tactic))")
            con.execute("CREATE TABLE IF NOT EXISTS tactic_log (user_id INTEGER, ts INTEGER, start TEXT, end TEXT)")
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _settings(con, uid) -> dict:
        r = con.execute("SELECT tactic, rules, report FROM club_tactics WHERE user_id=?", (uid,)).fetchone()
        rules = json.loads(r[1]) if r and r[1] else {}
        return {"tactic": (r[0] if r and r[0] in TACTICS else DEFAULT), "rules": {k: v for k, v in rules.items() if v in TACTICS},
                "report": bool(r and r[2])}

    async def settings(self, uid: int) -> dict:
        return await self._tx(lambda con: self._settings(con, uid))

    async def set_tactic(self, uid: int, tactic: str) -> dict:
        assert tactic in TACTICS

        def fn(con):
            con.execute("INSERT INTO club_tactics(user_id, tactic) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET tactic=excluded.tactic",
                        (uid, tactic))
            return self._settings(con, uid)
        return await self._tx(fn)

    async def set_rule(self, uid: int, state: str, tactic: Optional[str]) -> dict:
        assert state in dict(RULE_STATES) and (tactic is None or tactic in TACTICS)

        def fn(con):
            s = self._settings(con, uid)
            rules = dict(s["rules"])
            if tactic:
                rules[state] = tactic
            else:
                rules.pop(state, None)
            con.execute("INSERT INTO club_tactics(user_id, tactic, rules) VALUES(?, ?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET rules=excluded.rules", (uid, s["tactic"], json.dumps(rules)))
            return self._settings(con, uid)
        return await self._tx(fn)

    @staticmethod
    def _fits(con, uid, pids, now_ts) -> dict[str, dict[str, float]]:
        out = {p: {t: float(FIT_START) for t in TACTICS} for p in pids}
        if pids:
            q = ",".join("?" * len(pids))
            for pid, t, fit, last in con.execute(
                    f"SELECT player_id, tactic, fit, last_ts FROM tactic_fit WHERE user_id=? AND player_id IN ({q})", (uid, *pids)):
                if pid in out and t in TACTICS:
                    out[pid][t] = decayed(fit, last, now_ts)
        return out

    async def player_fits(self, uid: int, pids: list[str], now_ts: int) -> dict[str, dict[str, float]]:
        return await self._tx(lambda con: self._fits(con, uid, list(pids), now_ts))

    async def prepare(self, uid: int, team: dict, side: dict, now_ts: int) -> dict:
        """경기에 나설 쪽(side)에 전술 정보를 붙인다: tactic · rules · fit(팀 평균) · comp · switch."""
        def fn(con):
            s = self._settings(con, uid)
            xi = side["xi"]
            if team.get("squad") == "B":
                fit = {t: float(ELITE_FIT) for t in TACTICS}
            else:
                f = self._fits(con, uid, [str(p["player_id"]) for p in xi], now_ts)
                fit = {t: (sum(v[t] for v in f.values()) / len(f) if f else FIT_START) for t in TACTICS}
            side.update(tactic=s["tactic"], rules=s["rules"], fit=fit, comp={t: composition(xi, t) for t in TACTICS},
                        switch=switch_mult(team.get("manager")), report=s["report"])
            return side
        return await self._tx(fn)

    async def after_match(self, uid: int, side: dict, used: tuple[str, str], now_ts: int) -> None:
        """선발 선수 적응도 +5 (후반에 바꾼 전술도 +5) · 전술 기록 (상대 분석용). 스쿼드 B 는 기록만."""
        day = _kst_day(now_ts)

        def fn(con):
            con.execute("INSERT INTO tactic_log VALUES(?,?,?,?)", (uid, now_ts, used[0], used[1]))
            if side.get("squad") == "B":
                return
            pids = [str(p["player_id"]) for p in side["xi"]]
            cur = self._fits(con, uid, pids, now_ts)
            for t in {used[0], used[1]}:
                for pid in pids:
                    r = con.execute("SELECT day_key, day_gain FROM tactic_fit WHERE user_id=? AND player_id=? AND tactic=?",
                                    (uid, pid, t)).fetchone()
                    today_gain = r[1] if r and r[0] == day else 0
                    gain = max(0, min(FIT_GAIN, FIT_DAILY_GAIN - today_gain))
                    fit = min(FIT_MAX, cur[pid][t] + gain)
                    con.execute("INSERT INTO tactic_fit VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id, player_id, tactic) DO UPDATE SET "
                                "fit=excluded.fit, last_ts=excluded.last_ts, day_key=excluded.day_key, day_gain=excluded.day_gain",
                                (uid, pid, t, fit, now_ts, day, today_gain + gain))
        await self._tx(fn)

    async def recent(self, uid: int, n: int = 5) -> list[tuple[str, str]]:
        return await self._tx(lambda con: [(s, e) for s, e in con.execute(
            "SELECT start, end FROM tactic_log WHERE user_id=? ORDER BY ts DESC, rowid DESC LIMIT ?", (uid, n))])

    async def use_note(self, uid: int, pids: list[str], now_ts: int) -> dict:
        """📓 전술 노트: 기본 전술의 선발 적응도 +15 (하루 상한과 상관없음)."""
        def fn(con):
            if not con.execute("UPDATE inventory SET qty = qty - 1 WHERE user_id=? AND item='tactic_note' AND qty>0",
                               (uid,)).rowcount:
                return {"ok": False}
            t = self._settings(con, uid)["tactic"]
            cur = self._fits(con, uid, pids, now_ts)
            for pid in pids:
                fit = min(FIT_MAX, cur[pid][t] + NOTE_GAIN)
                con.execute("INSERT INTO tactic_fit(user_id, player_id, tactic, fit, last_ts, day_key, day_gain) VALUES(?,?,?,?,?,?,0) "
                            "ON CONFLICT(user_id, player_id, tactic) DO UPDATE SET fit=excluded.fit, last_ts=excluded.last_ts",
                            (uid, pid, t, fit, now_ts, _kst_day(now_ts)))
            return {"ok": True, "tactic": t}
        return await self._tx(fn)

    async def use_report(self, uid: int) -> bool:
        """📊 분석 리포트: 다음 경기 킥오프 때 상대 기본 전술 · 하프타임 규칙 공개."""
        def fn(con):
            if not con.execute("UPDATE inventory SET qty = qty - 1 WHERE user_id=? AND item='analysis_report' AND qty>0",
                               (uid,)).rowcount:
                return False
            con.execute("INSERT INTO club_tactics(user_id, tactic, report) VALUES(?, ?, 1) "
                        "ON CONFLICT(user_id) DO UPDATE SET report=1", (uid, DEFAULT))
            return True
        return await self._tx(fn)

    async def consume_report(self, uid: int) -> bool:
        return await self._tx(lambda con: con.execute(
            "UPDATE club_tactics SET report=0 WHERE user_id=? AND report=1", (uid,)).rowcount > 0)


def rules_text(side: dict) -> str:
    r = side.get("rules", {})
    return " · ".join(f"{n.replace('고 있을 때', '')} → {label(r[k]) if r.get(k) else '유지'}" for k, n in RULE_STATES)


def now() -> int:
    return int(time.time())

