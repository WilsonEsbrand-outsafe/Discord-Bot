# services/lobby.py — /로비 (3.8.2, 테스트 서버 먼저): 일주일에 한 번 열리는 축구 봇 대선
# 11/16 규완님: '대선후보한테 로비해주는 그런 느낌' · '일주일에 한 번' · '공약 갯수를 늘리고' ·
#              '사건이 하루하루 혹은 몇 시간마다 뉴스로 터져도 ㄱㅊ을듯'
# 당마다 후보 한 명(모두 가상 인물), 후보마다 공약 5개. 유저는 축구 봇 돈으로 후보에게 로비한다 → 로비가 몰릴수록 지지율이 오른다.
# 몇 시간마다 사건 뉴스(토론 · 스캔들 · 지지 선언 …)가 터져서 지지율이 오르내린다.
# 일요일 21:00 (KST) 마감: 지지율 + 선거일 변수로 당선자 결정. 당선자에게 로비한 사람마다 LOBBY_CAUGHT 확률로 '로비 적발' — 원금 · 배당 없음.
# 당선 공약은 다음 선거가 끝날 때까지 적용된다 (공개 전엔 테스트 서버 사람만 — TESTERS).
# 11/17 규완님 '선거를 방식을 /경마나 그런 느낌으로 그냥 바꾸자' → 후보마다 배당(배수)이 붙고, 로비할 때 배당이 정해진다.
#   당선되면 그 후보에게 로비한 돈 × 그때 배당을 받는다 (낙선 후보에게 넣은 돈은 잃는다). 로비는 여전히 지지율도 올린다.
# 11/17 규완님 '그런 이벤트들 추가해줘' → 뉴스 대신 가끔 합당 · 사퇴 · 자격 박탈. 빠진 후보에게 로비한 돈은 돌려준다.
# 11/17 규완님 '/로비 -> 여러 사건 -> 개표 -> 임기 이렇게. 임기 도중 사건이 일어나서 문제가 생길 수도 있게'
#   → 당선자는 다음 선거 개표까지 임기. 임기 중 가끔 사건이 터져 공약 하나가 멈추거나, 드물게 탄핵돼 공약이 전부 멈춘다.
# 수치는 전부 내 기본값.
import json
import random
from typing import Optional

import release
from services.economy_db import EconomyDB

LOBBY_MIN = 10_000            # 한 번에 넣는 최소 로비 금액
LOBBY_CAUGHT = 0.05           # 당선자에게 로비한 사람이 적발될 확률
PLEDGES_N = 5                 # 11/17 규완님 '후보별 공약 개수를 늘려주고' (3 → 5)
RATE_RANGE = (40, 95)         # 11/17 규완님 '공약 이행률' — 당선되면 공약마다 이 확률로 지킨다 (지킨 공약만 적용)
LOBBY_WEIGHT = 25.0           # 지지율 = 기본 인기 + 사건 + 로비 몫(전체 로비 중 그 후보 몫) × 이것
ELECTION_SWING = 15.0         # 선거일 변수: 후보마다 ± 이만큼 (지지율 점수)
NEWS_GAP = (2 * 3600, 4 * 3600)   # 사건 뉴스 간격 (2~4시간)
SNAP_GAP = 3600                   # 지지율 그래프 기록 간격 (뉴스 · 로비 때는 따로 남긴다)
SNAP_MERGE = 600                  # 이 안에 또 기록하면 새 점 대신 마지막 점을 고친다 (로비가 몰려도 점이 너무 많지 않게)
CLOSE_WDAY, CLOSE_HOUR = 6, 21    # 일요일 21:00 KST 마감 (월요일 = 0)
ODDS_MARGIN = 0.9                 # 배당 = 이것 / 당선 확률
ODDS_RANGE = (1.5, 50.0)          # 배당 최소 · 최대 (1배 근처는 안 나오게)
ODDS_SIMS = 3000                  # 당선 확률 계산할 때 선거를 몇 번 돌려 보는지
DROP_CHANCE = 0.12                # 뉴스 차례마다 합당 · 사퇴 사건이 날 확률
DROP_KEEP = 2                     # 후보가 이만큼 남으면 더 빠지지 않는다
TERM_GAP = (12 * 3600, 24 * 3600)   # 임기 중 사건을 굴리는 간격
TERM_TROUBLE = 0.35               # 굴릴 때마다 문제가 터질 확률
TERM_IMPEACH = 0.15               # 터진 문제가 탄핵일 확률 (나머지는 공약 하나 중단)
MERGE_GIVE = 0.7                  # 합당하면 빠지는 후보 인기(기본 + 사건)의 이만큼이 받는 후보에게

