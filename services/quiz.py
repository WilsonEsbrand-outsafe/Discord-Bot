# services/quiz.py
# 축구 퀴즈 — 문제 생성 / 정답 판정 / 점수 계산 / 기록 저장. Discord UI 는 cogs/quiz.py.
import asyncio
import difflib
import json
import random
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
QUIZ_DIR = BASE_DIR / "quiz_data"
DB_PATH = BASE_DIR / "data" / "economy.sqlite3"

KST = 9 * 3600
TYPED_LIMIT = 60        # 주관식 제한 시간(초)
CHOICE_LIMIT = 20       # 객관식 제한 시간(초)
PAID_PER_DAY = 20       # 하루 상금 지급 횟수 (전 서버 합산)
MONEY_PER_POINT = 100   # 1점 = 100원
DAILY_MULT = 2          # 오늘의 퀴즈 점수·상금 배수

DIFF_MULT = {"easy": 1.0, "normal": 1.5, "hard": 2.0}
DIFF_LABEL = {"easy": "쉬움", "normal": "보통", "hard": "어려움"}

KINDS = {
    "player":   "🕵️ 선수 맞히기",
    "career":   "🔀 커리어 맞히기",
    "transfer": "💸 이적시장 퀴즈",
    "trivia":   "🧠 축구 상식",
}


def day_key(ts: int) -> int:
    return (int(ts) + KST) // 86400


# ───────────── 데이터 ─────────────

def _load(name: str) -> list:
    return json.loads((QUIZ_DIR / name).read_text(encoding="utf-8"))

PLAYERS: list = _load("players.json")
TRANSFERS: list = _load("transfers.json")
TRIVIA: list = _load("trivia.json")


@dataclass
class Question:
    kind: str
    qid: str
    difficulty: str
    lines: List[str]                         # 처음부터 보이는 내용
    hints: List[str] = field(default_factory=list)
    choices: Optional[List[str]] = None      # 객관식이면 보기
    answer_idx: int = -1                     # 객관식 정답 위치
    answer: str = ""                         # 정답 표시용
    aliases: List[str] = field(default_factory=list)

    @property
    def limit(self) -> int:
        return CHOICE_LIMIT if self.choices else TYPED_LIMIT


def _career_difficulty(n: int) -> str:
    return "easy" if n <= 3 else ("normal" if n <= 5 else "hard")


def _pool(kind: str) -> list:
    if kind == "player":
        return [p for p in PLAYERS if p.get("clues")]
    if kind == "career":
        return [p for p in PLAYERS if p.get("career")]
    if kind == "transfer":
        return TRANSFERS
    return TRIVIA


def _qid(kind: str, item: dict) -> str:
    if kind == "transfer":
        return f"{item['player']}|{item['window']}"
    return item["id"]


def pick(kind: str, rng: random.Random, exclude=()) -> Question:
    pool = _pool(kind)
    fresh = [x for x in pool if _qid(kind, x) not in exclude] or pool
    return build(kind, rng.choice(fresh), rng)


def build(kind: str, item: dict, rng: random.Random) -> Question:
    qid = _qid(kind, item)
    if kind == "player":
        return Question(
            kind, qid, item["difficulty"],
            lines=[f"국적: **{item['nation']}**", f"포지션: **{item['position']}**", f"단서: {item['clues'][0]}"],
            hints=item["clues"][1:],
            answer=item["answer"], aliases=item["aliases"],
        )
    if kind == "career":
        path = "\n↓\n".join(item["career"])
        return Question(
            kind, qid, _career_difficulty(len(item["career"])),
            lines=[f"```\n{path}\n```", "이 커리어의 주인공은?"],
            hints=[f"국적: {item['nation']}", f"포지션: {item['position']}"],
            answer=item["answer"], aliases=item["aliases"],
        )
    if kind == "transfer":
        to_dest = rng.random() < 0.6
        key, other = ("to", "from") if to_dest else ("from", "to")
        correct = item[key]
        clubs = sorted({t[key] for t in TRANSFERS} - {correct, item[other]})
        choices = rng.sample(clubs, 3) + [correct]
        rng.shuffle(choices)
        if to_dest:
            q = f"**{item['player']}**의 새 소속팀은?\n이전 소속: {item['from']}"
        else:
            q = f"**{item['player']}**이(가) 떠나온 팀은?\n새 소속: {item['to']}"
        return Question(
            kind, qid, "easy",
            lines=[f"📅 {item['window']} 이적시장 · {item['type']}", q],
            choices=choices, answer_idx=choices.index(correct), answer=correct,
        )
    correct = item["choices"][0]
    choices = list(item["choices"])
    rng.shuffle(choices)
    return Question(
        kind, qid, item["difficulty"], lines=[item["q"]],
        choices=choices, answer_idx=choices.index(correct), answer=correct,
    )


