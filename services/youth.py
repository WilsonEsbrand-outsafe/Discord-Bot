# services/youth.py — 🎓 유스 입단 (2.8): 구단 안에서 키우는 어린 선수 (유망주 · 선수 카드와 별개)
# 슬롯 = 유스 아카데미 레벨 (Lv.0 2명 ~ Lv.5 8명). 하루 1번 입단 테스트(30만원): 지원자 4~6명 중 한 명 → 3장면 테스트.
# 육성: 선수마다 하루 훈련 2번 + 주 1회 NPC 연습경기. 1살 = 실제 7일, 19세 졸업(그 뒤 판매가 주마다 -15%).
# 끝: NPC 구단에 판매(일반 선수 기준가 공식 × 시장 열기 × 1.5) 또는 📝 1군 등록권 + 100만원으로 내 구단 선수로 등록.
# 1군 등록 선수는 선수 카드(pm_players)가 아니라 유망주처럼 구단 명단에만 들어간다 — 팩 · 시세 · 이적시장 · 트레이드 · 랭킹에
# 섞이지 않게. 주 1회 · OVR 상한(유스 레벨별) · 7일 = 1살 · 35세 은퇴.
import json
import random
import time
import zlib

from services.economy_db import EconomyDB, _kst_day

SLOTS = (2, 3, 4, 5, 6, 8)
APPLICANTS = (4, 4, 5, 5, 6, 6)
TRYOUT_FEE, TRYOUT_REFUND = 300_000, 0.5
GRADES = (("C", 60, 69), ("B", 70, 77), ("A", 78, 84), ("S", 85, 90))
GRADE_ODDS = ((50, 35, 13, 2), (25, 40, 27, 8))     # Lv.0 · Lv.5 (사이는 직선)
SCENE_PASS = (0.55, 0.60, 0.65, 0.70)               # 등급별 장면 하나 통과 확률 (2장면 이상 = 합격, 평균 약 60%)
SCENES = ("⚽ 기본기", "🏃 체력", "🧠 정신력")
YEAR = 7 * 86400
GRAD_AGE, FIRST_RETIRE = 19, 35
TRAIN_DAILY, GROWTH = 2, 0.02                       # 훈련 한 번 = 잠재력까지 남은 거리의 2%
MATCH_TRAINS = 3                                    # 주간 연습경기 = 훈련 3번 분량
AFTER_GRAD_DECAY = 0.85                             # 졸업 뒤 판매가 주마다
SELL_MULT = 1.5
REGISTER_FEE = 1_000_000
REGISTER_CAP = (62, 65, 68, 71, 74, 77)
REVEAL = (5, 15, 30)                                # 훈련 횟수 → 등급 · ±5 · 정확한 값
PERSONALITIES = {   # key → (이모지, 이름, 설명)
    "diligent": ("📚", "성실", "훈련 효과 +20%"),
    "genius": ("✨", "천재", "연습경기 +50% · 훈련 -10%"),
    "late": ("🌱", "대기만성", "18세부터 성장 +30%"),
    "clutch": ("🔥", "승부사", "연습경기를 이기면 +50%"),
}
_SUR = "김이박최정강조윤장임한오서신권황안송류홍"
_GIVEN = ("민준", "서준", "도윤", "예준", "시우", "하준", "지호", "주원", "지후", "준우", "건우", "현우", "우진", "선우", "연우",
          "유준", "정우", "승우", "승현", "시윤", "준혁", "은우", "지환", "승민", "지우", "민성", "태윤", "재원", "한결", "윤호")
_NICKS = ("번개", "작은 거인", "왼발 마법사", "골목 메시", "철벽", "강철 심장", "새벽 훈련왕", "몸통 박치기", "축구 천재",
          "거미손", "헤딩 머신", "드리블러", "꾀돌이", "황소", "독수리", "여우", "치타", "탱크")