# 가상 인물 이름 · 정당 (실제 정치인 · 정당 이름은 쓰지 않는다)
# 11/17 규완님 '후보 이름을 좀 바꿔줘' → '이름을 유명 연예인 말고 다르게' — 흔치 않은 성 + 이름으로 지어낸 이름
NAMES = ("탁서율", "변하진", "길도운", "봉채이", "예소담", "방시온", "국태린", "소연우", "피은재", "여다솔",
         "함주호", "모가윤", "어지환", "맹서후", "추다빈", "편리안", "용하람", "남궁태오")
# 11/17 규완님: '당 이름을 우리나라 당이랑 비슷하게' — 실제 당 이름을 비튼 것 (그대로 쓰지는 않는다)
PARTIES = ("더불어골대당", "국민의골", "조국축구당", "개혁슈팅당", "진보킥당", "정의패스당")
CANDIDATES_N = len(PARTIES)   # 11/17 규완님 '후보가 4개 말고 있는 당 다로' — 당마다 후보 한 명
_OLD_PARTIES = dict(zip(("골대당", "잔디당", "그물당", "코너킥당", "오프사이드당", "하프라인당"), PARTIES))

# 공약: key → (설명 틀, 세기 후보) — 세기는 배수에 더하는 값 (0.3 = 30% 더)
PLEDGES = {
    "attend": ("출석 보상 {p}% 더", (0.5, 1.0)),
    "fish_sell": ("낚시 판매가 {p}% 더", (0.2, 0.3)),
    "farm_sell": ("농사 판매 코인 {p}% 더", (0.2, 0.3)),
    "farm_energy": ("농사 에너지 {p}% 빨리 참", (0.3, 0.5)),
    "farm_grow": ("농작물 {p}% 빨리 자람", (0.2, 0.3)),
    "farm_chest": ("농사 상자 {p}% 더 자주", (0.3, 0.5)),
    "elite_income": ("명문 구단 하루 수입 {p}% 더", (0.3, 0.5)),
    "pack_cost": ("선수팩 값 {p}% 할인", (0.1, 0.2)),
    "item_cost": ("아이템상점 값 {p}% 할인", (0.1, 0.2)),
}

# 사건 뉴스: (틀, 지지율 변화 최소, 최대) — {n} = 후보 이름
NEWS_GOOD = (
    ("{n} 후보, TV 토론에서 압승했다는 평가", 4, 8),
    ("{n} 후보, 유명 축구 선수의 공개 지지를 받아", 3, 7),
    ("{n} 후보의 길거리 유세에 구름 인파", 2, 5),
    ("{n} 후보, 공약 발표회에서 박수 세례", 2, 5),
    ("{n} 후보, 키코 인형 탈을 쓰고 깜짝 등장", 1, 4),
    ("{n} 후보, 여론조사 1위 단체의 지지 선언", 3, 6),
)
NEWS_BAD = (
    ("{n} 후보, 과거 발언 논란으로 사과", 3, 7),
    ("{n} 후보 캠프, 회계 의혹 보도", 4, 8),
    ("{n} 후보, TV 토론에서 말실수 연발", 2, 6),
    ("{n} 후보, 유세 도중 넘어져 화제", 1, 3),
    ("{n} 후보, 공약 베끼기 논란", 2, 5),
    ("{n} 후보 측근, 로비 연루 의혹으로 조사", 3, 7),
)

# 합당 · 사퇴 사건: {n} = 빠지는 후보, {m} = 합당해서 받는 후보
DROP_MERGE = ("{n} 후보, {m} 후보와 전격 합당. {n} 후보는 후보직을 내려놓습니다",
              "{n} 후보, {m} 후보로 단일화 합의하고 사퇴")
DROP_QUIT = ("{n} 후보, 지지율 부진 끝에 전격 사퇴", "{n} 후보, 건강 문제로 후보직 사퇴")
DROP_BAN = ("{n} 후보, 선거법 위반으로 후보 자격 박탈", "{n} 후보, 서류 위조가 드러나 후보 등록 무효")

