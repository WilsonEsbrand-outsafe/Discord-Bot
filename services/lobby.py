# services/lobby.py — /로비 (3.8.4, 테스트 서버 먼저): 일주일에 한 번 열리는 축구 봇 대선
# 11/21 규완님 '/로비를 초안으로 되돌리기' → 3.8.2 의 첫 /로비 (PR #146, 일주일마다 대선 · 사건 뉴스 · 당선 공약) 로 되돌리고,
# 그 위에 규완님이 적은 추가 구성:
#   - 현실처럼 다당제지만 양당제 양상: 큰 두 당이 표의 85% 쯤을 가져가고, 나머지 3~4당은 동정표 · 버리는 표.
#     후보가 진짜 능력 있으면(돌풍) 작은 당도 뚫을 수 있다.
#   - 축구 봇만의 대선 15번을 먼저 돌려 둔 기록이 있고, 플레이어는 제16회부터. 지지 기반은 그 기록에서 나온다.
#     여러 번 집권한 당과 이번 대선 당선 확률을 보여 준다.
#   - 누가 당선될지 베팅 (당선 확률로 정한 배당, 베팅할 때 고정).
#   - 투표: 사람마다 기본 1표(무료), 100만원에 1표씩 더, 최대 100표. 지지 후보는 한 명.
#     지지한 후보가 당선되면 혜택(당선 공약), 그 후보에게 표를 가장 많이 준 사람은 혜택이 두 배.
#   - 당선된 대통령은 임기 중 하는 일(사건 뉴스)에 따라 국정 지지율이 오르내리고, 너무 떨어지면 탄핵 (공약이 멈춘다).
# 수치는 전부 내 기본값.
import calendar
import json
import random
from typing import Optional

import release
from services.economy_db import EconomyDB

# ───────────── 회차 · 일정 ─────────────
HISTORY_N = 15                    # 미리 돌려 둔 대선 수 (제1회 ~ 제15회)
HISTORY_SEED = 384                # 기록은 늘 같게
FIRST_NO = HISTORY_N + 1          # 플레이어가 참여하는 첫 대선
# 11/21 규완님 '대선은 오늘 패치 종료 후 바로 시작, 종료는 내일 오후 9시' → 제16회는 3.9.1 공개 때 새로 열고 (restart)
# 개표는 11월 22일 (일) 21:00 KST, 그다음부터 일요일 21:00 마다. (처음엔 '11월 넷째 주' 로 11/29 였다)
FIRST_CLOSE = calendar.timegm((2026, 11, 22, 12, 0, 0))
CLOSE_WDAY, CLOSE_HOUR = 6, 21    # 일요일 21:00 KST 마감 (월요일 = 0)

# ───────────── 투표 · 베팅 ─────────────
VOTE_FREE = 1                     # 사람마다 기본 1표
VOTE_PRICE = 1_000_000            # 그다음부터 100만원에 1표
VOTE_MAX = 100                    # 한 사람 최대 100표
BET_MIN = 10_000                  # 한 번에 거는 최소 베팅
BET_CAUGHT = 0.05                 # 당선자에게 건 사람이 '로비 적발'될 확률 (첫 /로비 그대로) — 당첨금 없음
ODDS_MARGIN = 0.9                 # 배당 = 이것 / 당선 확률
ODDS_RANGE = (1.2, 50.0)          # 배당 최소 · 최대
ODDS_SIMS = 2000                  # 당선 확률을 계산할 때 선거일을 몇 번 돌려 보는지
TOP_FACTOR = 2                    # 당선자에게 표를 가장 많이 준 사람: 공약 효과 두 배

# ───────────── 지지율 ─────────────
MAJOR_SHARE = 85.0                # 큰 두 당이 가져가는 표 (%)
MINOR_RUN = (3, 4)                # 작은 당 후보 수
ABILITY_W = 0.8                   # 능력(1~10) 한 칸마다 지지율 점수
STORM_P = 0.08                    # 작은 당 후보 하나가 돌풍을 일으킬 확률
STORM_BONUS = (14, 22)            # 돌풍 후보 점수
INCUMBENT = 2.0                   # 지난번 이긴 당은 이만큼 깎인다 (정권 심판)
VOTE_WEIGHT = 20.0                # 플레이어 표: 그 후보 몫 × 이것 (점수)
VOTE_FLOOR = 100                  # 표가 적을 때 몇 표로 몇 점이 휙 바뀌지 않게 (전체 표가 이보다 적으면 이것으로 나눈다)
SWING = 9.0                       # 선거일 변수: 후보마다 ± 이만큼 (점수)
NEWS_GAP = (2 * 3600, 4 * 3600)   # 선거운동 사건 뉴스 간격 (2~4시간)