_SCOUT = {   # 등급 → 스카우터 한마디 (가끔은 빗나간다)
    "C": ("성실하긴 한데… 글쎄요.", "기본은 돼 있어요.", "열정은 1등입니다."),
    "B": ("쓸 만한 재목이에요.", "꾸준히 키우면 1군감.", "발밑이 꽤 좋아요."),
    "A": ("이 나이에 이 정도면 대단하죠.", "눈에 띄는 재능이에요!", "몇 년 뒤가 기대돼요."),
    "S": ("이런 애는 처음 봅니다.", "놓치면 평생 후회할 거예요!", "괴물이 나타났어요."),
}
POS = ("GK", "DF", "DF", "MF", "MF", "MF", "FW", "FW")


def grade_odds(level: int) -> list[float]:
    t = max(0, min(5, level)) / 5
    w = [a + (b - a) * t for a, b in zip(*GRADE_ODDS)]
    return [x / sum(w) for x in w]


def grade_of(pot: int) -> str:
    return next(g for g, lo, hi in GRADES if lo <= pot <= hi) if pot >= 60 else "C"


def make_applicant(level: int, rng) -> dict:
    gi = rng.choices(range(4), weights=grade_odds(level))[0]
    g, lo, hi = GRADES[gi]
    pot = rng.randint(lo, hi)
    shown = g if rng.random() < 0.7 else rng.choice([x[0] for x in GRADES])   # 스카우터 말은 70% 만 맞다
    return {"name": rng.choice(_SUR) + rng.choice(_GIVEN), "nick": rng.choice(_NICKS), "pos": rng.choice(POS),
            "age": rng.choice((15, 16)), "ovr": rng.randint(42, 55), "pot": pot, "grade": g,
            "personality": rng.choice(list(PERSONALITIES)), "scout": rng.choice(_SCOUT[shown])}


def tryout_scenes(grade: str, rng) -> list[bool]:
    p = SCENE_PASS[[x[0] for x in GRADES].index(grade)]
    return [rng.random() < p for _ in SCENES]


def age_of(y: dict, now_ts: int) -> int:
    return y["join_age"] + max(0, now_ts - y["joined_ts"]) // YEAR


def grow(ovr: float, pot: int, times: float, mult: float) -> float:
    for _ in range(int(times)):
        ovr += GROWTH * mult * (pot - ovr)
    frac = times - int(times)
    if frac:
        ovr += GROWTH * mult * frac * (pot - ovr)
    return min(float(pot), ovr)


def train_mult(y: dict, now_ts: int, match: bool = False, won: bool = False) -> float:
    k = y["personality"]
    m = 1.0
    if k == "diligent" and not match:
        m *= 1.2
    if k == "genius":
        m *= 1.5 if match else 0.9
    if k == "late" and age_of(y, now_ts) >= 18:
        m *= 1.3
    if k == "clutch" and match and won:
        m *= 1.5
    return m


def base_value(age: int, ovr: int, pot: int) -> int:
    """일반 선수 기준가 공식 (player_market_db._compute_base_value 와 같다)."""
    core = int(22_000 * (1.20 ** max(0, ovr - 55)))
    gap = max(0, pot - ovr)
    pot_mult = 1.0 + gap * (0.060 if age <= 21 else 0.036 if age <= 26 else 0.016)
    age_f = max(0.20, 1.0 - (age - 29) * 0.12) if age >= 30 else 1.0
    return max(10_000, int(core * pot_mult * age_f))


def heat(now_ts: int) -> float:
    """시장 열기 — 날짜마다 0.8 ~ 1.3 (같은 날은 같은 값)."""
    return round(random.Random(zlib.crc32(f"youth-heat:{_kst_day(now_ts)}".encode())).uniform(0.8, 1.3), 2)