# 임기 중 사건: {n} = 당선자, {p} = 멈추는 공약
TERM_SUSPEND = ("{n} 대통령, '{p}' 예산 비리 의혹으로 그 공약 중단",
                "{n} 대통령 측근 비리 터져, '{p}' 공약 보류",
                "국회, '{p}' 공약 예산 전액 삭감")
TERM_IMPEACH_TEXT = ("{n} 대통령 탄핵 소추안 가결. 남은 임기 동안 모든 공약이 멈춥니다",
                     "{n} 대통령, 로비 스캔들로 탄핵. 남은 임기 동안 모든 공약이 멈춥니다")

# 당선 공약 효과: key → 배수 (선거가 끝날 때 cog 가 채운다) · 공개 전 효과를 받는 사람 (테스트 서버 멤버)
ACTIVE: dict[str, float] = {}
TESTERS: set[int] = set()


def mult(key: str, uid: Optional[int] = None) -> float:
    """지금 적용 중인 당선 공약 배수 (없으면 1). 공개 전엔 테스트 서버 사람만."""
    m = ACTIVE.get(key)
    if not m:
        return 1.0
    if release.released_382() or (uid is not None and int(uid) in TESTERS):
        return m
    return 1.0


def discount(key: str, uid: Optional[int] = None) -> float:
    """할인 공약 (pack_cost · item_cost) → 값에 곱할 수 (할인 없으면 1)."""
    return max(0.0, 2 - mult(key, uid))


def pledge_text(key: str, power: float) -> str:
    return PLEDGES[key][0].format(p=round(power * 100))


def close_after(ts: float) -> int:
    """ts 다음에 오는 마감 시각 (일요일 21:00 KST)."""
    kst = int(ts) + 9 * 3600
    day = kst // 86400
    wday = (day + 3) % 7          # 1970-01-01 은 목요일 (월요일 = 0)
    close = (day + (CLOSE_WDAY - wday) % 7) * 86400 + CLOSE_HOUR * 3600
    if close <= kst:
        close += 7 * 86400
    return close - 9 * 3600


def make_candidates(rng=random) -> list[dict]:
    names, parties = rng.sample(NAMES, CANDIDATES_N), list(PARTIES)   # 당마다 한 명 (당 순서 그대로)
    out = []
    for i in range(CANDIDATES_N):
        keys = rng.sample(list(PLEDGES), PLEDGES_N)
        out.append({"name": names[i], "party": parties[i], "base": rng.randint(10, 30), "events": 0,
                    "rate": rng.randint(*RATE_RANGE), "out": None,
                    "pledges": [[k, rng.choice(PLEDGES[k][1])] for k in keys]})
    return out


def keep_pledges(c: dict, rng=random) -> list[bool]:
    """당선자가 공약마다 이행률(rate)만큼의 확률로 지킨다 → 공약마다 지켰는지."""
    return [rng.random() * 100 < c.get("rate", 100) for _ in c["pledges"]]


def alive(c: dict) -> bool:
    return not c.get("out")


def support(cands: list[dict], pots: list[int]) -> list[float]:
    """지지율 (%) — 기본 인기 + 사건 + 로비 몫. 합 100. 사퇴 · 합당한 후보는 0."""
    total = sum(p for c, p in zip(cands, pots) if alive(c))
    score = [max(1.0, c["base"] + c["events"] + (LOBBY_WEIGHT * p / total if total else 0)) if alive(c) else 0.0
             for c, p in zip(cands, pots)]
    s = sum(score)
    return [x * 100 / s for x in score]


def final_shares(cands: list[dict], pots: list[int], rng=random) -> list[float]:
    """선거일 득표율 (%) — 지지율에 후보마다 ±ELECTION_SWING 변수를 더한 것. 합 100."""
    sup = support(cands, pots)
    raw = [max(0.5, s + rng.uniform(-ELECTION_SWING, ELECTION_SWING)) if s > 0 else 0.0 for s in sup]
    t = sum(raw)
    return [x * 100 / t for x in raw]


def pick_winner(cands: list[dict], pots: list[int], rng=random) -> int:
    """선거일: 득표율이 가장 높은 후보."""
    sh = final_shares(cands, pots, rng)
    return max(range(len(cands)), key=lambda i: sh[i])