# ───────────── 임기 ─────────────
TERM_GAP = (3 * 3600, 6 * 3600)   # 임기 중 뉴스 간격
TERM_START = 55.0                 # 국정 지지율 처음 값
TERM_DELTA = (3, 10)              # 뉴스 한 번에 오르내리는 폭
IMPEACH_AT = 20.0                 # 국정 지지율이 이 아래로 떨어지면 탄핵

# 가상 인물 이름 · 정당 (실제 정치인 이름은 쓰지 않는다)
# 11/17 규완님 '이름을 유명 연예인 말고 다르게' — 흔치 않은 성 + 이름으로 지어낸 이름
NAMES = ("탁서율", "변하진", "길도운", "봉채이", "예소담", "방시온", "국태린", "소연우", "피은재", "여다솔",
         "함주호", "모가윤", "어지환", "맹서후", "추다빈", "편리안", "용하람", "남궁태오")
# 11/17 규완님 '당 이름을 우리나라 당이랑 비슷하게' — 실제 당 이름을 비튼 것
MAJORS = ("더불어골대당", "국민의골")
MINORS = ("조국축구당", "개혁슈팅당", "진보킥당", "정의패스당")
PARTIES = MAJORS + MINORS
PLEDGES_N = 3                     # 첫 /로비 그대로 후보마다 공약 3개

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

# 선거운동 사건 뉴스: (틀, 점수 변화 최소, 최대) — {n} = 후보 이름
NEWS_GOOD = (
    ("{n} 후보, TV 토론에서 압승했다는 평가", 2, 4),
    ("{n} 후보, 유명 축구 선수의 공개 지지를 받아", 1, 4),
    ("{n} 후보의 길거리 유세에 구름 인파", 1, 3),
    ("{n} 후보, 공약 발표회에서 박수 세례", 1, 3),
    ("{n} 후보, 키코 인형 탈을 쓰고 깜짝 등장", 1, 2),
    ("{n} 후보, 여론조사 1위 단체의 지지 선언", 2, 3),
)
NEWS_BAD = (
    ("{n} 후보, 과거 발언 논란으로 사과", 1, 4),
    ("{n} 후보 캠프, 회계 의혹 보도", 2, 4),
    ("{n} 후보, TV 토론에서 말실수 연발", 1, 3),
    ("{n} 후보, 유세 도중 넘어져 화제", 1, 2),
    ("{n} 후보, 공약 베끼기 논란", 1, 3),
    ("{n} 후보 측근, 로비 연루 의혹으로 조사", 2, 4),
)
# 임기 중 뉴스: {n} = 대통령, {p} = 공약 하나
TERM_GOOD = ("{n} 대통령, '{p}' 공약 예정보다 빨리 시행", "{n} 대통령, 축구 봇 경제 살리기 대책 발표에 호평",
             "{n} 대통령, 국가대표 경기 직관 응원으로 화제", "{n} 대통령, 야당 대표와 손잡고 민생 법안 통과",
             "{n} 대통령, 재난 현장 신속 대응에 박수")
TERM_BAD = ("{n} 대통령 측근 비리 의혹 보도", "{n} 대통령, '{p}' 공약 예산 낭비 논란",
            "{n} 대통령, 기자회견 막말 논란", "{n} 대통령 가족 특혜 의혹 확산",
            "{n} 대통령, 로비 스캔들 연루 의혹으로 국회 조사")
IMPEACH_TEXT = "{n} 대통령 탄핵 소추안 가결. 국정 지지율 {a}%. 남은 임기 동안 모든 공약이 멈춥니다"

# 당선 공약 효과: key → 배수 · 지지자 uid → 몇 배로 받는지 (cog 가 DB 에서 채운다)
ACTIVE: dict[str, float] = {}
VOTERS: dict[int, int] = {}
TESTERS: set[int] = set()        # 테스트 서버 멤버 (다른 3.8.4 기능이 공개 전 효과를 줄 때 쓴다)