def sell_price(y: dict, now_ts: int) -> int:
    age = age_of(y, now_ts)
    weeks_after = max(0, (now_ts - y["joined_ts"] - (GRAD_AGE - y["join_age"]) * YEAR) // (7 * 86400)) if age >= GRAD_AGE else 0
    v = base_value(age, round(y["ovr"]), y["pot"]) * heat(now_ts) * SELL_MULT * (AFTER_GRAD_DECAY ** weeks_after)
    return int(round(v, -3))


def pot_text(y: dict) -> str:
    t = y["trains"]
    if t >= REVEAL[2]:
        return f"**{y['pot']}**"
    if t >= REVEAL[1]:
        lo = max(y["pot"] - rng_off(y), 60)
        return f"{lo}~{lo + 10}"
    if t >= REVEAL[0]:
        return f"{y['grade']}등급"
    return f"{y['grade']}등급" if y.get("revealed_grade") else "???"


def rng_off(y: dict) -> int:
    return random.Random(zlib.crc32(f"youth-pot:{y['id']}".encode())).randint(0, 10)


def week_key(ts: int) -> int:
    return (_kst_day(ts) + 3) // 7


_COLS = ("id", "user_id", "name", "nick", "pos", "personality", "ovr", "pot", "grade", "join_age", "joined_ts", "trains",
         "day_key", "day_trains", "day_bonus", "status", "first_ts", "first_ovr")


def _row(r) -> dict:
    y = dict(zip(_COLS, r))
    y["revealed_grade"] = True   # 합격할 때 등급은 공개된다
    return y


class YouthDB(EconomyDB):
    def _init_db(self):
        super()._init_db()
        con = self._connect()
        try:
            con.execute("""CREATE TABLE IF NOT EXISTS youth_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT, nick TEXT, pos TEXT, personality TEXT,
                ovr REAL, pot INTEGER, grade TEXT, join_age INTEGER, joined_ts INTEGER, trains INTEGER DEFAULT 0,
                day_key INTEGER DEFAULT 0, day_trains INTEGER DEFAULT 0, day_bonus INTEGER DEFAULT 0,
                status TEXT DEFAULT 'youth', first_ts INTEGER DEFAULT 0, first_ovr INTEGER DEFAULT 0)""")
            con.execute("CREATE TABLE IF NOT EXISTS youth_daily (user_id INTEGER, day_key INTEGER, tryout INTEGER DEFAULT 0, "
                        "applicants TEXT, PRIMARY KEY(user_id, day_key))")
            con.execute("CREATE TABLE IF NOT EXISTS youth_week (user_id INTEGER, week INTEGER, match INTEGER DEFAULT 0, "
                        "register INTEGER DEFAULT 0, PRIMARY KEY(user_id, week))")
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _level(con, uid) -> int:
        try:
            r = con.execute("SELECT youth FROM club_facilities WHERE user_id=?", (uid,)).fetchone()
            return int(r[0]) if r else 0
        except Exception:
            return 0

    @staticmethod
    def _youth(con, uid, status="youth") -> list[dict]:
        return [_row(r) for r in con.execute(f"SELECT {', '.join(_COLS)} FROM youth_players WHERE user_id=? AND status=? "
                                             "ORDER BY id", (uid, status))]

    @staticmethod
    def _bal(con, uid) -> int:
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (uid,))
        return int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (uid,)).fetchone()[0])

    @staticmethod
    def _take_item(con, uid, item) -> bool:
        return con.execute("UPDATE inventory SET qty = qty - 1 WHERE user_id=? AND item=? AND qty>0", (uid, item)).rowcount > 0

    async def status(self, uid: int, now_ts: int) -> dict:
        day, wk = _kst_day(now_ts), week_key(now_ts)

        def fn(con):
            lv = self._level(con, uid)
            d = con.execute("SELECT tryout, applicants FROM youth_daily WHERE user_id=? AND day_key=?", (uid, day)).fetchone()
            w = con.execute("SELECT match, register FROM youth_week WHERE user_id=? AND week=?", (uid, wk)).fetchone()
            youth = self._youth(con, uid)
            for y in youth:
                y["age"] = age_of(y, now_ts)
                y["price"] = sell_price(y, now_ts)
                y["trains_left"] = (TRAIN_DAILY + y["day_bonus"] - y["day_trains"]) if y["day_key"] == day else TRAIN_DAILY
            return {"level": lv, "slots": SLOTS[lv], "cap": REGISTER_CAP[lv], "youth": youth,
                    "tried": bool(d and d[0]), "applicants": json.loads(d[1]) if d and d[1] else None,
                    "matched": bool(w and w[0]), "registered": bool(w and w[1]), "heat": heat(now_ts),
                    "first": self._youth(con, uid, "first"),
                    "items": {k: int(con.execute("SELECT COALESCE(SUM(qty),0) FROM inventory WHERE user_id=? AND item=?",
                                                 (uid, k)).fetchone()[0]) for k in ("scout_coupon", "special_training", "first_team")}}
        return await self._tx(fn)

    async def open_tryout(self, uid: int, now_ts: int, rng=random, coupon: bool = False) -> dict:
        """오늘 지원자 카드 (한 번 뽑으면 그날은 그대로). reason: done / full / coupon."""
        day = _kst_day(now_ts)

        def fn(con):
            lv = self._level(con, uid)
            d = con.execute("SELECT tryout, applicants FROM youth_daily WHERE user_id=? AND day_key=?", (uid, day)).fetchone()
            if d and d[0]:
                return {"ok": False, "reason": "done"}
            if len(self._youth(con, uid)) >= SLOTS[lv]:
                return {"ok": False, "reason": "full"}
            if d and d[1]:
                return {"ok": True, "applicants": json.loads(d[1])}
            n = APPLICANTS[lv]
            free = False
            if coupon:
                if not self._take_item(con, uid, "scout_coupon"):
                    return {"ok": False, "reason": "coupon"}
                n, free = n + 2, True
            apps = [make_applicant(lv, rng) for _ in range(n)]
            for a in apps:
                a["free"] = free
            con.execute("INSERT INTO youth_daily(user_id, day_key, tryout, applicants) VALUES(?,?,0,?) "
                        "ON CONFLICT(user_id, day_key) DO UPDATE SET applicants=excluded.applicants", (uid, day, json.dumps(apps)))
            return {"ok": True, "applicants": apps}
        return await self._tx(fn)

    async def tryout(self, uid: int, idx: int, now_ts: int, rng=random) -> dict:
        """지원자 idx 테스트. {ok, passed, scenes, youth, refund}. reason: none / balance / full / done."""
        day = _kst_day(now_ts)

        def fn(con):
            d = con.execute("SELECT tryout, applicants FROM youth_daily WHERE user_id=? AND day_key=?", (uid, day)).fetchone()
            if not d or not d[1] or d[0]:
                return {"ok": False, "reason": "done" if d and d[0] else "none"}
            apps = json.loads(d[1])
            if not 0 <= idx < len(apps):
                return {"ok": False, "reason": "none"}
            a = apps[idx]
            if len(self._youth(con, uid)) >= SLOTS[self._level(con, uid)]:
                return {"ok": False, "reason": "full"}
            fee = 0 if a.get("free") else TRYOUT_FEE
            bal = self._bal(con, uid)
            if bal < fee:
                return {"ok": False, "reason": "balance", "balance": bal}
            scenes = tryout_scenes(a["grade"], rng)
            passed = sum(scenes) >= 2
            refund = 0 if passed else int(fee * TRYOUT_REFUND)
            con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (fee - refund, uid))
            con.execute("UPDATE youth_daily SET tryout=1 WHERE user_id=? AND day_key=?", (uid, day))
            y = None
            if passed:
                cur = con.execute("INSERT INTO youth_players(user_id, name, nick, pos, personality, ovr, pot, grade, join_age, "
                                  "joined_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                  (uid, a["name"], a["nick"], a["pos"], a["personality"], float(a["ovr"]), a["pot"], a["grade"],
                                   a["age"], now_ts))
                y = {**a, "id": cur.lastrowid}
            return {"ok": True, "passed": passed, "scenes": scenes, "youth": y, "applicant": a, "fee": fee, "refund": refund,
                    "balance": self._bal(con, uid)}
        return await self._tx(fn)

    def _get(self, con, uid, yid):
        r = con.execute(f"SELECT {', '.join(_COLS)} FROM youth_players WHERE id=? AND user_id=? AND status='youth'",
                        (yid, uid)).fetchone()
        return _row(r) if r else None

    async def train(self, uid: int, yid: int, now_ts: int, ticket: bool = False) -> dict:
        """훈련 한 번 (ticket=🏋️ 특훈권: 오늘 한 번 더). reason: none / limit / ticket."""
        day = _kst_day(now_ts)

        def fn(con):
            y = self._get(con, uid, yid)
            if not y:
                return {"ok": False, "reason": "none"}
            if y["day_key"] != day:
                y["day_trains"], y["day_bonus"] = 0, 0
            if ticket:
                if not self._take_item(con, uid, "special_training"):
                    return {"ok": False, "reason": "ticket"}
                y["day_bonus"] += 1
            if y["day_trains"] >= TRAIN_DAILY + y["day_bonus"]:
                return {"ok": False, "reason": "limit"}
            before = y["ovr"]
            y["ovr"] = grow(y["ovr"], y["pot"], 1, train_mult(y, now_ts))
            con.execute("UPDATE youth_players SET ovr=?, trains=trains+1, day_key=?, day_trains=?, day_bonus=? WHERE id=?",
                        (y["ovr"], day, y["day_trains"] + 1, y["day_bonus"], yid))
            return {"ok": True, "name": y["name"], "before": before, "ovr": y["ovr"], "trains": y["trains"] + 1,
                    "left": TRAIN_DAILY + y["day_bonus"] - y["day_trains"] - 1}
        return await self._tx(fn)

    async def weekly_match(self, uid: int, now_ts: int, rng=random) -> dict:
        """주 1회 NPC 연습경기 — 유스 전원 훈련 3번 분량 (승부사는 이기면 +50%). reason: done / empty."""
        wk = week_key(now_ts)

        def fn(con):
            w = con.execute("SELECT match FROM youth_week WHERE user_id=? AND week=?", (uid, wk)).fetchone()
            if w and w[0]:
                return {"ok": False, "reason": "done"}
            youth = self._youth(con, uid)
            if not youth:
                return {"ok": False, "reason": "empty"}
            gf, ga = rng.randint(0, 4), rng.randint(0, 3)
            won = gf > ga
            out = []
            for y in youth:
                new = grow(y["ovr"], y["pot"], MATCH_TRAINS, train_mult(y, now_ts, match=True, won=won))
                con.execute("UPDATE youth_players SET ovr=?, trains=trains+? WHERE id=?", (new, MATCH_TRAINS, y["id"]))
                out.append((y["name"], y["ovr"], new))
            con.execute("INSERT INTO youth_week(user_id, week, match) VALUES(?,?,1) ON CONFLICT(user_id, week) DO UPDATE SET match=1",
                        (uid, wk))
            return {"ok": True, "score": (gf, ga), "won": won, "grown": out}
        return await self._tx(fn)

    async def sell(self, uid: int, yid: int, now_ts: int) -> dict:
        def fn(con):
            y = self._get(con, uid, yid)
            if not y:
                return {"ok": False, "reason": "none"}
            price = sell_price(y, now_ts)
            self._bal(con, uid)
            con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (price, uid))
            con.execute("UPDATE youth_players SET status='sold' WHERE id=?", (yid,))
            return {"ok": True, "name": y["name"], "price": price, "balance": self._bal(con, uid)}
        return await self._tx(fn)

    async def register(self, uid: int, yid: int, now_ts: int) -> dict:
        """📝 1군 등록권 + 100만원 → 내 구단 선수. reason: none / cap / week / item / balance / club."""
        wk = week_key(now_ts)

        def fn(con):
            y = self._get(con, uid, yid)
            if not y:
                return {"ok": False, "reason": "none"}
            if not con.execute("SELECT 1 FROM clubs WHERE user_id=?", (uid,)).fetchone():
                return {"ok": False, "reason": "club"}
            cap = REGISTER_CAP[self._level(con, uid)]
            if round(y["ovr"]) > cap:
                return {"ok": False, "reason": "cap", "cap": cap}
            w = con.execute("SELECT register FROM youth_week WHERE user_id=? AND week=?", (uid, wk)).fetchone()
            if w and w[0]:
                return {"ok": False, "reason": "week"}
            if self._bal(con, uid) < REGISTER_FEE:
                return {"ok": False, "reason": "balance"}
            if not self._take_item(con, uid, "first_team"):
                return {"ok": False, "reason": "item"}
            con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (REGISTER_FEE, uid))
            con.execute("UPDATE youth_players SET status='first', first_ts=?, first_ovr=? WHERE id=?",
                        (now_ts, round(y["ovr"]), yid))
            con.execute("INSERT INTO youth_week(user_id, week, register) VALUES(?,?,1) "
                        "ON CONFLICT(user_id, week) DO UPDATE SET register=1", (uid, wk))
            return {"ok": True, "name": y["name"], "ovr": round(y["ovr"]), "pid": f"YT{yid}", "balance": self._bal(con, uid)}
        return await self._tx(fn)