def daily_question(day: int) -> Question:
    """그날의 문제는 모든 서버·유저에게 같다 (날짜로 시드)."""
    rng = random.Random(f"daily-{day}")
    return pick(rng.choice(["player", "career", "transfer", "trivia"]), rng)


# ───────────── 정답 판정 ─────────────

_STRIP = re.compile(r"[\s.\-'’·_]")

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))   # é → e
    return _STRIP.sub("", unicodedata.normalize("NFC", s).lower())


def is_correct(user_input: str, q: Question) -> bool:
    guess = _norm(user_input)
    if len(guess) < 2:
        return False
    cands = {_norm(a) for a in [q.answer, *q.aliases]}
    if guess in cands:
        return True
    # 오타 1~2글자 허용. 짧은 이름은 오탐이 커서 제외한다.
    return any(len(c) >= 4 and difflib.SequenceMatcher(None, guess, c).ratio() >= 0.85 for c in cands)


# ───────────── 점수 ─────────────

def streak_bonus(streak: int) -> int:
    return {0: 0, 1: 0, 2: 10, 3: 20, 4: 30}.get(streak, 50)


def calc_score(correct: bool, hints: int, difficulty: str, elapsed: float, limit: int, streak: int) -> int:
    """streak = 이번 정답을 포함한 연승 수."""
    if not correct:
        return 0
    base = max(20, 100 - 20 * hints)
    time_bonus = int(20 * max(0.0, 1 - elapsed / limit))
    return int(base * DIFF_MULT[difficulty]) + time_bonus + streak_bonus(streak)


# ───────────── DB ─────────────

