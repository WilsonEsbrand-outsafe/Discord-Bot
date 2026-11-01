# services/agent.py — 3.7.0 🕴️ 선수 에이전트 (/스카우트 새 방식, release.preview_370 서버)
# 스카우트가 돈을 바로 주지 않는다: 가상의 선수를 발견 → NPC 구단 3곳이 몸값을 부른다 → 어디로 보낼지 고른다 →
# 성사되면 수수료(× 스카우트 레벨 배율), 실패하면 0원. 선수는 리포트 안에서만 쓰이는 가상 선수라
# 선수 풀 · 이적시장 · 보유 카드와는 아무 상관이 없다 (규완님: "스카우트랑 시장을 연결할 이유는 없다").
import json
import random
from typing import Optional

STAR_WEIGHTS = ((38, 30, 20, 9, 3), (34, 30, 21, 11, 4), (30, 29, 23, 13, 5), (26, 28, 25, 15, 6), (22, 27, 27, 17, 7))
STAR_FEE = {1: 30_000, 2: 50_000, 3: 80_000, 4: 140_000, 5: 260_000}   # 별마다 기준 수수료 (Lv.1)
# 제안 등급: (이름 목록, 수수료 배수, 기본 성사 확률, 별 하나당 확률 +)
OFFER_TIERS = (
    (("동네 구단", "아마추어 클럽", "3부 리그 구단"), 0.6, 0.86, 0.02),
    (("2부 리그 구단", "1부 중위권 구단", "승격팀"), 1.0, 0.60, 0.04),
    (("빅클럽", "유럽 명문", "챔피언스리그 단골"), 2.3, 0.22, 0.06),
)
NATIONS = ("🇧🇷 브라질", "🇦🇷 아르헨티나", "🇫🇷 프랑스", "🇳🇬 나이지리아", "🇪🇸 스페인", "🇯🇵 일본", "🇰🇷 한국", "🏴 잉글랜드",
           "🇵🇹 포르투갈", "🇳🇱 네덜란드", "🇩🇪 독일", "🇮🇹 이탈리아", "🇬🇭 가나", "🇺🇾 우루과이", "🇨🇴 콜롬비아")
_FIRST = ("루카스", "마테우스", "하파엘", "티아구", "엔조", "니코", "다니엘", "하루토", "민준", "이사", "카이", "레오", "오마르",
          "마르코", "펠릭스", "사무엘", "알렉스", "조엘", "에밀", "주니오르")
_LAST = ("실바", "산투스", "가르시아", "디아스", "오카포", "멘사", "모리", "김", "박", "반더르", "로시", "뮐러", "코스타", "페레이라",
         "아보아", "라미레스", "마르틴", "콩고", "타나카", "오르테가")
POS = ("GK", "DF", "MF", "FW")
SCOUT_LINES = ("빠른 발이 인상적이에요.", "왼발 킥이 예사롭지 않아요.", "체격이 단단해요.", "시야가 넓어요.", "투지가 넘쳐요.",
               "볼 컨트롤이 부드러워요.", "제공권이 좋아요.", "슈팅 타이밍이 기가 막혀요.")


def make_report(level: int, rng=random) -> dict:
    """가상의 선수 한 명 + NPC 구단 제안 3개 (수수료는 레벨 배율 전 금액)."""
    lv = max(1, min(len(STAR_WEIGHTS), int(level)))
    stars = rng.choices(range(1, 6), weights=STAR_WEIGHTS[lv - 1])[0]
    base = STAR_FEE[stars]
    offers = []
    for names, mult, p, per_star in OFFER_TIERS:
        fee = int(round(base * mult * rng.uniform(0.85, 1.15), -3))
        offers.append({"club": rng.choice(names), "fee": fee, "prob": round(min(0.95, p + per_star * (stars - 1)), 2)})
    return {"name": f"{rng.choice(_FIRST)} {rng.choice(_LAST)}", "nation": rng.choice(NATIONS), "pos": rng.choice(POS),
            "age": rng.randint(16, 21), "ovr": 50 + stars * 5 + rng.randint(-3, 3), "stars": stars,
            "line": rng.choice(SCOUT_LINES), "offers": offers}


def resolve(report: dict, idx: int, mult: float, rng=random) -> dict:
    """제안 idx 로 보낸다 → {ok, fee(받은 돈), offer}."""
    o = report["offers"][max(0, min(len(report["offers"]) - 1, int(idx)))]
    ok = rng.random() < o["prob"]
    return {"ok": ok, "fee": int(o["fee"] * mult) if ok else 0, "offer": o}


def best_auto(report: dict) -> int:
    """스킵권처럼 고를 수 없을 때: 기대값이 가장 큰 제안."""
    return max(range(len(report["offers"])), key=lambda i: report["offers"][i]["fee"] * report["offers"][i]["prob"])


def expected_per_play(level: int = 1, n: int = 20000, rng=None) -> float:
    rng = rng or random.Random(0)
    tot = 0.0
    for _ in range(n):
        r = make_report(level, rng)
        tot += max(o["fee"] * o["prob"] for o in r["offers"])
    return tot / n


# ── 저장: 아직 보내지 않은 리포트 (다시 /스카우트 해도 같은 리포트 — 좋은 선수 나올 때까지 다시 뽑기 방지) ──
def init_table(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS scout_reports (user_id INTEGER PRIMARY KEY, data TEXT NOT NULL, ts INTEGER NOT NULL)")


def pending(con, uid: int) -> Optional[dict]:
    init_table(con)
    r = con.execute("SELECT data FROM scout_reports WHERE user_id=?", (int(uid),)).fetchone()
    return json.loads(r[0]) if r else None


def save(con, uid: int, report: dict, now_ts: int) -> None:
    init_table(con)
    con.execute("INSERT OR REPLACE INTO scout_reports(user_id, data, ts) VALUES(?,?,?)", (int(uid), json.dumps(report), int(now_ts)))


def take(con, uid: int) -> Optional[dict]:
    """리포트를 꺼내고 지운다 (보내기 버튼 — 한 번만)."""
    r = pending(con, uid)
    if r is not None:
        con.execute("DELETE FROM scout_reports WHERE user_id=?", (int(uid),))
    return r
