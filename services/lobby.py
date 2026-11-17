# services/lobby.py — /로비 (3.8.2, 테스트 서버 먼저): 일주일에 한 번 열리는 축구 봇 대선
# 11/16 규완님: '대선후보한테 로비해주는 그런 느낌' · '일주일에 한 번' · '공약 갯수를 늘리고' ·
#              '사건이 하루하루 혹은 몇 시간마다 뉴스로 터져도 ㄱㅊ을듯'
# 후보 4명(모두 가상 인물)마다 공약 3개. 유저는 축구 봇 돈으로 후보에게 로비한다 → 로비가 몰릴수록 지지율이 오른다.
# 몇 시간마다 사건 뉴스(토론 · 스캔들 · 지지 선언 …)가 터져서 지지율이 오르내린다.
# 일요일 21:00 (KST) 마감: 지지율 + 선거일 변수로 당선자 결정 → 낙선 후보들에게 들어간 돈을 당선자에게 로비한 사람들이
# 넣은 금액 비율대로 나눠 받는다 (원금도 돌려받는다). 당선자에게 로비한 사람마다 LOBBY_CAUGHT 확률로 '로비 적발' — 원금 · 배당 없음.
# 당선 공약은 다음 선거가 끝날 때까지 적용된다 (공개 전엔 테스트 서버 사람만 — TESTERS).
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

# 가상 인물 이름 · 정당 (실제 정치인 · 정당 이름은 쓰지 않는다)
# 11/17 규완님 '후보 이름을 좀 바꿔줘' — 사람 이름처럼
NAMES = ("김민준", "이서연", "박지훈", "최유나", "정우성", "강하늘", "윤도현", "한지민", "오세훈", "임수정",
         "서강준", "문채원", "장기용", "배수지", "신동엽", "조인성", "홍진경", "남궁민")
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
                    "rate": rng.randint(*RATE_RANGE),
                    "pledges": [[k, rng.choice(PLEDGES[k][1])] for k in keys]})
    return out


def keep_pledges(c: dict, rng=random) -> list[bool]:
    """당선자가 공약마다 이행률(rate)만큼의 확률로 지킨다 → 공약마다 지켰는지."""
    return [rng.random() * 100 < c.get("rate", 100) for _ in c["pledges"]]


def support(cands: list[dict], pots: list[int]) -> list[float]:
    """지지율 (%) — 기본 인기 + 사건 + 로비 몫. 합 100."""
    total = sum(pots)
    score = [max(1.0, c["base"] + c["events"] + (LOBBY_WEIGHT * p / total if total else 0)) for c, p in zip(cands, pots)]
    s = sum(score)
    return [x * 100 / s for x in score]


def final_shares(cands: list[dict], pots: list[int], rng=random) -> list[float]:
    """선거일 득표율 (%) — 지지율에 후보마다 ±ELECTION_SWING 변수를 더한 것. 합 100."""
    sup = support(cands, pots)
    raw = [max(0.5, s + rng.uniform(-ELECTION_SWING, ELECTION_SWING)) for s in sup]
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
        raw = [max(0.5, f + left * (n + rng.uniform(-3, 3))) for f, n in zip(final, noise)]
        t = sum(raw)
        out.append((p, [x * 100 / t for x in raw] if p < 100 else list(final)))
    return out


def payouts(bets: list[tuple[int, int, int]], winner: int, caught: set[int]) -> dict[int, int]:
    """bets = [(uid, 후보, 금액)] → {uid: 받을 돈 (원금 포함)}. 적발된 사람은 0, 그 몫은 나머지 당선 로비자에게."""
    win = {}
    for u, c, a in bets:
        if c == winner and u not in caught:
            win[u] = win.get(u, 0) + a
    pool = sum(a for u, c, a in bets if c != winner or u in caught)
    stake = sum(win.values())
    return {u: a + (pool * a // stake if stake else 0) for u, a in win.items()}


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
            if len(e["cands"]) == CANDIDATES_N and all("rate" in c for c in e["cands"]):
                return e
            # 11/17 후보 수 · 공약 · 이행률이 바뀌기 전에 열린 선거 (테스트 서버만) → 로비한 돈을 돌려주고 새로 연다
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
            self._snap(con, e["id"], e["cands"], now_ts)
            return {"ok": True, "cand": e["cands"][cand], "amount": int(amount), "mine": mine, "balance": bal - int(amount)}
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
            i = rng.randrange(len(e["cands"]))
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
            caught = {u for u in {u for u, c, _ in bets if c == w} if rng.random() < LOBBY_CAUGHT}
            paid = payouts(bets, w, caught)
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
            con.execute("UPDATE lobby_elections SET status='done', winner=?, cands=? WHERE id=?",
                        (w, json.dumps(e["cands"], ensure_ascii=False), e["id"]))
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
    if not winner:
        return {}
    kept = winner.get("kept") or [True] * len(winner["pledges"])
    return {k: 1 + float(p) for (k, p), ok in zip(winner["pledges"], kept) if ok and k in PLEDGES}