def count_frames(final: list[float], rng=random, steps=(8, 21, 37, 55, 72, 88, 100)) -> list[tuple[int, list[float]]]:
    """개표 장면: [(개표율, 후보별 득표율)] — 초반엔 흔들리다가 100%에서 최종 득표율과 같아진다."""
    noise = [rng.uniform(-12, 12) for _ in final]
    out = []
    for p in steps:
        left = (1 - p / 100) ** 1.2
        raw = [max(0.5, f + left * (n + rng.uniform(-3, 3))) if f > 0 else 0.0 for f, n in zip(final, noise)]
        t = sum(raw)
        out.append((p, [x * 100 / t for x in raw] if p < 100 else list(final)))
    return out


def win_chances(cands: list[dict], pots: list[int]) -> list[float]:
    """후보마다 당선 확률 (0~1) — 선거일을 ODDS_SIMS 번 돌려 본다. 같은 지지율이면 늘 같은 값 (배당이 화면마다 흔들리지 않게)."""
    sup = support(cands, pots)
    rng = random.Random(json.dumps([round(x, 3) for x in sup]))
    wins = [0] * len(cands)
    live = [i for i, s in enumerate(sup) if s > 0]
    for _ in range(ODDS_SIMS):
        wins[max(live, key=lambda i: sup[i] + rng.uniform(-ELECTION_SWING, ELECTION_SWING))] += 1
    return [w / ODDS_SIMS for w in wins]


def odds(cands: list[dict], pots: list[int]) -> list[float]:
    """후보마다 배당 (배수, 소수 한 자리). 빠진 후보는 0."""
    lo, hi = ODDS_RANGE
    return [round(min(hi, max(lo, ODDS_MARGIN / p)) if p > 0 else hi, 1) if alive(c) else 0.0
            for c, p in zip(cands, win_chances(cands, pots))]


def payouts(tickets: list[tuple[int, int, int, float]], winner: int, caught: set[int]) -> dict[int, int]:
    """tickets = [(uid, 후보, 금액, 배당)] → {uid: 받을 돈 (원금 포함)} — 당선 후보 로비 × 로비할 때 배당. 적발된 사람은 0."""
    out: dict[int, int] = {}
    for u, c, a, o in tickets:
        if c == winner and u not in caught:
            out[u] = out.get(u, 0) + int(a * o)
    return out


_TABLES = (
    "CREATE TABLE IF NOT EXISTS lobby_elections (id INTEGER PRIMARY KEY AUTOINCREMENT, opens_ts INTEGER NOT NULL, "
    "closes_ts INTEGER NOT NULL, cands TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', winner INTEGER, "
    "next_news_ts INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS lobby_bets (eid INTEGER NOT NULL, uid INTEGER NOT NULL, cand INTEGER NOT NULL, "
    "amount INTEGER NOT NULL, PRIMARY KEY(eid, uid, cand))",
    "CREATE TABLE IF NOT EXISTS lobby_news (id INTEGER PRIMARY KEY AUTOINCREMENT, eid INTEGER NOT NULL, ts INTEGER NOT NULL, "
    "cand INTEGER, delta INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS lobby_results (eid INTEGER NOT NULL, uid INTEGER NOT NULL, staked INTEGER NOT NULL, "
    "paid INTEGER NOT NULL, caught INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(eid, uid))",
    "CREATE TABLE IF NOT EXISTS lobby_support (eid INTEGER NOT NULL, ts INTEGER NOT NULL, sup TEXT NOT NULL)",   # 지지율 그래프
    "CREATE INDEX IF NOT EXISTS lobby_support_eid ON lobby_support(eid, ts)",
    "CREATE TABLE IF NOT EXISTS lobby_tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, eid INTEGER NOT NULL, uid INTEGER NOT NULL, "
    "cand INTEGER NOT NULL, amount INTEGER NOT NULL, odds REAL NOT NULL)",   # /경마처럼: 로비할 때 배당이 정해진다
    "CREATE INDEX IF NOT EXISTS lobby_tickets_eid ON lobby_tickets(eid, cand)",
    "CREATE TABLE IF NOT EXISTS lobby_threads (guild_id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL)",   # 서버마다 대선 뉴스 스레드
)