def first_team_squad(con, uid: int, now_ts: int) -> dict:
    """club_db._squad 용: 1군 등록한 유스 선수 {player_id: {...}} (35세 은퇴 처리 포함). 표가 없으면 빈 dict."""
    out = {}
    try:
        rows = con.execute(f"SELECT {', '.join(_COLS)} FROM youth_players WHERE user_id=? AND status='first'", (uid,)).fetchall()
    except Exception:
        return out
    for r in rows:
        y = _row(r)
        if age_of(y, now_ts) >= FIRST_RETIRE:
            con.execute("UPDATE youth_players SET status='retired' WHERE id=?", (y["id"],))
            continue
        out[f"YT{y['id']}"] = {"player_id": f"YT{y['id']}", "name": y["name"], "pos": y["pos"], "ovr": int(y["first_ovr"]),
                               "nation": "대한민국", "youth": True}
    return out


def simulate_value(level: int, n: int = 20000, rng=None) -> dict:
    """밸런스 확인용: 매일 훈련 2번 + 주 1회 연습경기로 19세까지 키워서 판매했을 때. {ev(테스트 1번당 순이익), by_grade}."""
    rng = rng or random.Random(9)
    total, by = 0, {g[0]: [] for g in GRADES}
    t0 = 1_800_000_000
    for _ in range(n):
        a = make_applicant(level, rng)
        if sum(tryout_scenes(a["grade"], rng)) < 2:
            total -= TRYOUT_FEE * (1 - TRYOUT_REFUND)
            continue
        y = {**a, "id": 0, "join_age": a["age"], "joined_ts": t0, "ovr": float(a["ovr"])}
        days = (GRAD_AGE - a["age"]) * 7
        for d in range(days):
            now = t0 + d * 86400
            y["ovr"] = grow(y["ovr"], y["pot"], TRAIN_DAILY, train_mult(y, now))
            if d % 7 == 6:
                y["ovr"] = grow(y["ovr"], y["pot"], MATCH_TRAINS, train_mult(y, now, True, rng.random() < 0.5))
        price = base_value(GRAD_AGE, round(y["ovr"]), y["pot"]) * SELL_MULT   # 시장 열기 평균 1.05 는 빼고 보수적으로
        by[a["grade"]].append((round(y["ovr"]), price))
        total += price - TRYOUT_FEE
    return {"ev": total / n, "by_grade": {g: (sum(o for o, _ in v) / len(v), sum(p for _, p in v) / len(v)) if v else None
                                          for g, v in by.items()}}


def now() -> int:
    return int(time.time())


def personality_label(k: str) -> str:
    e, n, _ = PERSONALITIES[k]
    return f"{e} {n}"