def mult(key: str, uid: Optional[int] = None) -> float:
    """지금 적용 중인 당선 공약 배수 (없으면 1). 당선자를 지지(투표)한 사람만, 가장 많이 준 사람은 효과 두 배."""
    m = ACTIVE.get(key)
    k = VOTERS.get(int(uid)) if uid is not None else None
    if not m or not k:
        return 1.0
    return 1 + (m - 1) * k


def discount(key: str, uid: Optional[int] = None) -> float:
    """할인 공약 (pack_cost · item_cost) → 값에 곱할 수 (할인 없으면 1)."""
    return max(0.1, 2 - mult(key, uid))


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


def votes_cost(had: int, n: int) -> int:
    """이미 had 표를 가진 사람이 n 표를 더 넣을 때 드는 돈 (첫 1표는 무료)."""
    free = max(0, VOTE_FREE - had)
    return max(0, n - free) * VOTE_PRICE


# ───────────── 후보 · 지지율 ─────────────
def party_base(history: list[dict]) -> dict[str, float]:
    """지난 대선 기록 → 당마다 지지 기반 (기록에 나온 득표율 평균). 큰 두 당이 MAJOR_SHARE 를 나누고 작은 당이 나머지."""
    sums = {p: [] for p in PARTIES}
    for h in history:
        for c, s in zip(h["cands"], h["shares"]):
            sums[c["party"]].append(s)
    base = {p: (sum(v) / len(v) if v else (MAJOR_SHARE / 2 if p in MAJORS else (100 - MAJOR_SHARE) / len(MINORS)))
            for p, v in sums.items()}
    major = sum(base[p] for p in MAJORS) or 1
    minor = sum(base[p] for p in MINORS) or 1
    return {p: base[p] * (MAJOR_SHARE / major if p in MAJORS else (100 - MAJOR_SHARE) / minor) for p in PARTIES}


def make_candidates(base: dict[str, float], last_party: Optional[str] = None, rng=random) -> list[dict]:
    """큰 두 당 + 작은 당 3~4. 후보마다 능력(1~10), 가끔 작은 당 돌풍."""
    parties = list(MAJORS) + rng.sample(MINORS, rng.randint(*MINOR_RUN))
    names = rng.sample(NAMES, len(parties))
    run = sum(base[p] for p in parties)
    storm = rng.randrange(2, len(parties)) if rng.random() < STORM_P else None
    out = []
    for i, p in enumerate(parties):
        keys = rng.sample(list(PLEDGES), PLEDGES_N)
        b = base[p] * 100 / run - (INCUMBENT if p == last_party else 0)
        out.append({"name": names[i], "party": p, "major": p in MAJORS, "ability": rng.randint(1, 10),
                    "storm": i == storm, "bonus": rng.randint(*STORM_BONUS) if i == storm else 0,
                    "base": round(b, 2), "events": 0, "pledges": [[k, rng.choice(PLEDGES[k][1])] for k in keys]})
    return out


def scores(cands: list[dict], votes: list[int]) -> list[float]:
    total = max(VOTE_FLOOR, sum(votes))
    return [max(0.5, c["base"] + (c["ability"] - 5.5) * ABILITY_W + c["bonus"] + c["events"] + VOTE_WEIGHT * v / total)
            for c, v in zip(cands, votes)]


def _norm(xs: list[float]) -> list[float]:
    s = sum(xs) or 1
    return [x * 100 / s for x in xs]


def support(cands: list[dict], votes: list[int]) -> list[float]:
    """지금 지지율 (%) — 합 100."""
    return _norm(scores(cands, votes))


def final_shares(cands: list[dict], votes: list[int], rng=random) -> list[float]:
    """선거일 득표율 (%) — 점수에 후보마다 ±SWING."""
    return _norm([max(0.5, s + rng.uniform(-SWING, SWING)) for s in scores(cands, votes)])


def win_chances(cands: list[dict], votes: list[int], sims: int = ODDS_SIMS) -> list[float]:
    """당선 확률 (0~1) — 선거일을 sims 번 돌려 본다 (같은 상태면 늘 같은 값)."""
    rng = random.Random(7)
    sc = scores(cands, votes)
    wins = [0] * len(cands)
    for _ in range(sims):
        wins[max(range(len(sc)), key=lambda i: sc[i] + rng.uniform(-SWING, SWING))] += 1
    return [w / sims for w in wins]