class LobbyDB(EconomyDB):
    def _init_db(self):
        super()._init_db()
        con = self._connect()
        try:
            for ddl in _TABLES:
                con.execute(ddl)
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _row(r) -> dict:
        cands = json.loads(r[3])
        for c in cands:                                   # 예전 당 이름으로 열린 선거도 새 이름으로 보이게
            c["party"] = _OLD_PARTIES.get(c["party"], c["party"])
        return {"id": int(r[0]), "opens_ts": int(r[1]), "closes_ts": int(r[2]), "cands": cands,
                "status": r[4], "winner": r[5], "next_news_ts": int(r[6])}

    @classmethod
    def _current(cls, con, now_ts: int, rng=random) -> dict:
        """열려 있는 선거 (없으면 새로 연다)."""
        r = con.execute("SELECT id, opens_ts, closes_ts, cands, status, winner, next_news_ts FROM lobby_elections "
                        "WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
        if r:
            e = cls._row(r)
            if len(e["cands"]) == CANDIDATES_N and all("rate" in c and "out" in c for c in e["cands"]):
                return e
            # 11/17 후보 수 · 공약 · 이행률 · 배당 방식이 바뀌기 전에 열린 선거 (테스트 서버만) → 로비한 돈을 돌려주고 새로 연다
            for u, a in con.execute("SELECT uid, SUM(amount) FROM lobby_bets WHERE eid=? GROUP BY uid", (e["id"],)).fetchall():
                con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (int(a), int(u)))
            con.execute("UPDATE lobby_elections SET status='void' WHERE id=?", (e["id"],))
        cands = make_candidates(rng)
        closes = close_after(now_ts)
        cur = con.execute("INSERT INTO lobby_elections(opens_ts, closes_ts, cands, next_news_ts) VALUES(?,?,?,?)",
                          (int(now_ts), closes, json.dumps(cands, ensure_ascii=False), int(now_ts) + rng.randint(*NEWS_GAP)))
        eid = int(cur.lastrowid)
        con.execute("INSERT INTO lobby_news(eid, ts, text) VALUES(?,?,?)",
                    (eid, int(now_ts), f"제{eid}회 축구 봇 대선 후보 등록 마감. 후보 {CANDIDATES_N}명이 선거전에 뛰어들었습니다"))
        cls._snap(con, eid, cands, now_ts, force=True)
        return {"id": eid, "opens_ts": int(now_ts), "closes_ts": closes, "cands": cands, "status": "open", "winner": None,
                "next_news_ts": int(now_ts) + NEWS_GAP[0]}

    @staticmethod
    def _pots(con, eid: int, n: int) -> list[int]:
        pots = [0] * n
        for c, a in con.execute("SELECT cand, SUM(amount) FROM lobby_bets WHERE eid=? GROUP BY cand", (eid,)):
            if 0 <= int(c) < n:
                pots[int(c)] = int(a)
        return pots

    @classmethod
    def _snap(cls, con, eid: int, cands: list[dict], now_ts: int, force: bool = False) -> list[float]:
        """지금 지지율을 그래프 기록에 남긴다. SNAP_MERGE 안의 기록은 (force 가 아니면) 마지막 점을 고친다."""
        sup = support(cands, cls._pots(con, eid, len(cands)))
        last = con.execute("SELECT rowid, ts FROM lobby_support WHERE eid=? ORDER BY ts DESC, rowid DESC LIMIT 1", (eid,)).fetchone()
        txt = json.dumps([round(x, 2) for x in sup])
        if last and not force and now_ts - int(last[1]) < SNAP_MERGE:
            con.execute("UPDATE lobby_support SET sup=? WHERE rowid=?", (txt, last[0]))
        else:
            con.execute("INSERT INTO lobby_support(eid, ts, sup) VALUES(?,?,?)", (eid, int(now_ts), txt))
        return sup

    @staticmethod
    def _history(con, eid: int) -> list[tuple[int, list[float]]]:
        return [(int(t), json.loads(s)) for t, s in con.execute(
            "SELECT ts, sup FROM lobby_support WHERE eid=? ORDER BY ts, rowid", (eid,))]

    async def state(self, uid: int, now_ts: int) -> dict:
        """지금 선거 + 후보별 로비 총액 · 지지율 · 내 로비 · 최근 뉴스 · 지금 적용 중인 공약(지난 당선자)."""
        def fn(con):
            e = self._current(con, now_ts)
            n = len(e["cands"])
            e["pots"] = self._pots(con, e["id"], n)
            e["support"] = support(e["cands"], e["pots"])
            e["odds"] = odds(e["cands"], e["pots"])
            mine = [0] * n
            for c, a in con.execute("SELECT cand, amount FROM lobby_bets WHERE eid=? AND uid=?", (e["id"], int(uid))):
                mine[int(c)] = int(a)
            e["mine"] = mine
            e["news"] = [{"ts": int(t), "text": x, "delta": int(d)} for t, x, d in con.execute(
                "SELECT ts, text, delta FROM lobby_news WHERE eid=? ORDER BY id DESC LIMIT 6", (e["id"],))]
            e["last"] = self._last(con)
            e["history"] = self._history(con, e["id"])
            return e
        return await self._tx(fn)

    @classmethod
    def _last(cls, con) -> Optional[dict]:
        r = con.execute("SELECT id, opens_ts, closes_ts, cands, status, winner, next_news_ts FROM lobby_elections "
                        "WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
        return cls._row(r) if r else None

    async def last_winner(self) -> Optional[dict]:
        """지난 당선자 {name, party, pledges} (없으면 None)."""
        def fn(con):
            e = self._last(con)
            return e["cands"][e["winner"]] if e and e["winner"] is not None else None
        return await self._tx(fn)

    async def lobby(self, uid: int, cand: int, amount: int, now_ts: int) -> dict:
        """후보 cand 에게 amount 원 로비. reason: amount(최소 미만) / cand / closed / balance(have)."""
        def fn(con):
            e = self._current(con, now_ts)
            if not 0 <= cand < len(e["cands"]):
                return {"ok": False, "reason": "cand"}
            if not alive(e["cands"][cand]):
                return {"ok": False, "reason": "out"}
            if amount < LOBBY_MIN:
                return {"ok": False, "reason": "amount", "min": LOBBY_MIN}
            if now_ts >= e["closes_ts"]:
                return {"ok": False, "reason": "closed"}
            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(uid),))
            bal = int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (int(uid),)).fetchone()[0])
            if bal < amount:
                return {"ok": False, "reason": "balance", "have": bal}
            con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (int(amount), int(uid)))
            con.execute("INSERT INTO lobby_bets(eid, uid, cand, amount) VALUES(?,?,?,?) "
                        "ON CONFLICT(eid, uid, cand) DO UPDATE SET amount = amount + excluded.amount",
                        (e["id"], int(uid), int(cand), int(amount)))
            mine = int(con.execute("SELECT amount FROM lobby_bets WHERE eid=? AND uid=? AND cand=?",
                                   (e["id"], int(uid), int(cand))).fetchone()[0])
            # 배당은 이 로비까지 들어간 지지율로 정한다 (큰돈으로 지지율을 올리면서 높은 배당을 받는 걸 막는다)
            o = odds(e["cands"], self._pots(con, e["id"], len(e["cands"])))[cand]
            con.execute("INSERT INTO lobby_tickets(eid, uid, cand, amount, odds) VALUES(?,?,?,?,?)",
                        (e["id"], int(uid), int(cand), int(amount), o))
            self._snap(con, e["id"], e["cands"], now_ts)
            return {"ok": True, "cand": e["cands"][cand], "amount": int(amount), "mine": mine, "odds": o,
                    "balance": bal - int(amount)}
        return await self._tx(fn)

    async def news_tick(self, now_ts: int, rng=random) -> Optional[dict]:
        """뉴스 시각이 지났으면 사건 하나를 터뜨린다 → {eid, text, delta, cand} (아니면 None)."""
        def fn(con):
            e = self._current(con, now_ts, rng)
            if now_ts >= e["closes_ts"]:
                return None
            if now_ts < e["next_news_ts"]:
                last = con.execute("SELECT MAX(ts) FROM lobby_support WHERE eid=?", (e["id"],)).fetchone()[0]
                if last is None or now_ts - int(last) >= SNAP_GAP:
                    self._snap(con, e["id"], e["cands"], now_ts, force=True)
                return None
            live = [k for k, c in enumerate(e["cands"]) if alive(c)]
            if len(live) > DROP_KEEP and rng.random() < DROP_CHANCE:
                return self._drop(con, e, live, now_ts, rng)
            i = rng.choice(live)
            good = rng.random() < 0.5
            tpl, lo, hi = rng.choice(NEWS_GOOD if good else NEWS_BAD)
            delta = rng.randint(lo, hi) * (1 if good else -1)
            e["cands"][i]["events"] += delta
            text = tpl.format(n=e["cands"][i]["name"])
            con.execute("UPDATE lobby_elections SET cands=?, next_news_ts=? WHERE id=?",
                        (json.dumps(e["cands"], ensure_ascii=False), int(now_ts) + rng.randint(*NEWS_GAP), e["id"]))
            con.execute("INSERT INTO lobby_news(eid, ts, cand, delta, text) VALUES(?,?,?,?,?)", (e["id"], int(now_ts), i, delta, text))
            sup = self._snap(con, e["id"], e["cands"], now_ts, force=True)
            return {"eid": e["id"], "text": text, "delta": delta, "cand": i, "support": sup[i]}
        return await self._tx(fn)

    @classmethod
    def _drop(cls, con, e: dict, live: list[int], now_ts: int, rng) -> dict:
        """합당 · 사퇴 · 자격 박탈: 후보 하나가 빠진다. 그 후보에게 로비한 돈은 돌려준다 (경마 출전 취소처럼)."""
        i = rng.choice(live)
        c = e["cands"][i]
        kind = rng.choice(("merge", "quit", "ban"))
        if kind == "merge":
            j = rng.choice([k for k in live if k != i])
            m = e["cands"][j]
            m["base"] += max(0, round((c["base"] + c["events"]) * MERGE_GIVE))
            c["out"] = {"kind": "merge", "into": j}
            text = rng.choice(DROP_MERGE).format(n=c["name"], m=m["name"])
        else:
            j = None
            c["out"] = {"kind": kind}
            text = rng.choice(DROP_QUIT if kind == "quit" else DROP_BAN).format(n=c["name"])
        refund: dict[int, int] = {}
        for u, a in con.execute("SELECT uid, amount FROM lobby_bets WHERE eid=? AND cand=?", (e["id"], i)).fetchall():
            refund[int(u)] = refund.get(int(u), 0) + int(a)
        for u, a in refund.items():
            con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (a, u))
        con.execute("DELETE FROM lobby_bets WHERE eid=? AND cand=?", (e["id"], i))
        con.execute("DELETE FROM lobby_tickets WHERE eid=? AND cand=?", (e["id"], i))
        con.execute("UPDATE lobby_elections SET cands=?, next_news_ts=? WHERE id=?",
                    (json.dumps(e["cands"], ensure_ascii=False), int(now_ts) + rng.randint(*NEWS_GAP), e["id"]))
        con.execute("INSERT INTO lobby_news(eid, ts, cand, delta, text) VALUES(?,?,?,?,?)", (e["id"], int(now_ts), i, 0, text))
        sup = cls._snap(con, e["id"], e["cands"], now_ts, force=True)
        return {"eid": e["id"], "text": text, "delta": 0, "cand": i, "kind": kind, "into": j,
                "support": sup[j] if j is not None else None, "refund": refund}

    async def term_tick(self, now_ts: int, rng=random) -> Optional[dict]:
        """임기 중 사건: 시각이 되면 굴려서 문제가 터지면 → {text, kind, winner(바뀐 당선자)} (아니면 None)."""
        def fn(con):
            last = self._last(con)
            if not last or last["winner"] is None or now_ts < last["next_news_ts"]:
                return None
            w = last["cands"][last["winner"]]
            kept = w.get("kept") or [True] * len(w["pledges"])
            on = [k for k, ok in enumerate(kept) if ok]
            if w.get("impeached") or not on:
                return None
            nxt = int(now_ts) + rng.randint(*TERM_GAP)
            out = None
            if rng.random() < TERM_TROUBLE:
                if rng.random() < TERM_IMPEACH:
                    w["impeached"] = True
                    out = {"kind": "impeach", "text": rng.choice(TERM_IMPEACH_TEXT).format(n=w["name"])}
                else:
                    k = rng.choice(on)
                    kept[k] = False
                    w["kept"] = kept
                    w.setdefault("suspended", []).append(k)
                    out = {"kind": "suspend", "text": rng.choice(TERM_SUSPEND).format(
                        n=w["name"], p=pledge_text(*w["pledges"][k]))}
            con.execute("UPDATE lobby_elections SET cands=?, next_news_ts=? WHERE id=?",
                        (json.dumps(last["cands"], ensure_ascii=False), nxt, last["id"]))
            if out:
                cur = self._current(con, now_ts, rng)   # 뉴스는 지금 선거 화면에도 보이게
                con.execute("INSERT INTO lobby_news(eid, ts, text) VALUES(?,?,?)", (cur["id"], int(now_ts), out["text"]))
                out["winner"] = w
                out["eid"] = last["id"]
            return out
        return await self._tx(fn)

    async def settle(self, now_ts: int, rng=random) -> Optional[dict]:
        """마감이 지난 선거를 정산한다 → {eid, winner, cand, support, paid: {uid: 돈}, caught: [uid], pool, ...} (아니면 None).
        정산과 함께 다음 선거를 연다. 한 트랜잭션이라 두 번 정산되지 않는다."""
        def fn(con):
            r = con.execute("SELECT id, opens_ts, closes_ts, cands, status, winner, next_news_ts FROM lobby_elections "
                            "WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
            if not r:
                return None
            e = self._row(r)
            if now_ts < e["closes_ts"]:
                return None
            n = len(e["cands"])
            pots = self._pots(con, e["id"], n)
            self._snap(con, e["id"], e["cands"], e["closes_ts"], force=True)   # 그래프 마지막 점 = 마감 순간
            history = self._history(con, e["id"])
            shares = final_shares(e["cands"], pots, rng)
            w = max(range(n), key=lambda i: shares[i])
            bets = [(int(u), int(c), int(a)) for u, c, a in con.execute(
                "SELECT uid, cand, amount FROM lobby_bets WHERE eid=?", (e["id"],))]
            tickets = [(int(u), int(c), int(a), float(o)) for u, c, a, o in con.execute(
                "SELECT uid, cand, amount, odds FROM lobby_tickets WHERE eid=?", (e["id"],))]
            caught = {u for u in {u for u, c, _ in bets if c == w} if rng.random() < LOBBY_CAUGHT}
            paid = payouts(tickets, w, caught)
            staked: dict[int, int] = {}
            for u, _, a in bets:
                staked[u] = staked.get(u, 0) + a
            for u, m in paid.items():
                con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (u,))
                con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (m, u))
            for u, s in staked.items():
                con.execute("INSERT OR REPLACE INTO lobby_results(eid, uid, staked, paid, caught) VALUES(?,?,?,?,?)",
                            (e["id"], u, s, paid.get(u, 0), 1 if u in caught else 0))
            e["cands"][w]["kept"] = keep_pledges(e["cands"][w], rng)   # 공약 이행률 — 지킨 공약만 적용
            con.execute("UPDATE lobby_elections SET status='done', winner=?, cands=?, next_news_ts=? WHERE id=?",
                        (w, json.dumps(e["cands"], ensure_ascii=False), int(now_ts) + rng.randint(*TERM_GAP), e["id"]))
            sup = support(e["cands"], pots)
            con.execute("INSERT INTO lobby_news(eid, ts, cand, text) VALUES(?,?,?,?)",
                        (e["id"], int(now_ts), w, f"{e['cands'][w]['name']} 후보 당선! 득표율 {shares[w]:.1f}%"))
            nxt = self._current(con, now_ts, rng)
            return {"eid": e["id"], "winner": w, "cand": e["cands"][w], "support": sup[w], "paid": paid,
                    "cands": e["cands"], "shares": shares, "history": history, "opens_ts": e["opens_ts"],
                    "closes_ts": e["closes_ts"],
                    "caught": sorted(caught), "pool": sum(pots), "people": len(staked), "next": nxt["id"],
                    "next_closes": nxt["closes_ts"]}
        return await self._tx(fn)


def active_from(winner: Optional[dict]) -> dict[str, float]:
    """당선자 공약 → {공약 key: 배수}."""
    if not winner or winner.get("impeached"):
        return {}
    kept = winner.get("kept") or [True] * len(winner["pledges"])
    return {k: 1 + float(p) for (k, p), ok in zip(winner["pledges"], kept) if ok and k in PLEDGES}

