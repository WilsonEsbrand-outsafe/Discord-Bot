"""팩 가격/확률 밸런스 점검 도구.

실제 스폰·가치·추첨 로직을 그대로 import해서 돌린다.
(예전 sim_pack.py는 공식을 복사해 둬서 본체와 어긋나 있었다.)

실행:
    .venv/Scripts/python.exe tools/pack_balance.py                      # 스폰 분포로 시뮬
    .venv/Scripts/python.exe tools/pack_balance.py data/economy.sqlite3 # 실제 서버 DB로 시뮬

콘솔이 cp949일 수 있어 이모지 없이 출력한다.
"""
import os
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.player_market_db as pmdb

_WORK = Path(tempfile.mkdtemp())
pmdb.DB_PATH = _WORK / "balance.sqlite3"

from services.player_market_db import (  # noqa: E402
    JACKPOT_PITY, JACKPOT_PROB, JACKPOT_RANGE, PACKS, POOL_SIZE,
    PlayerMarketDB, _draw_from_pool,
)

DRAWS = 40_000
LABELS = [("대박", 3.0), ("이득", 1.3), ("본전", 0.9), ("손해", 0.5), ("폭망", 0.0)]


def label_for(price: int, pack_price: int) -> str:
    ratio = price / pack_price if pack_price > 0 else 0
    for name, thr in LABELS:
        if ratio >= thr:
            return name
    return LABELS[-1][0]


def money(n: float) -> str:
    n = int(n)
    if n >= 100_000_000:
        return "%.2f억" % (n / 100_000_000)
    if n >= 10_000:
        return "%d만" % (n / 10_000)
    return "%d" % n


def build_pool(db_path: str | None) -> sqlite3.Connection:
    """시뮬용 DB를 만든다. db_path가 있으면 그 사본을 쓴다."""
    pm = PlayerMarketDB()          # 스키마 생성
    if db_path:
        shutil.copy(db_path, pmdb.DB_PATH)
        return sqlite3.connect(pmdb.DB_PATH)

    con = sqlite3.connect(pmdb.DB_PATH)
    for _ in range(POOL_SIZE):
        p = pm._spawn_player(0, 0)
        con.execute(
            "INSERT INTO pm_players(player_id,name,nation,position,age,ovr,pot,pot_grade,"
            "base_value,retired,created_month,updated_ts) VALUES(?,?,?,?,?,?,?,?,?,0,0,0)",
            (p["player_id"], p["name"], p["nation"], p["position"], p["age"],
             p["ovr"], p["pot"], p["pot_grade"], p["base_value"]),
        )
        con.execute(
            "INSERT INTO pm_market(player_id,price,floor_price,ceil_price,prev_dir,last_update_ts)"
            " VALUES(?,?,?,?,0,0)",
            (p["player_id"], p["price"], p["floor_price"], p["ceil_price"]),
        )
    con.commit()
    return con


def report(con: sqlite3.Connection) -> None:
    pool = sorted(
        (int(r[0]) for r in con.execute(
            "SELECT COALESCE(m.price,p.base_value) FROM pm_players p "
            "LEFT JOIN pm_market m ON m.player_id=p.player_id "
            "WHERE p.retired=0 AND p.player_id NOT LIKE 'AMT_%'")),
        reverse=True,
    )
    print("\n선수 풀 %d명 | 최고 %s / 중앙 %s / 최저 %s"
          % (len(pool), money(pool[0]), money(statistics.median(pool)), money(pool[-1])))
    print("  상위 백분위: " + "  ".join(
        "%d%%=%s" % (q, money(pool[min(len(pool) - 1, int(len(pool) * q / 100))]))
        for q in (1, 5, 10, 25, 50)))
    print("  잭팟 %.1f%% · 대상 단가의 %.1f~%.1f배 · 천장 %d장"
          % (JACKPOT_PROB * 100, JACKPOT_RANGE[0], JACKPOT_RANGE[1], JACKPOT_PITY))

    hdr = ("  %-10s %9s %5s %9s %6s %6s   " % ("팩", "단가", "풀", "평균획득", "EV", "잭팟")
           + " ".join("%5s" % n for n, _ in LABELS))
    print("\n" + hdr)
    print("  " + "-" * (len(hdr) - 2))

    for name, cfg in PACKS.items():
        price = int(cfg["price"])
        pity = 0
        values, jackpots = [], 0
        # 천장이 실제로 도는 것까지 반영하려면 pity를 이어서 굴려야 한다.
        while len(values) < DRAWS:
            status, picks, pity = _draw_from_pool(con, cfg, price, min(200, DRAWS - len(values)), pity)
            if status != "OK":
                break
            for row, hit in picks:
                values.append(row[1])
                jackpots += hit
        if not values:
            print("  %-10s %9s %5d  (해당 구간에 선수 없음)" % (name, money(price), 0))
            continue

        lo = int(cfg.get("min_price") or 0)
        hi = cfg.get("max_price")
        depth = sum(1 for v in pool if v >= lo and (hi is None or v <= hi))
        avg = statistics.mean(values)
        cnt = Counter(label_for(v, price) for v in values)
        print("  %-10s %9s %5d %9s %5.2fx %5.1f%%   " % (
            name, money(price), depth, money(avg), avg / price, jackpots / len(values) * 100)
            + " ".join("%4.1f%%" % (cnt.get(n, 0) / len(values) * 100) for n, _ in LABELS))

    print("\n  EV = 획득 선수의 평균 현재가 / 팩 단가. 1.00x 미만이면 팩이 유저에게 손해다.")
    print("  목표: EV 0.90~0.98x (완만한 하우스 엣지), 대박 2~5%%.")


if __name__ == "__main__":
    random.seed(int(os.getenv("SEED", "7")))
    src = sys.argv[1] if len(sys.argv) > 1 else None
    print("=== %s ===" % (("실제 DB: " + src) if src else "스폰 분포 시뮬레이션 (%d명)" % POOL_SIZE))
    report(build_pool(src))