def odds_of(p: float) -> float:
    return round(min(ODDS_RANGE[1], max(ODDS_RANGE[0], ODDS_MARGIN / max(p, 1e-6))), 2)


def make_history(rng=None) -> list[dict]:
    """축구 봇만의 대선 15번 — 플레이어 표 없이 같은 규칙으로 돌린다. 지지 기반은 그때까지의 기록으로."""
    rng = rng or random.Random(HISTORY_SEED)
    out: list[dict] = []
    for no in range(1, HISTORY_N + 1):
        base = party_base(out)
        cands = make_candidates(base, out[-1]["cands"][out[-1]["winner"]]["party"] if out else None, rng)
        for c in cands:   # 선거운동 사건 몇 번
            c["events"] = sum(rng.choice((1, -1)) * rng.randint(1, 3) for _ in range(rng.randint(2, 5)))
        shares = final_shares(cands, [0] * len(cands), rng)
        w = max(range(len(cands)), key=lambda i: shares[i])
        out.append({"no": no, "cands": cands, "shares": [round(s, 1) for s in shares], "winner": w})
    return out


def party_wins(rounds: list[dict]) -> dict[str, int]:
    """당마다 집권 횟수 (많은 순)."""
    n = {p: 0 for p in PARTIES}
    for r in rounds:
        if r.get("winner") is not None:
            n[r["cands"][r["winner"]]["party"]] += 1
    return dict(sorted(n.items(), key=lambda x: -x[1]))


def payouts(bets: list[tuple[int, int, int, float]], winner: int, caught: set[int]) -> dict[int, int]:
    """bets = [(uid, 후보, 금액, 배당)] → {uid: 받을 돈}. 적발된 사람은 0."""
    out: dict[int, int] = {}
    for u, c, a, o in bets:
        if c == winner and u not in caught:
            out[u] = out.get(u, 0) + int(a * o)
    return out


def top_voter(votes: list[tuple[int, int, int]], winner: int) -> Optional[int]:
    """votes = [(uid, 후보, 표)] — 당선자에게 표를 가장 많이 준 사람 (관리자 계정은 순위에서 뺀다, 같으면 먼저 투표한 사람)."""
    best = None
    for u, c, v in votes:
        if c == winner and u not in release.RANK_EXCLUDE and (best is None or v > best[1]):
            best = (u, v)
    return best[0] if best else None


def active_from(r: Optional[dict]) -> tuple[dict[str, float], dict[int, int]]:
    """끝난 대선 → ({공약 key: 배수}, {지지자: 몇 배}). 탄핵됐으면 아무것도 없다."""
    if not r or r.get("winner") is None or r.get("impeached"):
        return {}, {}
    w = r["cands"][r["winner"]]
    voters = {int(u): 1 for u in r.get("supporters", [])}
    if r.get("top") is not None:
        voters[int(r["top"])] = TOP_FACTOR
    return {k: 1 + float(p) for k, p in w["pledges"] if k in PLEDGES}, voters


_TABLES = (
    # 회차 하나: status history(미리 돌린 기록) / open(투표 중) / done(끝남 — 마지막 done 이 지금 대통령)
    "CREATE TABLE IF NOT EXISTS elect_rounds (no INTEGER PRIMARY KEY, status TEXT NOT NULL, opens_ts INTEGER NOT NULL DEFAULT 0, "
    "closes_ts INTEGER NOT NULL DEFAULT 0, cands TEXT NOT NULL, shares TEXT, winner INTEGER, next_news_ts INTEGER NOT NULL DEFAULT 0, "
    "approval REAL, impeached INTEGER NOT NULL DEFAULT 0, term_next_ts INTEGER NOT NULL DEFAULT 0, top INTEGER)",
    "CREATE TABLE IF NOT EXISTS elect_votes (no INTEGER NOT NULL, uid INTEGER NOT NULL, cand INTEGER NOT NULL, "
    "votes INTEGER NOT NULL, paid INTEGER NOT NULL DEFAULT 0, ts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(no, uid))",
    "CREATE TABLE IF NOT EXISTS elect_bets (id INTEGER PRIMARY KEY AUTOINCREMENT, no INTEGER NOT NULL, uid INTEGER NOT NULL, "
    "cand INTEGER NOT NULL, amount INTEGER NOT NULL, odds REAL NOT NULL, ts INTEGER NOT NULL, paid INTEGER, caught INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS elect_news (id INTEGER PRIMARY KEY AUTOINCREMENT, no INTEGER NOT NULL, ts INTEGER NOT NULL, "
    "text TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS lobby_threads (guild_id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL)",   # 서버마다 대선 뉴스 스레드
    "CREATE TABLE IF NOT EXISTS elect_flags (key TEXT PRIMARY KEY, ts INTEGER NOT NULL)",   # 한 번만 하는 일 (restart)
)
_COLS = "no, status, opens_ts, closes_ts, cands, shares, winner, next_news_ts, approval, impeached, term_next_ts, top"