class QuizDB:
    def __init__(self):
        from services.economy_db import EconomyDB
        EconomyDB()  # 상금 지급에 쓰는 wallets 테이블 보장 (스키마는 economy_db 한 곳에서만 정의)
        self._lock = asyncio.Lock()
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS quiz_stats (
                    user_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    total_score INTEGER NOT NULL DEFAULT 0,
                    games_played INTEGER NOT NULL DEFAULT 0,
                    games_won INTEGER NOT NULL DEFAULT 0,
                    current_streak INTEGER NOT NULL DEFAULT 0,
                    best_streak INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, guild_id)
                );
                CREATE TABLE IF NOT EXISTS quiz_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    game_type TEXT NOT NULL,
                    question_id TEXT NOT NULL,
                    correct INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    reward INTEGER NOT NULL,
                    time_taken REAL NOT NULL,
                    day INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_quiz_results_user_day ON quiz_results(user_id, day);
                CREATE TABLE IF NOT EXISTS quiz_daily (
                    user_id INTEGER PRIMARY KEY,
                    last_day INTEGER NOT NULL DEFAULT 0,
                    streak INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            con.commit()
        finally:
            con.close()

    def _connect(self):
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute("PRAGMA journal_mode=WAL;")
        return con

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

    async def start_daily(self, user_id: int, now_ts: int):
        """오늘의 퀴즈 참가권을 쓴다. 이미 썼으면 None, 아니면 갱신된 연속 참여 일수."""
        today = day_key(now_ts)
        def fn(con):
            con.execute("INSERT OR IGNORE INTO quiz_daily(user_id) VALUES(?)", (user_id,))
            last, streak = con.execute("SELECT last_day, streak FROM quiz_daily WHERE user_id=?", (user_id,)).fetchone()
            if last == today:
                return None
            streak = streak + 1 if last == today - 1 else 1
            con.execute("UPDATE quiz_daily SET last_day=?, streak=? WHERE user_id=?", (today, streak, user_id))
            return streak
        return await self._tx(fn)

    async def record(self, user_id: int, guild_id: int, q: Question, correct: bool, hints: int,
                     elapsed: float, now_ts: int, daily: bool = False) -> dict:
        today = day_key(now_ts)
        def fn(con):
            con.execute("INSERT OR IGNORE INTO quiz_stats(user_id, guild_id) VALUES(?, ?)", (user_id, guild_id))
            cur, best = con.execute(
                "SELECT current_streak, best_streak FROM quiz_stats WHERE user_id=? AND guild_id=?",
                (user_id, guild_id),
            ).fetchone()
            streak = cur + 1 if correct else 0
            score = calc_score(correct, hints, q.difficulty, elapsed, q.limit, streak)
            if daily:
                score *= DAILY_MULT

            paid_today = con.execute(
                "SELECT COUNT(*) FROM quiz_results WHERE user_id=? AND day=? AND reward>0 AND game_type!='daily'",
                (user_id, today),
            ).fetchone()[0]
            paid = score > 0 and (daily or paid_today < PAID_PER_DAY)
            reward = score * MONEY_PER_POINT if paid else 0

            con.execute(
                """UPDATE quiz_stats SET total_score=total_score+?, games_played=games_played+1,
                   games_won=games_won+?, current_streak=?, best_streak=? WHERE user_id=? AND guild_id=?""",
                (score, int(correct), streak, max(best, streak), user_id, guild_id),
            )
            con.execute(
                """INSERT INTO quiz_results(user_id, guild_id, game_type, question_id, correct, score, reward,
                   time_taken, day, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (user_id, guild_id, "daily" if daily else q.kind, q.qid, int(correct), score, reward,
                 round(elapsed, 2), today, now_ts),
            )
            if reward:
                con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (user_id,))
                con.execute("UPDATE wallets SET balance=balance+? WHERE user_id=?", (reward, user_id))
            return {
                "score": score, "reward": reward, "streak": streak, "best": max(best, streak),
                "paid_left": max(0, PAID_PER_DAY - paid_today - (1 if paid and not daily else 0)),
            }
        return await self._tx(fn)

    async def leaderboard(self, guild_id: int, limit: int = 10) -> list:
        return await self._tx(lambda con: con.execute(
            """SELECT user_id, total_score, games_played, games_won FROM quiz_stats
               WHERE guild_id=? AND games_played>0 ORDER BY total_score DESC LIMIT ?""",
            (guild_id, limit),
        ).fetchall())

    async def profile(self, user_id: int, guild_id: int) -> dict:
        def fn(con):
            row = con.execute(
                """SELECT total_score, games_played, games_won, current_streak, best_streak
                   FROM quiz_stats WHERE user_id=? AND guild_id=?""", (user_id, guild_id),
            ).fetchone() or (0, 0, 0, 0, 0)
            rank = None
            if row[1]:
                rank = con.execute(
                    "SELECT COUNT(*)+1 FROM quiz_stats WHERE guild_id=? AND games_played>0 AND total_score>?",
                    (guild_id, row[0]),
                ).fetchone()[0]
            d = con.execute("SELECT last_day, streak FROM quiz_daily WHERE user_id=?", (user_id,)).fetchone() or (0, 0)
            return dict(zip(("total_score", "played", "won", "streak", "best"), row),
                        rank=rank, daily_last=d[0], daily_streak=d[1])
        return await self._tx(fn)