class LobbyDB(EconomyDB):
    def _init_db(self):
        super()._init_db()
        con = self._connect()
        try:
            for ddl in _TABLES:
                con.execute(ddl)
            if not con.execute("SELECT 1 FROM elect_rounds LIMIT 1").fetchone():
                for h in make_history():
                    con.execute("INSERT INTO elect_rounds(no, status, cands, shares, winner) VALUES(?,?,?,?,?)",
                                (h["no"], "history", json.dumps(h["cands"], ensure_ascii=False), json.dumps(h["shares"]), h["winner"]))
            # 3.9.1: 첫 대선 개표를 11/29 → 11/22 21:00 으로 당겼다 — 테스트 서버에서 이미 열린 제16회도 맞춘다
            con.execute("UPDATE elect_rounds SET closes_ts=? WHERE status='open' AND no<=? AND closes_ts>?",
                        (FIRST_CLOSE, FIRST_NO, FIRST_CLOSE))
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _row(r) -> dict:
        return {"no": int(r[0]), "status": r[1], "opens_ts": int(r[2]), "closes_ts": int(r[3]), "cands": json.loads(r[4]),
                "shares": json.loads(r[5]) if r[5] else None, "winner": r[6], "next_news_ts": int(r[7]),
                "approval": r[8], "impeached": bool(r[9]), "term_next_ts": int(r[10]), "top": r[11]}

    @classmethod
    def _rounds(cls, con, where: str = "status IN ('history', 'done')") -> list[dict]:
        return [cls._row(r) for r in con.execute(f"SELECT {_COLS} FROM elect_rounds WHERE {where} ORDER BY no")]

    @classmethod
    def _current(cls, con, now_ts: int, rng=random) -> dict:
        """열려 있는 대선 (없으면 새로 연다)."""
        r = con.execute(f"SELECT {_COLS} FROM elect_rounds WHERE status='open' ORDER BY no DESC LIMIT 1").fetchone()
        if r:
            return cls._row(r)
        past = cls._rounds(con)
        last = past[-1] if past else None
        no = (last["no"] if last else 0) + 1
        cands = make_candidates(party_base(past), last["cands"][last["winner"]]["party"] if last else None, rng)
        closes = FIRST_CLOSE if no <= FIRST_NO and now_ts < FIRST_CLOSE else close_after(now_ts)
        con.execute("INSERT INTO elect_rounds(no, status, opens_ts, closes_ts, cands, next_news_ts) VALUES(?,?,?,?,?,?)",
                    (no, "open", int(now_ts), closes, json.dumps(cands, ensure_ascii=False), int(now_ts) + rng.randint(*NEWS_GAP)))
        con.execute("INSERT INTO elect_news(no, ts, text) VALUES(?,?,?)",
                    (no, int(now_ts), f"제{no}회 축구 봇 대선 후보 등록 마감. 후보 {len(cands)}명이 선거전에 뛰어들었습니다"))
        return cls._row(con.execute(f"SELECT {_COLS} FROM elect_rounds WHERE no=?", (no,)).fetchone())

    async def restart(self, now_ts: int, rng=random) -> Optional[dict]:
        """3.9.1 공개 때 한 번: 테스트 서버에서 열려 있던 대선은 없던 일로 하고 (투표에 낸 돈 · 베팅 돌려줌) 지금 새로 연다.
        → {"no", "closes_ts", "refunds": {uid: 돈}} (이미 했으면 None)."""
        def fn(con):
            if con.execute("SELECT 1 FROM elect_flags WHERE key='restart_391'").fetchone():
                return None
            con.execute("INSERT INTO elect_flags(key, ts) VALUES('restart_391', ?)", (int(now_ts),))
            refunds: dict[int, int] = {}
            for (no,) in con.execute("SELECT no FROM elect_rounds WHERE status='open'").fetchall():
                for uid, m in con.execute("SELECT uid, paid FROM elect_votes WHERE no=?", (no,)).fetchall():
                    refunds[int(uid)] = refunds.get(int(uid), 0) + int(m)
                for uid, m in con.execute("SELECT uid, amount FROM elect_bets WHERE no=? AND paid IS NULL", (no,)).fetchall():
                    refunds[int(uid)] = refunds.get(int(uid), 0) + int(m)
                for t in ("elect_votes", "elect_bets", "elect_news", "elect_rounds"):
                    con.execute(f"DELETE FROM {t} WHERE no=?", (no,))
            for uid, m in refunds.items():
                if m:
                    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (uid,))
                    con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (m, uid))
            e = self._current(con, now_ts, rng)
            return {"no": e["no"], "closes_ts": e["closes_ts"], "refunds": {u: m for u, m in refunds.items() if m}}
        return await self._tx(fn)

    @staticmethod
    def _votes(con, no: int, n: int) -> list[int]:
        out = [0] * n
        for c, v in con.execute("SELECT cand, SUM(votes) FROM elect_votes WHERE no=? GROUP BY cand", (no,)):
            if 0 <= int(c) < n:
                out[int(c)] = int(v)
        return out

    @staticmethod
    def _supporters(con, no: int, winner: int) -> list[int]:
        return [int(u) for (u,) in con.execute("SELECT uid FROM elect_votes WHERE no=? AND cand=?", (no, winner))]

    @classmethod
    def _president(cls, con) -> Optional[dict]:
        """지금 대통령 (마지막으로 끝난 대선, 플레이어가 참여한 것) + 지지자."""
        r = con.execute(f"SELECT {_COLS} FROM elect_rounds WHERE status='done' ORDER BY no DESC LIMIT 1").fetchone()
        if not r:
            return None
        e = cls._row(r)
        e["supporters"] = cls._supporters(con, e["no"], e["winner"])
        return e

    async def state(self, uid: int, now_ts: int) -> dict:
        """지금 대선 + 지지율 · 당선 확률 · 배당 · 내 표 · 내 베팅 · 최근 뉴스 · 지난 기록 · 지금 대통령."""
        def fn(con):
            e = self._current(con, now_ts)
            n = len(e["cands"])
            e["votes"] = self._votes(con, e["no"], n)
            e["support"] = support(e["cands"], e["votes"])
            e["chance"] = win_chances(e["cands"], e["votes"])
            e["odds"] = [odds_of(p) for p in e["chance"]]
            r = con.execute("SELECT cand, votes FROM elect_votes WHERE no=? AND uid=?", (e["no"], int(uid))).fetchone()
            e["my_vote"] = (int(r[0]), int(r[1])) if r else None
            e["my_bets"] = [(int(c), int(a), float(o)) for c, a, o in con.execute(
                "SELECT cand, amount, odds FROM elect_bets WHERE no=? AND uid=? ORDER BY id", (e["no"], int(uid)))]
            e["news"] = [{"ts": int(t), "text": x} for t, x in con.execute(
                "SELECT ts, text FROM elect_news WHERE no>=? ORDER BY id DESC LIMIT 6", (e["no"] - 1,))]
            past = self._rounds(con)
            e["history"] = past
            e["wins"] = party_wins(past)
            e["president"] = self._president(con)
            return e
        return await self._tx(fn)

    async def president(self) -> Optional[dict]:
        return await self._tx(self._president)

    async def vote(self, uid: int, cand: int, n: int, now_ts: int) -> dict:
        """후보 cand 에게 n 표. 지지 후보는 한 명 (처음 투표한 후보). reason: cand / other(다른 후보를 이미 지지) /
        n / max(left) / closed / balance(cost, have)."""
        def fn(con):
            e = self._current(con, now_ts)
            if not 0 <= cand < len(e["cands"]):
                return {"ok": False, "reason": "cand"}
            if now_ts >= e["closes_ts"]:
                return {"ok": False, "reason": "closed"}
            r = con.execute("SELECT cand, votes FROM elect_votes WHERE no=? AND uid=?", (e["no"], int(uid))).fetchone()
            had = int(r[1]) if r else 0
            if r and int(r[0]) != cand:
                return {"ok": False, "reason": "other", "cand": e["cands"][int(r[0])]}
            if n < 1:
                return {"ok": False, "reason": "n"}
            if had + n > VOTE_MAX:
                return {"ok": False, "reason": "max", "left": VOTE_MAX - had}
            cost = votes_cost(had, n)
            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(uid),))
            bal = int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (int(uid),)).fetchone()[0])
            if bal < cost:
                return {"ok": False, "reason": "balance", "cost": cost, "have": bal}
            if cost:
                con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (cost, int(uid)))
            con.execute("INSERT INTO elect_votes(no, uid, cand, votes, paid, ts) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(no, uid) DO UPDATE SET votes = votes + excluded.votes, paid = paid + excluded.paid",
                        (e["no"], int(uid), int(cand), int(n), cost, int(now_ts)))
            return {"ok": True, "cand": e["cands"][cand], "n": int(n), "votes": had + n, "cost": cost, "balance": bal - cost}
        return await self._tx(fn)

    async def bet(self, uid: int, cand: int, amount: int, now_ts: int) -> dict:
        """후보 cand 가 당선된다에 amount 원. 배당은 지금 당선 확률로 정하고 고정. reason: cand / amount / closed / balance(have)."""
        def fn(con):
            e = self._current(con, now_ts)
            if not 0 <= cand < len(e["cands"]):
                return {"ok": False, "reason": "cand"}
            if amount < BET_MIN:
                return {"ok": False, "reason": "amount"}
            if now_ts >= e["closes_ts"]:
                return {"ok": False, "reason": "closed"}
            con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(uid),))
            bal = int(con.execute("SELECT balance FROM wallets WHERE user_id=?", (int(uid),)).fetchone()[0])
            if bal < amount:
                return {"ok": False, "reason": "balance", "have": bal}
            o = odds_of(win_chances(e["cands"], self._votes(con, e["no"], len(e["cands"])))[cand])
            con.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (int(amount), int(uid)))
            con.execute("INSERT INTO elect_bets(no, uid, cand, amount, odds, ts) VALUES(?,?,?,?,?,?)",
                        (e["no"], int(uid), int(cand), int(amount), o, int(now_ts)))
            return {"ok": True, "cand": e["cands"][cand], "amount": int(amount), "odds": o, "win": int(amount * o),
                    "balance": bal - int(amount)}
        return await self._tx(fn)

    async def news_tick(self, now_ts: int, rng=random) -> Optional[dict]:
        """선거운동 뉴스 시각이 지났으면 사건 하나 → {no, text, support, cand} (아니면 None)."""
        def fn(con):
            e = self._current(con, now_ts, rng)
            if now_ts < e["next_news_ts"] or now_ts >= e["closes_ts"]:
                return None
            i = rng.randrange(len(e["cands"]))
            good = rng.random() < 0.5
            tpl, lo, hi = rng.choice(NEWS_GOOD if good else NEWS_BAD)
            e["cands"][i]["events"] += rng.randint(lo, hi) * (1 if good else -1)
            text = tpl.format(n=e["cands"][i]["name"])
            con.execute("UPDATE elect_rounds SET cands=?, next_news_ts=? WHERE no=?",
                        (json.dumps(e["cands"], ensure_ascii=False), int(now_ts) + rng.randint(*NEWS_GAP), e["no"]))
            con.execute("INSERT INTO elect_news(no, ts, text) VALUES(?,?,?)", (e["no"], int(now_ts), text))
            sup = support(e["cands"], self._votes(con, e["no"], len(e["cands"])))
            return {"no": e["no"], "text": text, "cand": i, "support": sup[i]}
        return await self._tx(fn)

    async def term_tick(self, now_ts: int, rng=random) -> Optional[dict]:
        """대통령 임기 중 뉴스 → {no, text, approval, impeached} (아니면 None). 능력이 좋을수록 좋은 뉴스가 많다."""
        def fn(con):
            p = self._president(con)
            if not p or p["impeached"] or now_ts < p["term_next_ts"]:
                return None
            w = p["cands"][p["winner"]]
            good = rng.random() < 0.35 + 0.03 * w["ability"]
            pledge = pledge_text(*rng.choice(w["pledges"]))
            text = rng.choice(TERM_GOOD if good else TERM_BAD).format(n=w["name"], p=pledge)
            a = max(0.0, min(100.0, float(p["approval"] or TERM_START) + rng.randint(*TERM_DELTA) * (1 if good else -1)))
            imp = a < IMPEACH_AT
            text += f". 국정 지지율 {a:.0f}%"
            con.execute("UPDATE elect_rounds SET approval=?, impeached=?, term_next_ts=? WHERE no=?",
                        (a, 1 if imp else 0, int(now_ts) + rng.randint(*TERM_GAP), p["no"]))
            con.execute("INSERT INTO elect_news(no, ts, text) VALUES(?,?,?)", (p["no"], int(now_ts), text))
            out = {"no": p["no"], "text": text, "approval": a, "impeached": imp, "good": good, "name": w["name"]}
            if imp:
                out["impeach_text"] = IMPEACH_TEXT.format(n=w["name"], a=round(a))
                con.execute("INSERT INTO elect_news(no, ts, text) VALUES(?,?,?)", (p["no"], int(now_ts), out["impeach_text"]))
            return out
        return await self._tx(fn)

    async def settle(self, now_ts: int, rng=random) -> Optional[dict]:
        """마감이 지난 대선을 개표 · 정산하고 다음 대선을 연다 → {no, winner, cand, shares, paid, caught, top, ...} (아니면 None).
        한 트랜잭션이라 두 번 정산되지 않는다."""
        def fn(con):
            r = con.execute(f"SELECT {_COLS} FROM elect_rounds WHERE status='open' ORDER BY no DESC LIMIT 1").fetchone()
            if not r:
                return None
            e = self._row(r)
            if now_ts < e["closes_ts"]:
                return None
            n = len(e["cands"])
            votes = self._votes(con, e["no"], n)
            shares = final_shares(e["cands"], votes, rng)
            w = max(range(n), key=lambda i: shares[i])
            bets = [(int(i), int(u), int(c), int(a), float(o)) for i, u, c, a, o in con.execute(
                "SELECT id, uid, cand, amount, odds FROM elect_bets WHERE no=?", (e["no"],))]
            caught = {u for u in {u for _, u, c, _, _ in bets if c == w} if rng.random() < BET_CAUGHT}
            paid = payouts([(u, c, a, o) for _, u, c, a, o in bets], w, caught)
            for i, u, c, a, o in bets:
                con.execute("UPDATE elect_bets SET paid=?, caught=? WHERE id=?",
                            (int(a * o) if c == w and u not in caught else 0, 1 if u in caught and c == w else 0, i))
            for u, m in paid.items():
                con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (u,))
                con.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (m, u))
            vrows = [(int(u), int(c), int(v)) for u, c, v in con.execute(
                "SELECT uid, cand, votes FROM elect_votes WHERE no=? ORDER BY ts, rowid", (e["no"],))]
            top = top_voter(vrows, w)
            con.execute("UPDATE elect_rounds SET status='done', shares=?, winner=?, approval=?, impeached=0, term_next_ts=?, top=? "
                        "WHERE no=?", (json.dumps([round(s, 1) for s in shares]), w, TERM_START,
                                       int(now_ts) + rng.randint(*TERM_GAP), top, e["no"]))
            c = e["cands"][w]
            con.execute("INSERT INTO elect_news(no, ts, text) VALUES(?,?,?)",
                        (e["no"], int(now_ts), f"{c['name']} ({c['party']}) 후보 당선! 득표율 {shares[w]:.1f}%"))
            nxt = self._current(con, now_ts, rng)
            people = {u for _, u, _, _, _ in bets} | {u for u, _, _ in vrows}
            return {"no": e["no"], "winner": w, "cand": c, "cands": e["cands"], "shares": shares, "paid": paid,
                    "caught": sorted(caught), "top": top, "supporters": [u for u, cc, _ in vrows if cc == w],
                    "votes": sum(votes), "bets": sum(a for _, _, _, a, _ in bets), "people": len(people),
                    "next": nxt["no"], "next_closes": nxt["closes_ts"], "impeached": False}
        return await self._tx(fn)
